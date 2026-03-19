"""Explore AWS Pricing API response shapes for multiple service types.

Uses boto3 GetProducts to fetch a few items from each service and
print the raw response structure so we can design the normalizer.

NOTE: AWS Pricing API is only available in us-east-1 and ap-south-1.
      Requires valid AWS credentials (IAM/SSO).
"""

import json

import boto3

client = boto3.client("pricing", region_name="us-east-1")

# Services to explore
SERVICES = {
    "AmazonEC2": {
        "filters": [
            {"Type": "TERM_MATCH", "Field": "location", "Value": "US East (N. Virginia)"},
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": "m5.xlarge"},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
            {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        ],
    },
    "AWSLambda": {
        "filters": [
            {"Type": "TERM_MATCH", "Field": "location", "Value": "US East (N. Virginia)"},
        ],
    },
    "AmazonRDS": {
        "filters": [
            {"Type": "TERM_MATCH", "Field": "location", "Value": "US East (N. Virginia)"},
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": "db.m5.xlarge"},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": "PostgreSQL"},
        ],
    },
    "AmazonEKS": {
        "filters": [
            {"Type": "TERM_MATCH", "Field": "location", "Value": "US East (N. Virginia)"},
        ],
    },
    "AmazonS3": {
        "filters": [
            {"Type": "TERM_MATCH", "Field": "location", "Value": "US East (N. Virginia)"},
        ],
    },
}


def explore_service(service_code: str, filters: list[dict]) -> None:
    """Fetch and print products for a given service."""
    print(f"\n{'='*80}")
    print(f"SERVICE: {service_code}")
    print(f"{'='*80}")

    try:
        resp = client.get_products(
            ServiceCode=service_code,
            Filters=filters,
            MaxResults=2,
        )

        price_list = resp.get("PriceList", [])
        print(f"Got {len(price_list)} items")

        for i, raw in enumerate(price_list[:2]):
            data = json.loads(raw) if isinstance(raw, str) else raw
            product = data.get("product", {})
            attrs = product.get("attributes", {})
            terms = data.get("terms", {})

            print(f"\n--- Item {i+1} ---")
            print(f"  SKU: {product.get('sku', 'N/A')}")
            print(f"  productFamily: {product.get('productFamily', 'N/A')}")

            # Print ALL attributes
            print(f"  attributes ({len(attrs)} fields):")
            for k, v in sorted(attrs.items()):
                print(f"    {k}: {v}")

            # Print pricing terms
            for term_type in ["OnDemand", "Reserved"]:
                term_data = terms.get(term_type, {})
                if term_data:
                    print(f"  terms.{term_type}:")
                    for sku_key, sku_val in term_data.items():
                        price_dims = sku_val.get("priceDimensions", {})
                        offer_attrs = sku_val.get("termAttributes", {})
                        if offer_attrs:
                            print(f"    termAttributes: {offer_attrs}")
                        for dim_key, dim_val in price_dims.items():
                            usd = dim_val.get("pricePerUnit", {}).get("USD", "?")
                            unit = dim_val.get("unit", "?")
                            desc = dim_val.get("description", "")
                            print(f"    ${usd}/{unit} — {desc[:80]}")

    except Exception as e:
        print(f"  ERROR: {e}")


for svc, cfg in SERVICES.items():
    explore_service(svc, cfg["filters"])

print("\n\n✅ Done exploring AWS Pricing API shapes")
