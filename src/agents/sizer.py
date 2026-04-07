"""Sizer Agent — SKU selection and Kubernetes node pool sizing.

The Sizer is the third stage of the orchestration pipeline, running
after the Profiler has produced a ``WorkloadProfile``.  It maps each
``ComponentProfile`` to concrete cloud SKUs using the ``PricingService``
and the algorithmic engines (scoring, bin-packing).

### Responsibilities

1. **SKU search** — For each component, query the ``PricingService``
   to obtain candidate ``NormalizedPriceItem`` records that match the
   resolved category, region, and provider.
2. **Compute scoring** — For COMPUTE / AI_ML workloads, run the
   multi-criteria ``scoring.score_skus()`` engine to rank candidates.
3. **Container bin-packing** — For CONTAINER workloads, run the
   ``bin_packing.pack_workloads()`` engine to compute optimal node
   pool sizes.
4. **Category-aware selection** — For DATABASE, STORAGE, SERVERLESS,
   and other categories, select the best-priced SKU by filtering and
   sorting candidates.
5. **Result assembly** — Produce one ``SizedWorkloadResult`` per
   workload per provider, with fit scores, selected SKU, alternatives,
   and rationale.

### Flow

```
WorkloadProfile (from Profiler)
      │
      ▼
┌──────────────────────────────────────────┐
│  For each ComponentProfile × Provider    │
│    ├─ PricingService.search_prices()     │
│    ├─ Filter/score/bin-pack by category  │
│    └─ Build SizedWorkloadResult          │
└──────────────────┬───────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────┐
│  Append list[SizedWorkloadResult] to     │
│  state['sized_results']                  │
└──────────────────────────────────────────┘
```

### Usage

```python
from src.agents.sizer import run_sizer_node

# state already has workload_profile populated by profiler
state = await run_sizer_node(state, llm, pricing_service)
# state['sized_results'] now contains per-workload, per-provider selections
# state['messages'] has sizer summary appended
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

from src.engines import extract_memory_gb, extract_vcpus
from src.engines.bin_packing import PackingAlgorithm, pack_workloads
from src.engines.scoring import ScoredSKU, score_skus
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.conversation import ChatMessage, MessageRole
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.models.recommendation import BinPackingResult
from src.models.workload import (
    ComponentProfile,
    WorkloadProfile,
    WorkloadRequest,
    WorkloadRequirement,
)
from src.orchestrator.state import (
    AgentExecution,
    AgentStatus,
    OrchestratorState,
    SizedWorkloadResult,
)
from src.services.pricing_service import PricingService

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Category → service name mapping for PricingService queries
# ---------------------------------------------------------------------------

#: Maps (provider, category) to the service name filter used by the
#: PricingService search.  Falls back to category-only search when no
#: mapping exists.
_SERVICE_NAME_MAP: dict[tuple[str, ServiceCategory], str] = {
    # AWS
    ("aws", ServiceCategory.COMPUTE): "AmazonEC2",
    ("aws", ServiceCategory.DATABASE): "AmazonRDS",
    ("aws", ServiceCategory.STORAGE): "AmazonS3",
    ("aws", ServiceCategory.CONTAINER): "AmazonEKS",
    ("aws", ServiceCategory.AI_ML): "AmazonSageMaker",
    ("aws", ServiceCategory.SERVERLESS_FUNCTION): "AWSLambda",
    ("aws", ServiceCategory.NETWORKING): "AmazonVPC",
    ("aws", ServiceCategory.ANALYTICS): "AmazonRedshift",
    # Azure
    ("azure", ServiceCategory.COMPUTE): "Virtual Machines",
    ("azure", ServiceCategory.DATABASE): "SQL Database",
    ("azure", ServiceCategory.STORAGE): "Storage",
    ("azure", ServiceCategory.CONTAINER): "Azure Kubernetes Service",
    ("azure", ServiceCategory.AI_ML): "Azure Machine Learning",
    ("azure", ServiceCategory.SERVERLESS_FUNCTION): "Functions",
    ("azure", ServiceCategory.NETWORKING): "Virtual Network",
    ("azure", ServiceCategory.ANALYTICS): "Azure Synapse Analytics",
    # GCP
    ("gcp", ServiceCategory.COMPUTE): "Compute Engine",
    ("gcp", ServiceCategory.DATABASE): "Cloud SQL",
    ("gcp", ServiceCategory.STORAGE): "Cloud Storage",
    ("gcp", ServiceCategory.CONTAINER): "Kubernetes Engine",
    ("gcp", ServiceCategory.AI_ML): "Vertex AI",
    ("gcp", ServiceCategory.SERVERLESS_FUNCTION): "Cloud Functions",
    ("gcp", ServiceCategory.NETWORKING): "Cloud Networking",
    ("gcp", ServiceCategory.ANALYTICS): "BigQuery",
}

#: Categories that should use the multi-criteria scoring engine
_SCORED_CATEGORIES: set[ServiceCategory] = {
    ServiceCategory.COMPUTE,
    ServiceCategory.AI_ML,
}

#: Categories that should use the bin-packing engine
_BINPACKED_CATEGORIES: set[ServiceCategory] = {
    ServiceCategory.CONTAINER,
}

#: Max alternative SKUs to keep per workload
_MAX_ALTERNATIVES = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_region_for_provider(
    provider: CloudProvider,
    workload_request: WorkloadRequest,
    workload: WorkloadRequirement | None = None,
) -> str:
    """Resolve the region string for a provider.

    Priority: workload region_affinity > provider_regions map > preferred_region.

    Args:
        provider: The target cloud provider.
        workload_request: Top-level request with region info.
        workload: Optional specific workload (may have region_affinity).

    Returns:
        Provider-native region string.
    """
    if workload and workload.region_affinity:
        return workload.region_affinity

    region = workload_request.provider_regions.get(
        provider.value,
        workload_request.preferred_region,
    )
    return region


def _build_workload_requirement_for_component(
    component: ComponentProfile,
    original_workload: WorkloadRequirement,
) -> WorkloadRequirement:
    """Reconstruct a WorkloadRequirement from profiler output.

    The scoring engine needs ``WorkloadRequirement`` objects.  We
    merge the profiler's enriched estimates back onto the original
    workload's resource spec.

    Args:
        component: Profiler's component profile.
        original_workload: The original workload requirement.

    Returns:
        A WorkloadRequirement with profiler-enriched resource values.
    """
    from src.models.workload import ResourceSpec

    enriched_resources = original_workload.resources.model_copy(
        update={
            "vcpus": component.estimated_vcpus or original_workload.resources.vcpus,
            "memory_gb": component.estimated_memory_gb or original_workload.resources.memory_gb,
            "storage_gb": component.estimated_storage_gb or original_workload.resources.storage_gb,
            "gpu_count": (
                max(original_workload.resources.gpu_count, 1)
                if component.requires_gpu
                else original_workload.resources.gpu_count
            ),
        },
    )

    return original_workload.model_copy(
        update={
            "resources": enriched_resources,
            "suggested_category": component.resolved_category,
        },
    )


def _select_best_by_price(
    candidates: list[NormalizedPriceItem],
) -> tuple[NormalizedPriceItem | None, list[NormalizedPriceItem]]:
    """Select the cheapest SKU from a list and return alternatives.

    Filters to items with a computable monthly cost, sorts ascending.

    Args:
        candidates: List of candidate SKUs.

    Returns:
        Tuple of (best_sku, alternatives).
    """
    with_cost = [
        c for c in candidates
        if c.monthly_cost_estimate is not None and c.monthly_cost_estimate > 0
    ]
    if not with_cost:
        return (None, [])

    sorted_items = sorted(with_cost, key=lambda c: c.monthly_cost_estimate or 0.0)
    best = sorted_items[0]
    alternatives = sorted_items[1: 1 + _MAX_ALTERNATIVES]
    return (best, alternatives)


# ---------------------------------------------------------------------------
# Sizing strategies per category
# ---------------------------------------------------------------------------


@observe()
async def _size_compute_workload(
    component: ComponentProfile,
    original_workload: WorkloadRequirement,
    candidates: list[NormalizedPriceItem],
    provider: CloudProvider,
) -> SizedWorkloadResult:
    """Size a COMPUTE or AI_ML workload using the scoring engine.

    Args:
        component: Profiler's analysis.
        original_workload: Original workload requirement.
        candidates: Candidate SKUs from PricingService.
        provider: Target cloud provider.

    Returns:
        SizedWorkloadResult with scored best-fit SKU.
    """
    log = logger.bind(
        agent="sizer",
        step="size_compute",
        workload=component.workload_name,
        provider=provider.value,
    )
    log.info("scoring_started", candidate_count=len(candidates))

    # Build a WorkloadRequirement with profiler-enriched values
    enriched_wl = _build_workload_requirement_for_component(component, original_workload)

    scored: list[ScoredSKU] = score_skus(enriched_wl, candidates)

    if not scored:
        log.warning("no_eligible_skus_after_scoring")
        # Fall back to cheapest
        best, alts = _select_best_by_price(candidates)
        return SizedWorkloadResult(
            workload_name=component.workload_name,
            provider=provider,
            selected_sku=best,
            alternative_skus=alts,
            monthly_cost_usd=(best.monthly_cost_estimate or 0.0) if best else 0.0,
            fit_score=0.3,
            rationale=(
                f"No SKU passed scoring filters for {component.workload_name}. "
                f"Selected cheapest available option."
            ),
        )

    best_scored = scored[0]
    alternatives = [s.sku for s in scored[1: 1 + _MAX_ALTERNATIVES]]

    monthly = best_scored.sku.monthly_cost_estimate or 0.0

    log.info(
        "scoring_completed",
        best_sku=best_scored.sku.sku_name,
        fit_score=round(best_scored.total_score, 4),
        monthly_cost=round(monthly, 2),
        eligible_count=len(scored),
    )

    return SizedWorkloadResult(
        workload_name=component.workload_name,
        provider=provider,
        selected_sku=best_scored.sku,
        alternative_skus=alternatives,
        monthly_cost_usd=round(monthly, 2),
        fit_score=round(best_scored.total_score, 4),
        rationale=(
            f"Scored {len(scored)} eligible SKUs. "
            f"Best fit: {best_scored.sku.sku_name} "
            f"(cost={best_scored.cost_score:.2f}, "
            f"cpu_fit={best_scored.cpu_fit_score:.2f}, "
            f"mem_fit={best_scored.memory_fit_score:.2f}, "
            f"gen={best_scored.generation_score:.2f}). "
            f"Est. ${monthly:.2f}/mo."
        ),
    )


@observe()
async def _size_container_workload(
    component: ComponentProfile,
    original_workload: WorkloadRequirement,
    container_workloads: list[WorkloadRequirement],
    candidates: list[NormalizedPriceItem],
    provider: CloudProvider,
) -> tuple[SizedWorkloadResult, BinPackingResult | None]:
    """Size a CONTAINER workload using the bin-packing engine.

    Selects the best node SKU, then packs all container workloads
    onto it.

    Args:
        component: Profiler's analysis.
        original_workload: Original workload requirement.
        container_workloads: All container workloads (for packing together).
        candidates: Candidate node SKUs from PricingService.
        provider: Target cloud provider.

    Returns:
        Tuple of (SizedWorkloadResult, BinPackingResult or None).
    """
    log = logger.bind(
        agent="sizer",
        step="size_container",
        workload=component.workload_name,
        provider=provider.value,
    )
    log.info(
        "bin_packing_started",
        candidate_count=len(candidates),
        container_workload_count=len(container_workloads),
    )

    # Select node SKU: pick a reasonably-sized node
    # Filter to nodes with at least 2 vCPUs and 4 GB memory
    viable_nodes = [
        c for c in candidates
        if extract_vcpus(c) >= 2
        and extract_memory_gb(c) >= 4.0
        and c.monthly_cost_estimate is not None
        and c.monthly_cost_estimate > 0
    ]

    if not viable_nodes:
        log.warning("no_viable_node_skus")
        best, alts = _select_best_by_price(candidates)
        return (
            SizedWorkloadResult(
                workload_name=component.workload_name,
                provider=provider,
                selected_sku=best,
                alternative_skus=alts,
                monthly_cost_usd=(best.monthly_cost_estimate or 0.0) if best else 0.0,
                fit_score=0.2,
                rationale="No viable node SKUs found for container packing.",
            ),
            None,
        )

    # Sort by cost-efficiency: monthly_cost / (vcpus * memory_gb)
    def cost_efficiency(sku: NormalizedPriceItem) -> float:
        vcpus = max(extract_vcpus(sku), 1)
        mem = max(extract_memory_gb(sku), 1.0)
        cost = sku.monthly_cost_estimate or float("inf")
        return cost / (vcpus * mem)

    viable_nodes.sort(key=cost_efficiency)
    best_node = viable_nodes[0]
    alt_nodes = viable_nodes[1: 1 + _MAX_ALTERNATIVES]

    # Pack all container workloads onto selected node type
    packing_result = pack_workloads(
        workloads=container_workloads,
        node_sku=best_node,
        algorithm=PackingAlgorithm.BEST_FIT_DECREASING,
        pool_name=f"{provider.value}-pool",
    )

    monthly = packing_result.total_monthly_cost_usd

    log.info(
        "bin_packing_completed",
        node_sku=best_node.sku_name,
        total_nodes=packing_result.total_nodes,
        efficiency=packing_result.packing_efficiency_pct,
        monthly_cost=round(monthly, 2),
    )

    return (
        SizedWorkloadResult(
            workload_name=component.workload_name,
            provider=provider,
            selected_sku=best_node,
            alternative_skus=alt_nodes,
            monthly_cost_usd=round(monthly, 2),
            fit_score=round(packing_result.packing_efficiency_pct / 100.0, 4),
            rationale=(
                f"Bin-packed {len(container_workloads)} container workload(s) "
                f"onto {packing_result.total_nodes} × {best_node.sku_name} nodes. "
                f"Packing efficiency: {packing_result.packing_efficiency_pct:.1f}%. "
                f"Est. ${monthly:.2f}/mo."
            ),
        ),
        packing_result,
    )


@observe()
async def _size_generic_workload(
    component: ComponentProfile,
    candidates: list[NormalizedPriceItem],
    provider: CloudProvider,
) -> SizedWorkloadResult:
    """Size a DATABASE, STORAGE, SERVERLESS, or other workload.

    Uses simple cheapest-match selection.

    Args:
        component: Profiler's analysis.
        candidates: Candidate SKUs from PricingService.
        provider: Target cloud provider.

    Returns:
        SizedWorkloadResult with cheapest matching SKU.
    """
    log = logger.bind(
        agent="sizer",
        step="size_generic",
        workload=component.workload_name,
        provider=provider.value,
        category=component.resolved_category.value,
    )
    log.info("generic_sizing_started", candidate_count=len(candidates))

    best, alts = _select_best_by_price(candidates)

    if best is None:
        log.warning("no_priced_skus_found")
        return SizedWorkloadResult(
            workload_name=component.workload_name,
            provider=provider,
            selected_sku=None,
            alternative_skus=[],
            monthly_cost_usd=0.0,
            fit_score=0.0,
            rationale=(
                f"No priced SKUs found for {component.resolved_category.value} "
                f"workload '{component.workload_name}' on {provider.value}."
            ),
        )

    monthly = best.monthly_cost_estimate or 0.0

    log.info(
        "generic_sizing_completed",
        best_sku=best.sku_name,
        monthly_cost=round(monthly, 2),
        alternatives=len(alts),
    )

    return SizedWorkloadResult(
        workload_name=component.workload_name,
        provider=provider,
        selected_sku=best,
        alternative_skus=alts,
        monthly_cost_usd=round(monthly, 2),
        fit_score=0.7,
        rationale=(
            f"Selected {best.sku_name} as cheapest option for "
            f"{component.resolved_category.value} workload "
            f"'{component.workload_name}'. Est. ${monthly:.2f}/mo."
        ),
    )


# ---------------------------------------------------------------------------
# LLM-assisted rationale enhancement
# ---------------------------------------------------------------------------

_SIZER_SYSTEM_PROMPT = """\
You are a cloud infrastructure sizing expert. Given a set of workload \
sizing results, provide a brief overall summary that:

1. Highlights key sizing decisions and trade-offs
2. Notes any workloads where the fit score is low (<0.5)
3. Suggests potential optimizations (e.g. reserved instances, spot)

Respond in 3-5 sentences. Be concise and technical.
"""


@observe()
async def _generate_sizer_summary(
    llm: BaseChatModel,
    results: list[SizedWorkloadResult],
    workload_profile: WorkloadProfile,
) -> str:
    """Use the LLM to generate an overall sizing summary.

    Args:
        llm: The LLM model (BaseChatModel interface).
        results: All sizing results produced.
        workload_profile: The profiler's workload analysis.

    Returns:
        LLM-generated summary string.
    """
    log = logger.bind(agent="sizer", step="generate_summary")
    log.debug("summary_generation_started")

    # Build a concise input for the LLM
    result_summaries = []
    for r in results:
        sku_name = r.selected_sku.sku_name if r.selected_sku else "none"
        result_summaries.append({
            "workload": r.workload_name,
            "provider": r.provider.value,
            "sku": sku_name,
            "monthly_cost": r.monthly_cost_usd,
            "fit_score": r.fit_score,
        })

    user_content = json.dumps({
        "environment": workload_profile.environment.value,
        "tier": workload_profile.tier.value,
        "total_vcpus": workload_profile.total_vcpus,
        "total_memory_gb": workload_profile.total_memory_gb,
        "total_components": len(workload_profile.components),
        "sizing_results": result_summaries,
    }, indent=2)

    messages = [
        SystemMessage(content=_SIZER_SYSTEM_PROMPT),
        HumanMessage(content=f"Summarize these sizing results:\n\n{user_content}"),
    ]

    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, "content") else str(response)
        log.debug("summary_generation_completed", length=len(content))
        return content[:800]
    except Exception:
        log.warning("summary_generation_failed", exc_info=True)
        # Heuristic fallback
        total_cost = sum(r.monthly_cost_usd for r in results)
        providers_used = {r.provider.value for r in results}
        low_fit = [r for r in results if r.fit_score < 0.5]
        parts = [
            f"Sized {len(results)} workload-provider combinations "
            f"across {', '.join(sorted(providers_used))}.",
            f"Total estimated cost: ${total_cost:.2f}/mo.",
        ]
        if low_fit:
            names = ", ".join(r.workload_name for r in low_fit)
            parts.append(f"Low fit scores for: {names} — review SKU availability.")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------


@observe()
async def run_sizer_node(
    state: OrchestratorState,
    llm: BaseChatModel,
    pricing_service: PricingService,
) -> OrchestratorState:
    """Execute the Sizer agent — select SKUs and size node pools.

    This is a LangGraph node function.  It reads
    ``state['workload_profile']`` and ``state['workload_request']``
    and produces ``state['sized_results']`` with one
    ``SizedWorkloadResult`` per component per provider.

    Args:
        state: Current ``OrchestratorState`` (TypedDict).
        llm: LLM instance for generating summaries (``BaseChatModel``
             interface — never import a provider-specific class here).
        pricing_service: Initialised ``PricingService`` with providers
            registered.

    Returns:
        Updated ``OrchestratorState`` with ``sized_results`` populated
        and a summary message appended to ``messages``.

    Raises:
        ValueError: If ``workload_profile`` or ``workload_request``
            is missing from state.
    """
    log = logger.bind(
        agent="sizer",
        request_id=state.get("request_id", "unknown"),
    )

    start_time = datetime.now(timezone.utc)
    log.info("sizer_node_started")

    try:
        # ── Validate inputs ───────────────────────────────────
        workload_profile: WorkloadProfile | None = state.get("workload_profile")
        if workload_profile is None:
            raise ValueError(
                "workload_profile is missing from state — Profiler must run first"
            )

        workload_request: WorkloadRequest | None = state.get("workload_request")
        if workload_request is None:
            raise ValueError(
                "workload_request is missing from state — Clarifier must run first"
            )

        if not workload_profile.components:
            raise ValueError(
                "workload_profile has no components — Profiler produced empty output"
            )

        target_providers = workload_request.target_providers

        log.info(
            "sizing_workloads",
            project=workload_request.project_name,
            component_count=len(workload_profile.components),
            target_providers=[p.value for p in target_providers],
        )

        # ── Build a name → WorkloadRequirement lookup ─────────
        workload_lookup: dict[str, WorkloadRequirement] = {
            wl.name: wl for wl in workload_request.workloads
        }

        # ── Identify container workloads for combined packing ─
        container_components = [
            c for c in workload_profile.components
            if c.resolved_category in _BINPACKED_CATEGORIES
        ]
        container_workloads = [
            workload_lookup[c.workload_name]
            for c in container_components
            if c.workload_name in workload_lookup
        ]

        # Track which container workloads have been bin-packed
        # (to avoid duplicate sizing)
        container_packed_for: set[tuple[str, str]] = set()  # (workload_name, provider)

        # ── Size each component × provider ────────────────────
        all_results: list[SizedWorkloadResult] = []
        bin_packing_results: dict[str, BinPackingResult] = {}

        for component in workload_profile.components:
            original_wl = workload_lookup.get(component.workload_name)
            if original_wl is None:
                log.warning(
                    "workload_not_found_in_request",
                    workload_name=component.workload_name,
                )
                continue

            for provider in target_providers:
                # Skip if locked to a different provider
                if (
                    original_wl.provider_preference is not None
                    and original_wl.provider_preference != provider
                ):
                    log.debug(
                        "skipping_provider_preference",
                        workload=component.workload_name,
                        provider=provider.value,
                        preferred=original_wl.provider_preference.value,
                    )
                    continue

                comp_log = log.bind(
                    workload=component.workload_name,
                    provider=provider.value,
                    category=component.resolved_category.value,
                )
                comp_log.info("sizing_component_started")

                # ── Fetch candidate SKUs ──────────────────────
                region = _get_region_for_provider(
                    provider, workload_request, original_wl,
                )
                service_name = _SERVICE_NAME_MAP.get(
                    (provider.value, component.resolved_category),
                )

                try:
                    candidates = await pricing_service.search_prices(
                        provider,
                        service_name=service_name,
                        service_category=component.resolved_category,
                        region=region,
                        pricing_tier=PricingTier.ON_DEMAND,
                        max_results=100,
                    )
                except Exception:
                    comp_log.error("pricing_search_failed", exc_info=True)
                    all_results.append(
                        SizedWorkloadResult(
                            workload_name=component.workload_name,
                            provider=provider,
                            selected_sku=None,
                            alternative_skus=[],
                            monthly_cost_usd=0.0,
                            fit_score=0.0,
                            rationale=(
                                f"Pricing search failed for "
                                f"{component.workload_name} on {provider.value}."
                            ),
                        )
                    )
                    continue

                comp_log.info(
                    "candidates_fetched",
                    count=len(candidates),
                    region=region,
                )

                if not candidates:
                    comp_log.warning("no_candidates_found")
                    all_results.append(
                        SizedWorkloadResult(
                            workload_name=component.workload_name,
                            provider=provider,
                            selected_sku=None,
                            alternative_skus=[],
                            monthly_cost_usd=0.0,
                            fit_score=0.0,
                            rationale=(
                                f"No SKU candidates found for "
                                f"{component.resolved_category.value} on "
                                f"{provider.value} in {region}."
                            ),
                        )
                    )
                    continue

                # ── Route to appropriate sizing strategy ──────
                if component.resolved_category in _SCORED_CATEGORIES:
                    result = await _size_compute_workload(
                        component, original_wl, candidates, provider,
                    )
                    all_results.append(result)

                elif component.resolved_category in _BINPACKED_CATEGORIES:
                    pack_key = (component.workload_name, provider.value)
                    if pack_key in container_packed_for:
                        comp_log.debug("already_packed")
                        continue

                    result, packing = await _size_container_workload(
                        component,
                        original_wl,
                        container_workloads,
                        candidates,
                        provider,
                    )
                    all_results.append(result)

                    if packing is not None:
                        bin_packing_results[provider.value] = packing

                    # Mark all container workloads as packed for this provider
                    for cw in container_workloads:
                        container_packed_for.add((cw.name, provider.value))

                else:
                    result = await _size_generic_workload(
                        component, candidates, provider,
                    )
                    all_results.append(result)

                comp_log.info(
                    "sizing_component_completed",
                    selected_sku=(
                        result.selected_sku.sku_name
                        if result.selected_sku else "none"
                    ),
                    fit_score=result.fit_score,
                    monthly_cost=result.monthly_cost_usd,
                )

        # ── Generate LLM summary ─────────────────────────────
        sizer_summary = await _generate_sizer_summary(
            llm, all_results, workload_profile,
        )

        # ── Build summary message ─────────────────────────────
        total_cost_by_provider: dict[str, float] = {}
        for r in all_results:
            prov = r.provider.value
            total_cost_by_provider[prov] = (
                total_cost_by_provider.get(prov, 0.0) + r.monthly_cost_usd
            )

        cost_lines = "\n".join(
            f"  • **{prov}**: ${cost:.2f}/mo"
            for prov, cost in sorted(total_cost_by_provider.items())
        )

        result_lines = "\n".join(
            f"  • {r.workload_name} ({r.provider.value}): "
            f"{r.selected_sku.sku_name if r.selected_sku else 'N/A'} — "
            f"${r.monthly_cost_usd:.2f}/mo (fit: {r.fit_score:.2f})"
            for r in all_results
        )

        summary_content = (
            f"**Sizer Analysis Complete** — "
            f"{len(all_results)} workload-provider combinations sized:\n"
            f"{result_lines}\n\n"
            f"**Estimated monthly costs by provider:**\n{cost_lines}\n\n"
            f"{sizer_summary}"
        )

        summary_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=summary_content,
            agent_name="sizer",
            metadata={
                "result_count": len(all_results),
                "cost_by_provider": total_cost_by_provider,
                "bin_packing_providers": list(bin_packing_results.keys()),
            },
        )

        # ── Compute timing ────────────────────────────────────
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        execution = AgentExecution(
            agent_name="sizer",
            status=AgentStatus.COMPLETED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
        )

        log.info(
            "sizer_node_completed",
            result_count=len(all_results),
            cost_by_provider=total_cost_by_provider,
            duration_ms=round(duration_ms, 1),
        )

        # ── Return state update ───────────────────────────────
        return {
            "sized_results": all_results,
            "messages": [summary_message],
            "current_agent": "sizer",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "sizer": execution,
            },
        }

    except Exception:
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        log.error("sizer_node_failed", exc_info=True, duration_ms=round(duration_ms, 1))

        execution = AgentExecution(
            agent_name="sizer",
            status=AgentStatus.FAILED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
            error_message=str(Exception),
        )

        error_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="**Sizer Error** — Failed to complete SKU sizing. Check logs for details.",
            agent_name="sizer",
            metadata={"error": True},
        )

        return {
            "sized_results": [],
            "messages": [error_message],
            "current_agent": "sizer",
            "error": f"Sizer failed: {Exception}",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "sizer": execution,
            },
        }
