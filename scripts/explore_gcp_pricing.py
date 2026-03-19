"""Explore GCP Cloud Billing Catalog API response shapes.

Uses the google-cloud-billing SDK to list services and fetch
a few SKUs, printing the raw response structure.

NOTE: Requires either:
  - GOOGLE_APPLICATION_CREDENTIALS env var pointing to a service account JSON
  - gcloud auth application-default login
"""

from google.cloud import billing_v1


def explore_gcp_billing():
    """List GCP services and fetch sample SKUs."""
    client = billing_v1.CloudCatalogClient()

    # 1. List services
    print("=== GCP Billing Services ===")
    services = list(client.list_services())
    print(f"Total services: {len(services)}")

    # Show a few interesting ones
    target_names = [
        "Compute Engine",
        "Cloud Functions",
        "Cloud SQL",
        "Kubernetes Engine",
        "Cloud Storage",
    ]

    target_services = {}
    for svc in services:
        if svc.display_name in target_names:
            target_services[svc.display_name] = svc
            print(f"  {svc.display_name}: name={svc.name}, service_id={svc.service_id}")

    # 2. Fetch first 3 SKUs from each target service
    for display_name, svc in target_services.items():
        print(f"\n{'='*80}")
        print(f"SERVICE: {display_name} ({svc.name})")
        print(f"{'='*80}")

        skus = client.list_skus(parent=svc.name)
        for i, sku in enumerate(skus):
            if i >= 3:
                break

            print(f"\n--- SKU {i+1} ---")
            print(f"  name: {sku.name}")
            print(f"  sku_id: {sku.sku_id}")
            print(f"  description: {sku.description}")
            print(f"  category:")
            print(f"    service_display_name: {sku.category.service_display_name}")
            print(f"    resource_family: {sku.category.resource_family}")
            print(f"    resource_group: {sku.category.resource_group}")
            print(f"    usage_type: {sku.category.usage_type}")

            print(f"  service_regions: {list(sku.service_regions)[:5]}")

            for j, pi in enumerate(sku.pricing_info):
                print(f"  pricing_info[{j}]:")
                print(f"    effective_time: {pi.effective_time}")
                print(f"    summary: {pi.summary}")
                expr = pi.pricing_expression
                print(f"    usage_unit: {expr.usage_unit}")
                print(f"    usage_unit_description: {expr.usage_unit_description}")
                print(f"    display_quantity: {expr.display_quantity}")
                for k, rate in enumerate(expr.tiered_rates):
                    units = rate.unit_price.units
                    nanos = rate.unit_price.nanos
                    currency = rate.unit_price.currency_code
                    price = units + nanos / 1e9
                    print(
                        f"    tiered_rates[{k}]: "
                        f"start={rate.start_usage_amount}, "
                        f"price={price:.10f} {currency} "
                        f"(units={units}, nanos={nanos})"
                    )

    print("\n\n✅ Done exploring GCP Billing Catalog shapes")


try:
    explore_gcp_billing()
except Exception as e:
    print(f"ERROR: {e}")
    print("\nFalling back to documenting known GCP response shape from API docs...")
    print("""
Known GCP SKU shape (from API docs + prior research):
{
  "name": "services/6F81-5844-456A/skus/0048-21CE-74C3",
  "skuId": "0048-21CE-74C3",
  "description": "N1 Predefined Instance Core running in Americas",
  "category": {
    "serviceDisplayName": "Compute Engine",
    "resourceFamily": "Compute",
    "resourceGroup": "CPU",
    "usageType": "OnDemand"  // or "Preemptible", "Commit1Yr", "Commit3Yr"
  },
  "serviceRegions": ["us-central1", "us-east1", ...],
  "pricingInfo": [{
    "effectiveTime": "2024-01-01T00:00:00Z",
    "summary": "",
    "pricingExpression": {
      "usageUnit": "h",
      "usageUnitDescription": "hour",
      "displayQuantity": 1,
      "tieredRates": [{
        "startUsageAmount": 0,
        "unitPrice": {"currencyCode": "USD", "units": "0", "nanos": 31611000}
      }]
    }
  }]
}
    """)
