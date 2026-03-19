"""Cache-aware pricing facade for agents and engines.

``PricingService`` is the **single entry point** that all agent and
engine code should use for pricing data.  It sits between consumers
(agents, engines) and producers (cloud provider adapters), adding a
transparent SQLite cache layer.

Architecture::

    Agents / Engines
           │
           ▼
      PricingService  ← cache-transparent facade
           │
     ┌─────┴─────┐
     │  SQLite    │  ← NormalizedPriceItem rows + TTL metadata
     │  Cache     │
     └─────┬─────┘
           │ cache miss / expired
           ▼
     Provider Adapters (AWS / Azure / GCP)

Cache behaviour:

* **Hit** — data exists in SQLite and ``fetched_at`` is within the
  tier-appropriate TTL → served directly from the database.
* **Miss** — stale or absent → live API call, results stored in
  SQLite, ``fetch_log`` entry updated.
* **Graceful degradation** — if a live API call fails *and* stale
  data exists, the stale data is returned with a warning log.

Per-tier TTL overrides ensure volatile prices (spot) are refreshed
more often than stable ones (reserved commitments).
"""

from __future__ import annotations

from typing import Any

import structlog
from langfuse import observe

from src.config.settings import AppSettings, get_settings
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.providers.base_provider import BaseCloudProvider
from src.services.pricing_cache import PricingCache, make_cache_key

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Per-tier TTL overrides (hours)
# ---------------------------------------------------------------------------

_TIER_TTL_HOURS: dict[PricingTier, int] = {
    # Volatile — refresh every 6 hours
    PricingTier.SPOT: 6,
    PricingTier.LOW_PRIORITY: 6,
    # Standard — refresh every 24 hours (matches default)
    PricingTier.ON_DEMAND: 24,
    PricingTier.DEV_TEST: 24,
    # Stable — refresh weekly
    PricingTier.RESERVED_1YR: 168,
    PricingTier.RESERVED_3YR: 168,
    PricingTier.SAVINGS_PLAN_1YR: 168,
    PricingTier.SAVINGS_PLAN_3YR: 168,
}


# ---------------------------------------------------------------------------
# PricingService
# ---------------------------------------------------------------------------


class PricingService:
    """Cache-aware pricing facade.

    Provides a unified API for searching cloud pricing across providers
    with transparent SQLite caching.  Agents and engines should **always**
    use this class instead of calling provider adapters directly.

    Usage::

        service = PricingService()
        service.register_provider(AzurePricingProvider())
        service.register_provider(AWSPricingProvider())
        service.register_provider(GCPPricingProvider())
        await service.initialize()

        # Search with automatic caching
        items = await service.search_prices(
            CloudProvider.AZURE,
            service_name="Virtual Machines",
            region="eastus",
        )

        # Compare across all registered providers
        results = await service.compare_across_providers(
            service_category=ServiceCategory.COMPUTE,
            regions={
                CloudProvider.AWS: "us-east-1",
                CloudProvider.AZURE: "eastus",
                CloudProvider.GCP: "us-central1",
            },
        )

        await service.close()
    """

    def __init__(self, settings: AppSettings | None = None) -> None:
        """Initialise the service (does NOT open the database yet).

        Args:
            settings: Application settings.  Uses the global singleton
                when ``None``.
        """
        self._settings = settings or get_settings()
        self._cache = PricingCache(
            db_path=self._settings.sku_cache_path,
            default_ttl_hours=self._settings.sku_cache_ttl_hours,
        )
        self._providers: dict[CloudProvider, BaseCloudProvider] = {}
        self._initialized = False

    # ------------------------------------------------------------------
    # Provider registration
    # ------------------------------------------------------------------

    def register_provider(self, provider: BaseCloudProvider) -> None:
        """Register a cloud provider adapter.

        Args:
            provider: A concrete ``BaseCloudProvider`` instance
                (e.g. ``AzurePricingProvider()``).
        """
        self._providers[provider.provider] = provider
        logger.bind(component="pricing_service").info(
            "provider_registered",
            provider=provider.provider.value,
        )

    @property
    def registered_providers(self) -> list[CloudProvider]:
        """Return the list of currently registered provider enums."""
        return list(self._providers.keys())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @observe(name="pricing_service_initialize")
    async def initialize(self) -> None:
        """Create the cache database and tables.

        Must be called **once** before any search operation.
        Safe to call multiple times.
        """
        log = logger.bind(component="pricing_service")
        log.info("service_initializing")

        await self._cache.initialize()
        self._initialized = True

        log.info(
            "service_initialized",
            providers=[p.value for p in self._providers],
            cache_path=str(self._settings.sku_cache_path),
            default_ttl_hours=self._settings.sku_cache_ttl_hours,
        )

    def _ensure_initialized(self) -> None:
        """Raise ``RuntimeError`` if ``initialize()`` hasn't been called."""
        if not self._initialized:
            raise RuntimeError(
                "PricingService.initialize() must be called before use. "
                "Call `await service.initialize()` first."
            )

    def _get_provider(self, provider: CloudProvider) -> BaseCloudProvider:
        """Look up a registered provider or raise ``ValueError``."""
        if provider not in self._providers:
            available = [p.value for p in self._providers]
            raise ValueError(
                f"Provider {provider.value!r} not registered. "
                f"Available: {available}"
            )
        return self._providers[provider]

    # ------------------------------------------------------------------
    # TTL logic
    # ------------------------------------------------------------------

    def _effective_ttl(self, pricing_tier: PricingTier | None) -> int:
        """Return the effective cache TTL for a given pricing tier.

        Uses the tier-specific override if available, capped at the
        global ``sku_cache_ttl_hours`` from settings.  Falls back to
        the global default when no tier is specified.

        Args:
            pricing_tier: The tier being queried, or ``None``.

        Returns:
            TTL in hours.
        """
        if pricing_tier and pricing_tier in _TIER_TTL_HOURS:
            return min(
                _TIER_TTL_HOURS[pricing_tier],
                self._settings.sku_cache_ttl_hours,
            )
        return self._settings.sku_cache_ttl_hours

    # ------------------------------------------------------------------
    # Core search
    # ------------------------------------------------------------------

    @observe(name="pricing_service_search")
    async def search_prices(
        self,
        provider: CloudProvider,
        *,
        service_name: str | None = None,
        service_category: ServiceCategory | None = None,
        region: str | None = None,
        sku_name: str | None = None,
        pricing_tier: PricingTier | None = None,
        max_results: int = 100,
        force_refresh: bool = False,
    ) -> list[NormalizedPriceItem]:
        """Search pricing with transparent caching.

        Checks the SQLite cache first.  On a cache miss (or stale
        data), fetches from the live provider API and stores results.

        If the live API call fails *and* stale cached data exists,
        the stale data is returned (graceful degradation).

        Args:
            provider: Which cloud provider to query.
            service_name: Provider-native service name
                (e.g. ``"Virtual Machines"``).
            service_category: Normalised service category.
            region: Provider-native region (e.g. ``"eastus"``).
            sku_name: SKU name substring filter (case-insensitive).
            pricing_tier: Commitment tier filter.
            max_results: Maximum items to return.
            force_refresh: Bypass cache and always fetch live.

        Returns:
            List of ``NormalizedPriceItem``.

        Raises:
            RuntimeError: If ``initialize()`` hasn't been called.
            ValueError: If the provider isn't registered.
        """
        self._ensure_initialized()
        log = logger.bind(
            component="pricing_service",
            provider=provider.value,
            service_name=service_name,
            service_category=service_category.value if service_category else None,
            region=region,
        )
        log.info("search_prices_started", force_refresh=force_refresh)

        cache_key = make_cache_key(
            provider.value,
            service_name,
            service_category.value if service_category else None,
            region,
        )
        ttl = self._effective_ttl(pricing_tier)

        # --- Cache hit? ---------------------------------------------------
        if not force_refresh and await self._cache.is_fresh(cache_key, ttl):
            log.debug("cache_hit", cache_key=cache_key[:8])
            items = await self._cache.query_items(
                provider=provider.value,
                service_name=service_name,
                service_category=(
                    service_category.value if service_category else None
                ),
                region=region,
                sku_name=sku_name,
                pricing_tier=pricing_tier.value if pricing_tier else None,
                max_results=max_results,
            )
            log.info("search_prices_from_cache", count=len(items))
            return items

        # --- Cache miss — fetch live --------------------------------------
        log.debug("cache_miss", cache_key=cache_key[:8])
        adapter = self._get_provider(provider)

        try:
            live_items = await adapter.search_prices(
                service_name=service_name,
                service_category=service_category,
                region=region,
                sku_name=sku_name,
                pricing_tier=pricing_tier,
                max_results=max_results,
            )
        except Exception:
            log.error("live_fetch_failed", exc_info=True)
            # Graceful degradation: serve stale data if available
            stale = await self._cache.query_items(
                provider=provider.value,
                service_name=service_name,
                service_category=(
                    service_category.value if service_category else None
                ),
                region=region,
                sku_name=sku_name,
                pricing_tier=pricing_tier.value if pricing_tier else None,
                max_results=max_results,
            )
            if stale:
                log.warning("serving_stale_cache", count=len(stale))
                return stale
            raise  # no stale data to fall back on

        # --- Store in cache -----------------------------------------------
        if live_items:
            await self._cache.upsert_items(
                live_items,
                cache_key=cache_key,
                provider=provider.value,
                service_name=service_name,
                service_category=(
                    service_category.value if service_category else None
                ),
                region=region,
            )

        log.info("search_prices_from_live", count=len(live_items))
        return live_items

    # ------------------------------------------------------------------
    # SKU-specific lookup
    # ------------------------------------------------------------------

    @observe(name="pricing_service_get_sku")
    async def get_sku_prices(
        self,
        provider: CloudProvider,
        sku_name: str,
        region: str,
        *,
        force_refresh: bool = False,
    ) -> list[NormalizedPriceItem]:
        """Get all pricing tiers for a specific SKU.

        Uses a dedicated cache key scoped to the SKU so it doesn't
        collide with broader service-level searches.

        Args:
            provider: Which cloud provider.
            sku_name: SKU name / description to look up.
            region: Provider-native region.
            force_refresh: Bypass cache.

        Returns:
            All pricing rows matching the SKU.
        """
        self._ensure_initialized()
        log = logger.bind(
            component="pricing_service",
            provider=provider.value,
            sku_name=sku_name,
            region=region,
        )
        log.info("get_sku_prices_started")

        cache_key = make_cache_key(
            provider.value,
            f"sku:{sku_name}",
            None,
            region,
        )

        # Check cache
        if not force_refresh and await self._cache.is_fresh(cache_key):
            items = await self._cache.query_items(
                provider=provider.value,
                sku_name=sku_name,
                region=region,
                max_results=50,
            )
            if items:
                log.info("get_sku_prices_from_cache", count=len(items))
                return items

        # Fetch live
        adapter = self._get_provider(provider)
        try:
            live_items = await adapter.get_sku_prices(sku_name, region)
        except Exception:
            log.error("live_sku_fetch_failed", exc_info=True)
            stale = await self._cache.query_items(
                provider=provider.value,
                sku_name=sku_name,
                region=region,
                max_results=50,
            )
            if stale:
                log.warning("serving_stale_sku_cache", count=len(stale))
                return stale
            raise

        if live_items:
            await self._cache.upsert_items(
                live_items,
                cache_key=cache_key,
                provider=provider.value,
                service_name=live_items[0].service_name if live_items else None,
                region=region,
            )

        log.info("get_sku_prices_from_live", count=len(live_items))
        return live_items

    # ------------------------------------------------------------------
    # Cross-provider comparison
    # ------------------------------------------------------------------

    @observe(name="pricing_service_compare")
    async def compare_across_providers(
        self,
        *,
        service_category: ServiceCategory,
        regions: dict[CloudProvider, str],
        sku_name: str | None = None,
        pricing_tier: PricingTier | None = None,
        max_results_per_provider: int = 50,
        force_refresh: bool = False,
    ) -> dict[CloudProvider, list[NormalizedPriceItem]]:
        """Compare pricing across multiple providers for a category.

        Queries each provider's pricing for the given
        ``ServiceCategory`` in the specified region.  Designed for the
        FinOps agent's cross-provider cost comparison workflow.

        Providers that are not registered or that fail are logged
        and returned as empty lists (no exception propagation).

        Args:
            service_category: Normalised category to compare.
            regions: Mapping of ``CloudProvider`` → region string.
            sku_name: Optional SKU name filter applied to all providers.
            pricing_tier: Optional tier filter applied to all providers.
            max_results_per_provider: Max items per provider.
            force_refresh: Bypass cache for all providers.

        Returns:
            Dict mapping ``CloudProvider`` → list of
            ``NormalizedPriceItem``.
        """
        self._ensure_initialized()
        log = logger.bind(
            component="pricing_service",
            category=service_category.value,
            providers=[p.value for p in regions],
        )
        log.info("compare_started")

        results: dict[CloudProvider, list[NormalizedPriceItem]] = {}

        for provider_key, region in regions.items():
            if provider_key not in self._providers:
                log.warning(
                    "provider_not_registered",
                    provider=provider_key.value,
                )
                results[provider_key] = []
                continue

            try:
                items = await self.search_prices(
                    provider_key,
                    service_category=service_category,
                    region=region,
                    sku_name=sku_name,
                    pricing_tier=pricing_tier,
                    max_results=max_results_per_provider,
                    force_refresh=force_refresh,
                )
                results[provider_key] = items
            except Exception:
                log.error(
                    "compare_provider_failed",
                    provider=provider_key.value,
                    exc_info=True,
                )
                results[provider_key] = []

        log.info(
            "compare_completed",
            counts={p.value: len(items) for p, items in results.items()},
        )
        return results

    # ------------------------------------------------------------------
    # Maintenance / admin
    # ------------------------------------------------------------------

    @observe(name="pricing_service_refresh")
    async def refresh(
        self,
        provider: CloudProvider,
        *,
        service_name: str | None = None,
        service_category: ServiceCategory | None = None,
        region: str | None = None,
        max_results: int = 200,
    ) -> int:
        """Force-refresh cached data for a provider/service/region.

        Convenience wrapper around ``search_prices`` with
        ``force_refresh=True``.

        Args:
            provider: Which provider to refresh.
            service_name: Specific service to refresh.
            service_category: Category to refresh.
            region: Specific region to refresh.
            max_results: Max items to fetch from the live API.

        Returns:
            Number of items fetched and stored.
        """
        items = await self.search_prices(
            provider,
            service_name=service_name,
            service_category=service_category,
            region=region,
            max_results=max_results,
            force_refresh=True,
        )
        return len(items)

    async def clear_cache(
        self,
        provider: CloudProvider | None = None,
    ) -> int:
        """Clear cached pricing data.

        Args:
            provider: If given, clear only that provider's data.
                If ``None``, clears everything.

        Returns:
            Number of ``price_items`` rows deleted.
        """
        self._ensure_initialized()
        return await self._cache.clear(
            provider=provider.value if provider else None,
        )

    async def evict_stale(
        self,
        ttl_hours: int | None = None,
    ) -> int:
        """Remove items older than the TTL.

        Useful as a periodic maintenance task (e.g. on app startup
        or via a scheduled job).

        Args:
            ttl_hours: Override TTL.  Uses the default from settings
                when ``None``.

        Returns:
            Number of rows evicted.
        """
        self._ensure_initialized()
        return await self._cache.evict_stale(ttl_hours=ttl_hours)

    async def cache_stats(self) -> dict[str, Any]:
        """Return cache statistics.

        Returns:
            Dict with ``total_items``, ``items_by_provider``,
            ``fetch_entries``, ``oldest_fetch``, ``newest_fetch``,
            ``db_size_bytes``.
        """
        self._ensure_initialized()
        return await self._cache.stats()

    async def close(self) -> None:
        """Shut down the service and close database connections.

        After calling ``close()``, the service must be re-initialised
        with ``initialize()`` before further use.
        """
        log = logger.bind(component="pricing_service")
        log.info("service_closing")
        await self._cache.close()
        self._initialized = False
        log.info("service_closed")
