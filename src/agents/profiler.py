"""Profiler Agent — Workload analysis and component profiling.

The Profiler is the second stage of the orchestration pipeline, running
immediately after the Clarifier marks requirements as complete.  It takes
the structured ``WorkloadRequest`` and enriches each ``WorkloadRequirement``
into a ``ComponentProfile`` with concrete compute, storage, and I/O
estimates that the Sizer agent needs for SKU matching.

### Responsibilities

1. **Category resolution** — Confirm or refine the ``suggested_category``
   from the Clarifier into a definitive ``resolved_category``.
2. **Resource estimation** — Estimate vCPUs, memory, storage, and IOPS for
   each component based on the ``ResourceSpec`` and workload context.
3. **Instance-family recommendation** — Suggest appropriate instance
   families per cloud provider (e.g. ``m5`` for AWS, ``Standard_D`` for
   Azure, ``n2-standard`` for GCP) using heuristics and LLM reasoning.
4. **GPU detection** — Flag workloads that require GPU acceleration.
5. **HA considerations** — Note high-availability requirements that
   affect sizing (multi-AZ, read replicas, etc.).

### Flow

```
WorkloadRequest (from Clarifier)
      │
      ▼
┌──────────────────────────────┐
│  For each WorkloadRequirement│
│    ├─ Resolve category       │
│    ├─ Estimate resources     │
│    ├─ Recommend families     │
│    └─ Build ComponentProfile │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│  Aggregate into              │
│  WorkloadProfile             │
│    ├─ Sum totals             │
│    ├─ Copy environment/tier  │
│    └─ Generate profiler_notes│
└──────────────────────────────┘
```

### Usage

```python
from src.agents.profiler import run_profiler_node

# state already has workload_request populated by clarifier
state = await run_profiler_node(state, llm)
# state['workload_profile'] now contains the enriched profile
# state['messages'] has profiler summary appended
```
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langfuse import observe

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.conversation import ChatMessage, MessageRole
from src.models.workload import (
    ComponentProfile,
    EnvironmentType,
    ResourceSpec,
    ScalingPattern,
    WorkloadProfile,
    WorkloadRequest,
    WorkloadRequirement,
    WorkloadTier,
)
from src.orchestrator.state import AgentExecution, AgentStatus, OrchestratorState

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Category resolution heuristics
# ---------------------------------------------------------------------------

#: Keywords and resource-spec fields that help resolve ambiguous categories.
_CATEGORY_SIGNALS: dict[ServiceCategory, dict[str, Any]] = {
    ServiceCategory.COMPUTE: {
        "keywords": ["vm", "virtual machine", "ec2", "compute", "instance", "server"],
        "resource_check": lambda r: r.vcpus is not None and r.vcpus >= 1,
    },
    ServiceCategory.CONTAINER: {
        "keywords": ["kubernetes", "k8s", "container", "docker", "aks", "eks", "gke", "pod"],
        "resource_check": lambda r: r.cpu_request_millicores is not None,
    },
    ServiceCategory.DATABASE: {
        "keywords": [
            "database", "db", "postgres", "mysql", "mongodb", "redis",
            "rds", "sql", "dynamo", "cosmos", "cache", "memcached",
        ],
        "resource_check": lambda r: r.database_engine is not None,
    },
    ServiceCategory.STORAGE: {
        "keywords": ["storage", "s3", "blob", "gcs", "bucket", "disk", "volume", "archive"],
        "resource_check": lambda r: (
            r.storage_gb is not None
            and r.storage_gb > 0
            and r.vcpus is None
            and r.database_engine is None
        ),
    },
    ServiceCategory.SERVERLESS_FUNCTION: {
        "keywords": ["lambda", "function", "serverless", "cloud function", "faas"],
        "resource_check": lambda r: r.invocations_per_month is not None,
    },
    ServiceCategory.SERVERLESS_COMPUTE: {
        "keywords": [
            "app service", "cloud run", "app runner", "elastic beanstalk",
            "fargate", "platform",
        ],
        "resource_check": lambda _r: False,
    },
    ServiceCategory.NETWORKING: {
        "keywords": [
            "load balancer", "lb", "cdn", "vpn", "gateway", "firewall",
            "application gateway", "cloudfront",
        ],
        "resource_check": lambda r: r.network_bandwidth_gbps is not None and r.vcpus is None,
    },
    ServiceCategory.AI_ML: {
        "keywords": [
            "ml", "machine learning", "ai", "sagemaker", "vertex",
            "training", "inference", "gpu",
        ],
        "resource_check": lambda r: r.gpu_count > 0,
    },
    ServiceCategory.ANALYTICS: {
        "keywords": [
            "analytics", "bigquery", "redshift", "synapse", "databricks",
            "data warehouse", "etl",
        ],
        "resource_check": lambda _r: False,
    },
}

#: Default instance families per cloud provider, keyed by ServiceCategory.
_INSTANCE_FAMILY_MAP: dict[ServiceCategory, dict[str, list[str]]] = {
    ServiceCategory.COMPUTE: {
        "aws": ["m5", "m6i", "c5", "r5"],
        "azure": ["Standard_D", "Standard_E", "Standard_F"],
        "gcp": ["n2-standard", "n2-highmem", "c2-standard"],
    },
    ServiceCategory.CONTAINER: {
        "aws": ["m5", "m6i", "c5"],
        "azure": ["Standard_D", "Standard_DS"],
        "gcp": ["n2-standard", "e2-standard"],
    },
    ServiceCategory.DATABASE: {
        "aws": ["db.m5", "db.r5", "db.t3"],
        "azure": ["GP_Gen5", "MO_Gen5", "BC_Gen5"],
        "gcp": ["db-custom", "db-n1-standard"],
    },
    ServiceCategory.AI_ML: {
        "aws": ["p3", "p4d", "g5", "inf1"],
        "azure": ["Standard_NC", "Standard_ND", "Standard_NV"],
        "gcp": ["a2-highgpu", "n1-standard (with GPU)"],
    },
    ServiceCategory.SERVERLESS_FUNCTION: {
        "aws": ["Lambda"],
        "azure": ["Functions"],
        "gcp": ["Cloud Functions"],
    },
    ServiceCategory.SERVERLESS_COMPUTE: {
        "aws": ["App Runner", "Fargate"],
        "azure": ["App Service P1v3", "Container Apps"],
        "gcp": ["Cloud Run"],
    },
    ServiceCategory.STORAGE: {
        "aws": ["S3 Standard", "EBS gp3", "EFS"],
        "azure": ["StorageV2", "Premium SSD", "Azure Files"],
        "gcp": ["Standard", "Nearline", "pd-ssd"],
    },
    ServiceCategory.NETWORKING: {
        "aws": ["ALB", "NLB", "CloudFront"],
        "azure": ["Standard LB", "Application Gateway", "Front Door"],
        "gcp": ["Cloud Load Balancing", "Cloud CDN"],
    },
    ServiceCategory.ANALYTICS: {
        "aws": ["Redshift dc2", "Redshift ra3"],
        "azure": ["Synapse DWU"],
        "gcp": ["BigQuery"],
    },
}


# ---------------------------------------------------------------------------
# Resource estimation defaults
# ---------------------------------------------------------------------------

#: Minimum resource baselines per category when user hasn't specified values.
_RESOURCE_DEFAULTS: dict[ServiceCategory, dict[str, Any]] = {
    ServiceCategory.COMPUTE: {
        "vcpus": 2, "memory_gb": 4.0, "storage_gb": 50.0,
    },
    ServiceCategory.CONTAINER: {
        "vcpus": 2, "memory_gb": 4.0, "storage_gb": 20.0,
        "cpu_request_millicores": 500, "memory_request_mb": 512,
    },
    ServiceCategory.DATABASE: {
        "vcpus": 2, "memory_gb": 8.0, "storage_gb": 100.0, "iops": 3000,
    },
    ServiceCategory.STORAGE: {
        "storage_gb": 100.0,
    },
    ServiceCategory.SERVERLESS_FUNCTION: {
        "memory_mb": 256, "invocations_per_month": 1_000_000,
        "avg_duration_ms": 200,
    },
    ServiceCategory.SERVERLESS_COMPUTE: {
        "vcpus": 1, "memory_gb": 2.0,
    },
    ServiceCategory.NETWORKING: {
        "network_bandwidth_gbps": 1.0,
    },
    ServiceCategory.AI_ML: {
        "vcpus": 8, "memory_gb": 64.0, "storage_gb": 200.0,
        "gpu_count": 1,
    },
    ServiceCategory.ANALYTICS: {
        "vcpus": 4, "memory_gb": 32.0, "storage_gb": 500.0,
    },
}

#: Tier-based resource multipliers for HA / redundancy.
_TIER_MULTIPLIERS: dict[WorkloadTier, dict[str, float]] = {
    WorkloadTier.MISSION_CRITICAL: {
        "compute": 1.5,   # overprovisioned for headroom
        "storage": 2.0,   # geo-redundant
        "iops": 1.5,
    },
    WorkloadTier.BUSINESS_CRITICAL: {
        "compute": 1.2,
        "storage": 1.5,
        "iops": 1.2,
    },
    WorkloadTier.NON_CRITICAL: {
        "compute": 1.0,
        "storage": 1.0,
        "iops": 1.0,
    },
}

#: Environment-based scaling factors.
_ENVIRONMENT_FACTORS: dict[EnvironmentType, float] = {
    EnvironmentType.PRODUCTION: 1.0,
    EnvironmentType.STAGING: 0.5,
    EnvironmentType.DEVELOPMENT: 0.25,
    EnvironmentType.DR: 0.75,
}


# ---------------------------------------------------------------------------
# Internal helper functions
# ---------------------------------------------------------------------------


def _resolve_category(
    workload: WorkloadRequirement,
) -> ServiceCategory:
    """Resolve the definitive ServiceCategory for a workload.

    Uses a combination of the ``suggested_category``, workload name /
    description keywords, and ``ResourceSpec`` field checks.

    Args:
        workload: The workload requirement to classify.

    Returns:
        The resolved ``ServiceCategory``.
    """
    log = logger.bind(step="resolve_category", workload=workload.name)

    # Priority order: most specific categories first, generic COMPUTE last.
    # This prevents vcpus (common across many categories) from shadowing
    # more specific signals like database_engine or gpu_count.
    _CATEGORY_PRIORITY: list[ServiceCategory] = [
        ServiceCategory.AI_ML,               # GPU — very specific
        ServiceCategory.SERVERLESS_FUNCTION,  # invocations — very specific
        ServiceCategory.DATABASE,             # database_engine — specific
        ServiceCategory.CONTAINER,            # K8s fields — specific
        ServiceCategory.STORAGE,             # storage-only (no vcpus) — specific
        ServiceCategory.NETWORKING,          # network-only (no vcpus) — specific
        ServiceCategory.SERVERLESS_COMPUTE,  # keyword-only
        ServiceCategory.ANALYTICS,           # keyword-only
        ServiceCategory.COMPUTE,             # generic fallback (vcpus >= 1)
    ]

    # 1. Check resource-spec signals (strongest indicator)
    resources = workload.resources
    for category in _CATEGORY_PRIORITY:
        signals = _CATEGORY_SIGNALS.get(category)
        if not signals:
            continue
        resource_check = signals.get("resource_check")
        if resource_check and resource_check(resources):
            if category != workload.suggested_category:
                log.debug(
                    "category_overridden_by_resource_spec",
                    suggested=workload.suggested_category.value,
                    resolved=category.value,
                )
            return category

    # 2. Check name + description keywords
    text = f"{workload.name} {workload.description}".lower()
    for category in _CATEGORY_PRIORITY:
        signals = _CATEGORY_SIGNALS.get(category)
        if not signals:
            continue
        keywords = signals.get("keywords", [])
        if any(kw in text for kw in keywords):
            if category != workload.suggested_category:
                log.debug(
                    "category_overridden_by_keywords",
                    suggested=workload.suggested_category.value,
                    resolved=category.value,
                    matched_keyword=next(kw for kw in keywords if kw in text),
                )
            return category

    # 3. Fallback to the suggested_category
    log.debug("category_kept_as_suggested", category=workload.suggested_category.value)
    return workload.suggested_category


def _estimate_resources(
    workload: WorkloadRequirement,
    resolved_category: ServiceCategory,
    tier: WorkloadTier,
    environment: EnvironmentType,
) -> dict[str, Any]:
    """Estimate concrete resource numbers for a workload component.

    Fills in missing values from ``_RESOURCE_DEFAULTS``, then applies
    tier-based multipliers and environment scaling factors.

    Args:
        workload: The source workload requirement.
        resolved_category: The resolved service category.
        tier: Workload criticality tier (affects redundancy multipliers).
        environment: Deployment environment (affects scaling factor).

    Returns:
        Dict with keys ``vcpus``, ``memory_gb``, ``storage_gb``,
        ``iops``, ``requires_gpu``.
    """
    resources = workload.resources
    defaults = _RESOURCE_DEFAULTS.get(resolved_category, {})
    tier_mult = _TIER_MULTIPLIERS.get(tier, _TIER_MULTIPLIERS[WorkloadTier.BUSINESS_CRITICAL])
    env_factor = _ENVIRONMENT_FACTORS.get(environment, 1.0)

    # Base values: user-specified or defaults
    vcpus = resources.vcpus or defaults.get("vcpus", 2)
    memory_gb = resources.memory_gb or defaults.get("memory_gb", 4.0)
    storage_gb = resources.storage_gb or defaults.get("storage_gb", 50.0)
    iops = resources.iops or defaults.get("iops")

    # Apply tier multiplier (redundancy / headroom)
    vcpus = max(1, int(vcpus * tier_mult.get("compute", 1.0)))
    memory_gb = round(memory_gb * tier_mult.get("compute", 1.0), 1)
    storage_gb = round(storage_gb * tier_mult.get("storage", 1.0), 1)
    if iops is not None:
        iops = int(iops * tier_mult.get("iops", 1.0))

    # Apply environment scaling (dev/staging get smaller resources)
    if environment != EnvironmentType.PRODUCTION:
        vcpus = max(1, int(vcpus * env_factor))
        memory_gb = max(0.5, round(memory_gb * env_factor, 1))
        # Storage doesn't scale down as aggressively in dev
        storage_gb = max(10.0, round(storage_gb * max(env_factor, 0.5), 1))

    # GPU detection
    requires_gpu = resources.gpu_count > 0 or resolved_category == ServiceCategory.AI_ML

    return {
        "vcpus": vcpus,
        "memory_gb": memory_gb,
        "storage_gb": storage_gb,
        "iops": iops,
        "requires_gpu": requires_gpu,
    }


def _get_instance_families(
    resolved_category: ServiceCategory,
    requires_gpu: bool,
    target_providers: list[CloudProvider],
) -> list[str]:
    """Return recommended instance families for the resolved category.

    Looks up ``_INSTANCE_FAMILY_MAP`` and returns families for the
    requested providers.  If GPU is required and category is not AI_ML,
    overrides to AI_ML families.

    Args:
        resolved_category: The resolved service category.
        requires_gpu: Whether GPU acceleration is needed.
        target_providers: Which providers to include families for.

    Returns:
        Flat list of instance family names across providers.
    """
    # Override to AI_ML families if GPU required but category differs
    lookup_category = resolved_category
    if requires_gpu and resolved_category != ServiceCategory.AI_ML:
        lookup_category = ServiceCategory.AI_ML

    families_by_provider = _INSTANCE_FAMILY_MAP.get(lookup_category, {})
    result: list[str] = []

    for provider in target_providers:
        provider_families = families_by_provider.get(provider.value, [])
        result.extend(provider_families)

    return result


def _build_component_profile(
    workload: WorkloadRequirement,
    resolved_category: ServiceCategory,
    estimated: dict[str, Any],
    instance_families: list[str],
    rationale: str,
) -> ComponentProfile:
    """Construct a ``ComponentProfile`` from analysis results.

    Args:
        workload: The original workload requirement.
        resolved_category: Definitive service category.
        estimated: Resource estimation dict from ``_estimate_resources``.
        instance_families: Recommended instance families.
        rationale: LLM or heuristic-generated reasoning.

    Returns:
        A populated ``ComponentProfile``.
    """
    return ComponentProfile(
        workload_name=workload.name,
        resolved_category=resolved_category,
        estimated_vcpus=estimated["vcpus"],
        estimated_memory_gb=estimated["memory_gb"],
        estimated_storage_gb=estimated["storage_gb"],
        estimated_iops=estimated.get("iops"),
        requires_gpu=estimated["requires_gpu"],
        recommended_instance_families=instance_families,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# LLM-assisted profiling
# ---------------------------------------------------------------------------

_PROFILER_SYSTEM_PROMPT = """\
You are a cloud infrastructure profiling expert. Your task is to analyze a \
workload component and provide:

1. A brief rationale for the service category classification
2. Whether the resource estimates are appropriate
3. Any adjustments you'd recommend to the instance families

Respond in JSON with these fields:
- "rationale": string (2-3 sentences explaining the profiling decision)
- "adjustments": string (any recommended adjustments, or "none")
- "confidence": float (0.0-1.0, how confident you are in the profiling)

Be concise and technical. Focus on cloud architecture best practices.
"""


@observe()
async def _llm_enrich_profile(
    llm: BaseChatModel,
    workload: WorkloadRequirement,
    resolved_category: ServiceCategory,
    estimated: dict[str, Any],
    instance_families: list[str],
    environment: EnvironmentType,
    tier: WorkloadTier,
) -> str:
    """Use the LLM to generate a rationale for the profiling decision.

    Args:
        llm: The LLM model (BaseChatModel interface).
        workload: The workload requirement being profiled.
        resolved_category: The resolved category.
        estimated: Resource estimates.
        instance_families: Recommended families.
        environment: Deployment environment.
        tier: Criticality tier.

    Returns:
        LLM-generated rationale string.
    """
    log = logger.bind(agent="profiler", step="llm_enrich", workload=workload.name)
    log.debug("llm_enrich_started")

    user_content = json.dumps(
        {
            "workload_name": workload.name,
            "description": workload.description,
            "suggested_category": workload.suggested_category.value,
            "resolved_category": resolved_category.value,
            "scaling_pattern": workload.scaling_pattern.value,
            "resources": {
                "vcpus": estimated["vcpus"],
                "memory_gb": estimated["memory_gb"],
                "storage_gb": estimated["storage_gb"],
                "iops": estimated.get("iops"),
                "requires_gpu": estimated["requires_gpu"],
            },
            "instance_families": instance_families,
            "environment": environment.value,
            "tier": tier.value,
            "count": workload.count,
            "compliance_tags": workload.compliance_tags,
            "notes": workload.notes,
        },
        indent=2,
    )

    messages = [
        SystemMessage(content=_PROFILER_SYSTEM_PROMPT),
        HumanMessage(content=f"Analyze this workload component:\n\n{user_content}"),
    ]

    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, "content") else str(response)

        # Try to parse JSON for the rationale field
        try:
            parsed = json.loads(content)
            rationale = parsed.get("rationale", content)
            adjustments = parsed.get("adjustments", "none")
            if adjustments and adjustments.lower() != "none":
                rationale = f"{rationale} Adjustments: {adjustments}"
            log.debug(
                "llm_enrich_completed",
                confidence=parsed.get("confidence", "N/A"),
                rationale_length=len(rationale),
            )
            return rationale
        except (json.JSONDecodeError, TypeError):
            # LLM didn't return valid JSON — use raw content
            log.debug("llm_response_not_json", content_length=len(content))
            return content[:500]

    except Exception:
        log.warning("llm_enrich_failed", exc_info=True)
        # Graceful degradation: generate heuristic rationale
        return _heuristic_rationale(workload, resolved_category, estimated)


def _heuristic_rationale(
    workload: WorkloadRequirement,
    resolved_category: ServiceCategory,
    estimated: dict[str, Any],
) -> str:
    """Generate a heuristic rationale when LLM is unavailable.

    Args:
        workload: The workload requirement.
        resolved_category: The resolved category.
        estimated: Resource estimates.

    Returns:
        A generated rationale string.
    """
    parts = [
        f"'{workload.name}' classified as {resolved_category.value}",
        f"with {estimated['vcpus']} vCPUs, {estimated['memory_gb']} GB RAM",
    ]
    if estimated.get("iops"):
        parts.append(f"and {estimated['iops']} IOPS")
    if estimated["requires_gpu"]:
        parts.append("(GPU-accelerated)")
    if workload.resources.high_availability:
        parts.append("with high-availability enabled")
    if workload.scaling_pattern != ScalingPattern.STEADY:
        parts.append(f"using {workload.scaling_pattern.value} scaling pattern")

    return ". ".join(parts) + "."


# ---------------------------------------------------------------------------
# Profile summary generation
# ---------------------------------------------------------------------------


@observe()
async def _generate_profiler_notes(
    llm: BaseChatModel,
    workload_request: WorkloadRequest,
    components: list[ComponentProfile],
) -> str:
    """Generate an overall summary of the workload profile.

    Args:
        llm: The LLM model.
        workload_request: The top-level request.
        components: All profiled components.

    Returns:
        A summary string for ``WorkloadProfile.profiler_notes``.
    """
    log = logger.bind(agent="profiler", step="generate_notes")
    log.debug("generating_profiler_notes")

    component_summary = "\n".join(
        f"- {c.workload_name}: {c.resolved_category.value} "
        f"({c.estimated_vcpus} vCPU, {c.estimated_memory_gb} GB RAM, "
        f"{c.estimated_storage_gb} GB storage"
        f"{', GPU' if c.requires_gpu else ''})"
        for c in components
    )

    prompt = (
        f"Summarize this cloud workload profile for project "
        f"'{workload_request.project_name}' "
        f"({workload_request.environment.value} / "
        f"{workload_request.tier.value}):\n\n"
        f"{component_summary}\n\n"
        f"Provide a 2-3 sentence technical summary covering: total resource "
        f"footprint, key architectural considerations, and any recommendations "
        f"for the sizing phase. Be concise."
    )

    try:
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        notes = response.content if hasattr(response, "content") else str(response)
        log.debug("profiler_notes_generated", length=len(notes))
        return notes[:1000]
    except Exception:
        log.warning("profiler_notes_llm_failed", exc_info=True)
        # Fallback heuristic summary
        total_vcpus = sum(c.estimated_vcpus for c in components)
        total_mem = sum(c.estimated_memory_gb for c in components)
        total_storage = sum(c.estimated_storage_gb for c in components)
        gpu_count = sum(1 for c in components if c.requires_gpu)
        return (
            f"Workload comprises {len(components)} components totaling "
            f"{total_vcpus} vCPUs, {total_mem:.1f} GB RAM, "
            f"{total_storage:.1f} GB storage"
            f"{f', {gpu_count} GPU workloads' if gpu_count else ''}. "
            f"Environment: {workload_request.environment.value}, "
            f"Tier: {workload_request.tier.value}."
        )


# ---------------------------------------------------------------------------
# Main profiler node
# ---------------------------------------------------------------------------


@observe()
async def run_profiler_node(
    state: OrchestratorState,
    llm: BaseChatModel,
) -> OrchestratorState:
    """Execute the Profiler agent — analyze workloads and build profiles.

    This is a LangGraph node function.  It reads ``state['workload_request']``
    and produces ``state['workload_profile']`` with one ``ComponentProfile``
    per ``WorkloadRequirement``.

    Args:
        state: Current ``OrchestratorState`` (TypedDict).
        llm: LLM instance for generating rationales (``BaseChatModel``
             interface — never import a provider-specific class here).

    Returns:
        Updated ``OrchestratorState`` with ``workload_profile`` populated
        and a summary message appended to ``messages``.

    Raises:
        ValueError: If ``workload_request`` is missing or has no workloads.
    """
    log = logger.bind(
        agent="profiler",
        request_id=state.get("request_id", "unknown"),
    )

    start_time = datetime.now(timezone.utc)
    log.info("profiler_node_started")

    try:
        # ── Validate input ────────────────────────────────────
        workload_request: WorkloadRequest | None = state.get("workload_request")
        if workload_request is None:
            raise ValueError("workload_request is missing from state — Clarifier must run first")

        if not workload_request.workloads:
            raise ValueError(
                "workload_request has no workloads — "
                "Clarifier should have extracted at least one"
            )

        log.info(
            "profiling_workloads",
            project=workload_request.project_name,
            environment=workload_request.environment.value,
            tier=workload_request.tier.value,
            workload_count=len(workload_request.workloads),
            target_providers=[p.value for p in workload_request.target_providers],
        )

        # ── Profile each workload component ───────────────────
        components: list[ComponentProfile] = []
        total_vcpus = 0
        total_memory_gb = 0.0
        total_storage_gb = 0.0
        total_gpu_count = 0
        any_gpu = False

        for idx, workload in enumerate(workload_request.workloads):
            wl_log = log.bind(
                workload_name=workload.name,
                workload_index=idx,
            )
            wl_log.info(
                "profiling_component_started",
                suggested_category=workload.suggested_category.value,
            )

            # 1. Resolve category
            resolved_category = _resolve_category(workload)
            wl_log.info(
                "category_resolved",
                suggested=workload.suggested_category.value,
                resolved=resolved_category.value,
            )

            # 2. Estimate resources
            estimated = _estimate_resources(
                workload,
                resolved_category,
                workload_request.tier,
                workload_request.environment,
            )
            wl_log.info(
                "resources_estimated",
                vcpus=estimated["vcpus"],
                memory_gb=estimated["memory_gb"],
                storage_gb=estimated["storage_gb"],
                iops=estimated.get("iops"),
                requires_gpu=estimated["requires_gpu"],
            )

            # 3. Get instance families
            instance_families = _get_instance_families(
                resolved_category,
                estimated["requires_gpu"],
                workload_request.target_providers,
            )
            wl_log.debug(
                "instance_families_selected",
                families=instance_families,
            )

            # 4. Generate rationale (LLM-assisted)
            rationale = await _llm_enrich_profile(
                llm=llm,
                workload=workload,
                resolved_category=resolved_category,
                estimated=estimated,
                instance_families=instance_families,
                environment=workload_request.environment,
                tier=workload_request.tier,
            )

            # 5. Build ComponentProfile
            profile = _build_component_profile(
                workload=workload,
                resolved_category=resolved_category,
                estimated=estimated,
                instance_families=instance_families,
                rationale=rationale,
            )
            components.append(profile)

            # 6. Accumulate totals (account for workload count / replicas)
            count = workload.count
            total_vcpus += estimated["vcpus"] * count
            total_memory_gb += estimated["memory_gb"] * count
            total_storage_gb += estimated["storage_gb"] * count
            if estimated["requires_gpu"]:
                any_gpu = True
                total_gpu_count += max(workload.resources.gpu_count, 1) * count

            wl_log.info(
                "profiling_component_completed",
                resolved_category=resolved_category.value,
                rationale_length=len(rationale),
            )

        # ── Generate profiler notes (LLM-assisted) ────────────
        profiler_notes = await _generate_profiler_notes(
            llm=llm,
            workload_request=workload_request,
            components=components,
        )

        # ── Assemble WorkloadProfile ──────────────────────────
        workload_profile = WorkloadProfile(
            components=components,
            total_vcpus=total_vcpus,
            total_memory_gb=round(total_memory_gb, 1),
            total_storage_gb=round(total_storage_gb, 1),
            total_gpu_count=total_gpu_count,
            requires_gpu=any_gpu,
            environment=workload_request.environment,
            tier=workload_request.tier,
            profiler_notes=profiler_notes,
        )

        log.info(
            "workload_profile_assembled",
            component_count=len(components),
            total_vcpus=total_vcpus,
            total_memory_gb=round(total_memory_gb, 1),
            total_storage_gb=round(total_storage_gb, 1),
            total_gpu_count=total_gpu_count,
            requires_gpu=any_gpu,
        )

        # ── Build summary message ─────────────────────────────
        component_lines = "\n".join(
            f"  • {c.workload_name}: {c.resolved_category.value} — "
            f"{c.estimated_vcpus} vCPU, {c.estimated_memory_gb} GB RAM"
            f"{', GPU' if c.requires_gpu else ''}"
            for c in components
        )
        summary_content = (
            f"**Profiler Analysis Complete** — {len(components)} component(s) profiled:\n"
            f"{component_lines}\n\n"
            f"**Totals**: {total_vcpus} vCPUs, {total_memory_gb:.1f} GB RAM, "
            f"{total_storage_gb:.1f} GB storage"
            f"{f', {total_gpu_count} GPU(s)' if any_gpu else ''}\n\n"
            f"{profiler_notes}"
        )

        summary_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=summary_content,
            agent_name="profiler",
            metadata={
                "component_count": len(components),
                "total_vcpus": total_vcpus,
                "total_memory_gb": round(total_memory_gb, 1),
                "total_storage_gb": round(total_storage_gb, 1),
            },
        )

        # ── Update state ──────────────────────────────────────
        state["workload_profile"] = workload_profile
        state["messages"] = [summary_message]  # append-only reducer merges
        state["current_agent"] = "sizer"

        # Track execution
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        if "agent_executions" not in state:
            state["agent_executions"] = {}
        state["agent_executions"]["profiler"] = AgentExecution(
            agent_name="profiler",
            status=AgentStatus.COMPLETED,
            started_at=start_time,
            completed_at=datetime.now(timezone.utc),
            duration_ms=elapsed,
        )

        # Update KPIs
        kpis = state.get("kpis", {})
        kpis["total_llm_calls"] = kpis.get("total_llm_calls", 0) + len(components) + 1
        state["kpis"] = kpis

        log.info(
            "profiler_node_completed",
            elapsed_ms=round(elapsed, 1),
            component_count=len(components),
        )

        return state

    except Exception as e:
        log.error("profiler_node_failed", exc_info=True)
        state["error"] = str(e)
        if "agent_executions" not in state:
            state["agent_executions"] = {}
        state["agent_executions"]["profiler"] = AgentExecution(
            agent_name="profiler",
            status=AgentStatus.FAILED,
            error_message=str(e),
        )
        raise
