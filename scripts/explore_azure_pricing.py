"""Explore Azure Retail Prices API — discover real response shapes.

This script calls the LIVE public (no auth) Azure pricing API
for several service categories and dumps the first item from each
to help us design normalized data models.
"""

import asyncio
import json

import httpx

BASE_URL = "https://prices.azure.com/api/retail/prices"
REGION = "eastus"

# Services we care about — covering compute, serverless, DB, K8s, storage, networking
QUERIES = {
    "Virtual Machines": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Virtual Machines' "
        f"and priceType eq 'Consumption' and contains(armSkuName, 'Standard_D4s_v5')"
    ),
    "Functions": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Functions'"
    ),
    "Azure Kubernetes Service": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Azure Kubernetes Service'"
    ),
    "SQL Database": (
        f"armRegionName eq '{REGION}' and serviceName eq 'SQL Database' "
        f"and priceType eq 'Consumption'"
    ),
    "Storage": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Storage' "
        f"and priceType eq 'Consumption'"
    ),
    "Load Balancer": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Load Balancer'"
    ),
    "Azure App Service": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Azure App Service' "
        f"and priceType eq 'Consumption'"
    ),
    "Container Instances": (
        f"armRegionName eq '{REGION}' and serviceName eq 'Container Instances'"
    ),
}


async def fetch_first_items(service_name: str, odata_filter: str, n: int = 2) -> list[dict]:
    """Fetch the first n items from Azure Retail Prices for a given filter."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(BASE_URL, params={"$filter": odata_filter})
        resp.raise_for_status()
        data = resp.json()
        items = data.get("Items", [])
        return items[:n]


async def main() -> None:
    print("=" * 80)
    print("AZURE RETAIL PRICES API — LIVE EXPLORATION")
    print("=" * 80)

    for service_name, odata_filter in QUERIES.items():
        print(f"\n{'─' * 80}")
        print(f"SERVICE: {service_name}")
        print(f"FILTER:  {odata_filter}")
        print(f"{'─' * 80}")

        try:
            items = await fetch_first_items(service_name, odata_filter)
            if not items:
                print("  ⚠️  No results returned")
                continue

            print(f"  ✅ Got {len(items)} item(s)")
            for i, item in enumerate(items):
                print(f"\n  --- Item {i + 1} ---")
                # Print all fields, sorted for readability
                for key in sorted(item.keys()):
                    val = item[key]
                    print(f"    {key:30s} = {json.dumps(val)}")

            # Also show unique keys
            all_keys = set()
            for item in items:
                all_keys.update(item.keys())
            print(f"\n  FIELDS ({len(all_keys)}): {sorted(all_keys)}")

        except Exception as e:
            print(f"  ❌ Error: {e}")

    print(f"\n{'=' * 80}")
    print("DONE — Use these shapes to design normalized models")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    asyncio.run(main())
