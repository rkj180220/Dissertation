"""Unit tests for profiler agent logic (pure functions, no LLM calls)."""
from __future__ import annotations

import pytest

from src.agents.profiler import (
    _estimate_resources,
    _guard_ai_ml,
    _heuristic_rationale,
    _resolve_category,
)
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.workload import (
    EnvironmentType,
    ResourceSpec,
    WorkloadRequirement,
    WorkloadTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workload(
    name: str = "test-svc",
    description: str = "",
    suggested_category: ServiceCategory = ServiceCategory.COMPUTE,
    resources: ResourceSpec | None = None,
    notes: str = "",
) -> WorkloadRequirement:
    return WorkloadRequirement(
        name=name,
        description=description,
        suggested_category=suggested_category,
        resources=resources or ResourceSpec(),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# _resolve_category
# ---------------------------------------------------------------------------


class TestResolveCategory:
    def test_cluster_management_fee_forces_kubernetes(self) -> None:
        w = _workload(
            name="Kubernetes Cluster Management",
            suggested_category=ServiceCategory.CONTAINER,
            notes="cluster_management_fee",
        )
        result = _resolve_category(w)
        assert result == ServiceCategory.KUBERNETES

    def test_kubernetes_suggested_returns_kubernetes(self) -> None:
        w = _workload(suggested_category=ServiceCategory.KUBERNETES)
        result = _resolve_category(w)
        assert result == ServiceCategory.KUBERNETES

    def test_container_keyword_resolves_container(self) -> None:
        w = _workload(
            name="K8s Pod",
            description="kubernetes pod workload",
            suggested_category=ServiceCategory.COMPUTE,
            resources=ResourceSpec(cpu_request_millicores=500),
        )
        result = _resolve_category(w)
        assert result == ServiceCategory.CONTAINER

    def test_database_keyword_resolves_database(self) -> None:
        w = _workload(
            name="PostgreSQL DB",
            description="managed database",
            suggested_category=ServiceCategory.COMPUTE,
        )
        result = _resolve_category(w)
        assert result == ServiceCategory.DATABASE

    def test_storage_keyword_resolves_storage(self) -> None:
        w = _workload(
            name="S3 Object Storage",
            description="blob storage bucket",
            suggested_category=ServiceCategory.COMPUTE,
        )
        result = _resolve_category(w)
        assert result == ServiceCategory.STORAGE

    def test_suggested_category_used_when_no_signal(self) -> None:
        w = _workload(
            name="my-svc",
            description="generic workload",
            suggested_category=ServiceCategory.NETWORKING,
        )
        result = _resolve_category(w)
        assert result == ServiceCategory.NETWORKING


# ---------------------------------------------------------------------------
# _guard_ai_ml
# ---------------------------------------------------------------------------


class TestGuardAiMl:
    def test_non_ai_ml_category_passes_through(self) -> None:
        w = _workload(suggested_category=ServiceCategory.COMPUTE)
        result = _guard_ai_ml(ServiceCategory.COMPUTE, w)
        assert result == ServiceCategory.COMPUTE

    def test_ai_ml_with_gpu_is_kept(self) -> None:
        w = _workload(
            name="GPU Training Job",
            resources=ResourceSpec(gpu_count=1),
            suggested_category=ServiceCategory.AI_ML,
        )
        result = _guard_ai_ml(ServiceCategory.AI_ML, w)
        assert result == ServiceCategory.AI_ML

    def test_ai_ml_with_ml_keyword_is_kept(self) -> None:
        w = _workload(
            name="machine learning pipeline",
            resources=ResourceSpec(gpu_count=0),
            suggested_category=ServiceCategory.COMPUTE,
        )
        result = _guard_ai_ml(ServiceCategory.AI_ML, w)
        assert result == ServiceCategory.AI_ML

    def test_ai_ml_without_gpu_or_keyword_reverts(self) -> None:
        """No GPU, no ML keywords → revert to suggested_category."""
        w = _workload(
            name="API Server",
            description="REST backend",
            resources=ResourceSpec(gpu_count=0),
            suggested_category=ServiceCategory.COMPUTE,
        )
        result = _guard_ai_ml(ServiceCategory.AI_ML, w)
        assert result == ServiceCategory.COMPUTE  # reverted to suggested

    def test_inference_keyword_keeps_ai_ml(self) -> None:
        w = _workload(
            name="inference endpoint",
            resources=ResourceSpec(gpu_count=0),
            suggested_category=ServiceCategory.AI_ML,
        )
        result = _guard_ai_ml(ServiceCategory.AI_ML, w)
        assert result == ServiceCategory.AI_ML


# ---------------------------------------------------------------------------
# _estimate_resources
# ---------------------------------------------------------------------------


class TestEstimateResources:
    def test_cluster_management_fee_returns_zero_resources(self) -> None:
        w = _workload(
            name="K8s Cluster",
            notes="cluster_management_fee",
            suggested_category=ServiceCategory.KUBERNETES,
        )
        result = _estimate_resources(
            w,
            resolved_category=ServiceCategory.KUBERNETES,
            tier=WorkloadTier.BUSINESS_CRITICAL,
            environment=EnvironmentType.PRODUCTION,
        )
        assert result["vcpus"] == 0
        assert result["memory_gb"] == 0.0
        assert result["storage_gb"] == 0.0
        assert result["requires_gpu"] is False

    def test_container_derives_from_millicore_specs(self) -> None:
        """500m × 3 replicas → 1.5 vCPU; 512MB × 3 → 1.5 GB (env factor × tier mult applied)."""
        w = _workload(
            name="Auth Service",
            suggested_category=ServiceCategory.CONTAINER,
            resources=ResourceSpec(
                cpu_request_millicores=500,
                memory_request_mb=512,
                replicas=3,
            ),
        )
        result = _estimate_resources(
            w,
            resolved_category=ServiceCategory.CONTAINER,
            tier=WorkloadTier.NON_CRITICAL,
            environment=EnvironmentType.PRODUCTION,
        )
        # 500m × 3 replicas = 1.5 vCPU → round() → 2; 512MB × 3 = 1536MB = 1.5 GB
        assert result["vcpus"] == pytest.approx(2, abs=1)  # round(1.5) = 2 (banker's rounding)
        assert result["memory_gb"] == pytest.approx(1.5, abs=0.1)

    def test_compute_applies_tier_multiplier(self) -> None:
        """MISSION_CRITICAL tier adds 1.5× compute multiplier."""
        w = _workload(
            name="API",
            suggested_category=ServiceCategory.COMPUTE,
            resources=ResourceSpec(vcpus=4, memory_gb=8.0),
        )
        result_mc = _estimate_resources(
            w,
            resolved_category=ServiceCategory.COMPUTE,
            tier=WorkloadTier.MISSION_CRITICAL,
            environment=EnvironmentType.PRODUCTION,
        )
        result_nc = _estimate_resources(
            w,
            resolved_category=ServiceCategory.COMPUTE,
            tier=WorkloadTier.NON_CRITICAL,
            environment=EnvironmentType.PRODUCTION,
        )
        assert result_mc["vcpus"] > result_nc["vcpus"]

    def test_staging_environment_reduces_resources(self) -> None:
        w = _workload(
            suggested_category=ServiceCategory.COMPUTE,
            resources=ResourceSpec(vcpus=4, memory_gb=8.0),
        )
        prod = _estimate_resources(
            w,
            resolved_category=ServiceCategory.COMPUTE,
            tier=WorkloadTier.BUSINESS_CRITICAL,
            environment=EnvironmentType.PRODUCTION,
        )
        staging = _estimate_resources(
            w,
            resolved_category=ServiceCategory.COMPUTE,
            tier=WorkloadTier.BUSINESS_CRITICAL,
            environment=EnvironmentType.STAGING,
        )
        assert staging["vcpus"] < prod["vcpus"]

    def test_gpu_detected_from_spec(self) -> None:
        w = _workload(
            name="ML Training",
            suggested_category=ServiceCategory.AI_ML,
            resources=ResourceSpec(gpu_count=2),
        )
        result = _estimate_resources(
            w,
            resolved_category=ServiceCategory.AI_ML,
            tier=WorkloadTier.BUSINESS_CRITICAL,
            environment=EnvironmentType.PRODUCTION,
        )
        assert result["requires_gpu"] is True

    def test_no_gpu_spec_returns_false(self) -> None:
        w = _workload(
            name="API",
            suggested_category=ServiceCategory.COMPUTE,
            resources=ResourceSpec(gpu_count=0),
        )
        result = _estimate_resources(
            w,
            resolved_category=ServiceCategory.COMPUTE,
            tier=WorkloadTier.BUSINESS_CRITICAL,
            environment=EnvironmentType.PRODUCTION,
        )
        assert result["requires_gpu"] is False

    def test_defaults_applied_when_no_spec(self) -> None:
        """When no resources given, defaults from _RESOURCE_DEFAULTS are used."""
        w = _workload(suggested_category=ServiceCategory.DATABASE)
        result = _estimate_resources(
            w,
            resolved_category=ServiceCategory.DATABASE,
            tier=WorkloadTier.NON_CRITICAL,
            environment=EnvironmentType.PRODUCTION,
        )
        # DATABASE default: vcpus=2, memory_gb=8.0
        assert result["vcpus"] >= 2
        assert result["memory_gb"] >= 8.0


# ---------------------------------------------------------------------------
# _heuristic_rationale
# ---------------------------------------------------------------------------


class TestHeuristicRationale:
    def test_returns_non_empty_string(self) -> None:
        w = _workload(name="API Server", suggested_category=ServiceCategory.COMPUTE)
        rationale = _heuristic_rationale(
            workload=w,
            resolved_category=ServiceCategory.COMPUTE,
            estimated={"vcpus": 2, "memory_gb": 4.0, "storage_gb": 50.0, "iops": None, "requires_gpu": False},
        )
        assert isinstance(rationale, str)
        assert len(rationale) > 10

    def test_contains_category_name(self) -> None:
        w = _workload(name="Database", suggested_category=ServiceCategory.DATABASE)
        rationale = _heuristic_rationale(
            workload=w,
            resolved_category=ServiceCategory.DATABASE,
            estimated={"vcpus": 2, "memory_gb": 8.0, "storage_gb": 100.0, "iops": None, "requires_gpu": False},
        )
        assert "database" in rationale.lower() or "DATABASE" in rationale
