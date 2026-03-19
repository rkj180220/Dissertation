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
    Clarifier  → messages, conversation, workload_request
    Profiler   → workload_profile, messages
    Sizer      → sized_results, messages
    FinOps     → cost_comparison, messages
    RFP Writer → rfp_document, messages
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
        """Calculate duration from timestamps if not explicitly set."""
        if self.started_at and self.completed_at:
            delta = self.completed_at - self.started_at
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
            for name in ("clarifier", "profiler", "sizer", "finops", "rfp_writer")
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
    )
