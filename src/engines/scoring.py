"""Multi-criteria scoring engine for SKU selection.

Evaluates candidate compute SKUs against workload requirements
using a weighted scoring model that balances cost, fit, and
architectural preferences.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass

from src.engines import (
    extract_generation,
    extract_gpu_count,
    extract_memory_gb,
    extract_vcpus,
)
from src.models.pricing import NormalizedPriceItem
from src.models.workload import WorkloadRequirement

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ScoringWeights:
    """Configurable weights for the scoring function.

    All weights should sum to 1.0 for normalized scoring.
    """

    cost: float = 0.40
    cpu_fit: float = 0.25
    memory_fit: float = 0.25
    generation: float = 0.10


@dataclass
class ScoredSKU:
    """A normalised price item annotated with its computed score."""

    sku: NormalizedPriceItem
    total_score: float
    cost_score: float
    cpu_fit_score: float
    memory_fit_score: float
    generation_score: float


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

        total = round(
            weights.cost * cs
            + weights.cpu_fit * cpu_fs
            + weights.memory_fit * mem_fs
            + weights.generation * gs,
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
