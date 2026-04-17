"""AWS Pricing API provider adapter.

Fetches pricing data via the **boto3 Pricing API** and normalises
every item into ``NormalizedPriceItem``.

API reference:
    https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/price-changes.html

Key properties of the API:
    * Requires IAM authentication (access key / SSO / role).
    * Only available in **us-east-1** and **ap-south-1**.
    * ``GetProducts`` accepts ``ServiceCode`` + attribute ``Filters``.
    * Each item in ``PriceList`` is a **stringified JSON** blob containing
      ``product`` (attributes) + ``terms`` (OnDemand / Reserved).
    * Product attributes are **service-specific** — EC2 has ``instanceType``
      and ``vcpu``, Lambda has ``group`` and ``usagetype``, etc.
    * Reserved terms carry ``termAttributes`` with ``LeaseContractLength``
      and ``PurchaseOption``.
    * Pagination via ``NextToken``; max 100 items per request.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import structlog
from langfuse import observe

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.providers.base_provider import BaseCloudProvider

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PRICING_REGION = "us-east-1"
"""AWS Pricing API is only available in us-east-1 and ap-south-1."""

# ---------------------------------------------------------------------------
# AWS ServiceCode → ServiceCategory mapping
# ---------------------------------------------------------------------------

_SERVICE_CODE_MAP: dict[str, ServiceCategory] = {
    # Compute
    "AmazonEC2": ServiceCategory.COMPUTE,
    # Serverless compute
    "AmazonAppRunner": ServiceCategory.SERVERLESS_COMPUTE,
    "AmazonLightsail": ServiceCategory.SERVERLESS_COMPUTE,
    # Containers
    "AmazonEKS": ServiceCategory.CONTAINER,
    "AmazonECS": ServiceCategory.CONTAINER,
    "AWSFargate": ServiceCategory.CONTAINER,
    # Serverless functions
    "AWSLambda": ServiceCategory.SERVERLESS_FUNCTION,
    # Databases
    "AmazonRDS": ServiceCategory.DATABASE,
    "AmazonDynamoDB": ServiceCategory.DATABASE,
    "AmazonElastiCache": ServiceCategory.DATABASE,
    "AmazonRedshift": ServiceCategory.DATABASE,
    "AmazonNeptune": ServiceCategory.DATABASE,
    "AmazonDocDB": ServiceCategory.DATABASE,
    "AmazonMemoryDB": ServiceCategory.DATABASE,
    # Storage
    "AmazonS3": ServiceCategory.STORAGE,
    "AmazonEBS": ServiceCategory.STORAGE,
    "AmazonEFS": ServiceCategory.STORAGE,
    "AmazonFSx": ServiceCategory.STORAGE,
    "AmazonGlacier": ServiceCategory.STORAGE,
    # Networking
    "AmazonVPC": ServiceCategory.NETWORKING,
    "AmazonCloudFront": ServiceCategory.NETWORKING,
    "AmazonRoute53": ServiceCategory.NETWORKING,
    "AWSELB": ServiceCategory.NETWORKING,
    "AWSGlobalAccelerator": ServiceCategory.NETWORKING,
    # AI/ML
    "AmazonSageMaker": ServiceCategory.AI_ML,
    "AmazonBedrock": ServiceCategory.AI_ML,
    "AmazonRekognition": ServiceCategory.AI_ML,
    "AmazonComprehend": ServiceCategory.AI_ML,
    # Analytics
    "AmazonAthena": ServiceCategory.ANALYTICS,
    "AmazonEMR": ServiceCategory.ANALYTICS,
    "AmazonKinesis": ServiceCategory.ANALYTICS,
    "AWSGlue": ServiceCategory.ANALYTICS,
    # Management
    "AmazonCloudWatch": ServiceCategory.MANAGEMENT,
    "AWSCloudTrail": ServiceCategory.MANAGEMENT,
    "AWSConfig": ServiceCategory.MANAGEMENT,
    # Security
    "awswaf": ServiceCategory.SECURITY,
    "AWSKeyManagementService": ServiceCategory.SECURITY,
    "AmazonGuardDuty": ServiceCategory.SECURITY,
    "AWSSecretsManager": ServiceCategory.SECURITY,
    # Integration
    "AmazonSNS": ServiceCategory.INTEGRATION,
    "AmazonSQS": ServiceCategory.INTEGRATION,
    "AmazonMQ": ServiceCategory.INTEGRATION,
    "AmazonApiGateway": ServiceCategory.INTEGRATION,
    "AWSStepFunctions": ServiceCategory.INTEGRATION,
    # IoT
    "AWSIoT": ServiceCategory.IOT,
}

# Reverse mapping: ServiceCategory → representative AWS ServiceCodes
_CATEGORY_TO_SERVICE_CODES: dict[ServiceCategory, list[str]] = {
    ServiceCategory.COMPUTE: ["AmazonEC2"],
    ServiceCategory.SERVERLESS_COMPUTE: ["AmazonAppRunner", "AmazonLightsail"],
    ServiceCategory.CONTAINER: ["AmazonEKS", "AmazonECS", "AWSFargate"],
    ServiceCategory.SERVERLESS_FUNCTION: ["AWSLambda"],
    ServiceCategory.DATABASE: ["AmazonRDS", "AmazonDynamoDB", "AmazonElastiCache"],
    ServiceCategory.STORAGE: ["AmazonS3", "AmazonEBS", "AmazonEFS"],
    ServiceCategory.NETWORKING: ["AWSELB", "AmazonCloudFront", "AmazonRoute53"],
    ServiceCategory.AI_ML: ["AmazonSageMaker", "AmazonBedrock"],
    ServiceCategory.ANALYTICS: ["AmazonAthena", "AmazonEMR", "AWSGlue"],
    ServiceCategory.MANAGEMENT: ["AmazonCloudWatch"],
    ServiceCategory.SECURITY: ["awswaf", "AWSKeyManagementService"],
    ServiceCategory.INTEGRATION: ["AmazonSNS", "AmazonSQS", "AmazonApiGateway"],
    ServiceCategory.IOT: ["AWSIoT"],
}

# ---------------------------------------------------------------------------
# AWS region code → human-readable location (used in Pricing API filters)
# ---------------------------------------------------------------------------

_REGION_TO_LOCATION: dict[str, str] = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-central-2": "EU (Zurich)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "sa-east-1": "South America (Sao Paulo)",
    "ca-central-1": "Canada (Central)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "af-south-1": "Africa (Cape Town)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
}

_LOCATION_TO_REGION: dict[str, str] = {v: k for k, v in _REGION_TO_LOCATION.items()}


# ---------------------------------------------------------------------------
# AWS term parsing → PricingTier
# ---------------------------------------------------------------------------


def _resolve_pricing_tier(
    term_type: str,
    term_attributes: dict[str, str],
) -> PricingTier:
    """Map an AWS pricing term to a ``PricingTier`` enum value.

    Args:
        term_type: "OnDemand" or "Reserved".
        term_attributes: The ``termAttributes`` dict from the reserved
            term (empty for OnDemand).
    """
    if term_type == "Reserved":
        lease = term_attributes.get("LeaseContractLength", "")
        if "3" in lease:
            return PricingTier.RESERVED_3YR
        return PricingTier.RESERVED_1YR
    return PricingTier.ON_DEMAND


def _resolve_service_category(service_code: str) -> ServiceCategory:
    """Map an AWS ServiceCode to a ``ServiceCategory`` enum value."""
    return _SERVICE_CODE_MAP.get(service_code, ServiceCategory.OTHER)


# ---------------------------------------------------------------------------
# AWS → NormalizedPriceItem transformer
# ---------------------------------------------------------------------------


def _parse_product(
    raw_json: str | dict,
    service_code: str,
) -> list[NormalizedPriceItem]:
    """Parse a single AWS PriceList item into ``NormalizedPriceItem`` rows.

    One AWS product can yield **multiple** rows — one per pricing
    dimension × term type (OnDemand hourly, Reserved upfront, etc.).

    Args:
        raw_json: Stringified or already-parsed JSON from ``PriceList``.
        service_code: The AWS ServiceCode used in the query.

    Returns:
        List of normalised price items (typically 1–5 per product).
    """
    data: dict[str, Any] = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    product: dict[str, Any] = data.get("product", {})
    attrs: dict[str, str] = product.get("attributes", {})
    terms: dict[str, Any] = data.get("terms", {})
    sku: str = product.get("sku", "")

    # Build common fields
    service_category = _resolve_service_category(service_code)
    product_family = attrs.get("productFamily", attrs.get("group", ""))

    # Region: AWS uses human-readable "location" in attributes
    location = attrs.get("location", "")
    region = _LOCATION_TO_REGION.get(location, location)

    # SKU name: use instanceType for EC2/RDS, group for Lambda, etc.
    sku_name = (
        attrs.get("instanceType", "")
        or attrs.get("instancetype", "")
        or attrs.get("usagetype", "")
        or sku
    )

    product_name = product_family or service_code

    items: list[NormalizedPriceItem] = []

    for term_type in ["OnDemand", "Reserved"]:
        term_block = terms.get(term_type, {})
        for _offer_key, offer in term_block.items():
            term_attributes = offer.get("termAttributes", {})
            pricing_tier = _resolve_pricing_tier(term_type, term_attributes)

            # Reservation term label
            reservation_term: str | None = None
            if term_type == "Reserved":
                lease = term_attributes.get("LeaseContractLength", "")
                reservation_term = lease if lease else None

            price_dims = offer.get("priceDimensions", {})
            for _dim_key, dim in price_dims.items():
                price_str = dim.get("pricePerUnit", {}).get("USD", "0")
                try:
                    price_usd = float(price_str)
                except (ValueError, TypeError):
                    price_usd = 0.0

                # Skip $0 dimensions (e.g. "free tier" rows) unless
                # they're the only dimension
                if price_usd == 0.0 and len(price_dims) > 1:
                    continue

                unit = dim.get("unit", "")
                description = dim.get("description", "")

                items.append(
                    NormalizedPriceItem(
                        provider=CloudProvider.AWS,
                        service_name=service_code,
                        service_category=service_category,
                        sku_id=sku,
                        sku_name=sku_name,
                        product_name=product_name,
                        meter_name=description[:120] if description else "",
                        region=region,
                        retail_price=price_usd,
                        unit_price=price_usd,
                        currency="USD",
                        unit_of_measure=_normalise_unit(unit),
                        pricing_tier=pricing_tier,
                        reservation_term=reservation_term,
                        effective_date=datetime.now(tz=timezone.utc),
                        attributes={
                            k: v for k, v in attrs.items()
                            if k in _INTERESTING_ATTRS
                        },
                    )
                )

    return items


def _normalise_unit(unit: str) -> str:
    """Normalise AWS billing units to a consistent format.

    AWS uses inconsistent casing: "Hrs", "hrs", "GB-Mo", "GB", etc.
    We map common variants to match Azure-style "1 Hour", "1 GB/Month".
    """
    mapping: dict[str, str] = {
        "Hrs": "1 Hour",
        "hrs": "1 Hour",
        "Hours": "1 Hour",
        "GB-Mo": "1 GB/Month",
        "GB": "1 GB",
        "Requests": "1 Request",
        "Request": "1 Request",
        "Lambda-GB-Second": "1 GB Second",
        "Second": "1 Second",
        "Queries": "1 Query",
        "Keys": "1 Key",
    }
    return mapping.get(unit, unit)


# Subset of AWS product attributes worth keeping in the normalised extras
_INTERESTING_ATTRS: set[str] = {
    "instanceType",
    "vcpu",
    "memory",
    "storage",
    "networkPerformance",
    "physicalProcessor",
    "processorArchitecture",
    "operatingSystem",
    "databaseEngine",
    "instanceFamily",
    "currentGeneration",
    "gpu",
    "gpuMemory",
    "usagetype",
    "group",
    "storageClass",
    "volumeApiName",
    "volumeType",
    "maxIopsvolume",
    "maxThroughputvolume",
}


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------


class AWSPricingProvider(BaseCloudProvider):
    """AWS Pricing API adapter.

    Uses ``boto3`` to call ``GetProducts``, parses the stringified
    JSON responses, and returns ``NormalizedPriceItem`` instances.

    The boto3 Pricing client is **synchronous**; all calls are wrapped
    with ``asyncio.to_thread`` to avoid blocking the event loop.

    Args:
        region: AWS region for the Pricing API endpoint (must be
            us-east-1 or ap-south-1).
    """

    provider = CloudProvider.AWS

    def __init__(
        self,
        *,
        region: str = _PRICING_REGION,
        access_key_id: str = "",
        secret_access_key: str = "",
        session_token: str = "",
    ) -> None:
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._client = None  # lazy-init

    def _get_client(self):
        """Lazily create the boto3 Pricing client.

        If explicit credentials were provided, they are passed to the
        client.  Otherwise boto3 falls back to its default credential
        chain (env vars → shared-credentials → instance profile).

        Returns:
            A ``boto3.client('pricing')`` instance.
        """
        if self._client is None:
            import boto3

            kwargs: dict[str, str] = {"region_name": self._region}
            if self._access_key_id and self._secret_access_key:
                kwargs["aws_access_key_id"] = self._access_key_id
                kwargs["aws_secret_access_key"] = self._secret_access_key
                if self._session_token:
                    kwargs["aws_session_token"] = self._session_token
            self._client = boto3.client("pricing", **kwargs)
        return self._client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_filters(
        self,
        *,
        region: str | None = None,
        sku_name: str | None = None,
        extra_filters: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        """Build boto3-style filter dicts for ``GetProducts``.

        Args:
            region: AWS region code (converted to location name).
            sku_name: Instance type / SKU name filter.
            extra_filters: Additional ``{Type, Field, Value}`` dicts.

        Returns:
            List of filter dicts.
        """
        filters: list[dict[str, str]] = []

        if region:
            location = _REGION_TO_LOCATION.get(region, region)
            filters.append(
                {"Type": "TERM_MATCH", "Field": "location", "Value": location}
            )

        if sku_name:
            # instanceType is the most common filter for EC2/RDS
            filters.append(
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": sku_name}
            )

        if extra_filters:
            filters.extend(extra_filters)

        return filters

    @observe(name="aws_get_products")
    async def _fetch_products(
        self,
        service_code: str,
        filters: list[dict[str, str]],
        max_results: int = 100,
    ) -> list[NormalizedPriceItem]:
        """Fetch and parse products from the AWS Pricing API.

        Args:
            service_code: AWS service code (e.g. "AmazonEC2").
            filters: List of boto3-style filter dicts.
            max_results: Maximum items to collect.

        Returns:
            List of normalised price items.
        """
        log = logger.bind(
            provider="aws",
            service_code=service_code,
            filters=str(filters)[:200],
        )
        log.debug("aws_fetch_started", max_results=max_results)

        all_items: list[NormalizedPriceItem] = []
        next_token: str | None = None
        pages = 0

        client = self._get_client()

        while len(all_items) < max_results:
            kwargs: dict[str, Any] = {
                "ServiceCode": service_code,
                "Filters": filters,
                "MaxResults": min(100, max_results - len(all_items)),
            }
            if next_token:
                kwargs["NextToken"] = next_token

            # boto3 is sync — wrap in thread
            response = await asyncio.to_thread(client.get_products, **kwargs)

            price_list = response.get("PriceList", [])
            pages += 1

            for raw in price_list:
                parsed = _parse_product(raw, service_code)
                all_items.extend(parsed)

            log.debug(
                "aws_page_fetched",
                page=pages,
                products_this_page=len(price_list),
                items_so_far=len(all_items),
            )

            next_token = response.get("NextToken")
            if not next_token:
                break

        result = all_items[:max_results]
        log.info(
            "aws_fetch_completed",
            service_code=service_code,
            pages=pages,
            total_items=len(result),
        )
        return result

    # ------------------------------------------------------------------
    # BaseCloudProvider interface
    # ------------------------------------------------------------------

    @observe(name="aws_search_prices")
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
        """Search AWS pricing with flexible filters.

        ``service_name`` is interpreted as an AWS ``ServiceCode``
        (e.g. ``"AmazonEC2"``).  When only ``service_category`` is
        provided, the adapter queries representative service codes
        for that category.

        Args:
            service_name: AWS ServiceCode.
            service_category: Normalised category.
            region: AWS region code (e.g. "us-east-1").
            sku_name: Instance type / SKU name.
            pricing_tier: Filter by commitment tier (client-side).
            max_results: Maximum items to return.

        Returns:
            List of ``NormalizedPriceItem``.
        """
        log = logger.bind(
            provider="aws",
            service_name=service_name,
            service_category=service_category,
            region=region,
            sku_name=sku_name,
            pricing_tier=pricing_tier,
        )
        log.info("search_prices_started", max_results=max_results)

        # Resolve which service codes to query
        service_codes: list[str]
        if service_name:
            service_codes = [service_name]
        elif service_category and service_category in _CATEGORY_TO_SERVICE_CODES:
            service_codes = _CATEGORY_TO_SERVICE_CODES[service_category]
        else:
            log.warning("search_prices_no_filter", msg="No service_name or known category")
            return []

        filters = self._build_filters(region=region, sku_name=sku_name)

        all_items: list[NormalizedPriceItem] = []
        per_svc_limit = max(max_results // len(service_codes), 20)

        for svc_code in service_codes:
            items = await self._fetch_products(svc_code, filters, per_svc_limit)
            all_items.extend(items)
            if len(all_items) >= max_results:
                break

        # Client-side filtering
        if pricing_tier:
            all_items = [i for i in all_items if i.pricing_tier == pricing_tier]

        if service_category:
            all_items = [
                i for i in all_items if i.service_category == service_category
            ]

        result = all_items[:max_results]
        log.info("search_prices_completed", count=len(result))
        return result

    @observe(name="aws_get_sku_prices")
    async def get_sku_prices(
        self,
        sku_name: str,
        region: str,
    ) -> list[NormalizedPriceItem]:
        """Get all pricing tiers for a specific AWS instance type.

        Queries ``AmazonEC2`` by default; also works for RDS instance
        types (``db.m5.xlarge``) which are queried via ``AmazonRDS``.

        Args:
            sku_name: Instance type (e.g. "m5.xlarge", "db.m5.xlarge").
            region: AWS region code (e.g. "us-east-1").

        Returns:
            All pricing rows for that instance type.
        """
        log = logger.bind(provider="aws", sku_name=sku_name, region=region)
        log.info("get_sku_prices_started")

        # Determine service code from SKU name prefix
        if sku_name.startswith("db."):
            service_code = "AmazonRDS"
        elif sku_name.startswith("cache."):
            service_code = "AmazonElastiCache"
        else:
            service_code = "AmazonEC2"

        filters = self._build_filters(region=region, sku_name=sku_name)
        items = await self._fetch_products(service_code, filters, max_results=50)

        log.info("get_sku_prices_completed", count=len(items))
        return items

    @observe(name="aws_list_regions")
    async def list_regions(self) -> list[str]:
        """Return AWS regions known to the pricing adapter.

        Returns the static mapping of region codes rather than making
        an API call (the Pricing API doesn't have a list-regions
        endpoint).
        """
        log = logger.bind(provider="aws")
        log.info("list_regions_started")

        regions = sorted(_REGION_TO_LOCATION.keys())

        log.info("list_regions_completed", count=len(regions))
        return regions
