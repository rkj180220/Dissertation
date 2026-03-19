"""Azure Retail Prices provider adapter.

Fetches pricing data from the **public, unauthenticated** Azure Retail
Prices REST API and normalises every item into ``NormalizedPriceItem``.

API reference:
    https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices

Key properties of the API:
    * No authentication required.
    * OData ``$filter`` for server-side filtering.
    * Every item returns the **same 20 fields** regardless of service.
    * 1 000 items per page; ``NextPageLink`` for pagination.
    * Reservation / spot / savings-plan prices appear as *separate rows*.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog
from langfuse import observe

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.providers.base_provider import BaseCloudProvider

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://prices.azure.com/api/retail/prices"
_PAGE_SIZE = 1000  # Azure returns max 1 000 items per page

# ---------------------------------------------------------------------------
# Azure → ServiceCategory mapping
# ---------------------------------------------------------------------------

# Step 1: specific serviceName overrides (checked first)
_SERVICE_NAME_MAP: dict[str, ServiceCategory] = {
    # Serverless / PaaS compute
    "Functions": ServiceCategory.SERVERLESS_FUNCTION,
    "Azure App Service": ServiceCategory.SERVERLESS_COMPUTE,
    "Azure Spring Apps": ServiceCategory.SERVERLESS_COMPUTE,
    # Containers
    "Azure Kubernetes Service": ServiceCategory.CONTAINER,
    "Container Instances": ServiceCategory.CONTAINER,
    "Azure Container Apps": ServiceCategory.CONTAINER,
    # Databases (some live under serviceFamily "Compute")
    "SQL Database": ServiceCategory.DATABASE,
    "Azure Database for MySQL": ServiceCategory.DATABASE,
    "Azure Database for PostgreSQL": ServiceCategory.DATABASE,
    "Azure Database for MariaDB": ServiceCategory.DATABASE,
    "Azure Cosmos DB": ServiceCategory.DATABASE,
    "Azure Cache for Redis": ServiceCategory.DATABASE,
    # AI / ML
    "Azure Machine Learning": ServiceCategory.AI_ML,
    "Azure Cognitive Services": ServiceCategory.AI_ML,
    "Azure OpenAI Service": ServiceCategory.AI_ML,
}

# Step 2: serviceFamily fallback
_FAMILY_MAP: dict[str, ServiceCategory] = {
    "Compute": ServiceCategory.COMPUTE,
    "Databases": ServiceCategory.DATABASE,
    "Storage": ServiceCategory.STORAGE,
    "Networking": ServiceCategory.NETWORKING,
    "AI + Machine Learning": ServiceCategory.AI_ML,
    "Analytics": ServiceCategory.ANALYTICS,
    "Management and Governance": ServiceCategory.MANAGEMENT,
    "Security": ServiceCategory.SECURITY,
    "Integration": ServiceCategory.INTEGRATION,
    "Internet of Things": ServiceCategory.IOT,
    "Developer Tools": ServiceCategory.OTHER,
    "Data": ServiceCategory.ANALYTICS,
    "Azure Communication Services": ServiceCategory.OTHER,
    "Microsoft Syntex": ServiceCategory.OTHER,
    "Other": ServiceCategory.OTHER,
}

# Step 3: reverse mapping — ServiceCategory → representative Azure serviceNames
# Used to build OData filters when caller searches by category only.
_CATEGORY_TO_SERVICE_NAMES: dict[ServiceCategory, list[str]] = {
    ServiceCategory.COMPUTE: ["Virtual Machines"],
    ServiceCategory.SERVERLESS_COMPUTE: ["Azure App Service", "Azure Spring Apps"],
    ServiceCategory.CONTAINER: [
        "Azure Kubernetes Service",
        "Container Instances",
        "Azure Container Apps",
    ],
    ServiceCategory.SERVERLESS_FUNCTION: ["Functions"],
    ServiceCategory.DATABASE: [
        "SQL Database",
        "Azure Database for PostgreSQL",
        "Azure Database for MySQL",
        "Azure Cosmos DB",
        "Azure Cache for Redis",
    ],
    ServiceCategory.STORAGE: ["Storage"],
    ServiceCategory.NETWORKING: ["Load Balancer", "Application Gateway", "Bandwidth"],
    ServiceCategory.AI_ML: [
        "Azure Machine Learning",
        "Azure Cognitive Services",
        "Azure OpenAI Service",
    ],
    ServiceCategory.ANALYTICS: ["Azure Synapse Analytics", "Azure Databricks"],
    ServiceCategory.MANAGEMENT: ["Azure Monitor", "Log Analytics"],
    ServiceCategory.SECURITY: ["Key Vault", "Microsoft Defender for Cloud"],
    ServiceCategory.INTEGRATION: ["API Management", "Event Grid", "Service Bus"],
    ServiceCategory.IOT: ["IoT Hub"],
}

# ---------------------------------------------------------------------------
# Azure `type` + SKU hints → PricingTier
# ---------------------------------------------------------------------------


def _resolve_pricing_tier(item: dict[str, Any]) -> PricingTier:
    """Map an Azure price item to a ``PricingTier`` enum value."""
    type_val: str = item.get("type", "")
    sku_name: str = item.get("skuName", "")
    reservation_term: str | None = item.get("reservationTerm")

    if type_val == "Reservation":
        if reservation_term and "3" in reservation_term:
            return PricingTier.RESERVED_3YR
        return PricingTier.RESERVED_1YR

    if type_val == "DevTestConsumption":
        return PricingTier.DEV_TEST

    # Spot / Low Priority are Consumption rows whose SKU name contains hints
    if "Spot" in sku_name:
        return PricingTier.SPOT
    if "Low Priority" in sku_name:
        return PricingTier.LOW_PRIORITY

    return PricingTier.ON_DEMAND


def _resolve_service_category(item: dict[str, Any]) -> ServiceCategory:
    """Map an Azure price item to a ``ServiceCategory`` enum value."""
    svc_name: str = item.get("serviceName", "")
    svc_family: str = item.get("serviceFamily", "")

    if svc_name in _SERVICE_NAME_MAP:
        return _SERVICE_NAME_MAP[svc_name]
    return _FAMILY_MAP.get(svc_family, ServiceCategory.OTHER)


# ---------------------------------------------------------------------------
# Azure → NormalizedPriceItem transformer
# ---------------------------------------------------------------------------


def _to_normalized(item: dict[str, Any]) -> NormalizedPriceItem:
    """Convert a single Azure REST API item dict to ``NormalizedPriceItem``."""
    effective = item.get("effectiveStartDate", "")
    effective_dt = (
        datetime.fromisoformat(effective.replace("Z", "+00:00"))
        if effective
        else datetime.min
    )
    end_date_raw = item.get("effectiveEndDate")
    end_dt = (
        datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
        if end_date_raw
        else None
    )

    return NormalizedPriceItem(
        provider=CloudProvider.AZURE,
        service_name=item.get("serviceName", ""),
        service_category=_resolve_service_category(item),
        sku_id=item.get("skuId", ""),
        sku_name=item.get("skuName", ""),
        product_name=item.get("productName", ""),
        meter_name=item.get("meterName", ""),
        region=item.get("armRegionName", ""),
        retail_price=item.get("retailPrice", 0),
        unit_price=item.get("unitPrice", 0),
        currency=item.get("currencyCode", "USD"),
        unit_of_measure=item.get("unitOfMeasure", ""),
        pricing_tier=_resolve_pricing_tier(item),
        reservation_term=item.get("reservationTerm"),
        effective_date=effective_dt,
        effective_end_date=end_dt,
        is_primary_meter=item.get("isPrimaryMeterRegion", True),
        attributes={
            "arm_sku_name": item.get("armSkuName", ""),
            "meter_id": item.get("meterId", ""),
            "product_id": item.get("productId", ""),
            "service_id": item.get("serviceId", ""),
            "service_family": item.get("serviceFamily", ""),
            "tier_minimum_units": item.get("tierMinimumUnits", 0),
            "azure_type": item.get("type", ""),
        },
    )


# ---------------------------------------------------------------------------
# OData filter builder
# ---------------------------------------------------------------------------


def _build_filter(
    *,
    service_name: str | None = None,
    region: str | None = None,
    sku_name: str | None = None,
    pricing_tier: PricingTier | None = None,
) -> str | None:
    """Build an OData ``$filter`` expression for the Azure Retail Prices API.

    Returns:
        A filter string, or ``None`` if no filters apply.
    """
    clauses: list[str] = []

    if service_name:
        clauses.append(f"serviceName eq '{service_name}'")
    if region:
        clauses.append(f"armRegionName eq '{region}'")
    if sku_name:
        # Contains search — armSkuName eq is exact match
        clauses.append(f"armSkuName eq '{sku_name}'")

    # Tier → Azure 'type' mapping
    if pricing_tier:
        tier_type = _tier_to_azure_type(pricing_tier)
        if tier_type:
            clauses.append(f"type eq '{tier_type}'")

    return " and ".join(clauses) if clauses else None


def _tier_to_azure_type(tier: PricingTier) -> str | None:
    """Map a ``PricingTier`` to the Azure ``type`` OData filter value."""
    mapping: dict[PricingTier, str] = {
        PricingTier.ON_DEMAND: "Consumption",
        PricingTier.SPOT: "Consumption",  # spot is Consumption with 'Spot' in skuName
        PricingTier.RESERVED_1YR: "Reservation",
        PricingTier.RESERVED_3YR: "Reservation",
        PricingTier.DEV_TEST: "DevTestConsumption",
    }
    return mapping.get(tier)


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------


class AzurePricingProvider(BaseCloudProvider):
    """Azure Retail Prices adapter.

    Calls the public REST API (no auth required), paginates through
    results, and returns ``NormalizedPriceItem`` instances.

    Args:
        timeout: HTTP request timeout in seconds.
    """

    provider = CloudProvider.AZURE

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch a single page from the Azure pricing API.

        Returns:
            Tuple of (items, next_page_url_or_none).
        """
        response = await client.get(url, params=params)
        response.raise_for_status()
        body = response.json()
        items: list[dict[str, Any]] = body.get("Items", [])
        next_page: str | None = body.get("NextPageLink")
        return items, next_page

    @observe(name="azure_fetch_all_pages")
    async def _fetch_all(
        self,
        *,
        odata_filter: str | None = None,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """Paginate through the Azure Retail Prices API.

        Args:
            odata_filter: OData ``$filter`` expression.
            max_results: Stop collecting after this many items.

        Returns:
            List of raw Azure price-item dicts.
        """
        log = logger.bind(provider="azure", odata_filter=odata_filter)
        log.debug("azure_fetch_started", max_results=max_results)

        all_items: list[dict[str, Any]] = []
        params: dict[str, str] = {}
        if odata_filter:
            params["$filter"] = odata_filter

        url: str | None = _BASE_URL
        pages = 0

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while url and len(all_items) < max_results:
                if pages == 0:
                    items, next_url = await self._fetch_page(client, url, params)
                else:
                    # NextPageLink is a full URL (includes $filter already)
                    items, next_url = await self._fetch_page(client, url)

                all_items.extend(items)
                url = next_url
                pages += 1

                log.debug(
                    "azure_page_fetched",
                    page=pages,
                    items_this_page=len(items),
                    total_so_far=len(all_items),
                )

        # Trim to max_results
        result = all_items[:max_results]
        log.info(
            "azure_fetch_completed",
            pages=pages,
            total_items=len(result),
        )
        return result

    # ------------------------------------------------------------------
    # BaseCloudProvider interface
    # ------------------------------------------------------------------

    @observe(name="azure_search_prices")
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
        """Search Azure pricing with flexible filters.

        When ``service_category`` is provided without ``service_name``,
        the adapter uses a reverse-mapping to build server-side OData
        queries for the representative Azure service names in that
        category (one API call per service name, results merged).

        Args:
            service_name: Azure-native service name (e.g. "Virtual Machines").
            service_category: Normalised category — triggers multi-query
                when service_name is not supplied.
            region: ARM region name (e.g. "eastus").
            sku_name: Exact ARM SKU name to match.
            pricing_tier: Commitment tier filter (applied both server-
                and client-side for accuracy).
            max_results: Maximum items to return.

        Returns:
            List of ``NormalizedPriceItem`` matching the criteria.
        """
        log = logger.bind(
            provider="azure",
            service_name=service_name,
            service_category=service_category,
            region=region,
            sku_name=sku_name,
            pricing_tier=pricing_tier,
        )
        log.info("search_prices_started", max_results=max_results)

        # Determine which Azure service names to query
        svc_names_to_query: list[str | None]
        if service_name:
            svc_names_to_query = [service_name]
        elif service_category and service_category in _CATEGORY_TO_SERVICE_NAMES:
            svc_names_to_query = _CATEGORY_TO_SERVICE_NAMES[service_category]
        else:
            # No category or unknown category — broad query
            svc_names_to_query = [None]

        all_items: list[NormalizedPriceItem] = []
        per_query_limit = max(max_results // len(svc_names_to_query), 20)

        for svc in svc_names_to_query:
            odata = _build_filter(
                service_name=svc,
                region=region,
                sku_name=sku_name,
                pricing_tier=pricing_tier,
            )
            raw_items = await self._fetch_all(
                odata_filter=odata,
                max_results=per_query_limit,
            )
            all_items.extend(_to_normalized(i) for i in raw_items)

            if len(all_items) >= max_results:
                break

        # Client-side filtering: pricing_tier (Spot/LowPriority share
        # Azure type='Consumption' with ON_DEMAND, so server-side alone
        # is not enough)
        if pricing_tier:
            all_items = [i for i in all_items if i.pricing_tier == pricing_tier]

        # Client-side filtering: service_category (in case the reverse
        # mapping returned a broader set than expected)
        if service_category:
            all_items = [i for i in all_items if i.service_category == service_category]

        result = all_items[:max_results]
        log.info("search_prices_completed", count=len(result))
        return result

    @observe(name="azure_get_sku_prices")
    async def get_sku_prices(
        self,
        sku_name: str,
        region: str,
    ) -> list[NormalizedPriceItem]:
        """Get all pricing tiers for a specific Azure SKU.

        Args:
            sku_name: ARM SKU name (e.g. "Standard_D4s_v5").
            region: ARM region (e.g. "eastus").

        Returns:
            All pricing rows: on-demand, reserved, spot, etc.
        """
        log = logger.bind(provider="azure", sku_name=sku_name, region=region)
        log.info("get_sku_prices_started")

        odata = f"armSkuName eq '{sku_name}' and armRegionName eq '{region}'"

        # Fetch enough to cover all tiers (on-demand + reserved 1yr/3yr + spot ≈ <20 rows)
        raw = await self._fetch_all(odata_filter=odata, max_results=50)
        items = [_to_normalized(i) for i in raw]

        log.info("get_sku_prices_completed", count=len(items))
        return items

    @observe(name="azure_list_regions")
    async def list_regions(self) -> list[str]:
        """Return distinct Azure regions from the pricing catalog.

        Note:
            This fetches a small sample (first page of VM pricing) and
            extracts unique ``armRegionName`` values.  For production use,
            the Azure Management API ``/subscriptions/{sub}/locations``
            is more authoritative but requires authentication.
        """
        log = logger.bind(provider="azure")
        log.info("list_regions_started")

        odata = "serviceName eq 'Virtual Machines'"
        raw = await self._fetch_all(odata_filter=odata, max_results=_PAGE_SIZE)

        regions = sorted({
            item.get("armRegionName", "")
            for item in raw
            if item.get("armRegionName")
        })

        log.info("list_regions_completed", count=len(regions))
        return regions
