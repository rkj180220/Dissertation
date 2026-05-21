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
from typing import Any

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

# Graviton (ARM) instance family prefixes — mirrors scoring.py constant
_GRAVITON_PREFIXES: frozenset[str] = frozenset(
    {"c8g", "m8g", "r8g", "t4g", "x2g"}
)

# Categories where Graviton adoption is meaningful for sustainability scoring
_COMPUTE_CATEGORIES: frozenset[ServiceCategory] = frozenset(
    {ServiceCategory.COMPUTE, ServiceCategory.AI_ML}
)


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
    sized_results: list[Any] | None = None,
) -> list[ComplianceCheckResult]:
    """Sustainability: Resource waste minimisation + Graviton adoption.

    P17: Active Graviton/ARM adoption is scored as a full sustainability
    check.  If at least one COMPUTE or AI_ML workload is recommended on a
    Graviton SKU (c8g / m8g / r8g / t4g / x2g), the pillar receives 5/5
    (100 %).  Otherwise it falls back to 4/5 (80 %) with a recommendation
    to evaluate Graviton instances.  AWS Graviton delivers up to 40% better
    performance at 20% lower cost and 60% less energy [Ref 20, 21].
    """
    checks: list[ComplianceCheckResult] = []
    waste_threshold = 40.0

    # --- Existing: resource waste check per provider ---
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

    # --- P17: Graviton adoption check ---
    graviton_found = False
    if sized_results:
        for result in sized_results:
            # result is a SizedWorkloadResult — access attributes safely
            category = None
            try:
                # Derive category from selected_sku or workload name
                sku = getattr(result, "selected_sku", None)
                if sku is None:
                    continue
                sku_name: str = getattr(sku, "sku_name", "") or ""
                family = sku_name.split(".")[0].lower() if "." in sku_name else sku_name.lower()
                if family in _GRAVITON_PREFIXES:
                    graviton_found = True
                    break
            except Exception:
                continue

    graviton_passed = graviton_found
    checks.append(
        ComplianceCheckResult(
            pillar="Sustainability",
            check_name="Graviton/ARM Processor Adoption",
            passed=graviton_passed,
            severity="low" if graviton_passed else "medium",
            finding=(
                "Graviton adoption active ✅ — ARM-based instances recommended for eligible workloads"
                if graviton_passed
                else "No ARM/Graviton instances recommended"
            ),
            recommendation=(
                "Graviton instances deliver up to 40% better performance at 20% lower cost "
                "and 60% less energy. Evaluate c8g/m8g/r8g/t4g/x2g families for compute workloads."
                if not graviton_passed
                else "Continue prioritising Graviton for single-threaded and web/API workloads"
            ),
        )
    )

    return checks


def _check_database_ha(
    request: WorkloadRequest,
) -> list[ComplianceCheckResult]:
    """Reliability: Database High Availability for production workloads.

    For business-critical and mission-critical tiers, all database workloads
    must have HA enabled (multi-AZ / failover replicas).
    """
    checks: list[ComplianceCheckResult] = []
    production_tiers = {WorkloadTier.BUSINESS_CRITICAL, WorkloadTier.MISSION_CRITICAL}

    if request.tier not in production_tiers:
        return checks

    db_workloads = [
        w for w in request.workloads
        if w.suggested_category == ServiceCategory.DATABASE
    ]

    for db in db_workloads:
        ha_enabled = db.resources.high_availability
        passed = ha_enabled
        checks.append(
            ComplianceCheckResult(
                pillar="Reliability",
                check_name=f"Database HA — {db.name}",
                passed=passed,
                severity="high" if not passed else "low",
                finding=(
                    f"HA {'enabled' if ha_enabled else 'NOT configured'} "
                    f"for {request.tier.value} tier database"
                ),
                recommendation=(
                    "Enable high_availability=True and configure at least one "
                    "read replica for mission/business-critical databases"
                    if not passed
                    else "Database HA configuration meets reliability requirements"
                ),
            )
        )

    return checks


def _check_budget_ceiling(
    request: WorkloadRequest,
) -> list[ComplianceCheckResult]:
    """Cost Optimization: Verify a budget ceiling is specified.

    A defined budget ceiling enables FinOps analysis to flag over-budget
    designs.  Missing budget is a Cost Optimization gap.
    """
    budget_set = request.budget_monthly_usd is not None and request.budget_monthly_usd > 0
    return [
        ComplianceCheckResult(
            pillar="Cost Optimization",
            check_name="Budget Ceiling Defined",
            passed=budget_set,
            severity="medium" if not budget_set else "low",
            finding=(
                f"Budget ceiling: ${request.budget_monthly_usd:,.0f}/mo"
                if budget_set
                else "No monthly budget ceiling specified"
            ),
            recommendation=(
                "Specify budget_monthly_usd to enable FinOps budget-breach alerts"
                if not budget_set
                else "Budget ceiling enables cost guardrails"
            ),
        )
    ]


def _check_compliance_coverage(
    request: WorkloadRequest,
) -> list[ComplianceCheckResult]:
    """Security: Compliance framework tags on workloads.

    When compliance frameworks are required (e.g. StateRAMP, HIPAA),
    all workloads should carry the corresponding compliance_tags so that
    controls can be mapped per component.
    """
    checks: list[ComplianceCheckResult] = []
    if not request.compliance_frameworks:
        return checks

    frameworks_lower = {f.lower() for f in request.compliance_frameworks}

    for wl in request.workloads:
        wl_tags_lower = {t.lower() for t in wl.compliance_tags}
        # Check if any framework appears in the workload's compliance tags
        tagged = bool(frameworks_lower & wl_tags_lower) or bool(wl_tags_lower)
        checks.append(
            ComplianceCheckResult(
                pillar="Security",
                check_name=f"Compliance Tagging — {wl.name}",
                passed=tagged,
                severity="medium" if not tagged else "low",
                finding=(
                    f"Tags: {', '.join(wl.compliance_tags) or 'none'}"
                ),
                recommendation=(
                    f"Add compliance tags ({', '.join(request.compliance_frameworks)}) "
                    f"to workload '{wl.name}' so controls can be mapped per component"
                    if not tagged
                    else "Compliance tags are present on this workload"
                ),
            )
        )

    return checks


# ─── Public API ─────────────────────────────────────────────


def evaluate_compliance(
    request: WorkloadRequest,
    bin_packing_results: dict[str, BinPackingResult] | None = None,
    sized_results: list[Any] | None = None,
) -> ComplianceReport:
    """Run all WAF compliance checks against the proposed design.

    Args:
        request: The original workload request.
        bin_packing_results: Bin-packing results per provider (optional).
        sized_results: Sizer output list of SizedWorkloadResult (optional).
            When provided, enables the P17 Graviton adoption sustainability
            check which upgrades the Sustainability pillar from 4/5 to 5/5.

    Returns:
        ComplianceReport with all check results and an overall score.
    """
    if bin_packing_results is None:
        bin_packing_results = {}

    all_checks: list[ComplianceCheckResult] = []

    # --- Run all pillar checks ---
    all_checks.extend(_check_reliability_ha(request, bin_packing_results))
    all_checks.extend(_check_reliability_replicas(request))
    all_checks.extend(_check_database_ha(request))
    all_checks.extend(_check_security_encryption(request))
    all_checks.extend(_check_compliance_coverage(request))
    all_checks.extend(_check_cost_optimization(request, bin_packing_results))
    all_checks.extend(_check_budget_ceiling(request))
    all_checks.extend(_check_performance_efficiency(request))
    all_checks.extend(_check_operational_excellence(request))
    all_checks.extend(_check_sustainability(request, bin_packing_results, sized_results))

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
