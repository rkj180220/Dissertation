"""Orchestration endpoints — invoke and stream the LangGraph pipeline.

* ``POST /orchestrate``       — run the full pipeline, return JSON result.
* ``POST /orchestrate/stream`` — run the pipeline, stream agent progress
  as Server-Sent Events (SSE).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

import structlog


def _json_default(obj: Any) -> Any:
    """Fallback serializer for ``json.dumps`` — handles datetime + Pydantic."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "value"):          # enums
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from src.agents.clarifier import (
    _extract_workloads_from_text,
    _parse_budget,
    _parse_compliance,
    _parse_environment,
    _parse_providers,
)
from src.orchestrator.state import OrchestratorState, create_initial_state

logger = structlog.get_logger()

router = APIRouter(tags=["orchestration"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class OrchestrationRequest(BaseModel):
    """Payload for the orchestration endpoints."""

    user_input: str = Field(
        ..., min_length=1, max_length=10000,
        description="The user's workload description or follow-up message",
    )
    project_name: str = Field(
        default="untitled",
        max_length=200,
        description="Customer / project identifier",
    )
    request_id: str | None = Field(
        default=None,
        description="Caller-supplied correlation ID (auto-generated if omitted)",
    )


class OrchestrationResponse(BaseModel):
    """Envelope for the full pipeline result."""

    request_id: str
    status: str
    rfp_document: str = ""
    executive_summary: str = ""
    recommended_provider: str | None = None
    cost_comparison: dict[str, Any] = {}
    compliance_report: dict[str, Any] = {}
    error: str | None = None
    duration_ms: float | None = None


# ---------------------------------------------------------------------------
# POST /orchestrate — synchronous JSON response
# ---------------------------------------------------------------------------


@router.post("/orchestrate", response_model=OrchestrationResponse)
async def orchestrate(body: OrchestrationRequest, request: Request) -> OrchestrationResponse:
    """Run the full 5-agent pipeline and return the result as JSON.

    This blocks until all agents complete.  For real-time streaming,
    use the ``/orchestrate/stream`` endpoint instead.
    """
    request_id = body.request_id or str(uuid.uuid4())
    log = logger.bind(component="orchestrate", request_id=request_id)
    log.info("orchestrate_started", project=body.project_name)

    graph = request.app.state.graph
    start_ts = datetime.now(timezone.utc)

    initial_state = create_initial_state(
        request_id=request_id,
        project_name=body.project_name,
        raw_user_input=body.user_input,
    )

    try:
        result: OrchestratorState = await graph.ainvoke(initial_state)
        elapsed = (datetime.now(timezone.utc) - start_ts).total_seconds() * 1000

        log.info(
            "orchestrate_completed",
            duration_ms=elapsed,
            recommended_provider=result.get("recommended_provider"),
        )

        return OrchestrationResponse(
            request_id=request_id,
            status="completed",
            rfp_document=result.get("rfp_document", ""),
            executive_summary=result.get("executive_summary", ""),
            recommended_provider=result.get("recommended_provider"),
            cost_comparison=result.get("cost_comparison", {}),
            compliance_report=result.get("compliance_report", {}),
            duration_ms=elapsed,
        )
    except Exception as exc:
        elapsed = (datetime.now(timezone.utc) - start_ts).total_seconds() * 1000
        log.error("orchestrate_failed", exc_info=True, duration_ms=elapsed)
        return OrchestrationResponse(
            request_id=request_id,
            status="failed",
            error=str(exc),
            duration_ms=elapsed,
        )


# ---------------------------------------------------------------------------
# POST /orchestrate/stream — SSE streaming
# ---------------------------------------------------------------------------


@router.post("/orchestrate/stream")
async def orchestrate_stream(
    body: OrchestrationRequest,
    request: Request,
) -> EventSourceResponse:
    """Run the pipeline and stream agent progress via Server-Sent Events.

    Each SSE event has:
    * ``event: agent_started`` / ``agent_completed`` / ``pipeline_complete`` / ``error``
    * ``data:`` — JSON payload with agent name, status, partial results.
    """
    request_id = body.request_id or str(uuid.uuid4())

    async def _event_generator() -> AsyncGenerator[dict[str, str]]:
        log = logger.bind(component="orchestrate_stream", request_id=request_id)
        log.info("stream_started", project=body.project_name)
        start_ts = datetime.now(timezone.utc)

        graph = request.app.state.graph

        initial_state = create_initial_state(
            request_id=request_id,
            project_name=body.project_name,
            raw_user_input=body.user_input,
        )

        try:
            # LangGraph's astream gives per-node state updates
            final_state: dict[str, Any] = {}
            async for event in graph.astream(
                initial_state,
                stream_mode="updates",
            ):
                # Each `event` is a dict {node_name: state_update}
                for node_name, update in event.items():
                    if isinstance(update, dict):
                        final_state.update(update)

                    # Emit agent-update event
                    yield {
                        "event": "agent_update",
                        "data": json.dumps({
                            "request_id": request_id,
                            "agent": node_name,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "keys_updated": list(update.keys()) if isinstance(update, dict) else [],
                        }),
                    }

                    # If messages were appended, emit each new one
                    messages = update.get("messages", []) if isinstance(update, dict) else []
                    for msg in messages:
                        content = msg.content if hasattr(msg, "content") else str(msg)
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "request_id": request_id,
                                "agent": node_name,
                                "content": content,
                            }),
                        }

            elapsed = (datetime.now(timezone.utc) - start_ts).total_seconds() * 1000
            log.info("stream_completed", duration_ms=elapsed)

            # Serialize final results for the pipeline_complete event
            cost_comparison = final_state.get("cost_comparison", {})
            compliance_report = final_state.get("compliance_report", {})

            yield {
                "event": "pipeline_complete",
                "data": json.dumps({
                    "request_id": request_id,
                    "status": "completed",
                    "duration_ms": elapsed,
                    "rfp_document": final_state.get("rfp_document", ""),
                    "executive_summary": final_state.get("executive_summary", ""),
                    "recommended_provider": final_state.get("recommended_provider"),
                    "cost_comparison": cost_comparison,
                    "compliance_report": compliance_report,
                }, default=_json_default),
            }

        except Exception as exc:
            elapsed = (datetime.now(timezone.utc) - start_ts).total_seconds() * 1000
            log.error("stream_failed", exc_info=True, duration_ms=elapsed)
            yield {
                "event": "error",
                "data": json.dumps({
                    "request_id": request_id,
                    "error": str(exc),
                    "duration_ms": elapsed,
                }),
            }

    return EventSourceResponse(_event_generator())


# ---------------------------------------------------------------------------
# POST /orchestrate/clarify — multi-turn requirement clarification
# ---------------------------------------------------------------------------

# In-memory session store (demo-grade; use Redis/DB for production)
_clarify_sessions: dict[str, dict[str, Any]] = {}


class ClarifyRequest(BaseModel):
    """Payload for the clarification endpoint."""

    user_input: str = Field(
        ..., min_length=1, max_length=10000,
        description="User's message (initial request or answer to a question)",
    )
    project_name: str = Field(
        default="untitled",
        max_length=200,
        description="Customer / project identifier",
    )
    request_id: str | None = Field(
        default=None,
        description="Session ID (auto-generated on first call, required on follow-ups)",
    )


class ClarifyResponse(BaseModel):
    """Response from the clarification endpoint."""

    request_id: str
    status: str  # "clarifying" | "ready"
    message: str
    enriched_input: str | None = None  # Only populated when status="ready"


_QUESTION_TEXT: dict[str, str] = {
    "environment": (
        "What **environment** is this deployment for?\n"
        "(production / staging / development / disaster_recovery)"
    ),
    "providers": (
        "Which **cloud providers** should I compare?\n"
        "(AWS, Azure, GCP — or any combination)"
    ),
    "budget": (
        "Do you have a **monthly budget** target?\n"
        "(e.g. `$5000` — or type `skip` to proceed without a budget constraint)"
    ),
    "compliance": (
        "Any **compliance requirements**?\n"
        "(e.g. hipaa, pci-dss, sox — or `none` for standard WAF checks only)"
    ),
}


@router.post("/orchestrate/clarify", response_model=ClarifyResponse)
async def clarify_requirements(
    body: ClarifyRequest,
    request: Request,
) -> ClarifyResponse:
    """Multi-turn clarification endpoint.

    * **First call** — parse the user's initial input, acknowledge what was
      understood, and ask about the first missing field.
    * **Subsequent calls** — process the user's answer, then ask the next
      question or return ``status="ready"`` with an ``enriched_input``
      string that can be passed directly to ``/orchestrate/stream``.
    """
    request_id = body.request_id or str(uuid.uuid4())
    log = logger.bind(component="clarify", request_id=request_id)

    if request_id not in _clarify_sessions:
        # ── First turn: parse initial input ──────────────────────
        log.info("clarify_new_session", input_len=len(body.user_input))

        raw = body.user_input
        text_lower = raw.lower()
        workloads = _extract_workloads_from_text(raw)

        extracted: dict[str, Any] = {
            "workloads": workloads,
            "environment": None,
            "budget": None,
            "providers": [],
            "compliance": None,
        }

        # Try heuristic extraction from the raw input
        if any(kw in text_lower for kw in (
            "production", "prod ", "staging", "development", "dev ",
        )):
            extracted["environment"] = _parse_environment(raw)

        budget = _parse_budget(raw)
        if budget is not None:
            extracted["budget"] = budget

        # _parse_providers defaults to ALL if none detected; only store
        # if the user explicitly named at least one provider.
        if any(kw in text_lower for kw in ("aws", "azure", "gcp")):
            extracted["providers"] = _parse_providers(raw)

        if any(kw in text_lower for kw in ("hipaa", "pci", "sox")):
            extracted["compliance"] = _parse_compliance(raw)

        # Determine which fields still need clarification
        pending: list[str] = []
        if not extracted["environment"]:
            pending.append("environment")
        if not extracted["providers"]:
            pending.append("providers")
        if extracted["budget"] is None:
            pending.append("budget")
        if not extracted["compliance"]:
            pending.append("compliance")

        session: dict[str, Any] = {
            "project_name": body.project_name,
            "raw_input": raw,
            "extracted": extracted,
            "pending": pending,
            "current_field": None,
        }
        _clarify_sessions[request_id] = session

        # Build acknowledgment
        wk_names = [w.name for w in workloads]
        ack_parts = [
            f"I've identified **{len(wk_names)} workload(s)** from your request:",
        ]
        for w in workloads:
            ack_parts.append(f"  - **{w.name}**: {w.description}")

        if extracted["providers"]:
            prov_str = ", ".join(p.value.upper() for p in extracted["providers"])
            ack_parts.append(f"\nProviders: **{prov_str}**")
        if extracted["environment"]:
            ack_parts.append(
                f"Environment: **{extracted['environment'].value}**"
            )
        if extracted["budget"] is not None:
            ack_parts.append(f"Budget: **${extracted['budget']:,.0f}/mo**")

        acknowledgment = "\n".join(ack_parts)

        # Ask first pending question (or mark ready if nothing to ask)
        question = _pop_next_question(session)
        if question:
            log.info("clarify_asking", field=session["current_field"])
            return ClarifyResponse(
                request_id=request_id,
                status="clarifying",
                message=f"{acknowledgment}\n\n{question}",
            )
        else:
            enriched = _build_enriched_input(session)
            log.info("clarify_ready_immediately")
            return ClarifyResponse(
                request_id=request_id,
                status="ready",
                message=(
                    f"{acknowledgment}\n\n"
                    "All requirements captured. Starting analysis..."
                ),
                enriched_input=enriched,
            )

    else:
        # ── Subsequent turn: process the user's answer ───────────
        session = _clarify_sessions[request_id]
        field = session["current_field"]
        answer = body.user_input.strip()
        log.info("clarify_answer", field=field, answer_len=len(answer))

        # Apply the answer to the session
        if field == "environment":
            session["extracted"]["environment"] = _parse_environment(answer)
        elif field == "providers":
            session["extracted"]["providers"] = _parse_providers(answer)
        elif field == "budget":
            session["extracted"]["budget"] = _parse_budget(answer)
        elif field == "compliance":
            session["extracted"]["compliance"] = _parse_compliance(answer)

        # Ask next pending question
        question = _pop_next_question(session)
        if question:
            ack = _format_answer_ack(field, session["extracted"])
            log.info("clarify_asking", field=session["current_field"])
            return ClarifyResponse(
                request_id=request_id,
                status="clarifying",
                message=f"{ack} {question}",
            )
        else:
            enriched = _build_enriched_input(session)
            # Clean up session
            del _clarify_sessions[request_id]
            log.info("clarify_ready")
            return ClarifyResponse(
                request_id=request_id,
                status="ready",
                message=(
                    "All requirements captured. "
                    "Starting the analysis pipeline..."
                ),
                enriched_input=enriched,
            )


# ---------------------------------------------------------------------------
# Clarification helpers
# ---------------------------------------------------------------------------


def _pop_next_question(session: dict[str, Any]) -> str | None:
    """Pop and return the next pending question, or *None* if done."""
    pending: list[str] = session["pending"]
    if not pending:
        session["current_field"] = None
        return None
    field = pending.pop(0)
    session["current_field"] = field
    return _QUESTION_TEXT.get(field, f"What is the {field}?")


def _format_answer_ack(field: str | None, extracted: dict[str, Any]) -> str:
    """Short acknowledgment of the user's answer."""
    if field == "environment" and extracted.get("environment"):
        return f"Got it — **{extracted['environment'].value}** environment."
    if field == "providers" and extracted.get("providers"):
        prov = ", ".join(p.value.upper() for p in extracted["providers"])
        return f"Comparing **{prov}**."
    if field == "budget":
        if extracted.get("budget") is not None:
            return f"Budget set to **${extracted['budget']:,.0f}/mo**."
        return "No budget constraint — understood."
    if field == "compliance" and extracted.get("compliance"):
        return f"Compliance: **{', '.join(extracted['compliance'])}**."
    return "Noted."


def _build_enriched_input(session: dict[str, Any]) -> str:
    """Combine original input + extracted fields into a single text
    that the pipeline's clarifier node can parse heuristically."""
    parts = [session["raw_input"]]
    e = session["extracted"]

    if e.get("environment"):
        parts.append(f"Environment: {e['environment'].value}")
    if e.get("budget") is not None:
        parts.append(f"Budget: ${e['budget']}/month")
    if e.get("providers"):
        parts.append(
            f"Providers: {', '.join(p.value for p in e['providers'])}"
        )
    if e.get("compliance"):
        parts.append(f"Compliance: {', '.join(e['compliance'])}")

    return "\n".join(parts)
