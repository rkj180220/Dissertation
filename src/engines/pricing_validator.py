"""Pricing Comparison Validator Engine — P15k.

Validates that cross-provider ``SizedWorkloadResult`` records represent
genuine apples-to-apples comparisons before FinOps picks a vendor.  A
wrong SKU match (e.g. ``cache.t3.micro`` for a 16 GB Redis workload)
causes FinOps to recommend the wrong vendor — not because it is cheaper,
but because the comparison is invalid.

Purely algorithmic — no LLM call.  Fast, deterministic, fully unit-testable.

8 checks performed in order:

0. ``size_adequacy``    — selected SKU meets ≥ 80 % of required memory/vCPU.
1. ``tier_consistency`` — same pricing tier (on-demand/reserved/spot) across providers.
2. ``price_anomaly``    — SKU cost not > 5× or < 0.2× category-workload median.
3. ``sku_staleness``    — cached pricing data not older than ``ttl × 2`` = 14 days.
4. ``category_match``   — SKU service_category matches workload suggested_category.
5. ``provider_parity``  — all required providers have ≥ 1 result per workload.
6. ``ratio_check``      — instance memory:vCPU ratio within 25 % of required ratio.
7. ``zero_cost_sku``    — no result has fit_score=0 + cost=0 + no SKU (failed lookup).

Typical usage::

    from src.engines.pricing_validator import validate_pricing
    result = validate_pricing(state["sized_results"], state["requirements"])
    if not result.is_valid:
        # surface findings in RFP cost section
        ...
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

import structlog
from langfuse import observe
from pydantic import BaseModel, Field

from src.orchestrator.state import SizedWorkloadResult

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class PricingValidationFinding(BaseModel):
    """A single finding from the pricing validator.

    Attributes:
        check_id: The check that produced this finding (e.g. ``"size_adequacy"``).
        workload_name: The workload this finding applies to.
        provider: The provider (or ``"all"`` for cross-provider checks).
        severity: ``"error"`` | ``"warning"`` | ``"info"``.
        description: Human-readable explanation of the problem.
        actual_value: What was observed (e.g. ``"selected: 13.1 GB"``).
        expected_value: What was expected (e.g. ``"required: >= 16 GB"``).
        recommended_action: How to fix this finding.
    """

    check_id: str
    workload_name: str
    provider: str
    severity: str = Field(pattern=r"^(error|warning|info)$")
    description: str
    actual_value: str
    expected_value: str
    recommended_action: str


class PricingValidationResult(BaseModel):
    """Output of the pricing validator engine.

    Attributes:
        is_valid: True only if zero ``"error"`` severity findings.
        findings: All findings (errors, warnings, info).
        error_count: Number of error-severity findings.
        warning_count: Number of warning-severity findings.
        summary: One-line summary (e.g. ``"2 errors, 1 warning — comparison may be unreliable"``).
    """

    is_valid: bool
    findings: list[PricingValidationFinding]
    error_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    summary: str


# ---------------------------------------------------------------------------
# Internal per-check helpers
# ---------------------------------------------------------------------------


def _check_size_adequacy(
    results: list[SizedWorkloadResult],
    requirements_by_name: dict[str, Any],
) -> list[PricingValidationFinding]:
    """Check [0]: selected SKU meets ≥ 80 % of required memory_gb and vcpus.

    Args:
        results: Sizer output records.
        requirements_by_name: ``{workload_name: WorkloadRequirement}`` lookup.

    Returns:
        Findings for this check.
    """
    findings: list[PricingValidationFinding] = []
    for r in results:
        if r.selected_sku is None:
            continue
        req = requirements_by_name.get(r.workload_name)
        if req is None:
            continue

        required_mem = getattr(req.resources, "memory_gb", None)
        required_cpu = getattr(req.resources, "vcpus", None)
        sku_mem = r.selected_sku.attributes.get("memory_gb")
        sku_cpu = r.selected_sku.attributes.get("vcpus")

        if required_mem and sku_mem:
            try:
                if float(sku_mem) < float(required_mem) * 0.80:
                    findings.append(
                        PricingValidationFinding(
                            check_id="size_adequacy",
                            workload_name=r.workload_name,
                            provider=r.provider.value,
                            severity="error",
                            description="Selected SKU memory is below 80 % of required.",
                            actual_value=f"selected: {sku_mem} GB",
                            expected_value=f"required: >= {float(required_mem) * 0.80:.1f} GB",
                            recommended_action=(
                                f"Re-run sizer with memory_gb >= {required_mem} "
                                f"filter for {r.provider.value}."
                            ),
                        )
                    )
            except (TypeError, ValueError):
                pass

        if required_cpu and sku_cpu:
            try:
                if float(sku_cpu) < float(required_cpu) * 0.80:
                    findings.append(
                        PricingValidationFinding(
                            check_id="size_adequacy",
                            workload_name=r.workload_name,
                            provider=r.provider.value,
                            severity="error",
                            description="Selected SKU vCPU count is below 80 % of required.",
                            actual_value=f"selected: {sku_cpu} vCPUs",
                            expected_value=(
                                f"required: >= {float(required_cpu) * 0.80:.1f} vCPUs"
                            ),
                            recommended_action=(
                                f"Re-run sizer with vcpus >= {required_cpu} "
                                f"filter for {r.provider.value}."
                            ),
                        )
                    )
            except (TypeError, ValueError):
                pass

    return findings


def _check_tier_consistency(
    results: list[SizedWorkloadResult],
) -> list[PricingValidationFinding]:
    """Check [1]: same pricing tier used for all providers in a workload comparison.

    Args:
        results: Sizer output records.

    Returns:
        Findings for this check.
    """
    findings: list[PricingValidationFinding] = []
    by_workload: dict[str, list[SizedWorkloadResult]] = {}
    for r in results:
        by_workload.setdefault(r.workload_name, []).append(r)

    for wl_name, wl_results in by_workload.items():
        tiers: set[str] = {
            r.selected_sku.pricing_tier.value
            for r in wl_results
            if r.selected_sku is not None
        }
        if len(tiers) > 1:
            findings.append(
                PricingValidationFinding(
                    check_id="tier_consistency",
                    workload_name=wl_name,
                    provider="all",
                    severity="warning",
                    description="Mixed pricing tiers across providers — comparison may be unfair.",
                    actual_value=f"tiers found: {sorted(tiers)}",
                    expected_value="all providers use the same tier (e.g. all on-demand)",
                    recommended_action=(
                        "Normalise all providers to on-demand before comparing, "
                        "or apply equivalent reserved discounts to each provider."
                    ),
                )
            )

    return findings


def _check_price_anomaly(
    results: list[SizedWorkloadResult],
) -> list[PricingValidationFinding]:
    """Check [2]: no SKU cost > 5× or < 0.2× the per-workload median.

    Args:
        results: Sizer output records.

    Returns:
        Findings for this check.
    """
    findings: list[PricingValidationFinding] = []

    by_workload: dict[str, list[SizedWorkloadResult]] = {}
    for r in results:
        if r.monthly_cost_usd > 0:
            by_workload.setdefault(r.workload_name, []).append(r)

    for wl_name, wl_results in by_workload.items():
        costs = [r.monthly_cost_usd for r in wl_results]
        if len(costs) < 2:
            continue
        med = statistics.median(costs)
        if med == 0:
            continue

        for r in wl_results:
            ratio = r.monthly_cost_usd / med
            if ratio > 5.0:
                findings.append(
                    PricingValidationFinding(
                        check_id="price_anomaly",
                        workload_name=wl_name,
                        provider=r.provider.value,
                        severity="error",
                        description=(
                            f"SKU cost is {ratio:.1f}× the per-workload median — "
                            f"likely wrong SKU family."
                        ),
                        actual_value=f"${r.monthly_cost_usd:,.2f}/mo",
                        expected_value=f"median ${med:,.2f}/mo (max 5×: ${med * 5:,.2f})",
                        recommended_action=(
                            "Verify SKU family is correct for this workload category."
                        ),
                    )
                )
            elif ratio < 0.2:
                findings.append(
                    PricingValidationFinding(
                        check_id="price_anomaly",
                        workload_name=wl_name,
                        provider=r.provider.value,
                        severity="error",
                        description=(
                            f"SKU cost is {ratio:.2f}× the per-workload median — "
                            f"likely undersized SKU."
                        ),
                        actual_value=f"${r.monthly_cost_usd:,.2f}/mo",
                        expected_value=f"median ${med:,.2f}/mo (min 0.2×: ${med * 0.2:,.2f})",
                        recommended_action=(
                            "Verify selected SKU is not a micro/nano tier used by mistake."
                        ),
                    )
                )

    return findings


def _check_sku_staleness(
    results: list[SizedWorkloadResult],
    ttl_multiplier: float = 2.0,
) -> list[PricingValidationFinding]:
    """Check [3]: cached SKU data within acceptable staleness window.

    Uses ``effective_date`` as a proxy for cache time.  Staleness is
    flagged when the SKU age exceeds ``ttl_multiplier × 7`` days.

    Args:
        results: Sizer output records.
        ttl_multiplier: Staleness tolerance multiplier (default 2.0 → 14 days).

    Returns:
        Findings for this check.
    """
    findings: list[PricingValidationFinding] = []
    max_age_days = 7.0 * ttl_multiplier
    now = datetime.now(tz=timezone.utc)

    for r in results:
        if r.selected_sku is None:
            continue
        age_days = (now - r.selected_sku.effective_date).days
        if age_days > max_age_days:
            findings.append(
                PricingValidationFinding(
                    check_id="sku_staleness",
                    workload_name=r.workload_name,
                    provider=r.provider.value,
                    severity="warning",
                    description=(
                        f"Cached pricing data is {age_days} days old — may be stale."
                    ),
                    actual_value=f"effective_date: {r.selected_sku.effective_date.date()}",
                    expected_value=f"within {int(max_age_days)} days of today",
                    recommended_action=(
                        "Invalidate cache and re-fetch latest pricing from provider API."
                    ),
                )
            )

    return findings


def _check_category_match(
    results: list[SizedWorkloadResult],
    requirements_by_name: dict[str, Any],
) -> list[PricingValidationFinding]:
    """Check [4]: SKU service_category matches workload suggested_category.

    Known equivalent category pairs (e.g. container/kubernetes) are allowed.

    Args:
        results: Sizer output records.
        requirements_by_name: ``{workload_name: WorkloadRequirement}`` lookup.

    Returns:
        Findings for this check.
    """
    findings: list[PricingValidationFinding] = []

    # Category pairs considered equivalent (bidirectional)
    _EQUIVALENT = {
        ("container", "kubernetes"),
        ("kubernetes", "container"),
        ("serverless_function", "serverless"),
        ("serverless", "serverless_function"),
        ("serverless_compute", "serverless_function"),
        ("serverless_function", "serverless_compute"),
    }

    for r in results:
        if r.selected_sku is None:
            continue
        req = requirements_by_name.get(r.workload_name)
        if req is None:
            continue

        actual = r.selected_sku.service_category.value
        expected = req.suggested_category.value
        if actual != expected and (actual, expected) not in _EQUIVALENT:
            findings.append(
                PricingValidationFinding(
                    check_id="category_match",
                    workload_name=r.workload_name,
                    provider=r.provider.value,
                    severity="error",
                    description=(
                        f"SKU category '{actual}' does not match "
                        f"workload category '{expected}'."
                    ),
                    actual_value=f"sku.service_category = {actual}",
                    expected_value=f"workload.suggested_category = {expected}",
                    recommended_action=(
                        f"Re-query pricing API for {r.provider.value} "
                        f"using service_category='{expected}'."
                    ),
                )
            )

    return findings


def _check_provider_parity(
    results: list[SizedWorkloadResult],
    required_providers: list[str] | None = None,
) -> list[PricingValidationFinding]:
    """Check [5]: all required providers have ≥ 1 result per workload.

    Args:
        results: Sizer output records.
        required_providers: Expected provider names (default: aws, azure, gcp).

    Returns:
        Findings for this check.
    """
    findings: list[PricingValidationFinding] = []
    expected = set(required_providers or ["aws", "azure", "gcp"])

    by_workload: dict[str, set[str]] = {}
    for r in results:
        by_workload.setdefault(r.workload_name, set()).add(r.provider.value)

    for wl_name, found in by_workload.items():
        missing = expected - found
        if missing:
            findings.append(
                PricingValidationFinding(
                    check_id="provider_parity",
                    workload_name=wl_name,
                    provider=", ".join(sorted(missing)),
                    severity="warning",
                    description=(
                        f"Provider(s) {sorted(missing)} missing from comparison — "
                        f"FinOps cannot produce a complete cross-provider recommendation."
                    ),
                    actual_value=f"found providers: {sorted(found)}",
                    expected_value=f"expected providers: {sorted(expected)}",
                    recommended_action=(
                        "Re-run sizer for missing providers or exclude them from "
                        "target_providers explicitly."
                    ),
                )
            )

    return findings


def _check_ratio(
    results: list[SizedWorkloadResult],
    requirements_by_name: dict[str, Any],
) -> list[PricingValidationFinding]:
    """Check [6]: selected instance memory:vCPU ratio within 25 % of required ratio.

    Args:
        results: Sizer output records.
        requirements_by_name: ``{workload_name: WorkloadRequirement}`` lookup.

    Returns:
        Findings for this check.
    """
    findings: list[PricingValidationFinding] = []

    for r in results:
        if r.selected_sku is None:
            continue
        req = requirements_by_name.get(r.workload_name)
        if req is None:
            continue

        required_mem = getattr(req.resources, "memory_gb", None)
        required_cpu = getattr(req.resources, "vcpus", None)
        sku_mem = r.selected_sku.attributes.get("memory_gb")
        sku_cpu = r.selected_sku.attributes.get("vcpus")

        if not (required_mem and required_cpu and sku_mem and sku_cpu):
            continue

        try:
            req_ratio = float(required_mem) / float(required_cpu)
            sku_ratio = float(sku_mem) / float(sku_cpu)
            tolerance = req_ratio * 0.25
            if abs(sku_ratio - req_ratio) > tolerance:
                direction = "memory-optimized" if req_ratio > sku_ratio else "compute-optimized"
                findings.append(
                    PricingValidationFinding(
                        check_id="ratio_check",
                        workload_name=r.workload_name,
                        provider=r.provider.value,
                        severity="warning",
                        description=(
                            f"Selected instance memory:vCPU ratio ({sku_ratio:.1f}) "
                            f"deviates > 25 % from required ({req_ratio:.1f}) — "
                            f"may be wrong instance family."
                        ),
                        actual_value=f"sku ratio: {sku_ratio:.1f} GB/vCPU",
                        expected_value=(
                            f"required: {req_ratio:.1f} ± {tolerance:.1f} GB/vCPU"
                        ),
                        recommended_action=(
                            f"Switch to a {direction} instance family "
                            f"for {r.provider.value}."
                        ),
                    )
                )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return findings


# Fixed-cost workload name prefixes — these legitimately have no SKU (flat rate).
_FIXED_COST_PREFIXES = ("[Infra]", "K8s Cluster", "Load Balancer")


def _check_zero_cost_sku(
    results: list[SizedWorkloadResult],
) -> list[PricingValidationFinding]:
    """Check [7]: no result has fit_score=0, monthly_cost=0, and no SKU selected.

    A result with all three conditions indicates the sizer found no matching
    SKU for that provider/workload combination.  Including it in a cost
    comparison makes that provider appear artificially cheap.  Fixed-cost
    line items (K8s management fee, load balancer, data transfer) are excluded
    because they legitimately have no SKU but use a flat rate.

    Args:
        results: Sizer output records.

    Returns:
        Findings for this check.
    """
    findings: list[PricingValidationFinding] = []

    for r in results:
        if any(r.workload_name.startswith(pfx) for pfx in _FIXED_COST_PREFIXES):
            continue
        if (
            r.selected_sku is None
            and r.monthly_cost_usd == 0.0
            and r.fit_score == 0.0
        ):
            provider = r.provider.value if hasattr(r.provider, "value") else str(r.provider)
            findings.append(
                PricingValidationFinding(
                    check_id="zero_cost_sku",
                    workload_name=r.workload_name,
                    provider=provider,
                    severity="error",
                    description=(
                        f"No SKU found for '{r.workload_name}' on {provider} — "
                        f"fit_score=0, cost=$0.  Including this in cost comparison "
                        f"makes {provider} appear artificially cheaper."
                    ),
                    actual_value="fit_score=0.0, monthly_cost_usd=0.0, selected_sku=None",
                    expected_value="fit_score > 0 and monthly_cost_usd > 0",
                    recommended_action=(
                        f"Re-run sizer for {provider} '{r.workload_name}' with broader "
                        f"SKU search, or exclude {provider} from comparison if no "
                        f"equivalent managed service exists."
                    ),
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


@observe()
def validate_pricing(
    sized_results: list[SizedWorkloadResult],
    requirements: list | None = None,
    required_providers: list[str] | None = None,
) -> PricingValidationResult:
    """Run 8 pricing integrity checks on sizer output.

    Purely algorithmic — no LLM call.  Deterministic and fast.

    Args:
        sized_results: All ``SizedWorkloadResult`` items from orchestrator state.
        requirements: Optional ``list[WorkloadRequirement]`` for context-aware checks
            (size_adequacy, category_match, ratio_check).
        required_providers: Expected provider names for the provider_parity check
            (default: ``["aws", "azure", "gcp"]``).

    Returns:
        :class:`PricingValidationResult` with ``is_valid`` flag and all findings.
    """
    log = logger.bind(agent="pricing_validator", step="validate_pricing")
    log.info("pricing_validation_started", result_count=len(sized_results))

    req_by_name: dict[str, Any] = {}
    if requirements:
        for req in requirements:
            req_by_name[req.name] = req

    all_findings: list[PricingValidationFinding] = []
    all_findings.extend(_check_size_adequacy(sized_results, req_by_name))
    all_findings.extend(_check_tier_consistency(sized_results))
    all_findings.extend(_check_price_anomaly(sized_results))
    all_findings.extend(_check_sku_staleness(sized_results))
    all_findings.extend(_check_category_match(sized_results, req_by_name))
    all_findings.extend(_check_provider_parity(sized_results, required_providers))
    all_findings.extend(_check_ratio(sized_results, req_by_name))
    all_findings.extend(_check_zero_cost_sku(sized_results))

    error_count = sum(1 for f in all_findings if f.severity == "error")
    warning_count = sum(1 for f in all_findings if f.severity == "warning")
    is_valid = error_count == 0

    if not is_valid:
        log.warning(
            "pricing_comparison_invalid",
            error_count=error_count,
            warning_count=warning_count,
        )

    summary = (
        f"{error_count} error{'s' if error_count != 1 else ''}, "
        f"{warning_count} warning{'s' if warning_count != 1 else ''} — "
        f"{'comparison is valid' if is_valid else 'comparison may be unreliable'}"
    )

    log.info("pricing_validation_completed", is_valid=is_valid, summary=summary)

    return PricingValidationResult(
        is_valid=is_valid,
        findings=all_findings,
        error_count=error_count,
        warning_count=warning_count,
        summary=summary,
    )
