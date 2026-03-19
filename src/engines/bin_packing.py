"""Bin-packing algorithms for Kubernetes node pool optimization.

Implements First-Fit Decreasing (FFD) and Best-Fit Decreasing (BFD)
algorithms to pack container workloads onto the smallest possible
set of compute nodes, maximising resource density and minimising cost.

References:
    [4] E. G. Coffman et al., "Bin Packing Approximation Algorithms:
        Survey and Classification," Handbook of Combinatorial Optimization,
        Springer, 2013, pp. 455–531.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.models.cloud_resource import CloudProvider, ComputeSKU
from src.models.recommendation import BinPackingResult, PackedNode
from src.models.workload import ContainerWorkload

logger = structlog.get_logger(__name__)


class PackingAlgorithm(str, Enum):
    """Supported bin-packing strategies."""

    FIRST_FIT_DECREASING = "first-fit-decreasing"
    BEST_FIT_DECREASING = "best-fit-decreasing"


# ─── Internal Helpers ───────────────────────────────────────

# System overhead reserved on each node (kubelet, OS, daemonsets).
NODE_CPU_OVERHEAD_MILLICORES = 250
NODE_MEMORY_OVERHEAD_MB = 512


@dataclass
class _Bin:
    """Internal mutable representation of a node during packing."""

    sku: ComputeSKU
    remaining_cpu_mc: int
    remaining_mem_mb: int
    assigned: list[str] = field(default_factory=list)

    @property
    def allocatable_cpu_mc(self) -> int:
        """Total allocatable CPU after system overhead."""
        return (self.sku.vcpus * 1000) - NODE_CPU_OVERHEAD_MILLICORES

    @property
    def allocatable_mem_mb(self) -> int:
        """Total allocatable memory after system overhead."""
        return int(self.sku.memory_gb * 1024) - NODE_MEMORY_OVERHEAD_MB

    def fits(self, cpu_mc: int, mem_mb: int) -> bool:
        """Check whether a workload fits in the remaining capacity."""
        return self.remaining_cpu_mc >= cpu_mc and self.remaining_mem_mb >= mem_mb

    def place(self, workload_name: str, cpu_mc: int, mem_mb: int) -> None:
        """Place a workload into this bin."""
        self.remaining_cpu_mc -= cpu_mc
        self.remaining_mem_mb -= mem_mb
        self.assigned.append(workload_name)

    def waste(self) -> tuple[int, int]:
        """Return (wasted_cpu_mc, wasted_mem_mb)."""
        return self.remaining_cpu_mc, self.remaining_mem_mb

    def utilization(self) -> tuple[float, float]:
        """Return (cpu_util_pct, mem_util_pct)."""
        total_cpu = self.allocatable_cpu_mc
        total_mem = self.allocatable_mem_mb
        cpu_pct = ((total_cpu - self.remaining_cpu_mc) / total_cpu * 100) if total_cpu else 0
        mem_pct = ((total_mem - self.remaining_mem_mb) / total_mem * 100) if total_mem else 0
        return round(cpu_pct, 2), round(mem_pct, 2)


def _new_bin(sku: ComputeSKU) -> _Bin:
    """Create a fresh bin from a compute SKU."""
    allocatable_cpu = (sku.vcpus * 1000) - NODE_CPU_OVERHEAD_MILLICORES
    allocatable_mem = int(sku.memory_gb * 1024) - NODE_MEMORY_OVERHEAD_MB
    return _Bin(
        sku=sku,
        remaining_cpu_mc=max(allocatable_cpu, 0),
        remaining_mem_mb=max(allocatable_mem, 0),
    )


# ─── Item Representation ───────────────────────────────────

@dataclass
class _Item:
    """A single unit of demand (one replica of a container workload)."""

    name: str
    cpu_mc: int
    mem_mb: int

    @property
    def sort_key(self) -> int:
        """Primary sort: largest CPU first, then largest memory."""
        return self.cpu_mc * 10_000 + self.mem_mb


def _expand_workloads(workloads: list[ContainerWorkload]) -> list[_Item]:
    """Expand workloads × replicas into individual packable items."""
    items: list[_Item] = []
    for wl in workloads:
        for r in range(wl.replicas):
            items.append(
                _Item(
                    name=f"{wl.name}-replica-{r}",
                    cpu_mc=wl.cpu_request_millicores,
                    mem_mb=wl.memory_request_mb,
                )
            )
    # Sort descending by size (decreasing order for FFD/BFD).
    items.sort(key=lambda i: i.sort_key, reverse=True)
    return items


# ─── Packing Algorithms ────────────────────────────────────

def _first_fit_decreasing(items: list[_Item], sku: ComputeSKU) -> list[_Bin]:
    """First-Fit Decreasing bin-packing.

    Place each item (sorted largest-first) into the *first* bin
    that has enough remaining capacity.  Open a new bin only when
    no existing bin can accommodate the item.

    Args:
        items: Pre-sorted list of workload items (descending).
        sku: The node SKU template to use for new bins.

    Returns:
        List of bins with workloads assigned.
    """
    bins: list[_Bin] = []
    for item in items:
        placed = False
        for b in bins:
            if b.fits(item.cpu_mc, item.mem_mb):
                b.place(item.name, item.cpu_mc, item.mem_mb)
                placed = True
                break
        if not placed:
            new = _new_bin(sku)
            if not new.fits(item.cpu_mc, item.mem_mb):
                logger.warning(
                    "workload_exceeds_node",
                    workload=item.name,
                    cpu_mc=item.cpu_mc,
                    mem_mb=item.mem_mb,
                    node_cpu_mc=new.remaining_cpu_mc,
                    node_mem_mb=new.remaining_mem_mb,
                )
                continue  # Skip items that cannot fit any single node
            new.place(item.name, item.cpu_mc, item.mem_mb)
            bins.append(new)
    return bins


def _best_fit_decreasing(items: list[_Item], sku: ComputeSKU) -> list[_Bin]:
    """Best-Fit Decreasing bin-packing.

    Place each item into the bin where it fits *most tightly*
    (least remaining capacity after placement), reducing fragmentation.

    Args:
        items: Pre-sorted list of workload items (descending).
        sku: The node SKU template to use for new bins.

    Returns:
        List of bins with workloads assigned.
    """
    bins: list[_Bin] = []
    for item in items:
        best_bin: Optional[_Bin] = None
        best_remaining = float("inf")

        for b in bins:
            if b.fits(item.cpu_mc, item.mem_mb):
                remaining = (b.remaining_cpu_mc - item.cpu_mc) + (
                    b.remaining_mem_mb - item.mem_mb
                )
                if remaining < best_remaining:
                    best_remaining = remaining
                    best_bin = b

        if best_bin is not None:
            best_bin.place(item.name, item.cpu_mc, item.mem_mb)
        else:
            new = _new_bin(sku)
            if not new.fits(item.cpu_mc, item.mem_mb):
                logger.warning(
                    "workload_exceeds_node",
                    workload=item.name,
                    cpu_mc=item.cpu_mc,
                    mem_mb=item.mem_mb,
                )
                continue
            new.place(item.name, item.cpu_mc, item.mem_mb)
            bins.append(new)
    return bins


# ─── Public API ─────────────────────────────────────────────

def pack_workloads(
    workloads: list[ContainerWorkload],
    node_sku: ComputeSKU,
    algorithm: PackingAlgorithm = PackingAlgorithm.FIRST_FIT_DECREASING,
    pool_name: str = "default-pool",
) -> BinPackingResult:
    """Pack container workloads onto nodes using the chosen algorithm.

    This is the primary entry point for the bin-packing engine.

    Args:
        workloads: Container workloads to pack.
        node_sku: The compute SKU to use as the node template.
        algorithm: Packing strategy (FFD or BFD).
        pool_name: Kubernetes node pool name for the result.

    Returns:
        BinPackingResult with nodes, utilisation, and cost data.
    """
    items = _expand_workloads(workloads)

    if not items:
        logger.info("no_items_to_pack")
        return BinPackingResult(
            provider=node_sku.provider,
            node_pool_name=pool_name,
            algorithm_used=algorithm.value,
        )

    logger.info(
        "bin_packing_start",
        algorithm=algorithm.value,
        total_items=len(items),
        node_sku=node_sku.display_name,
    )

    if algorithm == PackingAlgorithm.FIRST_FIT_DECREASING:
        bins = _first_fit_decreasing(items, node_sku)
    else:
        bins = _best_fit_decreasing(items, node_sku)

    # --- Build result ---
    packed_nodes: list[PackedNode] = []
    total_wasted_cpu = 0
    total_wasted_mem = 0
    total_alloc_cpu = 0
    total_alloc_mem = 0

    for b in bins:
        cpu_util, mem_util = b.utilization()
        wasted_cpu, wasted_mem = b.waste()
        total_wasted_cpu += wasted_cpu
        total_wasted_mem += wasted_mem
        total_alloc_cpu += b.allocatable_cpu_mc
        total_alloc_mem += b.allocatable_mem_mb

        packed_nodes.append(
            PackedNode(
                node_sku=b.sku,
                assigned_workloads=b.assigned,
                cpu_utilization_pct=cpu_util,
                memory_utilization_pct=mem_util,
                wasted_cpu_millicores=wasted_cpu,
                wasted_memory_mb=wasted_mem,
            )
        )

    packing_efficiency = 0.0
    if total_alloc_cpu > 0 and total_alloc_mem > 0:
        cpu_eff = (total_alloc_cpu - total_wasted_cpu) / total_alloc_cpu * 100
        mem_eff = (total_alloc_mem - total_wasted_mem) / total_alloc_mem * 100
        packing_efficiency = round((cpu_eff + mem_eff) / 2, 2)

    total_cost = round(len(bins) * node_sku.price_per_hour_usd * 730, 2)

    result = BinPackingResult(
        provider=node_sku.provider,
        node_pool_name=pool_name,
        nodes=packed_nodes,
        total_nodes=len(bins),
        packing_efficiency_pct=packing_efficiency,
        total_monthly_cost_usd=total_cost,
        algorithm_used=algorithm.value,
    )

    logger.info(
        "bin_packing_complete",
        total_nodes=result.total_nodes,
        efficiency_pct=result.packing_efficiency_pct,
        monthly_cost=result.total_monthly_cost_usd,
    )

    return result
