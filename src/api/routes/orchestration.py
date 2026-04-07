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
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

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
            # LangGraph's astream_events gives per-node lifecycle events
            async for event in graph.astream(
                initial_state,
                stream_mode="updates",
            ):
                # Each `event` is a dict {node_name: state_update}
                for node_name, update in event.items():
                    # Emit agent-started event
                    yield {
                        "event": "agent_update",
                        "data": json.dumps({
                            "request_id": request_id,
                            "agent": node_name,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "keys_updated": list(update.keys()) if isinstance(update, dict) else [],
                        }),
                    }

                    # If messages were appended, emit the latest one
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

            yield {
                "event": "pipeline_complete",
                "data": json.dumps({
                    "request_id": request_id,
                    "status": "completed",
                    "duration_ms": elapsed,
                }),
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
