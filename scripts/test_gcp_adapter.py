#!/usr/bin/env python3
"""Live smoke test for the GCP Cloud Billing adapter.

Requires Application Default Credentials to be set up via:
    gcloud auth application-default login
"""
from __future__ import annotations

import asyncio
from src.providers.gcp_provider import GCPPricingProvider
from src.models.cloud_resource import ServiceCategory, CloudProvider

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
    gcp = GCPPricingProvider()

    # ── Test 1: Compute Engine search ─────────────────────────────────────
    print("--- Test 1: Compute Engine VMs in us-central1 ---")
    results = await gcp.search_prices(
        service_name="Compute Engine",
        region="us-central1",
        max_results=5,
    )
    print(f"  Returned {len(results)} items")
    for r in results[:3]:
        monthly = f"~${r.monthly_cost_estimate:.2f}/mo" if r.monthly_cost_estimate else "per-request"
        print(f"  • {r.sku_name} | {r.pricing_tier.value} | ${r.unit_price:.6f}/{r.unit_of_measure} | {monthly}")

    def t1():
        assert len(results) > 0, "Expected Compute Engine results"
        assert all(r.provider == CloudProvider.GCP for r in results)
        assert all(r.region == "us-central1" for r in results)

    check("Compute Engine search returns results", t1)

    # ── Test 2: SERVICE CATEGORY search ───────────────────────────────────
    print("\n--- Test 2: DATABASE category in us-central1 ---")
    db_results = await gcp.search_prices(
        service_category=ServiceCategory.DATABASE,
        region="us-central1",
        max_results=5,
    )
    print(f"  Returned {len(db_results)} items")
    for r in db_results[:3]:
        print(f"  • [{r.service_name}] {r.sku_name} | {r.pricing_tier.value} | ${r.unit_price:.6f}/{r.unit_of_measure}")

    def t2():
        assert len(db_results) > 0, "Expected DATABASE results"
        assert all(r.service_category == ServiceCategory.DATABASE for r in db_results)

    check("DATABASE category search returns results", t2)

    # ── Test 3: Pricing tiers present ─────────────────────────────────────
    print("\n--- Test 3: Pricing tiers ---")
    all_results = await gcp.search_prices(
        service_name="Compute Engine",
        region="us-central1",
        max_results=50,
    )
    tiers_found = {r.pricing_tier.value for r in all_results}
    print(f"  Tiers found: {sorted(tiers_found)}")

    def t3():
        assert "on_demand" in tiers_found, f"Expected on_demand tier, got {tiers_found}"

    check("on_demand tier present in Compute Engine results", t3)

    # ── Test 4: monthly_cost_estimate ─────────────────────────────────────
    print("\n--- Test 4: monthly_cost_estimate on hourly items ---")
    hourly = [r for r in all_results if r.is_hourly and r.unit_price > 0]
    if hourly:
        sample = hourly[0]
        print(f"  {sample.sku_name}: ${sample.unit_price:.6f}/hr → ~${sample.monthly_cost_estimate:.2f}/mo")

    def t4():
        assert len(hourly) > 0, "No hourly items found"
        assert all(r.monthly_cost_estimate is not None for r in hourly)
        assert all(r.monthly_cost_estimate > 0 for r in hourly)

    check("monthly_cost_estimate computed for hourly items", t4)

    # ── Test 5: NormalizedPriceItem fields ────────────────────────────────
    print("\n--- Test 5: NormalizedPriceItem field completeness ---")
    sample = results[0]
    print(f"  provider={sample.provider.value}, sku_id={sample.sku_id}, currency={sample.currency}")

    def t5():
        assert sample.provider == CloudProvider.GCP
        assert sample.sku_id != ""
        assert sample.currency == "USD"
        assert sample.service_category is not None
        assert sample.effective_date is not None

    check("NormalizedPriceItem fields complete", t5)

    # ── Test 6: list_regions ──────────────────────────────────────────────
    print("\n--- Test 6: list_regions ---")
    regions = await gcp.list_regions()
    print(f"  {len(regions)} regions. Sample: {sorted(regions)[:6]}")

    def t6():
        assert len(regions) > 10, f"Expected >10 regions, got {len(regions)}"
        assert "us-central1" in regions

    check("list_regions returns valid GCP regions", t6)

    # ── Summary ───────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    print(f"\n{'='*60}")
    print(f"  GCP Adapter Live Test")
    print(f"  {passed}/{total} checks passed{', 0 failed' if passed == total else f', {total-passed} failed'}")
    print(f"{'='*60}")
    for name, ok, err in checks:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}" + (f"\n      {err}" if err else ""))

    if passed < total:
        raise SystemExit(1)
    print("\nGCP adapter fully operational! ✅")


if __name__ == "__main__":
    asyncio.run(main())
