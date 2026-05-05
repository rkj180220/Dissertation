"""Well-Architected Framework (WAF) compliance checker.

Evaluates proposed infrastructure designs against the six pillars
of the AWS Well-Architected Framework, ensuring that generated
recommendations meet enterprise-grade architectural standards.

References:
    [5] Amazon Web Services, "The Well-Architected Framework: The Six Pillars,"
        AWS Whitepapers & Guides, 2024.
"""

from __future__ import annotations

import structlog

from src.models.recommendation import (
    BinPackingResult,
    ComplianceCheckResult,
    ComplianceReport,
)
from src.models.cloud_resource import ServiceCategory
from src.models.workload import (
    EnvironmentType,
    WorkloadRequest,
    WorkloadTier,
)

logger = structlog.get_logger(__name__)


# ─── WAF Pillar Checks ─────────────────────────────────────


def _check_reliability_ha(
    request: WorkloadRequest,
    bin_results: dict[str, BinPackingResult],
) -> list[ComplianceCheckResult]:
    """Reliability: High Availability checks.

    - Production workloads must have ≥ 2 nodes per pool.
    - Mission-critical workloads must have ≥ 3 nodes.
    """
    checks: list[ComplianceCheckResult] = []

    min_nodes = 2 if request.tier == WorkloadTier.BUSINESS_CRITICAL else 3
    if request.tier == WorkloadTier.NON_CRITICAL:
        min_nodes = 1

    for provider, result in bin_results.items():
        passed = result.total_nodes >= min_nodes
        checks.append(
            ComplianceCheckResult(
                pillar="Reliability",
                check_name=f"Minimum Node Count ({provider})",
                passed=passed,
                severity="high" if not passed else "low",
                finding=(
                    f"{result.total_nodes} node(s) for {request.tier.value} workload"
                ),
                recommendation=(
                    f"Increase to ≥ {min_nodes} nodes for {request.tier.value} tier"
                    if not passed
                    else "Node count meets HA requirements"
                ),
            )
        )

    return checks


def _check_reliability_replicas(
    request: WorkloadRequest,
) -> list[ComplianceCheckResult]:
    """Reliability: Container replica counts."""
    checks: list[ComplianceCheckResult] = []

    container_workloads = [
        w for w in request.workloads
        if w.suggested_category == ServiceCategory.CONTAINER
    ]

    for wl in container_workloads:
        replicas = wl.resources.replicas
        min_replicas = 2 if request.environment == EnvironmentType.PRODUCTION else 1
        passed = replicas >= min_replicas
        checks.append(
            ComplianceCheckResult(
                pillar="Reliability",
                check_name=f"Replica Count — {wl.name}",
                passed=passed,
                severity="high" if not passed else "low",
                finding=f"{replicas} replica(s) in {request.environment.value}",
                recommendation=(
                    f"Set replicas ≥ {min_replicas} for {request.environment.value}"
                    if not passed
                    else "Replica count is adequate"
                ),
            )
        )

    return checks


def _check_security_encryption(
    request: WorkloadRequest,
) -> list[ComplianceCheckResult]:
    """Security: Encryption-at-rest for storage."""
    checks: list[ComplianceCheckResult] = []

    storage_workloads = [
        w for w in request.workloads
        if w.suggested_category == ServiceCategory.STORAGE
    ]

    for sw in storage_workloads:
        # Default assumption: cloud-managed volumes have encryption at rest.
        checks.append(
            ComplianceCheckResult(
                pillar="Security",
                check_name=f"Encryption at Rest — {sw.name}",
                passed=True,
                severity="critical",
                finding="Managed block storage encrypts at rest by default",
                recommendation="Verify customer-managed keys (CMK) if required",
            )
        )

    if not storage_workloads:
        checks.append(
            ComplianceCheckResult(
                pillar="Security",
                check_name="Encryption at Rest — No Storage Defined",
                passed=True,
                severity="low",
                finding="No block storage defined; check not applicable",
                recommendation="N/A",
            )
        )

    return checks


def _check_cost_optimization(
    request: WorkloadRequest,
    bin_results: dict[str, BinPackingResult],
) -> list[ComplianceCheckResult]:
    """Cost Optimization: Packing efficiency threshold."""
    checks: list[ComplianceCheckResult] = []
    efficiency_threshold = 60.0  # Minimum acceptable packing efficiency

    for provider, result in bin_results.items():
        passed = result.packing_efficiency_pct >= efficiency_threshold
        checks.append(
            ComplianceCheckResult(
                pillar="Cost Optimization",
                check_name=f"Packing Efficiency ({provider})",
                passed=passed,
                severity="medium" if not passed else "low",
                finding=f"{result.packing_efficiency_pct:.1f}% efficiency",
                recommendation=(
                    f"Consider smaller node SKUs to improve utilisation above "
                    f"{efficiency_threshold}%"
                    if not passed
                    else "Packing efficiency is within acceptable range"
                ),
            )
        )

    return checks


def _check_performance_efficiency(
    request: WorkloadRequest,
) -> list[ComplianceCheckResult]:
    """Performance Efficiency: Over-provisioning detection for VMs."""
    checks: list[ComplianceCheckResult] = []

    compute_workloads = [
        w for w in request.workloads
        if w.suggested_category == ServiceCategory.COMPUTE
    ]

    for vm in compute_workloads:
        vcpus = vm.resources.vcpus or 0
        gpu_required = (vm.resources.gpu_count or 0) > 0
        # Flag potentially over-specified VMs (> 64 vCPUs without GPU)
        over_provisioned = vcpus > 64 and not gpu_required
        checks.append(
            ComplianceCheckResult(
                pillar="Performance Efficiency",
                check_name=f"Right-sizing Check — {vm.name}",
                passed=not over_provisioned,
                severity="medium" if over_provisioned else "low",
                finding=f"{vcpus} vCPUs requested, GPU={gpu_required}",
                recommendation=(
                    "Verify that > 64 vCPUs are justified; consider horizontal scaling"
                    if over_provisioned
                    else "Resource specification appears reasonable"
                ),
            )
        )

    return checks


def _check_operational_excellence(
    request: WorkloadRequest,
) -> list[ComplianceCheckResult]:
    """Operational Excellence: Multi-provider evaluation."""
    checks: list[ComplianceCheckResult] = []

    provider_count = len(request.target_providers)
    multi_provider = provider_count >= 2

    # Fix 7f — deliberate single-provider choice (e.g. government data-residency
    # or explicit client requirement) is a valid architectural decision.
    # Detect this by checking the raw_user_input for "single_" provider strategy
    # markers emitted by build_enriched_input_from_structured(), or by checking
    # for explicit provider keywords like "aws only", "azure only".
    raw = (request.raw_user_input or "").lower()
    deliberate_single = (
        provider_count == 1
        and any(
            marker in raw
            for marker in (
                "provider strategy: single_",
                "aws only",
                "azure only",
                "gcp only",
                "only aws",
                "only azure",
                "only gcp",
                "single provider",
                "no comparison",
            )
        )
    )

    if deliberate_single:
        # Client has a hard single-provider requirement (data-residency, contract, etc.)
        # Mark as PASS / NOT_APPLICABLE — not a compliance gap.
        checks.append(
            ComplianceCheckResult(
                pillar="Operational Excellence",
                check_name="Multi-Cloud Evaluation",
                passed=True,
                severity="low",
                finding=(
                    f"Single-provider strategy ({request.target_providers[0].value}) "
                    "per explicit client requirement"
                ),
                recommendation=(
                    "Single-provider architecture is an intentional client choice. "
                    "Ensure vendor lock-in risks are documented in the RFP assumptions."
                ),
            )
        )
    else:
        checks.append(
            ComplianceCheckResult(
                pillar="Operational Excellence",
                check_name="Multi-Cloud Evaluation",
                passed=multi_provider,
                severity="medium" if not multi_provider else "low",
                finding=f"Evaluating {provider_count} provider(s)",
                recommendation=(
                    "Evaluate ≥ 2 providers to avoid vendor lock-in"
                    if not multi_provider
                    else "Multi-provider comparison is enabled"
                ),
            )
        )

    return checks


def _check_sustainability(
    request: WorkloadRequest,
    bin_results: dict[str, BinPackingResult],
) -> list[ComplianceCheckResult]:
    """Sustainability: Resource waste minimisation."""
    checks: list[ComplianceCheckResult] = []
    waste_threshold = 40.0  # Max acceptable waste percentage

    for provider, result in bin_results.items():
        waste_pct = 100.0 - result.packing_efficiency_pct
        passed = waste_pct <= waste_threshold
        checks.append(
            ComplianceCheckResult(
                pillar="Sustainability",
                check_name=f"Resource Waste ({provider})",
                passed=passed,
                severity="medium" if not passed else "low",
                finding=f"{waste_pct:.1f}% resource waste",
                recommendation=(
                    "Reduce waste by choosing more appropriately sized nodes"
                    if not passed
                    else "Resource waste is within acceptable limits"
                ),
            )
        )

    return checks


# ─── Public API ─────────────────────────────────────────────


def evaluate_compliance(
    request: WorkloadRequest,
    bin_packing_results: dict[str, BinPackingResult] | None = None,
) -> ComplianceReport:
    """Run all WAF compliance checks against the proposed design.

    Args:
        request: The original workload request.
        bin_packing_results: Bin-packing results per provider (optional).

    Returns:
        ComplianceReport with all check results and an overall score.
    """
    if bin_packing_results is None:
        bin_packing_results = {}

    all_checks: list[ComplianceCheckResult] = []

    # --- Run all pillar checks ---
    all_checks.extend(_check_reliability_ha(request, bin_packing_results))
    all_checks.extend(_check_reliability_replicas(request))
    all_checks.extend(_check_security_encryption(request))
    all_checks.extend(_check_cost_optimization(request, bin_packing_results))
    all_checks.extend(_check_performance_efficiency(request))
    all_checks.extend(_check_operational_excellence(request))
    all_checks.extend(_check_sustainability(request, bin_packing_results))

    total = len(all_checks)
    passed = sum(1 for c in all_checks if c.passed)
    score = round((passed / total * 100) if total > 0 else 0.0, 2)

    report = ComplianceReport(
        framework="Well-Architected Framework",
        checks=all_checks,
        total_checks=total,
        passed_checks=passed,
        compliance_score_pct=score,
    )

    logger.info(
        "waf_compliance_evaluated",
        total_checks=total,
        passed=passed,
        score_pct=score,
    )

    return report
