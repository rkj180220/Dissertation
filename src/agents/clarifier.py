"""Clarifier Agent — Single-pass intelligent requirement extraction.

The Clarifier is the entry point to the orchestration pipeline. It analyses
the user's raw input, extracts structured workload requirements using both
heuristics and the LLM, applies smart defaults for any gaps, and produces
a refined ``WorkloadRequest`` ready for the Profiler.

This is a **single-pass** agent — it does not loop or ask follow-up
questions. The user's initial input is processed once, and the pipeline
proceeds immediately to the Profiler.

### Flow

```
User's raw input (from chat)
      │
      ▼
┌─────────────────────────┐
│ Heuristic extraction    │  ← Keywords → WorkloadRequirements
│ Parse budget/env/tier   │  ← Regex/keyword parsing
│ LLM enrichment          │  ← Infer missing fields from context
│ Apply defaults           │  ← Fill remaining gaps
│ Mark complete            │  ← requirements_complete = True
└────────┬────────────────┘
         │
         ▼
   Proceed to Profiler
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
# state['conversation'].requirements_complete is now True
# state['workload_request'] is populated
# state['messages'] has clarifier summary appended
```
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langfuse import observe

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.conversation import (
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
    """Parse budget from user input.

    Matches explicit ``$`` amounts (e.g. ``$5000``, ``$5,000/mo``) or
    standalone numbers when the user answers a direct budget question.
    Avoids matching incidental numbers like "3 microservices".
    """
    text = text.strip()
    if not text or text.lower() in ("skip", "none", "no"):
        return None
    # Explicit dollar sign: $5000, $5,000.00
    match = re.search(r"\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", text)
    if match:
        return float(match.group(1).replace(",", ""))
    # Standalone number (direct answer to "what's your budget?"): "5000"
    match = re.match(
        r"^\s*(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:/\s*(?:mo|month))?\s*$",
        text,
        re.IGNORECASE,
    )
    if match:
        return float(match.group(1).replace(",", ""))
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

    # Object storage workload
    if (
        "s3" in text_lower
        or "object storage" in text_lower
        or "blob" in text_lower
        or "bucket" in text_lower
    ):
        workloads.append(
            WorkloadRequirement(
                name="Object Storage",
                description="Scalable object/blob storage",
                suggested_category=ServiceCategory.STORAGE,
                scaling_pattern=ScalingPattern.GROWING,
                resources=ResourceSpec(storage_gb=1000, storage_type="object"),
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
    """Single-pass requirement extraction and enrichment.

    Extracts workloads, budget, environment, providers, and constraints
    from the user's raw input using heuristics, parses explicit values,
    then uses the LLM to infer anything that's missing. Sets
    ``requirements_complete = True`` and proceeds.

    Args:
        state: Current OrchestratorState (TypedDict)
        llm: LLM for enriching the requirement analysis
        pricing_service: For context (e.g., listing available regions)

    Returns:
        Updated OrchestratorState with refined workload_request and
        only NEW messages (for the append-only reducer).
    """
    log = logger.bind(
        agent="clarifier",
        request_id=state.get("request_id", "unknown"),
    )

    start_time = datetime.now(timezone.utc)
    log.info("clarifier_node_started")

    try:
        workload_request = state.get("workload_request") or WorkloadRequest(
            project_name=state.get("project_name", "untitled"),
            raw_user_input=_extract_first_user_content(state.get("messages", [])),
        )

        raw_input = workload_request.raw_user_input or ""
        log.info("parsing_raw_input", raw_length=len(raw_input))

        # --- 1. Heuristic extraction of workloads ---
        workload_request.workloads = _extract_workloads_from_text(raw_input)
        log.info("workloads_extracted", count=len(workload_request.workloads))

        # --- 2. Parse explicit values from raw input ---
        _parse_explicit_values(workload_request, raw_input)
        log.info(
            "explicit_values_parsed",
            environment=str(workload_request.environment),
            budget=workload_request.budget_monthly_usd,
            providers=[str(p) for p in (workload_request.target_providers or [])],
        )

        # --- 3. LLM enrichment — infer missing context ---
        llm_summary = await _llm_enrich_requirements(
            llm, raw_input, workload_request, log
        )

        # --- 4. Apply smart defaults for anything still missing ---
        _apply_defaults(workload_request)

        # --- 5. Build summary message ---
        summary_parts = [
            f"**Clarifier Analysis Complete** — {len(workload_request.workloads)} workload(s) identified:",
        ]
        for w in workload_request.workloads:
            category = w.suggested_category.value if w.suggested_category else "unknown"
            summary_parts.append(f"  • **{w.name}**: {category} — {w.description}")

        summary_parts.append(f"\n**Environment**: {workload_request.environment.value}")
        summary_parts.append(f"**Tier**: {workload_request.tier.value}")
        if workload_request.budget_monthly_usd:
            summary_parts.append(f"**Budget**: ${workload_request.budget_monthly_usd:,.0f}/mo")
        if workload_request.target_providers:
            providers_str = ", ".join(p.value for p in workload_request.target_providers)
            summary_parts.append(f"**Providers**: {providers_str}")
        if workload_request.preferred_region:
            summary_parts.append(f"**Region**: {workload_request.preferred_region}")

        if llm_summary:
            summary_parts.append(f"\n{llm_summary}")

        summary_content = "\n".join(summary_parts)

        summary_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=summary_content,
            agent_name="clarifier",
            metadata={"phase": "requirement_extraction"},
        )

        # --- 6. Mark requirements complete ---
        from src.models.conversation import ConversationState as _CS
        conversation = _CS(
            conversation_id=state.get("request_id", ""),
            current_turn=1,
            requirements_complete=True,
        )

        # --- 7. Return ONLY new data (messages uses operator.add reducer) ---
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

        log.info(
            "clarifier_node_completed",
            elapsed_ms=elapsed,
            workload_count=len(workload_request.workloads),
            environment=str(workload_request.environment),
        )

        return {
            "messages": [summary_message],  # append-only: only new messages
            "conversation": conversation,
            "workload_request": workload_request,
            "current_agent": "profiler",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "clarifier": AgentExecution(
                    agent_name="clarifier",
                    status=AgentStatus.COMPLETED,
                    started_at=start_time,
                    completed_at=datetime.now(timezone.utc),
                    duration_ms=elapsed,
                ),
            },
        }

    except Exception as e:
        log.error("clarifier_node_failed", exc_info=True)
        return {
            "error": str(e),
            "current_agent": "clarifier",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "clarifier": AgentExecution(
                    agent_name="clarifier",
                    status=AgentStatus.FAILED,
                    error_message=str(e),
                ),
            },
        }


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _extract_first_user_content(messages: list) -> str:
    """Safely extract content from the first message (dict or ChatMessage)."""
    if not messages:
        return ""
    first = messages[0]
    if hasattr(first, "content"):
        return first.content
    if isinstance(first, dict):
        return first.get("content", "")
    return ""


def _parse_explicit_values(req: WorkloadRequest, raw_input: str) -> None:
    """Parse explicitly stated values from user's raw input text."""
    text = raw_input.lower()

    # Environment detection
    if any(kw in text for kw in ("production", "prod ")):
        req.environment = EnvironmentType.PRODUCTION
    elif "staging" in text:
        req.environment = EnvironmentType.STAGING
    elif any(kw in text for kw in ("development", "dev ")):
        req.environment = EnvironmentType.DEVELOPMENT

    # Tier detection
    if "mission" in text and "critical" in text:
        req.tier = WorkloadTier.MISSION_CRITICAL
    elif "non" in text and "critical" in text:
        req.tier = WorkloadTier.NON_CRITICAL

    # Budget detection
    budget = _parse_budget(raw_input)
    if budget is not None:
        req.budget_monthly_usd = budget

    # Provider detection
    providers = _parse_providers(raw_input)
    if providers:
        req.target_providers = providers

    # Region detection
    region_patterns = [
        r"(us-east-\d|us-west-\d|eu-west-\d|eu-central-\d|ap-southeast-\d)",
        r"(eastus|westus\d?|northeurope|westeurope|centralus)",
        r"(us-central\d|us-east\d|europe-west\d|asia-east\d)",
    ]
    for pattern in region_patterns:
        match = re.search(pattern, raw_input, re.IGNORECASE)
        if match:
            req.preferred_region = match.group(1)
            break

    # Compliance detection
    compliance = _parse_compliance(raw_input)
    if compliance and compliance != ["waf"]:
        req.compliance_frameworks = compliance


async def _llm_enrich_requirements(
    llm: BaseChatModel,
    raw_input: str,
    req: WorkloadRequest,
    log: Any,
) -> str:
    """Use LLM to analyse the user's input and provide contextual enrichment.

    Returns a summary string for the user. Does NOT modify req — we keep
    heuristic + default values authoritative to avoid hallucination.
    """
    prompt = f"""Analyse this cloud infrastructure request and provide a brief technical assessment.

USER INPUT:
{raw_input}

EXTRACTED SO FAR:
- Workloads: {', '.join(w.name for w in req.workloads)}
- Environment: {req.environment.value if req.environment else 'not specified'}
- Budget: {'$' + str(req.budget_monthly_usd) + '/mo' if req.budget_monthly_usd else 'not specified'}
- Providers: {', '.join(p.value for p in req.target_providers) if req.target_providers else 'all'}

Respond in 2-3 sentences. Note any implicit requirements, potential concerns, or assumptions being made. Do NOT ask questions — just analyse."""

    try:
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        summary = response.content if hasattr(response, "content") else str(response)
        log.info("llm_enrichment_complete", summary_length=len(summary))
        return summary
    except Exception:
        log.warning("llm_enrichment_failed", exc_info=True)
        return ""


def _apply_defaults(req: WorkloadRequest) -> None:
    """Fill in smart defaults for any missing fields."""
    if not req.environment:
        req.environment = EnvironmentType.PRODUCTION
    if not req.tier:
        req.tier = WorkloadTier.BUSINESS_CRITICAL
    if not req.target_providers:
        req.target_providers = [CloudProvider.AWS, CloudProvider.AZURE, CloudProvider.GCP]
    if not req.preferred_region:
        req.preferred_region = "us-east-1"
    if not req.compliance_frameworks:
        req.compliance_frameworks = ["waf"]
    if not req.project_name or req.project_name == "untitled":
        # Try to infer from input
        req.project_name = req.project_name or "untitled"