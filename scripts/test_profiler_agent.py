#!/usr/bin/env python3
"""Test the Profiler Agent — verifies category resolution, resource estimation,
instance family selection, profile assembly, and the main node function.

Run: uv run python scripts/test_profiler_agent.py
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

from src.agents.profiler import (
    _build_component_profile,
    _estimate_resources,
    _get_instance_families,
    _heuristic_rationale,
    _resolve_category,
    _CATEGORY_SIGNALS,
    _ENVIRONMENT_FACTORS,
    _INSTANCE_FAMILY_MAP,
    _RESOURCE_DEFAULTS,
    _TIER_MULTIPLIERS,
    run_profiler_node,
)
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.conversation import ChatMessage, MessageRole
from src.models.workload import (
    ComponentProfile,
    EnvironmentType,
    ResourceSpec,
    ScalingPattern,
    WorkloadProfile,
    WorkloadRequest,
    WorkloadRequirement,
    WorkloadTier,
)
from src.orchestrator.state import (
    AgentExecution,
    AgentStatus,
    OrchestratorState,
    create_initial_state,
)

checks: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    """Run a check function and record the result."""
    try:
        fn()
        checks.append((name, True, ""))
    except AssertionError as e:
        checks.append((name, False, str(e)))
    except Exception as e:
        checks.append((name, False, f"{type(e).__name__}: {e}"))


# ═══════════════════════════════════════════════════════════════════
# 1. Category Resolution
# ═══════════════════════════════════════════════════════════════════


def test_resolve_category_by_resource_spec_database():
    """Database engine in ResourceSpec → DATABASE."""
    wl = WorkloadRequirement(
        name="PostgreSQL DB",
        suggested_category=ServiceCategory.COMPUTE,  # wrong hint
        resources=ResourceSpec(database_engine="postgresql"),
    )
    assert _resolve_category(wl) == ServiceCategory.DATABASE


check("resolve_category: resource_spec → DATABASE", test_resolve_category_by_resource_spec_database)


def test_resolve_category_by_resource_spec_container():
    """K8s fields in ResourceSpec → CONTAINER."""
    wl = WorkloadRequirement(
        name="API pod",
        suggested_category=ServiceCategory.COMPUTE,
        resources=ResourceSpec(cpu_request_millicores=500, memory_request_mb=512),
    )
    assert _resolve_category(wl) == ServiceCategory.CONTAINER


check("resolve_category: resource_spec → CONTAINER", test_resolve_category_by_resource_spec_container)


def test_resolve_category_by_resource_spec_gpu():
    """GPU in ResourceSpec → AI_ML."""
    wl = WorkloadRequirement(
        name="Training Job",
        suggested_category=ServiceCategory.COMPUTE,
        resources=ResourceSpec(gpu_count=2, vcpus=8, memory_gb=64),
    )
    assert _resolve_category(wl) == ServiceCategory.AI_ML


check("resolve_category: resource_spec → AI_ML (GPU)", test_resolve_category_by_resource_spec_gpu)


def test_resolve_category_by_keywords():
    """Keywords in name/description override suggested_category."""
    wl = WorkloadRequirement(
        name="S3 Object Storage",
        description="Store uploaded images and documents",
        suggested_category=ServiceCategory.COMPUTE,
        resources=ResourceSpec(storage_gb=5000),
    )
    # storage_gb alone without vcpus/db → STORAGE by resource check
    assert _resolve_category(wl) == ServiceCategory.STORAGE


check("resolve_category: keywords → STORAGE", test_resolve_category_by_keywords)


def test_resolve_category_fallback():
    """No signals → keep suggested_category."""
    wl = WorkloadRequirement(
        name="Custom Widget",
        description="Something unique",
        suggested_category=ServiceCategory.INTEGRATION,
        resources=ResourceSpec(),
    )
    assert _resolve_category(wl) == ServiceCategory.INTEGRATION


check("resolve_category: fallback keeps suggested", test_resolve_category_fallback)


def test_resolve_category_serverless():
    """Invocations per month → SERVERLESS_FUNCTION."""
    wl = WorkloadRequirement(
        name="Event Handler",
        suggested_category=ServiceCategory.COMPUTE,
        resources=ResourceSpec(invocations_per_month=1_000_000, avg_duration_ms=200, memory_mb=256),
    )
    assert _resolve_category(wl) == ServiceCategory.SERVERLESS_FUNCTION


check("resolve_category: invocations → SERVERLESS_FUNCTION", test_resolve_category_serverless)


# ═══════════════════════════════════════════════════════════════════
# 2. Resource Estimation
# ═══════════════════════════════════════════════════════════════════


def test_estimate_resources_defaults():
    """Missing values filled from _RESOURCE_DEFAULTS."""
    wl = WorkloadRequirement(
        name="DB",
        suggested_category=ServiceCategory.DATABASE,
        resources=ResourceSpec(),  # all None
    )
    result = _estimate_resources(wl, ServiceCategory.DATABASE, WorkloadTier.BUSINESS_CRITICAL, EnvironmentType.PRODUCTION)
    assert result["vcpus"] >= 1
    assert result["memory_gb"] > 0
    assert result["storage_gb"] > 0
    assert result["iops"] is not None


check("estimate_resources: defaults applied", test_estimate_resources_defaults)


def test_estimate_resources_user_values_preserved():
    """User-specified values are used instead of defaults."""
    wl = WorkloadRequirement(
        name="Custom VM",
        resources=ResourceSpec(vcpus=16, memory_gb=64, storage_gb=500),
    )
    result = _estimate_resources(wl, ServiceCategory.COMPUTE, WorkloadTier.NON_CRITICAL, EnvironmentType.PRODUCTION)
    # Non-critical tier has 1.0 multipliers
    assert result["vcpus"] == 16
    assert result["memory_gb"] == 64.0
    assert result["storage_gb"] == 500.0


check("estimate_resources: user values preserved", test_estimate_resources_user_values_preserved)


def test_estimate_resources_tier_multiplier():
    """Mission-critical tier scales up resources."""
    wl = WorkloadRequirement(
        name="Critical DB",
        resources=ResourceSpec(vcpus=4, memory_gb=16, storage_gb=100, iops=3000),
    )
    result = _estimate_resources(wl, ServiceCategory.DATABASE, WorkloadTier.MISSION_CRITICAL, EnvironmentType.PRODUCTION)
    # MISSION_CRITICAL: compute=1.5, storage=2.0, iops=1.5
    assert result["vcpus"] == 6  # int(4 * 1.5)
    assert result["memory_gb"] == 24.0  # round(16 * 1.5, 1)
    assert result["storage_gb"] == 200.0  # round(100 * 2.0, 1)
    assert result["iops"] == 4500  # int(3000 * 1.5)


check("estimate_resources: mission_critical scales up", test_estimate_resources_tier_multiplier)


def test_estimate_resources_dev_environment():
    """Development environment scales down resources."""
    wl = WorkloadRequirement(
        name="Dev API",
        resources=ResourceSpec(vcpus=4, memory_gb=16, storage_gb=100),
    )
    result = _estimate_resources(wl, ServiceCategory.COMPUTE, WorkloadTier.NON_CRITICAL, EnvironmentType.DEVELOPMENT)
    # NON_CRITICAL: mult=1.0, DEV: factor=0.25
    assert result["vcpus"] == 1  # max(1, int(4 * 0.25))
    assert result["memory_gb"] == 4.0  # max(0.5, round(16 * 0.25, 1))
    # Storage: max(10.0, round(100 * max(0.25, 0.5), 1)) = max(10, 50) = 50
    assert result["storage_gb"] == 50.0


check("estimate_resources: dev environment scales down", test_estimate_resources_dev_environment)


def test_estimate_resources_gpu_detection():
    """GPU is detected from ResourceSpec or AI_ML category."""
    wl_gpu = WorkloadRequirement(
        name="GPU Job",
        resources=ResourceSpec(gpu_count=1),
    )
    result = _estimate_resources(wl_gpu, ServiceCategory.COMPUTE, WorkloadTier.BUSINESS_CRITICAL, EnvironmentType.PRODUCTION)
    assert result["requires_gpu"] is True

    wl_aiml = WorkloadRequirement(
        name="ML Training",
        resources=ResourceSpec(),
    )
    result = _estimate_resources(wl_aiml, ServiceCategory.AI_ML, WorkloadTier.BUSINESS_CRITICAL, EnvironmentType.PRODUCTION)
    assert result["requires_gpu"] is True


check("estimate_resources: GPU detection", test_estimate_resources_gpu_detection)


# ═══════════════════════════════════════════════════════════════════
# 3. Instance Family Selection
# ═══════════════════════════════════════════════════════════════════


def test_instance_families_compute():
    """Compute category returns expected families."""
    families = _get_instance_families(
        ServiceCategory.COMPUTE, False,
        [CloudProvider.AWS, CloudProvider.AZURE],
    )
    assert any("m5" in f or "m6i" in f for f in families), f"Expected m5/m6i in {families}"
    assert any("Standard_D" in f for f in families), f"Expected Standard_D in {families}"


check("instance_families: COMPUTE", test_instance_families_compute)


def test_instance_families_gpu_override():
    """GPU required with non-AI_ML category → AI_ML families."""
    families = _get_instance_families(
        ServiceCategory.COMPUTE, True,  # GPU required but COMPUTE category
        [CloudProvider.AWS],
    )
    assert any("p3" in f or "p4d" in f or "g5" in f for f in families), f"Expected GPU families: {families}"


check("instance_families: GPU override → AI_ML", test_instance_families_gpu_override)


def test_instance_families_database():
    """Database category returns DB-prefixed families."""
    families = _get_instance_families(
        ServiceCategory.DATABASE, False,
        [CloudProvider.AWS, CloudProvider.GCP],
    )
    assert any("db." in f for f in families), f"Expected db. prefix in {families}"


check("instance_families: DATABASE", test_instance_families_database)


# ═══════════════════════════════════════════════════════════════════
# 4. Component Profile Assembly
# ═══════════════════════════════════════════════════════════════════


def test_build_component_profile():
    """ComponentProfile is correctly assembled."""
    wl = WorkloadRequirement(name="API Server", resources=ResourceSpec(vcpus=4, memory_gb=16))
    estimated = {"vcpus": 4, "memory_gb": 16.0, "storage_gb": 50.0, "iops": None, "requires_gpu": False}
    families = ["m5", "Standard_D"]
    rationale = "Test rationale"

    profile = _build_component_profile(wl, ServiceCategory.COMPUTE, estimated, families, rationale)

    assert isinstance(profile, ComponentProfile)
    assert profile.workload_name == "API Server"
    assert profile.resolved_category == ServiceCategory.COMPUTE
    assert profile.estimated_vcpus == 4
    assert profile.estimated_memory_gb == 16.0
    assert profile.estimated_storage_gb == 50.0
    assert profile.requires_gpu is False
    assert profile.recommended_instance_families == ["m5", "Standard_D"]
    assert profile.rationale == "Test rationale"


check("build_component_profile: assembly", test_build_component_profile)


# ═══════════════════════════════════════════════════════════════════
# 5. Heuristic Rationale
# ═══════════════════════════════════════════════════════════════════


def test_heuristic_rationale():
    """Heuristic rationale covers key details."""
    wl = WorkloadRequirement(
        name="Redis Cache",
        resources=ResourceSpec(database_engine="redis", memory_gb=32, high_availability=True),
        scaling_pattern=ScalingPattern.BURSTY,
    )
    estimated = {"vcpus": 2, "memory_gb": 32.0, "storage_gb": 100.0, "iops": 3000, "requires_gpu": False}
    rationale = _heuristic_rationale(wl, ServiceCategory.DATABASE, estimated)

    assert "Redis Cache" in rationale
    assert "database" in rationale
    assert "32" in rationale or "32.0" in rationale
    assert "high-availability" in rationale
    assert "bursty" in rationale


check("heuristic_rationale: covers details", test_heuristic_rationale)


# ═══════════════════════════════════════════════════════════════════
# 6. Lookup Table Integrity
# ═══════════════════════════════════════════════════════════════════


def test_lookup_tables():
    """All lookup tables are consistent and non-empty."""
    assert len(_CATEGORY_SIGNALS) >= 9, "Need at least 9 category signals"
    assert len(_INSTANCE_FAMILY_MAP) >= 7, "Need at least 7 family maps"
    assert len(_RESOURCE_DEFAULTS) >= 7, "Need at least 7 resource default sets"
    assert len(_TIER_MULTIPLIERS) == 3, "Need exactly 3 tier multipliers"
    assert len(_ENVIRONMENT_FACTORS) == 4, "Need exactly 4 environment factors"

    # Every category in INSTANCE_FAMILY_MAP should have at least AWS entries
    for cat, families in _INSTANCE_FAMILY_MAP.items():
        assert "aws" in families, f"Missing AWS families for {cat}"


check("lookup_tables: integrity", test_lookup_tables)


# ═══════════════════════════════════════════════════════════════════
# 7. run_profiler_node (with mocked LLM)
# ═══════════════════════════════════════════════════════════════════


def test_run_profiler_node_happy_path():
    """Full profiler node with mocked LLM produces valid WorkloadProfile."""

    async def _run():
        # Create a mock LLM that returns valid JSON
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = '{"rationale": "Test component analyzed.", "adjustments": "none", "confidence": 0.9}'
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        # Build state with workload_request
        state = create_initial_state(
            request_id="test-profiler-001",
            project_name="TestProject",
            raw_user_input="We need a Kubernetes cluster and a PostgreSQL database",
        )

        # Populate workloads
        state["workload_request"] = WorkloadRequest(
            project_name="TestProject",
            environment=EnvironmentType.PRODUCTION,
            tier=WorkloadTier.BUSINESS_CRITICAL,
            target_providers=[CloudProvider.AWS, CloudProvider.AZURE],
            workloads=[
                WorkloadRequirement(
                    name="K8s Cluster",
                    description="Container orchestration",
                    suggested_category=ServiceCategory.CONTAINER,
                    resources=ResourceSpec(
                        cpu_request_millicores=1000,
                        memory_request_mb=2048,
                        replicas=3,
                    ),
                ),
                WorkloadRequirement(
                    name="PostgreSQL DB",
                    description="Main relational database",
                    suggested_category=ServiceCategory.DATABASE,
                    resources=ResourceSpec(
                        vcpus=4,
                        memory_gb=16,
                        storage_gb=500,
                        database_engine="postgresql",
                        high_availability=True,
                        iops=3000,
                    ),
                ),
            ],
        )

        # Run the profiler
        result_state = await run_profiler_node(state, mock_llm)

        # Verify WorkloadProfile
        profile = result_state["workload_profile"]
        assert isinstance(profile, WorkloadProfile), f"Expected WorkloadProfile, got {type(profile)}"
        assert len(profile.components) == 2, f"Expected 2 components, got {len(profile.components)}"

        # Verify component categories
        k8s_comp = profile.components[0]
        assert k8s_comp.workload_name == "K8s Cluster"
        assert k8s_comp.resolved_category == ServiceCategory.CONTAINER

        db_comp = profile.components[1]
        assert db_comp.workload_name == "PostgreSQL DB"
        assert db_comp.resolved_category == ServiceCategory.DATABASE

        # Verify totals are positive
        assert profile.total_vcpus > 0
        assert profile.total_memory_gb > 0
        assert profile.total_storage_gb > 0

        # Verify environment/tier copied
        assert profile.environment == EnvironmentType.PRODUCTION
        assert profile.tier == WorkloadTier.BUSINESS_CRITICAL

        # Verify message appended
        assert len(result_state["messages"]) >= 1
        last_msg = result_state["messages"][-1]
        assert isinstance(last_msg, ChatMessage)
        assert last_msg.agent_name == "profiler"

        # Verify agent execution tracking
        exec_info = result_state["agent_executions"]["profiler"]
        assert exec_info.status == AgentStatus.COMPLETED
        assert exec_info.duration_ms is not None
        assert exec_info.duration_ms > 0

        # Verify current_agent advanced
        assert result_state["current_agent"] == "sizer"

        # Verify KPIs updated (components + 1 for notes = 3 LLM calls)
        assert result_state["kpis"]["total_llm_calls"] >= 3

    asyncio.run(_run())


check("run_profiler_node: happy path (mocked LLM)", test_run_profiler_node_happy_path)


def test_run_profiler_node_missing_workload_request():
    """Profiler fails with ValueError if workload_request is None."""

    async def _run():
        mock_llm = AsyncMock()
        state = create_initial_state(request_id="test-fail-001")
        state["workload_request"] = None  # type: ignore[assignment]

        try:
            await run_profiler_node(state, mock_llm)
            return False  # Should have raised
        except ValueError as e:
            return "missing" in str(e).lower()

    result = asyncio.run(_run())
    assert result, "Expected ValueError for missing workload_request"


check("run_profiler_node: missing workload_request raises", test_run_profiler_node_missing_workload_request)


def test_run_profiler_node_empty_workloads():
    """Profiler fails with ValueError if workloads list is empty."""

    async def _run():
        mock_llm = AsyncMock()
        state = create_initial_state(request_id="test-fail-002")
        state["workload_request"] = WorkloadRequest(
            project_name="Empty",
            workloads=[],  # no workloads
        )

        try:
            await run_profiler_node(state, mock_llm)
            return False
        except ValueError as e:
            return "no workloads" in str(e).lower()

    result = asyncio.run(_run())
    assert result, "Expected ValueError for empty workloads"


check("run_profiler_node: empty workloads raises", test_run_profiler_node_empty_workloads)


def test_run_profiler_node_llm_fallback():
    """Profiler degrades gracefully when LLM fails."""

    async def _run():
        # LLM that always raises
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("LLM unavailable"))

        state = create_initial_state(request_id="test-fallback-001")
        state["workload_request"] = WorkloadRequest(
            project_name="FallbackTest",
            workloads=[
                WorkloadRequirement(
                    name="Simple VM",
                    resources=ResourceSpec(vcpus=2, memory_gb=4),
                ),
            ],
        )

        result_state = await run_profiler_node(state, mock_llm)

        # Should still succeed with heuristic rationale
        profile = result_state["workload_profile"]
        assert len(profile.components) == 1
        assert profile.components[0].rationale  # has some rationale
        assert profile.components[0].workload_name == "Simple VM"
        return True

    result = asyncio.run(_run())
    assert result, "Profiler should degrade gracefully"


check("run_profiler_node: LLM fallback works", test_run_profiler_node_llm_fallback)


# ═══════════════════════════════════════════════════════════════════
# 8. Import verification
# ═══════════════════════════════════════════════════════════════════


def test_no_provider_imports():
    """Profiler must NOT import provider-specific LLM classes."""
    import inspect
    import src.agents.profiler as mod

    source = inspect.getsource(mod)
    forbidden = [
        "ChatBedrockConverse",
        "ChatGoogleGenerativeAI",
        "langchain_aws",
        "langchain_google",
    ]
    for term in forbidden:
        assert term not in source, f"Found forbidden import '{term}' in profiler.py"


check("no_provider_imports: clean abstraction", test_no_provider_imports)


def test_observe_decorator():
    """Key functions must have @observe() decorator."""
    import inspect
    import src.agents.profiler as mod

    source = inspect.getsource(mod)
    assert source.count("@observe()") >= 3, (
        f"Expected at least 3 @observe() decorators, "
        f"found {source.count('@observe()')}"
    )


check("observe_decorator: at least 3 @observe()", test_observe_decorator)


# ═══════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("  Profiler Agent Validation Results")
    print("=" * 65)

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = sum(1 for _, ok, _ in checks if not ok)

    for name, ok, err in checks:
        icon = "✅" if ok else "❌"
        msg = f"  {icon}  {name}"
        if err:
            msg += f"\n       └─ {err}"
        print(msg)

    print("-" * 65)
    print(f"  {passed}/{passed + failed} checks passed")
    if failed:
        print(f"  ⚠️  {failed} check(s) FAILED")
    else:
        print("  🎉 All checks passed!")
    print("=" * 65 + "\n")

    sys.exit(0 if failed == 0 else 1)
