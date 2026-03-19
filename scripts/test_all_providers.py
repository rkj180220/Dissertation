"""Smoke test: verify all 3 provider adapters import and pass structural checks."""

from src.providers import (
    BaseCloudProvider,
    AWSPricingProvider,
    AzurePricingProvider,
    GCPPricingProvider,
)

print("✅ All 4 exports import cleanly")

# Verify class hierarchy
for cls in [AWSPricingProvider, AzurePricingProvider, GCPPricingProvider]:
    assert issubclass(cls, BaseCloudProvider), f"{cls.__name__} is not a BaseCloudProvider"
    print(f"  ✅ {cls.__name__} → BaseCloudProvider")

# Verify CloudProvider enum on each
from src.models.cloud_resource import CloudProvider

assert AWSPricingProvider.provider == CloudProvider.AWS
assert AzurePricingProvider.provider == CloudProvider.AZURE
assert GCPPricingProvider.provider == CloudProvider.GCP
print("  ✅ All .provider enums correct")

# Verify abstract interface methods exist
for cls in [AWSPricingProvider, AzurePricingProvider, GCPPricingProvider]:
    inst = cls()
    for method in ["search_prices", "get_sku_prices", "list_regions"]:
        assert hasattr(inst, method), f"{cls.__name__} missing {method}"
    print(f"  ✅ {cls.__name__} has all 3 abstract methods")

# Verify GCP helper functions
from src.providers.gcp_provider import (
    _resolve_pricing_tier,
    _resolve_service_category,
    _units_nanos_to_float,
    _normalise_usage_unit,
    _SERVICE_DISPLAY_NAME_MAP,
    _CATEGORY_TO_DISPLAY_NAMES,
    _RESOURCE_FAMILY_MAP,
)

print(f"  ✅ {len(_SERVICE_DISPLAY_NAME_MAP)} GCP service→category mappings")
print(f"  ✅ {len(_CATEGORY_TO_DISPLAY_NAMES)} category→service reverse mappings")

# Verify tier resolution
from src.models.pricing import PricingTier

assert _resolve_pricing_tier("OnDemand") == PricingTier.ON_DEMAND
assert _resolve_pricing_tier("Preemptible") == PricingTier.SPOT
assert _resolve_pricing_tier("Commit1Yr") == PricingTier.RESERVED_1YR
assert _resolve_pricing_tier("Commit3Yr") == PricingTier.RESERVED_3YR
print("  ✅ Pricing tier resolution correct")

# Verify price calculation
assert _units_nanos_to_float(1, 500_000_000) == 1.5
assert _units_nanos_to_float(0, 250_000_000) == 0.25
assert _units_nanos_to_float(0, 0) == 0.0
print("  ✅ units+nanos→float conversion correct")

# Verify unit normalisation
assert _normalise_usage_unit("h") == "1 Hour"
assert _normalise_usage_unit("GiBy") == "1 GB"
assert _normalise_usage_unit("GiBy.mo") == "1 GB/Month"
print("  ✅ Usage unit normalisation correct")

# Verify AWS helper functions
from src.providers.aws_provider import (
    _SERVICE_CODE_MAP,
    _CATEGORY_TO_SERVICE_CODES,
    _REGION_TO_LOCATION,
)

print(f"  ✅ {len(_SERVICE_CODE_MAP)} AWS service→category mappings")
print(f"  ✅ {len(_CATEGORY_TO_SERVICE_CODES)} category→service reverse mappings")
print(f"  ✅ {len(_REGION_TO_LOCATION)} AWS region→location mappings")

# Verify ServiceCategory coverage for each provider
from src.models.cloud_resource import ServiceCategory
from src.providers.azure_provider import _SERVICE_NAME_MAP as AZURE_MAP

aws_categories = set(_SERVICE_CODE_MAP.values())
azure_categories = set(AZURE_MAP.values())
gcp_categories = set(_SERVICE_DISPLAY_NAME_MAP.values())

all_categories = set(ServiceCategory)
print(f"\n  AWS covers {len(aws_categories)}/{len(all_categories)} categories")
print(f"  Azure covers {len(azure_categories)}/{len(all_categories)} categories")
print(f"  GCP covers {len(gcp_categories)}/{len(all_categories)} categories")

print("\n🎉 All checks passed — providers package fully operational")
