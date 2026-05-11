"""Architecture Selector Engine — P15a.

Ranks four architectural patterns (managed-serverless, self-hosted-serverless,
containers, hybrid) against a set of workload requirements using three signal
groups: traffic pattern (avg_rps / cost crossover), workload characteristics,
and compliance tags.

Purely algorithmic — no LLM call.  Fast, deterministic, fully unit-testable.

Typical usage::

    from src.engines.architecture_selector import select_architecture
    recommendation = select_architecture(requirements, sized_results, profile)
    print(recommendation.winner.label, recommendation.winner.score)
"""

from __future__ import annotations

import math
import statistics
from typing import Any

import structlog
from langfuse import observe
from pydantic import BaseModel, Field

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.workload import ScalingPattern, WorkloadProfile, WorkloadRequest, WorkloadRequirement

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default WAF scoring weights (must sum to 1.0).
_DEFAULT_WEIGHTS: dict[str, float] = {
    "reliability": 0.30,
    "cost": 0.25,
    "scale": 0.25,
    "compliance": 0.10,
    "latency": 0.10,
}

# ---------------------------------------------------------------------------
# P15j — Dynamic WAF weight profiles
# ---------------------------------------------------------------------------

#: Priority-signal keyword → dimension boost pairs.
_PRIORITY_BOOSTS: dict[str, dict[str, float]] = {
    # Cost priority signals
    "cost": {"cost": +0.15, "reliability": -0.05, "scale": -0.05, "latency": -0.05},
    "budget": {"cost": +0.15, "reliability": -0.05, "scale": -0.05, "latency": -0.05},
    "cheap": {"cost": +0.10, "reliability": -0.05, "latency": -0.05},
    "affordable": {"cost": +0.10, "reliability": -0.05, "latency": -0.05},
    # Reliability / availability priority signals
    "reliable": {"reliability": +0.15, "cost": -0.08, "scale": -0.07},
    "availability": {"reliability": +0.15, "cost": -0.08, "scale": -0.07},
    "99.99": {"reliability": +0.20, "cost": -0.10, "scale": -0.10},
    "high availability": {"reliability": +0.15, "cost": -0.08, "scale": -0.07},
    # Latency priority signals
    "low latency": {"latency": +0.15, "cost": -0.08, "compliance": -0.07},
    "fast": {"latency": +0.10, "cost": -0.05, "compliance": -0.05},
    "real-time": {"latency": +0.10, "cost": -0.05, "compliance": -0.05},
    # Compliance / security priority signals
    "compliance": {"compliance": +0.15, "cost": -0.08, "scale": -0.07},
    "hipaa": {"compliance": +0.20, "cost": -0.10, "scale": -0.10},
    "fedramp": {"compliance": +0.20, "cost": -0.10, "scale": -0.10},
    "gdpr": {"compliance": +0.15, "cost": -0.08, "scale": -0.07},
    # Scale / performance priority signals
    "scale": {"scale": +0.15, "cost": -0.08, "compliance": -0.07},
    "performance": {"scale": +0.10, "latency": +0.05, "cost": -0.08, "compliance": -0.07},
    "throughput": {"scale": +0.10, "latency": +0.05, "cost": -0.08, "compliance": -0.07},
}


def derive_weights_from_workload(
    workload_request: WorkloadRequest | None,
) -> dict[str, float]:
    """P15j — Derive dynamic WAF dimension weights from workload priority signals.

    Scans the user's input, notes, and compliance frameworks for priority
    keywords and adjusts the default weights accordingly.  After applying all
    boosts, the weights are normalised to sum to 1.0.

    If ``workload_request`` is ``None`` or no signals are detected, the default
    weights are returned unchanged.

    Args:
        workload_request: The top-level workload request (provides raw_user_input
            and workload descriptions).

    Returns:
        A 5-key dict of normalised dimension weights.
    """
    log = logger.bind(component="architecture_selector", step="derive_weights")
    w = {**_DEFAULT_WEIGHTS}

    if workload_request is None:
        return w

    # Build a combined lowercase text from all available sources
    sources = [workload_request.raw_user_input or ""]
    for wl in workload_request.workloads:
        sources.extend([wl.description or "", wl.notes or ""])
    for fw in workload_request.compliance_frameworks:
        sources.append(fw.lower() if hasattr(fw, "lower") else str(fw).lower())

    text = " ".join(sources).lower()

    applied: list[str] = []
    for signal, adjustments in _PRIORITY_BOOSTS.items():
        if signal in text:
            for dim, delta in adjustments.items():
                w[dim] = w.get(dim, 0.0) + delta
            applied.append(signal)

    if not applied:
        return w  # no signals detected — keep defaults

    # Clamp all weights to [0.01, 0.70]
    w = {k: max(0.01, min(0.70, v)) for k, v in w.items()}

    # Normalise to sum to 1.0
    total = sum(w.values())
    if total > 0:
        w = {k: round(v / total, 4) for k, v in w.items()}

    log.info(
        "dynamic_weights_derived",
        signals=applied,
        weights=w,
    )
    return w

#: Typical EKS m5.xlarge on-demand hourly rate (USD) — Knative node baseline.
_KNATIVE_NODE_HOURLY_USD: float = 0.192
#: Approximate RPS capacity per Knative pod at ~50 % CPU utilisation.
_RPS_PER_POD: int = 100
#: Knative pods that fit on one m5.xlarge (4 vCPU / 500 m per pod).
_PODS_PER_NODE: int = 6
#: Minimum Knative node count (control-plane headroom).
_MIN_KNATIVE_NODES: int = 2
#: Hours in a 30-day month.
_HOURS_PER_MONTH: float = 720.0
#: Seconds in a 30-day month.
_SECONDS_PER_MONTH: int = 2_592_000
#: Assumed requests per user per second (session think-time model).
_REQ_PER_USER_PER_SEC: float = 0.016
#: Lambda request price per invocation (USD).
_LAMBDA_REQ_PRICE: float = 0.0000002
#: Lambda compute price per GB-second (USD).
_LAMBDA_GB_SEC_PRICE: float = 0.0000166667
#: Default Lambda average execution duration (ms) when not specified.
_DEFAULT_LAMBDA_DURATION_MS: float = 200.0
#: Default Lambda memory allocation (MB) when not specified.
_DEFAULT_LAMBDA_MEMORY_MB: float = 512.0
#: Burst-ratio approximations by scaling pattern.
_BURST_RATIO_BY_PATTERN: dict[ScalingPattern, float] = {
    ScalingPattern.STEADY: 1.5,
    ScalingPattern.BURSTY: 15.0,
}

# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class ArchitectureOption(BaseModel):
    """One evaluated architecture pattern with per-dimension WAF scores.

    Attributes:
        name: Machine identifier (e.g. ``"managed_serverless"``).
        label: Human-readable label.
        score: Composite WAF score in [0, 1].
        monthly_cost_estimate: Estimated monthly cost in USD.
        reliability_score: Reliability dimension score.
        cost_score: Cost dimension score (higher = cheaper).
        scale_score: Scalability dimension score.
        compliance_score: Compliance dimension score.
        latency_score: Latency dimension score.
        rationale: Key signals that drove this option's score.
        trade_offs: What the customer sacrifices by choosing this option.
    """

    name: str
    label: str
    score: float = Field(ge=0.0, le=1.0)
    monthly_cost_estimate: float = Field(ge=0.0)
    reliability_score: float = Field(ge=0.0, le=1.0)
    cost_score: float = Field(ge=0.0, le=1.0)
    scale_score: float = Field(ge=0.0, le=1.0)
    compliance_score: float = Field(ge=0.0, le=1.0)
    latency_score: float = Field(ge=0.0, le=1.0)
    rationale: str
    trade_offs: str


class ArchitectureRecommendation(BaseModel):
    """Ranked architecture recommendation produced by the selector engine.

    Attributes:
        winner: Top-scoring option.
        ranked: All four options sorted by score descending.
        weights_used: Actual WAF dimension weights applied (dynamic or default).
        key_signals: Diagnostic signals (avg_rps, burst_ratio, crossover_rps, …).
        recommendation_rationale: Full narrative suitable for an RFP Alternatives section.
        warning: Non-None if the algorithmic winner conflicts with an assumption
            embedded in workload notes (e.g. clarifier said "use Lambda" but the
            engine recommends Knative due to high avg_rps).
    """

    winner: ArchitectureOption
    ranked: list[ArchitectureOption]
    weights_used: dict[str, float]
    key_signals: dict[str, Any]
    recommendation_rationale: str
    warning: str | None = None


# ---------------------------------------------------------------------------
# Signal extraction helpers
# ---------------------------------------------------------------------------


def _extract_traffic_signals(
    requirements: list[WorkloadRequirement],
) -> tuple[float, float]:
    """Derive ``(avg_rps, burst_ratio)`` from workload requirements.

    Uses ``throughput_rps`` when available; falls back to
    ``concurrent_users × _REQ_PER_USER_PER_SEC``.  ``burst_ratio`` is
    estimated from ``ScalingPattern`` when peak concurrency is not
    explicitly specified.

    Args:
        requirements: All workload requirements for the current request.

    Returns:
        Tuple of ``(avg_rps, burst_ratio)``.
    """
    avg_rps_values: list[float] = []
    burst_ratios: list[float] = []

    for wl in requirements:
        if wl.throughput_rps is not None and wl.throughput_rps > 0:
            avg_rps_values.append(float(wl.throughput_rps))
        elif wl.concurrent_users is not None and wl.concurrent_users > 0:
            avg_rps_values.append(wl.concurrent_users * _REQ_PER_USER_PER_SEC)

        burst_ratios.append(_BURST_RATIO_BY_PATTERN.get(wl.scaling_pattern, 3.0))

    avg_rps = sum(avg_rps_values) if avg_rps_values else 100.0
    burst_ratio = max(burst_ratios) if burst_ratios else 3.0
    return avg_rps, burst_ratio


def _lambda_monthly_cost(
    avg_rps: float,
    avg_duration_ms: float,
    memory_mb: float,
) -> float:
    """Estimate Lambda monthly cost at a sustained average RPS.

    Args:
        avg_rps: Average requests per second (sustained load).
        avg_duration_ms: Average function execution time in milliseconds.
        memory_mb: Lambda memory allocation in MB.

    Returns:
        Estimated monthly cost in USD (no free-tier deduction).
    """
    invocations = avg_rps * _SECONDS_PER_MONTH
    mem_gb = memory_mb / 1024.0
    dur_sec = avg_duration_ms / 1000.0
    return round(
        invocations * _LAMBDA_REQ_PRICE
        + invocations * dur_sec * mem_gb * _LAMBDA_GB_SEC_PRICE,
        2,
    )


def _knative_monthly_cost(avg_rps: float) -> float:
    """Estimate EKS + Knative monthly node cost at a given average RPS.

    Uses m5.xlarge on-demand pricing as the representative Knative node type.

    Args:
        avg_rps: Average RPS used for minimum node-pool sizing.

    Returns:
        Estimated monthly cost in USD.
    """
    pods_needed = math.ceil(avg_rps / _RPS_PER_POD)
    nodes = max(_MIN_KNATIVE_NODES, math.ceil(pods_needed / _PODS_PER_NODE))
    return round(nodes * _KNATIVE_NODE_HOURLY_USD * _HOURS_PER_MONTH, 2)


def _crossover_rps(avg_duration_ms: float, memory_mb: float) -> float:
    """Binary-search the RPS at which Lambda cost crosses Knative cost.

    Args:
        avg_duration_ms: Lambda average duration in milliseconds.
        memory_mb: Lambda memory allocation in MB.

    Returns:
        Crossover RPS (Lambda becomes more expensive above this value).
    """
    lo, hi = 1.0, 10_000.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _lambda_monthly_cost(mid, avg_duration_ms, memory_mb) < _knative_monthly_cost(mid):
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0, 1)


# ---------------------------------------------------------------------------
# Dimension score helpers
# ---------------------------------------------------------------------------


def _all_compliance_tags(requirements: list[WorkloadRequirement]) -> set[str]:
    """Collect all compliance tags from all workload requirements.

    Args:
        requirements: Workload requirements to scan.

    Returns:
        Lowercase set of all compliance tags.
    """
    tags: set[str] = set()
    for wl in requirements:
        tags.update(t.lower() for t in wl.compliance_tags)
    return tags


def _min_latency_p99(requirements: list[WorkloadRequirement]) -> int | None:
    """Return the strictest (lowest) p99 latency requirement across workloads.

    Args:
        requirements: Workload requirements to scan.

    Returns:
        Minimum latency_p99_ms value, or None if not specified.
    """
    values = [wl.latency_p99_ms for wl in requirements if wl.latency_p99_ms is not None]
    return min(values) if values else None


def _has_stateful_workloads(profile: WorkloadProfile | None) -> bool:
    """Detect whether any component requires stateful persistence.

    Args:
        profile: Profiler output (may be None if profiler has not run yet).

    Returns:
        True if any component uses DATABASE or STORAGE category.
    """
    if not profile:
        return False
    stateful = {ServiceCategory.DATABASE, ServiceCategory.STORAGE}
    return any(c.resolved_category in stateful for c in profile.components)


def _event_driven_fraction(profile: WorkloadProfile | None) -> float:
    """Fraction of components that are event-driven / analytics workloads.

    Args:
        profile: Profiler output (may be None).

    Returns:
        Float in [0, 1].
    """
    if not profile or not profile.components:
        return 0.0
    event_cats = {ServiceCategory.INTEGRATION, ServiceCategory.ANALYTICS}
    event_count = sum(1 for c in profile.components if c.resolved_category in event_cats)
    return event_count / len(profile.components)


def _compliance_scores(compliance_tags: set[str]) -> dict[str, float]:
    """Compute compliance dimension scores for each option.

    Args:
        compliance_tags: All compliance tags across all workloads.

    Returns:
        Dict mapping option name → compliance score in [0, 1].
    """
    base = 0.85
    scores = {
        "managed_serverless": base,
        "self_hosted_serverless": base,
        "containers": base,
        "hybrid": base,
    }
    if "hipaa" in compliance_tags:
        scores["containers"] = min(1.0, scores["containers"] + 0.10)
        scores["self_hosted_serverless"] = min(1.0, scores["self_hosted_serverless"] + 0.05)
        scores["managed_serverless"] = max(0.0, scores["managed_serverless"] - 0.10)
        scores["hybrid"] = max(0.0, scores["hybrid"] - 0.05)
    if "stateramp" in compliance_tags or "fedramp" in compliance_tags:
        scores["containers"] = min(1.0, scores["containers"] + 0.05)
        scores["self_hosted_serverless"] = min(1.0, scores["self_hosted_serverless"] + 0.05)
        scores["managed_serverless"] = max(0.0, scores["managed_serverless"] - 0.05)
    return scores


def _latency_scores(latency_p99_ms: int | None) -> dict[str, float]:
    """Compute latency dimension scores based on strictest p99 requirement.

    Args:
        latency_p99_ms: Strictest p99 latency requirement (ms), or None.

    Returns:
        Dict mapping option name → latency score.
    """
    if latency_p99_ms is not None and latency_p99_ms < 100:
        # Cold-start risk penalises managed Lambda
        return {
            "managed_serverless": 0.55,
            "self_hosted_serverless": 0.95,
            "containers": 1.0,
            "hybrid": 0.80,
        }
    # Cold start acceptable
    return {
        "managed_serverless": 0.85,
        "self_hosted_serverless": 0.90,
        "containers": 0.95,
        "hybrid": 0.85,
    }


def _reliability_scores(stateful: bool) -> dict[str, float]:
    """Compute reliability dimension scores.

    Args:
        stateful: Whether stateful services are present in the workload.

    Returns:
        Dict mapping option name → reliability score.
    """
    scores = {
        "managed_serverless": 0.90,
        "self_hosted_serverless": 0.80,
        "containers": 0.90,
        "hybrid": 0.75,
    }
    if stateful:
        scores["managed_serverless"] = max(0.0, scores["managed_serverless"] - 0.10)
    return scores


# ---------------------------------------------------------------------------
# Real-cost extraction helpers (P16a)
# ---------------------------------------------------------------------------

#: Knative self-hosted overhead vs pure containers (Knative infra is cheaper
#: due to scale-to-zero but adds operator overhead — net ~90% of container cost).
_KNATIVE_COST_FACTOR = 0.90

#: Categories that map to serverless billing (Lambda/Functions-as-a-Service).
_SERVERLESS_CATEGORIES = frozenset(
    [ServiceCategory.SERVERLESS, ServiceCategory.SERVERLESS_FUNCTION]
)


def _extract_costs_from_sized_results(
    sized_results: list,
) -> tuple[float, float, float]:
    """Derive architecture cost baselines from actual sizer output.

    Groups ``SizedWorkloadResult`` entries by provider and category to
    produce three cost figures used by the architecture scorer:

    * **container_cost** — total monthly cost on the cheapest provider
      (represents the "containers" architecture).
    * **lambda_cost** — cost if the workload ran on managed serverless
      (Lambda/Functions). Uses actual SERVERLESS-category sizer results
      if present; otherwise falls back to the heuristic caller will supply.
    * **knative_cost** — EKS + Knative/KEDA equivalent cost, estimated as
      ``_KNATIVE_COST_FACTOR × container_cost``.

    Args:
        sized_results: List of ``SizedWorkloadResult`` from the sizer agent.

    Returns:
        Tuple ``(container_cost, lambda_cost, knative_cost)`` in USD/month.
        All three are ``0.0`` when ``sized_results`` is empty or all costs
        are zero, signalling the caller to use its own heuristics.
    """
    if not sized_results:
        return 0.0, 0.0, 0.0

    # Accumulate costs per provider for each category group.
    provider_container: dict[str, float] = {}
    provider_serverless: dict[str, float] = {}

    for r in sized_results:
        cost = getattr(r, "monthly_cost_usd", 0.0) or 0.0
        if cost <= 0.0:
            continue

        provider = getattr(r, "provider", None)
        provider_key: str = provider.value if provider is not None else "unknown"

        sku = getattr(r, "selected_sku", None)
        category = getattr(sku, "service_category", None) if sku is not None else None

        if category in _SERVERLESS_CATEGORIES:
            provider_serverless[provider_key] = provider_serverless.get(provider_key, 0.0) + cost
        else:
            # Everything else (COMPUTE, CONTAINER, DATABASE, STORAGE, …) counts
            # as always-on container/VM infrastructure.
            provider_container[provider_key] = provider_container.get(provider_key, 0.0) + cost

    if not provider_container and not provider_serverless:
        return 0.0, 0.0, 0.0

    # Prefer AWS for the container baseline (Lambda crossover comparison is
    # most meaningful on the same provider).  Fall back to cheapest provider.
    def _best_provider(costs: dict[str, float]) -> float:
        if not costs:
            return 0.0
        if "aws" in costs:
            return costs["aws"]
        return min(costs.values())

    container_cost = _best_provider(provider_container)
    lambda_cost_real = _best_provider(provider_serverless)
    knative_cost = round(container_cost * _KNATIVE_COST_FACTOR, 2) if container_cost > 0 else 0.0

    return container_cost, lambda_cost_real, knative_cost


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


@observe()
def select_architecture(
    requirements: list[WorkloadRequirement],
    sized_results: list | None = None,
    workload_profile: WorkloadProfile | None = None,
    weights: dict[str, float] | None = None,
) -> ArchitectureRecommendation:
    """Score and rank four architecture patterns against the given workloads.

    Purely algorithmic — no LLM call.  All inputs come from the orchestrator
    state.  The function is deterministic for identical inputs.

    The four patterns evaluated:

    * ``managed_serverless`` — Lambda + DynamoDB + API Gateway.
    * ``self_hosted_serverless`` — EKS + Knative/KEDA + RDS.
    * ``containers`` — EKS + RDS + ElastiCache + ALB.
    * ``hybrid`` — Lambda for event paths + K8s for stateful services.

    Args:
        requirements: All workload requirements from the orchestrator state.
        sized_results: Optional sizer output for container cost baseline.
        workload_profile: Optional profiler output for category analysis.
        weights: Optional WAF dimension weight overrides (P15j dynamic weights).

    Returns:
        :class:`ArchitectureRecommendation` with winner, ranked options, and
        diagnostic signals.
    """
    log = logger.bind(agent="architecture_selector", step="select_architecture")
    log.info("architecture_selection_started", requirement_count=len(requirements))

    w = {**_DEFAULT_WEIGHTS, **(weights or {})}

    # ── Signal Group 1: Traffic Pattern ───────────────────────────────────
    avg_rps, burst_ratio = _extract_traffic_signals(requirements)

    # Serverless function parameters from requirements (first match wins)
    avg_duration_ms = _DEFAULT_LAMBDA_DURATION_MS
    memory_mb = _DEFAULT_LAMBDA_MEMORY_MB
    for wl in requirements:
        if wl.resources.avg_duration_ms:
            avg_duration_ms = float(wl.resources.avg_duration_ms)
            memory_mb = float(wl.resources.memory_mb or _DEFAULT_LAMBDA_MEMORY_MB)
            break

    crossover = _crossover_rps(avg_duration_ms, memory_mb)
    lambda_cost = _lambda_monthly_cost(avg_rps, avg_duration_ms, memory_mb)
    knative_cost = _knative_monthly_cost(avg_rps)

    # Container cost: sum actual sizer output per provider (P16a).
    # Falls back to Knative heuristic when sized_results is absent or all-zero.
    real_container, real_lambda, real_knative = _extract_costs_from_sized_results(
        sized_results or []
    )

    if real_container > 0.0:
        container_cost = real_container
        # Self-hosted serverless mirrors container infra at _KNATIVE_COST_FACTOR ratio.
        knative_cost = real_knative if real_knative > 0.0 else round(real_container * _KNATIVE_COST_FACTOR, 2)
        # Use actual Lambda/Functions cost from sizer if workload has SERVERLESS items;
        # otherwise keep the heuristic (meaningful as a relative crossover signal).
        if real_lambda > 0.0:
            lambda_cost = real_lambda
        log.info(
            "container_cost_from_sizer",
            container_cost=container_cost,
            knative_cost=knative_cost,
            lambda_cost=lambda_cost,
            real_lambda=real_lambda,
        )
    else:
        container_cost = knative_cost
        log.debug("container_cost_from_heuristic", knative_cost=knative_cost)

    # ── Signal Group 2: Workload Characteristics ──────────────────────────
    stateful = _has_stateful_workloads(workload_profile)
    event_frac = _event_driven_fraction(workload_profile)

    # ── Signal Group 3: Compliance ────────────────────────────────────────
    compliance_tags = _all_compliance_tags(requirements)
    latency_p99_ms = _min_latency_p99(requirements)

    log.debug(
        "signals_computed",
        avg_rps=avg_rps,
        burst_ratio=burst_ratio,
        crossover_rps=crossover,
        lambda_cost=lambda_cost,
        knative_cost=knative_cost,
        container_cost=container_cost,
        compliance_tags=sorted(compliance_tags),
        latency_p99_ms=latency_p99_ms,
        stateful=stateful,
        event_fraction=event_frac,
    )

    # ── Dimension scores ──────────────────────────────────────────────────
    comp_scores = _compliance_scores(compliance_tags)
    lat_scores = _latency_scores(latency_p99_ms)
    rel_scores = _reliability_scores(stateful)

    scale_sc = {
        "managed_serverless": min(burst_ratio / 20.0, 1.0),
        "self_hosted_serverless": min(burst_ratio / 50.0, 0.95),
        "containers": min(1.0, max(0.0, 1.0 - (burst_ratio - 5.0) / 40.0)),
        "hybrid": min(
            (burst_ratio / 20.0) * 0.7 + max(0.0, 1.0 - (burst_ratio - 5.0) / 40.0) * 0.3,
            1.0,
        ),
    }

    hybrid_cost = lambda_cost * (0.3 + event_frac * 0.3) + container_cost * (
        0.7 - event_frac * 0.3
    )
    cost_raw = {
        "managed_serverless": lambda_cost,
        "self_hosted_serverless": knative_cost,
        "containers": container_cost,
        "hybrid": hybrid_cost,
    }
    max_cost = max(cost_raw.values()) or 1.0
    cost_sc = {k: max(0.0, 1.0 - v / max_cost) for k, v in cost_raw.items()}

    # Stateful workloads reduce managed-serverless cost attractiveness
    if stateful:
        cost_sc["managed_serverless"] = max(0.0, cost_sc["managed_serverless"] - 0.10)

    def _composite(name: str) -> float:
        return round(
            w["reliability"] * rel_scores[name]
            + w["cost"] * cost_sc[name]
            + w["scale"] * scale_sc[name]
            + w["compliance"] * comp_scores[name]
            + w["latency"] * lat_scores[name],
            4,
        )

    # ── Build option rationale strings ────────────────────────────────────
    def _rationale(name: str) -> str:
        parts: list[str] = []
        above = avg_rps > crossover
        if name == "managed_serverless":
            parts.append(
                f"avg_rps={avg_rps:.0f} {'>' if above else '<'} crossover={crossover:.0f} RPS: "
                f"{'cost disadvantage vs Knative' if above else 'cost advantage at near-zero idle'}."
            )
            parts.append(f"burst_ratio={burst_ratio:.1f}× → scale_score={scale_sc[name]:.2f}.")
        elif name == "self_hosted_serverless":
            if above:
                parts.append(
                    f"avg_rps={avg_rps:.0f} > crossover={crossover:.0f}: cost advantage over Lambda."
                )
            parts.append(
                f"KEDA handles {burst_ratio:.1f}× burst with pre-warmed pods — no cold start."
            )
        elif name == "containers":
            if burst_ratio > 5:
                parts.append(
                    f"burst_ratio={burst_ratio:.1f}× penalises EKS HPA → scale_score={scale_sc[name]:.2f}."
                )
        elif name == "hybrid":
            parts.append(
                f"event_fraction={event_frac:.0%}: Lambda for async paths, K8s for stateful."
            )
        if latency_p99_ms is not None and latency_p99_ms < 100:
            parts.append(
                f"p99 < {latency_p99_ms} ms: cold-start risk penalises managed_serverless."
            )
        if compliance_tags:
            parts.append(
                f"compliance={sorted(compliance_tags)} → compliance_score={comp_scores[name]:.2f}."
            )
        return " ".join(parts) or f"Composite score={_composite(name):.3f}."

    # ── Static trade-off strings ──────────────────────────────────────────
    trade_offs_map = {
        "managed_serverless": (
            "Cold-start latency (50–500 ms p99 on warm-up). BAA complexity for HIPAA. "
            "Max 15-min execution time. Vendor lock-in on Lambda runtime."
        ),
        "self_hosted_serverless": (
            "Operational overhead of managing Knative/KEDA. "
            "Minimum 2-node cluster even at near-zero load. "
            "Slower cold-scale-from-zero vs managed Lambda."
        ),
        "containers": (
            "Does not scale to zero — baseline cost even at minimal traffic. "
            "Requires over-provisioning headroom for > 3× bursts. "
            "HPA alone is insufficient for 10×+ spike scenarios."
        ),
        "hybrid": (
            "Highest architectural complexity — two deployment pipelines. "
            "Distributed tracing across Lambda and K8s is non-trivial. "
            "Data-plane boundary design requires careful ownership definition."
        ),
    }

    labels = {
        "managed_serverless": "Lambda + DynamoDB + API Gateway (Managed Serverless)",
        "self_hosted_serverless": "EKS + Knative/KEDA (Self-Hosted Serverless)",
        "containers": "EKS + RDS + ElastiCache + ALB (Containers)",
        "hybrid": "Lambda (event paths) + K8s (stateful) Hybrid",
    }

    options: list[ArchitectureOption] = [
        ArchitectureOption(
            name=name,
            label=labels[name],
            score=_composite(name),
            monthly_cost_estimate=cost_raw[name],
            reliability_score=rel_scores[name],
            cost_score=cost_sc[name],
            scale_score=scale_sc[name],
            compliance_score=comp_scores[name],
            latency_score=lat_scores[name],
            rationale=_rationale(name),
            trade_offs=trade_offs_map[name],
        )
        for name in ("managed_serverless", "self_hosted_serverless", "containers", "hybrid")
    ]

    ranked = sorted(options, key=lambda o: o.score, reverse=True)
    winner = ranked[0]

    key_signals: dict[str, Any] = {
        "avg_rps": avg_rps,
        "burst_ratio": burst_ratio,
        "crossover_rps": crossover,
        "lambda_monthly_cost_usd": lambda_cost,
        "knative_monthly_cost_usd": knative_cost,
        "container_monthly_cost_usd": container_cost,
        "above_crossover": avg_rps > crossover,
        "latency_p99_ms": latency_p99_ms,
        "compliance_tags": sorted(compliance_tags),
        "stateful_workloads": stateful,
        "event_driven_fraction": round(event_frac, 2),
        "cost_source": "sizer_output" if real_container > 0.0 else "heuristic",
    }

    narrative = (
        f"The architecture selector evaluated four patterns for a workload averaging "
        f"{avg_rps:.0f} RPS with {burst_ratio:.1f}× burst capacity.\n\n"
        f"**Winner**: {winner.label} (composite score: {winner.score:.3f}).\n"
        f"{winner.rationale}\n\n"
        f"**Cost crossover** (Lambda vs Knative): Lambda becomes more expensive at "
        f"~{crossover:.0f} RPS sustained average. Current avg_rps is "
        f"{'above' if avg_rps > crossover else 'below'} crossover.\n\n"
        f"**Runner-up**: {ranked[1].label} (score: {ranked[1].score:.3f}). "
        f"{ranked[1].rationale}"
    )

    log.info(
        "architecture_selection_completed",
        winner=winner.name,
        winner_score=winner.score,
        avg_rps=avg_rps,
        crossover_rps=crossover,
        above_crossover=avg_rps > crossover,
    )

    return ArchitectureRecommendation(
        winner=winner,
        ranked=ranked,
        weights_used=w,
        key_signals=key_signals,
        recommendation_rationale=narrative,
    )
