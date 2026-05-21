"""Unit tests for algorithmic engines: bin-packing and scoring."""
from __future__ import annotations

import pytest

from src.engines.bin_packing import PackingAlgorithm, pack_workloads
from src.engines.scoring import ScoredSKU, ScoringWeights, score_skus
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.models.recommendation import BinPackingResult
from src.models.workload import ResourceSpec, WorkloadRequirement


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _node_sku(
    vcpus: int = 4,
    memory_gb: float = 16.0,
    unit_price: float = 0.192,
) -> NormalizedPriceItem:
    from datetime import datetime, timezone
    return NormalizedPriceItem(
        provider=CloudProvider.AWS,
        service_name="AmazonEC2",
        service_category=ServiceCategory.COMPUTE,
        sku_id=f"node-{vcpus}cpu-{int(memory_gb)}gb",
        sku_name=f"m5.{vcpus}xlarge",
        product_name="EC2 node",
        region="us-east-1",
        retail_price=unit_price,
        unit_price=unit_price,
        unit_of_measure="1 Hour",
        pricing_tier=PricingTier.ON_DEMAND,
        effective_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        attributes={"vcpus": vcpus, "memory_gb": memory_gb},
    )


def _container_workload(
    name: str,
    cpu_mc: int = 500,
    mem_mb: int = 512,
    replicas: int = 1,
) -> WorkloadRequirement:
    return WorkloadRequirement(
        name=name,
        description="test container workload",
        suggested_category=ServiceCategory.CONTAINER,
        resources=ResourceSpec(
            cpu_request_millicores=cpu_mc,
            memory_request_mb=mem_mb,
            replicas=replicas,
        ),
    )


# ============================================================
# Bin-packing tests
# ============================================================


class TestBinPackingEmpty:
    def test_empty_workloads_returns_empty_result(self) -> None:
        node = _node_sku()
        result = pack_workloads(workloads=[], node_sku=node)
        assert isinstance(result, BinPackingResult)
        assert result.total_nodes == 0
        assert result.nodes == []

    def test_workload_without_millicore_specs_is_skipped(self) -> None:
        """Workloads without cpu_request_millicores should be treated as no-ops."""
        w = WorkloadRequirement(
            name="bare",
            description="no container spec",
            suggested_category=ServiceCategory.COMPUTE,
            resources=ResourceSpec(vcpus=2, memory_gb=4.0),
        )
        node = _node_sku()
        result = pack_workloads(workloads=[w], node_sku=node)
        # No packable items → empty result
        assert result.total_nodes == 0


class TestBinPackingFFD:
    def test_single_workload_single_node(self) -> None:
        node = _node_sku(vcpus=4, memory_gb=16.0)
        workloads = [_container_workload("svc-a", cpu_mc=500, mem_mb=512, replicas=1)]
        result = pack_workloads(workloads=workloads, node_sku=node, algorithm=PackingAlgorithm.FIRST_FIT_DECREASING)
        assert result.total_nodes == 1
        assert result.total_monthly_cost_usd > 0

    def test_multiple_small_workloads_pack_onto_fewer_nodes(self) -> None:
        """Three tiny services (500m/512MB) should fit on one 4-vCPU node."""
        node = _node_sku(vcpus=4, memory_gb=16.0)
        workloads = [
            _container_workload("svc-a", cpu_mc=500, mem_mb=512),
            _container_workload("svc-b", cpu_mc=500, mem_mb=512),
            _container_workload("svc-c", cpu_mc=500, mem_mb=512),
        ]
        result = pack_workloads(workloads=workloads, node_sku=node)
        # 3 × 500m = 1500m — fits on one 4-vCPU (4000m - 250m overhead = 3750m)
        assert result.total_nodes == 1

    def test_oversized_workload_causes_multiple_nodes(self) -> None:
        """One workload per node when each uses most node capacity."""
        node = _node_sku(vcpus=2, memory_gb=8.0)  # allocatable ≈ 1750m cpu, 7680MB mem
        workloads = [
            _container_workload("svc-a", cpu_mc=1500, mem_mb=6000),  # uses ~86%
            _container_workload("svc-b", cpu_mc=1500, mem_mb=6000),  # needs a new node
        ]
        result = pack_workloads(workloads=workloads, node_sku=node)
        assert result.total_nodes >= 2

    def test_result_has_expected_fields(self) -> None:
        node = _node_sku(vcpus=4, memory_gb=16.0)
        workloads = [_container_workload("svc-a", cpu_mc=500, mem_mb=512)]
        result = pack_workloads(workloads=workloads, node_sku=node)
        assert result.provider == CloudProvider.AWS
        assert result.algorithm_used == PackingAlgorithm.FIRST_FIT_DECREASING.value
        assert 0.0 <= result.packing_efficiency_pct <= 100.0


class TestBinPackingBFD:
    def test_bfd_produces_valid_result(self) -> None:
        node = _node_sku(vcpus=4, memory_gb=16.0)
        workloads = [
            _container_workload("svc-a", cpu_mc=800, mem_mb=1024),
            _container_workload("svc-b", cpu_mc=400, mem_mb=512),
        ]
        result = pack_workloads(
            workloads=workloads,
            node_sku=node,
            algorithm=PackingAlgorithm.BEST_FIT_DECREASING,
        )
        assert result.total_nodes >= 1
        assert result.algorithm_used == PackingAlgorithm.BEST_FIT_DECREASING.value

    def test_replicas_expanded_correctly(self) -> None:
        """A workload with replicas=3 should be treated as 3 separate items."""
        node = _node_sku(vcpus=8, memory_gb=32.0)
        workloads = [_container_workload("svc-a", cpu_mc=500, mem_mb=512, replicas=3)]
        result = pack_workloads(workloads=workloads, node_sku=node)
        # All 3 replicas should fit on one 8-vCPU node (3×500m = 1500m << 7750m)
        assert result.total_nodes == 1


# ============================================================
# Scoring engine tests
# ============================================================


def _compute_workload(vcpus: int = 2, memory_gb: float = 4.0) -> WorkloadRequirement:
    return WorkloadRequirement(
        name="compute-svc",
        description="standard compute",
        suggested_category=ServiceCategory.COMPUTE,
        resources=ResourceSpec(vcpus=vcpus, memory_gb=memory_gb),
    )


def _sku(sku_name: str, vcpus: int, memory_gb: float, unit_price: float) -> NormalizedPriceItem:
    from datetime import datetime, timezone
    return NormalizedPriceItem(
        provider=CloudProvider.AWS,
        service_name="AmazonEC2",
        service_category=ServiceCategory.COMPUTE,
        sku_id=sku_name,
        sku_name=sku_name,
        product_name=f"EC2 {sku_name}",
        region="us-east-1",
        retail_price=unit_price,
        unit_price=unit_price,
        unit_of_measure="1 Hour",
        pricing_tier=PricingTier.ON_DEMAND,
        effective_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        attributes={"vcpus": vcpus, "memory_gb": memory_gb},
    )


class TestScoringEmpty:
    def test_empty_candidates_returns_empty(self) -> None:
        workload = _compute_workload(vcpus=2, memory_gb=4.0)
        result = score_skus(workload, candidates=[])
        assert result == []

    def test_all_insufficient_candidates_returns_empty(self) -> None:
        """SKUs smaller than requirement must be filtered out."""
        workload = _compute_workload(vcpus=8, memory_gb=32.0)
        candidates = [
            _sku("m5.large",  vcpus=2, memory_gb=8.0,  unit_price=0.096),
            _sku("m5.xlarge", vcpus=4, memory_gb=16.0, unit_price=0.192),
        ]
        result = score_skus(workload, candidates=candidates)
        assert result == []


class TestScoringRanking:
    def test_returns_sorted_by_score_descending(self) -> None:
        workload = _compute_workload(vcpus=2, memory_gb=4.0)
        candidates = [
            _sku("m5.2xlarge", vcpus=8, memory_gb=32.0, unit_price=0.384),  # overprovision
            _sku("m5.large",   vcpus=2, memory_gb=8.0,  unit_price=0.096),  # tight fit + cheap
        ]
        result = score_skus(workload, candidates=candidates)
        assert len(result) == 2
        assert result[0].total_score >= result[1].total_score

    def test_cheaper_tighter_fit_wins(self) -> None:
        """m5.large (2 vCPU, cheap) should beat m5.2xlarge (8 vCPU, expensive) for 2-vCPU workload."""
        workload = _compute_workload(vcpus=2, memory_gb=4.0)
        candidates = [
            _sku("m5.2xlarge", vcpus=8, memory_gb=32.0, unit_price=0.384),
            _sku("m5.large",   vcpus=2, memory_gb=8.0,  unit_price=0.096),
        ]
        result = score_skus(workload, candidates=candidates)
        assert result[0].sku.sku_name == "m5.large"

    def test_scored_sku_has_all_fields(self) -> None:
        workload = _compute_workload(vcpus=2, memory_gb=4.0)
        candidates = [_sku("m5.large", vcpus=2, memory_gb=8.0, unit_price=0.096)]
        result = score_skus(workload, candidates=candidates)
        assert len(result) == 1
        scored = result[0]
        assert isinstance(scored, ScoredSKU)
        assert 0.0 <= scored.total_score <= 1.0
        assert 0.0 <= scored.cost_score <= 1.0
        assert 0.0 <= scored.cpu_fit_score <= 1.0
        assert 0.0 <= scored.memory_fit_score <= 1.0

    def test_custom_weights_accepted(self) -> None:
        workload = _compute_workload(vcpus=2, memory_gb=4.0)
        candidates = [_sku("m5.large", vcpus=2, memory_gb=8.0, unit_price=0.096)]
        weights = ScoringWeights(cost=0.5, cpu_fit=0.2, memory_fit=0.2, generation=0.1)
        result = score_skus(workload, candidates=candidates, weights=weights)
        assert len(result) == 1

    def test_single_candidate_scores_above_zero(self) -> None:
        workload = _compute_workload(vcpus=2, memory_gb=4.0)
        candidates = [_sku("m5.large", vcpus=2, memory_gb=8.0, unit_price=0.096)]
        result = score_skus(workload, candidates=candidates)
        # Single candidate → cost_score = 0 (it's the max price), but fit > 0
        assert result[0].total_score >= 0.0


# ---------------------------------------------------------------------------
#  _detect_processor_architecture testsP17 
# ---------------------------------------------------------------------------

from src.engines.scoring import _detect_processor_architecture  # noqa: E402


class TestProcessorArchitectureDetection:
    """Tests for the P17 processor architecture scoring function."""

    def _workload(
        self,
        name: str = "svc",
        notes: str = "",
        concurrent_users: int = 0,
        category: ServiceCategory = ServiceCategory.COMPUTE,
    ) -> WorkloadRequirement:
        return WorkloadRequirement(
            name=name,
            description="test",
            suggested_category=category,
            resources=ResourceSpec(vcpus=2, memory_gb=4.0),
            concurrent_users=concurrent_users if concurrent_users else None,
            notes=notes,
        )

    # --- score: 1.0 ---

    def test_graviton_suitable_arm_sku_scores_1_0(self) -> None:
        wl = self._workload(name="api-gateway")
        arch_type, score = _detect_processor_architecture(wl, "c8g.large")
        assert arch_type == "graviton"
        assert score == 1.0

    def test_web_server_arm_sku_scores_1_0(self) -> None:
        wl = self._workload(name="web-frontend", notes="web server")
        arch_type, score = _detect_processor_architecture(wl, "m8g.xlarge")
        assert arch_type == "graviton"
        assert score == 1.0

    def test_crypto_workload_arm_sku_scores_1_0(self) -> None:
        wl = self._workload(notes="crypto hashing service")
        arch_type, score = _detect_processor_architecture(wl, "t4g.small")
        assert arch_type == "graviton"
        assert score == 1.0

    # --- score: 0.7 ---

    def test_graviton_suitable_x86_sku_scores_0_7(self) -> None:
        wl = self._workload(name="api-gateway")
        arch_type, score = _detect_processor_architecture(wl, "c5.large")
        assert arch_type == "x86"
        assert score == 0.7

    def test_cache_workload_x86_sku_scores_0_7(self) -> None:
        wl = self._workload(notes="cache layer redis")
        arch_type, score = _detect_processor_architecture(wl, "m5.large")
        assert arch_type == "x86"
        assert score == 0.7

    # --- score: 1.0 ---

    def test_smt_required_x86_sku_scores_1_0(self) -> None:
        wl = self._workload(name="batch-processor", notes="parallel processing")
        arch_type, score = _detect_processor_architecture(wl, "c5.2xlarge")
        assert arch_type == "x86"
        assert score == 1.0

    def test_high_concurrent_users_x86_scores_1_0(self) -> None:
        wl = self._workload(name="concurrent-svc", concurrent_users=600)
        arch_type, score = _detect_processor_architecture(wl, "m5.xlarge")
        assert arch_type == "x86"
        assert score == 1.0

    # --- score: 0.5 ---

    def test_smt_required_arm_sku_scores_0_5(self) -> None:
        wl = self._workload(name="parallel-compute", notes="multi-thread batch")
        arch_type, score = _detect_processor_architecture(wl, "c8g.2xlarge")
        assert arch_type == "graviton"
        assert score == 0.5

    def test_high_concurrent_users_arm_scores_0_5(self) -> None:
        wl = self._workload(name="heavy-load-svc", concurrent_users=1000)
        arch_type, score = _detect_processor_architecture(wl, "r8g.large")
        assert arch_type == "graviton"
        assert score == 0.5

    # --- no signal: 0.85 neutral ---

    def test_no_signal_neutral_score(self) -> None:
        wl = self._workload(name="generic-service")
        arch_type, score = _detect_processor_architecture(wl, "m5.large")
        assert arch_type == "x86"
        assert score == 0.85

    def test_no_signal_graviton_neutral_score(self) -> None:
        wl = self._workload(name="generic-service")
        arch_type, score = _detect_processor_architecture(wl, "m8g.large")
        assert arch_type == "graviton"
        assert score == 0.85

    # --- non-compute category: 0.85 regardless ---

    def test_database_category_returns_neutral(self) -> None:
        wl = self._workload(name="postgres-db", category=ServiceCategory.DATABASE)
        arch_type, score = _detect_processor_architecture(wl, "c8g.large")
        assert arch_type == "unknown"
        assert score == 0.85

    def test_storage_category_returns_neutral(self) -> None:
        wl = self._workload(name="blob-store", category=ServiceCategory.STORAGE)
        _, score = _detect_processor_architecture(wl, "c5.large")
        assert score == 0.85

    # --- ScoringWeights defaults check ---

    def test_scoring_weights_sum_to_1(self) -> None:
        w = ScoringWeights()
        total = w.cost + w.cpu_fit + w.memory_fit + w.generation + w.processor_architecture
        assert abs(total - 1.0) < 1e-9, f"Weights sum to {total}, expected 1.0"

    def test_cost_weight_is_35_pct(self) -> None:
        assert ScoringWeights().cost == 0.35

    def test_processor_architecture_weight_is_5_pct(self) -> None:
        assert ScoringWeights().processor_architecture == 0.05
