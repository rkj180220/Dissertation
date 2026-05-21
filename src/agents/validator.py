"""Validator Agent — Architecture quality gate.

Runs after FinOps in every pipeline.  Performs 7 checks:

0. **Pricing Data Integrity** — calls :mod:`src.engines.pricing_validator`
   (8 sub-checks including the new ``zero_cost_sku`` check that flags
   fit_score=0/$0 results which artificially deflate a provider's total cost)
1. **Architecture Correctness** — calls :mod:`src.engines.architecture_selector`
2. **Sizing Adequacy** — verifies every SKU meets workload requirements (±20 %)
3. **Budget Fit** — compares total cost to ``workload_request.budget_monthly_usd``
4. **WAF Compliance** — calls :mod:`src.engines.waf_compliance`
5. **Tier vs SLA Consistency** — verifies ``workload_request.tier`` is appropriate
   for the stated SLA (e.g. ``non_critical`` + 99.99 % SLA is flagged as a fail)
6. **Provider Preference Alignment** — warns when FinOps recommendation diverges
   from ``workload_request.user_preferred_provider``

All reasoning uses the **Principal Architect Reasoning Pattern** (§22g).

### Usage

```python
from src.agents.validator import run_validator_node

result = await run_validator_node(state, llm)
# state["validation_report"] and state["architecture_alternatives"] populated
```
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langfuse import observe

from src.engines.architecture_selector import derive_weights_from_workload, select_architecture
from src.engines.pricing_validator import validate_pricing
from src.engines.waf_compliance import evaluate_compliance
from src.models.conversation import ChatMessage, MessageRole
from src.orchestrator.state import AgentExecution, AgentStatus, OrchestratorState

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# System prompt — Principal Architect Reasoning (§22g)
# ---------------------------------------------------------------------------

_VALIDATOR_SYSTEM_PROMPT = """\
You are a principal cloud architect with 20+ years of experience acting as a \
senior architecture reviewer.

You have received a set of automated validation results and must produce a \
concise rationale for each finding. Use the Principal Architect Reasoning \
pattern below before producing output.

<architect_reasoning>
STEP 1 — UNDERSTAND THE PROBLEM
  - What is the user ACTUALLY trying to achieve?
  - Which validation failures, if any, indicate a fundamental architecture risk?

STEP 2 — IDENTIFY THE RISKS
  - Which findings are cosmetic vs. production-critical?
  - Which SKU undersizing could cause an outage at peak load?
  - Are pricing anomalies from wrong SKU class or genuine provider pricing?

STEP 3 — EVALUATE ALTERNATIVES
  - For each "fail", what is the lowest-friction fix?
  - Is the architecture recommendation change a cost optimisation or a reliability improvement?

STEP 4 — CHALLENGE MY INITIAL ASSUMPTION
  - Is the architecture selector output consistent with the workload's scaling pattern?
  - Am I recommending a change because it is genuinely better, or because it is novel?

STEP 5 — COMMIT WITH RATIONALE
  - Summarise all findings in one paragraph
  - Flag the highest severity issue first
  - Give the customer one clear next step
</architect_reasoning>

After reasoning, produce a one-paragraph summary of all validation findings.
Do NOT repeat the structured data — it is already in the report.
Focus on the most important insight.
"""


# ---------------------------------------------------------------------------
# Check helpers
# ---------------------------------------------------------------------------


def _check_sizing_adequacy(state: OrchestratorState) -> list[dict[str, Any]]:
    """Check 2 — verify each sized SKU meets the workload resource spec.

    Tolerance: selected resource must be ≥ required × 0.80.

    Args:
        state: Current orchestrator state.

    Returns:
        List of per-workload sizing check result dicts.
    """
    log = logger.bind(agent="validator", step="check_sizing_adequacy")
    results: list[dict[str, Any]] = []

    workload_profile = state.get("workload_profile")
    sized_results = state.get("sized_results", [])

    if workload_profile is None:
        log.warning("no_workload_profile_for_sizing_check")
        return results

    components = getattr(workload_profile, "components", [])
    comp_map = {c.workload_name: c for c in components}

    seen: set[str] = set()
    for sized in sized_results:
        name = getattr(sized, "workload_name", "unknown")
        if name in seen:
            continue
        seen.add(name)

        comp = comp_map.get(name)
        if comp is None:
            continue

        required_mem = getattr(comp, "memory_gb", 0.0) or 0.0
        required_vcpu = getattr(comp, "vcpus", 0) or 0
        required_storage = getattr(comp, "storage_gb", 0.0) or 0.0

        selected_sku = getattr(sized, "selected_sku", None)
        if selected_sku is None:
            results.append({"workload": name, "status": "skip", "reason": "no_sku"})
            continue

        sel_mem = getattr(selected_sku, "memory_gb", 0.0) or 0.0
        sel_vcpu = getattr(selected_sku, "vcpus", 0) or 0
        sel_storage = getattr(selected_sku, "storage_gb", 0.0) or 0.0

        tolerance = 0.80
        issues: list[str] = []
        if required_mem > 0 and sel_mem < required_mem * tolerance:
            issues.append(
                f"memory: selected {sel_mem:.1f} GB < required {required_mem:.1f} GB"
            )
        if required_vcpu > 0 and sel_vcpu < required_vcpu * tolerance:
            issues.append(f"vcpus: selected {sel_vcpu} < required {required_vcpu}")
        if required_storage > 0 and sel_storage < required_storage * tolerance:
            issues.append(
                f"storage: selected {sel_storage:.0f} GB < required {required_storage:.0f} GB"
            )

        provider = getattr(sized, "provider", "unknown")
        if hasattr(provider, "value"):
            provider = provider.value

        results.append(
            {
                "workload": name,
                "provider": provider,
                "status": "fail" if issues else "pass",
                "issues": issues,
                "selected_memory_gb": sel_mem,
                "required_memory_gb": required_mem,
                "selected_vcpus": sel_vcpu,
                "required_vcpus": required_vcpu,
            }
        )

    log.debug("sizing_adequacy_checked", total=len(results))
    return results


def _check_budget_fit(state: OrchestratorState) -> dict[str, Any]:
    """Check 3 — compare total monthly cost to the budget.

    Args:
        state: Current orchestrator state.

    Returns:
        Budget fit result dict.
    """
    workload_request = state.get("workload_request")
    budget = None
    if workload_request:
        budget = getattr(workload_request, "budget_monthly_usd", None)

    sized_results = state.get("sized_results", [])
    # Sum monthly costs for the recommended provider (or all)
    recommended = state.get("recommended_provider", "")
    total = 0.0
    count = 0
    for r in sized_results:
        prov = getattr(r, "provider", None)
        prov_val = prov.value if hasattr(prov, "value") else str(prov)
        if not recommended or prov_val == recommended:
            cost = getattr(r, "monthly_cost_usd", 0.0) or 0.0
            total += cost
            count += 1

    if budget is None or budget <= 0:
        return {
            "monthly_total": round(total, 2),
            "budget_monthly": None,
            "utilization_pct": None,
            "status": "skip",
            "note": "No budget specified",
        }

    utilization = (total / budget) * 100 if budget > 0 else 0.0
    if utilization > 100:
        status = "fail"
        note = f"Over budget by {utilization - 100:.1f}%"
    elif utilization < 50:
        status = "warn"
        note = "Under-utilising budget — may be under-provisioned"
    else:
        status = "pass"
        note = "Within budget"

    return {
        "monthly_total": round(total, 2),
        "budget_monthly": budget,
        "utilization_pct": round(utilization, 2),
        "status": status,
        "note": note,
    }


def _check_tier_sla_consistency(state: OrchestratorState) -> dict[str, Any]:
    """Check 5 — verify workload_request.tier is appropriate for the stated SLA.

    Catches miscategorisation where the clarifier sets a low tier (e.g.
    ``non_critical``) despite a 99.99 % SLA requirement or a public-safety sector.

    Args:
        state: Current orchestrator state.

    Returns:
        Dict with ``status`` (``"pass"`` | ``"warn"`` | ``"fail"``), ``tier``,
        ``max_uptime_sla``, and ``note``.
    """
    log = logger.bind(agent="validator", step="check_tier_sla_consistency")

    workload_request = state.get("workload_request")
    workload_profile = state.get("workload_profile")

    if workload_request is None:
        return {"status": "skip", "note": "No workload_request in state"}

    tier = getattr(workload_request, "tier", None)
    tier_value = tier.value if hasattr(tier, "value") else str(tier)

    # Collect max SLA across all profiled components
    max_sla: float = 0.0
    components = getattr(workload_profile, "components", []) if workload_profile else []
    for comp in components:
        sla = getattr(comp, "uptime_sla", None) or 0.0
        if sla > max_sla:
            max_sla = sla

    # Also check raw_user_input / enriched_input for SLA keywords if profile is empty
    raw = getattr(workload_request, "raw_user_input", "") or ""
    if not max_sla and "99.99" in raw:
        max_sla = 99.99
    elif not max_sla and "99.9" in raw:
        max_sla = 99.9

    # Detect critical sector from raw input
    _CRITICAL_SECTOR_KEYWORDS = (
        "public safety", "emergency", "fire protection", "police", "911",
        "hospital", "healthcare", "outages cost lives", "life safety",
        "first responder", "law enforcement",
    )
    is_critical_sector = any(kw in raw.lower() for kw in _CRITICAL_SECTOR_KEYWORDS)

    if tier_value == "non_critical":
        if max_sla >= 99.99 or is_critical_sector:
            status = "fail"
            note = (
                f"Tier '{tier_value}' is inconsistent with SLA {max_sla}% "
                + ("/ critical-sector keywords detected" if is_critical_sector else "")
                + ". Expected: mission_critical."
            )
        elif max_sla >= 99.9:
            status = "warn"
            note = (
                f"Tier '{tier_value}' may be too low for SLA {max_sla}%. "
                "Consider business_critical or mission_critical."
            )
        else:
            status = "pass"
            note = "Tier matches SLA requirements."
    elif tier_value == "business_critical" and max_sla >= 99.99:
        status = "warn"
        note = (
            f"Tier '{tier_value}' may be too low for SLA {max_sla}%. "
            "Consider mission_critical for 99.99% SLA."
        )
    else:
        status = "pass"
        note = "Tier matches SLA requirements."

    log.info(
        "tier_sla_consistency_checked",
        tier=tier_value,
        max_sla=max_sla,
        is_critical_sector=is_critical_sector,
        status=status,
    )
    return {
        "status": status,
        "tier": tier_value,
        "max_uptime_sla": max_sla,
        "is_critical_sector": is_critical_sector,
        "note": note,
    }


def _check_provider_preference_alignment(state: OrchestratorState) -> dict[str, Any]:
    """Check 6 — warn when recommendation diverges from the user's stated provider preference.

    If the user said "we're thinking AWS" but FinOps recommended GCP because GCP
    appeared cheaper due to incomplete pricing data, the validator surfaces this
    discrepancy so the RFP writer can include a justification paragraph.

    Args:
        state: Current orchestrator state.

    Returns:
        Dict with ``status`` (``"pass"`` | ``"warn"`` | ``"skip"``),
        ``preferred``, ``recommended``, and ``note``.
    """
    log = logger.bind(agent="validator", step="check_provider_preference_alignment")

    workload_request = state.get("workload_request")
    if workload_request is None:
        return {"status": "skip", "note": "No workload_request in state"}

    preferred = getattr(workload_request, "user_preferred_provider", None)
    if preferred is None:
        return {
            "status": "skip",
            "preferred": None,
            "recommended": state.get("recommended_provider"),
            "note": "No user provider preference stated.",
        }

    preferred_val = preferred.value if hasattr(preferred, "value") else str(preferred)
    recommended = state.get("recommended_provider", "")

    if recommended and recommended.lower() != preferred_val.lower():
        status = "warn"
        note = (
            f"User stated a preference for '{preferred_val}' but cost analysis "
            f"recommends '{recommended}'. The RFP should explicitly justify this "
            f"divergence (e.g. cost savings, missing services)."
        )
    else:
        status = "pass"
        note = f"Recommendation '{recommended}' aligns with user preference '{preferred_val}'."

    log.info(
        "provider_preference_alignment_checked",
        preferred=preferred_val,
        recommended=recommended,
        status=status,
    )
    return {
        "status": status,
        "preferred": preferred_val,
        "recommended": recommended,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Main validator node
# ---------------------------------------------------------------------------


@observe()
async def run_validator_node(
    state: OrchestratorState,
    llm: BaseChatModel,
) -> OrchestratorState:
    """Execute the Validator agent — architecture quality gate.

    Runs 5 checks and produces ``validation_report`` + ``architecture_alternatives``.

    Args:
        state: Current ``OrchestratorState``.
        llm: LLM instance (``BaseChatModel`` — never provider-specific).

    Returns:
        Updated ``OrchestratorState`` with validation results.
    """
    log = logger.bind(
        agent="validator",
        request_id=state.get("request_id", "unknown"),
    )
    start_time = datetime.now(timezone.utc)
    log.info("validator_node_started")

    validation_report: dict[str, Any] = {}
    architecture_alternatives: list = []

    try:
        sized_results = state.get("sized_results", [])
        workload_request = state.get("workload_request")
        workload_profile = state.get("workload_profile")

        # ── Check 0: Pricing Data Integrity ──────────────────────────────
        log.info("check_0_pricing_integrity_started")
        required_providers = []
        if workload_request:
            required_providers = [
                p.value if hasattr(p, "value") else str(p)
                for p in getattr(workload_request, "target_providers", [])
            ]
        pricing_result = validate_pricing(sized_results, [], required_providers)
        validation_report["pricing_validation"] = pricing_result.model_dump()
        error_count = sum(
            1 for f in pricing_result.findings if f.severity == "error"
        )
        log.info(
            "check_0_pricing_integrity_completed",
            is_valid=pricing_result.is_valid,
            finding_count=len(pricing_result.findings),
            error_count=error_count,
        )

        # ── Check 1: Architecture Correctness ────────────────────────────
        log.info("check_1_architecture_correctness_started")
        requirements = getattr(workload_profile, "components", []) if workload_profile else []
        # P15j: derive dynamic weights from user priority signals
        dynamic_weights = derive_weights_from_workload(workload_request)
        arch_rec = select_architecture(requirements, sized_results, workload_profile, weights=dynamic_weights)

        ranked = [
            {
                "name": opt.name,
                "label": opt.label,
                "score": round(opt.score, 3),
                "monthly_cost_estimate": round(opt.monthly_cost_estimate, 2),
                "rationale": opt.rationale,
                "trade_offs": opt.trade_offs,
                # Per-WAF-pillar scores for radar chart (P16e)
                "reliability_score": round(opt.reliability_score, 3),
                "cost_score": round(opt.cost_score, 3),
                "scale_score": round(opt.scale_score, 3),
                "compliance_score": round(opt.compliance_score, 3),
                "latency_score": round(opt.latency_score, 3),
            }
            for opt in arch_rec.ranked
        ]
        recommended_arch = arch_rec.winner.name
        arch_warning = arch_rec.warning
        if not arch_warning and arch_rec.ranked:
            top_option = arch_rec.ranked[0].name
            if top_option != recommended_arch:
                arch_warning = (
                    f"Recommended: {recommended_arch}, "
                    f"top-scored: {top_option}"
                )

        architecture_alternatives = ranked

        validation_report["architecture_validation"] = {
            "selected": recommended_arch,
            "recommended": recommended_arch,
            "ranked": ranked,
            "crossover_rps": arch_rec.key_signals.get("crossover_rps"),
            "warning": arch_warning,
        }
        log.info(
            "check_1_architecture_correctness_completed",
            recommended=recommended_arch,
            options=len(ranked),
        )

        # ── Check 2: Sizing Adequacy ──────────────────────────────────────
        log.info("check_2_sizing_adequacy_started")
        sizing_results = _check_sizing_adequacy(state)
        sizing_failures = [r for r in sizing_results if r.get("status") == "fail"]
        validation_report["sizing_validation"] = sizing_results
        log.info(
            "check_2_sizing_adequacy_completed",
            total=len(sizing_results),
            failures=len(sizing_failures),
        )

        # ── Check 3: Budget Fit ───────────────────────────────────────────
        log.info("check_3_budget_fit_started")
        budget_result = _check_budget_fit(state)
        validation_report["budget_validation"] = budget_result
        log.info("check_3_budget_fit_completed", status=budget_result.get("status"))

        # ── Check 4: WAF Compliance ───────────────────────────────────────
        log.info("check_4_waf_compliance_started")
        if workload_request:
            try:
                waf_report = evaluate_compliance(
                    workload_request,
                    sized_results=sized_results,
                )
                validation_report["waf_report"] = waf_report.model_dump()
                log.info(
                    "check_4_waf_compliance_completed",
                    overall_score=waf_report.overall_score,
                    passed=waf_report.passed_checks,
                    failed=waf_report.failed_checks,
                )
            except Exception:
                log.warning("check_4_waf_compliance_failed", exc_info=True)
                validation_report["waf_report"] = {"error": "WAF compliance check failed"}
        else:
            validation_report["waf_report"] = {"note": "No workload_request available"}

        # ── Check 5: Tier vs SLA Consistency ─────────────────────────────
        log.info("check_5_tier_sla_consistency_started")
        tier_result = _check_tier_sla_consistency(state)
        validation_report["tier_sla_validation"] = tier_result
        log.info(
            "check_5_tier_sla_consistency_completed",
            status=tier_result.get("status"),
            tier=tier_result.get("tier"),
            max_sla=tier_result.get("max_uptime_sla"),
        )

        # ── Check 6: Provider Preference Alignment ────────────────────────
        log.info("check_6_provider_preference_started")
        pref_result = _check_provider_preference_alignment(state)
        validation_report["provider_preference_validation"] = pref_result
        log.info(
            "check_6_provider_preference_completed",
            status=pref_result.get("status"),
            preferred=pref_result.get("preferred"),
            recommended=pref_result.get("recommended"),
        )

        # ── LLM summary (Principal Architect Reasoning) ───────────────────
        log.info("validator_llm_summary_started")
        tier_note = tier_result.get("note", "")
        pref_note = pref_result.get("note", "")
        summary_input = (
            f"Pricing integrity: {'valid' if pricing_result.is_valid else f'INVALID ({error_count} errors)'}\n"
            f"Architecture: {recommended_arch} (top-scored: {ranked[0]['name'] if ranked else 'N/A'})\n"
            f"Sizing failures: {len(sizing_failures)}/{len(sizing_results)}\n"
            f"Budget: {budget_result.get('note', 'unknown')}\n"
            f"WAF score: {validation_report.get('waf_report', {}).get('overall_score', 'N/A')}\n"
            f"Tier vs SLA: {tier_result.get('status', 'skip')} — {tier_note}\n"
            f"Provider preference: {pref_result.get('status', 'skip')} — {pref_note}"
        )

        try:
            response = await llm.ainvoke(
                [
                    SystemMessage(content=_VALIDATOR_SYSTEM_PROMPT),
                    HumanMessage(content=summary_input),
                ]
            )
            llm_summary = response.content if hasattr(response, "content") else str(response)
        except Exception:
            log.warning("validator_llm_summary_failed", exc_info=True)
            llm_summary = (
                f"Validation completed: {len(sized_results)} SKUs checked. "
                f"Architecture: {recommended_arch}. "
                f"Pricing valid: {pricing_result.is_valid}."
            )

        validation_report["llm_summary"] = llm_summary

        duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        log.info(
            "validator_node_completed",
            duration_ms=round(duration_ms, 1),
            checks_run=7,
            sizing_failures=len(sizing_failures),
            tier_sla_status=tier_result.get("status"),
            provider_pref_status=pref_result.get("status"),
        )

        agent_exec = state.get("agent_executions", {}).get(
            "validator", AgentExecution(agent_name="validator")
        )
        agent_exec.status = AgentStatus.COMPLETED
        agent_exec.completed_at = datetime.now(timezone.utc)
        agent_exec.duration_ms = duration_ms

        updated_executions = {
            **state.get("agent_executions", {}),
            "validator": agent_exec,
        }

        return {
            "validation_report": validation_report,
            "architecture_alternatives": architecture_alternatives,
            "current_agent": "validator",
            "agent_executions": updated_executions,
            "messages": [
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=(
                        f"[Validator] Architecture: {recommended_arch} | "
                        f"Pricing valid: {pricing_result.is_valid} | "
                        f"Sizing failures: {len(sizing_failures)} | "
                        f"Budget: {budget_result.get('status', 'unknown')} | "
                        f"Tier/SLA: {tier_result.get('status', 'skip')} | "
                        f"Provider pref: {pref_result.get('status', 'skip')}"
                    ),
                )
            ],
        }

    except Exception:
        duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        log.error(
            "validator_node_failed",
            exc_info=True,
            duration_ms=round(duration_ms, 1),
            architecture_alternatives_count=len(architecture_alternatives),
        )
        return {
            "validation_report": validation_report or {"error": "Validator failed"},
            "architecture_alternatives": architecture_alternatives,
            "current_agent": "validator",
        }
