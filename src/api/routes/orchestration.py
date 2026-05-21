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
from langfuse import Langfuse, observe


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
    build_enriched_input_from_structured,
    llm_clarify_turn,
)
from src.orchestrator.state import OrchestratorState, create_initial_state

logger = structlog.get_logger()

router = APIRouter(tags=["orchestration"])


# ---------------------------------------------------------------------------
# LangFuse flush helper
# ---------------------------------------------------------------------------


def _flush_langfuse() -> None:
    """Flush any buffered LangFuse traces so they are not lost."""
    try:
        Langfuse().flush()
    except Exception:
        logger.warning("langfuse_flush_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Pipeline execution helpers (parent trace context)
# ---------------------------------------------------------------------------


@observe(name="orchestrate_pipeline")
async def _execute_pipeline(
    graph: Any,
    initial_state: OrchestratorState,
) -> OrchestratorState:
    """Run the full graph with a parent LangFuse trace."""
    return await graph.ainvoke(initial_state)


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
    architecture_alternatives: list[dict[str, Any]] = []
    processor_architecture_insights: list[dict[str, Any]] = []
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
        result: OrchestratorState = await _execute_pipeline(graph, initial_state)
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
            architecture_alternatives=result.get("architecture_alternatives", []),
            processor_architecture_insights=result.get("processor_architecture_insights", []),
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
    finally:
        _flush_langfuse()


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
            architecture_alternatives = final_state.get("architecture_alternatives", [])
            arch_insights = final_state.get("processor_architecture_insights", [])

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
                    "architecture_alternatives": architecture_alternatives,
                    "processor_architecture_insights": arch_insights,
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
        finally:
            _flush_langfuse()

    return EventSourceResponse(_event_generator())


# ---------------------------------------------------------------------------
# POST /orchestrate/clarify — LLM-powered multi-turn requirement clarification
# ---------------------------------------------------------------------------

# In-memory session store (demo-grade; swap for Redis in production)
# Session structure:
#   {
#     "project_name": str,
#     "raw_input": str,         ← the user's very first message
#     "history": [(role, content), ...]  ← full conversation log
#   }
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


@router.post("/orchestrate/clarify", response_model=ClarifyResponse)
async def clarify_requirements(
    body: ClarifyRequest,
    request: Request,
) -> ClarifyResponse:
    """LLM-powered multi-turn requirement clarification.

    Each call passes the user's message to the LLM along with the full
    conversation history.  The LLM decides:

    * What was understood from the message.
    * What critical information is still missing.
    * What 1-2 questions to ask next (adaptive — not a fixed sequence).

    When the LLM determines it has enough context, it switches to
    ``status="ready"`` and the response includes an ``enriched_input``
    string ready to post to ``/orchestrate`` or ``/orchestrate/stream``.
    """
    request_id = body.request_id or str(uuid.uuid4())
    log = logger.bind(component="clarify", request_id=request_id)
    llm = request.app.state.llm

    is_new_session = request_id not in _clarify_sessions

    if is_new_session:
        log.info("clarify_new_session", input_len=len(body.user_input))
        _clarify_sessions[request_id] = {
            "project_name": body.project_name,
            "raw_input": body.user_input,
            "history": [],
        }

    session = _clarify_sessions[request_id]
    history: list[tuple[str, str]] = session["history"]

    log.info(
        "clarify_turn",
        turn=len(history) + 1,
        is_new_session=is_new_session,
        input_len=len(body.user_input),
    )

    try:
        result = await llm_clarify_turn(
            llm=llm,
            history=history,
            user_input=body.user_input,
            log=log,
        )
    except Exception:
        log.error("clarify_llm_call_failed", exc_info=True)
        return ClarifyResponse(
            request_id=request_id,
            status="clarifying",
            message=(
                "I'm having trouble processing your request right now. "
                "Could you tell me: what are you building and roughly how many users will it serve?"
            ),
        )
    finally:
        _flush_langfuse()

    # Update history — append user turn + architect response
    history.append(("user", body.user_input))
    history.append(("architect", result["response"]))

    if result["status"] == "ready":
        structured = result.get("structured", {})
        enriched = build_enriched_input_from_structured(
            raw_input=session["raw_input"],
            structured=structured,
        )
        # Clean up session memory
        del _clarify_sessions[request_id]
        log.info(
            "clarify_ready",
            turns=len(history) // 2,
            enriched_len=len(enriched),
        )
        return ClarifyResponse(
            request_id=request_id,
            status="ready",
            message=result["response"],
            enriched_input=enriched,
        )

    log.info("clarify_still_clarifying", turn=len(history) // 2)
    return ClarifyResponse(
        request_id=request_id,
        status="clarifying",
        message=result["response"],
    )
