#!/usr/bin/env python3
"""Validate migrated engine modules against new model types.

Exercises:
  1. engines/__init__.py  — attribute extraction helpers
  2. engines/bin_packing  — FFD & BFD with NormalizedPriceItem + WorkloadRequirement
  3. engines/scoring      — multi-criteria scoring with NormalizedPriceItem + WorkloadRequirement
  4. engines/waf_compliance — WAF checks with WorkloadRequest.workloads filtering

Run:
    uv run python scripts/test_engines.py
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

# ── Models ──────────────────────────────────────────────────────

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.models.workload import (
    EnvironmentType,
    ResourceSpec,
    WorkloadRequest,
    WorkloadRequirement,
    WorkloadTier,
)

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {label}")
    else:
        failed += 1
        msg = f"  ❌ {label}"
        if detail:
            msg += f"  — {detail}"
        print(msg)


# ── Fixtures ────────────────────────────────────────────────────

def _make_node_sku(
    vcpus: int = 4,
    memory_gb: float = 16.0,
    gpu_count: int = 0,
    price: float = 0.20,
    generation: str = "current",
    name: str = "Standard_D4s_v5",
    provider: CloudProvider = CloudProvider.AZURE,
) -> NormalizedPriceItem:
    """Build a NormalizedPriceItem with compute attributes."""
    return NormalizedPriceItem(
        provider=provider,
        service_name="Virtual Machines",
        service_category=ServiceCategory.COMPUTE,
        sku_id=f"sku-{name.lower()}",
        sku_name=name,
        product_name=f"Virtual Machines {name}",
        region="eastus",
        retail_price=price,
        unit_price=price,
        unit_of_measure="1 Hour",
        pricing_tier=PricingTier.ON_DEMAND,
        effective_date=datetime.now(tz=timezone.utc),
        attributes={
            "vcpus": vcpus,
            "memory_gb": memory_gb,
            "gpu_count": gpu_count,
            "generation": generation,
        },
    )


def _make_aws_node_sku(
    vcpus: int = 8,
    memory_str: str = "32 GiB",
    price: float = 0.384,
    instance_type: str = "m5.2xlarge",
) -> NormalizedPriceItem:
    """Build a NormalizedPriceItem with AWS-style attributes."""
    return NormalizedPriceItem(
        provider=CloudProvider.AWS,
        service_name="Amazon Elastic Compute Cloud",
        service_category=ServiceCategory.COMPUTE,
        sku_id=f"sku-{instance_type}",
        sku_name=instance_type,
        product_name=f"AmazonEC2 {instance_type}",
        region="us-east-1",
        retail_price=price,
        unit_price=price,
        unit_of_measure="1 Hour",
        pricing_tier=PricingTier.ON_DEMAND,
        effective_date=datetime.now(tz=timezone.utc),
        attributes={
            "vcpu": str(vcpus),
            "memory": memory_str,
            "currentGeneration": "Yes",
            "instanceType": instance_type,
            "instanceFamily": "General purpose",
        },
    )


def _make_container_workload(
    name: str = "api-server",
    cpu_mc: int = 500,
    mem_mb: int = 512,
    replicas: int = 3,
) -> WorkloadRequirement:
    """Build a container WorkloadRequirement."""
    return WorkloadRequirement(
        name=name,
        description=f"Container workload: {name}",
        suggested_category=ServiceCategory.CONTAINER,
        resources=ResourceSpec(
            cpu_request_millicores=cpu_mc,
            cpu_limit_millicores=cpu_mc * 2,
            memory_request_mb=mem_mb,
            memory_limit_mb=mem_mb * 2,
            replicas=replicas,
        ),
    )


def _make_compute_workload(
    name: str = "api-gateway",
    vcpus: int = 4,
    memory_gb: float = 16.0,
    gpu_count: int = 0,
) -> WorkloadRequirement:
    """Build a compute WorkloadRequirement."""
    return WorkloadRequirement(
        name=name,
        description=f"VM workload: {name}",
        suggested_category=ServiceCategory.COMPUTE,
        resources=ResourceSpec(
            vcpus=vcpus,
            memory_gb=memory_gb,
            gpu_count=gpu_count,
        ),
    )


# ══════════════════════════════════════════════════════════════
# 1. Attribute extraction helpers
# ══════════════════════════════════════════════════════════════

def test_attribute_helpers() -> None:
    print("\n═══ 1. Attribute Extraction Helpers ═══")

    from src.engines import (
        extract_generation,
        extract_gpu_count,
        extract_memory_gb,
        extract_vcpus,
    )

    # Standardised keys
    sku = _make_node_sku(vcpus=8, memory_gb=32.0, gpu_count=2, generation="v5")
    check("extract_vcpus (standardised)", extract_vcpus(sku) == 8)
    check("extract_memory_gb (standardised)", extract_memory_gb(sku) == 32.0)
    check("extract_gpu_count (standardised)", extract_gpu_count(sku) == 2)
    check("extract_generation (standardised)", extract_generation(sku) == "v5")

    # AWS-style keys
    aws_sku = _make_aws_node_sku(vcpus=16, memory_str="64 GiB", price=0.768)
    check("extract_vcpus (AWS)", extract_vcpus(aws_sku) == 16)
    check("extract_memory_gb (AWS '64 GiB')", extract_memory_gb(aws_sku) == 64.0)
    check("extract_generation (AWS current)", extract_generation(aws_sku) == "current")

    # AWS memory with MiB
    mib_sku = _make_aws_node_sku()
    mib_sku.attributes["memory"] = "2048 MiB"
    check("extract_memory_gb (AWS '2048 MiB')", extract_memory_gb(mib_sku) == 2.0)

    # Missing attributes → defaults
    empty_sku = _make_node_sku()
    empty_sku.attributes = {}
    check("extract_vcpus (missing) → 0", extract_vcpus(empty_sku) == 0)
    check("extract_memory_gb (missing) → 0.0", extract_memory_gb(empty_sku) == 0.0)
    check("extract_gpu_count (missing) → 0", extract_gpu_count(empty_sku) == 0)
    check("extract_generation (missing) → ''", extract_generation(empty_sku) == "")

    # AWS currentGeneration=No
    prev_sku = _make_aws_node_sku()
    prev_sku.attributes["currentGeneration"] = "No"
    check("extract_generation (AWS previous)", extract_generation(prev_sku) == "previous")


# ══════════════════════════════════════════════════════════════
# 2. Bin-packing engine
# ══════════════════════════════════════════════════════════════

def test_bin_packing() -> None:
    print("\n═══ 2. Bin-Packing Engine ═══")

    from src.engines.bin_packing import PackingAlgorithm, pack_workloads

    node_sku = _make_node_sku(vcpus=4, memory_gb=16.0, price=0.20)

    workloads = [
        _make_container_workload("api", cpu_mc=500, mem_mb=512, replicas=3),
        _make_container_workload("worker", cpu_mc=1000, mem_mb=1024, replicas=2),
    ]

    # --- FFD ---
    result = pack_workloads(workloads, node_sku, PackingAlgorithm.FIRST_FIT_DECREASING)
    check("FFD returns BinPackingResult", result is not None)
    check("FFD total_nodes > 0", result.total_nodes > 0, f"got {result.total_nodes}")
    check("FFD provider = AZURE", result.provider == CloudProvider.AZURE)
    check("FFD algorithm_used", result.algorithm_used == "first-fit-decreasing")
    check(
        "FFD packing_efficiency > 0",
        result.packing_efficiency_pct > 0,
        f"got {result.packing_efficiency_pct}%",
    )
    check(
        "FFD total_monthly_cost > 0",
        result.total_monthly_cost_usd > 0,
        f"got ${result.total_monthly_cost_usd}",
    )

    # Verify cost = nodes × monthly_cost_estimate
    expected_cost = round(result.total_nodes * 0.20 * 730, 2)
    check(
        "FFD cost = nodes × unit_price × 730",
        result.total_monthly_cost_usd == expected_cost,
        f"got ${result.total_monthly_cost_usd}, expected ${expected_cost}",
    )

    # Verify PackedNode.node_sku is a NormalizedPriceItem
    from src.models.pricing import NormalizedPriceItem as NPI

    check(
        "PackedNode.node_sku is NormalizedPriceItem",
        isinstance(result.nodes[0].node_sku, NPI),
    )

    # --- BFD ---
    bfd_result = pack_workloads(
        workloads, node_sku, PackingAlgorithm.BEST_FIT_DECREASING, pool_name="bfd-pool"
    )
    check("BFD returns result", bfd_result is not None)
    check("BFD total_nodes > 0", bfd_result.total_nodes > 0)
    check("BFD pool_name", bfd_result.node_pool_name == "bfd-pool")

    # --- Empty workloads ---
    empty_result = pack_workloads([], node_sku)
    check("Empty workloads → 0 nodes", empty_result.total_nodes == 0)

    # --- Workload missing K8s fields (should be skipped) ---
    incomplete_wl = WorkloadRequirement(
        name="incomplete",
        suggested_category=ServiceCategory.CONTAINER,
        resources=ResourceSpec(vcpus=2, memory_gb=4.0),  # no cpu_request_millicores
    )
    skip_result = pack_workloads([incomplete_wl], node_sku)
    check("Incomplete workload → 0 nodes (skipped)", skip_result.total_nodes == 0)

    # --- AWS-style SKU ---
    aws_sku = _make_aws_node_sku(vcpus=8, memory_str="32 GiB", price=0.384)
    aws_result = pack_workloads(workloads, aws_sku, pool_name="aws-pool")
    check("AWS SKU packing works", aws_result.total_nodes > 0)
    check("AWS SKU provider = AWS", aws_result.provider == CloudProvider.AWS)


# ══════════════════════════════════════════════════════════════
# 3. Scoring engine
# ══════════════════════════════════════════════════════════════

def test_scoring() -> None:
    print("\n═══ 3. Scoring Engine ═══")

    from src.engines.scoring import ScoringWeights, score_skus

    workload = _make_compute_workload("web-server", vcpus=4, memory_gb=16.0)

    candidates = [
        _make_node_sku(vcpus=4, memory_gb=16.0, price=0.20, name="D4s_v5"),
        _make_node_sku(vcpus=8, memory_gb=32.0, price=0.40, name="D8s_v5"),
        _make_node_sku(vcpus=16, memory_gb=64.0, price=0.80, name="D16s_v5"),
        _make_node_sku(vcpus=2, memory_gb=8.0, price=0.10, name="D2s_v5"),  # too small
    ]

    scored = score_skus(workload, candidates)
    check("score_skus returns list", isinstance(scored, list))
    check(
        "Filtered out undersized SKU (D2s_v5)",
        len(scored) == 3,
        f"got {len(scored)}",
    )
    check(
        "Results sorted by total_score descending",
        all(scored[i].total_score >= scored[i + 1].total_score for i in range(len(scored) - 1)),
    )
    check(
        "Best fit is D4s_v5 (perfect match)",
        scored[0].sku.sku_name == "D4s_v5",
        f"got {scored[0].sku.sku_name}",
    )
    check(
        "ScoredSKU.sku is NormalizedPriceItem",
        isinstance(scored[0].sku, NormalizedPriceItem),
    )
    check("cost_score in [0, 1]", 0 <= scored[0].cost_score <= 1)
    check("cpu_fit_score in [0, 1]", 0 <= scored[0].cpu_fit_score <= 1)
    check("memory_fit_score in [0, 1]", 0 <= scored[0].memory_fit_score <= 1)
    check("generation_score in [0, 1]", 0 <= scored[0].generation_score <= 1)

    # --- No eligible SKUs ---
    huge = _make_compute_workload("gpu-beast", vcpus=128, memory_gb=512.0, gpu_count=8)
    empty = score_skus(huge, candidates)
    check("No eligible SKUs → empty list", len(empty) == 0)

    # --- Custom weights ---
    cost_only = ScoringWeights(cost=1.0, cpu_fit=0.0, memory_fit=0.0, generation=0.0)
    cost_scored = score_skus(workload, candidates, weights=cost_only)
    check(
        "Cost-only scoring: cheapest first (D4s_v5)",
        cost_scored[0].sku.sku_name == "D4s_v5",
        f"got {cost_scored[0].sku.sku_name}",
    )

    # --- GPU workload filtering ---
    gpu_workload = _make_compute_workload("ml-train", vcpus=8, memory_gb=32.0, gpu_count=1)
    gpu_sku = _make_node_sku(vcpus=8, memory_gb=32.0, price=3.0, gpu_count=1, name="NC8_v3")
    gpu_scored = score_skus(gpu_workload, candidates + [gpu_sku])
    check(
        "GPU workload: only GPU SKU eligible",
        len(gpu_scored) == 1,
        f"got {len(gpu_scored)}",
    )
    check(
        "GPU SKU selected is NC8_v3",
        gpu_scored[0].sku.sku_name == "NC8_v3" if gpu_scored else False,
    )

    # --- AWS-style scoring ---
    aws_candidates = [
        _make_aws_node_sku(vcpus=4, memory_str="16 GiB", price=0.192, instance_type="m5.xlarge"),
        _make_aws_node_sku(vcpus=8, memory_str="32 GiB", price=0.384, instance_type="m5.2xlarge"),
    ]
    aws_scored = score_skus(workload, aws_candidates)
    check("AWS SKU scoring works", len(aws_scored) >= 1)


# ══════════════════════════════════════════════════════════════
# 4. WAF Compliance engine
# ══════════════════════════════════════════════════════════════

def test_waf_compliance() -> None:
    print("\n═══ 4. WAF Compliance Engine ═══")

    from src.engines.waf_compliance import evaluate_compliance
    from src.models.recommendation import BinPackingResult

    request = WorkloadRequest(
        project_name="test-project",
        environment=EnvironmentType.PRODUCTION,
        tier=WorkloadTier.BUSINESS_CRITICAL,
        target_providers=[CloudProvider.AWS, CloudProvider.AZURE],
        workloads=[
            # Container workload
            WorkloadRequirement(
                name="api-server",
                suggested_category=ServiceCategory.CONTAINER,
                resources=ResourceSpec(
                    cpu_request_millicores=500,
                    memory_request_mb=512,
                    replicas=3,
                ),
            ),
            # Compute workload
            WorkloadRequirement(
                name="batch-processor",
                suggested_category=ServiceCategory.COMPUTE,
                resources=ResourceSpec(vcpus=8, memory_gb=32.0),
            ),
            # Storage workload
            WorkloadRequirement(
                name="data-lake",
                suggested_category=ServiceCategory.STORAGE,
                resources=ResourceSpec(storage_gb=1000.0, storage_type="ssd"),
            ),
        ],
    )

    bin_results = {
        "aws": BinPackingResult(
            provider=CloudProvider.AWS,
            total_nodes=3,
            packing_efficiency_pct=75.0,
        ),
        "azure": BinPackingResult(
            provider=CloudProvider.AZURE,
            total_nodes=2,
            packing_efficiency_pct=82.0,
        ),
    }

    report = evaluate_compliance(request, bin_results)

    check("Report is ComplianceReport", report is not None)
    check("total_checks > 0", report.total_checks > 0, f"got {report.total_checks}")
    check("passed_checks >= 0", report.passed_checks >= 0)
    check(
        "compliance_score in [0, 100]",
        0 <= report.compliance_score_pct <= 100,
        f"got {report.compliance_score_pct}%",
    )

    # Check pillar coverage
    pillars = {c.pillar for c in report.checks}
    check("Reliability pillar present", "Reliability" in pillars, f"pillars: {pillars}")
    check("Security pillar present", "Security" in pillars)
    check("Cost Optimization pillar present", "Cost Optimization" in pillars)
    check("Performance Efficiency pillar present", "Performance Efficiency" in pillars)
    check("Operational Excellence pillar present", "Operational Excellence" in pillars)
    check("Sustainability pillar present", "Sustainability" in pillars)

    # Specific check: container replicas (3 replicas in prod → pass)
    replica_checks = [
        c for c in report.checks if "Replica Count" in c.check_name
    ]
    check(
        "Container replica check exists",
        len(replica_checks) == 1,
        f"found {len(replica_checks)}",
    )
    if replica_checks:
        check("Container replica check passed", replica_checks[0].passed)

    # Specific check: storage encryption
    enc_checks = [c for c in report.checks if "Encryption" in c.check_name]
    check(
        "Storage encryption check exists",
        len(enc_checks) == 1,
        f"found {len(enc_checks)}",
    )
    if enc_checks:
        check("Storage encryption check passed", enc_checks[0].passed)

    # Specific check: performance (8 vCPUs, no GPU → should pass)
    perf_checks = [c for c in report.checks if "Right-sizing" in c.check_name]
    check(
        "Performance check exists",
        len(perf_checks) == 1,
        f"found {len(perf_checks)}",
    )
    if perf_checks:
        check("Performance check passed (8 vCPUs OK)", perf_checks[0].passed)

    # --- No workloads → still produces report ---
    empty_req = WorkloadRequest(project_name="empty", workloads=[])
    empty_report = evaluate_compliance(empty_req)
    check("Empty workloads → valid report", empty_report.total_checks > 0)

    # --- Over-provisioned VM ---
    overprov_req = WorkloadRequest(
        project_name="big-vm",
        workloads=[
            WorkloadRequirement(
                name="mega-compute",
                suggested_category=ServiceCategory.COMPUTE,
                resources=ResourceSpec(vcpus=128, memory_gb=512.0, gpu_count=0),
            ),
        ],
    )
    overprov_report = evaluate_compliance(overprov_req)
    overprov_checks = [c for c in overprov_report.checks if "Right-sizing" in c.check_name]
    check(
        "Over-provisioned VM detected",
        len(overprov_checks) == 1 and not overprov_checks[0].passed,
    )


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🔧 Engine Migration Validation")
    print("=" * 50)

    tests = [
        ("Attribute Helpers", test_attribute_helpers),
        ("Bin-Packing Engine", test_bin_packing),
        ("Scoring Engine", test_scoring),
        ("WAF Compliance Engine", test_waf_compliance),
    ]

    for name, fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"\n💥 {name} CRASHED:")
            traceback.print_exc()

    print("\n" + "=" * 50)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if failed:
        print("❌ SOME CHECKS FAILED")
        sys.exit(1)
    else:
        print("✅ ALL CHECKS PASSED")
        sys.exit(0)
