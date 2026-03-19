"""SQLite-backed pricing cache.

Provides persistent, TTL-aware storage for ``NormalizedPriceItem``
rows fetched from cloud provider APIs.  All database operations are
async via ``aiosqlite``.

Schema
------
``price_items``
    One row per (provider, sku_id, region, pricing_tier) combination.
    Stores every field of ``NormalizedPriceItem`` plus a ``fetched_at``
    timestamp for TTL calculations.

``fetch_log``
    Tracks when each unique query pattern was last refreshed from the
    live API.  Used to decide cache hit vs. miss without scanning
    ``price_items``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import structlog
from langfuse import observe

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# SQL DDL
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """\
CREATE TABLE IF NOT EXISTS price_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Identity
    provider        TEXT NOT NULL,
    service_name    TEXT NOT NULL,
    service_category TEXT NOT NULL,
    sku_id          TEXT NOT NULL,
    sku_name        TEXT NOT NULL,
    product_name    TEXT NOT NULL,
    meter_name      TEXT NOT NULL DEFAULT '',
    -- Location
    region          TEXT NOT NULL,
    -- Pricing
    retail_price    REAL NOT NULL,
    unit_price      REAL NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    unit_of_measure TEXT NOT NULL,
    pricing_tier    TEXT NOT NULL,
    reservation_term TEXT,
    -- Metadata
    effective_date      TEXT NOT NULL,
    effective_end_date  TEXT,
    is_primary_meter    INTEGER NOT NULL DEFAULT 1,
    attributes_json     TEXT NOT NULL DEFAULT '{}',
    -- Cache bookkeeping
    fetched_at      TEXT NOT NULL,
    UNIQUE(provider, sku_id, region, pricing_tier)
);

CREATE INDEX IF NOT EXISTS idx_price_provider_service_region
    ON price_items(provider, service_name, region);

CREATE INDEX IF NOT EXISTS idx_price_provider_category_region
    ON price_items(provider, service_category, region);

CREATE INDEX IF NOT EXISTS idx_price_sku_name_region
    ON price_items(sku_name, region);

CREATE INDEX IF NOT EXISTS idx_price_fetched_at
    ON price_items(fetched_at);

CREATE TABLE IF NOT EXISTS fetch_log (
    cache_key        TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,
    service_name     TEXT,
    service_category TEXT,
    region           TEXT,
    fetched_at       TEXT NOT NULL,
    item_count       INTEGER NOT NULL DEFAULT 0
);
"""

_UPSERT_ITEM_SQL = """\
INSERT OR REPLACE INTO price_items (
    provider, service_name, service_category, sku_id, sku_name,
    product_name, meter_name, region, retail_price, unit_price,
    currency, unit_of_measure, pricing_tier, reservation_term,
    effective_date, effective_end_date, is_primary_meter,
    attributes_json, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_FETCH_LOG_SQL = """\
INSERT OR REPLACE INTO fetch_log
    (cache_key, provider, service_name, service_category, region,
     fetched_at, item_count)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cache_key(
    provider: str,
    service_name: str | None,
    service_category: str | None,
    region: str | None,
) -> str:
    """Build a deterministic cache key for fetch_log lookups.

    Args:
        provider: Cloud provider value (e.g. ``"azure"``).
        service_name: Provider-native service name, or ``None``.
        service_category: Normalised category value, or ``None``.
        region: Provider-native region, or ``None``.

    Returns:
        A 16-char hex digest.
    """
    raw = f"{provider}|{service_name or ''}|{service_category or ''}|{region or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _item_to_row(item: NormalizedPriceItem, fetched_at: datetime) -> tuple:
    """Serialise a ``NormalizedPriceItem`` into a SQLite-compatible tuple."""
    return (
        item.provider.value,
        item.service_name,
        item.service_category.value,
        item.sku_id,
        item.sku_name,
        item.product_name,
        item.meter_name,
        item.region,
        item.retail_price,
        item.unit_price,
        item.currency,
        item.unit_of_measure,
        item.pricing_tier.value,
        item.reservation_term,
        item.effective_date.isoformat(),
        item.effective_end_date.isoformat() if item.effective_end_date else None,
        1 if item.is_primary_meter else 0,
        json.dumps(item.attributes, default=str),
        fetched_at.isoformat(),
    )


def _row_to_item(row: sqlite3.Row) -> NormalizedPriceItem:
    """Deserialise a SQLite row back into a ``NormalizedPriceItem``.

    Args:
        row: A ``sqlite3.Row`` with column-name access.

    Returns:
        Reconstructed ``NormalizedPriceItem``.
    """
    return NormalizedPriceItem(
        provider=CloudProvider(row["provider"]),
        service_name=row["service_name"],
        service_category=ServiceCategory(row["service_category"]),
        sku_id=row["sku_id"],
        sku_name=row["sku_name"],
        product_name=row["product_name"],
        meter_name=row["meter_name"] or "",
        region=row["region"],
        retail_price=row["retail_price"],
        unit_price=row["unit_price"],
        currency=row["currency"],
        unit_of_measure=row["unit_of_measure"],
        pricing_tier=PricingTier(row["pricing_tier"]),
        reservation_term=row["reservation_term"],
        effective_date=datetime.fromisoformat(row["effective_date"]),
        effective_end_date=(
            datetime.fromisoformat(row["effective_end_date"])
            if row["effective_end_date"]
            else None
        ),
        is_primary_meter=bool(row["is_primary_meter"]),
        attributes=json.loads(row["attributes_json"]) if row["attributes_json"] else {},
    )


# ---------------------------------------------------------------------------
# PricingCache
# ---------------------------------------------------------------------------


class PricingCache:
    """Async SQLite cache for ``NormalizedPriceItem`` rows.

    Manages two tables:

    * ``price_items`` — individual pricing rows keyed by
      (provider, sku_id, region, pricing_tier).
    * ``fetch_log`` — records when each query pattern was last
      refreshed from the live API.

    Usage::

        cache = PricingCache(Path("data/sku_cache.db"))
        await cache.initialize()

        # Check freshness
        if not await cache.is_fresh(cache_key, ttl_hours=24):
            items = fetch_from_provider(...)
            await cache.upsert_items(items, cache_key=cache_key, ...)

        # Query cached items
        items = await cache.query_items(provider="azure", region="eastus")

        await cache.close()
    """

    def __init__(
        self,
        db_path: Path,
        default_ttl_hours: int = 24,
    ) -> None:
        """Initialise the cache (does NOT open the database yet).

        Args:
            db_path: Path to the SQLite database file.
            default_ttl_hours: Default cache time-to-live in hours.
        """
        self._db_path = db_path
        self._default_ttl_hours = default_ttl_hours
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def _get_db(self) -> aiosqlite.Connection:
        """Get or create the database connection.

        Creates the parent directory if it doesn't exist.  Enables
        WAL mode and NORMAL synchronous for better concurrent
        read/write performance.

        Returns:
            An open ``aiosqlite.Connection``.
        """
        if self._db is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(str(self._db_path))
            self._db.row_factory = sqlite3.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
        return self._db

    @observe(name="cache_initialize")
    async def initialize(self) -> None:
        """Create tables and indexes if they don't exist.

        Safe to call multiple times — uses ``CREATE IF NOT EXISTS``.
        """
        log = logger.bind(component="pricing_cache")
        log.info("cache_initializing", db_path=str(self._db_path))

        db = await self._get_db()
        await db.executescript(_CREATE_TABLES_SQL)
        await db.commit()

        log.info("cache_initialized", db_path=str(self._db_path))

    # ------------------------------------------------------------------
    # Freshness check
    # ------------------------------------------------------------------

    @observe(name="cache_is_fresh")
    async def is_fresh(
        self,
        cache_key: str,
        ttl_hours: int | None = None,
    ) -> bool:
        """Check whether a cache entry is still within its TTL.

        Args:
            cache_key: Deterministic key from ``make_cache_key()``.
            ttl_hours: Override TTL (uses instance default if ``None``).

        Returns:
            ``True`` if the entry exists and ``fetched_at`` is within TTL.
        """
        effective_ttl = ttl_hours if ttl_hours is not None else self._default_ttl_hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=effective_ttl)

        db = await self._get_db()
        cursor = await db.execute(
            "SELECT fetched_at FROM fetch_log WHERE cache_key = ?",
            (cache_key,),
        )
        row = await cursor.fetchone()

        if row is None:
            return False

        fetched_at = datetime.fromisoformat(row["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)

        return fetched_at > cutoff

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @observe(name="cache_query_items")
    async def query_items(
        self,
        *,
        provider: str | None = None,
        service_name: str | None = None,
        service_category: str | None = None,
        region: str | None = None,
        sku_name: str | None = None,
        pricing_tier: str | None = None,
        max_results: int = 100,
    ) -> list[NormalizedPriceItem]:
        """Query cached price items with flexible filters.

        All filters are optional — omitted filters are not applied.
        ``sku_name`` uses substring matching (``LIKE %pattern%``),
        all others use exact equality.

        Args:
            provider: Provider value (e.g. ``"azure"``).
            service_name: Exact service name match.
            service_category: Exact category value match.
            region: Exact region match.
            sku_name: Case-insensitive substring match on SKU name.
            pricing_tier: Exact tier value match.
            max_results: Maximum rows to return.

        Returns:
            List of ``NormalizedPriceItem`` ordered by ``unit_price ASC``.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if provider:
            conditions.append("provider = ?")
            params.append(provider)
        if service_name:
            conditions.append("service_name = ?")
            params.append(service_name)
        if service_category:
            conditions.append("service_category = ?")
            params.append(service_category)
        if region:
            conditions.append("region = ?")
            params.append(region)
        if sku_name:
            conditions.append("sku_name LIKE ?")
            params.append(f"%{sku_name}%")
        if pricing_tier:
            conditions.append("pricing_tier = ?")
            params.append(pricing_tier)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM price_items WHERE {where} ORDER BY unit_price ASC LIMIT ?"  # noqa: S608
        params.append(max_results)

        log = logger.bind(component="pricing_cache")
        log.debug("cache_query", where=where, param_count=len(params))

        db = await self._get_db()
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

        items = [_row_to_item(row) for row in rows]
        log.debug("cache_query_result", count=len(items))
        return items

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    @observe(name="cache_upsert_items")
    async def upsert_items(
        self,
        items: list[NormalizedPriceItem],
        *,
        cache_key: str,
        provider: str,
        service_name: str | None = None,
        service_category: str | None = None,
        region: str | None = None,
    ) -> int:
        """Insert-or-replace price items and update the fetch log.

        Uses ``INSERT OR REPLACE`` on the ``(provider, sku_id, region,
        pricing_tier)`` unique constraint — existing rows are fully
        replaced with fresh data.

        Args:
            items: ``NormalizedPriceItem`` instances to store.
            cache_key: Key for ``fetch_log`` bookkeeping.
            provider: Provider value for ``fetch_log``.
            service_name: Service name for ``fetch_log``.
            service_category: Category for ``fetch_log``.
            region: Region for ``fetch_log``.

        Returns:
            Number of items upserted.
        """
        if not items:
            return 0

        log = logger.bind(component="pricing_cache", provider=provider)
        log.info("cache_upsert_started", count=len(items))

        now = datetime.now(timezone.utc)
        db = await self._get_db()

        rows = [_item_to_row(item, now) for item in items]
        await db.executemany(_UPSERT_ITEM_SQL, rows)

        await db.execute(
            _UPSERT_FETCH_LOG_SQL,
            (
                cache_key,
                provider,
                service_name,
                service_category,
                region,
                now.isoformat(),
                len(items),
            ),
        )

        await db.commit()
        log.info("cache_upsert_completed", count=len(items))
        return len(items)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    @observe(name="cache_clear")
    async def clear(self, provider: str | None = None) -> int:
        """Delete cached items, optionally filtered by provider.

        Also clears corresponding ``fetch_log`` entries.

        Args:
            provider: If given, only clear this provider's data.

        Returns:
            Number of ``price_items`` rows deleted.
        """
        log = logger.bind(component="pricing_cache")
        db = await self._get_db()

        if provider:
            cursor = await db.execute(
                "DELETE FROM price_items WHERE provider = ?", (provider,),
            )
            await db.execute(
                "DELETE FROM fetch_log WHERE provider = ?", (provider,),
            )
        else:
            cursor = await db.execute("DELETE FROM price_items")
            await db.execute("DELETE FROM fetch_log")

        deleted = cursor.rowcount
        await db.commit()
        log.info("cache_cleared", provider=provider, deleted=deleted)
        return deleted

    @observe(name="cache_evict_stale")
    async def evict_stale(self, ttl_hours: int | None = None) -> int:
        """Remove items older than the TTL.

        Useful as a periodic maintenance task to keep the database
        compact.

        Args:
            ttl_hours: Override TTL (uses instance default if ``None``).

        Returns:
            Number of rows evicted.
        """
        effective_ttl = ttl_hours if ttl_hours is not None else self._default_ttl_hours
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=effective_ttl)
        ).isoformat()

        log = logger.bind(component="pricing_cache")
        db = await self._get_db()

        cursor = await db.execute(
            "DELETE FROM price_items WHERE fetched_at < ?", (cutoff,),
        )
        await db.execute(
            "DELETE FROM fetch_log WHERE fetched_at < ?", (cutoff,),
        )

        evicted = cursor.rowcount
        await db.commit()
        log.info("cache_evicted_stale", ttl_hours=effective_ttl, evicted=evicted)
        return evicted

    @observe(name="cache_stats")
    async def stats(self) -> dict[str, Any]:
        """Return cache statistics.

        Returns:
            Dict with ``total_items``, ``items_by_provider``,
            ``fetch_entries``, ``oldest_fetch``, ``newest_fetch``,
            ``db_size_bytes``.
        """
        db = await self._get_db()

        cursor = await db.execute("SELECT COUNT(*) AS cnt FROM price_items")
        row = await cursor.fetchone()
        total_items = row["cnt"]

        cursor = await db.execute(
            "SELECT provider, COUNT(*) AS cnt FROM price_items GROUP BY provider",
        )
        items_by_provider = {r["provider"]: r["cnt"] for r in await cursor.fetchall()}

        cursor = await db.execute("SELECT COUNT(*) AS cnt FROM fetch_log")
        row = await cursor.fetchone()
        fetch_entries = row["cnt"]

        cursor = await db.execute(
            "SELECT MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest FROM fetch_log",
        )
        row = await cursor.fetchone()
        oldest_fetch = row["oldest"]
        newest_fetch = row["newest"]

        db_size = self._db_path.stat().st_size if self._db_path.exists() else 0

        return {
            "total_items": total_items,
            "items_by_provider": items_by_provider,
            "fetch_entries": fetch_entries,
            "oldest_fetch": oldest_fetch,
            "newest_fetch": newest_fetch,
            "db_size_bytes": db_size,
        }

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            logger.bind(component="pricing_cache").info("cache_connection_closed")
