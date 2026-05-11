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
    Handles million/billion shorthand and annual→monthly conversion.
    Avoids matching incidental numbers like "3 microservices".
    """
    text = text.strip()
    if not text or text.lower() in ("skip", "none", "no"):
        return None

    # Fix PROV-3: million/billion shorthand with annual→monthly conversion
    # Matches: $3.2 million/year, $3.2M per year, $3.2 million annually, $3M/yr, $3.2B/year
    million_match = re.search(
        r"\$\s*(\d+(?:\.\d+)?)\s*(million|M|billion|B)\b",
        text,
        re.IGNORECASE,
    )
    if million_match:
        val = float(million_match.group(1))
        unit = million_match.group(2).lower()
        if unit in ("billion", "b"):
            val *= 1_000_000_000
        else:
            val *= 1_000_000
        # Convert annual to monthly if the surrounding text says "year" / "annual"
        text_lower = text.lower()
        if any(kw in text_lower for kw in ("/year", "per year", "/yr", "per yr", "annually", "annual")):
            val = val / 12
        return round(val)

    # Explicit dollar sign: $5000, $5,000.00, $266,666/mo
    match = re.search(r"\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", text)
    if match:
        raw = float(match.group(1).replace(",", ""))
        # Convert annual to monthly if flagged
        text_lower = text.lower()
        if any(kw in text_lower for kw in ("/year", "per year", "/yr", "per yr", "annually", "annual")):
            raw = raw / 12
        return round(raw)

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
    """Extract workload components from raw user input using heuristics.

    Handles count patterns (``3 microservices``), K8s cluster management
    as a separate cost item, database engine propagation, and common
    infrastructure components.

    Args:
        text: Raw user input describing infrastructure needs.

    Returns:
        List of ``WorkloadRequirement`` — one per logical component.
    """
    workloads: list[WorkloadRequirement] = []
    text_lower = text.lower()

    # --- Microservices / API services on K8s/containers ---
    microservice_count = _parse_count(
        text_lower,
        [
            r"(\d+)\s*(?:micro[\-\s]?services?)",
            r"(\d+)\s*(?:services?\s+(?:on|in|running)\s+(?:k8s|kubernetes|eks|aks|gke|containers?))",
            r"(\d+)\s*(?:containerized\s+(?:services?|apps?|applications?))",
        ],
    )
    has_k8s = any(
        kw in text_lower
        for kw in ("kubernetes", "k8s", "eks", "aks", "gke", "container")
    )

    if microservice_count > 0:
        # Create individual CONTAINER workloads for each microservice
        for i in range(1, microservice_count + 1):
            workloads.append(
                WorkloadRequirement(
                    name=f"Microservice {i}",
                    description=f"Containerized microservice ({i} of {microservice_count})",
                    suggested_category=ServiceCategory.CONTAINER,
                    scaling_pattern=ScalingPattern.STEADY,
                    resources=ResourceSpec(
                        cpu_request_millicores=500,
                        cpu_limit_millicores=1000,
                        memory_request_mb=512,
                        memory_limit_mb=1024,
                        replicas=2,
                    ),
                )
            )
        # Also add K8s cluster management as a separate cost item
        if has_k8s:
            workloads.append(
                WorkloadRequirement(
                    name="Kubernetes Cluster Management",
                    description="K8s control plane + cluster management fee",
                    suggested_category=ServiceCategory.KUBERNETES,
                    scaling_pattern=ScalingPattern.STEADY,
                    resources=ResourceSpec(),
                    notes="cluster_management_fee",
                    spot_eligible=False,
                )
            )
    elif has_k8s:
        # K8s mentioned without explicit microservice count.
        # If text also contains "microservices" or "containeris*" infer app workloads.
        has_implicit_containers = any(
            kw in text_lower
            for kw in ("microservice", "containeris", "containeriz", "containers running")
        )
        if has_implicit_containers:
            # Add one generic CONTAINER workload to represent the application pods
            workloads.append(
                WorkloadRequirement(
                    name="Containerised Application",
                    description="Containerised microservice workloads running on Kubernetes",
                    suggested_category=ServiceCategory.CONTAINER,
                    scaling_pattern=ScalingPattern.BURSTY,
                    resources=ResourceSpec(
                        cpu_request_millicores=500,
                        cpu_limit_millicores=2000,
                        memory_request_mb=512,
                        memory_limit_mb=2048,
                        replicas=3,
                    ),
                )
            )
        # Always add the cluster management fee as a separate line item
        workloads.append(
            WorkloadRequirement(
                name="Kubernetes Cluster",
                description="Container orchestration platform with worker nodes",
                suggested_category=ServiceCategory.KUBERNETES,
                scaling_pattern=ScalingPattern.STEADY,
                resources=ResourceSpec(
                    cpu_request_millicores=2000,
                    cpu_limit_millicores=4000,
                    memory_request_mb=4096,
                    memory_limit_mb=8192,
                    replicas=3,
                ),
                notes="cluster_management_fee",
                spot_eligible=False,
            )
        )

    # --- API / web server (non-containerised) ---
    api_count = _parse_count(
        text_lower,
        [
            r"(\d+)\s*(?:api\s*(?:servers?|instances?|endpoints?))",
            r"(\d+)\s*(?:web\s*(?:servers?|instances?|apps?))",
        ],
    )
    if api_count > 0 and not microservice_count:
        # Only add if not already captured as microservices
        for i in range(1, api_count + 1):
            workloads.append(
                WorkloadRequirement(
                    name=f"API Server {i}",
                    description=f"API / web application server ({i} of {api_count})",
                    suggested_category=ServiceCategory.COMPUTE,
                    scaling_pattern=ScalingPattern.BURSTY,
                    resources=ResourceSpec(vcpus=2, memory_gb=4),
                )
            )
    elif not microservice_count and any(
        kw in text_lower for kw in ("api", "web server", "web app", "backend")
    ):
        workloads.append(
            WorkloadRequirement(
                name="API Server",
                description="API / web application server",
                suggested_category=ServiceCategory.COMPUTE,
                scaling_pattern=ScalingPattern.BURSTY,
                resources=ResourceSpec(vcpus=2, memory_gb=4),
            )
        )

    # --- Database workload ---
    if any(
        kw in text_lower
        for kw in ("database", "postgres", "mysql", "mariadb", "mongodb", "sql server", "rds")
    ):
        engine = "postgresql"  # default
        if "mysql" in text_lower:
            engine = "mysql"
        elif "mariadb" in text_lower:
            engine = "mariadb"
        elif "mongo" in text_lower:
            engine = "mongodb"
        elif "sql server" in text_lower or "mssql" in text_lower:
            engine = "sqlserver"

        workloads.append(
            WorkloadRequirement(
                name=f"{engine.title()} Database",
                description=f"Managed {engine} database",
                suggested_category=ServiceCategory.DATABASE,
                scaling_pattern=ScalingPattern.STEADY,
                resources=ResourceSpec(
                    vcpus=2,
                    memory_gb=8,
                    storage_gb=100,
                    database_engine=engine,
                    high_availability=True,
                ),
            )
        )

    # --- Cache workload ---
    if any(kw in text_lower for kw in ("redis", "cache", "memcached", "elasticache")):
        cache_engine = "redis"
        if "memcached" in text_lower:
            cache_engine = "memcached"
        workloads.append(
            WorkloadRequirement(
                name="Cache Layer",
                description=f"In-memory data store ({cache_engine})",
                suggested_category=ServiceCategory.DATABASE,
                scaling_pattern=ScalingPattern.STEADY,
                resources=ResourceSpec(
                    vcpus=2,
                    memory_gb=8,
                    database_engine=cache_engine,
                ),
            )
        )

    # --- VM / bare compute workload ---
    if any(
        kw in text_lower
        for kw in ("vm", "virtual machine", "ec2", "compute instance")
    ):
        vm_count = _parse_count(text_lower, [r"(\d+)\s*(?:vm|virtual\s+machine|instance)"])
        count = max(vm_count, 1)
        workloads.append(
            WorkloadRequirement(
                name="Virtual Machines",
                description="General-purpose compute instances",
                suggested_category=ServiceCategory.COMPUTE,
                scaling_pattern=ScalingPattern.STEADY,
                count=count,
                resources=ResourceSpec(vcpus=4, memory_gb=16),
            )
        )

    # --- Object storage ---
    if any(kw in text_lower for kw in ("s3", "object storage", "blob", "bucket")):
        workloads.append(
            WorkloadRequirement(
                name="Object Storage",
                description="Scalable object/blob storage",
                suggested_category=ServiceCategory.STORAGE,
                scaling_pattern=ScalingPattern.GROWING,
                resources=ResourceSpec(storage_gb=1000, storage_type="object"),
            )
        )

    # --- Serverless functions ---
    if any(kw in text_lower for kw in ("lambda", "serverless", "cloud function", "azure function")):
        workloads.append(
            WorkloadRequirement(
                name="Serverless Functions",
                description="Event-driven serverless compute",
                suggested_category=ServiceCategory.SERVERLESS_FUNCTION,
                scaling_pattern=ScalingPattern.BURSTY,
                resources=ResourceSpec(
                    invocations_per_month=1_000_000,
                    avg_duration_ms=200,
                    memory_mb=256,
                ),
            )
        )

    # --- Load balancer (infer from microservices or web apps) ---
    if has_k8s or microservice_count > 0 or api_count > 0:
        workloads.append(
            WorkloadRequirement(
                name="Load Balancer",
                description="Application load balancer for traffic distribution",
                suggested_category=ServiceCategory.NETWORKING,
                scaling_pattern=ScalingPattern.STEADY,
                resources=ResourceSpec(),
            )
        )

    # --- AI / ML workload ---
    # Use whole-word matching for short abbreviations (ai, ml) to avoid false
    # positives from words like "availability", "reliability", "trail", etc.
    _ai_ml_keywords_substr = (
        "gpu", "machine learning", "deep learning", "neural network",
        "training", "inference", "sagemaker", "vertex ai", "bedrock",
        "tensorflow", "pytorch", "hugging face",
    )
    _ai_ml_keywords_word = ("ai", "ml", "llm", "nlp", "cv")
    has_ai_ml_signal = any(kw in text_lower for kw in _ai_ml_keywords_substr) or any(
        re.search(rf"\b{kw}\b", text_lower) for kw in _ai_ml_keywords_word
    )
    if has_ai_ml_signal:
        workloads.append(
            WorkloadRequirement(
                name="AI/ML Workload",
                description="Machine learning training or inference",
                suggested_category=ServiceCategory.AI_ML,
                scaling_pattern=ScalingPattern.BATCH,
                resources=ResourceSpec(
                    vcpus=8,
                    memory_gb=32,
                    gpu_count=1,
                    gpu_type="nvidia-t4",
                ),
            )
        )

    # --- CDN workload (Fix 7c) ---
    has_cdn = any(
        kw in text_lower
        for kw in ("cdn", "content delivery", "cloudfront", "akamai", "fastly")
    )
    # Also infer CDN for public-facing platforms with geospatial / map / tile content
    has_public_heavy = any(
        kw in text_lower
        for kw in (
            "public-facing", "real-time public", "geospatial", "gis",
            "map tiles", "tile", "wildfire", "emergency alert",
        )
    )
    if has_cdn or has_public_heavy:
        # Only add CDN if it is not already in the workloads list
        cdn_already_present = any(
            "cdn" in (w.name + w.description).lower() or
            w.notes == "cdn"
            for w in workloads
        )
        if not cdn_already_present:
            workloads.append(
                WorkloadRequirement(
                    name="CDN / Edge Delivery",
                    description=(
                        "Global content delivery network for static assets, "
                        "map tiles, and edge caching"
                    ),
                    suggested_category=ServiceCategory.NETWORKING,
                    scaling_pattern=ScalingPattern.BURSTY,
                    resources=ResourceSpec(),
                    notes="cdn",
                    spot_eligible=False,
                )
            )

    # --- Geospatial / tile storage (Fix 7c) ---
    has_geospatial = any(
        kw in text_lower
        for kw in (
            "gis", "geospatial", "map tiles", "spatial data",
            "location data", "tile storage",
        )
    )
    if has_geospatial:
        geo_already_present = any("geospatial" in w.description.lower() for w in workloads)
        if not geo_already_present:
            workloads.append(
                WorkloadRequirement(
                    name="Geospatial / Tile Storage",
                    description="Object storage for GIS data, map tiles, and spatial assets",
                    suggested_category=ServiceCategory.STORAGE,
                    scaling_pattern=ScalingPattern.GROWING,
                    resources=ResourceSpec(
                        storage_gb=5000,
                        storage_type="object",
                    ),
                )
            )

    # --- Fallback: no workloads detected ---
    if not workloads:
        workloads.append(
            WorkloadRequirement(
                name="Workload",
                description="To be clarified — no specific components detected",
                suggested_category=ServiceCategory.COMPUTE,
                scaling_pattern=ScalingPattern.STEADY,
                resources=ResourceSpec(vcpus=2, memory_gb=4),
            )
        )

    return workloads


def _parse_count(text: str, patterns: list[str]) -> int:
    """Extract a numeric count from text using regex patterns.

    Returns the first matched integer, or 0 if no pattern matches.
    """
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


# ---------------------------------------------------------------------------
# Main clarifier node
# ---------------------------------------------------------------------------


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

        # --- 4a. Fix 7a — Propagate scale + SLA from enriched input lines ---
        _propagate_scale_and_sla(workload_request, raw_input, log)

        # --- 4b. Fix 7b — Propagate compliance frameworks → workload tags ---
        compliance_tags = workload_request.compliance_frameworks or []
        if compliance_tags and compliance_tags != ["waf"]:
            for w in workload_request.workloads:
                if not w.compliance_tags:
                    w.compliance_tags = list(compliance_tags)
            log.info(
                "compliance_tags_propagated",
                tags=compliance_tags,
                workload_count=len(workload_request.workloads),
            )

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

    # Fix 7d — Region detection: parse and propagate to provider_regions map
    _AWS_REGIONS = re.compile(
        r"\b(us-east-[12]|us-west-[12]|eu-west-[123]|eu-central-[12]|"
        r"ap-southeast-[1-4]|ap-northeast-[1-3]|ap-south-[12]|sa-east-1|"
        r"ca-central-1|me-south-1|af-south-1)\b",
        re.IGNORECASE,
    )
    _AZURE_REGIONS = re.compile(
        r"\b(eastus[2]?|westus[23]?|centralus|northcentralus|southcentralus|"
        r"northeurope|westeurope|uksouth|ukwest|francecentral|germanywestcentral)\b",
        re.IGNORECASE,
    )
    _GCP_REGIONS = re.compile(
        r"\b(us-central1|us-east[14]|us-west[1-4]|europe-west[1-9]|"
        r"europe-north1|asia-east[12]|asia-northeast[1-3]|asia-southeast[12])\b",
        re.IGNORECASE,
    )
    aws_match = _AWS_REGIONS.search(raw_input)
    azure_match = _AZURE_REGIONS.search(raw_input)
    gcp_match = _GCP_REGIONS.search(raw_input)

    if aws_match:
        region = aws_match.group(1).lower()
        req.preferred_region = region
        req.provider_regions["aws"] = region
    elif azure_match:
        region = azure_match.group(1).lower()
        req.preferred_region = region
        req.provider_regions["azure"] = region
    elif gcp_match:
        region = gcp_match.group(1).lower()
        req.preferred_region = region
        req.provider_regions["gcp"] = region

    # Compliance detection — also catch stateramp, fedramp, wcag, gdpr etc.
    compliance = _parse_compliance_extended(raw_input)
    if compliance and compliance != ["waf"]:
        req.compliance_frameworks = compliance


def _parse_compliance_extended(text: str) -> list[str]:
    """Extended compliance parser — handles stateramp, fedramp, wcag, gdpr, etc.

    Superset of ``_parse_compliance`` — used by ``_parse_explicit_values``
    so the enriched-input compliance line is fully captured.
    """
    text_lower = text.strip().lower()
    if "none" in text_lower and not any(
        kw in text_lower for kw in ("hipaa", "pci", "sox", "stateramp", "fedramp")
    ):
        return ["waf"]
    frameworks: list[str] = []
    _COMPLIANCE_PATTERNS = [
        ("hipaa", "hipaa"),
        ("pci-dss", "pci"),
        ("sox", "sox"),
        ("waf", "waf"),
        ("fedramp-high", "fedramp high"),
        ("fedramp-moderate", "fedramp moderate"),
        ("fedramp-low", "fedramp low"),
        ("fedramp", "fedramp"),
        ("stateramp-high", "stateramp high"),
        ("stateramp-moderate", "stateramp moderate"),
        ("stateramp-low", "stateramp low"),
        ("stateramp", "stateramp"),
        ("wcag-2.2-aa", "wcag 2.2 aa"),
        ("wcag-2.1-aa", "wcag 2.1 aa"),
        ("wcag", "wcag"),
        ("gdpr", "gdpr"),
        ("iso27001", "iso 27001"),
        ("soc2", "soc 2"),
        ("soc2", "soc2"),
        ("nist-800-53", "nist 800-53"),
        ("fisma", "fisma"),
        ("cmmc", "cmmc"),
    ]
    for tag, keyword in _COMPLIANCE_PATTERNS:
        if keyword in text_lower and tag not in frameworks:
            frameworks.append(tag)
    return frameworks or ["waf"]


def _parse_peak_concurrent_users(text: str) -> int | None:
    """Extract the peak concurrent user count from a scale description.

    Handles patterns like:
    - "50,000 normal, 2M peak"
    - "peaks to 2 million during emergencies"
    - "2M concurrent users at peak"
    - "50k concurrent"

    Returns:
        Peak user count as int, or None if not parseable.
    """
    text_lower = text.lower()
    # Match peak-specific patterns first
    peak_patterns = [
        r"peak(?:s?\s+to)?\s+([\d,]+)\s*(?:m\b|million)",
        r"peaks?\s+to\s+([\d,.]+)\s*[km]?\b",
        r"([\d,.]+)\s*m(?:illion)?\s+(?:during|at\s+)?peak",
        r"([\d,.]+)\s*m\b.*peak",
        r"peak[^.]*?([\d,.]+)\s*[km]?\b",
        # fallback: any "Xk" or "XM" number in scale description
        r"([\d,.]+)\s*m(?:illion)?\s+concurrent",
        r"([\d,.]+)\s*k\s+concurrent",
        r"concurrent.*?([\d,.]+)\s*m",
    ]
    for pattern in peak_patterns:
        match = re.search(pattern, text_lower)
        if match:
            val_str = match.group(1).replace(",", "")
            try:
                val = float(val_str)
                # Check if "m" / "million" suffix follows
                full_match = match.group(0)
                if "m" in full_match.split(val_str)[-1][:4]:
                    return int(val * 1_000_000)
                if "k" in full_match.split(val_str)[-1][:4]:
                    return int(val * 1_000)
                return int(val)
            except (ValueError, IndexError):
                pass
    # Fallback: look for largest number in scale string
    numbers = re.findall(r"([\d,]+(?:\.\d+)?)\s*([mk]?)\b", text_lower)
    if numbers:
        candidates = []
        for num_str, suffix in numbers:
            try:
                n = float(num_str.replace(",", ""))
                if suffix == "m":
                    n *= 1_000_000
                elif suffix == "k":
                    n *= 1_000
                candidates.append(int(n))
            except ValueError:
                pass
        return max(candidates) if candidates else None
    return None


def _parse_availability_sla(text: str) -> float | None:
    """Extract uptime SLA percentage from text like '99.99%' or '99.99% SLA'.

    Returns:
        SLA as a float percentage (e.g. 99.99), or None.
    """
    match = re.search(r"(9\d(?:\.\d+)?)\s*%", text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


def _parse_rpo_rto(text: str) -> tuple[int | None, int | None]:
    """Extract RPO and RTO values (in minutes) from DR/availability text.

    Handles patterns like "RPO < 15min", "RTO < 1hr", "RTO 30 minutes".

    Returns:
        Tuple of (rpo_minutes, rto_minutes), either can be None.
    """
    rpo_minutes: int | None = None
    rto_minutes: int | None = None

    rpo_match = re.search(
        r"rpo\s*[<:=]?\s*([\d.]+)\s*(min|hr|hour|h\b)", text, re.IGNORECASE
    )
    if rpo_match:
        val, unit = float(rpo_match.group(1)), rpo_match.group(2).lower()
        rpo_minutes = int(val * 60) if unit.startswith("h") else int(val)

    rto_match = re.search(
        r"rto\s*[<:=]?\s*([\d.]+)\s*(min|hr|hour|h\b)", text, re.IGNORECASE
    )
    if rto_match:
        val, unit = float(rto_match.group(1)), rto_match.group(2).lower()
        rto_minutes = int(val * 60) if unit.startswith("h") else int(val)

    return rpo_minutes, rto_minutes


def _propagate_scale_and_sla(
    req: WorkloadRequest, raw_input: str, log: Any
) -> None:
    """Apply scale/SLA/DR values from the enriched input to all workloads.

    Reads ``Scale:``, ``Availability:``, ``DR requirements:`` lines from the
    enriched text and writes the parsed values into every
    ``WorkloadRequirement`` that doesn't already have them set.

    Args:
        req: WorkloadRequest to mutate in-place.
        raw_input: Enriched input text (may include structured key lines).
        log: Bound structlog logger.
    """
    if not req.workloads:
        return

    # --- Extract scale line ---
    scale_text: str | None = None
    for line in raw_input.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("scale:"):
            scale_text = stripped.split(":", 1)[1].strip()
            break

    # --- Extract availability line ---
    avail_text: str | None = None
    for line in raw_input.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("availability:"):
            avail_text = stripped.split(":", 1)[1].strip()
            break

    # --- Extract DR line ---
    dr_text: str | None = None
    for line in raw_input.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("dr requirements:") or stripped.lower().startswith("dr:"):
            dr_text = stripped.split(":", 1)[1].strip()
            break

    # Parse values
    peak_users: int | None = None
    if scale_text:
        peak_users = _parse_peak_concurrent_users(scale_text)
        if peak_users:
            log.info("scale_parsed", peak_users=peak_users, scale_text=scale_text)

    uptime_sla: float | None = None
    if avail_text:
        uptime_sla = _parse_availability_sla(avail_text)
        if uptime_sla:
            log.info("sla_parsed", uptime_sla=uptime_sla)

    rpo_min: int | None = None
    rto_min: int | None = None
    if dr_text:
        rpo_min, rto_min = _parse_rpo_rto(dr_text)
        log.info("dr_parsed", rpo_minutes=rpo_min, rto_minutes=rto_min)

    # Detect burst pattern: scale string has BOTH normal and peak numbers
    is_burst = False
    if scale_text:
        numbers_in_scale = re.findall(r"[\d,]+(?:\.\d+)?\s*[mk]?\b", scale_text.lower())
        if len(numbers_in_scale) >= 2:
            is_burst = True  # multiple scale numbers → bursty pattern

    # Apply to all workloads (don't overwrite values already set by heuristics)
    for workload in req.workloads:
        if peak_users and workload.concurrent_users is None:
            workload.concurrent_users = peak_users
            # Rough RPS estimate: assume ~1 request per 2 seconds per concurrent user.
            # This is realistic for web/API platforms (0.5 RPS/user).
            # The previous multiplier of ×10 (10 RPS/user) was 20× too aggressive,
            # producing implausible values like "20,000,000 RPS" for 2M-user systems.
            if workload.throughput_rps is None:
                # 0.5 RPS per concurrent user, floored at 500, capped at 500,000
                workload.throughput_rps = max(500, min(peak_users // 2, 500_000))

        if uptime_sla and workload.uptime_sla is None:
            workload.uptime_sla = uptime_sla

        if is_burst and workload.scaling_pattern == ScalingPattern.STEADY:
            workload.scaling_pattern = ScalingPattern.BURSTY

        if rpo_min is not None and workload.rpo_minutes is None:
            workload.rpo_minutes = rpo_min
        if rto_min is not None and workload.rto_minutes is None:
            workload.rto_minutes = rto_min

    log.info(
        "scale_sla_propagated",
        workload_count=len(req.workloads),
        peak_users=peak_users,
        uptime_sla=uptime_sla,
        is_burst=is_burst,
    )


async def _llm_enrich_requirements(
    llm: BaseChatModel,
    raw_input: str,
    req: WorkloadRequest,
    log: Any,
) -> str:
    """Use LLM to analyse the user's input and enrich the WorkloadRequest.

    The LLM provides:
    - Missing workloads that heuristics didn't catch
    - Resource spec adjustments (vCPUs, memory) based on described scale
    - Architecture insights (microservice topology, data flow)
    - Implicit requirements (HA, DR, security, networking)

    Returns a summary string for the user.
    """
    workload_summary = "\n".join(
        f"  - {w.name} ({w.suggested_category.value}): {w.description}"
        + (f" [db_engine={w.resources.database_engine}]" if w.resources.database_engine else "")
        + (f" [vcpus={w.resources.vcpus}, mem={w.resources.memory_gb}GB]" if w.resources.vcpus else "")
        + (f" [replicas={w.resources.replicas}]" if w.resources.replicas > 1 else "")
        for w in req.workloads
    )

    prompt = f"""You are a cloud solutions architect analysing an infrastructure request.

USER REQUEST:
{raw_input}

WORKLOADS EXTRACTED BY HEURISTIC PARSER:
{workload_summary}

CURRENT SETTINGS:
- Environment: {req.environment.value if req.environment else 'not specified'}
- Tier: {req.tier.value if req.tier else 'not specified'}
- Budget: {'$' + str(req.budget_monthly_usd) + '/mo' if req.budget_monthly_usd else 'not specified'}
- Providers: {', '.join(p.value for p in req.target_providers) if req.target_providers else 'all'}
- Region: {req.preferred_region or 'not specified'}

TASK: Provide a structured technical assessment in this exact format:

ARCHITECTURE: <1-2 sentences describing the inferred architecture pattern>

MISSING_COMPONENTS: <comma-separated list of infrastructure components NOT in the heuristic extraction but likely needed, e.g. "NAT gateway, monitoring, CI/CD, DNS". Write "none" if all covered.>

RESOURCE_ADJUSTMENTS: <for each workload where the heuristic defaults seem wrong based on context, write "workload_name: field=value, field=value". Write "none" if defaults are reasonable.>

SCALING_NOTES: <1-2 sentences about scaling considerations based on the described use case>

COST_ESTIMATE_RANGE: <rough monthly range based on described scope, e.g. "$800-2,000/mo">

CONCERNS: <any architectural concerns, anti-patterns, or risks. 1-2 sentences.>

Be specific and practical. Do NOT invent requirements the user didn't mention or imply."""

    try:
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        llm_text = response.content if hasattr(response, "content") else str(response)
        log.info("llm_enrichment_complete", summary_length=len(llm_text))

        # Parse and apply resource adjustments if present
        _apply_llm_adjustments(llm_text, req, log)

        # Format a user-friendly summary from the LLM output
        summary_lines = ["**LLM Analysis:**"]
        for section in ("ARCHITECTURE:", "SCALING_NOTES:", "COST_ESTIMATE_RANGE:", "CONCERNS:"):
            value = _extract_llm_section(llm_text, section)
            if value and value.lower() != "none":
                label = section.replace("_", " ").replace(":", "").title()
                summary_lines.append(f"*{label}*: {value}")

        missing = _extract_llm_section(llm_text, "MISSING_COMPONENTS:")
        if missing and missing.lower() != "none":
            summary_lines.append(f"*Note*: Additional components may be needed: {missing}")

        return "\n".join(summary_lines) if len(summary_lines) > 1 else ""
    except Exception:
        log.warning("llm_enrichment_failed", exc_info=True)
        return ""


def _extract_llm_section(text: str, section_name: str) -> str:
    """Extract the value after a section label from LLM output."""
    for line in text.split("\n"):
        if section_name in line:
            return line.split(section_name, 1)[1].strip()
    return ""


def _apply_llm_adjustments(llm_text: str, req: WorkloadRequest, log: Any) -> None:
    """Parse RESOURCE_ADJUSTMENTS from LLM output and apply to workloads.

    Only applies adjustments for workloads that already exist (won't create new ones).
    Only modifies numeric resource fields to avoid hallucinated categories.
    """
    adj_text = _extract_llm_section(llm_text, "RESOURCE_ADJUSTMENTS:")
    if not adj_text or adj_text.lower() == "none":
        return

    workload_map = {w.name.lower(): w for w in req.workloads}
    # Parse "workload_name: field=value, field=value"
    for part in adj_text.split(";"):
        part = part.strip()
        if ":" not in part:
            continue
        wk_name, fields = part.split(":", 1)
        wk_name = wk_name.strip().lower()

        # Find the matching workload (fuzzy: check if name is substring)
        target = workload_map.get(wk_name)
        if not target:
            for name, wk in workload_map.items():
                if wk_name in name or name in wk_name:
                    target = wk
                    break
        if not target:
            continue

        # Apply safe numeric field adjustments
        _SAFE_FIELDS = {
            "vcpus": ("vcpus", int),
            "memory_gb": ("memory_gb", float),
            "storage_gb": ("storage_gb", float),
            "replicas": ("replicas", int),
            "cpu_request_millicores": ("cpu_request_millicores", int),
            "memory_request_mb": ("memory_request_mb", int),
            "iops": ("iops", int),
        }
        for field_part in fields.split(","):
            field_part = field_part.strip()
            if "=" not in field_part:
                continue
            key, val = field_part.split("=", 1)
            key = key.strip().lower()
            val = val.strip()
            if key in _SAFE_FIELDS:
                attr_name, cast_fn = _SAFE_FIELDS[key]
                try:
                    setattr(target.resources, attr_name, cast_fn(val))
                    log.debug("llm_adjustment_applied", workload=target.name, field=key, value=val)
                except (ValueError, TypeError):
                    pass


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


# ---------------------------------------------------------------------------
# Multi-turn LLM conversation manager (used by /orchestrate/clarify)
# ---------------------------------------------------------------------------

_CLARIFY_SYSTEM_PROMPT = """\
You are a Pre-Sales Cloud Solutions Architect conducting a brief discovery session with \
a client. Your goal is to understand their business requirements well enough to produce a \
complete AWS Well-Architected cloud infrastructure proposal.

## Your Two Roles

**Role 1 — INTERVIEWER** (during the conversation)
Ask the minimal set of business-context questions that only the CLIENT can answer. \
Stop when you have enough to make confident architectural decisions.

**Role 2 — ARCHITECT** (when writing the STATUS: ready block)
Use your expertise to INFER and DECIDE the right architecture for each WAF pillar. \
The WAF pillar fields are YOUR architectural recommendations — not transcribed client \
answers.

---

## What to Ask (client-answerable business questions only)

Gather answers to these topics (adapt order to the use case):

1. **Platform type + users** — what is being built and who uses it?
2. **Scale** — how many concurrent users at normal load and at peak?
3. **Compliance** — any regulatory requirements? (healthcare, government, finance, none?)
4. **Cloud provider** — preferred provider(s) and any data residency constraints?
   - Accepted answers: **AWS only** / **Azure only** / **GCP only** / a specific combination
     (e.g. AWS + Azure) / or **"no preference — compare all and recommend the best price"**.
   - "Best price" mode is valid and common: the system will size on all three providers,
     run a cost comparison, and recommend the cheapest architecture.
5. **Budget** — monthly infrastructure envelope (or "no constraint")?
6. **Availability + DR** — required uptime SLA and disaster recovery target (RPO/RTO)?

## What NOT to Ask

Never quiz the client on implementation details they should not need to decide:
- CI/CD pipelines, GitOps, or IaC tools (you decide based on platform type)
- Monitoring stack / APM tools (you recommend based on provider)
- Spot vs Reserved Instances commitment (you decide based on workload pattern + budget)
- CMK vs AWS-managed keys (you decide based on compliance)
- Network CIDR blocks or VPC layout details
- Caching strategy, CDN configuration, or database replication count
- Backup retention periods or automated failover mechanisms
- Sustainability / carbon footprint (only ask if the client raises it)

## Adaptive Question Strategy

- **Public consumer app**: ask scale first (it drives everything), then compliance, then budget.
- **Government / regulated system**: ask compliance first (it drives architecture), then scale, then DR.
- **Internal tool**: ask scale + budget first, then availability SLA.
- **Data pipeline**: ask throughput + batch schedule first, then budget.
- Acknowledge what the client said before asking the next question.
- Ask **at most 2 questions per turn**. Never repeat a question already answered.
- You may conclude after **4–8 exchanges** when you have enough to make confident decisions.

## Completion Criteria

Conclude (switch to STATUS: ready) when you have:
- Platform type + user types ✓
- Scale / concurrent users ✓
- Cloud provider preference ✓
- Compliance / regulatory context ✓ (or explicit "none")
- Availability SLA ✓ (or explicit "no specific target")
- Budget ✓ (or "no constraint")

DR (RTO/RPO) can be inferred from availability SLA + tier if not explicitly stated.

---

## Response Format

You MUST respond in EXACTLY this format — no text before STATUS:

STATUS: clarifying

RESPONSE: <your conversational message — acknowledge what was heard, ask 1-2 focused questions>

---OR when the completion criteria are met:

STATUS: ready

RESPONSE: <brief wrap-up, e.g. "Perfect — I have everything I need. Let me put together the architecture now.">

PLATFORM_TYPE: <e.g. public-facing mobile and web app for wildfire emergency management>

USERS: <e.g. general public via mobile/web; internal CAL FIRE staff via admin portal>

SCALE: <e.g. 50,000 concurrent users at steady state; peaks to 2M during active emergencies>

PROVIDERS: <comma-separated providers to evaluate — use all three if client has no \
preference or explicitly wants cost comparison: aws, azure, gcp>

PROVIDER_STRATEGY: <one of: best_price_all | best_price_aws_azure | best_price_aws_gcp | \
best_price_azure_gcp | single_aws | single_azure | single_gcp. \
Use best_price_all when client said "no preference" or "compare all". \
Use single_* when client has a hard provider requirement.>

COMPLIANCE: <e.g. stateramp-moderate, wcag-2.2-aa — or none>

AVAILABILITY: <e.g. 99.99% public app, 99.95% internal portal>

DR: <e.g. cross-region active-passive, us-west-2 primary us-east-1 secondary, RTO < 1hr RPO < 15min — \
or inferred from SLA if client did not specify>

BUDGET_MONTHLY_USD: <Monthly budget as a number only. IMPORTANT: convert to monthly if stated \
as annual (e.g. "$3.2 million/year" → 266667, "$3.2M/yr" → 266667, "$1.2M annually" → 100000, \
"$500k/year" → 41667). Output only the integer, no symbols, no units. \
Example: 266667 or null if no budget was stated.>

ENVIRONMENTS: <e.g. production, staging, dr>

INTEGRATIONS: <e.g. CAL FIRE dispatch, GIS feeds, CalID SSO — or none>

WORKLOAD_SUMMARY: <List EVERY infrastructure component the workload requires, one per line \
in the format "- <component>: <brief role>". Be exhaustive — do NOT stop at 3 components. \
Use PROVIDER-NEUTRAL technology names only. \
Correct: "- kubernetes cluster: container orchestration", "- postgresql database: primary OLTP store", \
"- redis cache: session and query cache", "- CDN: edge delivery for static assets and map tiles", \
"- object storage: geospatial data, logs, backups", "- api gateway: request routing and rate limiting", \
"- load balancer: L7 traffic distribution", "- streaming pipeline: real-time event ingestion". \
Wrong: "EKS", "RDS", "CloudFront", "S3", "Azure SQL", "GKE". \
Provider-specific service selection happens AFTER cost comparison by the Sizer agent. \
MANDATORY components for a containerised public web app: kubernetes cluster, load balancer, \
CDN, API gateway, relational database (postgresql or mysql), redis cache, object storage. \
Add any additional components inferred from the use case (e.g. geospatial data store for \
mapping, streaming pipeline for real-time events, message queue for async tasks). \
After listing components, add 1-2 sentences on HA topology (multi-AZ yes/no) and DR region \
strategy (e.g. US West primary, US East DR). \
This text feeds automated workload extraction — every component you list here becomes a \
separately sized and priced line item.>

ARCHITECTURE_PATTERN: <1-3 sentences naming the overall design pattern, completely \
provider-neutral. This is the "what" of the architecture — not the "where" (provider). \
E.g. "3-tier containerised web platform: managed Kubernetes for microservices, managed \
relational database with HA read replicas, Redis caching layer, global CDN, cross-region \
active-passive DR." Another example: "Serverless event-driven analytics pipeline: managed \
Kafka stream ingestion, distributed batch processing, columnar data warehouse, \
BI dashboard layer with sub-second query response.">

WAF_OPERATIONAL_EXCELLENCE: <YOUR architectural recommendation (not a user answer). \
State the recommended deployment strategy (GitOps with ArgoCD, AWS CodePipeline + CDK, \
Terraform IaC, etc.), monitoring stack (CloudWatch + X-Ray + Datadog/Prometheus/Grafana), \
alerting model (PagerDuty on-call rotation vs MSP), incident response runbook approach, \
and change management process — all inferred from the platform type, team signals, and \
provider choice.>

WAF_SECURITY: <YOUR architectural recommendation. Specify: authentication mechanism \
(Amazon Cognito + SAML federation, Azure AD SSO, IAP, etc.), network isolation design \
(public subnets for ALB/CDN, private subnets for app + DB tiers, PrivateLink/VPC endpoints), \
encryption strategy (AES-256 at rest with AWS KMS, TLS 1.3 in transit), key management \
(AWS-managed vs CMK — based on compliance), WAF + DDoS protection layer (AWS WAF + Shield \
Standard/Advanced), security monitoring (GuardDuty, Security Hub, CloudTrail). \
All inferred from compliance frameworks + data classification signals.>

WAF_RELIABILITY: <YOUR architectural recommendation. Specify: multi-AZ deployment (yes/no), \
multi-region topology (active-active/active-passive/single-region — inferred from SLA), \
inferred RTO and RPO targets (if not stated), backup strategy (automated snapshots, \
cross-region replication, retention period — inferred from tier), health checks + \
circuit breakers, auto-scaling approach, criticality tier (mission_critical / \
business_critical / non_critical — inferred from SLA + sector).>

WAF_PERFORMANCE_EFFICIENCY: <YOUR architectural recommendation. State: auto-scaling \
strategy (HPA + Cluster Autoscaler for K8s, ASG for EC2), caching architecture \
(Redis ElastiCache for session + query cache, CloudFront for edge), CDN requirement \
(yes/no and why), database read optimization (read replicas count — inferred from \
read-heavy pattern), traffic pattern classification (steady/bursty/batch — inferred \
from scale + use case), estimated RPS at peak (derived from concurrent users ÷ avg \
session length), p99 latency target (inferred from UI-facing vs API vs batch).>

WAF_COST_OPTIMIZATION: <YOUR architectural recommendation. State: RI/Savings Plan \
strategy (1yr or 3yr — inferred from budget + workload pattern), spot eligibility \
(which workload types can use spot — batch/non-critical only), right-sizing approach \
(start with heuristic SKU selection then refine with CloudWatch metrics after 30 days), \
cost allocation tagging strategy (by environment, service, team), multi-cloud cost \
comparison scope (all three providers vs single-provider focus — from client preference).>

WAF_SUSTAINABILITY: <YOUR architectural recommendation. If government/regulated sector \
or client raised it: recommend low-carbon AWS regions (us-west-2 Oregon, eu-west-1 Ireland, \
eu-north-1 Stockholm) and Graviton3 instance family for better power efficiency. \
Otherwise: "No specific sustainability constraints identified; default region selection \
driven by latency and compliance requirements.">
"""


async def llm_clarify_turn(
    llm: BaseChatModel,
    history: list[tuple[str, str]],
    user_input: str,
    log: Any,
) -> dict[str, Any]:
    """Run one turn of the LLM-powered clarification conversation.

    On each call the full conversation history is sent to the LLM so it can
    decide what is still missing and what to ask next.  When enough context
    has been gathered the LLM switches to ``STATUS: ready`` and emits a
    structured summary that the route converts into an ``enriched_input``
    string for the pipeline.

    Args:
        llm: Model-agnostic ``BaseChatModel`` instance.
        history: List of ``(role, content)`` tuples — role is "user" or "architect".
        user_input: The user's latest message.
        log: structlog bound logger.

    Returns:
        dict with keys:
            ``status``: ``"clarifying"`` | ``"ready"``
            ``response``: str — message to send back to the user
            ``structured``: dict — only present when ``status == "ready"``
    """
    # Build conversation context block
    history_text = ""
    for role, content in history:
        prefix = "CLIENT" if role == "user" else "ARCHITECT"
        history_text += f"{prefix}: {content}\n\n"

    full_prompt = (
        f"{_CLARIFY_SYSTEM_PROMPT}\n\n"
        f"CONVERSATION SO FAR:\n"
        f"{history_text if history_text else '(this is the first message)'}\n\n"
        f"CLIENT'S LATEST MESSAGE:\n{user_input}"
    )

    try:
        response = await llm.ainvoke([HumanMessage(content=full_prompt)])
        llm_text = response.content if hasattr(response, "content") else str(response)
        log.debug("llm_clarify_raw_response", length=len(llm_text))

        # Parse STATUS
        status = "clarifying"
        for line in llm_text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("STATUS:"):
                if "ready" in stripped.split(":", 1)[1].lower():
                    status = "ready"
                break

        # Parse RESPONSE section
        response_text = _extract_clarify_section(llm_text, "RESPONSE:")
        if not response_text:
            # Fallback: use the full LLM output
            response_text = llm_text.strip()

        result: dict[str, Any] = {"status": status, "response": response_text}

        if status == "ready":
            result["structured"] = {
                "platform_type": _extract_clarify_section(llm_text, "PLATFORM_TYPE:"),
                "users": _extract_clarify_section(llm_text, "USERS:"),
                "scale": _extract_clarify_section(llm_text, "SCALE:"),
                "providers": _parse_providers_from_clarify(
                    _extract_clarify_section(llm_text, "PROVIDERS:")
                ),
                "provider_strategy": _extract_clarify_section(
                    llm_text, "PROVIDER_STRATEGY:"
                ),
                "compliance": _parse_compliance_from_clarify(
                    _extract_clarify_section(llm_text, "COMPLIANCE:")
                ),
                "availability": _extract_clarify_section(llm_text, "AVAILABILITY:"),
                "dr": _extract_clarify_section(llm_text, "DR:"),
                "budget_monthly_usd": _parse_budget_from_clarify(
                    _extract_clarify_section(llm_text, "BUDGET_MONTHLY_USD:")
                ),
                "environments": _extract_clarify_section(llm_text, "ENVIRONMENTS:"),
                "integrations": _extract_clarify_section(llm_text, "INTEGRATIONS:"),
                "workload_summary": _extract_clarify_section(llm_text, "WORKLOAD_SUMMARY:"),
                "architecture_pattern": _extract_clarify_section(
                    llm_text, "ARCHITECTURE_PATTERN:"
                ),
                # AWS Well-Architected Framework pillar summaries
                "waf_operational_excellence": _extract_clarify_section(
                    llm_text, "WAF_OPERATIONAL_EXCELLENCE:"
                ),
                "waf_security": _extract_clarify_section(llm_text, "WAF_SECURITY:"),
                "waf_reliability": _extract_clarify_section(llm_text, "WAF_RELIABILITY:"),
                "waf_performance_efficiency": _extract_clarify_section(
                    llm_text, "WAF_PERFORMANCE_EFFICIENCY:"
                ),
                "waf_cost_optimization": _extract_clarify_section(
                    llm_text, "WAF_COST_OPTIMIZATION:"
                ),
                "waf_sustainability": _extract_clarify_section(
                    llm_text, "WAF_SUSTAINABILITY:"
                ),
            }

        log.info(
            "llm_clarify_turn_complete",
            status=status,
            response_len=len(response_text),
            turn=len(history) + 1,
        )

        # PROV-1 fix: deterministic provider override.
        # If the user never explicitly named a single provider, always use best_price_all.
        # This prevents the LLM from inferring "single_aws" when it mentions AWS services
        # in its own architectural notes, while the user actually said "no preference".
        if status == "ready":
            all_user_text = (
                " ".join(content for role, content in history if role == "user")
                + " "
                + user_input
            )
            if not _user_named_single_provider(all_user_text):
                result["structured"]["provider_strategy"] = "best_price_all"
                result["structured"]["providers"] = ["aws", "azure", "gcp"]
                log.info(
                    "provider_override_applied",
                    reason="no_explicit_single_provider_in_conversation",
                    strategy="best_price_all",
                )

        return result

    except Exception:
        log.error("llm_clarify_turn_failed", exc_info=True)
        # Graceful degradation — ask a generic but useful question
        return {
            "status": "clarifying",
            "response": (
                "I need a bit more detail to size your infrastructure accurately.\n\n"
                "Could you tell me:\n"
                "1. How many concurrent users do you expect at peak?\n"
                "2. Do you have a preferred cloud provider — AWS, Azure, or GCP — or no preference "
                "(we can compare all three and recommend the best price)?"
            ),
        }


def _user_named_single_provider(all_user_text: str) -> bool:
    """Return True only if the user explicitly requested a single cloud provider.

    Patterns that indicate explicit single-provider preference:
    - "use AWS", "AWS only", "we use AWS", "prefer AWS", "on AWS"
    - "Azure only", "Microsoft Azure", "prefer Azure"
    - "GCP only", "Google Cloud", "prefer GCP", "we use GCP"

    Patterns that do NOT indicate provider preference (return False):
    - "no preference", "whatever is best", "best price", "compare all"
    - Mentioning AWS service names in passing (e.g. "like Route 53", "similar to S3")
    """
    text = all_user_text.lower()

    # Explicit "no preference" / "compare all" signals → NOT single provider
    no_pref_patterns = [
        "no preference",
        "no provider preference",
        "no cloud preference",
        "whatever is best",
        "best price",
        "compare all",
        "all three",
        "no specific preference",
        "doesn't matter",
        "don't have a preference",
    ]
    if any(p in text for p in no_pref_patterns):
        return False

    # Explicit single-provider patterns
    single_patterns = [
        r"\buse aws\b",
        r"\baws only\b",
        r"\bamazon only\b",
        r"\bwe(?:'re| are) on aws\b",
        r"\bwe use aws\b",
        r"\bprefer aws\b",
        r"\bmust be aws\b",
        r"\baws[\s-]first\b",
        r"\bonly aws\b",
        r"\buse azure\b",
        r"\bazure only\b",
        r"\bprefer azure\b",
        r"\bmust be azure\b",
        r"\bonly azure\b",
        r"\bwe use azure\b",
        r"\buse gcp\b",
        r"\bgcp only\b",
        r"\bprefer gcp\b",
        r"\bgoogle cloud only\b",
        r"\bmust be gcp\b",
        r"\bonly gcp\b",
        r"\bwe use gcp\b",
    ]
    return any(re.search(pat, text) for pat in single_patterns)


def _extract_clarify_section(text: str, section: str) -> str:
    """Extract content after a section label from the LLM clarify response.

    Handles multi-line values by stopping at the next section header or
    the ``---`` separator.
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    collecting = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(section):
            collecting = True
            value = stripped[len(section):].strip()
            if value:
                result_lines.append(value)
        elif collecting:
            # Stop at next section header (ALL_CAPS_WORD: pattern) or separator
            if stripped == "---" or re.match(r"^[A-Z][A-Z_]+:", stripped):
                break
            result_lines.append(line)

    return "\n".join(result_lines).strip()


def _parse_providers_from_clarify(text: str) -> list[str]:
    """Parse provider list from an LLM clarify PROVIDERS: field."""
    text_lower = text.lower()
    providers = []
    if "aws" in text_lower or "amazon" in text_lower:
        providers.append("aws")
    if "azure" in text_lower or "microsoft" in text_lower:
        providers.append("azure")
    if "gcp" in text_lower or "google" in text_lower:
        providers.append("gcp")
    return providers or ["aws", "azure", "gcp"]


def _parse_compliance_from_clarify(text: str) -> list[str]:
    """Parse compliance frameworks from an LLM clarify COMPLIANCE: field."""
    text_lower = text.lower()
    if not text_lower.strip() or "none" in text_lower:
        return []
    _MAPPINGS = {
        "hipaa": "hipaa",
        "pci": "pci-dss",
        "sox": "sox",
        "fedramp": "fedramp",
        "stateramp": "stateramp",
        "gdpr": "gdpr",
        "wcag": "wcag-2.2-aa",
        "iso 27001": "iso-27001",
        "iso-27001": "iso-27001",
        "soc2": "soc2",
        "soc 2": "soc2",
        "waf": "waf",
    }
    frameworks = []
    for keyword, framework in _MAPPINGS.items():
        if keyword in text_lower and framework not in frameworks:
            frameworks.append(framework)
    return frameworks


def _parse_budget_from_clarify(text: str) -> float | None:
    """Parse a monthly budget number from an LLM clarify BUDGET_MONTHLY_USD: field."""
    text = text.strip().lower()
    if not text or text in ("null", "none", "n/a", "not specified", "no constraint"):
        return None
    # Strip currency symbols and commas then find first number
    cleaned = re.sub(r"[$,]", "", text)
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if match:
        return float(match.group(0))
    return None


def build_enriched_input_from_structured(
    raw_input: str,
    structured: dict[str, Any],
) -> str:
    """Convert structured LLM clarify output into a rich text block.

    The resulting string is passed to ``/orchestrate`` or ``/orchestrate/stream``
    as ``user_input``.  The pipeline's Clarifier node will extract workloads
    from the ``WORKLOAD_SUMMARY`` and parse explicit key-value fields.

    Args:
        raw_input: The user's very first message (preserved for context).
        structured: Dict returned by ``llm_clarify_turn`` when ``status == "ready"``.

    Returns:
        Enriched input string ready for the pipeline.
    """
    parts: list[str] = []

    # Workload summary from LLM is the richest signal for the pipeline clarifier
    workload_summary = structured.get("workload_summary", "").strip()
    if workload_summary:
        parts.append(workload_summary)
    else:
        parts.append(raw_input)

    # Architecture pattern — provider-neutral design summary (appears before details)
    architecture_pattern = structured.get("architecture_pattern", "").strip()
    if architecture_pattern:
        parts.append(f"Architecture pattern: {architecture_pattern}")

    # Provider strategy — drives which providers the Sizer evaluates.
    # "best_price_*" → all target providers are included (comparison mode).
    # "single_*" → only that provider is used.
    provider_strategy = structured.get("provider_strategy", "").strip().lower()
    if provider_strategy:
        parts.append(f"Provider strategy: {provider_strategy}")

    # Explicit key-value lines that heuristic parsers in run_clarifier_node can pick up.
    # The providers list is derived from PROVIDER_STRATEGY when possible, with
    # the LLM-populated PROVIDERS field as the fallback.
    if structured.get("providers"):
        provider_str = ", ".join(structured["providers"])
        parts.append(f"Providers: {provider_str}")

    if structured.get("compliance"):
        compliance_str = ", ".join(structured["compliance"])
        parts.append(f"Compliance: {compliance_str}")

    budget = structured.get("budget_monthly_usd")
    if budget is not None:
        parts.append(f"Budget: ${budget:,.0f}/month")

    envs = structured.get("environments", "").strip()
    if envs:
        # Map to first environment type for the parser
        first_env = envs.split(",")[0].strip().lower()
        if "prod" in first_env:
            parts.append("Environment: production")
        elif "stag" in first_env:
            parts.append("Environment: staging")
        elif "dev" in first_env:
            parts.append("Environment: development")
        elif "dr" in first_env or "disaster" in first_env:
            parts.append("Environment: disaster_recovery")

    if structured.get("scale"):
        parts.append(f"Scale: {structured['scale']}")

    if structured.get("availability"):
        parts.append(f"Availability: {structured['availability']}")

    if structured.get("dr"):
        parts.append(f"DR requirements: {structured['dr']}")

    if structured.get("integrations") and structured["integrations"].lower() != "none":
        parts.append(f"Integrations: {structured['integrations']}")

    # AWS Well-Architected Framework pillar summaries — provide full WAF context to pipeline
    _WAF_PILLARS = [
        ("waf_operational_excellence", "WAF Operational Excellence"),
        ("waf_security", "WAF Security"),
        ("waf_reliability", "WAF Reliability"),
        ("waf_performance_efficiency", "WAF Performance Efficiency"),
        ("waf_cost_optimization", "WAF Cost Optimization"),
        ("waf_sustainability", "WAF Sustainability"),
    ]
    waf_parts: list[str] = []
    for key, label in _WAF_PILLARS:
        val = structured.get(key, "").strip()
        if val and val.lower() not in ("not specified", "none", "n/a"):
            waf_parts.append(f"  {label}: {val}")
    if waf_parts:
        parts.append("\nWell-Architected Framework Assessment:")
        parts.extend(waf_parts)

    # Preserve the original request at the end for full context
    if workload_summary and raw_input not in workload_summary:
        parts.append(f"\nOriginal request: {raw_input}")

    return "\n".join(parts)