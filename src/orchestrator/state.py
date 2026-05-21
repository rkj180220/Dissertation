"""LangGraph shared state schema.

Defines the ``OrchestratorState`` TypedDict that flows through every
node in the LangGraph workflow.  Each agent reads what it needs from
this dict and writes its outputs back.

Design decisions
~~~~~~~~~~~~~~~~
* **TypedDict** (not Pydantic) — LangGraph uses TypedDict natively for
  its ``StateGraph`` type parameter.
* **Annotated + operator.add** on list fields — gives LangGraph
  *append-only* reducer semantics so each agent appends messages /
  items rather than overwriting the previous agent's output.
* **Pydantic models inside** — the *values* stored in state fields
  are still Pydantic ``BaseModel`` instances for validation; only the
  top-level container is a TypedDict.

Flow::

    ┌──────────┐     ┌──────────┐     ┌───────┐     ┌────────┐     ┌───────────┐
    │ Clarifier│────▶│ Profiler │────▶│ Sizer │────▶│ FinOps │────▶│ RFP Writer│
    │ (loop)   │     │          │     │       │     │        │     │           │
    └──────────┘     └──────────┘     └───────┘     └────────┘     └───────────┘

    State fields written per agent:
    ─────────────────────────────────────────────────────────────────────────────
    Clarifier       → messages, conversation, workload_request
    Profiler        → workload_profile, messages
    Sizer           → sized_results, messages
    FinOps          → cost_comparison, messages
    Validator       → validation_report, architecture_alternatives, messages
    RFP Writer      → rfp_document, messages
    Router+Orch     → execution_plan, routing_decision, pipeline_mode, turn_number
"""

from __future__ import annotations

import operator
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field

from src.models.cloud_resource import CloudProvider
from src.models.conversation import ChatMessage, ConversationState
from src.models.pricing import NormalizedPriceItem
from src.models.workload import WorkloadProfile, WorkloadRequest


# ---------------------------------------------------------------------------
# Execution plan model (Router+Orchestrator — P15d)
# ---------------------------------------------------------------------------


class ExecutionPlan(TypedDict, total=False):
    """Concrete execution plan produced by the Router+Orchestrator agent.

    Written to ``OrchestratorState`` on every turn ≥ 2.  Downstream agents
    read this to understand their scope (full pipeline vs. delta-only).
    """

    intent: str
    """Route type: ``new_request`` | ``amendment`` | ``validate`` | ``answer`` | ``clarify``."""

    pipeline_mode: str
    """Execution mode: ``full`` | ``amendment`` | ``validation`` | ``query``."""

    agents_to_run: list[str]
    """Names of agents that must run, e.g. ``["profiler", "sizer", "finops", "rfp_writer"]``."""

    scope: str
    """``full`` or ``delta_only``.  Delta-only agents re-process only ``scope_components``."""

    scope_components: list[str]
    """Component names to re-process when ``scope == "delta_only"``."""

    rfp_amendment_sections: list[str]
    """RFP section identifiers to regenerate, e.g. ``["§4", "§7"]``."""

    amendment_delta: str
    """One-sentence description of what changed."""

    confidence: str
    """Router's confidence in the classification: ``high`` | ``medium`` | ``low``."""

    turn_number: int
    """Turn index within the session (increments per user message)."""

    preferred_architecture: str
    """Target architecture for delta/amendment runs: ``self_hosted_serverless`` | ``managed_serverless`` | ``containers`` | ``hybrid``. Empty string on full runs."""


# ---------------------------------------------------------------------------
# Agent execution tracking
# ---------------------------------------------------------------------------


class AgentStatus(str, Enum):
    """Lifecycle status of a single agent within the pipeline."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentExecution(BaseModel):
    """Metadata about one agent's run — for observability."""

    agent_name: str = Field(description="E.g. 'clarifier', 'profiler'")
    status: AgentStatus = Field(default=AgentStatus.PENDING)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    duration_ms: float | None = Field(default=None, ge=0)
    error_message: str | None = Field(default=None)
    retry_count: int = Field(default=0, ge=0)

    @property
    def elapsed_ms(self) -> float | None:
        """Calculate duration from timestamps if not explicitly set.

        Returns wall-clock ms when running (started_at only), final duration
        when completed (both timestamps set), or falls back to duration_ms.
        """
        if self.started_at and self.completed_at:
            delta = self.completed_at - self.started_at
            return delta.total_seconds() * 1000
        if self.started_at:
            from datetime import datetime, timezone
            delta = datetime.now(timezone.utc) - self.started_at
            return delta.total_seconds() * 1000
        return self.duration_ms


# ---------------------------------------------------------------------------
# Sized result (Sizer agent output per workload per provider)
# ---------------------------------------------------------------------------


class SizedWorkloadResult(BaseModel):
    """Sizer agent's SKU selection for a single workload component.

    Maps one ``WorkloadRequirement`` to the best-fit ``NormalizedPriceItem``
    from each target cloud provider.
    """

    workload_name: str = Field(description="References WorkloadRequirement.name")
    provider: CloudProvider
    selected_sku: NormalizedPriceItem | None = Field(
        default=None,
        description="Best-fit SKU from the provider's catalog",
    )
    alternative_skus: list[NormalizedPriceItem] = Field(
        default_factory=list,
        description="Runner-up SKUs for comparison",
    )
    monthly_cost_usd: float = Field(default=0.0, ge=0)
    fit_score: float = Field(
        default=0.0, ge=0, le=1.0,
        description="How well the SKU fits the requirement (1.0 = perfect)",
    )
    rationale: str = Field(
        default="",
        description="LLM-generated reasoning for this selection",
    )


# ---------------------------------------------------------------------------
# LangGraph shared state
# ---------------------------------------------------------------------------


class OrchestratorState(TypedDict, total=False):
    """Shared state that flows through every LangGraph node.

    Fields use ``Annotated[list[T], operator.add]`` for append-only
    semantics — each agent *adds* to the list, never overwrites.

    Non-list fields (dicts, Pydantic models) use last-writer-wins:
    whatever the last agent writes is the final value.
    """

    # ── Request / session identifiers ─────────────────────────
    request_id: str
    """Unique correlation ID for the entire pipeline run."""

    project_name: str
    """Customer / project name from the WorkloadRequest."""

    # ── Chat history (append-only) ────────────────────────────
    messages: Annotated[list[ChatMessage], operator.add]
    """Full conversation log — every agent appends its messages."""

    # ── Clarifier outputs ─────────────────────────────────────
    conversation: ConversationState
    """Multi-turn clarification state (last-writer-wins)."""

    workload_request: WorkloadRequest
    """Parsed & enriched workload request (Clarifier's final output)."""

    # ── Profiler outputs ──────────────────────────────────────
    workload_profile: WorkloadProfile
    """Aggregated component analysis (Profiler's output)."""

    # ── Sizer outputs ─────────────────────────────────────────
    sized_results: Annotated[list[SizedWorkloadResult], operator.add]
    """Per-workload, per-provider SKU selections (append-only)."""

    # ── FinOps outputs ────────────────────────────────────────
    cost_comparison: dict[str, Any]
    """Multi-provider cost breakdown (serialised CostComparison)."""

    recommended_provider: str | None
    """Provider slug recommended by FinOps ('aws' | 'azure' | 'gcp')."""

    savings_opportunities: Annotated[list[dict[str, Any]], operator.add]
    """RI / SP / spot savings identified by FinOps (append-only)."""

    # ── RFP Writer outputs ────────────────────────────────────
    rfp_document: str
    """Generated RFP / procurement document (Markdown)."""

    executive_summary: str
    """LLM-generated executive summary for stakeholders."""

    # ── Compliance ────────────────────────────────────────────
    compliance_report: dict[str, Any]
    """Serialised ComplianceReport (WAF checks)."""

    # ── Agent execution tracking ──────────────────────────────
    agent_executions: dict[str, AgentExecution]
    """agent_name → AgentExecution — tracks status & timing."""

    # ── Pipeline control ──────────────────────────────────────
    current_agent: str
    """Name of the agent currently executing."""

    error: str | None
    """Set if the pipeline encounters a fatal error."""

    # ── KPIs (dissertation evaluation) ────────────────────────
    kpis: dict[str, Any]
    """Measurable KPIs accumulated during the run."""

    # ── Session management (P15h) ─────────────────────────────
    session_id: str | None
    """Persistent session UUID — populated for turn ≥ 2 (multi-turn conversations)."""

    turn_number: int
    """Turn index within the session; 0 for fresh requests, ≥ 1 for amendments."""

    # ── Router+Orchestrator outputs (P15d / P15h) ─────────────
    execution_plan: ExecutionPlan
    """Written by Router+Orchestrator; tells each downstream agent what to run."""

    routing_decision: str
    """Mirrors ``execution_plan.intent`` for quick conditional-edge access."""

    pipeline_mode: str
    """Mirrors ``execution_plan.pipeline_mode``."""

    # ── Validator outputs (P15e / P15h) ───────────────────────
    validation_report: dict[str, Any]
    """5-check quality-gate report (Validator agent output)."""

    architecture_alternatives: list[dict[str, Any]]
    """Ranked architecture options from the Validator / architecture_selector."""

    processor_architecture_insights: list[dict[str, Any]]
    """P17 — Per-workload processor architecture fit insights (ARM vs x86).
    Populated by the Sizer node after scoring. Each entry is a serialised
    ProcessorArchitectureEntry from src.models.recommendation."""


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def create_initial_state(
    request_id: str,
    project_name: str = "",
    raw_user_input: str = "",
) -> OrchestratorState:
    """Create a fresh ``OrchestratorState`` with sensible defaults.

    Call this at the start of every pipeline invocation to
    initialise the shared state before handing it to LangGraph.

    Args:
        request_id: Unique correlation ID for this run.
        project_name: Customer / project identifier.
        raw_user_input: The user's original chat message.

    Returns:
        A fully-initialised ``OrchestratorState`` dict.
    """
    now = datetime.now(timezone.utc).isoformat()

    initial_messages: list[ChatMessage] = []
    if raw_user_input:
        initial_messages.append(
            ChatMessage(role="user", content=raw_user_input),
        )

    return OrchestratorState(
        request_id=request_id,
        project_name=project_name,
        messages=initial_messages,
        conversation=ConversationState(conversation_id=request_id),
        workload_request=WorkloadRequest(
            project_name=project_name or "untitled",
            raw_user_input=raw_user_input,
        ),
        workload_profile=WorkloadProfile(),
        sized_results=[],
        cost_comparison={},
        recommended_provider=None,
        savings_opportunities=[],
        rfp_document="",
        executive_summary="",
        compliance_report={},
        agent_executions={
            name: AgentExecution(agent_name=name)
            for name in ("clarifier", "profiler", "sizer", "finops", "validator", "rfp_writer", "router")
        },
        current_agent="clarifier",
        error=None,
        kpis={
            "pipeline_started_at": now,
            "total_llm_calls": 0,
            "total_api_calls": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        },
        session_id=None,
        turn_number=0,
        execution_plan=ExecutionPlan(
            intent="new_request",
            pipeline_mode="full",
            agents_to_run=["clarifier", "profiler", "sizer", "finops", "validator", "rfp_writer"],
            scope="full",
            scope_components=[],
            rfp_amendment_sections=[],
            amendment_delta="N/A",
            confidence="high",
            turn_number=0,
        ),
        routing_decision="new_request",
        pipeline_mode="full",
        validation_report={},
        architecture_alternatives=[],
        processor_architecture_insights=[],
    )
