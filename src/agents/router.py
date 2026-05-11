"""Router+Orchestrator Agent — Intent classification and execution planning.

The Router+Orchestrator is the entry-point agent for all requests where a
prior session exists (turn ≥ 2).  It performs two tasks in a single LLM call:

1. **Intent classification** — determines what the user actually wants.
2. **Execution planning** — produces a concrete ``ExecutionPlan`` that tells
   every downstream agent what to run and how (full pipeline vs. delta).

### Intent Types

| Route | Trigger Pattern |
|-------|----------------|
| ``new_request`` | No prior session or explicit restart |
| ``amendment`` | "add X", "change Y", "what if we add…" |
| ``validate`` | "is this correct?", "validate the architecture" |
| ``answer`` | "what does X mean?", "why was X chosen?" |
| ``clarify`` | Ambiguous follow-up |

### Principal Architect Reasoning (§22g)

All LLM calls embed the Principal Architect chain-of-thought prompt so the
router doesn't just classify the surface intent but dissects the real scope
of change before committing to an execution plan.

### Usage

```python
from src.agents.router import run_router_node

result = await run_router_node(state, llm)
# state["execution_plan"] is now populated
# state["routing_decision"] mirrors the intent
```
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langfuse import observe

from src.models.conversation import ChatMessage, MessageRole
from src.orchestrator.state import (
    AgentExecution,
    AgentStatus,
    ExecutionPlan,
    OrchestratorState,
)

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# System prompt — Principal Architect Reasoning (§22g)
# ---------------------------------------------------------------------------

_ROUTER_SYSTEM_PROMPT = """\
You are a principal cloud architect with 20+ years of experience acting as an \
intelligent orchestration agent.

You have the full conversation history and current system state. Your job is to:
(1) Understand what the user ACTUALLY wants (not just surface intent)
(2) Identify the MINIMUM set of agents that need to run
(3) Identify the SPECIFIC components that changed (not "everything")
(4) Identify which RFP sections are affected

Before producing output, reason through the problem:

<architect_reasoning>
STEP 1 — UNDERSTAND THE PROBLEM
  - What is the user ACTUALLY trying to achieve? (not just surface intent)
  - What constraints are non-negotiable? (compliance, availability, budget, latency)
  - What assumptions am I making that could be wrong?

STEP 2 — IDENTIFY THE RISKS
  - Is this an amendment that requires re-pricing, or just an explanation request?
  - Could this be a new requirement that invalidates the current architecture?
  - What scaling inflection points does this change introduce?

STEP 3 — EVALUATE ALTERNATIVES
  - Could this be answered from the existing state without re-running the pipeline?
  - Is this an amendment to an existing component or a completely new one?
  - What is the minimum set of agents that must re-run?

STEP 4 — CHALLENGE MY INITIAL ASSUMPTION
  - Am I routing to 'amendment' because it is genuinely an amendment, or because
    the wording sounds like one?
  - If I route to 'answer', will the user be satisfied or will they need fresh analysis?
  - What data or signals would change my routing decision?

STEP 5 — COMMIT WITH RATIONALE
  - State the route clearly
  - Why this route wins
  - What specific components / sections are affected
</architect_reasoning>

After completing your reasoning, respond in this EXACT format (no extra text):

ROUTE: <new_request|amendment|validate|answer|clarify>
CONFIDENCE: <high|medium|low>
CHANGED_COMPONENTS: <comma-separated component names, or "ALL" or "NONE">
AGENTS_TO_RUN: <comma-separated from: profiler,sizer,finops,validator,rfp_writer>
SCOPE: <full|delta_only>
RFP_SECTIONS: <comma-separated section numbers, e.g. "§4,§7" or "NONE">
AMENDMENT_DELTA: <one-sentence description of what changed, or "N/A">
DIRECT_ANSWER: <full answer if ROUTE=answer, else "N/A">
"""


# ---------------------------------------------------------------------------
# Parse the structured LLM response
# ---------------------------------------------------------------------------


def _parse_router_response(raw: str) -> dict[str, str]:
    """Parse the structured text response from the router LLM.

    Args:
        raw: Raw LLM text output.

    Returns:
        Dict mapping field names to their raw string values.
    """
    fields = {
        "ROUTE": "new_request",
        "CONFIDENCE": "medium",
        "CHANGED_COMPONENTS": "ALL",
        "AGENTS_TO_RUN": "profiler,sizer,finops,validator,rfp_writer",
        "SCOPE": "full",
        "RFP_SECTIONS": "NONE",
        "AMENDMENT_DELTA": "N/A",
        "DIRECT_ANSWER": "N/A",
    }
    for key in fields:
        pattern = rf"^{key}:\s*(.+)$"
        match = re.search(pattern, raw, re.MULTILINE | re.IGNORECASE)
        if match:
            fields[key] = match.group(1).strip()
    return fields


def _build_execution_plan(parsed: dict[str, str], turn_number: int) -> ExecutionPlan:
    """Convert parsed router fields into an ``ExecutionPlan``.

    Args:
        parsed: Output from ``_parse_router_response()``.
        turn_number: Current turn index within the session.

    Returns:
        An ``ExecutionPlan`` TypedDict.
    """
    route = parsed["ROUTE"].lower().strip()
    scope = parsed["SCOPE"].lower().strip()

    # Map route → pipeline_mode
    mode_map = {
        "new_request": "full",
        "amendment": "amendment",
        "validate": "validation",
        "answer": "query",
        "clarify": "full",
    }
    pipeline_mode = mode_map.get(route, "full")

    # Parse agents_to_run
    agents_raw = parsed["AGENTS_TO_RUN"].lower()
    agents_to_run = [a.strip() for a in agents_raw.split(",") if a.strip()]

    # Parse scope_components
    comp_raw = parsed["CHANGED_COMPONENTS"]
    if comp_raw.upper() in ("ALL", "NONE", "N/A"):
        scope_components: list[str] = []
    else:
        scope_components = [c.strip() for c in comp_raw.split(",") if c.strip()]

    # Parse rfp sections
    sec_raw = parsed["RFP_SECTIONS"]
    if sec_raw.upper() in ("NONE", "N/A"):
        rfp_sections: list[str] = []
    else:
        rfp_sections = [s.strip() for s in sec_raw.split(",") if s.strip()]

    return ExecutionPlan(
        intent=route,
        pipeline_mode=pipeline_mode,
        agents_to_run=agents_to_run,
        scope=scope if scope in ("full", "delta_only") else "full",
        scope_components=scope_components,
        rfp_amendment_sections=rfp_sections,
        amendment_delta=parsed["AMENDMENT_DELTA"],
        confidence=parsed["CONFIDENCE"].lower().strip(),
        turn_number=turn_number,
    )


# ---------------------------------------------------------------------------
# State summary builder
# ---------------------------------------------------------------------------


def _build_state_summary(state: OrchestratorState) -> str:
    """Build a concise summary of current state for the router prompt.

    Args:
        state: Current ``OrchestratorState``.

    Returns:
        Multi-line string with relevant excerpts.
    """
    parts: list[str] = []

    workload_request = state.get("workload_request")
    if workload_request:
        workloads = getattr(workload_request, "workloads", [])
        parts.append(
            f"Current workloads ({len(workloads)}): "
            + ", ".join(w.name for w in workloads[:10])
        )
        budget = getattr(workload_request, "budget_monthly_usd", None)
        if budget:
            parts.append(f"Budget: ${budget:,.0f}/month")

    recommended = state.get("recommended_provider")
    if recommended:
        parts.append(f"Recommended provider: {recommended}")

    rfp = state.get("rfp_document", "")
    if rfp:
        parts.append(f"RFP exists ({len(rfp)} chars)")
        # First 300 chars of the RFP as context
        parts.append("RFP excerpt: " + rfp[:300].replace("\n", " "))

    prior_messages = state.get("messages", [])[-3:]
    if prior_messages:
        parts.append("Recent messages:")
        for msg in prior_messages:
            role = getattr(msg, "role", "?")
            content = getattr(msg, "content", "")
            parts.append(f"  [{role}] {content[:150]}")

    return "\n".join(parts) if parts else "No prior state."


# ---------------------------------------------------------------------------
# Main router node
# ---------------------------------------------------------------------------


@observe()
async def run_router_node(
    state: OrchestratorState,
    llm: BaseChatModel,
) -> OrchestratorState:
    """Execute the Router+Orchestrator agent — classify intent and plan execution.

    This LangGraph node is invoked on turn ≥ 2 (when a session exists and an
    RFP has already been generated).  It determines the intent of the new user
    message and writes an ``ExecutionPlan`` to state that controls which agents
    run downstream.

    Args:
        state: Current ``OrchestratorState``.
        llm: LLM instance (``BaseChatModel`` — never provider-specific).

    Returns:
        Updated ``OrchestratorState`` with ``execution_plan``, ``routing_decision``,
        ``pipeline_mode``, and ``turn_number`` populated.
    """
    log = logger.bind(
        agent="router",
        request_id=state.get("request_id", "unknown"),
    )
    start_time = datetime.now(timezone.utc)
    log.info("router_node_started")

    try:
        new_message = ""
        for msg in reversed(state.get("messages", [])):
            role = getattr(msg, "role", None)
            if role and str(role).lower() in ("user", MessageRole.USER.value if hasattr(MessageRole, "USER") else "user"):
                new_message = getattr(msg, "content", "")
                break

        turn_number = state.get("turn_number", 0) + 1
        state_summary = _build_state_summary(state)

        user_prompt = (
            f"STATE_SUMMARY:\n{state_summary}\n\n"
            f"USER_INPUT:\n{new_message or '(empty)'}"
        )

        log.debug("router_llm_call", turn=turn_number, input_length=len(user_prompt))

        response = await llm.ainvoke(
            [
                SystemMessage(content=_ROUTER_SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        log.debug("router_llm_response", raw_length=len(raw))

        parsed = _parse_router_response(raw)
        execution_plan = _build_execution_plan(parsed, turn_number)

        duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        log.info(
            "router_node_completed",
            intent=execution_plan["intent"],
            pipeline_mode=execution_plan["pipeline_mode"],
            agents_to_run=execution_plan["agents_to_run"],
            scope=execution_plan["scope"],
            confidence=execution_plan["confidence"],
            duration_ms=round(duration_ms, 1),
        )

        # Build a summary message for the conversation log
        answer = parsed["DIRECT_ANSWER"]
        summary_content = (
            answer
            if execution_plan["intent"] == "answer" and answer != "N/A"
            else (
                f"[Router] Intent: {execution_plan['intent']} | "
                f"Mode: {execution_plan['pipeline_mode']} | "
                f"Agents: {', '.join(execution_plan['agents_to_run'])} | "
                f"Delta: {execution_plan['amendment_delta']}"
            )
        )

        agent_exec = state.get("agent_executions", {}).get("router", AgentExecution(agent_name="router"))
        agent_exec.status = AgentStatus.COMPLETED
        agent_exec.completed_at = datetime.now(timezone.utc)
        agent_exec.duration_ms = duration_ms

        updated_executions = {**state.get("agent_executions", {}), "router": agent_exec}

        return {
            **state,
            "execution_plan": execution_plan,
            "routing_decision": execution_plan["intent"],
            "pipeline_mode": execution_plan["pipeline_mode"],
            "turn_number": turn_number,
            "current_agent": "router",
            "agent_executions": updated_executions,
            "messages": [
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=summary_content,
                )
            ],
        }

    except Exception:
        duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        log.error("router_node_failed", exc_info=True, duration_ms=round(duration_ms, 1))

        # Fall back to a safe default: full pipeline re-run
        fallback_plan = ExecutionPlan(
            intent="new_request",
            pipeline_mode="full",
            agents_to_run=["profiler", "sizer", "finops", "validator", "rfp_writer"],
            scope="full",
            scope_components=[],
            rfp_amendment_sections=[],
            amendment_delta="Router failed — defaulting to full pipeline",
            confidence="low",
            turn_number=state.get("turn_number", 0) + 1,
        )
        return {
            **state,
            "execution_plan": fallback_plan,
            "routing_decision": "new_request",
            "pipeline_mode": "full",
            "turn_number": fallback_plan["turn_number"],
            "current_agent": "router",
        }
