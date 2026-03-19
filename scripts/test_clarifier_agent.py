#!/usr/bin/env python3
"""Test the Clarifier Agent — verifies multi-turn logic and workload parsing."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from src.agents.clarifier import (
    _extract_workloads_from_text,
    _parse_budget,
    _parse_compliance,
    _parse_environment,
    _parse_providers,
    _parse_tier,
    _generate_clarification_questions,
)
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.conversation import (
    ClarificationPriority,
    ClarificationStatus,
    ChatMessage,
    ConversationState,
    MessageRole,
)
from src.models.workload import (
    EnvironmentType,
    WorkloadRequest,
    WorkloadTier,
)
from src.orchestrator.state import create_initial_state

checks: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        checks.append((name, True, ""))
    except AssertionError as e:
        checks.append((name, False, str(e)))
    except Exception as e:
        checks.append((name, False, f"{type(e).__name__}: {e}"))


# ─── 1. Parsing utilities ──────────────────────────────────────

def test_parse_environment():
    assert _parse_environment("production") == EnvironmentType.PRODUCTION
    assert _parse_environment("staging") == EnvironmentType.STAGING
    assert _parse_environment("dev") == EnvironmentType.DEVELOPMENT
    assert _parse_environment("disaster") == EnvironmentType.DR
    assert _parse_environment("unknown") == EnvironmentType.PRODUCTION  # default


check("_parse_environment", test_parse_environment)


def test_parse_tier():
    from src.models.workload import WorkloadTier

    assert _parse_tier("mission critical") == WorkloadTier.MISSION_CRITICAL
    assert _parse_tier("business") == WorkloadTier.BUSINESS_CRITICAL
    assert _parse_tier("non-critical") == WorkloadTier.NON_CRITICAL


check("_parse_tier", test_parse_tier)


def test_parse_providers():
    result = _parse_providers("aws, azure")
    assert CloudProvider.AWS in result
    assert CloudProvider.AZURE in result
    assert CloudProvider.GCP not in result

    result = _parse_providers("unknown")
    assert len(result) == 3  # defaults to all


check("_parse_providers", test_parse_providers)


def test_parse_budget():
    assert _parse_budget("5000") == 5000.0
    assert _parse_budget("$5,000") == 5000.0
    assert _parse_budget("skip") is None
    assert _parse_budget("") is None


check("_parse_budget", test_parse_budget)


def test_parse_compliance():
    result = _parse_compliance("hipaa, pci-dss")
    assert "hipaa" in result
    assert "pci-dss" in result

    result = _parse_compliance("none")
    assert result == ["waf"]  # default


check("_parse_compliance", test_parse_compliance)


# ─── 2. Workload extraction ───────────────────────────────────

def test_extract_workloads_from_text():
    # Kubernetes
    wls = _extract_workloads_from_text("We need a Kubernetes cluster")
    assert len(wls) > 0
    assert any(w.suggested_category == ServiceCategory.CONTAINER for w in wls)

    # Database
    wls = _extract_workloads_from_text("PostgreSQL database")
    assert any(w.suggested_category == ServiceCategory.DATABASE for w in wls)
    assert any("postgres" in w.resources.database_engine.lower() for w in wls if w.resources.database_engine)

    # Redis
    wls = _extract_workloads_from_text("Redis cache")
    assert any(w.resources.database_engine == "redis" for w in wls if w.resources.database_engine)

    # VMs
    wls = _extract_workloads_from_text("EC2 instances")
    assert any(w.suggested_category == ServiceCategory.COMPUTE for w in wls)

    # Generic fallback
    wls = _extract_workloads_from_text("random text")
    assert len(wls) == 1  # placeholder
    assert wls[0].name == "Workload"


check("_extract_workloads_from_text", test_extract_workloads_from_text)


# ─── 3. Question generation ───────────────────────────────────

def test_generate_clarification_questions():
    questions = _generate_clarification_questions({}, WorkloadRequest(project_name="test"))
    assert len(questions) > 0
    # Should have required + recommended
    assert any(q.priority == ClarificationPriority.REQUIRED for q in questions)
    assert any(q.priority == ClarificationPriority.RECOMMENDED for q in questions)

    # All should start as PENDING
    assert all(q.status == ClarificationStatus.PENDING for q in questions)

    # Should cover the expected fields
    question_ids = {q.question_id for q in questions}
    assert "q_project_name" in question_ids
    assert "q_environment" in question_ids
    assert "q_budget" in question_ids


check("_generate_clarification_questions", test_generate_clarification_questions)


# ─── 4. ConversationState behavior ────────────────────────────

def test_conversation_state():
    cs = ConversationState(conversation_id="test-1")
    assert cs.should_continue_clarifying is True
    assert cs.has_pending_questions is False

    # Add a pending question
    from src.models.conversation import ClarificationQuestion

    q = ClarificationQuestion(
        question_id="test",
        question_text="Test?",
        target_field="test_field",
    )
    cs.clarification_questions.append(q)
    assert cs.has_pending_questions is True
    assert len(cs.pending_questions) == 1

    # Mark as answered
    q.status = ClarificationStatus.ANSWERED
    assert cs.has_pending_questions is False
    assert len(cs.pending_questions) == 0

    # Mark complete
    cs.requirements_complete = True
    assert cs.should_continue_clarifying is False


check("ConversationState behavior", test_conversation_state)


# ─── 5. create_initial_state ──────────────────────────────────

def test_create_initial_state():
    state = create_initial_state(
        request_id="req-001",
        project_name="MyProject",
        raw_user_input="I need Kubernetes",
    )

    assert state["request_id"] == "req-001"
    assert state["project_name"] == "MyProject"
    assert state["current_agent"] == "clarifier"
    assert len(state["messages"]) == 1
    assert state["messages"][0].content == "I need Kubernetes"
    assert state["messages"][0].role == MessageRole.USER
    assert state["agent_executions"]["clarifier"].status.value == "pending"
    assert state["kpis"]["total_llm_calls"] == 0
    assert state["sized_results"] == []
    assert state["rfp_document"] == ""
    assert state["error"] is None


check("create_initial_state", test_create_initial_state)


# ─── 6. ChatMessage + ConversationState ────────────────────

def test_chat_message():
    msg = ChatMessage(
        role=MessageRole.ASSISTANT,
        content="Hello, what's your project?",
        agent_name="clarifier",
        metadata={"question_id": "q_1"},
    )
    assert msg.role == MessageRole.ASSISTANT
    assert msg.agent_name == "clarifier"
    assert msg.metadata["question_id"] == "q_1"
    assert msg.timestamp is not None


check("ChatMessage", test_chat_message)


def test_workload_request_with_new_models():
    from src.models.workload import WorkloadRequirement, ResourceSpec

    wr = WorkloadRequirement(
        name="API Server",
        suggested_category=ServiceCategory.COMPUTE,
        resources=ResourceSpec(vcpus=4, memory_gb=16),
    )

    req = WorkloadRequest(
        project_name="test-proj",
        workloads=[wr],
        target_providers=[CloudProvider.AWS, CloudProvider.AZURE],
        budget_monthly_usd=5000,
    )

    assert req.project_name == "test-proj"
    assert len(req.workloads) == 1
    assert req.workloads[0].resources.vcpus == 4
    assert req.budget_monthly_usd == 5000


check("WorkloadRequest + new models", test_workload_request_with_new_models)


# ─── 7. Agent execution tracking ───────────────────────────────

def test_agent_execution():
    from src.orchestrator.state import AgentExecution, AgentStatus

    ae = AgentExecution(agent_name="clarifier")
    assert ae.status == AgentStatus.PENDING
    assert ae.elapsed_ms is None

    ae.status = AgentStatus.RUNNING
    ae.started_at = datetime.now(timezone.utc)
    import time

    time.sleep(0.01)
    ae.status = AgentStatus.COMPLETED
    ae.completed_at = datetime.now(timezone.utc)

    assert ae.elapsed_ms is not None
    assert ae.elapsed_ms >= 10  # at least 10ms


check("AgentExecution tracking", test_agent_execution)


# ─── Report ────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in checks if ok)
failed = sum(1 for _, ok, _ in checks if not ok)

print(f"\n{'=' * 60}")
print(f"  Clarifier Agent Validation")
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
