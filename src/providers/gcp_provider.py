"""GCP Cloud Billing Catalog provider adapter.

Fetches pricing data via the **google-cloud-billing** SDK and normalises
every SKU into ``NormalizedPriceItem``.

API reference:
    https://cloud.google.com/billing/docs/reference/rest/v1/services.skus

Key properties of the API:
    * Requires OAuth2 authentication (service-account JSON or
      ``gcloud auth application-default login``).
    * Two-step discovery: list services → list SKUs per service.
    * SKU ``category`` contains ``resourceFamily``, ``resourceGroup``,
      and ``usageType`` (OnDemand / Preemptible / Commit1Yr / Commit3Yr).
    * Prices are split into ``units`` (int64 string) + ``nanos`` (int32),
      combined as ``float(units) + nanos / 1e9``.
    * Tiered rates: multiple price tiers based on ``startUsageAmount``.
    * Up to 5 000 SKUs per page; ``next_page_token`` for pagination.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog
from langfuse import observe

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.providers.base_provider import BaseCloudProvider

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# GCP service display name → ServiceCategory mapping
# ---------------------------------------------------------------------------

_SERVICE_DISPLAY_NAME_MAP: dict[str, ServiceCategory] = {
    # Compute
    "Compute Engine": ServiceCategory.COMPUTE,
    # Serverless compute
    "Cloud Run": ServiceCategory.SERVERLESS_COMPUTE,
    "App Engine": ServiceCategory.SERVERLESS_COMPUTE,
    # Containers
    "Kubernetes Engine": ServiceCategory.CONTAINER,
    # Serverless functions
    "Cloud Functions": ServiceCategory.SERVERLESS_FUNCTION,
    # Databases
    "Cloud SQL": ServiceCategory.DATABASE,
    "Cloud Spanner": ServiceCategory.DATABASE,
    "Cloud Bigtable": ServiceCategory.DATABASE,
    "Firestore": ServiceCategory.DATABASE,
    "Cloud Memorystore for Redis": ServiceCategory.DATABASE,
    "AlloyDB for PostgreSQL": ServiceCategory.DATABASE,
    # Storage
    "Cloud Storage": ServiceCategory.STORAGE,
    "Persistent Disk": ServiceCategory.STORAGE,
    "Filestore": ServiceCategory.STORAGE,
    # Networking
    "Cloud Load Balancing": ServiceCategory.NETWORKING,
    "Cloud DNS": ServiceCategory.NETWORKING,
    "Cloud CDN": ServiceCategory.NETWORKING,
    "Cloud NAT": ServiceCategory.NETWORKING,
    "Cloud Armor": ServiceCategory.NETWORKING,
    "Virtual Private Cloud": ServiceCategory.NETWORKING,
    # AI/ML
    "Vertex AI": ServiceCategory.AI_ML,
    "Cloud Natural Language API": ServiceCategory.AI_ML,
    "Cloud Vision API": ServiceCategory.AI_ML,
    "Cloud Translation API": ServiceCategory.AI_ML,
    # Analytics
    "BigQuery": ServiceCategory.ANALYTICS,
    "Dataflow": ServiceCategory.ANALYTICS,
    "Dataproc": ServiceCategory.ANALYTICS,
    "Cloud Pub/Sub": ServiceCategory.ANALYTICS,
    # Management
    "Cloud Monitoring": ServiceCategory.MANAGEMENT,
    "Cloud Logging": ServiceCategory.MANAGEMENT,
    "Cloud Trace": ServiceCategory.MANAGEMENT,
    # Security
    "Secret Manager": ServiceCategory.SECURITY,
    "Cloud Key Management Service": ServiceCategory.SECURITY,
    # Integration
    "API Gateway": ServiceCategory.INTEGRATION,
    "Cloud Tasks": ServiceCategory.INTEGRATION,
    "Cloud Scheduler": ServiceCategory.INTEGRATION,
    "Eventarc": ServiceCategory.INTEGRATION,
    # IoT
    "Cloud IoT Core": ServiceCategory.IOT,
}

# GCP resourceFamily fallback (from category.resourceFamily)
_RESOURCE_FAMILY_MAP: dict[str, ServiceCategory] = {
    "Compute": ServiceCategory.COMPUTE,
    "Storage": ServiceCategory.STORAGE,
    "Network": ServiceCategory.NETWORKING,
    "ApplicationServices": ServiceCategory.INTEGRATION,
}

# Reverse mapping: ServiceCategory → GCP service display names
_CATEGORY_TO_DISPLAY_NAMES: dict[ServiceCategory, list[str]] = {
    ServiceCategory.COMPUTE: ["Compute Engine"],
    ServiceCategory.SERVERLESS_COMPUTE: ["Cloud Run", "App Engine"],
    ServiceCategory.CONTAINER: ["Kubernetes Engine"],
    ServiceCategory.SERVERLESS_FUNCTION: ["Cloud Functions"],
    ServiceCategory.DATABASE: ["Cloud SQL", "Cloud Spanner", "Cloud Bigtable"],
    ServiceCategory.STORAGE: ["Cloud Storage", "Persistent Disk"],
    ServiceCategory.NETWORKING: ["Cloud Load Balancing", "Cloud DNS", "Cloud CDN"],
    ServiceCategory.AI_ML: ["Vertex AI"],
    ServiceCategory.ANALYTICS: ["BigQuery", "Dataflow", "Dataproc"],
    ServiceCategory.MANAGEMENT: ["Cloud Monitoring", "Cloud Logging"],
    ServiceCategory.SECURITY: ["Secret Manager", "Cloud Key Management Service"],
    ServiceCategory.INTEGRATION: ["API Gateway", "Cloud Tasks"],
    ServiceCategory.IOT: ["Cloud IoT Core"],
}


# ---------------------------------------------------------------------------
# GCP category.usageType → PricingTier
# ---------------------------------------------------------------------------


def _resolve_pricing_tier(usage_type: str) -> PricingTier:
    """Map a GCP SKU ``category.usageType`` to ``PricingTier``."""
    mapping: dict[str, PricingTier] = {
        "OnDemand": PricingTier.ON_DEMAND,
        "Preemptible": PricingTier.SPOT,
        "Commit1Yr": PricingTier.RESERVED_1YR,
        "Commit3Yr": PricingTier.RESERVED_3YR,
    }
    return mapping.get(usage_type, PricingTier.ON_DEMAND)


def _resolve_service_category(
    display_name: str,
    resource_family: str,
) -> ServiceCategory:
    """Map GCP service display name / resourceFamily to ``ServiceCategory``."""
    if display_name in _SERVICE_DISPLAY_NAME_MAP:
        return _SERVICE_DISPLAY_NAME_MAP[display_name]
    return _RESOURCE_FAMILY_MAP.get(resource_family, ServiceCategory.OTHER)


# ---------------------------------------------------------------------------
# GCP price calculation helper
# ---------------------------------------------------------------------------


def _units_nanos_to_float(units: int, nanos: int) -> float:
    """Convert GCP ``{units, nanos}`` money representation to float.

    Args:
        units: Whole units of the amount (int64 in proto).
        nanos: Nano units (10^-9) of the amount (int32 in proto).

    Returns:
        Float price value.
    """
    return float(units) + nanos / 1_000_000_000


def _normalise_usage_unit(usage_unit: str) -> str:
    """Normalise GCP usage units to consistent format.

    GCP uses short codes: "h", "GiBy", "GiBy.mo", "mo", etc.
    """
    mapping: dict[str, str] = {
        "h": "1 Hour",
        "min": "1 Minute",
        "s": "1 Second",
        "mo": "1 Month",
        "d": "1 Day",
        "GiBy": "1 GB",
        "GiBy.h": "1 GB Hour",
        "GiBy.mo": "1 GB/Month",
        "GiBy.d": "1 GB Day",
        "By": "1 Byte",
        "MiBy": "1 MB",
        "MiBy.mo": "1 MB/Month",
        "count": "1 Request",
    }
    return mapping.get(usage_unit, usage_unit)


# ---------------------------------------------------------------------------
# GCP SKU → NormalizedPriceItem transformer
# ---------------------------------------------------------------------------


def _sku_to_normalized_items(
    sku: Any,
    service_display_name: str,
    target_region: str | None = None,
) -> list[NormalizedPriceItem]:
    """Convert a GCP SKU proto to ``NormalizedPriceItem`` rows.

    One SKU can serve multiple regions (``service_regions``) and have
    tiered rates.  We produce one item per region × tier combination.

    When ``target_region`` is given, only items for that region are
    returned.  Otherwise all regions are included.

    Args:
        sku: A ``google.cloud.billing_v1.types.Sku`` proto object.
        service_display_name: Display name of the parent service.
        target_region: Optional region filter.

    Returns:
        List of normalised price items.
    """
    category = sku.category
    usage_type = category.usage_type if category else "OnDemand"
    pricing_tier = _resolve_pricing_tier(usage_type)
    service_category = _resolve_service_category(
        service_display_name,
        category.resource_family if category else "",
    )

    # Determine reservation term
    reservation_term: str | None = None
    if pricing_tier == PricingTier.RESERVED_1YR:
        reservation_term = "1 Year"
    elif pricing_tier == PricingTier.RESERVED_3YR:
        reservation_term = "3 Years"

    # Regions
    sku_regions: list[str] = list(sku.service_regions) if sku.service_regions else []
    if target_region:
        if target_region not in sku_regions:
            return []
        sku_regions = [target_region]

    items: list[NormalizedPriceItem] = []

    for pricing_info in sku.pricing_info:
        expr = pricing_info.pricing_expression
        usage_unit = expr.usage_unit if expr else ""

        effective_time = (
            pricing_info.effective_time.timestamp()
            if pricing_info.effective_time
            else 0
        )
        effective_dt = datetime.fromtimestamp(effective_time, tz=timezone.utc)

        # Use the first (primary) tiered rate.  Additional tiers are
        # volume discounts which we store in attributes.
        tiered_rates = list(expr.tiered_rates) if expr else []
        if not tiered_rates:
            continue

        primary_rate = tiered_rates[0]
        unit_price_obj = primary_rate.unit_price
        price_usd = _units_nanos_to_float(
            int(unit_price_obj.units) if unit_price_obj.units else 0,
            unit_price_obj.nanos if unit_price_obj.nanos else 0,
        )

        # Build extra tiers info for attributes
        extra_tiers: list[dict[str, Any]] = []
        for rate in tiered_rates[1:]:
            up = rate.unit_price
            extra_tiers.append({
                "start_usage_amount": rate.start_usage_amount,
                "price_usd": _units_nanos_to_float(
                    int(up.units) if up.units else 0,
                    up.nanos if up.nanos else 0,
                ),
            })

        for region in sku_regions:
            items.append(
                NormalizedPriceItem(
                    provider=CloudProvider.GCP,
                    service_name=service_display_name,
                    service_category=service_category,
                    sku_id=sku.sku_id or "",
                    sku_name=sku.description or "",
                    product_name=service_display_name,
                    meter_name="",
                    region=region,
                    retail_price=price_usd,
                    unit_price=price_usd,
                    currency=unit_price_obj.currency_code or "USD",
                    unit_of_measure=_normalise_usage_unit(usage_unit),
                    pricing_tier=pricing_tier,
                    reservation_term=reservation_term,
                    effective_date=effective_dt,
                    attributes={
                        "resource_family": category.resource_family if category else "",
                        "resource_group": category.resource_group if category else "",
                        "usage_type": usage_type,
                        "display_quantity": expr.display_quantity if expr else 0,
                        **({"tiered_rates": extra_tiers} if extra_tiers else {}),
                    },
                )
            )

    return items


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------


class GCPPricingProvider(BaseCloudProvider):
    """GCP Cloud Billing Catalog adapter.

    Uses ``google-cloud-billing`` to list services and SKUs, then
    normalises each into ``NormalizedPriceItem``.

    The gRPC client is **synchronous**; all calls are wrapped with
    ``asyncio.to_thread`` to avoid blocking the event loop.
    """

    provider = CloudProvider.GCP

    def __init__(self, credentials_path: str = "") -> None:
        """Initialise the GCP provider.

        Args:
            credentials_path: Optional path to a service-account JSON key file.
                Falls back to the ``GOOGLE_APPLICATION_CREDENTIALS`` environment
                variable or Application Default Credentials if not supplied.
        """
        self._client = None  # lazy-init
        self._service_cache: dict[str, str] = {}  # display_name → resource name
        self._credentials_path = credentials_path

    def _get_client(self):
        """Lazily create the Cloud Catalog client.

        Uses explicit service-account credentials when ``credentials_path`` is
        supplied; otherwise falls back to Application Default Credentials
        (``GOOGLE_APPLICATION_CREDENTIALS`` env var or ``gcloud auth
        application-default login``).

        Returns:
            A ``CloudCatalogClient`` instance.
        """
        if self._client is None:
            from google.cloud import billing_v1

            if self._credentials_path:
                from google.oauth2 import service_account

                credentials = service_account.Credentials.from_service_account_file(
                    self._credentials_path,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                self._client = billing_v1.CloudCatalogClient(credentials=credentials)
                logger.info(
                    "gcp_client_init",
                    auth="service_account",
                    credentials_path=self._credentials_path,
                )
            else:
                # ADC: GOOGLE_APPLICATION_CREDENTIALS env var or gcloud login
                self._client = billing_v1.CloudCatalogClient()
                logger.info("gcp_client_init", auth="application_default_credentials")
        return self._client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @observe(name="gcp_resolve_services")
    async def _resolve_service_names(
        self,
        display_names: list[str],
    ) -> dict[str, str]:
        """Resolve GCP service display names to API resource names.

        Caches results so repeated calls don't re-fetch.

        Args:
            display_names: E.g. ``["Compute Engine", "Cloud SQL"]``.

        Returns:
            Dict mapping display_name → resource name
            (e.g. ``"services/6F81-5844-456A"``).
        """
        # Check what's already cached
        missing = [n for n in display_names if n not in self._service_cache]
        if not missing:
            return {n: self._service_cache[n] for n in display_names}

        log = logger.bind(provider="gcp")
        log.debug("gcp_listing_services", missing=missing)

        client = self._get_client()

        def _list_services():
            return list(client.list_services())

        services = await asyncio.to_thread(_list_services)

        for svc in services:
            self._service_cache[svc.display_name] = svc.name

        log.debug("gcp_services_cached", count=len(self._service_cache))

        return {
            n: self._service_cache[n]
            for n in display_names
            if n in self._service_cache
        }

    @observe(name="gcp_fetch_skus")
    async def _fetch_skus(
        self,
        service_resource_name: str,
        service_display_name: str,
        *,
        region: str | None = None,
        max_results: int = 100,
    ) -> list[NormalizedPriceItem]:
        """Fetch and normalise SKUs for a GCP service.

        Args:
            service_resource_name: E.g. ``"services/6F81-5844-456A"``.
            service_display_name: E.g. ``"Compute Engine"``.
            region: Optional region filter (client-side).
            max_results: Max items to collect.

        Returns:
            List of normalised price items.
        """
        log = logger.bind(
            provider="gcp",
            service=service_display_name,
            region=region,
        )
        log.debug("gcp_fetch_skus_started", max_results=max_results)

        client = self._get_client()

        def _list_skus():
            return list(
                client.list_skus(
                    parent=service_resource_name,
                    # The SDK handles pagination internally
                )
            )

        skus = await asyncio.to_thread(_list_skus)

        all_items: list[NormalizedPriceItem] = []
        for sku in skus:
            items = _sku_to_normalized_items(sku, service_display_name, region)
            all_items.extend(items)
            if len(all_items) >= max_results:
                break

        result = all_items[:max_results]
        log.info(
            "gcp_fetch_skus_completed",
            service=service_display_name,
            skus_fetched=len(skus),
            items_normalised=len(result),
        )
        return result

    # ------------------------------------------------------------------
    # BaseCloudProvider interface
    # ------------------------------------------------------------------

    @observe(name="gcp_search_prices")
    async def search_prices(
        self,
        *,
        service_name: str | None = None,
        service_category: ServiceCategory | None = None,
        region: str | None = None,
        sku_name: str | None = None,
        pricing_tier: PricingTier | None = None,
        max_results: int = 100,
    ) -> list[NormalizedPriceItem]:
        """Search GCP pricing with flexible filters.

        ``service_name`` is interpreted as a GCP service display name
        (e.g. ``"Compute Engine"``).  When only ``service_category`` is
        provided, the adapter queries representative services for that
        category.

        Args:
            service_name: GCP service display name.
            service_category: Normalised category.
            region: GCP region (e.g. "us-central1").
            sku_name: SKU description substring filter (client-side).
            pricing_tier: Commitment tier filter (client-side).
            max_results: Maximum items to return.

        Returns:
            List of ``NormalizedPriceItem``.
        """
        log = logger.bind(
            provider="gcp",
            service_name=service_name,
            service_category=service_category,
            region=region,
            sku_name=sku_name,
            pricing_tier=pricing_tier,
        )
        log.info("search_prices_started", max_results=max_results)

        # Resolve which GCP services to query
        display_names: list[str]
        if service_name:
            display_names = [service_name]
        elif service_category and service_category in _CATEGORY_TO_DISPLAY_NAMES:
            display_names = _CATEGORY_TO_DISPLAY_NAMES[service_category]
        else:
            log.warning("search_prices_no_filter", msg="No service_name or known category")
            return []

        # Resolve display names to resource names
        name_map = await self._resolve_service_names(display_names)
        if not name_map:
            log.warning("search_prices_no_services", display_names=display_names)
            return []

        all_items: list[NormalizedPriceItem] = []
        per_svc_limit = max(max_results // len(name_map), 20)

        for display_name, resource_name in name_map.items():
            items = await self._fetch_skus(
                resource_name,
                display_name,
                region=region,
                max_results=per_svc_limit,
            )
            all_items.extend(items)
            if len(all_items) >= max_results:
                break

        # Client-side filtering
        if pricing_tier:
            all_items = [i for i in all_items if i.pricing_tier == pricing_tier]

        if sku_name:
            sku_lower = sku_name.lower()
            all_items = [
                i for i in all_items if sku_lower in i.sku_name.lower()
            ]

        if service_category:
            all_items = [
                i for i in all_items if i.service_category == service_category
            ]

        result = all_items[:max_results]
        log.info("search_prices_completed", count=len(result))
        return result

    @observe(name="gcp_get_sku_prices")
    async def get_sku_prices(
        self,
        sku_name: str,
        region: str,
    ) -> list[NormalizedPriceItem]:
        """Get all pricing tiers for a GCP SKU description.

        Searches Compute Engine by default for instance descriptions.

        Args:
            sku_name: SKU description substring (e.g. "N2 Instance Core").
            region: GCP region (e.g. "us-central1").

        Returns:
            All pricing rows matching the description.
        """
        log = logger.bind(provider="gcp", sku_name=sku_name, region=region)
        log.info("get_sku_prices_started")

        # Fetch Compute Engine SKUs and filter by description
        name_map = await self._resolve_service_names(["Compute Engine"])
        if "Compute Engine" not in name_map:
            log.warning("get_sku_prices_no_compute_engine")
            return []

        items = await self._fetch_skus(
            name_map["Compute Engine"],
            "Compute Engine",
            region=region,
            max_results=500,
        )

        sku_lower = sku_name.lower()
        matched = [i for i in items if sku_lower in i.sku_name.lower()]

        log.info("get_sku_prices_completed", count=len(matched))
        return matched

    @observe(name="gcp_list_regions")
    async def list_regions(self) -> list[str]:
        """Return GCP regions from a Compute Engine SKU sample.

        Fetches a batch of Compute Engine SKUs and extracts the
        unique ``service_regions`` values.
        """
        log = logger.bind(provider="gcp")
        log.info("list_regions_started")

        name_map = await self._resolve_service_names(["Compute Engine"])
        if "Compute Engine" not in name_map:
            log.warning("list_regions_no_compute_engine")
            return []

        client = self._get_client()
        resource_name = name_map["Compute Engine"]

        def _list_skus():
            # Fetch first page only for region extraction
            return list(client.list_skus(parent=resource_name))

        skus = await asyncio.to_thread(_list_skus)

        regions: set[str] = set()
        for sku in skus[:200]:  # Sample first 200 SKUs
            regions.update(sku.service_regions or [])

        result = sorted(regions)
        log.info("list_regions_completed", count=len(result))
        return result
