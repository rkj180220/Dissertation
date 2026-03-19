"""Smoke test for PricingService + SQLite cache.

Uses the Azure adapter (public API, no auth) to verify the full
cache lifecycle: miss → live fetch → store → hit → stats → evict → clear.

Run:
    uv run python scripts/test_pricing_service.py
"""

import asyncio
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Setup — use a temp DB so we don't pollute the real cache
# ---------------------------------------------------------------------------

TEST_DB_PATH = Path("data/test_cache.db")


async def main() -> None:
    from src.config.settings import AppSettings
    from src.models.cloud_resource import CloudProvider, ServiceCategory
    from src.models.pricing import PricingTier
    from src.providers import AzurePricingProvider
    from src.services import PricingService
    from src.services.pricing_cache import make_cache_key

    # Build settings with test DB path
    settings = AppSettings(sku_cache_db_path=str(TEST_DB_PATH), sku_cache_ttl_hours=24)

    # Create service and register Azure (only one needing no creds)
    service = PricingService(settings=settings)
    service.register_provider(AzurePricingProvider())
    await service.initialize()

    passed = 0
    total = 0

    def check(label: str, condition: bool) -> None:
        nonlocal passed, total
        total += 1
        status = "✅" if condition else "❌"
        print(f"  {status} {label}")
        if condition:
            passed += 1

    try:
        # ==================================================================
        print("\n1. Service initialisation")
        # ==================================================================
        check("Service initialised", service._initialized)
        check(
            "Azure registered",
            CloudProvider.AZURE in service.registered_providers,
        )

        # ==================================================================
        print("\n2. Cache MISS → live fetch → store (VM on-demand, eastus)")
        # ==================================================================
        t0 = time.perf_counter()
        items = await service.search_prices(
            CloudProvider.AZURE,
            service_name="Virtual Machines",
            region="eastus",
            pricing_tier=PricingTier.ON_DEMAND,
            max_results=20,
        )
        live_elapsed = time.perf_counter() - t0

        check(f"Got {len(items)} items from live API", len(items) > 0)
        check("All items are AZURE", all(i.provider == CloudProvider.AZURE for i in items))
        check("All items are ON_DEMAND", all(i.pricing_tier == PricingTier.ON_DEMAND for i in items))
        check("All items in eastus", all(i.region == "eastus" for i in items))
        print(f"    ⏱  Live fetch took {live_elapsed:.2f}s")

        # ==================================================================
        print("\n3. Cache HIT → served from SQLite")
        # ==================================================================
        t0 = time.perf_counter()
        cached_items = await service.search_prices(
            CloudProvider.AZURE,
            service_name="Virtual Machines",
            region="eastus",
            pricing_tier=PricingTier.ON_DEMAND,
            max_results=20,
        )
        cache_elapsed = time.perf_counter() - t0

        check(f"Got {len(cached_items)} items from cache", len(cached_items) > 0)
        check(
            "Same count as live fetch",
            len(cached_items) == len(items),
        )
        check(
            f"Cache is faster ({cache_elapsed:.4f}s < {live_elapsed:.2f}s)",
            cache_elapsed < live_elapsed,
        )
        print(f"    ⏱  Cache fetch took {cache_elapsed:.4f}s")
        if live_elapsed > 0:
            speedup = live_elapsed / max(cache_elapsed, 0.0001)
            print(f"    🚀 Speedup: {speedup:.0f}x")

        # ==================================================================
        print("\n4. SKU-specific lookup (D4s_v5)")
        # ==================================================================
        sku_items = await service.get_sku_prices(
            CloudProvider.AZURE,
            sku_name="Standard_D4s_v5",
            region="eastus",
        )
        check(f"Got {len(sku_items)} tiers for D4s_v5", len(sku_items) > 0)
        tiers = {i.pricing_tier for i in sku_items}
        check(
            f"Multiple tiers: {sorted(t.value for t in tiers)}",
            len(tiers) > 1,
        )

        # ==================================================================
        print("\n5. Cache stats")
        # ==================================================================
        stats = await service.cache_stats()
        check(f"Total items: {stats['total_items']}", stats["total_items"] > 0)
        check(
            f"Azure items: {stats['items_by_provider'].get('azure', 0)}",
            stats["items_by_provider"].get("azure", 0) > 0,
        )
        check(
            f"Fetch entries: {stats['fetch_entries']}",
            stats["fetch_entries"] > 0,
        )
        check(
            f"DB size: {stats['db_size_bytes']} bytes",
            stats["db_size_bytes"] > 0,
        )
        print(f"    📊 Oldest fetch: {stats['oldest_fetch']}")
        print(f"    📊 Newest fetch: {stats['newest_fetch']}")

        # ==================================================================
        print("\n6. Force refresh (bypasses cache)")
        # ==================================================================
        refreshed = await service.refresh(
            CloudProvider.AZURE,
            service_name="Virtual Machines",
            region="eastus",
            max_results=10,
        )
        check(f"Force-refreshed {refreshed} items", refreshed > 0)

        # ==================================================================
        print("\n7. Cache key determinism")
        # ==================================================================
        key1 = make_cache_key("azure", "Virtual Machines", None, "eastus")
        key2 = make_cache_key("azure", "Virtual Machines", None, "eastus")
        key3 = make_cache_key("azure", "Virtual Machines", None, "westus")
        check("Same params → same key", key1 == key2)
        check("Different region → different key", key1 != key3)

        # ==================================================================
        print("\n8. Cross-provider compare (Azure only, others not registered)")
        # ==================================================================
        comparison = await service.compare_across_providers(
            service_category=ServiceCategory.COMPUTE,
            regions={
                CloudProvider.AZURE: "eastus",
                CloudProvider.AWS: "us-east-1",  # not registered
                CloudProvider.GCP: "us-central1",  # not registered
            },
            max_results_per_provider=10,
        )
        check(
            f"Azure returned {len(comparison.get(CloudProvider.AZURE, []))} items",
            len(comparison.get(CloudProvider.AZURE, [])) > 0,
        )
        check(
            "AWS returned empty (not registered)",
            comparison.get(CloudProvider.AWS) == [],
        )
        check(
            "GCP returned empty (not registered)",
            comparison.get(CloudProvider.GCP) == [],
        )

        # ==================================================================
        print("\n9. Evict stale (with 0h TTL → evicts everything)")
        # ==================================================================
        evicted = await service.evict_stale(ttl_hours=0)
        check(f"Evicted {evicted} stale items", evicted > 0)
        stats_after = await service.cache_stats()
        check(
            f"Items after eviction: {stats_after['total_items']}",
            stats_after["total_items"] == 0,
        )

        # ==================================================================
        print("\n10. Clear cache")
        # ==================================================================
        # Re-fetch to have something to clear
        await service.search_prices(
            CloudProvider.AZURE,
            service_name="Virtual Machines",
            region="eastus",
            max_results=5,
        )
        deleted = await service.clear_cache(provider=CloudProvider.AZURE)
        check(f"Cleared {deleted} Azure items", deleted > 0)

        stats_final = await service.cache_stats()
        check(
            f"Items after clear: {stats_final['total_items']}",
            stats_final["total_items"] == 0,
        )

    finally:
        await service.close()

        # Clean up test DB
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
            print(f"\n    🧹 Cleaned up {TEST_DB_PATH}")

    # Summary
    print(f"\n{'=' * 50}")
    if passed == total:
        print(f"🎉 All {total} checks passed!")
    else:
        print(f"⚠️  {passed}/{total} checks passed ({total - passed} failed)")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    asyncio.run(main())
