"""FinOps Agent — Multi-provider cost comparison and savings analysis.

The FinOps agent is the fourth stage of the orchestration pipeline.
It takes the Sizer's ``SizedWorkloadResult`` list and produces a
``CostComparison`` with per-provider cost breakdowns, reserved-instance
and spot savings opportunities, and a cheapest-provider recommendation.

### Responsibilities

1. **Aggregate costs** — Group ``SizedWorkloadResult`` by provider and
   compute ``ProviderCostBreakdown`` per provider.
2. **Identify savings** — For each provider, query reserved / spot
   pricing via ``PricingService`` to estimate RI/SP/spot savings.
3. **Cross-provider comparison** — Determine cheapest provider, savings
   vs. most expensive, and budget compliance.
4. **LLM-enhanced analysis** — Generate a narrative summary of the
   cost analysis with actionable recommendations.

### Flow

```
list[SizedWorkloadResult] (from Sizer)
      │
      ▼
┌──────────────────────────────────────────┐
│  Group by provider                       │
│    ├─ Sum costs by category              │
│    ├─ Query RI/spot prices               │
│    └─ Build ProviderCostBreakdown        │
└──────────────────┬───────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────┐
│  Build CostComparison                    │
│    ├─ Cheapest provider                  │
│    ├─ Savings vs. most expensive         │
│    └─ Budget check                       │
└──────────────────────────────────────────┘
```

### Usage

```python
from src.agents.finops import run_finops_node

# state already has sized_results populated by sizer
state = await run_finops_node(state, llm, pricing_service)
# state['cost_comparison'] now contains the full cost analysis
# state['savings_opportunities'] has RI/spot savings appended
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

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.conversation import ChatMessage, MessageRole
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.models.recommendation import CostComparison, ProviderCostBreakdown
from src.models.workload import WorkloadRequest
from src.orchestrator.state import (
    AgentExecution,
    AgentStatus,
    OrchestratorState,
    SizedWorkloadResult,
)
from src.services.pricing_service import PricingService

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Category → cost bucket mapping
# ---------------------------------------------------------------------------

_CATEGORY_TO_COST_FIELD: dict[ServiceCategory, str] = {
    ServiceCategory.COMPUTE: "compute_monthly_usd",
    ServiceCategory.AI_ML: "compute_monthly_usd",
    ServiceCategory.SERVERLESS_COMPUTE: "serverless_monthly_usd",
    ServiceCategory.SERVERLESS_FUNCTION: "serverless_monthly_usd",
    ServiceCategory.CONTAINER: "kubernetes_monthly_usd",
    ServiceCategory.DATABASE: "database_monthly_usd",
    ServiceCategory.STORAGE: "storage_monthly_usd",
    ServiceCategory.NETWORKING: "networking_monthly_usd",
    ServiceCategory.ANALYTICS: "compute_monthly_usd",
    ServiceCategory.MANAGEMENT: "other_monthly_usd",
    ServiceCategory.SECURITY: "other_monthly_usd",
    ServiceCategory.INTEGRATION: "other_monthly_usd",
    ServiceCategory.IOT: "other_monthly_usd",
    ServiceCategory.OTHER: "other_monthly_usd",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _group_results_by_provider(
    results: list[SizedWorkloadResult],
) -> dict[CloudProvider, list[SizedWorkloadResult]]:
    """Group sizing results by cloud provider.

    Args:
        results: All sizing results from the Sizer agent.

    Returns:
        Dict mapping provider → list of results.
    """
    grouped: dict[CloudProvider, list[SizedWorkloadResult]] = {}
    for r in results:
        grouped.setdefault(r.provider, []).append(r)
    return grouped


def _resolve_category_for_result(
    result: SizedWorkloadResult,
    workload_components: dict[str, ServiceCategory],
) -> ServiceCategory:
    """Determine the service category for a sized result.

    Uses the profiler's resolved category if available.

    Args:
        result: A single sizing result.
        workload_components: workload_name → resolved_category mapping.

    Returns:
        The ServiceCategory for this result.
    """
    return workload_components.get(
        result.workload_name, ServiceCategory.COMPUTE
    )


@observe()
async def _build_provider_breakdown(
    provider: CloudProvider,
    results: list[SizedWorkloadResult],
    workload_components: dict[str, ServiceCategory],
    pricing_service: PricingService,
    workload_request: WorkloadRequest,
) -> tuple[ProviderCostBreakdown, list[dict[str, Any]]]:
    """Build a cost breakdown for a single provider.

    Aggregates costs by category, queries RI/spot pricing for
    savings estimates.

    Args:
        provider: The cloud provider.
        results: Sizing results for this provider.
        workload_components: workload_name → category mapping.
        pricing_service: For querying alternative pricing tiers.
        workload_request: For region info.

    Returns:
        Tuple of (ProviderCostBreakdown, list of savings opportunities).
    """
    log = logger.bind(
        agent="finops",
        step="build_breakdown",
        provider=provider.value,
    )
    log.info("breakdown_started", result_count=len(results))

    # Aggregate costs by category bucket
    cost_buckets: dict[str, float] = {
        "compute_monthly_usd": 0.0,
        "database_monthly_usd": 0.0,
        "storage_monthly_usd": 0.0,
        "kubernetes_monthly_usd": 0.0,
        "networking_monthly_usd": 0.0,
        "serverless_monthly_usd": 0.0,
        "other_monthly_usd": 0.0,
    }

    selected_skus: list[NormalizedPriceItem] = []

    for r in results:
        category = _resolve_category_for_result(r, workload_components)
        cost_field = _CATEGORY_TO_COST_FIELD.get(category, "other_monthly_usd")
        cost_buckets[cost_field] += r.monthly_cost_usd

        if r.selected_sku is not None:
            selected_skus.append(r.selected_sku)

    total_on_demand = sum(cost_buckets.values())

    # ── Query RI / spot savings ───────────────────────────────
    savings: list[dict[str, Any]] = []
    reserved_1yr_total = 0.0
    reserved_3yr_total = 0.0
    spot_total = 0.0
    ri_1yr_queries = 0
    ri_3yr_queries = 0
    spot_queries = 0

    region = workload_request.provider_regions.get(
        provider.value,
        workload_request.preferred_region,
    )

    for r in results:
        if r.selected_sku is None:
            continue

        sku_name = r.selected_sku.sku_name
        on_demand_cost = r.monthly_cost_usd

        # Attempt reserved 1yr pricing
        try:
            ri_1yr_items = await pricing_service.search_prices(
                provider,
                sku_name=sku_name,
                region=region,
                pricing_tier=PricingTier.RESERVED_1YR,
                max_results=5,
            )
            ri_1yr_queries += 1

            if ri_1yr_items:
                best_ri = min(
                    ri_1yr_items,
                    key=lambda x: x.monthly_cost_estimate or float("inf"),
                )
                ri_monthly = best_ri.monthly_cost_estimate
                if ri_monthly is not None and ri_monthly > 0:
                    reserved_1yr_total += ri_monthly
                    if on_demand_cost > 0 and ri_monthly < on_demand_cost:
                        pct = round(
                            (1 - ri_monthly / on_demand_cost) * 100, 1,
                        )
                        savings.append({
                            "type": "reserved_1yr",
                            "provider": provider.value,
                            "workload": r.workload_name,
                            "sku": sku_name,
                            "on_demand_monthly": round(on_demand_cost, 2),
                            "reserved_monthly": round(ri_monthly, 2),
                            "savings_pct": pct,
                        })
                else:
                    reserved_1yr_total += on_demand_cost
            else:
                reserved_1yr_total += on_demand_cost
        except Exception:
            log.debug("ri_1yr_query_failed", sku=sku_name, exc_info=True)
            reserved_1yr_total += on_demand_cost

        # Attempt reserved 3yr pricing
        try:
            ri_3yr_items = await pricing_service.search_prices(
                provider,
                sku_name=sku_name,
                region=region,
                pricing_tier=PricingTier.RESERVED_3YR,
                max_results=5,
            )
            ri_3yr_queries += 1

            if ri_3yr_items:
                best_ri3 = min(
                    ri_3yr_items,
                    key=lambda x: x.monthly_cost_estimate or float("inf"),
                )
                ri3_monthly = best_ri3.monthly_cost_estimate
                if ri3_monthly is not None and ri3_monthly > 0:
                    reserved_3yr_total += ri3_monthly
                    if on_demand_cost > 0 and ri3_monthly < on_demand_cost:
                        pct = round(
                            (1 - ri3_monthly / on_demand_cost) * 100, 1,
                        )
                        savings.append({
                            "type": "reserved_3yr",
                            "provider": provider.value,
                            "workload": r.workload_name,
                            "sku": sku_name,
                            "on_demand_monthly": round(on_demand_cost, 2),
                            "reserved_monthly": round(ri3_monthly, 2),
                            "savings_pct": pct,
                        })
                else:
                    reserved_3yr_total += on_demand_cost
            else:
                reserved_3yr_total += on_demand_cost
        except Exception:
            log.debug("ri_3yr_query_failed", sku=sku_name, exc_info=True)
            reserved_3yr_total += on_demand_cost

        # Attempt spot pricing
        try:
            spot_items = await pricing_service.search_prices(
                provider,
                sku_name=sku_name,
                region=region,
                pricing_tier=PricingTier.SPOT,
                max_results=5,
            )
            spot_queries += 1

            if spot_items:
                best_spot = min(
                    spot_items,
                    key=lambda x: x.monthly_cost_estimate or float("inf"),
                )
                spot_monthly = best_spot.monthly_cost_estimate
                if spot_monthly is not None and spot_monthly > 0:
                    spot_total += spot_monthly
                    if on_demand_cost > 0 and spot_monthly < on_demand_cost:
                        pct = round(
                            (1 - spot_monthly / on_demand_cost) * 100, 1,
                        )
                        savings.append({
                            "type": "spot",
                            "provider": provider.value,
                            "workload": r.workload_name,
                            "sku": sku_name,
                            "on_demand_monthly": round(on_demand_cost, 2),
                            "spot_monthly": round(spot_monthly, 2),
                            "savings_pct": pct,
                        })
                else:
                    spot_total += on_demand_cost
            else:
                spot_total += on_demand_cost
        except Exception:
            log.debug("spot_query_failed", sku=sku_name, exc_info=True)
            spot_total += on_demand_cost

    # Compute savings percentages
    ri_1yr_savings_pct = None
    ri_1yr_monthly = None
    if total_on_demand > 0 and reserved_1yr_total < total_on_demand:
        ri_1yr_monthly = round(reserved_1yr_total, 2)
        ri_1yr_savings_pct = round(
            (1 - reserved_1yr_total / total_on_demand) * 100, 1,
        )

    ri_3yr_savings_pct = None
    ri_3yr_monthly = None
    if total_on_demand > 0 and reserved_3yr_total < total_on_demand:
        ri_3yr_monthly = round(reserved_3yr_total, 2)
        ri_3yr_savings_pct = round(
            (1 - reserved_3yr_total / total_on_demand) * 100, 1,
        )

    spot_savings_pct = None
    spot_monthly_out = None
    if total_on_demand > 0 and spot_total < total_on_demand:
        spot_monthly_out = round(spot_total, 2)
        spot_savings_pct = round(
            (1 - spot_total / total_on_demand) * 100, 1,
        )

    breakdown = ProviderCostBreakdown(
        provider=provider,
        compute_monthly_usd=round(cost_buckets["compute_monthly_usd"], 2),
        database_monthly_usd=round(cost_buckets["database_monthly_usd"], 2),
        storage_monthly_usd=round(cost_buckets["storage_monthly_usd"], 2),
        kubernetes_monthly_usd=round(cost_buckets["kubernetes_monthly_usd"], 2),
        networking_monthly_usd=round(cost_buckets["networking_monthly_usd"], 2),
        serverless_monthly_usd=round(cost_buckets["serverless_monthly_usd"], 2),
        other_monthly_usd=round(cost_buckets["other_monthly_usd"], 2),
        total_monthly_usd=round(total_on_demand, 2),
        total_annual_usd=round(total_on_demand * 12, 2),
        reserved_1yr_monthly_usd=ri_1yr_monthly,
        reserved_1yr_savings_pct=ri_1yr_savings_pct,
        reserved_3yr_monthly_usd=ri_3yr_monthly,
        reserved_3yr_savings_pct=ri_3yr_savings_pct,
        spot_monthly_usd=spot_monthly_out,
        spot_savings_pct=spot_savings_pct,
        selected_skus=selected_skus,
    )

    log.info(
        "breakdown_completed",
        total_monthly=round(total_on_demand, 2),
        ri_1yr_savings_pct=ri_1yr_savings_pct,
        ri_3yr_savings_pct=ri_3yr_savings_pct,
        spot_savings_pct=spot_savings_pct,
        ri_queries=ri_1yr_queries + ri_3yr_queries,
        spot_queries=spot_queries,
    )

    return breakdown, savings


# ---------------------------------------------------------------------------
# LLM-assisted analysis summary
# ---------------------------------------------------------------------------

_FINOPS_SYSTEM_PROMPT = """\
You are a FinOps (Cloud Financial Operations) expert. Given a multi-provider \
cost comparison, provide a concise analysis that:

1. Recommends the best provider with clear justification
2. Highlights the largest cost drivers
3. Identifies the most impactful savings opportunities (RI, spot)
4. Notes any budget concerns

Respond in 4-6 sentences. Be specific about dollar amounts and percentages.
"""


@observe()
async def _generate_finops_summary(
    llm: BaseChatModel,
    comparison: CostComparison,
    savings: list[dict[str, Any]],
) -> str:
    """Use the LLM to generate a FinOps analysis summary.

    Args:
        llm: The LLM model (BaseChatModel interface).
        comparison: The cost comparison result.
        savings: All identified savings opportunities.

    Returns:
        LLM-generated analysis string.
    """
    log = logger.bind(agent="finops", step="generate_summary")
    log.debug("summary_generation_started")

    provider_summaries = []
    for pb in comparison.providers:
        provider_summaries.append({
            "provider": pb.provider.value,
            "total_monthly": pb.total_monthly_usd,
            "compute": pb.compute_monthly_usd,
            "database": pb.database_monthly_usd,
            "storage": pb.storage_monthly_usd,
            "kubernetes": pb.kubernetes_monthly_usd,
            "ri_1yr_savings_pct": pb.reserved_1yr_savings_pct,
            "ri_3yr_savings_pct": pb.reserved_3yr_savings_pct,
            "spot_savings_pct": pb.spot_savings_pct,
        })

    user_content = json.dumps({
        "cheapest_provider": (
            comparison.cheapest_provider.value
            if comparison.cheapest_provider else "N/A"
        ),
        "savings_vs_most_expensive_pct": comparison.savings_vs_most_expensive_pct,
        "budget_monthly_usd": comparison.budget_monthly_usd,
        "budget_exceeded": comparison.budget_exceeded,
        "providers": provider_summaries,
        "top_savings": savings[:10],
    }, indent=2)

    messages = [
        SystemMessage(content=_FINOPS_SYSTEM_PROMPT),
        HumanMessage(content=f"Analyze this cost comparison:\n\n{user_content}"),
    ]

    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, "content") else str(response)
        log.debug("summary_generation_completed", length=len(content))
        return content[:1000]
    except Exception:
        log.warning("summary_generation_failed", exc_info=True)
        # Heuristic fallback
        parts = []
        if comparison.cheapest_provider:
            cheapest_bd = next(
                (p for p in comparison.providers
                 if p.provider == comparison.cheapest_provider),
                None,
            )
            if cheapest_bd:
                parts.append(
                    f"{comparison.cheapest_provider.value.upper()} is the "
                    f"cheapest at ${cheapest_bd.total_monthly_usd:.2f}/mo."
                )
        if comparison.savings_vs_most_expensive_pct > 0:
            parts.append(
                f"Savings vs. most expensive: "
                f"{comparison.savings_vs_most_expensive_pct:.1f}%."
            )
        if comparison.budget_exceeded:
            parts.append("WARNING: All providers exceed the stated budget.")
        if savings:
            best_saving = max(savings, key=lambda s: s.get("savings_pct", 0))
            parts.append(
                f"Best savings opportunity: {best_saving['type']} on "
                f"{best_saving.get('sku', 'N/A')} "
                f"({best_saving.get('savings_pct', 0):.1f}% savings)."
            )
        return " ".join(parts) if parts else "Cost analysis completed."


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------


@observe()
async def run_finops_node(
    state: OrchestratorState,
    llm: BaseChatModel,
    pricing_service: PricingService,
) -> OrchestratorState:
    """Execute the FinOps agent — compare costs across providers.

    This is a LangGraph node function.  It reads
    ``state['sized_results']`` and produces ``state['cost_comparison']``
    with per-provider breakdowns and savings analysis.

    Args:
        state: Current ``OrchestratorState`` (TypedDict).
        llm: LLM instance for generating analysis summaries
            (``BaseChatModel`` interface).
        pricing_service: Initialised ``PricingService`` with providers
            registered.

    Returns:
        Updated ``OrchestratorState`` with ``cost_comparison``,
        ``recommended_provider``, ``savings_opportunities``, and
        a summary message appended to ``messages``.

    Raises:
        ValueError: If ``sized_results`` or ``workload_request``
            is missing from state.
    """
    log = logger.bind(
        agent="finops",
        request_id=state.get("request_id", "unknown"),
    )

    start_time = datetime.now(timezone.utc)
    log.info("finops_node_started")

    try:
        # ── Validate inputs ───────────────────────────────────
        sized_results: list[SizedWorkloadResult] = state.get("sized_results", [])
        if not sized_results:
            raise ValueError(
                "sized_results is empty — Sizer must run first and produce results"
            )

        workload_request: WorkloadRequest | None = state.get("workload_request")
        if workload_request is None:
            raise ValueError(
                "workload_request is missing from state — Clarifier must run first"
            )

        # Build workload_name → category lookup from profiler output
        workload_profile = state.get("workload_profile")
        workload_components: dict[str, ServiceCategory] = {}
        if workload_profile and hasattr(workload_profile, "components"):
            for comp in workload_profile.components:
                workload_components[comp.workload_name] = comp.resolved_category

        log.info(
            "analyzing_costs",
            result_count=len(sized_results),
            providers=list({r.provider.value for r in sized_results}),
        )

        # ── Group results by provider ─────────────────────────
        grouped = _group_results_by_provider(sized_results)

        # ── Build breakdowns per provider ─────────────────────
        provider_breakdowns: list[ProviderCostBreakdown] = []
        all_savings: list[dict[str, Any]] = []

        for provider, results in grouped.items():
            breakdown, savings = await _build_provider_breakdown(
                provider,
                results,
                workload_components,
                pricing_service,
                workload_request,
            )
            provider_breakdowns.append(breakdown)
            all_savings.extend(savings)

        # ── Determine cheapest provider ───────────────────────
        cheapest: ProviderCostBreakdown | None = None
        most_expensive: ProviderCostBreakdown | None = None

        if provider_breakdowns:
            sorted_providers = sorted(
                provider_breakdowns, key=lambda p: p.total_monthly_usd,
            )
            cheapest = sorted_providers[0]
            most_expensive = sorted_providers[-1]

        savings_vs_expensive = 0.0
        if cheapest and most_expensive and most_expensive.total_monthly_usd > 0:
            savings_vs_expensive = round(
                (1 - cheapest.total_monthly_usd / most_expensive.total_monthly_usd)
                * 100,
                1,
            )

        # ── Budget check ──────────────────────────────────────
        budget = workload_request.budget_monthly_usd
        budget_exceeded = False
        if budget is not None and cheapest is not None:
            budget_exceeded = cheapest.total_monthly_usd > budget

        # ── Assemble CostComparison ───────────────────────────
        comparison = CostComparison(
            providers=provider_breakdowns,
            cheapest_provider=cheapest.provider if cheapest else None,
            savings_vs_most_expensive_pct=savings_vs_expensive,
            budget_monthly_usd=budget,
            budget_exceeded=budget_exceeded,
        )

        log.info(
            "cost_comparison_assembled",
            cheapest=cheapest.provider.value if cheapest else "N/A",
            cheapest_cost=cheapest.total_monthly_usd if cheapest else 0.0,
            savings_vs_expensive_pct=savings_vs_expensive,
            budget_exceeded=budget_exceeded,
            savings_opportunities=len(all_savings),
        )

        # ── Generate LLM summary ─────────────────────────────
        finops_summary = await _generate_finops_summary(
            llm, comparison, all_savings,
        )

        # ── Build summary message ─────────────────────────────
        provider_lines = "\n".join(
            f"  • **{pb.provider.value.upper()}**: "
            f"${pb.total_monthly_usd:.2f}/mo "
            f"(${pb.total_annual_usd:.2f}/yr)"
            + (
                f" | RI-1yr: {pb.reserved_1yr_savings_pct:.0f}% savings"
                if pb.reserved_1yr_savings_pct else ""
            )
            + (
                f" | Spot: {pb.spot_savings_pct:.0f}% savings"
                if pb.spot_savings_pct else ""
            )
            for pb in sorted(provider_breakdowns, key=lambda p: p.total_monthly_usd)
        )

        budget_line = ""
        if budget is not None:
            status = "EXCEEDED" if budget_exceeded else "within budget"
            budget_line = f"\n**Budget**: ${budget:.2f}/mo — {status}"

        summary_content = (
            f"**FinOps Analysis Complete** — "
            f"{len(provider_breakdowns)} provider(s) analyzed:\n"
            f"{provider_lines}{budget_line}\n\n"
            f"**Recommended**: "
            f"{cheapest.provider.value.upper() if cheapest else 'N/A'}"
            f" (saves {savings_vs_expensive:.1f}% vs. most expensive)\n\n"
            f"{finops_summary}"
        )

        summary_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=summary_content,
            agent_name="finops",
            metadata={
                "cheapest_provider": (
                    cheapest.provider.value if cheapest else None
                ),
                "savings_vs_expensive_pct": savings_vs_expensive,
                "budget_exceeded": budget_exceeded,
                "provider_count": len(provider_breakdowns),
            },
        )

        # ── Compute timing ────────────────────────────────────
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        execution = AgentExecution(
            agent_name="finops",
            status=AgentStatus.COMPLETED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
        )

        log.info(
            "finops_node_completed",
            duration_ms=round(duration_ms, 1),
            cheapest_provider=cheapest.provider.value if cheapest else "N/A",
        )

        # ── Return state update ───────────────────────────────
        return {
            "cost_comparison": comparison.model_dump(),
            "recommended_provider": (
                cheapest.provider.value if cheapest else None
            ),
            "savings_opportunities": all_savings,
            "messages": [summary_message],
            "current_agent": "finops",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "finops": execution,
            },
        }

    except Exception:
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        log.error("finops_node_failed", exc_info=True, duration_ms=round(duration_ms, 1))

        execution = AgentExecution(
            agent_name="finops",
            status=AgentStatus.FAILED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
            error_message=str(Exception),
        )

        error_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="**FinOps Error** — Failed to complete cost analysis. Check logs for details.",
            agent_name="finops",
            metadata={"error": True},
        )

        return {
            "cost_comparison": {},
            "savings_opportunities": [],
            "messages": [error_message],
            "current_agent": "finops",
            "error": f"FinOps failed: {Exception}",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "finops": execution,
            },
        }
