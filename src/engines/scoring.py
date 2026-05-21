"""Multi-criteria scoring engine for SKU selection.

Evaluates candidate compute SKUs against workload requirements
using a weighted scoring model that balances cost, fit, and
architectural preferences.

P17 — Processor Architecture Awareness:
    A 5th criterion (processor_architecture, 5%) rewards selecting
    ARM/Graviton instances for single-threaded/web/crypto workloads and
    x86 instances for multi-threaded/parallel workloads.
    Cost weight is reduced from 40% to 35% to accommodate this criterion.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field

from src.engines import (
    extract_generation,
    extract_gpu_count,
    extract_memory_gb,
    extract_vcpus,
)
from src.models.cloud_resource import ServiceCategory
from src.models.pricing import NormalizedPriceItem
from src.models.workload import WorkloadRequirement

logger = structlog.get_logger(__name__)

# ─── Graviton / Architecture Detection Constants ────────────

# AWS Graviton (ARM) instance family prefixes.  Any SKU name whose first
# segment (before the first ".") starts with one of these is Graviton.
_GRAVITON_PREFIXES: frozenset[str] = frozenset(
    {"c8g", "m8g", "r8g", "t4g", "x2g"}
)

# Workload name / notes keywords that indicate the workload benefits from SMT
# (simultaneous multi-threading) and should prefer x86.
_SMT_REQUIRED_KEYWORDS: tuple[str, ...] = (
    "parallel",
    "multi-thread",
    "multithread",
    "multi_thread",
    "concurrent",
    "batch",
    "hpc",
    "simulation",
)

# Workload name / notes keywords that indicate the workload is well-suited to
# Graviton (single-threaded, cache-heavy, or network-bound).
_GRAVITON_SUITABLE_KEYWORDS: tuple[str, ...] = (
    "web",
    "api",
    "http",
    "rest",
    "graphql",
    "crypto",
    "cache",
    "cdn",
    "static",
    "gateway",
    "proxy",
    "frontend",
)

# concurrent_users threshold above which SMT is considered required
_SMT_CONCURRENT_USER_THRESHOLD: int = 500

# Only apply architecture scoring to these categories
_ARCH_SCORED_CATEGORIES: frozenset[ServiceCategory] = frozenset(
    {ServiceCategory.COMPUTE, ServiceCategory.AI_ML}
)


@dataclass(frozen=True)
class ScoringWeights:
    """Configurable weights for the scoring function.

    All weights should sum to 1.0 for normalized scoring.

    P17: cost reduced 40% → 35%; processor_architecture 5% added.
    """

    cost: float = 0.35
    cpu_fit: float = 0.25
    memory_fit: float = 0.25
    generation: float = 0.10
    processor_architecture: float = 0.05


@dataclass
class ScoredSKU:
    """A normalised price item annotated with its computed score."""

    sku: NormalizedPriceItem
    total_score: float
    cost_score: float
    cpu_fit_score: float
    memory_fit_score: float
    generation_score: float
    architecture_score: float = field(default=0.85)
    arch_type: str = field(default="unknown")  # "graviton" | "x86" | "unknown"


def _cost_score(sku: NormalizedPriceItem, max_price: float) -> float:
    """Score inversely proportional to cost (cheaper = better).

    Args:
        sku: Candidate SKU.
        max_price: Maximum hourly price in the candidate set.

    Returns:
        Score between 0.0 and 1.0.
    """
    if max_price <= 0:
        return 1.0
    return round(1.0 - (sku.unit_price / max_price), 4)


def _fit_score(required: float, available: float) -> float:
    """Score based on how tightly a resource matches the requirement.

    Perfect fit (1:1) scores highest.  Over-provisioning is penalised
    to discourage wasteful selections.

    Args:
        required: Workload requirement (e.g., vCPUs).
        available: SKU capacity (e.g., vCPUs).

    Returns:
        Score between 0.0 and 1.0.
    """
    if available < required:
        return 0.0  # SKU cannot satisfy the requirement
    ratio = required / available
    # Penalise over-provisioning: score drops as ratio deviates from 1.0
    return round(ratio, 4)


def _generation_score(sku: NormalizedPriceItem) -> float:
    """Prefer newer processor generations.

    Simple heuristic: if generation info is available and contains
    a numeric suffix, higher numbers score better.  AWS-style
    ``currentGeneration=Yes`` maps to ``"current"`` and scores 0.8.

    Args:
        sku: Candidate SKU.

    Returns:
        Score between 0.0 and 1.0.
    """
    gen = extract_generation(sku)
    if not gen:
        return 0.5  # Neutral score for unknown generation
    if gen == "current":
        return 0.8  # AWS currentGeneration=Yes
    if gen == "previous":
        return 0.3  # AWS currentGeneration=No
    # Extract trailing digits
    digits = "".join(c for c in gen if c.isdigit())
    if not digits:
        return 0.5
    gen_num = int(digits)
    # Normalize assuming generations range 1–10
    return round(min(gen_num / 10.0, 1.0), 4)


def _detect_processor_architecture(
    workload: WorkloadRequirement,
    sku_name: str,
) -> tuple[str, float]:
    """Detect processor architecture fit between a workload and a SKU.

    Awards a higher score when the SKU's processor architecture (ARM/Graviton
    or x86) matches the workload's threading requirements:

    - Graviton (ARM) instances deliver better single-threaded performance and
      lower energy consumption but lack SMT.  Ideal for web/API/crypto/cache
      workloads.
    - x86 instances support SMT (hyperthreading) and handle multi-threaded /
      high-concurrency workloads better, especially past the ~60 % CPU load
      "breaking latency" threshold.

    Only applied to COMPUTE and AI_ML categories; all other categories receive
    a neutral score of 0.85.

    Scoring table:
        Graviton-suitable workload + ARM SKU  → 1.00
        Graviton-suitable workload + x86 SKU  → 0.70
        SMT-required workload + x86 SKU       → 1.00
        SMT-required workload + ARM SKU       → 0.50
        No clear signal                        → 0.85  (slight Graviton lean)

    Args:
        workload: The workload requirement being scored.
        sku_name: The SKU name string (e.g. "c8g.large", "c5.large").

    Returns:
        Tuple of (arch_type, score) where arch_type is "graviton", "x86",
        or "unknown", and score is between 0.0 and 1.0.
    """
    # Only scored for COMPUTE / AI_ML
    if workload.suggested_category not in _ARCH_SCORED_CATEGORIES:
        return ("unknown", 0.85)

    # Determine SKU architecture from family prefix
    family = sku_name.split(".")[0].lower() if "." in sku_name else sku_name.lower()
    is_graviton = family in _GRAVITON_PREFIXES

    # Classify workload threading preference from name + notes + resources
    text = (f"{workload.name} {workload.notes or ''}").lower()
    concurrent = workload.concurrent_users or 0

    smt_required = (
        any(kw in text for kw in _SMT_REQUIRED_KEYWORDS)
        or concurrent > _SMT_CONCURRENT_USER_THRESHOLD
    )
    graviton_suitable = any(kw in text for kw in _GRAVITON_SUITABLE_KEYWORDS)

    arch_type = "graviton" if is_graviton else "x86"

    if graviton_suitable and not smt_required:
        score = 1.0 if is_graviton else 0.7
    elif smt_required and not graviton_suitable:
        score = 0.5 if is_graviton else 1.0
    else:
        # Mixed signals or no signal — neutral, slight preference for Graviton
        score = 0.85

    logger.debug(
        "processor_architecture_detected",
        workload=workload.name,
        sku=sku_name,
        arch_type=arch_type,
        graviton_suitable=graviton_suitable,
        smt_required=smt_required,
        concurrent_users=concurrent,
        score=score,
    )
    return (arch_type, score)


def score_skus(
    workload: WorkloadRequirement,
    candidates: list[NormalizedPriceItem],
    weights: ScoringWeights | None = None,
) -> list[ScoredSKU]:
    """Score and rank candidate SKUs for a workload requirement.

    Args:
        workload: The workload requirement to match against.
        candidates: List of normalised price items to evaluate.
        weights: Optional scoring weights override.

    Returns:
        List of ScoredSKU sorted by total_score descending (best first).
    """
    if weights is None:
        weights = ScoringWeights()

    # Extract workload resource needs
    req_vcpus = workload.resources.vcpus or 0
    req_memory_gb = workload.resources.memory_gb or 0.0
    req_gpu = workload.resources.gpu_count or 0

    # Filter out SKUs that cannot meet minimum requirements
    eligible: list[NormalizedPriceItem] = []
    for sku in candidates:
        sku_vcpus = extract_vcpus(sku)
        sku_memory = extract_memory_gb(sku)
        sku_gpu = extract_gpu_count(sku)
        if (
            sku_vcpus >= req_vcpus
            and sku_memory >= req_memory_gb
            and (req_gpu == 0 or sku_gpu >= req_gpu)
        ):
            eligible.append(sku)

    if not eligible:
        logger.warning(
            "no_eligible_skus",
            workload=workload.name,
            vcpus=req_vcpus,
            memory_gb=req_memory_gb,
        )
        return []

    max_price = max(s.unit_price for s in eligible) if eligible else 1.0

    scored: list[ScoredSKU] = []
    for sku in eligible:
        cs = _cost_score(sku, max_price)
        cpu_fs = _fit_score(req_vcpus, extract_vcpus(sku))
        mem_fs = _fit_score(req_memory_gb, extract_memory_gb(sku))
        gs = _generation_score(sku)
        arch_type, arch_s = _detect_processor_architecture(workload, sku.sku_name)

        total = round(
            weights.cost * cs
            + weights.cpu_fit * cpu_fs
            + weights.memory_fit * mem_fs
            + weights.generation * gs
            + weights.processor_architecture * arch_s,
            4,
        )

        scored.append(
            ScoredSKU(
                sku=sku,
                total_score=total,
                cost_score=cs,
                cpu_fit_score=cpu_fs,
                memory_fit_score=mem_fs,
                generation_score=gs,
                architecture_score=arch_s,
                arch_type=arch_type,
            )
        )

    scored.sort(key=lambda s: s.total_score, reverse=True)

    logger.info(
        "sku_scoring_complete",
        workload=workload.name,
        candidates=len(candidates),
        eligible=len(eligible),
        top_sku=scored[0].sku.sku_name if scored else "none",
    )

    return scored
