"""RFP Writer Agent — Procurement document generation.

The RFP Writer is the fifth and final stage of the orchestration
pipeline.  It consumes all upstream agent outputs and produces a
structured Markdown procurement document (RFP) along with an
executive summary for stakeholders.

### Responsibilities

1. **Executive summary** — LLM-generated high-level overview of the
   recommendation, costs, and trade-offs.
2. **Technical specification** — Tabulated SKU selections per
   provider with resource specifications.
3. **Cost comparison tables** — Per-provider cost breakdowns with
   RI/spot savings.
4. **WAF compliance section** — Compliance check results from the
   WAF engine.
5. **Vendor shortlist** — Recommended providers with justification.

### Flow

```
All upstream state (request, profile, sized_results, cost_comparison)
      │
      ▼
┌──────────────────────────────────────────┐
│  1. Run WAF compliance checks            │
│  2. Generate executive summary (LLM)     │
│  3. Build Markdown sections              │
│  4. Assemble full RFP document           │
└──────────────────┬───────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────┐
│  state['rfp_document']                   │
│  state['executive_summary']              │
│  state['compliance_report']              │
└──────────────────────────────────────────┘
```

### Usage

```python
from src.agents.rfp_writer import run_rfp_writer_node

state = await run_rfp_writer_node(state, llm)
# state['rfp_document'] contains the full Markdown RFP
# state['executive_summary'] contains the stakeholder summary
# state['compliance_report'] contains WAF check results
```
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langfuse import observe

from src.engines.waf_compliance import evaluate_compliance
from src.models.cloud_resource import CloudProvider
from src.models.conversation import ChatMessage, MessageRole
from src.models.recommendation import (
    ComplianceReport,
    CostComparison,
    ProviderCostBreakdown,
)
from src.models.workload import WorkloadProfile, WorkloadRequest
from src.orchestrator.state import (
    AgentExecution,
    AgentStatus,
    OrchestratorState,
    SizedWorkloadResult,
)

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Document section builders
# ---------------------------------------------------------------------------


def _build_header_section(
    workload_request: WorkloadRequest,
) -> str:
    """Build the RFP document header.

    Args:
        workload_request: The original workload request.

    Returns:
        Markdown header string.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"# Cloud Infrastructure Procurement — {workload_request.project_name}\n\n"
        f"**Generated**: {now}  \n"
        f"**Environment**: {workload_request.environment.value}  \n"
        f"**Criticality**: {workload_request.tier.value}  \n"
        f"**Target Providers**: "
        f"{', '.join(p.value.upper() for p in workload_request.target_providers)}  \n"
        f"**Region**: {workload_request.preferred_region}  \n"
    )


def _build_workload_summary_section(
    workload_profile: WorkloadProfile,
    workload_request: WorkloadRequest,
) -> str:
    """Build the workload summary section.

    Args:
        workload_profile: Profiler output.
        workload_request: Original request.

    Returns:
        Markdown section string.
    """
    lines = ["## 1. Workload Summary\n"]

    lines.append(
        f"| Component | Category | vCPUs | Memory (GB) | Storage (GB) | GPU |\n"
        f"|-----------|----------|-------|-------------|--------------|-----|\n"
    )
    for comp in workload_profile.components:
        gpu = "Yes" if comp.requires_gpu else "No"
        lines.append(
            f"| {comp.workload_name} | {comp.resolved_category.value} "
            f"| {comp.estimated_vcpus} | {comp.estimated_memory_gb} "
            f"| {comp.estimated_storage_gb} | {gpu} |"
        )

    lines.append(
        f"\n**Totals**: {workload_profile.total_vcpus} vCPUs, "
        f"{workload_profile.total_memory_gb} GB RAM, "
        f"{workload_profile.total_storage_gb} GB storage"
    )
    if workload_profile.requires_gpu:
        lines.append(f", {workload_profile.total_gpu_count} GPU(s)")

    if workload_request.budget_monthly_usd:
        lines.append(
            f"\n\n**Budget Ceiling**: ${workload_request.budget_monthly_usd:,.2f}/month"
        )

    return "\n".join(lines) + "\n"


def _build_sku_selection_section(
    sized_results: list[SizedWorkloadResult],
) -> str:
    """Build the SKU selection section with per-provider tables.

    Args:
        sized_results: All sizing results.

    Returns:
        Markdown section string.
    """
    lines = ["## 2. SKU Selections\n"]

    # Group by provider
    by_provider: dict[str, list[SizedWorkloadResult]] = {}
    for r in sized_results:
        by_provider.setdefault(r.provider.value, []).append(r)

    for prov in sorted(by_provider.keys()):
        results = by_provider[prov]
        lines.append(f"### {prov.upper()}\n")
        lines.append(
            "| Workload | SKU | Monthly Cost | Fit Score | Rationale |\n"
            "|----------|-----|-------------|-----------|------------|"
        )
        for r in results:
            sku = r.selected_sku.sku_name if r.selected_sku else "N/A"
            cost = f"${r.monthly_cost_usd:,.2f}"
            fit = f"{r.fit_score:.2f}"
            # Truncate rationale for table readability
            rationale = r.rationale[:80] + "..." if len(r.rationale) > 80 else r.rationale
            lines.append(f"| {r.workload_name} | {sku} | {cost} | {fit} | {rationale} |")
        lines.append("")

    return "\n".join(lines) + "\n"


def _build_cost_comparison_section(
    cost_comparison: CostComparison,
) -> str:
    """Build the cost comparison section.

    Args:
        cost_comparison: FinOps comparison data.

    Returns:
        Markdown section string.
    """
    lines = ["## 3. Cost Comparison\n"]

    lines.append(
        "| Provider | Compute | Database | Storage | K8s | Networking | "
        "Serverless | Other | **Total/mo** | **Total/yr** |\n"
        "|----------|---------|----------|---------|-----|------------|"
        "-----------|-------|-------------|-------------|"
    )
    for pb in sorted(cost_comparison.providers, key=lambda p: p.total_monthly_usd):
        lines.append(
            f"| {pb.provider.value.upper()} "
            f"| ${pb.compute_monthly_usd:,.2f} "
            f"| ${pb.database_monthly_usd:,.2f} "
            f"| ${pb.storage_monthly_usd:,.2f} "
            f"| ${pb.kubernetes_monthly_usd:,.2f} "
            f"| ${pb.networking_monthly_usd:,.2f} "
            f"| ${pb.serverless_monthly_usd:,.2f} "
            f"| ${pb.other_monthly_usd:,.2f} "
            f"| **${pb.total_monthly_usd:,.2f}** "
            f"| **${pb.total_annual_usd:,.2f}** |"
        )

    # Savings opportunities
    lines.append("\n### Savings Opportunities\n")
    lines.append(
        "| Provider | Reserved 1yr | RI-1yr Savings | Reserved 3yr | "
        "RI-3yr Savings | Spot | Spot Savings |\n"
        "|----------|-------------|----------------|-------------|"
        "----------------|------|-------------|"
    )
    for pb in sorted(cost_comparison.providers, key=lambda p: p.total_monthly_usd):
        ri1 = f"${pb.reserved_1yr_monthly_usd:,.2f}" if pb.reserved_1yr_monthly_usd else "N/A"
        ri1s = f"{pb.reserved_1yr_savings_pct:.0f}%" if pb.reserved_1yr_savings_pct else "—"
        ri3 = f"${pb.reserved_3yr_monthly_usd:,.2f}" if pb.reserved_3yr_monthly_usd else "N/A"
        ri3s = f"{pb.reserved_3yr_savings_pct:.0f}%" if pb.reserved_3yr_savings_pct else "—"
        sp = f"${pb.spot_monthly_usd:,.2f}" if pb.spot_monthly_usd else "N/A"
        sps = f"{pb.spot_savings_pct:.0f}%" if pb.spot_savings_pct else "—"
        lines.append(
            f"| {pb.provider.value.upper()} | {ri1} | {ri1s} | {ri3} | {ri3s} | {sp} | {sps} |"
        )

    # Budget check
    if cost_comparison.budget_monthly_usd is not None:
        status = "**EXCEEDED**" if cost_comparison.budget_exceeded else "Within budget"
        lines.append(
            f"\n**Budget**: ${cost_comparison.budget_monthly_usd:,.2f}/mo — {status}"
        )

    if cost_comparison.cheapest_provider:
        lines.append(
            f"\n**Recommended Provider**: "
            f"{cost_comparison.cheapest_provider.value.upper()} "
            f"(saves {cost_comparison.savings_vs_most_expensive_pct:.1f}% "
            f"vs. most expensive)"
        )

    return "\n".join(lines) + "\n"


def _build_compliance_section(
    compliance_report: ComplianceReport,
) -> str:
    """Build the WAF compliance section.

    Args:
        compliance_report: WAF check results.

    Returns:
        Markdown section string.
    """
    lines = ["## 4. Compliance Report (Well-Architected Framework)\n"]

    lines.append(
        f"**Overall Score**: {compliance_report.compliance_score_pct:.0f}% "
        f"({compliance_report.passed_checks}/{compliance_report.total_checks} checks passed)\n"
    )

    if compliance_report.checks:
        lines.append(
            "| Pillar | Check | Status | Severity | Finding |\n"
            "|--------|-------|--------|----------|---------|"
        )
        for check in compliance_report.checks:
            status = "PASS" if check.passed else "**FAIL**"
            finding = check.finding[:60] + "..." if len(check.finding) > 60 else check.finding
            lines.append(
                f"| {check.pillar} | {check.check_name} | {status} "
                f"| {check.severity} | {finding} |"
            )

        # List recommendations for failed checks
        failed = [c for c in compliance_report.checks if not c.passed]
        if failed:
            lines.append("\n### Recommendations\n")
            for check in failed:
                lines.append(
                    f"- **{check.pillar} — {check.check_name}**: "
                    f"{check.recommendation}"
                )

    return "\n".join(lines) + "\n"


def _build_vendor_shortlist_section(
    cost_comparison: CostComparison,
    sized_results: list[SizedWorkloadResult],
) -> str:
    """Build the vendor shortlist section.

    Args:
        cost_comparison: FinOps comparison data.
        sized_results: All sizing results.

    Returns:
        Markdown section string.
    """
    lines = ["## 5. Vendor Shortlist & Recommendation\n"]

    # Rank providers
    sorted_providers = sorted(
        cost_comparison.providers,
        key=lambda p: p.total_monthly_usd,
    )

    for rank, pb in enumerate(sorted_providers, 1):
        # Count workloads with good fit for this provider
        prov_results = [r for r in sized_results if r.provider == pb.provider]
        avg_fit = (
            sum(r.fit_score for r in prov_results) / len(prov_results)
            if prov_results else 0.0
        )
        good_fit = sum(1 for r in prov_results if r.fit_score >= 0.6)

        label = " **(Recommended)**" if rank == 1 else ""
        lines.append(
            f"### {rank}. {pb.provider.value.upper()}{label}\n\n"
            f"- **Monthly Cost**: ${pb.total_monthly_usd:,.2f}\n"
            f"- **Annual Cost**: ${pb.total_annual_usd:,.2f}\n"
            f"- **Avg. Fit Score**: {avg_fit:.2f}\n"
            f"- **SKUs with good fit (≥0.6)**: {good_fit}/{len(prov_results)}\n"
        )
        if pb.reserved_1yr_savings_pct:
            lines.append(
                f"- **RI 1yr Savings**: {pb.reserved_1yr_savings_pct:.0f}%\n"
            )
        if pb.spot_savings_pct:
            lines.append(
                f"- **Spot Savings**: {pb.spot_savings_pct:.0f}%\n"
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# LLM-generated executive summary
# ---------------------------------------------------------------------------

_EXECUTIVE_SUMMARY_PROMPT = """\
You are drafting an executive summary for a cloud infrastructure procurement \
document. Write a concise 2-3 paragraph summary that:

1. States the project name and infrastructure requirements
2. Recommends the best cloud provider with cost justification
3. Highlights key trade-offs, savings opportunities, and compliance status
4. Provides a clear call to action

Target audience: IT leadership / procurement team. Use professional language.
Do NOT use markdown headers. Use plain paragraphs.
"""


@observe()
async def _generate_executive_summary(
    llm: BaseChatModel,
    workload_request: WorkloadRequest,
    workload_profile: WorkloadProfile,
    cost_comparison: CostComparison,
    compliance_report: ComplianceReport,
) -> str:
    """Generate an executive summary using the LLM.

    Args:
        llm: The LLM model (BaseChatModel interface).
        workload_request: Original request.
        workload_profile: Profiler output.
        cost_comparison: FinOps comparison.
        compliance_report: WAF report.

    Returns:
        Executive summary string.
    """
    log = logger.bind(agent="rfp_writer", step="executive_summary")
    log.debug("executive_summary_started")

    provider_costs = {
        pb.provider.value: pb.total_monthly_usd
        for pb in cost_comparison.providers
    }

    user_content = json.dumps({
        "project_name": workload_request.project_name,
        "environment": workload_request.environment.value,
        "tier": workload_request.tier.value,
        "total_vcpus": workload_profile.total_vcpus,
        "total_memory_gb": workload_profile.total_memory_gb,
        "component_count": len(workload_profile.components),
        "budget_monthly_usd": workload_request.budget_monthly_usd,
        "cheapest_provider": (
            cost_comparison.cheapest_provider.value
            if cost_comparison.cheapest_provider else "N/A"
        ),
        "provider_costs": provider_costs,
        "savings_vs_expensive_pct": cost_comparison.savings_vs_most_expensive_pct,
        "budget_exceeded": cost_comparison.budget_exceeded,
        "compliance_score": compliance_report.compliance_score_pct,
        "compliance_passed": compliance_report.passed_checks,
        "compliance_total": compliance_report.total_checks,
    }, indent=2)

    messages = [
        SystemMessage(content=_EXECUTIVE_SUMMARY_PROMPT),
        HumanMessage(
            content=f"Generate an executive summary for:\n\n{user_content}"
        ),
    ]

    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, "content") else str(response)
        log.debug("executive_summary_completed", length=len(content))
        return content[:1500]
    except Exception:
        log.warning("executive_summary_failed", exc_info=True)
        # Heuristic fallback
        cheapest = cost_comparison.cheapest_provider
        cheapest_cost = next(
            (p.total_monthly_usd for p in cost_comparison.providers
             if p.provider == cheapest),
            0.0,
        ) if cheapest else 0.0

        return (
            f"This document presents a cloud infrastructure procurement "
            f"recommendation for {workload_request.project_name}. "
            f"The analysis evaluates {len(workload_profile.components)} "
            f"workload components requiring {workload_profile.total_vcpus} vCPUs "
            f"and {workload_profile.total_memory_gb} GB of memory across "
            f"{len(cost_comparison.providers)} cloud providers.\n\n"
            f"Based on cost-optimized sizing, "
            f"{cheapest.value.upper() if cheapest else 'N/A'} is recommended "
            f"at an estimated ${cheapest_cost:,.2f}/month, saving "
            f"{cost_comparison.savings_vs_most_expensive_pct:.1f}% compared to "
            f"the most expensive option. WAF compliance score: "
            f"{compliance_report.compliance_score_pct:.0f}%."
        )


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------


@observe()
async def run_rfp_writer_node(
    state: OrchestratorState,
    llm: BaseChatModel,
) -> OrchestratorState:
    """Execute the RFP Writer agent — generate procurement document.

    This is a LangGraph node function.  It reads all upstream state
    and produces a Markdown RFP document, executive summary, and
    compliance report.

    Args:
        state: Current ``OrchestratorState`` (TypedDict).
        llm: LLM instance for generating narrative sections
            (``BaseChatModel`` interface).

    Returns:
        Updated ``OrchestratorState`` with ``rfp_document``,
        ``executive_summary``, ``compliance_report``, and a
        summary message appended to ``messages``.

    Raises:
        ValueError: If required upstream data is missing from state.
    """
    log = logger.bind(
        agent="rfp_writer",
        request_id=state.get("request_id", "unknown"),
    )

    start_time = datetime.now(timezone.utc)
    log.info("rfp_writer_node_started")

    try:
        # ── Validate inputs ───────────────────────────────────
        workload_request: WorkloadRequest | None = state.get("workload_request")
        if workload_request is None:
            raise ValueError("workload_request is missing from state")

        workload_profile: WorkloadProfile | None = state.get("workload_profile")
        if workload_profile is None:
            raise ValueError("workload_profile is missing from state")

        sized_results: list[SizedWorkloadResult] = state.get("sized_results", [])
        if not sized_results:
            raise ValueError("sized_results is empty — Sizer must run first")

        cost_comparison_data = state.get("cost_comparison", {})
        if cost_comparison_data:
            cost_comparison = CostComparison(**cost_comparison_data)
        else:
            cost_comparison = CostComparison()

        log.info(
            "generating_rfp",
            project=workload_request.project_name,
            components=len(workload_profile.components),
            sized_results=len(sized_results),
            providers=len(cost_comparison.providers),
        )

        # ── Run WAF compliance checks ─────────────────────────
        log.info("running_compliance_checks")
        compliance_report = evaluate_compliance(workload_request)
        log.info(
            "compliance_checks_completed",
            score=compliance_report.compliance_score_pct,
            passed=compliance_report.passed_checks,
            total=compliance_report.total_checks,
        )

        # ── Generate executive summary ────────────────────────
        executive_summary = await _generate_executive_summary(
            llm,
            workload_request,
            workload_profile,
            cost_comparison,
            compliance_report,
        )

        # ── Assemble the RFP document ─────────────────────────
        sections = [
            _build_header_section(workload_request),
            f"## Executive Summary\n\n{executive_summary}\n",
            _build_workload_summary_section(workload_profile, workload_request),
            _build_sku_selection_section(sized_results),
            _build_cost_comparison_section(cost_comparison),
            _build_compliance_section(compliance_report),
            _build_vendor_shortlist_section(cost_comparison, sized_results),
        ]

        rfp_document = "\n---\n\n".join(sections)

        log.info(
            "rfp_document_assembled",
            document_length=len(rfp_document),
            sections=len(sections),
        )

        # ── Build summary message ─────────────────────────────
        summary_content = (
            f"**RFP Document Generated** — "
            f"{len(rfp_document):,} characters, {len(sections)} sections.\n\n"
            f"**Compliance**: {compliance_report.compliance_score_pct:.0f}% "
            f"({compliance_report.passed_checks}/{compliance_report.total_checks} passed)\n\n"
            f"**Executive Summary**: {executive_summary[:200]}..."
        )

        summary_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=summary_content,
            agent_name="rfp_writer",
            metadata={
                "document_length": len(rfp_document),
                "compliance_score": compliance_report.compliance_score_pct,
                "sections": len(sections),
            },
        )

        # ── Compute timing ────────────────────────────────────
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        execution = AgentExecution(
            agent_name="rfp_writer",
            status=AgentStatus.COMPLETED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
        )

        log.info(
            "rfp_writer_node_completed",
            duration_ms=round(duration_ms, 1),
            document_length=len(rfp_document),
        )

        # ── Return state update ───────────────────────────────
        return {
            "rfp_document": rfp_document,
            "executive_summary": executive_summary,
            "compliance_report": compliance_report.model_dump(),
            "messages": [summary_message],
            "current_agent": "rfp_writer",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "rfp_writer": execution,
            },
        }

    except Exception:
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        log.error(
            "rfp_writer_node_failed",
            exc_info=True,
            duration_ms=round(duration_ms, 1),
        )

        execution = AgentExecution(
            agent_name="rfp_writer",
            status=AgentStatus.FAILED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
            error_message=str(Exception),
        )

        error_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=(
                "**RFP Writer Error** — Failed to generate procurement document. "
                "Check logs for details."
            ),
            agent_name="rfp_writer",
            metadata={"error": True},
        )

        return {
            "rfp_document": "",
            "executive_summary": "",
            "compliance_report": {},
            "messages": [error_message],
            "current_agent": "rfp_writer",
            "error": f"RFP Writer failed: {Exception}",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "rfp_writer": execution,
            },
        }
