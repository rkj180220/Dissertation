"""Integration tests for the full agent pipeline with mocked LLM and PricingService.

These tests exercise each agent node end-to-end (real code, mocked I/O) to verify
that state flows correctly through the pipeline.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.clarifier import run_clarifier_node
from src.agents.profiler import run_profiler_node
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.orchestrator.state import OrchestratorState, create_initial_state


# ---------------------------------------------------------------------------
# Clarifier integration tests
# ---------------------------------------------------------------------------


class TestClarifierNode:
    """run_clarifier_node() processes raw input and populates state correctly."""

    @pytest.mark.asyncio
    async def test_clarifier_sets_requirements_complete(
        self,
        mock_llm: MagicMock,
        mock_pricing_service: MagicMock,
    ) -> None:
        state = create_initial_state(
            request_id="int-test-001",
            project_name="E-Commerce Platform",
            raw_user_input="3 microservices on kubernetes with postgresql database on AWS",
        )
        result_state = await run_clarifier_node(state, mock_llm, mock_pricing_service)

        assert result_state["conversation"].requirements_complete is True

    @pytest.mark.asyncio
    async def test_clarifier_extracts_workloads(
        self,
        mock_llm: MagicMock,
        mock_pricing_service: MagicMock,
    ) -> None:
        state = create_initial_state(
            request_id="int-test-002",
            project_name="Test",
            raw_user_input="3 microservices on kubernetes",
        )
        result_state = await run_clarifier_node(state, mock_llm, mock_pricing_service)

        workloads = result_state["workload_request"].workloads
        assert len(workloads) >= 3  # 3 container workloads + K8s mgmt + LB

        categories = {w.suggested_category for w in workloads}
        assert ServiceCategory.CONTAINER in categories
        assert ServiceCategory.KUBERNETES in categories

    @pytest.mark.asyncio
    async def test_clarifier_appends_message(
        self,
        mock_llm: MagicMock,
        mock_pricing_service: MagicMock,
    ) -> None:
        state = create_initial_state(
            request_id="int-test-003",
            project_name="Test",
            raw_user_input="I need an API server on AWS",
        )
        initial_msg_count = len(state["messages"])
        result_state = await run_clarifier_node(state, mock_llm, mock_pricing_service)

        # Clarifier returns new messages (patch); at least one summary message
        assert len(result_state["messages"]) >= 1
        assert any("Clarifier" in m.content for m in result_state["messages"])

    @pytest.mark.asyncio
    async def test_clarifier_sets_provider_from_input(
        self,
        mock_llm: MagicMock,
        mock_pricing_service: MagicMock,
    ) -> None:
        state = create_initial_state(
            request_id="int-test-004",
            project_name="Test",
            raw_user_input="deploy on AWS only",
        )
        result_state = await run_clarifier_node(state, mock_llm, mock_pricing_service)
        providers = result_state["workload_request"].target_providers
        assert CloudProvider.AWS in providers

    @pytest.mark.asyncio
    async def test_clarifier_handles_empty_input_gracefully(
        self,
        mock_llm: MagicMock,
        mock_pricing_service: MagicMock,
    ) -> None:
        state = create_initial_state(
            request_id="int-test-005",
            project_name="Test",
            raw_user_input="",
        )
        # Should not raise; should complete with fallback workloads
        result_state = await run_clarifier_node(state, mock_llm, mock_pricing_service)
        assert result_state["conversation"].requirements_complete is True
        assert len(result_state["workload_request"].workloads) >= 1


# ---------------------------------------------------------------------------
# Profiler integration tests
# ---------------------------------------------------------------------------


class TestProfilerNode:
    """run_profiler_node() enriches workload_request into workload_profile."""

    @pytest.mark.asyncio
    async def test_profiler_produces_component_profiles(
        self,
        mock_llm: MagicMock,
        state_with_workload_request: OrchestratorState,
    ) -> None:
        result_state = await run_profiler_node(state_with_workload_request, mock_llm)

        profile = result_state["workload_profile"]
        assert profile is not None
        assert len(profile.components) >= 1

    @pytest.mark.asyncio
    async def test_profiler_resolves_categories(
        self,
        mock_llm: MagicMock,
        state_with_workload_request: OrchestratorState,
    ) -> None:
        result_state = await run_profiler_node(state_with_workload_request, mock_llm)
        profile = result_state["workload_profile"]
        for comp in profile.components:
            assert comp.resolved_category is not None
            assert isinstance(comp.resolved_category, ServiceCategory)

    @pytest.mark.asyncio
    async def test_profiler_appends_message(
        self,
        mock_llm: MagicMock,
        state_with_workload_request: OrchestratorState,
    ) -> None:
        initial_count = len(state_with_workload_request["messages"])
        result_state = await run_profiler_node(state_with_workload_request, mock_llm)
        # Profiler returns new messages (patch); at least one summary message
        assert len(result_state["messages"]) >= 1
        assert any("Profiler" in m.content for m in result_state["messages"])

    @pytest.mark.asyncio
    async def test_profiler_k8s_mgmt_has_zero_vcpus(
        self,
        mock_llm: MagicMock,
        mock_pricing_service: MagicMock,
    ) -> None:
        """K8s cluster management fee workload should produce zero-resource component."""
        state = create_initial_state(
            request_id="int-test-006",
            project_name="Test",
            raw_user_input="3 microservices on k8s",
        )
        state = await run_clarifier_node(state, mock_llm, mock_pricing_service)
        result_state = await run_profiler_node(state, mock_llm)

        profile = result_state["workload_profile"]
        k8s_mgmt = next(
            (c for c in profile.components if "cluster_management" in (c.workload_name or "").lower()),
            None,
        )
        if k8s_mgmt is not None:
            # Kubernetes management fee has no compute resources
            assert k8s_mgmt.estimated_vcpus == 0

    @pytest.mark.asyncio
    async def test_profiler_no_ai_ml_without_gpu(
        self,
        mock_llm: MagicMock,
        mock_pricing_service: MagicMock,
    ) -> None:
        """AI_ML guard: workloads without GPU should not be profiled as AI_ML."""
        state = create_initial_state(
            request_id="int-test-007",
            project_name="Test",
            raw_user_input="web api backend with postgres on aws",
        )
        state = await run_clarifier_node(state, mock_llm, mock_pricing_service)
        result_state = await run_profiler_node(state, mock_llm)

        profile = result_state["workload_profile"]
        for comp in profile.components:
            if comp.resolved_category == ServiceCategory.AI_ML:
                # If AI_ML, GPU must be explicitly requested
                assert comp.requires_gpu is True


# ---------------------------------------------------------------------------
# State shape tests (no agent calls needed)
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_initial_state_has_all_required_keys(self) -> None:
        state = create_initial_state(
            request_id="shape-test-001",
            project_name="Test",
            raw_user_input="dummy input",
        )
        required_keys = {
            "request_id",
            "project_name",
            "messages",
            "conversation",
            "workload_request",
            "workload_profile",
            "sized_results",
            "agent_executions",
            "kpis",
        }
        for key in required_keys:
            assert key in state, f"Missing state key: {key}"

    def test_initial_state_has_seven_agent_executions(self) -> None:
        state = create_initial_state(request_id="x", project_name="y", raw_user_input="z")
        assert len(state["agent_executions"]) == 7
        expected = {"clarifier", "profiler", "sizer", "finops", "rfp_writer", "validator", "router"}
        assert set(state["agent_executions"].keys()) == expected

    def test_initial_state_message_contains_user_input(self) -> None:
        raw = "I need a kubernetes cluster"
        state = create_initial_state(request_id="x", project_name="y", raw_user_input=raw)
        assert any(raw in m.content for m in state["messages"])
