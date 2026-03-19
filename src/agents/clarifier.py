"""Clarifier Agent — Multi-turn requirement refinement.

The Clarifier is the entry point to the orchestration pipeline. It engages
the user in a multi-turn dialogue to disambiguate and refine their cloud
infrastructure requirements until one of:

1. ``requirements_complete`` is marked True (all critical questions answered)
2. ``max_clarification_turns`` is reached (fallback to defaults)
3. All pending questions are resolved or skipped

The agent's output is a refined ``WorkloadRequest`` with structured
``WorkloadRequirement`` objects ready for the Profiler.

### Flow

```
User's raw input (from chat)
      │
      ▼
┌─────────────────────────┐
│ Parse raw_user_input    │  ← Extract project name, workloads, constraints
│ Generate questions      │    Build initial WorkloadRequest
└────────┬────────────────┘
         │
         ▼
    Loop until:
    - requirements_complete
    - max_turns reached
    - pending_questions empty
         │
         ├─► Ask pending question
         │
         ├─► User answers (append ChatMessage)
         │
         ├─► Parse answer → resolve ClarificationQuestion
         │
         └─► Check if more questions needed
         
         ▼
┌─────────────────────────┐
│ Mark complete           │
│ Pass to Profiler        │
└─────────────────────────┘
```

### Usage

```python
from src.agents.clarifier import run_clarifier_node
from src.orchestrator import create_initial_state

state = create_initial_state(
    request_id="req-001",
    project_name="MyProject",
    raw_user_input="We need a Kubernetes cluster with 3 nodes..."
)

state = await run_clarifier_node(state, llm, pricing_service)
# state['conversation'].requirements_complete is now True or max_turns reached
# state['workload_request'] is populated
# state['messages'] includes all clarification Q&A
```
"""

from __future__ import annotations

import json
import operator
import re
from datetime import datetime, timezone
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langfuse import observe

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.conversation import (
    ClarificationPriority,
    ClarificationQuestion,
    ClarificationStatus,
    ChatMessage,
    MessageRole,
)
from src.models.workload import (
    EnvironmentType,
    ResourceSpec,
    ScalingPattern,
    WorkloadRequirement,
    WorkloadRequest,
    WorkloadTier,
)
from src.orchestrator.state import AgentExecution, AgentStatus, OrchestratorState
from src.services.pricing_service import PricingService

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Clarification question templates
# ---------------------------------------------------------------------------


REQUIRED_QUESTIONS = [
    {
        "id": "q_project_name",
        "text": "What is the project or organization name?",
        "target_field": "project_name",
        "priority": ClarificationPriority.REQUIRED,
        "default": "untitled",
    },
    {
        "id": "q_environment",
        "text": "What environment is this for? (production/staging/development/disaster_recovery)",
        "target_field": "environment",
        "priority": ClarificationPriority.REQUIRED,
        "default": "production",
    },
    {
        "id": "q_tier",
        "text": "What is the criticality tier? (mission_critical/business_critical/non_critical)",
        "target_field": "tier",
        "priority": ClarificationPriority.REQUIRED,
        "default": "business_critical",
    },
    {
        "id": "q_workload_count",
        "text": "How many distinct workload components do you have? (e.g., API backend, database, cache, job processor)",
        "target_field": "workload_count",
        "priority": ClarificationPriority.REQUIRED,
        "default": "1",
    },
]

RECOMMENDED_QUESTIONS = [
    {
        "id": "q_budget",
        "text": "Do you have a monthly budget ceiling (USD)? (optional: press enter to skip)",
        "target_field": "budget_monthly_usd",
        "priority": ClarificationPriority.RECOMMENDED,
        "default": None,
    },
    {
        "id": "q_providers",
        "text": "Which cloud providers would you like us to evaluate? (aws/azure/gcp, comma-separated)",
        "target_field": "target_providers",
        "priority": ClarificationPriority.RECOMMENDED,
        "default": "aws,azure,gcp",
    },
    {
        "id": "q_region",
        "text": "Do you have a preferred region? (e.g., us-east-1, eastus, us-central1)",
        "target_field": "preferred_region",
        "priority": ClarificationPriority.RECOMMENDED,
        "default": "us-east-1",
    },
    {
        "id": "q_compliance",
        "text": "Any compliance requirements? (e.g., hipaa,pci-dss,sox,waf; comma-separated or 'none')",
        "target_field": "compliance_frameworks",
        "priority": ClarificationPriority.RECOMMENDED,
        "default": "waf",
    },
]


# ---------------------------------------------------------------------------
# Parsing utilities
# ---------------------------------------------------------------------------


def _parse_environment(text: str) -> EnvironmentType:
    """Parse environment from user input."""
    text_lower = text.strip().lower()
    # Check more specific patterns first to avoid prefix collisions
    if "dr" in text_lower or "disaster" in text_lower:
        return EnvironmentType.DR
    if "stag" in text_lower:
        return EnvironmentType.STAGING
    if "dev" in text_lower:
        return EnvironmentType.DEVELOPMENT
    if "prod" in text_lower:
        return EnvironmentType.PRODUCTION
    return EnvironmentType.PRODUCTION


def _parse_tier(text: str) -> WorkloadTier:
    """Parse tier from user input."""
    text_lower = text.strip().lower()
    if "mission" in text_lower:
        return WorkloadTier.MISSION_CRITICAL
    if "business" in text_lower:
        return WorkloadTier.BUSINESS_CRITICAL
    if "non" in text_lower or "non-critical" in text_lower:
        return WorkloadTier.NON_CRITICAL
    return WorkloadTier.BUSINESS_CRITICAL


def _parse_providers(text: str) -> list[CloudProvider]:
    """Parse provider list from user input."""
    providers = []
    text_lower = text.strip().lower()
    if "aws" in text_lower:
        providers.append(CloudProvider.AWS)
    if "azure" in text_lower:
        providers.append(CloudProvider.AZURE)
    if "gcp" in text_lower:
        providers.append(CloudProvider.GCP)
    return providers or [CloudProvider.AWS, CloudProvider.AZURE, CloudProvider.GCP]


def _parse_budget(text: str) -> float | None:
    """Parse budget from user input."""
    text = text.strip()
    if not text or text.lower() in ("skip", "none", "no"):
        return None
    match = re.search(r"(\d+(?:,\d{3})*|\d+(?:\.\d{2})?)", text.replace(",", ""))
    if match:
        return float(match.group(1))
    return None


def _parse_compliance(text: str) -> list[str]:
    """Parse compliance frameworks from user input."""
    text_lower = text.strip().lower()
    if "none" in text_lower or not text:
        return ["waf"]
    frameworks = []
    if "hipaa" in text_lower:
        frameworks.append("hipaa")
    if "pci" in text_lower or "pci-dss" in text_lower:
        frameworks.append("pci-dss")
    if "sox" in text_lower:
        frameworks.append("sox")
    if "waf" in text_lower:
        frameworks.append("waf")
    return frameworks or ["waf"]


# ---------------------------------------------------------------------------
# Workload parsing from raw input
# ---------------------------------------------------------------------------


def _extract_workloads_from_text(text: str) -> list[WorkloadRequirement]:
    """Attempt to extract workload components from raw user input.

    This is a heuristic approach — for detailed workload specs, the agent
    will ask follow-up questions. This just initializes a rough draft.

    Looks for keywords like: "kubernetes", "database", "postgres", "redis",
    "api", "web", "storage", etc.
    """
    workloads = []
    text_lower = text.lower()

    # Kubernetes / container workload
    if "kubernetes" in text_lower or "k8s" in text_lower or "eks" in text_lower:
        workloads.append(
            WorkloadRequirement(
                name="Kubernetes Cluster",
                description="Container orchestration platform",
                suggested_category=ServiceCategory.CONTAINER,
                scaling_pattern=ScalingPattern.STEADY,
                resources=ResourceSpec(replicas=3),
            )
        )

    # Database workload
    if (
        "database" in text_lower
        or "postgres" in text_lower
        or "mysql" in text_lower
        or "mongodb" in text_lower
    ):
        engine = "postgresql"
        if "mysql" in text_lower:
            engine = "mysql"
        if "mongo" in text_lower:
            engine = "mongodb"
        workloads.append(
            WorkloadRequirement(
                name="Database",
                description=f"Managed {engine} database",
                suggested_category=ServiceCategory.DATABASE,
                scaling_pattern=ScalingPattern.STEADY,
                resources=ResourceSpec(
                    database_engine=engine,
                    storage_gb=100,
                    high_availability=True,
                ),
            )
        )

    # Cache workload
    if "redis" in text_lower or "cache" in text_lower or "memcached" in text_lower:
        workloads.append(
            WorkloadRequirement(
                name="Cache Layer",
                description="In-memory data store",
                suggested_category=ServiceCategory.DATABASE,
                scaling_pattern=ScalingPattern.STEADY,
                resources=ResourceSpec(
                    database_engine="redis",
                    memory_gb=32,
                ),
            )
        )

    # VM / compute workload
    if (
        "vm" in text_lower
        or "virtual machine" in text_lower
        or "ec2" in text_lower
        or "compute" in text_lower
    ):
        workloads.append(
            WorkloadRequirement(
                name="Virtual Machines",
                description="General-purpose compute instances",
                suggested_category=ServiceCategory.COMPUTE,
                scaling_pattern=ScalingPattern.STEADY,
                resources=ResourceSpec(vcpus=4, memory_gb=16),
            )
        )

    # If no workloads detected, create a generic placeholder
    if not workloads:
        workloads.append(
            WorkloadRequirement(
                name="Workload",
                description="To be clarified",
                suggested_category=ServiceCategory.COMPUTE,
                scaling_pattern=ScalingPattern.STEADY,
            )
        )

    return workloads


# ---------------------------------------------------------------------------
# Main clarifier node
# ---------------------------------------------------------------------------


@observe()
async def run_clarifier_node(
    state: OrchestratorState,
    llm: BaseChatModel,
    pricing_service: PricingService,
) -> OrchestratorState:
    """Execute one clarification turn, or loop until complete.

    This is a LangGraph node function. It reads from state['messages'] and
    state['conversation'], appends clarification Q&A, and eventually sets
    state['conversation'].requirements_complete = True to gate the next agent.

    Args:
        state: Current OrchestratorState (TypedDict)
        llm: LLM for generating clarifications
        pricing_service: For context (e.g., listing available regions)

    Returns:
        Updated OrchestratorState with clarifications appended
    """
    log = logger.bind(
        agent="clarifier",
        request_id=state.get("request_id", "unknown"),
    )

    start_time = datetime.now(timezone.utc)
    log.info("clarifier_node_started")

    try:
        # Get/init conversation and workload request
        conversation = state.get("conversation") or {}
        workload_request = state.get("workload_request") or WorkloadRequest(
            project_name=state.get("project_name", "untitled"),
            raw_user_input=state.get("messages", [{}])[0].get("content", "")
            if state.get("messages")
            else "",
        )
        messages = state.get("messages", [])

        # Convert ChatMessage list to LangChain messages for LLM
        lc_messages: list[BaseMessage] = _chat_to_langchain(messages)

        # --- First turn: extract initial workloads from raw input ---
        if conversation.get("current_turn", 0) == 0:
            log.info("first_turn_initialization")

            # Parse raw input
            raw_input = workload_request.raw_user_input or ""
            workload_request.project_name = (
                workload_request.project_name or "untitled"
            )
            workload_request.workloads = _extract_workloads_from_text(raw_input)

            # Generate initial clarification questions
            questions = _generate_clarification_questions(conversation, workload_request)
            conversation["clarification_questions"] = [q.model_dump() for q in questions]
            conversation["current_turn"] = 1

            # Append LLM response with first question
            if questions:
                first_q = questions[0]
                assistant_msg = ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=first_q.get("text", "Tell me more about your requirements."),
                    agent_name="clarifier",
                    metadata={"question_id": first_q.get("id")},
                )
                messages.append(assistant_msg)

            log.info(
                "first_turn_complete",
                workload_count=len(workload_request.workloads),
                pending_questions=len(questions),
            )

        else:
            # --- Subsequent turns: parse answers, ask next question ---
            log.info("subsequent_turn", turn=conversation.get("current_turn", 1))

            # Find the last user message (their answer to previous question)
            user_answer = None
            for msg in reversed(messages):
                if isinstance(msg, ChatMessage) and msg.role == MessageRole.USER:
                    user_answer = msg.content
                    break

            if user_answer:
                questions = [
                    ClarificationQuestion(**q)
                    for q in conversation.get("clarification_questions", [])
                ]
                pending = [q for q in questions if q.status == ClarificationStatus.PENDING]

                if pending:
                    # Resolve the first pending question with the user's answer
                    pending[0].user_answer = user_answer
                    pending[0].status = ClarificationStatus.ANSWERED
                    pending[0].resolved_value = user_answer

                    # Apply to workload_request
                    _apply_clarification_to_request(
                        workload_request, pending[0], user_answer
                    )

                    # Check if there are more questions
                    remaining = [q for q in questions if q.status == ClarificationStatus.PENDING]

                    if remaining and conversation.get("current_turn", 0) < conversation.get(
                        "max_clarification_turns", 5
                    ):
                        # Ask next question
                        next_q = remaining[0]
                        assistant_msg = ChatMessage(
                            role=MessageRole.ASSISTANT,
                            content=next_q.question_text,
                            agent_name="clarifier",
                            metadata={"question_id": next_q.question_id},
                        )
                        messages.append(assistant_msg)
                        conversation["current_turn"] = conversation.get("current_turn", 1) + 1
                        log.info(
                            "next_question_asked",
                            question_id=next_q.question_id,
                            turn=conversation["current_turn"],
                        )
                    else:
                        # All questions done or max turns reached
                        conversation["requirements_complete"] = True
                        log.info("clarification_complete", turn=conversation.get("current_turn"))

        # Update state
        state["messages"] = messages
        state["conversation"] = conversation
        state["workload_request"] = workload_request

        # Track execution
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        if "agent_executions" not in state:
            state["agent_executions"] = {}
        state["agent_executions"]["clarifier"] = AgentExecution(
            agent_name="clarifier",
            status=AgentStatus.COMPLETED,
            started_at=start_time,
            completed_at=datetime.now(timezone.utc),
            duration_ms=elapsed,
        )

        log.info(
            "clarifier_node_completed",
            elapsed_ms=elapsed,
            total_messages=len(messages),
            requirements_complete=conversation.get("requirements_complete", False),
        )

        return state

    except Exception as e:
        log.error("clarifier_node_failed", exc_info=True)
        state["error"] = str(e)
        if "agent_executions" not in state:
            state["agent_executions"] = {}
        state["agent_executions"]["clarifier"] = AgentExecution(
            agent_name="clarifier",
            status=AgentStatus.FAILED,
            error_message=str(e),
        )
        raise


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _chat_to_langchain(messages: list[ChatMessage]) -> list[BaseMessage]:
    """Convert ChatMessage list to LangChain BaseMessage list."""
    lc_messages = []
    for msg in messages:
        if msg.role == MessageRole.USER:
            lc_messages.append(HumanMessage(content=msg.content))
        elif msg.role == MessageRole.ASSISTANT:
            from langchain_core.messages import AIMessage

            lc_messages.append(AIMessage(content=msg.content))
        elif msg.role == MessageRole.SYSTEM:
            lc_messages.append(SystemMessage(content=msg.content))
    return lc_messages


def _generate_clarification_questions(
    conversation: dict[str, Any],
    workload_request: WorkloadRequest,
) -> list[ClarificationQuestion]:
    """Generate the next set of clarification questions.

    Returns only REQUIRED questions on first pass, then RECOMMENDED.
    """
    questions = []

    # Always ask required questions first
    for template in REQUIRED_QUESTIONS:
        q = ClarificationQuestion(
            question_id=template["id"],
            question_text=template["text"],
            target_field=template["target_field"],
            priority=template["priority"],
            status=ClarificationStatus.PENDING,
            default_value=template.get("default"),
        )
        questions.append(q)

    # Then add recommended questions
    for template in RECOMMENDED_QUESTIONS:
        q = ClarificationQuestion(
            question_id=template["id"],
            question_text=template["text"],
            target_field=template["target_field"],
            priority=template["priority"],
            status=ClarificationStatus.PENDING,
            default_value=template.get("default"),
        )
        questions.append(q)

    return questions


def _apply_clarification_to_request(
    workload_request: WorkloadRequest,
    question: ClarificationQuestion,
    answer: str,
) -> None:
    """Apply a resolved clarification answer to the WorkloadRequest."""
    target = question.target_field

    if target == "project_name":
        workload_request.project_name = answer.strip()
    elif target == "environment":
        workload_request.environment = _parse_environment(answer)
    elif target == "tier":
        workload_request.tier = _parse_tier(answer)
    elif target == "budget_monthly_usd":
        budget = _parse_budget(answer)
        if budget is not None:
            workload_request.budget_monthly_usd = budget
    elif target == "target_providers":
        workload_request.target_providers = _parse_providers(answer)
    elif target == "preferred_region":
        workload_request.preferred_region = answer.strip()
    elif target == "compliance_frameworks":
        workload_request.compliance_frameworks = _parse_compliance(answer)