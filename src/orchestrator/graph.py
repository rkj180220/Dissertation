"""LangGraph workflow graph — orchestrates the 5-agent pipeline.

Wires the Clarifier → Profiler → Sizer → FinOps → RFP Writer
pipeline as a ``StateGraph`` with a conditional edge for the
Clarifier's multi-turn clarification loop.

### Graph Structure

```
START → clarifier ──[needs more info?]──→ clarifier (loop)
              │
              └──[complete]──→ profiler → sizer → finops → rfp_writer → END
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
from src.agents.sizer import run_sizer_node
from src.orchestrator.state import OrchestratorState
from src.services.pricing_service import PricingService

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Node wrappers (close over the injected dependencies)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(
    llm: BaseChatModel,
    pricing_service: PricingService,
) -> StateGraph:
    """Build and compile the LangGraph orchestration workflow.

    Creates a ``StateGraph`` with 5 agent nodes and a conditional
    edge for the Clarifier loop.  Dependencies (LLM, PricingService)
    are injected via closures.

    Args:
        llm: LLM instance (``BaseChatModel`` — never a provider-specific
            class).
        pricing_service: Initialised ``PricingService`` with providers
            registered.

    Returns:
        A compiled ``StateGraph`` ready for ``ainvoke()``.
    """
    log = logger.bind(component="graph_builder")
    log.info("building_graph")

    graph = StateGraph(OrchestratorState)

    # ── Add nodes ─────────────────────────────────────────────
    graph.add_node("clarifier", _make_clarifier_node(llm, pricing_service))
    graph.add_node("profiler", _make_profiler_node(llm))
    graph.add_node("sizer", _make_sizer_node(llm, pricing_service))
    graph.add_node("finops", _make_finops_node(llm, pricing_service))
    graph.add_node("rfp_writer", _make_rfp_writer_node(llm))

    # ── Add edges ─────────────────────────────────────────────
    # START → clarifier
    graph.add_edge(START, "clarifier")

    # Clarifier → conditional: loop or proceed to profiler
    graph.add_conditional_edges(
        "clarifier",
        _should_continue_clarifying,
        {
            "clarifier": "clarifier",
            "profiler": "profiler",
        },
    )

    # Linear pipeline: profiler → sizer → finops → rfp_writer → END
    graph.add_edge("profiler", "sizer")
    graph.add_edge("sizer", "finops")
    graph.add_edge("finops", "rfp_writer")
    graph.add_edge("rfp_writer", END)

    log.info("graph_built", nodes=5, edges=6)

    compiled = graph.compile()
    log.info("graph_compiled")

    return compiled
