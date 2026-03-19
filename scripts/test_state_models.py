#!/usr/bin/env python3
"""Validation script for the state schema + models redesign.

Run from project root:
    python scripts/test_state_models.py
"""

from __future__ import annotations

import sys

checks: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        checks.append((name, True, ""))
    except Exception as e:
        checks.append((name, False, str(e)))


# ── 1. Package-level imports ──────────────────────────────────

check("models.__init__ imports", lambda: __import__("src.models"))


# ── 2. Enum imports ───────────────────────────────────────────

check("CloudProvider", lambda: getattr(
    __import__("src.models", fromlist=["CloudProvider"]), "CloudProvider"))
check("ServiceCategory", lambda: getattr(
    __import__("src.models", fromlist=["ServiceCategory"]), "ServiceCategory"))
check("PricingTier", lambda: getattr(
    __import__("src.models", fromlist=["PricingTier"]), "PricingTier"))
check("EnvironmentType", lambda: getattr(
    __import__("src.models", fromlist=["EnvironmentType"]), "EnvironmentType"))
check("WorkloadTier", lambda: getattr(
    __import__("src.models", fromlist=["WorkloadTier"]), "WorkloadTier"))
check("ScalingPattern", lambda: getattr(
    __import__("src.models", fromlist=["ScalingPattern"]), "ScalingPattern"))
check("MessageRole", lambda: getattr(
    __import__("src.models", fromlist=["MessageRole"]), "MessageRole"))
check("ClarificationStatus", lambda: getattr(
    __import__("src.models", fromlist=["ClarificationStatus"]), "ClarificationStatus"))


# ── 3. Workload models ───────────────────────────────────────

check("WorkloadRequirement", lambda: getattr(
    __import__("src.models", fromlist=["WorkloadRequirement"]), "WorkloadRequirement"))
check("ResourceSpec", lambda: getattr(
    __import__("src.models", fromlist=["ResourceSpec"]), "ResourceSpec"))
check("ComponentProfile", lambda: getattr(
    __import__("src.models", fromlist=["ComponentProfile"]), "ComponentProfile"))
check("WorkloadProfile", lambda: getattr(
    __import__("src.models", fromlist=["WorkloadProfile"]), "WorkloadProfile"))
check("WorkloadRequest", lambda: getattr(
    __import__("src.models", fromlist=["WorkloadRequest"]), "WorkloadRequest"))


# ── 4. Conversation models ───────────────────────────────────

check("ChatMessage", lambda: getattr(
    __import__("src.models", fromlist=["ChatMessage"]), "ChatMessage"))
check("ClarificationQuestion", lambda: getattr(
    __import__("src.models", fromlist=["ClarificationQuestion"]), "ClarificationQuestion"))
check("ConversationState", lambda: getattr(
    __import__("src.models", fromlist=["ConversationState"]), "ConversationState"))


# ── 5. Recommendation models ─────────────────────────────────

check("PackedNode", lambda: getattr(
    __import__("src.models", fromlist=["PackedNode"]), "PackedNode"))
check("BinPackingResult", lambda: getattr(
    __import__("src.models", fromlist=["BinPackingResult"]), "BinPackingResult"))
check("CostComparison", lambda: getattr(
    __import__("src.models", fromlist=["CostComparison"]), "CostComparison"))
check("ComplianceReport", lambda: getattr(
    __import__("src.models", fromlist=["ComplianceReport"]), "ComplianceReport"))
check("CloudRecommendation", lambda: getattr(
    __import__("src.models", fromlist=["CloudRecommendation"]), "CloudRecommendation"))


# ── 6. Orchestrator ──────────────────────────────────────────

check("orchestrator.__init__ imports", lambda: __import__("src.orchestrator"))
check("OrchestratorState", lambda: getattr(
    __import__("src.orchestrator", fromlist=["OrchestratorState"]), "OrchestratorState"))
check("AgentStatus", lambda: getattr(
    __import__("src.orchestrator", fromlist=["AgentStatus"]), "AgentStatus"))
check("AgentExecution", lambda: getattr(
    __import__("src.orchestrator", fromlist=["AgentExecution"]), "AgentExecution"))
check("SizedWorkloadResult", lambda: getattr(
    __import__("src.orchestrator", fromlist=["SizedWorkloadResult"]), "SizedWorkloadResult"))
check("create_initial_state", lambda: getattr(
    __import__("src.orchestrator", fromlist=["create_initial_state"]), "create_initial_state"))


# ── 7. Legacy models still importable ────────────────────────

check("ComputeSKU (legacy)", lambda: getattr(
    __import__("src.models", fromlist=["ComputeSKU"]), "ComputeSKU"))
check("StorageSKU (legacy)", lambda: getattr(
    __import__("src.models", fromlist=["StorageSKU"]), "StorageSKU"))
check("VMWorkload (legacy)", lambda: getattr(
    __import__("src.models", fromlist=["VMWorkload"]), "VMWorkload"))
check("ContainerWorkload (legacy)", lambda: getattr(
    __import__("src.models", fromlist=["ContainerWorkload"]), "ContainerWorkload"))
check("StorageRequirement (legacy)", lambda: getattr(
    __import__("src.models", fromlist=["StorageRequirement"]), "StorageRequirement"))


# ── 8. Instantiation tests ───────────────────────────────────


def test_workload_requirement() -> None:
    from src.models.cloud_resource import CloudProvider, ServiceCategory
    from src.models.workload import ResourceSpec, WorkloadRequirement, WorkloadRequest

    wr = WorkloadRequirement(
        name="PostgreSQL DB",
        description="Main application database",
        suggested_category=ServiceCategory.DATABASE,
        resources=ResourceSpec(
            vcpus=4,
            memory_gb=16,
            storage_gb=500,
            database_engine="postgresql",
            high_availability=True,
        ),
    )
    assert wr.name == "PostgreSQL DB"
    assert wr.resources.database_engine == "postgresql"
    assert wr.resources.high_availability is True
    assert wr.suggested_category == ServiceCategory.DATABASE

    req = WorkloadRequest(
        project_name="test-project",
        workloads=[wr],
        target_providers=[CloudProvider.AWS, CloudProvider.AZURE],
    )
    assert len(req.workloads) == 1
    assert req.workloads[0].suggested_category == ServiceCategory.DATABASE
    assert req.raw_user_input == ""


check("WorkloadRequirement instantiation", test_workload_requirement)


def test_conversation_state() -> None:
    from src.models.conversation import (
        ClarificationQuestion,
        ClarificationStatus,
        ConversationState,
    )

    cs = ConversationState(conversation_id="test-123")
    assert cs.should_continue_clarifying is True
    assert cs.has_pending_questions is False

    q = ClarificationQuestion(
        question_id="q_budget",
        question_text="What is your monthly budget?",
        target_field="budget_monthly_usd",
    )
    cs.clarification_questions.append(q)
    assert cs.has_pending_questions is True

    q.status = ClarificationStatus.ANSWERED
    q.user_answer = "$5000"
    assert cs.has_pending_questions is False


check("ConversationState behaviour", test_conversation_state)


def test_create_initial_state() -> None:
    from src.orchestrator.state import AgentStatus, create_initial_state

    state = create_initial_state(
        request_id="req-001",
        project_name="MyProject",
        raw_user_input="I need a PostgreSQL database",
    )
    assert state["request_id"] == "req-001"
    assert state["project_name"] == "MyProject"
    assert len(state["messages"]) == 1
    assert state["messages"][0].content == "I need a PostgreSQL database"
    assert state["agent_executions"]["clarifier"].status == AgentStatus.PENDING
    assert state["agent_executions"]["profiler"].status == AgentStatus.PENDING
    assert state["current_agent"] == "clarifier"
    assert state["kpis"]["total_llm_calls"] == 0
    assert state["sized_results"] == []
    assert state["rfp_document"] == ""
    assert state["error"] is None


check("create_initial_state factory", test_create_initial_state)


def test_recommendation_uses_normalized_price_item() -> None:
    from src.models.pricing import NormalizedPriceItem
    from src.models.recommendation import CloudRecommendation, ProviderCostBreakdown

    # ProviderCostBreakdown.selected_skus should accept NormalizedPriceItem
    breakdown = ProviderCostBreakdown(provider="aws")
    assert isinstance(breakdown.selected_skus, list)

    # CloudRecommendation.sku_selections uses NormalizedPriceItem (not ComputeSKU)
    import inspect
    hints = inspect.get_annotations(CloudRecommendation)
    # The field should exist and NOT reference ComputeSKU
    assert "sku_selections" in hints
    assert "ComputeSKU" not in str(hints["sku_selections"])
    assert "vm_selections" not in hints  # old field removed
    assert "storage_selections" not in hints  # old field removed


check("Recommendation uses NormalizedPriceItem", test_recommendation_uses_normalized_price_item)


def test_workload_profile_has_components() -> None:
    from src.models.cloud_resource import ServiceCategory
    from src.models.workload import ComponentProfile, WorkloadProfile

    cp = ComponentProfile(
        workload_name="API Server",
        resolved_category=ServiceCategory.COMPUTE,
        estimated_vcpus=4,
        estimated_memory_gb=16,
        rationale="General-purpose compute workload",
    )
    profile = WorkloadProfile(
        components=[cp],
        total_vcpus=4,
        total_memory_gb=16,
        profiler_notes="Single compute workload",
    )
    assert len(profile.components) == 1
    assert profile.components[0].resolved_category == ServiceCategory.COMPUTE


check("WorkloadProfile with ComponentProfile", test_workload_profile_has_components)


def test_sized_workload_result() -> None:
    from src.orchestrator.state import SizedWorkloadResult

    r = SizedWorkloadResult(
        workload_name="API Server",
        provider="azure",
        monthly_cost_usd=150.0,
        fit_score=0.92,
        rationale="Standard_D4s_v3 is the best fit",
    )
    assert r.fit_score == 0.92
    assert r.selected_sku is None  # no SKU attached yet
    assert r.alternative_skus == []


check("SizedWorkloadResult instantiation", test_sized_workload_result)


def test_agent_execution_tracking() -> None:
    from datetime import datetime, timezone

    from src.orchestrator.state import AgentExecution, AgentStatus

    ae = AgentExecution(agent_name="profiler")
    assert ae.status == AgentStatus.PENDING
    assert ae.elapsed_ms is None

    ae.status = AgentStatus.RUNNING
    ae.started_at = datetime.now(timezone.utc)

    ae.status = AgentStatus.COMPLETED
    ae.completed_at = datetime.now(timezone.utc)
    assert ae.elapsed_ms is not None
    assert ae.elapsed_ms >= 0


check("AgentExecution lifecycle", test_agent_execution_tracking)


def test_cost_comparison_new_fields() -> None:
    from src.models.recommendation import CostComparison, ProviderCostBreakdown

    breakdown = ProviderCostBreakdown(
        provider="aws",
        compute_monthly_usd=500,
        database_monthly_usd=200,
        storage_monthly_usd=50,
        total_monthly_usd=750,
        total_annual_usd=9000,
    )
    cc = CostComparison(
        providers=[breakdown],
        cheapest_provider="aws",
        budget_monthly_usd=1000,
        budget_exceeded=False,
    )
    assert cc.budget_exceeded is False
    assert cc.providers[0].database_monthly_usd == 200


check("CostComparison with new fields", test_cost_comparison_new_fields)


# ── Report ────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in checks if ok)
failed = sum(1 for _, ok, _ in checks if not ok)

print(f"\n{'=' * 60}")
print(f"  State Schema + Models Validation")
print(f"  {passed}/{len(checks)} checks passed, {failed} failed")
print(f"{'=' * 60}")

for name, ok, err in checks:
    status = "✅" if ok else "❌"
    line = f"  {status} {name}"
    if err:
        line += f"  — {err}"
    print(line)

print()
if failed:
    sys.exit(1)
print("All checks passed! ✅")
