#!/usr/bin/env python3
"""Live smoke test for the AWS Pricing API adapter.

Requires AWS credentials in .env:
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN (if STS)
"""
from __future__ import annotations

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

# Wire boto3 creds from env before importing the adapter
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from src.providers.aws_provider import AWSPricingProvider
from src.models.cloud_resource import ServiceCategory, CloudProvider
from src.models.pricing import PricingTier

checks: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        checks.append((name, True, ""))
    except AssertionError as e:
        checks.append((name, False, str(e)))
    except Exception as e:
        checks.append((name, False, f"{type(e).__name__}: {e}"))


async def main() -> None:
    aws = AWSPricingProvider()

    # ── Test 1: EC2 on-demand search ──────────────────────────────────────
    print("--- Test 1: EC2 Compute (us-east-1, on-demand, max 5) ---")
    results = await aws.search_prices(
        service_category=ServiceCategory.COMPUTE,
        region="us-east-1",
        pricing_tier=PricingTier.ON_DEMAND,
        max_results=5,
    )
    print(f"  Returned {len(results)} items")
    for r in results[:3]:
        monthly = f"~${r.monthly_cost_estimate:.2f}/mo" if r.monthly_cost_estimate else "per-request"
        print(f"  • {r.sku_name} | {r.pricing_tier.value} | ${r.unit_price:.6f}/{r.unit_of_measure} | {monthly}")

    def t1():
        assert len(results) > 0, "Expected EC2 results"
        assert all(r.provider == CloudProvider.AWS for r in results)
        assert all(r.pricing_tier == PricingTier.ON_DEMAND for r in results)

    check("EC2 on-demand search returns results", t1)

    # ── Test 2: SKU lookup for a known instance type ───────────────────────
    print("\n--- Test 2: get_sku_prices for m5.xlarge in us-east-1 ---")
    sku_results = await aws.get_sku_prices("m5.xlarge", "us-east-1")
    print(f"  Returned {len(sku_results)} tiers")
    for r in sku_results:
        monthly = f"~${r.monthly_cost_estimate:.2f}/mo" if r.monthly_cost_estimate else "per-request"
        print(f"  • {r.sku_name} | {r.pricing_tier.value} | ${r.unit_price:.6f}/{r.unit_of_measure} | {monthly}")

    def t2():
        assert len(sku_results) > 0, "Expected tiers for m5.xlarge"
        tiers = {r.pricing_tier for r in sku_results}
        assert PricingTier.ON_DEMAND in tiers, f"Expected on_demand tier, got {tiers}"

    check("m5.xlarge SKU lookup returns tiers", t2)

    # ── Test 3: Reserved pricing present ─────────────────────────────────
    def t3():
        tiers = {r.pricing_tier for r in sku_results}
        reserved = {PricingTier.RESERVED_1YR, PricingTier.RESERVED_3YR}
        assert tiers & reserved, f"Expected reserved tiers in {tiers}"

    check("m5.xlarge has reserved pricing tiers", t3)

    # ── Test 4: DATABASE category ─────────────────────────────────────────
    print("\n--- Test 4: DATABASE category (us-east-1, max 5) ---")
    db_results = await aws.search_prices(
        service_category=ServiceCategory.DATABASE,
        region="us-east-1",
        max_results=5,
    )
    print(f"  Returned {len(db_results)} items")
    for r in db_results[:3]:
        print(f"  • [{r.service_name}] {r.sku_name} | {r.pricing_tier.value} | ${r.unit_price:.6f}/{r.unit_of_measure}")

    def t4():
        assert len(db_results) > 0, "Expected DATABASE results"
        assert all(r.service_category == ServiceCategory.DATABASE for r in db_results)

    check("DATABASE category search returns results", t4)

    # ── Test 5: NormalizedPriceItem completeness ──────────────────────────
    print("\n--- Test 5: NormalizedPriceItem field completeness ---")
    sample = results[0]
    print(f"  provider={sample.provider.value}, sku_id={sample.sku_id}, region={sample.region}, currency={sample.currency}")

    def t5():
        assert sample.provider == CloudProvider.AWS
        assert sample.sku_id != ""
        assert sample.region == "us-east-1"
        assert sample.currency == "USD"
        assert sample.service_category is not None

    check("NormalizedPriceItem fields complete", t5)

    # ── Test 6: list_regions ──────────────────────────────────────────────
    print("\n--- Test 6: list_regions ---")
    regions = await aws.list_regions()
    print(f"  {len(regions)} regions. Sample: {sorted(regions)[:6]}")

    def t6():
        assert len(regions) > 10, f"Expected >10 regions, got {len(regions)}"
        assert "us-east-1" in regions

    check("list_regions returns valid AWS regions", t6)

    # ── Summary ───────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    print(f"\n{'='*60}")
    print(f"  AWS Adapter Live Test")
    print(f"  {passed}/{total} checks passed" + (", 0 failed" if passed == total else f", {total-passed} failed"))
    print(f"{'='*60}")
    for name, ok, err in checks:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}" + (f"\n      {err}" if err else ""))

    if passed < total:
        raise SystemExit(1)
    print("\nAWS adapter fully operational! ✅")


if __name__ == "__main__":
    asyncio.run(main())
