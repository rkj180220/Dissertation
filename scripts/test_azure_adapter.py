"""Smoke-test: imports + NormalizedPriceItem construction + live Azure API call."""

import asyncio
from datetime import datetime

# ----- 1. Model imports -----
from src.models import (
    HOURS_PER_MONTH,
    BinPackingResult,
    CloudProvider,
    CloudRecommendation,
    ComplianceReport,
    ComputeSKU,
    ContainerWorkload,
    CostComparison,
    NormalizedPriceItem,
    PricingTier,
    SKUPricing,
    ServiceCategory,
    StorageRequirement,
    StorageSKU,
    VMWorkload,
    WorkloadProfile,
    WorkloadRequest,
)

print("✅ All model imports OK")

# ----- 2. Provider imports -----
from src.providers import AzurePricingProvider, BaseCloudProvider

print("✅ All provider imports OK")

# ----- 3. Construct a NormalizedPriceItem by hand -----
item = NormalizedPriceItem(
    provider=CloudProvider.AZURE,
    service_name="Virtual Machines",
    service_category=ServiceCategory.COMPUTE,
    sku_id="DZH318Z0BPVX/007D",
    sku_name="Standard_D4s_v5",
    product_name="Virtual Machines Dsv5 Series",
    meter_name="D4s v5",
    region="eastus",
    retail_price=0.192,
    unit_price=0.192,
    unit_of_measure="1 Hour",
    pricing_tier=PricingTier.ON_DEMAND,
    effective_date=datetime(2024, 1, 1),
)
assert item.is_hourly is True
assert item.monthly_cost_estimate is not None
print(f"✅ NormalizedPriceItem: {item.sku_name} @ ${item.unit_price}/hr")
print(f"   monthly_est=${item.monthly_cost_estimate:.2f}, HOURS_PER_MONTH={HOURS_PER_MONTH}")


# ----- 4. Live Azure API call -----
async def test_azure_live():
    azure = AzurePricingProvider(timeout=15.0)
    print(f"\n--- Azure adapter: {azure!r} ---")

    # 4a. Search VM pricing in eastus (first 5 items)
    print("\n🔍 Searching: Virtual Machines, eastus, on-demand, max 5...")
    vms = await azure.search_prices(
        service_name="Virtual Machines",
        region="eastus",
        pricing_tier=PricingTier.ON_DEMAND,
        max_results=5,
    )
    print(f"   Got {len(vms)} items:")
    for v in vms:
        monthly = v.monthly_cost_estimate
        monthly_str = f"${monthly:.2f}/mo" if monthly else "N/A"
        print(f"   • {v.sku_name:30s} ${v.unit_price:.4f}/{v.unit_of_measure:15s} ≈ {monthly_str}")

    # 4b. Get all tiers for a specific SKU
    print("\n🔍 All pricing tiers for Standard_D4s_v5 in eastus...")
    tiers = await azure.get_sku_prices("Standard_D4s_v5", "eastus")
    print(f"   Got {len(tiers)} rows:")
    for t in tiers:
        print(
            f"   • {t.pricing_tier.value:20s} "
            f"${t.unit_price:.4f}/{t.unit_of_measure:15s} "
            f"term={t.reservation_term or 'N/A':10s} "
            f"product={t.product_name}"
        )

    # 4c. Search across service categories (Functions)
    print("\n🔍 Searching: Functions, eastus, max 5...")
    funcs = await azure.search_prices(
        service_name="Functions",
        region="eastus",
        max_results=5,
    )
    print(f"   Got {len(funcs)} items:")
    for f in funcs:
        print(
            f"   • {f.meter_name:30s} ${f.unit_price}/{f.unit_of_measure:15s} "
            f"cat={f.service_category.value}"
        )

    # 4d. Search by ServiceCategory (CONTAINER)
    print("\n🔍 Searching: service_category=CONTAINER, eastus, max 5...")
    containers = await azure.search_prices(
        service_category=ServiceCategory.CONTAINER,
        region="eastus",
        max_results=200,  # need more to filter client-side
    )
    # Show first 5
    for c in containers[:5]:
        print(
            f"   • {c.service_name:30s} {c.sku_name:20s} "
            f"${c.unit_price}/{c.unit_of_measure}"
        )
    if not containers:
        print("   (no container items found — client-side category filter needs broader fetch)")

    print("\n✅ All live Azure tests passed!")


asyncio.run(test_azure_live())
