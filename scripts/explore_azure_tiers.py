"""Explore Azure Retail Prices API — reserved + savings plan pricing shapes."""

import asyncio
import json

import httpx

BASE_URL = "https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview"
REGION = "eastus"

QUERIES = {
    "VM Reserved 1yr": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Virtual Machines' "
        f"and priceType eq 'Reservation' and contains(armSkuName, 'Standard_D4s_v5') "
        f"and reservationTerm eq '1 Year'"
    ),
    "VM Reserved 3yr": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Virtual Machines' "
        f"and priceType eq 'Reservation' and contains(armSkuName, 'Standard_D4s_v5') "
        f"and reservationTerm eq '3 Years'"
    ),
    "VM Spot": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Virtual Machines' "
        f"and contains(meterName, 'Spot') and contains(armSkuName, 'Standard_D4s_v5')"
    ),
    "VM Savings Plan (preview API)": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Virtual Machines' "
        f"and priceType eq 'Consumption' and contains(armSkuName, 'Standard_D4s_v5') "
        f"and isPrimaryMeterRegion eq true"
    ),
    "All serviceFamily values (distinct)": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Virtual Machines' "
        f"and priceType eq 'Consumption' and isPrimaryMeterRegion eq true "
        f"and contains(armSkuName, 'Standard_D4s_v5')"
    ),
}


async def fetch_items(odata_filter: str, n: int = 3) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(BASE_URL, params={"$filter": odata_filter})
        resp.raise_for_status()
        data = resp.json()
        return data.get("Items", [])[:n]


async def main() -> None:
    print("=" * 80)
    print("AZURE PRICING — COMMITMENT TIERS + SAVINGS PLAN EXPLORATION")
    print("=" * 80)

    for label, odata_filter in QUERIES.items():
        print(f"\n{'─' * 80}")
        print(f"QUERY: {label}")
        print(f"{'─' * 80}")

        try:
            items = await fetch_items(odata_filter)
            if not items:
                print("  ⚠️  No results")
                continue

            for i, item in enumerate(items):
                print(f"\n  --- Item {i + 1} ---")
                # Show key pricing fields only
                fields = [
                    "armSkuName", "skuName", "meterName", "productName",
                    "retailPrice", "unitPrice", "unitOfMeasure", "type",
                    "reservationTerm", "savingsPlan", "serviceFamily",
                ]
                for key in fields:
                    if key in item:
                        val = item[key]
                        print(f"    {key:30s} = {json.dumps(val)}")

                # Show any extra fields not in the base 20
                base_keys = {
                    "armRegionName", "armSkuName", "currencyCode",
                    "effectiveStartDate", "isPrimaryMeterRegion", "location",
                    "meterId", "meterName", "productId", "productName",
                    "retailPrice", "serviceFamily", "serviceId", "serviceName",
                    "skuId", "skuName", "tierMinimumUnits", "type",
                    "unitOfMeasure", "unitPrice",
                }
                extras = set(item.keys()) - base_keys
                if extras:
                    print(f"    {'EXTRA FIELDS':30s} = {sorted(extras)}")
                    for ek in sorted(extras):
                        print(f"    {ek:30s} = {json.dumps(item[ek])}")

        except Exception as e:
            print(f"  ❌ Error: {e}")

    # Also fetch distinct serviceFamily values
    print(f"\n{'─' * 80}")
    print("DISTINCT serviceFamily values (from first 1000 items)")
    print(f"{'─' * 80}")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            BASE_URL,
            params={"$filter": f"armRegionName eq '{REGION}' and isPrimaryMeterRegion eq true"},
        )
        data = resp.json()
        families = set()
        service_names = set()
        for item in data.get("Items", []):
            families.add(item.get("serviceFamily", ""))
            service_names.add(item.get("serviceName", ""))
        print(f"  serviceFamilies: {sorted(families)}")
        print(f"  serviceNames (first 1000): {sorted(service_names)[:30]}...")

    print(f"\n{'=' * 80}")
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
