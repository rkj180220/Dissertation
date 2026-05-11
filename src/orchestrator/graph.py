"""LangGraph workflow graph — orchestrates the 6-agent pipeline.

Wires the Router → Clarifier → Profiler → Sizer → FinOps → Validator
→ RFP Writer pipeline as a ``StateGraph``.

### Graph Structure

```
START → clarifier ──[complete]──→ profiler → sizer → finops → validator → rfp_writer → END
```

Turn ≥ 2 (session amendment) entry point:
```
START → router ──[new_request/amendment/clarify]──→ clarifier/profiler → ... → END
                └──[validate]──→ validator → END
                └──[answer]──→ END (direct answer in messages)
```

### Usage

```python
from src.orchestrator.graph import build_graph

graph = build_graph(llm=my_llm, pricing_service=my_pricing)
result = await graph.ainvoke(initial_state)
```
"""

from __future__ import annotations

from functools import partial
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langfuse import observe
from langgraph.graph import END, START, StateGraph

from src.agents.clarifier import run_clarifier_node
from src.agents.finops import run_finops_node
from src.agents.profiler import run_profiler_node
from src.agents.rfp_writer import run_rfp_writer_node
from src.agents.router import run_router_node
from src.agents.sizer import run_sizer_node
from src.agents.validator import run_validator_node
from src.orchestrator.state import OrchestratorState
from src.services.pricing_service import PricingService

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Node wrappers (close over the injected dependencies)
# ---------------------------------------------------------------------------


def _make_router_node(llm: BaseChatModel):
    """Create a router node function with injected LLM.

    Args:
        llm: LLM instance.

    Returns:
        Async callable compatible with LangGraph node.
    """

    @observe(name="router_node")
    async def _node(state: OrchestratorState) -> OrchestratorState:
        log = logger.bind(agent="router", node="graph_node")
        log.info("router_node_invoked")
        return await run_router_node(state, llm)

    return _node


def _make_clarifier_node(
    llm: BaseChatModel,
    pricing_service: PricingService,
):
    """Create a clarifier node function with injected dependencies.

    Args:
        llm: LLM instance.
        pricing_service: Pricing service instance.

    Returns:
        Async callable compatible with LangGraph node.
    """

    @observe(name="clarifier_node")
    async def _node(state: OrchestratorState) -> OrchestratorState:
        log = logger.bind(agent="clarifier", node="graph_node")
        log.info("clarifier_node_invoked")
        return await run_clarifier_node(state, llm, pricing_service)

    return _node


def _make_profiler_node(llm: BaseChatModel):
    """Create a profiler node function with injected LLM.

    Args:
        llm: LLM instance.

    Returns:
        Async callable compatible with LangGraph node.
    """

    @observe(name="profiler_node")
    async def _node(state: OrchestratorState) -> OrchestratorState:
        log = logger.bind(agent="profiler", node="graph_node")
        log.info("profiler_node_invoked")
        return await run_profiler_node(state, llm)

    return _node


def _make_sizer_node(
    llm: BaseChatModel,
    pricing_service: PricingService,
):
    """Create a sizer node function with injected dependencies.

    Args:
        llm: LLM instance.
        pricing_service: Pricing service instance.

    Returns:
        Async callable compatible with LangGraph node.
    """

    @observe(name="sizer_node")
    async def _node(state: OrchestratorState) -> OrchestratorState:
        log = logger.bind(agent="sizer", node="graph_node")
        log.info("sizer_node_invoked")
        return await run_sizer_node(state, llm, pricing_service)

    return _node


def _make_finops_node(
    llm: BaseChatModel,
    pricing_service: PricingService,
):
    """Create a finops node function with injected dependencies.

    Args:
        llm: LLM instance.
        pricing_service: Pricing service instance.

    Returns:
        Async callable compatible with LangGraph node.
    """

    @observe(name="finops_node")
    async def _node(state: OrchestratorState) -> OrchestratorState:
        log = logger.bind(agent="finops", node="graph_node")
        log.info("finops_node_invoked")
        return await run_finops_node(state, llm, pricing_service)

    return _node


def _make_validator_node(llm: BaseChatModel):
    """Create a validator node function with injected LLM.

    Args:
        llm: LLM instance.

    Returns:
        Async callable compatible with LangGraph node.
    """

    @observe(name="validator_node")
    async def _node(state: OrchestratorState) -> OrchestratorState:
        log = logger.bind(agent="validator", node="graph_node")
        log.info("validator_node_invoked")
        return await run_validator_node(state, llm)

    return _node


def _make_rfp_writer_node(llm: BaseChatModel):
    """Create an RFP writer node function with injected LLM.

    Args:
        llm: LLM instance.

    Returns:
        Async callable compatible with LangGraph node.
    """

    @observe(name="rfp_writer_node")
    async def _node(state: OrchestratorState) -> OrchestratorState:
        log = logger.bind(agent="rfp_writer", node="graph_node")
        log.info("rfp_writer_node_invoked")
        return await run_rfp_writer_node(state, llm)

    return _node


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------


def _should_continue_clarifying(state: OrchestratorState) -> str:
    """Determine if the Clarifier should loop or proceed.

    Checks the ``ConversationState.should_continue_clarifying`` property.
    If ``True``, routes back to the clarifier; otherwise continues to
    the profiler.

    Args:
        state: Current orchestrator state.

    Returns:
        ``"clarifier"`` to loop or ``"profiler"`` to proceed.
    """
    conversation = state.get("conversation")
    if conversation is not None and hasattr(conversation, "should_continue_clarifying"):
        if conversation.should_continue_clarifying:
            logger.debug("clarifier_routing", decision="loop")
            return "clarifier"

    logger.debug("clarifier_routing", decision="proceed")
    return "profiler"


def _route_after_router(state: OrchestratorState) -> str:
    """Route after the Router+Orchestrator based on intent.

    Args:
        state: Current orchestrator state.

    Returns:
        Next node name: ``"clarifier"`` | ``"profiler"`` | ``"validator"`` | ``END``.
    """
    intent = state.get("routing_decision", "new_request")
    plan = state.get("execution_plan", {})
    agents = plan.get("agents_to_run", []) if plan else []

    if intent == "answer":
        # Direct answer — already written to messages, skip pipeline
        logger.debug("router_routing", decision=END, intent=intent)
        return END
    if intent == "validate":
        logger.debug("router_routing", decision="validator", intent=intent)
        return "validator"
    if intent in ("amendment", "clarify") and "profiler" not in agents:
        # Router says only validator or rfp_writer needed
        logger.debug("router_routing", decision="validator", intent=intent)
        return "validator"
    # Default: full pipeline via clarifier
    logger.debug("router_routing", decision="clarifier", intent=intent)
    return "clarifier"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(
    llm: BaseChatModel,
    pricing_service: PricingService,
    include_router: bool = False,
) -> Any:
    """Build and compile the LangGraph orchestration workflow.

    Creates a ``StateGraph`` with 6 agent nodes, a conditional edge for the
    Clarifier loop, and a Validator before the RFP Writer.

    Args:
        llm: LLM instance (``BaseChatModel`` — never a provider-specific class).
        pricing_service: Initialised ``PricingService`` with providers registered.
        include_router: If ``True``, adds the Router+Orchestrator node at
            ``START`` for multi-turn session support.  Defaults to ``False``
            (fresh request, single-turn) for backwards compatibility.

    Returns:
        A compiled ``StateGraph`` ready for ``ainvoke()``.
    """
    log = logger.bind(component="graph_builder")
    log.info("building_graph", include_router=include_router)

    graph = StateGraph(OrchestratorState)

    # ── Add nodes ─────────────────────────────────────────────
    graph.add_node("clarifier", _make_clarifier_node(llm, pricing_service))
    graph.add_node("profiler", _make_profiler_node(llm))
    graph.add_node("sizer", _make_sizer_node(llm, pricing_service))
    graph.add_node("finops", _make_finops_node(llm, pricing_service))
    graph.add_node("validator", _make_validator_node(llm))
    graph.add_node("rfp_writer", _make_rfp_writer_node(llm))

    # ── Linear pipeline edges ──────────────────────────────────
    # clarifier → profiler → sizer → finops → validator → rfp_writer → END
    graph.add_edge("profiler", "sizer")
    graph.add_edge("sizer", "finops")
    graph.add_edge("finops", "validator")
    graph.add_edge("validator", "rfp_writer")
    graph.add_edge("rfp_writer", END)

    node_count = 6
    edge_count = 5

    if include_router:
        # ── Router node at START (turn ≥ 2 / multi-turn) ──────
        graph.add_node("router", _make_router_node(llm))
        graph.add_edge(START, "router")
        graph.add_conditional_edges(
            "router",
            _route_after_router,
            {
                "clarifier": "clarifier",
                "profiler": "profiler",
                "validator": "validator",
                END: END,
            },
        )
        # Clarifier conditional loop
        graph.add_conditional_edges(
            "clarifier",
            _should_continue_clarifying,
            {"clarifier": "clarifier", "profiler": "profiler"},
        )
        node_count += 1
        edge_count += 2
    else:
        # ── Simple entry: START → clarifier (turn 1) ───────────
        graph.add_edge(START, "clarifier")
        graph.add_edge("clarifier", "profiler")
        edge_count += 2

    log.info("graph_built", nodes=node_count, edges=edge_count)

    compiled = graph.compile()
    log.info("graph_compiled", include_router=include_router)

    return compiled
