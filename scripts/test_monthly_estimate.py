"""Quick assertion: monthly_cost_estimate handles reservations correctly."""

from datetime import datetime

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import HOURS_PER_MONTH, NormalizedPriceItem, PricingTier

# Reserved 1yr: $992 total → $82.67/month
reserved = NormalizedPriceItem(
    provider=CloudProvider.AZURE, service_name="VM", service_category=ServiceCategory.COMPUTE,
    sku_id="x", sku_name="D4s_v5", product_name="VM Dsv5", region="eastus",
    retail_price=992.0, unit_price=992.0, unit_of_measure="1 Hour",
    pricing_tier=PricingTier.RESERVED_1YR, reservation_term="1 Year",
    effective_date=datetime(2024, 1, 1),
)
assert abs(reserved.monthly_cost_estimate - 82.666667) < 0.01
print(f"✅ Reserved 1yr: $992 total → ${reserved.monthly_cost_estimate:.2f}/mo")

# Reserved 3yr: $1867 total → $51.86/month
reserved3 = NormalizedPriceItem(
    provider=CloudProvider.AZURE, service_name="VM", service_category=ServiceCategory.COMPUTE,
    sku_id="x", sku_name="D4s_v5", product_name="VM Dsv5", region="eastus",
    retail_price=1867.0, unit_price=1867.0, unit_of_measure="1 Hour",
    pricing_tier=PricingTier.RESERVED_3YR, reservation_term="3 Years",
    effective_date=datetime(2024, 1, 1),
)
assert abs(reserved3.monthly_cost_estimate - 51.861111) < 0.01
print(f"✅ Reserved 3yr: $1867 total → ${reserved3.monthly_cost_estimate:.2f}/mo")

# On-demand: $0.192/hr × 730 = $140.16/month
od = NormalizedPriceItem(
    provider=CloudProvider.AZURE, service_name="VM", service_category=ServiceCategory.COMPUTE,
    sku_id="x", sku_name="D4s_v5", product_name="VM Dsv5", region="eastus",
    retail_price=0.192, unit_price=0.192, unit_of_measure="1 Hour",
    pricing_tier=PricingTier.ON_DEMAND,
    effective_date=datetime(2024, 1, 1),
)
assert abs(od.monthly_cost_estimate - 140.16) < 0.01
print(f"✅ On-demand: $0.192/hr → ${od.monthly_cost_estimate:.2f}/mo")

# Per-request pricing: should return None (can't auto-convert)
func = NormalizedPriceItem(
    provider=CloudProvider.AZURE, service_name="Functions", service_category=ServiceCategory.SERVERLESS_FUNCTION,
    sku_id="x", sku_name="func", product_name="Functions", region="eastus",
    retail_price=0.000004, unit_price=0.000004, unit_of_measure="10",
    pricing_tier=PricingTier.ON_DEMAND,
    effective_date=datetime(2024, 1, 1),
)
assert func.monthly_cost_estimate is None
print(f"✅ Per-request: ${func.unit_price}/10-units → monthly=None (correct)")

print("\n✅ All monthly_cost_estimate calculations verified!")
