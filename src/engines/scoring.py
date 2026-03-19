"""Multi-criteria scoring engine for SKU selection.

Evaluates candidate compute SKUs against workload requirements
using a weighted scoring model that balances cost, fit, and
architectural preferences.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass

from src.models.cloud_resource import ComputeSKU
from src.models.workload import VMWorkload

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
    """A compute SKU annotated with its computed score."""

    sku: ComputeSKU
    total_score: float
    cost_score: float
    cpu_fit_score: float
    memory_fit_score: float
    generation_score: float


def _cost_score(sku: ComputeSKU, max_price: float) -> float:
    """Score inversely proportional to cost (cheaper = better).

    Args:
        sku: Candidate SKU.
        max_price: Maximum hourly price in the candidate set.

    Returns:
        Score between 0.0 and 1.0.
    """
    if max_price <= 0:
        return 1.0
    return round(1.0 - (sku.price_per_hour_usd / max_price), 4)


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


def _generation_score(sku: ComputeSKU) -> float:
    """Prefer newer processor generations.

    Simple heuristic: if generation info is available and contains
    a numeric suffix, higher numbers score better.

    Args:
        sku: Candidate SKU.

    Returns:
        Score between 0.0 and 1.0.
    """
    if not sku.generation:
        return 0.5  # Neutral score for unknown generation
    # Extract trailing digits
    digits = "".join(c for c in sku.generation if c.isdigit())
    if not digits:
        return 0.5
    gen_num = int(digits)
    # Normalize assuming generations range 1–10
    return round(min(gen_num / 10.0, 1.0), 4)


def score_skus(
    workload: VMWorkload,
    candidates: list[ComputeSKU],
    weights: ScoringWeights | None = None,
) -> list[ScoredSKU]:
    """Score and rank candidate SKUs for a VM workload.

    Args:
        workload: The VM workload requirement.
        candidates: List of compute SKUs to evaluate.
        weights: Optional scoring weights override.

    Returns:
        List of ScoredSKU sorted by total_score descending (best first).
    """
    if weights is None:
        weights = ScoringWeights()

    # Filter out SKUs that cannot meet minimum requirements
    eligible = [
        sku for sku in candidates
        if sku.vcpus >= workload.vcpus
        and sku.memory_gb >= workload.memory_gb
        and (not workload.gpu_required or sku.gpu_count >= workload.gpu_count)
    ]

    if not eligible:
        logger.warning(
            "no_eligible_skus",
            workload=workload.name,
            vcpus=workload.vcpus,
            memory_gb=workload.memory_gb,
        )
        return []

    max_price = max(s.price_per_hour_usd for s in eligible) if eligible else 1.0

    scored: list[ScoredSKU] = []
    for sku in eligible:
        cs = _cost_score(sku, max_price)
        cpu_fs = _fit_score(workload.vcpus, sku.vcpus)
        mem_fs = _fit_score(workload.memory_gb, sku.memory_gb)
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
        top_sku=scored[0].sku.display_name if scored else "none",
    )

    return scored
