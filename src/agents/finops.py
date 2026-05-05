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
    ServiceCategory.KUBERNETES: "kubernetes_monthly_usd",
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
# Known discount rates (used as fallback when live RI/spot data is absent)
# ---------------------------------------------------------------------------

#: Provider → (ri_1yr_pct, ri_3yr_pct).  Industry-standard discounts.
_RI_DISCOUNT_RATES: dict[str, tuple[float, float]] = {
    "aws": (30.0, 45.0),    # AWS Reserved Instances
    "azure": (35.0, 50.0),  # Azure Reserved VM Instances
    "gcp": (25.0, 40.0),    # GCP Committed Use Discounts
}

#: Provider → spot savings percentage (typical, not guaranteed).
_SPOT_DISCOUNT_RATES: dict[str, float] = {
    "aws": 70.0,
    "azure": 80.0,
    "gcp": 60.0,
}

#: Categories NOT eligible for spot/preemptible pricing (stateful).
_SPOT_INELIGIBLE: set[ServiceCategory] = {
    ServiceCategory.DATABASE,
    ServiceCategory.STORAGE,
    ServiceCategory.NETWORKING,
    ServiceCategory.MANAGEMENT,
    ServiceCategory.SECURITY,
    ServiceCategory.KUBERNETES,   # managed control-plane fee — always fixed cost
}

#: Names that indicate fixed-cost line items (no discounting).
_FIXED_COST_PREFIXES = ("[Infra]", "K8s Cluster", "Load Balancer")


# ---------------------------------------------------------------------------
# TCO projection helper
# ---------------------------------------------------------------------------


def _compute_tco(
    monthly_usd: float,
    years: int,
    annual_growth_pct: float = 0.0,
) -> float:
    """Compute total cost of ownership over N years with compound growth.

    Args:
        monthly_usd: Current monthly cost (USD).
        years: Number of years to project.
        annual_growth_pct: Annual cost growth rate (0-100).

    Returns:
        Total USD over the projection period.
    """
    if annual_growth_pct == 0.0:
        return round(monthly_usd * 12 * years, 2)

    g = 1 + annual_growth_pct / 100.0
    total = 0.0
    for yr in range(years):
        total += monthly_usd * 12 * (g ** yr)
    return round(total, 2)


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
    ``[Infra]`` prefixed workloads (NAT Gateway, Data Transfer, etc.)
    added by the Sizer are always bucketed as NETWORKING.

    Args:
        result: A single sizing result.
        workload_components: workload_name → resolved_category mapping.

    Returns:
        The ServiceCategory for this result.
    """
    # Ancillary infrastructure line items injected by the Sizer
    if result.workload_name.startswith("[Infra]"):
        return ServiceCategory.NETWORKING

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
    savings estimates.  When live RI/spot prices are unavailable,
    applies industry-standard discount rate fallbacks so the savings
    table is always populated.

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

    # ── RI / spot savings analysis ────────────────────────────
    savings: list[dict[str, Any]] = []

    # Compute what portion of total cost is discount-eligible
    # Fixed-cost line items (K8s fee, LB, NAT, data transfer) cannot be discounted
    discountable_cost = sum(
        r.monthly_cost_usd
        for r in results
        if not any(r.workload_name.startswith(pfx) for pfx in _FIXED_COST_PREFIXES)
        and r.selected_sku is not None  # has an actual SKU to commit to
    )
    spot_eligible_cost = sum(
        r.monthly_cost_usd
        for r in results
        if not any(r.workload_name.startswith(pfx) for pfx in _FIXED_COST_PREFIXES)
        and _resolve_category_for_result(r, workload_components)
           not in _SPOT_INELIGIBLE
        and r.selected_sku is not None
    )
    non_discountable_cost = total_on_demand - discountable_cost

    ri_1yr_rate, ri_3yr_rate = _RI_DISCOUNT_RATES.get(
        provider.value, (25.0, 40.0),
    )
    spot_rate = _SPOT_DISCOUNT_RATES.get(provider.value, 60.0)

    # Attempt live RI pricing per SKU; fall back to rate when unavailable
    region = workload_request.provider_regions.get(
        provider.value,
        workload_request.preferred_region,
    )

    live_ri_1yr_total: float = 0.0
    live_ri_3yr_total: float = 0.0
    live_spot_total: float = 0.0
    live_1yr_found = False
    live_3yr_found = False
    live_spot_found = False

    for r in results:
        if r.selected_sku is None:
            continue
        if any(r.workload_name.startswith(pfx) for pfx in _FIXED_COST_PREFIXES):
            # Fixed costs go through unchanged for all tiers
            live_ri_1yr_total += r.monthly_cost_usd
            live_ri_3yr_total += r.monthly_cost_usd
            live_spot_total += r.monthly_cost_usd
            continue

        sku_name = r.selected_sku.sku_name
        on_demand_cost = r.monthly_cost_usd
        category = _resolve_category_for_result(r, workload_components)

        # -- RI 1yr --
        try:
            ri_1yr_items = await pricing_service.search_prices(
                provider,
                service_name=r.selected_sku.service_name,
                service_category=r.selected_sku.service_category,
                region=region,
                pricing_tier=PricingTier.RESERVED_1YR,
                max_results=10,
            )
            if ri_1yr_items:
                # Find closest match by vCPU/memory attributes
                best_ri = min(
                    ri_1yr_items,
                    key=lambda x: x.monthly_cost_estimate or float("inf"),
                )
                ri_monthly = best_ri.monthly_cost_estimate
                if ri_monthly is not None and 0 < ri_monthly < on_demand_cost:
                    live_ri_1yr_total += ri_monthly
                    live_1yr_found = True
                    pct = round((1 - ri_monthly / on_demand_cost) * 100, 1)
                    savings.append({
                        "type": "reserved_1yr",
                        "provider": provider.value,
                        "workload": r.workload_name,
                        "sku": sku_name,
                        "on_demand_monthly": round(on_demand_cost, 2),
                        "reserved_monthly": round(ri_monthly, 2),
                        "savings_pct": pct,
                        "source": "live",
                    })
                    continue
            # No live data → fall through to rate-based estimate
            live_ri_1yr_total += round(on_demand_cost * (1 - ri_1yr_rate / 100.0), 2)
        except Exception:
            log.debug("ri_1yr_query_failed", sku=sku_name, exc_info=True)
            live_ri_1yr_total += round(on_demand_cost * (1 - ri_1yr_rate / 100.0), 2)

        # -- RI 3yr --
        try:
            ri_3yr_items = await pricing_service.search_prices(
                provider,
                service_name=r.selected_sku.service_name,
                service_category=r.selected_sku.service_category,
                region=region,
                pricing_tier=PricingTier.RESERVED_3YR,
                max_results=10,
            )
            if ri_3yr_items:
                best_ri3 = min(
                    ri_3yr_items,
                    key=lambda x: x.monthly_cost_estimate or float("inf"),
                )
                ri3_monthly = best_ri3.monthly_cost_estimate
                if ri3_monthly is not None and 0 < ri3_monthly < on_demand_cost:
                    live_ri_3yr_total += ri3_monthly
                    live_3yr_found = True
                    pct = round((1 - ri3_monthly / on_demand_cost) * 100, 1)
                    savings.append({
                        "type": "reserved_3yr",
                        "provider": provider.value,
                        "workload": r.workload_name,
                        "sku": sku_name,
                        "on_demand_monthly": round(on_demand_cost, 2),
                        "reserved_monthly": round(ri3_monthly, 2),
                        "savings_pct": pct,
                        "source": "live",
                    })
                    continue
            live_ri_3yr_total += round(on_demand_cost * (1 - ri_3yr_rate / 100.0), 2)
        except Exception:
            log.debug("ri_3yr_query_failed", sku=sku_name, exc_info=True)
            live_ri_3yr_total += round(on_demand_cost * (1 - ri_3yr_rate / 100.0), 2)

        # -- Spot (only eligible categories) --
        if category in _SPOT_INELIGIBLE:
            live_spot_total += on_demand_cost  # no discount for stateful
            continue

        try:
            spot_items = await pricing_service.search_prices(
                provider,
                service_name=r.selected_sku.service_name,
                service_category=r.selected_sku.service_category,
                region=region,
                pricing_tier=PricingTier.SPOT,
                max_results=10,
            )
            if spot_items:
                best_spot = min(
                    spot_items,
                    key=lambda x: x.monthly_cost_estimate or float("inf"),
                )
                spot_monthly = best_spot.monthly_cost_estimate
                if spot_monthly is not None and 0 < spot_monthly < on_demand_cost:
                    live_spot_total += spot_monthly
                    live_spot_found = True
                    pct = round((1 - spot_monthly / on_demand_cost) * 100, 1)
                    savings.append({
                        "type": "spot",
                        "provider": provider.value,
                        "workload": r.workload_name,
                        "sku": sku_name,
                        "on_demand_monthly": round(on_demand_cost, 2),
                        "spot_monthly": round(spot_monthly, 2),
                        "savings_pct": pct,
                        "source": "live",
                    })
                    continue
            live_spot_total += round(on_demand_cost * (1 - spot_rate / 100.0), 2)
        except Exception:
            log.debug("spot_query_failed", sku=sku_name, exc_info=True)
            live_spot_total += round(on_demand_cost * (1 - spot_rate / 100.0), 2)

    # Add bucket-level savings entries (rate-based) when no live data found
    if not live_1yr_found and discountable_cost > 0:
        ri_1yr_discounted = round(discountable_cost * (1 - ri_1yr_rate / 100.0), 2)
        savings.append({
            "type": "reserved_1yr",
            "provider": provider.value,
            "workload": "(all discountable workloads)",
            "sku": "portfolio",
            "on_demand_monthly": round(discountable_cost, 2),
            "reserved_monthly": ri_1yr_discounted,
            "savings_pct": ri_1yr_rate,
            "source": "estimate",
        })

    if not live_3yr_found and discountable_cost > 0:
        ri_3yr_discounted = round(discountable_cost * (1 - ri_3yr_rate / 100.0), 2)
        savings.append({
            "type": "reserved_3yr",
            "provider": provider.value,
            "workload": "(all discountable workloads)",
            "sku": "portfolio",
            "on_demand_monthly": round(discountable_cost, 2),
            "reserved_monthly": ri_3yr_discounted,
            "savings_pct": ri_3yr_rate,
            "source": "estimate",
        })

    if not live_spot_found and spot_eligible_cost > 0:
        spot_discounted = round(spot_eligible_cost * (1 - spot_rate / 100.0), 2)
        savings.append({
            "type": "spot",
            "provider": provider.value,
            "workload": "(eligible stateless workloads only)",
            "sku": "portfolio",
            "on_demand_monthly": round(spot_eligible_cost, 2),
            "spot_monthly": spot_discounted,
            "savings_pct": spot_rate,
            "source": "estimate",
            "warning": "Spot instances are interruptible — suitable only for fault-tolerant workloads",
        })

    # Compute aggregate savings percentages
    ri_1yr_monthly: float | None = None
    ri_1yr_savings_pct: float | None = None
    if total_on_demand > 0 and live_ri_1yr_total < total_on_demand:
        ri_1yr_monthly = round(live_ri_1yr_total + non_discountable_cost, 2)
        effective_ri1 = non_discountable_cost + live_ri_1yr_total
        ri_1yr_savings_pct = round(
            (1 - effective_ri1 / total_on_demand) * 100, 1,
        ) if total_on_demand > 0 else None

    ri_3yr_monthly: float | None = None
    ri_3yr_savings_pct: float | None = None
    if total_on_demand > 0 and live_ri_3yr_total < total_on_demand:
        ri_3yr_monthly = round(live_ri_3yr_total + non_discountable_cost, 2)
        effective_ri3 = non_discountable_cost + live_ri_3yr_total
        ri_3yr_savings_pct = round(
            (1 - effective_ri3 / total_on_demand) * 100, 1,
        ) if total_on_demand > 0 else None

    spot_monthly_out: float | None = None
    spot_savings_pct: float | None = None
    if total_on_demand > 0 and spot_eligible_cost > 0:
        effective_spot = (
            total_on_demand - spot_eligible_cost
            + round(spot_eligible_cost * (1 - spot_rate / 100.0), 2)
        )
        spot_monthly_out = round(effective_spot, 2)
        spot_savings_pct = round(
            (1 - effective_spot / total_on_demand) * 100, 1,
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
        discountable_cost=round(discountable_cost, 2),
        ri_1yr_savings_pct=ri_1yr_savings_pct,
        ri_3yr_savings_pct=ri_3yr_savings_pct,
        spot_savings_pct=spot_savings_pct,
        savings_entries=len(savings),
    )

    return breakdown, savings


# ---------------------------------------------------------------------------
# LLM-assisted analysis summary
# ---------------------------------------------------------------------------

_FINOPS_SYSTEM_PROMPT = """\
You are a FinOps (Cloud Financial Operations) expert. Given a multi-provider \
cost comparison with TCO projections, provide a concise analysis that:

1. Recommends the best provider with clear justification (on-demand monthly cost)
2. Highlights the largest cost drivers (compute, database, networking, kubernetes)
3. Identifies the most impactful savings opportunities:
   - Reserved instance / committed use discounts (1yr and 3yr)
   - Spot/preemptible pricing for stateless workloads
4. Summarises 3-year and 5-year Total Cost of Ownership (TCO) for the cheapest provider
5. Notes any budget concerns or whether reserved instances resolve the budget gap

Respond in 5-7 sentences. Be specific about dollar amounts and percentages. \
Use "estimate" or "~" when data is based on industry-standard rates rather than \
live pricing.
"""


@observe()
async def _generate_finops_summary(
    llm: BaseChatModel,
    comparison: CostComparison,
    savings: list[dict[str, Any]],
    growth_pct: float = 15.0,
) -> str:
    """Use the LLM to generate a FinOps analysis summary with TCO projections.

    Args:
        llm: The LLM model (BaseChatModel interface).
        comparison: The cost comparison result.
        savings: All identified savings opportunities.
        growth_pct: Annual cost growth rate assumption (default 15%).

    Returns:
        LLM-generated analysis string.
    """
    log = logger.bind(agent="finops", step="generate_summary")
    log.debug("summary_generation_started", growth_pct=growth_pct)

    provider_summaries = []
    for pb in comparison.providers:
        tco_1yr = _compute_tco(pb.total_monthly_usd, 1)
        tco_3yr = _compute_tco(pb.total_monthly_usd, 3, growth_pct)
        tco_5yr = _compute_tco(pb.total_monthly_usd, 5, growth_pct)
        provider_summaries.append({
            "provider": pb.provider.value,
            "total_monthly": pb.total_monthly_usd,
            "compute": pb.compute_monthly_usd,
            "database": pb.database_monthly_usd,
            "storage": pb.storage_monthly_usd,
            "kubernetes": pb.kubernetes_monthly_usd,
            "networking": pb.networking_monthly_usd,
            "ri_1yr_savings_pct": pb.reserved_1yr_savings_pct,
            "ri_3yr_savings_pct": pb.reserved_3yr_savings_pct,
            "spot_savings_pct": pb.spot_savings_pct,
            "tco_1yr_usd": tco_1yr,
            "tco_3yr_usd_with_growth": tco_3yr,
            "tco_5yr_usd_with_growth": tco_5yr,
        })

    user_content = json.dumps({
        "cheapest_provider": (
            comparison.cheapest_provider.value
            if comparison.cheapest_provider else "N/A"
        ),
        "savings_vs_most_expensive_pct": comparison.savings_vs_most_expensive_pct,
        "budget_monthly_usd": comparison.budget_monthly_usd,
        "budget_exceeded": comparison.budget_exceeded,
        "growth_assumption_pct_per_year": growth_pct,
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
        return content[:1500]
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
                tco_3yr = _compute_tco(cheapest_bd.total_monthly_usd, 3, growth_pct)
                tco_5yr = _compute_tco(cheapest_bd.total_monthly_usd, 5, growth_pct)
                parts.append(
                    f"{comparison.cheapest_provider.value.upper()} is the "
                    f"cheapest at ${cheapest_bd.total_monthly_usd:.2f}/mo "
                    f"(TCO: ${tco_3yr:,.0f} over 3yr, ${tco_5yr:,.0f} over 5yr "
                    f"at {growth_pct:.0f}%/yr growth)."
                )
        if comparison.savings_vs_most_expensive_pct > 0:
            parts.append(
                f"Savings vs. most expensive: "
                f"{comparison.savings_vs_most_expensive_pct:.1f}%."
            )
        if comparison.budget_exceeded:
            parts.append("WARNING: All providers exceed the stated budget.")
        ri_savings = [s for s in savings if s.get("type") == "reserved_1yr"]
        if ri_savings:
            avg_ri = sum(s.get("savings_pct", 0) for s in ri_savings) / len(ri_savings)
            parts.append(
                f"Reserved instance savings: ~{avg_ri:.0f}% with 1-year commitment."
            )
        spot_savings = [s for s in savings if s.get("type") == "spot"]
        if spot_savings:
            avg_spot = sum(s.get("savings_pct", 0) for s in spot_savings) / len(spot_savings)
            parts.append(
                f"Spot/preemptible savings: ~{avg_spot:.0f}% for fault-tolerant workloads."
            )
        return " ".join(parts) if parts else "Cost analysis completed."


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------


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
        # Use growth_rate from workload request if provided, else 15% default
        growth_pct = getattr(workload_request, "growth_rate_pct", None) or 15.0
        finops_summary = await _generate_finops_summary(
            llm, comparison, all_savings, growth_pct=growth_pct,
        )

        # ── Build TCO projections for KPI tracking ────────────
        tco_projections: dict[str, Any] = {}
        if cheapest:
            tco_projections = {
                "growth_pct_per_year": growth_pct,
                "cheapest_provider": cheapest.provider.value,
                "tco_1yr_usd": _compute_tco(cheapest.total_monthly_usd, 1),
                "tco_3yr_usd": _compute_tco(
                    cheapest.total_monthly_usd, 3, growth_pct,
                ),
                "tco_5yr_usd": _compute_tco(
                    cheapest.total_monthly_usd, 5, growth_pct,
                ),
                "ri_3yr_tco_usd": _compute_tco(
                    cheapest.reserved_3yr_monthly_usd
                    if cheapest.reserved_3yr_monthly_usd
                    else cheapest.total_monthly_usd,
                    3,
                    growth_pct,
                ),
            }
            log.info(
                "tco_computed",
                tco_1yr=tco_projections["tco_1yr_usd"],
                tco_3yr=tco_projections["tco_3yr_usd"],
                tco_5yr=tco_projections["tco_5yr_usd"],
                ri_3yr_tco=tco_projections["ri_3yr_tco_usd"],
                growth_pct=growth_pct,
            )

        # ── Build summary message ─────────────────────────────
        tco_line = ""
        if tco_projections:
            tco_line = (
                f"\n**TCO ({growth_pct:.0f}%/yr growth)**: "
                f"3yr=${tco_projections['tco_3yr_usd']:,.0f} | "
                f"5yr=${tco_projections['tco_5yr_usd']:,.0f} | "
                f"RI-3yr-3yr=${tco_projections['ri_3yr_tco_usd']:,.0f}"
            )
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
            f"{provider_lines}{budget_line}{tco_line}\n\n"
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
                "tco_projections": tco_projections,
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
        existing_kpis = state.get("kpis", {}) or {}
        return {
            "cost_comparison": comparison.model_dump(),
            "recommended_provider": (
                cheapest.provider.value if cheapest else None
            ),
            "savings_opportunities": all_savings,
            "messages": [summary_message],
            "current_agent": "finops",
            "kpis": {**existing_kpis, "tco_projections": tco_projections},
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
