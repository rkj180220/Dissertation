"""Sizer Agent — SKU selection and Kubernetes node pool sizing.

The Sizer is the third stage of the orchestration pipeline, running
after the Profiler has produced a ``WorkloadProfile``.  It maps each
``ComponentProfile`` to concrete cloud SKUs using the ``PricingService``
and the algorithmic engines (scoring, bin-packing).

### Responsibilities

1. **SKU search** — For each component, query the ``PricingService``
   to obtain candidate ``NormalizedPriceItem`` records that match the
   resolved category, region, and provider.
2. **Compute scoring** — For COMPUTE / AI_ML workloads, run the
   multi-criteria ``scoring.score_skus()`` engine to rank candidates.
3. **Container bin-packing** — For CONTAINER workloads, run the
   ``bin_packing.pack_workloads()`` engine to compute optimal node
   pool sizes.
4. **Category-aware selection** — For DATABASE, STORAGE, SERVERLESS,
   and other categories, select the best-priced SKU by filtering and
   sorting candidates.
5. **Result assembly** — Produce one ``SizedWorkloadResult`` per
   workload per provider, with fit scores, selected SKU, alternatives,
   and rationale.

### Flow

```
WorkloadProfile (from Profiler)
      │
      ▼
┌──────────────────────────────────────────┐
│  For each ComponentProfile × Provider    │
│    ├─ PricingService.search_prices()     │
│    ├─ Filter/score/bin-pack by category  │
│    └─ Build SizedWorkloadResult          │
└──────────────────┬───────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────┐
│  Append list[SizedWorkloadResult] to     │
│  state['sized_results']                  │
└──────────────────────────────────────────┘
```

### Usage

```python
from src.agents.sizer import run_sizer_node

# state already has workload_profile populated by profiler
state = await run_sizer_node(state, llm, pricing_service)
# state['sized_results'] now contains per-workload, per-provider selections
# state['messages'] has sizer summary appended
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

from src.engines import extract_memory_gb, extract_vcpus
from src.engines.bin_packing import PackingAlgorithm, pack_workloads
from src.engines.scoring import ScoredSKU, score_skus
from src.engines.vm_specs import compose_gcp_vm_instances
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.conversation import ChatMessage, MessageRole
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.models.recommendation import BinPackingResult
from src.models.workload import (
    ComponentProfile,
    WorkloadProfile,
    WorkloadRequest,
    WorkloadRequirement,
    WorkloadTier,
)
from src.orchestrator.state import (
    AgentExecution,
    AgentStatus,
    OrchestratorState,
    SizedWorkloadResult,
)
from src.services.pricing_service import PricingService

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Category → service name mapping for PricingService queries
# ---------------------------------------------------------------------------

#: Maps (provider, category) to the service name filter used by the
#: PricingService search.  Falls back to category-only search when no
#: mapping exists.
_SERVICE_NAME_MAP: dict[tuple[str, ServiceCategory], str] = {
    # AWS
    ("aws", ServiceCategory.COMPUTE): "AmazonEC2",
    ("aws", ServiceCategory.DATABASE): "AmazonRDS",
    ("aws", ServiceCategory.STORAGE): "AmazonS3",
    ("aws", ServiceCategory.KUBERNETES): "AmazonEKS",
    ("aws", ServiceCategory.CONTAINER): "AmazonEKS",
    ("aws", ServiceCategory.AI_ML): "AmazonSageMaker",
    ("aws", ServiceCategory.SERVERLESS_FUNCTION): "AWSLambda",
    ("aws", ServiceCategory.SERVERLESS): "AWSLambda",
    ("aws", ServiceCategory.SERVERLESS_COMPUTE): "AmazonEKS",  # Knative runs on EKS nodes
    ("aws", ServiceCategory.NETWORKING): "AmazonVPC",
    ("aws", ServiceCategory.ANALYTICS): "AmazonRedshift",
    # Azure
    ("azure", ServiceCategory.COMPUTE): "Virtual Machines",
    ("azure", ServiceCategory.DATABASE): "SQL Database",
    ("azure", ServiceCategory.STORAGE): "Storage",
    ("azure", ServiceCategory.KUBERNETES): "Azure Kubernetes Service",
    ("azure", ServiceCategory.CONTAINER): "Azure Kubernetes Service",
    ("azure", ServiceCategory.AI_ML): "Azure Machine Learning",
    ("azure", ServiceCategory.SERVERLESS_FUNCTION): "Functions",
    ("azure", ServiceCategory.SERVERLESS): "Functions",
    ("azure", ServiceCategory.SERVERLESS_COMPUTE): "Azure Kubernetes Service",  # Knative on AKS
    ("azure", ServiceCategory.NETWORKING): "Virtual Network",
    ("azure", ServiceCategory.ANALYTICS): "Azure Synapse Analytics",
    # GCP
    ("gcp", ServiceCategory.COMPUTE): "Compute Engine",
    ("gcp", ServiceCategory.DATABASE): "Cloud SQL",
    ("gcp", ServiceCategory.STORAGE): "Cloud Storage",
    ("gcp", ServiceCategory.KUBERNETES): "Kubernetes Engine",
    ("gcp", ServiceCategory.CONTAINER): "Kubernetes Engine",
    ("gcp", ServiceCategory.AI_ML): "Vertex AI",
    ("gcp", ServiceCategory.SERVERLESS_FUNCTION): "Cloud Functions",
    ("gcp", ServiceCategory.SERVERLESS): "Cloud Run",
    ("gcp", ServiceCategory.SERVERLESS_COMPUTE): "Kubernetes Engine",  # Knative on GKE
    ("gcp", ServiceCategory.NETWORKING): "Cloud Networking",
    ("gcp", ServiceCategory.ANALYTICS): "BigQuery",
}

#: Categories that should use the multi-criteria scoring engine
_SCORED_CATEGORIES: set[ServiceCategory] = {
    ServiceCategory.COMPUTE,
    ServiceCategory.AI_ML,
}

#: Categories that should use the bin-packing engine
_BINPACKED_CATEGORIES: set[ServiceCategory] = {
    ServiceCategory.CONTAINER,
    # P16b: Knative/KEDA self-hosted serverless also bins onto K8s nodes.
    ServiceCategory.SERVERLESS_COMPUTE,
}

#: Max alternative SKUs to keep per workload
_MAX_ALTERNATIVES = 3


# ---------------------------------------------------------------------------
# Container / Database / Ancillary cost constants
# ---------------------------------------------------------------------------

#: Maps provider to the VM service used for K8s node pool sizing.
#: Container workloads need VM candidates, not K8s control-plane prices.
_NODE_POOL_VM_SERVICE: dict[str, str] = {
    "aws": "AmazonEC2",
    "azure": "Virtual Machines",
    "gcp": "Compute Engine",
}

#: Known K8s cluster management fees (monthly USD, per cluster).
_K8S_CLUSTER_FEE_MONTHLY: dict[str, float] = {
    "aws": 73.00,   # EKS: $0.10/hr
    "azure": 0.00,  # AKS free tier (basic)
    "gcp": 73.00,   # GKE standard: $0.10/hr
}

#: Database engine → provider-specific service + optional SKU name filter.
#: Values are ``(service_name_override, sku_name_filter | None)``.
_DATABASE_ENGINE_MAP: dict[tuple[str, str], tuple[str, str | None]] = {
    # AWS
    ("aws", "postgresql"): ("AmazonRDS", "PostgreSQL"),
    ("aws", "postgres"): ("AmazonRDS", "PostgreSQL"),
    ("aws", "mysql"): ("AmazonRDS", "MySQL"),
    ("aws", "mariadb"): ("AmazonRDS", "MariaDB"),
    ("aws", "aurora-postgresql"): ("AmazonRDS", "Aurora PostgreSQL"),
    ("aws", "aurora-mysql"): ("AmazonRDS", "Aurora MySQL"),
    ("aws", "sqlserver"): ("AmazonRDS", "SQL Server"),
    # Azure
    ("azure", "postgresql"): ("Azure Database for PostgreSQL", None),
    ("azure", "postgres"): ("Azure Database for PostgreSQL", None),
    ("azure", "mysql"): ("Azure Database for MySQL", None),
    ("azure", "sqlserver"): ("SQL Database", None),
    # GCP
    ("gcp", "postgresql"): ("Cloud SQL", "PostgreSQL"),
    ("gcp", "postgres"): ("Cloud SQL", "PostgreSQL"),
    ("gcp", "mysql"): ("Cloud SQL", "MySQL"),
    ("gcp", "sqlserver"): ("Cloud SQL", "SQL Server"),
    # AWS ElastiCache (Redis / Memcached) — separate service from RDS
    ("aws", "redis"): ("AmazonElastiCache", "cache.r6g"),
    ("aws", "elasticache"): ("AmazonElastiCache", "cache.r6g"),
    ("aws", "memcached"): ("AmazonElastiCache", "cache.m6g"),
    # Azure Cache for Redis
    ("azure", "redis"): ("Azure Cache for Redis", None),
    ("azure", "memcached"): ("Azure Cache for Redis", None),
    # GCP Memorystore (correct service display names as used by GCP Billing API)
    ("gcp", "redis"): ("Cloud Memorystore for Redis", None),
    ("gcp", "memcached"): ("Cloud Memorystore for Memcached", None),
}

#: Known load balancer base costs (monthly USD).
_LOAD_BALANCER_FEE_MONTHLY: dict[str, float] = {
    "aws": 22.27,   # ALB: ~$0.0225/hr + LCU
    "azure": 18.25, # Standard LB: ~$0.025/hr
    "gcp": 18.26,   # Cloud LB: ~$0.025/hr
}

#: Estimated ancillary infrastructure costs (monthly USD).
#: These cover baseline items NOT modeled as explicit workloads.
_ANCILLARY_COSTS: dict[str, list[tuple[str, float]]] = {
    "aws": [
        ("NAT Gateway", 32.40),
        ("Data Transfer (est. 100 GB egress)", 9.00),
    ],
    "azure": [
        ("NAT Gateway", 32.85),
        ("Data Transfer (est. 100 GB egress)", 8.70),
    ],
    "gcp": [
        ("Cloud NAT", 32.40),
        ("Data Transfer (est. 100 GB egress)", 12.00),
    ],
}

#: Estimated monthly CDN / edge delivery costs (fixed estimate, ~500 GB transfer).
_CDN_COST_MONTHLY: dict[str, float] = {
    "aws": 85.00,    # CloudFront: ~500 GB transfer + 10M requests
    "azure": 70.00,  # Azure CDN: ~500 GB transfer
    "gcp": 60.00,    # Cloud CDN: ~500 GB transfer
}

#: Fixed-cost estimates for Redis / Memcached cache services (monthly USD).
#: Used when the pricing API returns no SKU candidates for a cache workload,
#: which can happen for services billed via reserved capacity (e.g., Azure
#: Cache for Redis in some regions).
_REDIS_CACHE_COST_MONTHLY: dict[str, float] = {
    "aws": 65.00,    # ElastiCache cache.r6g.large: ~$0.084/hr  × 730h ≈ $61
    "azure": 55.00,  # Azure Cache for Redis C1 Standard: ~$55/mo
    "gcp": 48.00,    # Cloud Memorystore for Redis 1 GB Basic: ~$0.065/hr × 730h ≈ $47
}

#: Fixed-cost estimates for PostgreSQL / relational databases (monthly USD).
#: Applied when the pricing API returns zero candidates — most commonly AWS RDS
#: in regions where the Bulk Pricing API does not index RDS SKUs.
#: Values represent a multi-AZ db.t3.medium (2 vCPU / 4 GiB) baseline.
_RDS_COST_MONTHLY: dict[str, float] = {
    "aws": 185.00,   # RDS PostgreSQL db.t3.medium Multi-AZ: ~$0.253/hr × 730h ≈ $185
    "azure": 0.0,    # Azure PostgreSQL Flexible Server returns candidates; no fallback needed
    "gcp": 0.0,      # GCP Cloud SQL returns candidates; no fallback needed
}

#: Cross-provider region equivalence: AWS region → nearest Azure region.
#: Used to translate the user's AWS preferred_region to a geographically
#: equivalent Azure region when the Azure region hasn't been explicitly set.
_AWS_TO_AZURE_REGION: dict[str, str] = {
    "us-east-1": "eastus",
    "us-east-2": "eastus2",
    "us-west-1": "westus",
    "us-west-2": "westus2",
    "eu-west-1": "westeurope",
    "eu-west-2": "uksouth",
    "eu-west-3": "francecentral",
    "eu-central-1": "germanywestcentral",
    "eu-north-1": "swedencentral",
    "ap-southeast-1": "southeastasia",
    "ap-southeast-2": "australiaeast",
    "ap-northeast-1": "japaneast",
    "ap-south-1": "centralindia",
    "sa-east-1": "brazilsouth",
    "ca-central-1": "canadacentral",
}

#: Cross-provider region equivalence: AWS region → nearest GCP region.
_AWS_TO_GCP_REGION: dict[str, str] = {
    "us-east-1": "us-central1",
    "us-east-2": "us-east4",
    "us-west-1": "us-west2",
    "us-west-2": "us-west1",
    "eu-west-1": "europe-west1",
    "eu-west-2": "europe-west2",
    "eu-west-3": "europe-west9",
    "eu-central-1": "europe-west3",
    "eu-north-1": "europe-north1",
    "ap-southeast-1": "asia-southeast1",
    "ap-southeast-2": "australia-southeast1",
    "ap-northeast-1": "asia-northeast1",
    "ap-south-1": "asia-south1",
    "sa-east-1": "southamerica-east1",
    "ca-central-1": "northamerica-northeast1",
}

#: Preferred general-purpose instance families for container node pools.
#: Sorting viable nodes by these families prevents selecting oversized
#: memory-optimized (x, u, hpc) or specialized instance types.
_PREFERRED_CONTAINER_FAMILIES: dict[str, tuple[str, ...]] = {
    "aws": (
        "m5.", "m6i.", "m6a.", "m7i.", "m7g.",
        "c5.", "c6i.", "c6a.", "c7i.", "c7g.",
        "t3.", "t3a.",
    ),
    "azure": ("Standard_D", "Standard_B", "Standard_F"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_region_for_provider(
    provider: CloudProvider,
    workload_request: WorkloadRequest,
    workload: WorkloadRequirement | None = None,
) -> str:
    """Resolve the region string for a provider.

    Priority: workload region_affinity > provider_regions map > preferred_region.

    If ``preferred_region`` is an AWS region slug and the provider-specific
    region is still the model default (i.e. the user never explicitly set it),
    a cross-provider equivalent region is used instead of the default so that
    all three providers price in the same geography.

    Args:
        provider: The target cloud provider.
        workload_request: Top-level request with region info.
        workload: Optional specific workload (may have region_affinity).

    Returns:
        Provider-native region string.
    """
    if workload and workload.region_affinity:
        return workload.region_affinity

    region = workload_request.provider_regions.get(
        provider.value,
        workload_request.preferred_region,
    )

    # Cross-provider region mapping: if preferred_region is an AWS region
    # and the provider-specific entry is still the model default, translate
    # to a geographically equivalent region for that provider.
    preferred = workload_request.preferred_region
    _AZURE_DEFAULT = "eastus"
    _GCP_DEFAULT = "us-central1"

    if (
        provider == CloudProvider.AZURE
        and region == _AZURE_DEFAULT
        and preferred not in (_AZURE_DEFAULT, "")
    ):
        mapped = _AWS_TO_AZURE_REGION.get(preferred)
        if mapped:
            region = mapped

    elif (
        provider == CloudProvider.GCP
        and region == _GCP_DEFAULT
        and preferred not in (_GCP_DEFAULT, "")
    ):
        mapped = _AWS_TO_GCP_REGION.get(preferred)
        if mapped:
            region = mapped

    return region


def _build_workload_requirement_for_component(
    component: ComponentProfile,
    original_workload: WorkloadRequirement,
) -> WorkloadRequirement:
    """Reconstruct a WorkloadRequirement from profiler output.

    The scoring engine needs ``WorkloadRequirement`` objects.  We
    merge the profiler's enriched estimates back onto the original
    workload's resource spec.

    Args:
        component: Profiler's component profile.
        original_workload: The original workload requirement.

    Returns:
        A WorkloadRequirement with profiler-enriched resource values.
    """
    from src.models.workload import ResourceSpec

    enriched_resources = original_workload.resources.model_copy(
        update={
            "vcpus": component.estimated_vcpus or original_workload.resources.vcpus,
            "memory_gb": component.estimated_memory_gb or original_workload.resources.memory_gb,
            "storage_gb": component.estimated_storage_gb or original_workload.resources.storage_gb,
            "gpu_count": (
                max(original_workload.resources.gpu_count, 1)
                if component.requires_gpu
                else original_workload.resources.gpu_count
            ),
        },
    )

    return original_workload.model_copy(
        update={
            "resources": enriched_resources,
            "suggested_category": component.resolved_category,
        },
    )


def _select_best_by_price(
    candidates: list[NormalizedPriceItem],
) -> tuple[NormalizedPriceItem | None, list[NormalizedPriceItem]]:
    """Select the cheapest SKU from a list and return alternatives.

    Filters to items with a computable monthly cost, sorts ascending.

    Args:
        candidates: List of candidate SKUs.

    Returns:
        Tuple of (best_sku, alternatives).
    """
    with_cost = [
        c for c in candidates
        if c.monthly_cost_estimate is not None and c.monthly_cost_estimate > 0
    ]
    if not with_cost:
        return (None, [])

    sorted_items = sorted(with_cost, key=lambda c: c.monthly_cost_estimate or 0.0)
    best = sorted_items[0]
    alternatives = sorted_items[1: 1 + _MAX_ALTERNATIVES]
    return (best, alternatives)


def _is_fixed_cost_workload(workload: WorkloadRequirement) -> str | None:
    """Check if a workload has a known fixed cost instead of needing SKU lookup.

    Returns:
        A cost-type identifier (``"cluster_management_fee"`` or
        ``"load_balancer"``) or ``None`` if normal pricing applies.
    """
    notes = (workload.notes or "").lower()
    if "cluster_management_fee" in notes:
        return "cluster_management_fee"

    name_lower = workload.name.lower()
    if "load balancer" in name_lower:
        return "load_balancer"

    return None


def _infer_engine_from_name(workload_name: str) -> str:
    """Infer a database engine key from the workload name when the engine field is unset.

    This is a fallback for workloads where the Clarifier did not explicitly
    populate ``resources.database_engine`` (e.g. "Postgresql Database",
    "Cache Layer").

    Args:
        workload_name: Human-readable workload name.

    Returns:
        Lowercase engine key (e.g. ``"postgresql"``, ``"redis"``) or empty string.
    """
    name = workload_name.lower()
    if any(k in name for k in ("postgres", "postgresql")):
        return "postgresql"
    if any(k in name for k in ("mysql",)):
        return "mysql"
    if any(k in name for k in ("mariadb",)):
        return "mariadb"
    if any(k in name for k in ("sqlserver", "mssql", "sql server")):
        return "sqlserver"
    if any(k in name for k in ("redis", "cache", "elasticache", "memcache")):
        return "redis"
    if "memcached" in name:
        return "memcached"
    if any(k in name for k in ("aurora",)) and "mysql" in name:
        return "aurora-mysql"
    if any(k in name for k in ("aurora",)):
        return "aurora-postgresql"
    return ""


def _get_db_search_params(
    provider: CloudProvider,
    workload: WorkloadRequirement,
) -> tuple[str | None, str | None]:
    """Resolve database-engine-specific service name and SKU filter.

    Resolves in priority order:
    1. ``workload.resources.database_engine`` (explicit, set by Clarifier)
    2. Name-based inference via :func:`_infer_engine_from_name` (fallback)

    Args:
        provider: Target cloud provider.
        workload: Workload with optional ``resources.database_engine``.

    Returns:
        Tuple of ``(service_name_override, sku_name_filter)``.
        Both may be ``None`` if no engine is specified.
    """
    engine = (workload.resources.database_engine or "").lower().strip()
    if not engine:
        engine = _infer_engine_from_name(workload.name)
    if not engine:
        return (None, None)

    key = (provider.value, engine)
    if key in _DATABASE_ENGINE_MAP:
        return _DATABASE_ENGINE_MAP[key]

    # Partial match: try matching engine as a substring
    for (prov, eng), val in _DATABASE_ENGINE_MAP.items():
        if prov == provider.value and eng in engine:
            return val

    return (None, None)


def _build_ancillary_results(
    target_providers: list[CloudProvider],
    has_containers: bool,
) -> list[SizedWorkloadResult]:
    """Generate ancillary infrastructure cost estimates.

    Adds baseline costs for networking items that are not modeled
    as explicit workloads (NAT gateway, data transfer).

    Args:
        target_providers: Providers to generate estimates for.
        has_containers: Whether the workload includes container components.

    Returns:
        List of ``SizedWorkloadResult`` with estimated ancillary costs.
    """
    results: list[SizedWorkloadResult] = []
    for provider in target_providers:
        costs = _ANCILLARY_COSTS.get(provider.value, [])
        for cost_name, monthly in costs:
            # NAT only relevant when containers / VPCs are involved
            is_nat = "nat" in cost_name.lower()
            if is_nat and not has_containers:
                continue

            results.append(
                SizedWorkloadResult(
                    workload_name=f"[Infra] {cost_name}",
                    provider=provider,
                    selected_sku=None,
                    alternative_skus=[],
                    monthly_cost_usd=monthly,
                    fit_score=1.0,
                    rationale=(
                        f"Estimated baseline cost for {cost_name}: "
                        f"${monthly:.2f}/mo on {provider.value}."
                    ),
                )
            )
    return results


def _is_cdn_workload(workload: WorkloadRequirement) -> bool:
    """Detect CDN / edge delivery workloads by notes or name.

    CDN workloads have no vCPU-based pricing (billed per-GB transferred
    and per-request). We use a fixed-cost estimate instead of a SKU lookup.

    Args:
        workload: The workload requirement to check.

    Returns:
        True when the workload represents a CDN / edge delivery layer.
    """
    notes = (workload.notes or "").lower()
    name_lower = workload.name.lower()
    return (
        notes == "cdn"
        or "cdn" in name_lower
        or "edge delivery" in name_lower
        or "cloudfront" in name_lower
        or "cloud cdn" in name_lower
    )


def _is_redis_workload(workload: WorkloadRequirement) -> bool:
    """Detect Redis / Memcached cache workloads that need a fixed-cost fallback.

    Azure Cache for Redis and GCP Memorystore are not always returned by the
    pricing API for all regions.  When SKU lookup fails, we apply a monthly
    fixed-cost estimate rather than reporting $0.

    Args:
        workload: The workload requirement to check.

    Returns:
        True when the workload represents an in-memory cache service.
    """
    name_lower = workload.name.lower()
    engine = (workload.resources.database_engine or "").lower()
    return (
        "redis" in name_lower
        or "memcached" in name_lower
        or "cache" in name_lower
        or engine in ("redis", "memcached", "elasticache", "memorystore")
    )


def _is_postgres_workload(workload: WorkloadRequirement) -> bool:
    """Detect PostgreSQL / relational database workloads that need a fixed-cost fallback.

    AWS RDS pricing is not indexed in the Bulk Pricing API for all regions.
    When SKU lookup returns zero candidates for a database workload on AWS,
    apply a monthly fixed-cost estimate rather than reporting $0.

    Args:
        workload: The workload requirement to check.

    Returns:
        True when the workload represents a relational database service.
    """
    name_lower = workload.name.lower()
    engine = (workload.resources.database_engine or "").lower()
    return (
        "postgres" in name_lower
        or "database" in name_lower
        or "rds" in name_lower
        or "sql" in name_lower
        or engine in ("postgresql", "postgres", "mysql", "mariadb", "rds", "aurora")
    )


def _parse_memory_gib(memory_str: str | None) -> float:
    """Parse a cloud provider memory string like '8 GiB' or '16 GB' to float.

    Args:
        memory_str: Raw memory string from pricing attributes, e.g. "8 GiB".

    Returns:
        Memory in GiB as a float, or 0.0 if unparseable.
    """
    if not memory_str:
        return 0.0
    try:
        return float(str(memory_str).strip().split()[0])
    except (ValueError, IndexError):
        return 0.0


def _filter_database_candidates(
    candidates: list[NormalizedPriceItem],
    required_memory_gb: float,
    required_vcpus: int,
) -> list[NormalizedPriceItem]:
    """Filter database/cache SKU candidates to exclude deprecated and undersized instances.

    Applies two passes:
    1. Excludes legacy (``currentGeneration: No``) instances — these are
       deprecated hardware that AWS continues to price but should not be
       selected for new workloads.
    2. Filters to instances that meet the minimum memory and vCPU requirements
       of the workload (allowing up to 20% undersizing as tolerance).

    Falls back gracefully at each step — if a filter removes all candidates,
    the previous set is returned so we never return an empty list.

    Args:
        candidates: Raw candidates (already filtered to instance-hour rows).
        required_memory_gb: Minimum RAM in GiB required by the workload.
        required_vcpus: Minimum vCPU count required by the workload.

    Returns:
        Filtered candidate list with deprecated and undersized SKUs removed.
    """
    # Pass 1: exclude deprecated (non-current-generation) instances
    current_gen = [
        c for c in candidates
        if c.attributes.get("currentGeneration", "Yes") != "No"
    ]
    working = current_gen if current_gen else candidates

    # Pass 2: filter to instances meeting minimum resource requirements
    if required_memory_gb > 0 or required_vcpus > 0:
        resource_ok: list[NormalizedPriceItem] = []
        for c in working:
            cand_mem = _parse_memory_gib(c.attributes.get("memory"))
            try:
                cand_vcpu = int(str(c.attributes.get("vcpu", "0")).split()[0])
            except (ValueError, AttributeError):
                cand_vcpu = 0

            # 0 means the attribute is absent — don't filter those out
            mem_ok = cand_mem == 0 or cand_mem >= required_memory_gb * 0.8
            vcpu_ok = cand_vcpu == 0 or cand_vcpu >= max(1, int(required_vcpus * 0.5))

            if mem_ok and vcpu_ok:
                resource_ok.append(c)

        if resource_ok:
            return resource_ok
        # fallback: requirements couldn't be met — return current-gen set
        return working

    return working


def _filter_storage_candidates(
    candidates: list[NormalizedPriceItem],
    provider: CloudProvider,
) -> list[NormalizedPriceItem]:
    """Filter storage SKU candidates to exclude archival and penalty rows.

    Cloud storage APIs return many row types: standard tier, nearline,
    archive, early-delete penalties, retrieval fees, etc. We want standard
    hot-tier storage for a realistic baseline cost estimate.

    Args:
        candidates: Raw candidates from PricingService.
        provider: Target cloud provider (used for provider-specific logic).

    Returns:
        Filtered list with archival / penalty rows removed.
    """
    _EXCLUDE_PATTERNS = (
        "earlydelete", "earlydeletion", "glacier", "deeparchive",
        "retrievalfee", "retrieval", "coldline", "nearline",
        "archive", "infrequent",
        # Intelligent-Tiering archive sub-tiers: Archive Instant Access (AIA),
        # Archive Access (AA), and Deep Archive Access (DA) have the same
        # per-GB rate as Standard but should not be selected as a baseline.
        "int-aia", "int-aa", "int-da", "int-fa",
        "-aia-", "-aa-",
        # S3 Glacier Instant Retrieval has its own SKU code prefix (GIR) that
        # does not include the word "glacier" in the meter name.
        # e.g. "USW2-TimedStorage-GIR-ByteHrs"
        "-gir-", "gir-bytehrs",
        # S3 One Zone-IA (Zone Infrequent Access) rows slip through the
        # "infrequent" filter because their SKU uses the acronym "ZIA".
        # e.g. "USW2-TimedStorage-ZIA-ByteHrs"
        "-zia-", "zia-bytehrs",
    )
    # Pre-filter: remove per-request / per-IOPS / per-provisioned-unit rows.
    # These use units like "10K", "1/Month" (flat per-disk), or "1/Hour"
    # (per-IOPS), which are not per-GB capacity pricing and produce nonsensical
    # estimates when multiplied by storage_gb.
    _GB_UNITS = ("gb", "gib")
    per_gb_only = [
        c for c in candidates
        if any(u in c.unit_of_measure.lower() for u in _GB_UNITS)
        and not any(pat in c.sku_name.lower() for pat in _EXCLUDE_PATTERNS)
        and c.monthly_cost_estimate is not None
        and c.monthly_cost_estimate > 0
    ]
    filtered = per_gb_only if per_gb_only else [
        c for c in candidates
        if not any(pat in c.sku_name.lower() for pat in _EXCLUDE_PATTERNS)
        and c.monthly_cost_estimate is not None
        and c.monthly_cost_estimate > 0
    ]
    if not filtered:
        return candidates  # fall back if over-filtered

    # Prefer standard / hot tier rows per provider
    if provider == CloudProvider.AWS:
        standard = [
            c for c in filtered
            if "standardstorage" in c.sku_name.lower()
            or "standard" in c.sku_name.lower()
        ]
        if standard:
            return standard
    elif provider == CloudProvider.AZURE:
        hot = [
            c for c in filtered
            if "hot" in c.sku_name.lower() or "lrs" in c.sku_name.lower()
        ]
        if hot:
            return hot
    elif provider == CloudProvider.GCP:
        standard = [c for c in filtered if "standard" in c.sku_name.lower()]
        if standard:
            return standard

    return filtered


# ---------------------------------------------------------------------------
# Sizing strategies per category
# ---------------------------------------------------------------------------


@observe()
async def _size_compute_workload(
    component: ComponentProfile,
    original_workload: WorkloadRequirement,
    candidates: list[NormalizedPriceItem],
    provider: CloudProvider,
) -> SizedWorkloadResult:
    """Size a COMPUTE or AI_ML workload using the scoring engine.

    Args:
        component: Profiler's analysis.
        original_workload: Original workload requirement.
        candidates: Candidate SKUs from PricingService.
        provider: Target cloud provider.

    Returns:
        SizedWorkloadResult with scored best-fit SKU.
    """
    log = logger.bind(
        agent="sizer",
        step="size_compute",
        workload=component.workload_name,
        provider=provider.value,
    )
    log.info("scoring_started", candidate_count=len(candidates))

    # Build a WorkloadRequirement with profiler-enriched values
    enriched_wl = _build_workload_requirement_for_component(component, original_workload)

    scored: list[ScoredSKU] = score_skus(enriched_wl, candidates)

    if not scored:
        log.warning("no_eligible_skus_after_scoring")
        # Fall back to cheapest
        best, alts = _select_best_by_price(candidates)
        return SizedWorkloadResult(
            workload_name=component.workload_name,
            provider=provider,
            selected_sku=best,
            alternative_skus=alts,
            monthly_cost_usd=(best.monthly_cost_estimate or 0.0) if best else 0.0,
            fit_score=0.3,
            rationale=(
                f"No SKU passed scoring filters for {component.workload_name}. "
                f"Selected cheapest available option."
            ),
        )

    best_scored = scored[0]
    alternatives = [s.sku for s in scored[1: 1 + _MAX_ALTERNATIVES]]

    monthly = best_scored.sku.monthly_cost_estimate or 0.0

    log.info(
        "scoring_completed",
        best_sku=best_scored.sku.sku_name,
        fit_score=round(best_scored.total_score, 4),
        monthly_cost=round(monthly, 2),
        eligible_count=len(scored),
    )

    return SizedWorkloadResult(
        workload_name=component.workload_name,
        provider=provider,
        selected_sku=best_scored.sku,
        alternative_skus=alternatives,
        monthly_cost_usd=round(monthly, 2),
        fit_score=round(best_scored.total_score, 4),
        rationale=(
            f"Scored {len(scored)} eligible SKUs. "
            f"Best fit: {best_scored.sku.sku_name} "
            f"(cost={best_scored.cost_score:.2f}, "
            f"cpu_fit={best_scored.cpu_fit_score:.2f}, "
            f"mem_fit={best_scored.memory_fit_score:.2f}, "
            f"gen={best_scored.generation_score:.2f}). "
            f"Est. ${monthly:.2f}/mo."
        ),
    )


@observe()
async def _size_container_workload(
    component: ComponentProfile,
    original_workload: WorkloadRequirement,
    container_workloads: list[WorkloadRequirement],
    candidates: list[NormalizedPriceItem],
    provider: CloudProvider,
) -> tuple[SizedWorkloadResult, BinPackingResult | None]:
    """Size a CONTAINER workload using the bin-packing engine.

    Selects the best node SKU, then packs all container workloads
    onto it.

    Args:
        component: Profiler's analysis.
        original_workload: Original workload requirement.
        container_workloads: All container workloads (for packing together).
        candidates: Candidate node SKUs from PricingService.
        provider: Target cloud provider.

    Returns:
        Tuple of (SizedWorkloadResult, BinPackingResult or None).
    """
    log = logger.bind(
        agent="sizer",
        step="size_container",
        workload=component.workload_name,
        provider=provider.value,
    )
    log.info(
        "bin_packing_started",
        candidate_count=len(candidates),
        container_workload_count=len(container_workloads),
    )

    # Compute total vCPU demand across all container workloads.
    # This caps node size to prevent selecting massively oversized hosts
    # (e.g. x8i.48xlarge at 192 vCPU for a 3-vCPU workload set).
    total_needed_vcpus = 0.0
    for _wl in container_workloads:
        if _wl.resources.cpu_request_millicores:
            total_needed_vcpus += (
                (_wl.resources.cpu_request_millicores / 1000.0)
                * max(_wl.resources.replicas, 1)
            )
        elif _wl.resources.vcpus:
            total_needed_vcpus += float(_wl.resources.vcpus)
        else:
            total_needed_vcpus += 2.0
    total_needed_vcpus = max(2.0, total_needed_vcpus)
    # Reject any node with >4× the total workload vCPU demand
    max_node_vcpus = max(8, int(total_needed_vcpus * 4))

    # Select node SKU: pick a right-sized general-purpose node
    viable_nodes = [
        c for c in candidates
        if 2 <= extract_vcpus(c) <= max_node_vcpus
        and extract_memory_gb(c) >= 4.0
        and c.monthly_cost_estimate is not None
        and c.monthly_cost_estimate > 0
    ]

    if not viable_nodes:
        log.warning("no_viable_node_skus")
        best, alts = _select_best_by_price(candidates)
        return (
            SizedWorkloadResult(
                workload_name=component.workload_name,
                provider=provider,
                selected_sku=best,
                alternative_skus=alts,
                monthly_cost_usd=(best.monthly_cost_estimate or 0.0) if best else 0.0,
                fit_score=0.2,
                rationale="No viable node SKUs found for container packing.",
            ),
            None,
        )

    # Sort: general-purpose families first, then by cost-efficiency.
    # Preferred families (m5/m6i/c5 etc.) are right-sized for microservices.
    def cost_efficiency(sku: NormalizedPriceItem) -> float:
        vcpus = max(extract_vcpus(sku), 1)
        mem = max(extract_memory_gb(sku), 1.0)
        cost = sku.monthly_cost_estimate or float("inf")
        return cost / (vcpus * mem)

    def _sort_key(sku: NormalizedPriceItem) -> tuple:
        preferred = _PREFERRED_CONTAINER_FAMILIES.get(provider.value, ())
        is_not_preferred = 0 if any(
            sku.sku_name.startswith(p) or p in sku.sku_name
            for p in preferred
        ) else 1
        return (is_not_preferred, cost_efficiency(sku))

    viable_nodes.sort(key=_sort_key)
    best_node = viable_nodes[0]
    alt_nodes = viable_nodes[1: 1 + _MAX_ALTERNATIVES]

    # Pack all container workloads onto selected node type
    packing_result = pack_workloads(
        workloads=container_workloads,
        node_sku=best_node,
        algorithm=PackingAlgorithm.BEST_FIT_DECREASING,
        pool_name=f"{provider.value}-pool",
    )

    monthly = packing_result.total_monthly_cost_usd

    log.info(
        "bin_packing_completed",
        node_sku=best_node.sku_name,
        total_nodes=packing_result.total_nodes,
        efficiency=packing_result.packing_efficiency_pct,
        monthly_cost=round(monthly, 2),
    )

    return (
        SizedWorkloadResult(
            workload_name=component.workload_name,
            provider=provider,
            selected_sku=best_node,
            alternative_skus=alt_nodes,
            monthly_cost_usd=round(monthly, 2),
            fit_score=round(packing_result.packing_efficiency_pct / 100.0, 4),
            rationale=(
                f"{'Knative/KEDA' if component.resolved_category == ServiceCategory.SERVERLESS_COMPUTE else 'Bin-packed'} "
                f"{len(container_workloads)} workload(s) "
                f"onto {packing_result.total_nodes} × {best_node.sku_name} nodes "
                f"({'EKS+Knative' if provider.value == 'aws' else 'AKS+Knative' if provider.value == 'azure' else 'GKE+Knative'}"
                f" scale-to-zero" if component.resolved_category == ServiceCategory.SERVERLESS_COMPUTE
                else f"{'EKS' if provider.value == 'aws' else 'AKS' if provider.value == 'azure' else 'GKE'}"
                f"). "
                f"Packing efficiency: {packing_result.packing_efficiency_pct:.1f}%. "
                f"Est. ${monthly:.2f}/mo."
            ),
        ),
        packing_result,
    )


@observe()
async def _size_generic_workload(
    component: ComponentProfile,
    candidates: list[NormalizedPriceItem],
    provider: CloudProvider,
) -> SizedWorkloadResult:
    """Size a DATABASE, STORAGE, SERVERLESS, or other workload.

    Uses simple cheapest-match selection.

    Args:
        component: Profiler's analysis.
        candidates: Candidate SKUs from PricingService.
        provider: Target cloud provider.

    Returns:
        SizedWorkloadResult with cheapest matching SKU.
    """
    log = logger.bind(
        agent="sizer",
        step="size_generic",
        workload=component.workload_name,
        provider=provider.value,
        category=component.resolved_category.value,
    )
    log.info("generic_sizing_started", candidate_count=len(candidates))

    best, alts = _select_best_by_price(candidates)

    if best is None:
        log.warning("no_priced_skus_found")
        return SizedWorkloadResult(
            workload_name=component.workload_name,
            provider=provider,
            selected_sku=None,
            alternative_skus=[],
            monthly_cost_usd=0.0,
            fit_score=0.0,
            rationale=(
                f"No priced SKUs found for {component.resolved_category.value} "
                f"workload '{component.workload_name}' on {provider.value}."
            ),
        )

    monthly = best.monthly_cost_estimate or 0.0

    log.info(
        "generic_sizing_completed",
        best_sku=best.sku_name,
        monthly_cost=round(monthly, 2),
        alternatives=len(alts),
    )

    return SizedWorkloadResult(
        workload_name=component.workload_name,
        provider=provider,
        selected_sku=best,
        alternative_skus=alts,
        monthly_cost_usd=round(monthly, 2),
        fit_score=0.7,
        rationale=(
            f"Selected {best.sku_name} as cheapest option for "
            f"{component.resolved_category.value} workload "
            f"'{component.workload_name}'. Est. ${monthly:.2f}/mo."
        ),
    )


# ---------------------------------------------------------------------------
# Serverless workload sizing (Lambda + DynamoDB pattern)
# ---------------------------------------------------------------------------

#: Lambda compute price per GB-second (USD).
_LAMBDA_PRICE_PER_GB_SEC: float = 0.0000166667
#: Lambda request price per invocation (USD).
_LAMBDA_PRICE_PER_REQUEST: float = 0.0000002
#: Azure Functions consumption price per million executions (USD).
_AZURE_FUNC_PRICE_PER_MILLION_EXEC: float = 0.20
#: Azure Functions compute price per GB-second (USD).
_AZURE_FUNC_PRICE_PER_GB_SEC: float = 0.000016
#: Cloud Run request price per million requests (USD).
_CLOUDRUN_PRICE_PER_MILLION_REQ: float = 0.40
#: Cloud Run compute price per vCPU-second (USD).
_CLOUDRUN_PRICE_PER_VCPU_SEC: float = 0.00002400
#: Cloud Run memory price per GB-second (USD).
_CLOUDRUN_PRICE_PER_GB_SEC: float = 0.00000250
#: Seconds in a 30-day month.
_SECONDS_PER_MONTH: int = 2_592_000


def _estimate_serverless_monthly_cost(
    provider: CloudProvider,
    invocations_per_month: float,
    avg_duration_ms: float,
    memory_mb: float,
) -> float:
    """Estimate monthly cost for a serverless (Lambda-style) workload.

    Uses provider-specific per-invocation + compute pricing with no
    live API call — these rates are relatively stable.

    Args:
        provider: Target cloud provider.
        invocations_per_month: Expected function invocations per month.
        avg_duration_ms: Average function execution time in milliseconds.
        memory_mb: Memory allocated to the function in MB.

    Returns:
        Estimated monthly cost in USD.
    """
    mem_gb = memory_mb / 1024.0
    duration_sec = avg_duration_ms / 1000.0

    if provider == CloudProvider.AWS:
        # Lambda: request fee + GB-second compute fee
        request_cost = invocations_per_month * _LAMBDA_PRICE_PER_REQUEST
        gb_sec = invocations_per_month * duration_sec * mem_gb
        compute_cost = gb_sec * _LAMBDA_PRICE_PER_GB_SEC
        # First 1M requests/mo and 400K GB-sec/mo are free — subtract free tier
        request_cost = max(0.0, request_cost - 1_000_000 * _LAMBDA_PRICE_PER_REQUEST)
        compute_cost = max(0.0, compute_cost - 400_000 * _LAMBDA_PRICE_PER_GB_SEC)
        return round(request_cost + compute_cost, 2)

    if provider == CloudProvider.AZURE:
        # Azure Functions consumption plan
        millions_exec = invocations_per_month / 1_000_000
        exec_cost = max(0.0, millions_exec - 1.0) * _AZURE_FUNC_PRICE_PER_MILLION_EXEC
        gb_sec = invocations_per_month * duration_sec * mem_gb
        compute_cost = max(0.0, gb_sec - 400_000) * _AZURE_FUNC_PRICE_PER_GB_SEC
        return round(exec_cost + compute_cost, 2)

    if provider == CloudProvider.GCP:
        # Cloud Run: per-request + vCPU-second + GB-second fees
        vcpus = max(1.0, memory_mb / 512.0)  # typical 0.5 vCPU per 512 MB
        millions_req = invocations_per_month / 1_000_000
        req_cost = max(0.0, millions_req - 2.0) * _CLOUDRUN_PRICE_PER_MILLION_REQ
        vcpu_sec = invocations_per_month * duration_sec * vcpus
        mem_gb_sec = invocations_per_month * duration_sec * mem_gb
        compute_cost = (
            max(0.0, vcpu_sec - 180_000) * _CLOUDRUN_PRICE_PER_VCPU_SEC
            + max(0.0, mem_gb_sec - 360_000) * _CLOUDRUN_PRICE_PER_GB_SEC
        )
        return round(req_cost + compute_cost, 2)

    # Fallback: zero (unknown provider)
    return 0.0


@observe()
async def _size_serverless_workload(
    component: ComponentProfile,
    original_workload: WorkloadRequirement,
    provider: CloudProvider,
) -> SizedWorkloadResult:
    """Estimate cost for a SERVERLESS workload using consumption pricing.

    Computes Lambda / Azure Functions / Cloud Run monthly cost from the
    workload's ``invocations_per_month``, ``avg_duration_ms``, and
    ``memory_mb`` resource fields.  No live API call is made — these
    rates are stable enough for estimation purposes.

    Args:
        component: Profiler's component analysis.
        original_workload: Original workload requirement with resource fields.
        provider: Target cloud provider.

    Returns:
        SizedWorkloadResult with usage-based cost estimate.
    """
    log = logger.bind(
        agent="sizer",
        step="size_serverless",
        workload=component.workload_name,
        provider=provider.value,
    )
    log.info("serverless_sizing_started")

    res = original_workload.resources
    invocations = float(res.invocations_per_month or 10_000_000)
    avg_duration = float(res.avg_duration_ms or 200.0)
    memory_mb = float(res.memory_mb or 512.0)

    monthly = _estimate_serverless_monthly_cost(
        provider, invocations, avg_duration, memory_mb,
    )

    log.info(
        "serverless_sizing_completed",
        invocations=invocations,
        avg_duration_ms=avg_duration,
        memory_mb=memory_mb,
        monthly_cost=monthly,
    )

    provider_stack = {
        "aws": "Lambda + DynamoDB + API Gateway",
        "azure": "Azure Functions + Cosmos DB + APIM",
        "gcp": "Cloud Run + Firestore + API Gateway",
    }.get(provider.value, "Serverless Functions")

    return SizedWorkloadResult(
        workload_name=component.workload_name,
        provider=provider,
        selected_sku=None,
        alternative_skus=[],
        monthly_cost_usd=monthly,
        fit_score=0.85,
        rationale=(
            f"Serverless pricing estimate ({provider_stack}): "
            f"{invocations:,.0f} invocations/mo × {avg_duration:.0f}ms avg "
            f"@ {memory_mb:.0f}MB → ${monthly:.2f}/mo. "
            f"Scales to zero; no idle costs."
        ),
    )


# ---------------------------------------------------------------------------
# LLM-assisted rationale enhancement
# ---------------------------------------------------------------------------

_SIZER_SYSTEM_PROMPT = """\
You are a cloud infrastructure sizing expert. Given a set of workload \
sizing results, provide a brief overall summary that:

1. Highlights key sizing decisions and trade-offs
2. Notes any workloads where the fit score is low (<0.5)
3. Suggests potential optimizations (e.g. reserved instances, spot)

Respond in 3-5 sentences. Be concise and technical.
"""


@observe()
async def _generate_sizer_summary(
    llm: BaseChatModel,
    results: list[SizedWorkloadResult],
    workload_profile: WorkloadProfile,
) -> str:
    """Use the LLM to generate an overall sizing summary.

    Args:
        llm: The LLM model (BaseChatModel interface).
        results: All sizing results produced.
        workload_profile: The profiler's workload analysis.

    Returns:
        LLM-generated summary string.
    """
    log = logger.bind(agent="sizer", step="generate_summary")
    log.debug("summary_generation_started")

    # Build a concise input for the LLM
    result_summaries = []
    for r in results:
        sku_name = r.selected_sku.sku_name if r.selected_sku else "none"
        result_summaries.append({
            "workload": r.workload_name,
            "provider": r.provider.value,
            "sku": sku_name,
            "monthly_cost": r.monthly_cost_usd,
            "fit_score": r.fit_score,
        })

    user_content = json.dumps({
        "environment": workload_profile.environment.value,
        "tier": workload_profile.tier.value,
        "total_vcpus": workload_profile.total_vcpus,
        "total_memory_gb": workload_profile.total_memory_gb,
        "total_components": len(workload_profile.components),
        "sizing_results": result_summaries,
    }, indent=2)

    messages = [
        SystemMessage(content=_SIZER_SYSTEM_PROMPT),
        HumanMessage(content=f"Summarize these sizing results:\n\n{user_content}"),
    ]

    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, "content") else str(response)
        log.debug("summary_generation_completed", length=len(content))
        return content[:800]
    except Exception:
        log.warning("summary_generation_failed", exc_info=True)
        # Heuristic fallback
        total_cost = sum(r.monthly_cost_usd for r in results)
        providers_used = {r.provider.value for r in results}
        low_fit = [r for r in results if r.fit_score < 0.5]
        parts = [
            f"Sized {len(results)} workload-provider combinations "
            f"across {', '.join(sorted(providers_used))}.",
            f"Total estimated cost: ${total_cost:.2f}/mo.",
        ]
        if low_fit:
            names = ", ".join(r.workload_name for r in low_fit)
            parts.append(f"Low fit scores for: {names} — review SKU availability.")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------


async def run_sizer_node(
    state: OrchestratorState,
    llm: BaseChatModel,
    pricing_service: PricingService,
) -> OrchestratorState:
    """Execute the Sizer agent — select SKUs and size node pools.

    This is a LangGraph node function.  It reads
    ``state['workload_profile']`` and ``state['workload_request']``
    and produces ``state['sized_results']`` with one
    ``SizedWorkloadResult`` per component per provider.

    Args:
        state: Current ``OrchestratorState`` (TypedDict).
        llm: LLM instance for generating summaries (``BaseChatModel``
             interface — never import a provider-specific class here).
        pricing_service: Initialised ``PricingService`` with providers
            registered.

    Returns:
        Updated ``OrchestratorState`` with ``sized_results`` populated
        and a summary message appended to ``messages``.

    Raises:
        ValueError: If ``workload_profile`` or ``workload_request``
            is missing from state.
    """
    log = logger.bind(
        agent="sizer",
        request_id=state.get("request_id", "unknown"),
    )

    start_time = datetime.now(timezone.utc)
    log.info("sizer_node_started")

    try:
        # ── Validate inputs ───────────────────────────────────
        workload_profile: WorkloadProfile | None = state.get("workload_profile")
        if workload_profile is None:
            raise ValueError(
                "workload_profile is missing from state — Profiler must run first"
            )

        workload_request: WorkloadRequest | None = state.get("workload_request")
        if workload_request is None:
            raise ValueError(
                "workload_request is missing from state — Clarifier must run first"
            )

        if not workload_profile.components:
            raise ValueError(
                "workload_profile has no components — Profiler produced empty output"
            )

        target_providers = workload_request.target_providers

        log.info(
            "sizing_workloads",
            project=workload_request.project_name,
            component_count=len(workload_profile.components),
            target_providers=[p.value for p in target_providers],
        )

        # ── Build a name → WorkloadRequirement lookup ─────────
        workload_lookup: dict[str, WorkloadRequirement] = {
            wl.name: wl for wl in workload_request.workloads
        }

        # ── Identify container workloads for combined packing ─
        container_components = [
            c for c in workload_profile.components
            if c.resolved_category in _BINPACKED_CATEGORIES
        ]
        container_workloads = [
            workload_lookup[c.workload_name]
            for c in container_components
            if c.workload_name in workload_lookup
        ]

        # Track which container workloads have been bin-packed
        # (to avoid duplicate sizing)
        container_packed_for: set[tuple[str, str]] = set()  # (workload_name, provider)

        # ── Size each component × provider ────────────────────
        all_results: list[SizedWorkloadResult] = []
        bin_packing_results: dict[str, BinPackingResult] = {}

        for component in workload_profile.components:
            original_wl = workload_lookup.get(component.workload_name)
            if original_wl is None:
                log.warning(
                    "workload_not_found_in_request",
                    workload_name=component.workload_name,
                )
                continue

            for provider in target_providers:
                # Skip if locked to a different provider
                if (
                    original_wl.provider_preference is not None
                    and original_wl.provider_preference != provider
                ):
                    log.debug(
                        "skipping_provider_preference",
                        workload=component.workload_name,
                        provider=provider.value,
                        preferred=original_wl.provider_preference.value,
                    )
                    continue

                comp_log = log.bind(
                    workload=component.workload_name,
                    provider=provider.value,
                    category=component.resolved_category.value,
                )
                comp_log.info("sizing_component_started")

                # ── Fixed-cost workloads (no SKU lookup needed) ──
                fixed_type = _is_fixed_cost_workload(original_wl)
                if fixed_type == "cluster_management_fee":
                    fee = _K8S_CLUSTER_FEE_MONTHLY.get(provider.value, 73.0)
                    all_results.append(
                        SizedWorkloadResult(
                            workload_name=component.workload_name,
                            provider=provider,
                            selected_sku=None,
                            alternative_skus=[],
                            monthly_cost_usd=fee,
                            fit_score=1.0,
                            rationale=(
                                f"K8s cluster management fee: "
                                f"${fee:.2f}/mo on {provider.value}."
                            ),
                        )
                    )
                    comp_log.info("management_fee_added", fee=fee)
                    continue

                if fixed_type == "load_balancer":
                    fee = _LOAD_BALANCER_FEE_MONTHLY.get(provider.value, 20.0)
                    all_results.append(
                        SizedWorkloadResult(
                            workload_name=component.workload_name,
                            provider=provider,
                            selected_sku=None,
                            alternative_skus=[],
                            monthly_cost_usd=fee,
                            fit_score=1.0,
                            rationale=(
                                f"Application load balancer base cost: "
                                f"${fee:.2f}/mo on {provider.value}."
                            ),
                        )
                    )
                    comp_log.info("load_balancer_fee_added", fee=fee)
                    continue

                # ── CDN workloads: fixed-cost estimate ────────
                # CloudFront / Azure CDN / Cloud CDN are usage-based (per-GB,
                # per-request) — no flat instance SKU to query.
                if _is_cdn_workload(original_wl):
                    fee = _CDN_COST_MONTHLY.get(provider.value, 80.0)
                    all_results.append(
                        SizedWorkloadResult(
                            workload_name=component.workload_name,
                            provider=provider,
                            selected_sku=None,
                            alternative_skus=[],
                            monthly_cost_usd=fee,
                            fit_score=1.0,
                            rationale=(
                                f"CDN/Edge Delivery estimated cost: "
                                f"${fee:.2f}/mo on {provider.value} "
                                f"(~500 GB/mo transfer estimate)."
                            ),
                        )
                    )
                    comp_log.info("cdn_fixed_cost_added", fee=fee)
                    continue

                # ── Fetch candidate SKUs ──────────────────────
                region = _get_region_for_provider(
                    provider, workload_request, original_wl,
                )
                service_name = _SERVICE_NAME_MAP.get(
                    (provider.value, component.resolved_category),
                )
                sku_name_filter: str | None = None

                # Database engine propagation
                if component.resolved_category == ServiceCategory.DATABASE:
                    svc_override, sku_override = _get_db_search_params(
                        provider, original_wl,
                    )
                    if svc_override:
                        service_name = svc_override
                        comp_log.debug(
                            "db_service_override",
                            service_name=svc_override,
                        )
                    if sku_override:
                        sku_name_filter = sku_override
                        comp_log.debug(
                            "db_sku_filter",
                            sku_name=sku_override,
                        )

                # Container workloads → query VM SKUs for node pools
                search_category = component.resolved_category
                if component.resolved_category in _BINPACKED_CATEGORIES:
                    service_name = _NODE_POOL_VM_SERVICE.get(
                        provider.value, service_name,
                    )
                    search_category = ServiceCategory.COMPUTE
                    comp_log.debug(
                        "container_vm_service_override",
                        service_name=service_name,
                    )

                try:
                    candidates = await pricing_service.search_prices(
                        provider,
                        service_name=service_name,
                        service_category=search_category,
                        region=region,
                        pricing_tier=PricingTier.ON_DEMAND,
                        sku_name=sku_name_filter,
                        max_results=100,
                    )
                except Exception:
                    comp_log.error("pricing_search_failed", exc_info=True)
                    all_results.append(
                        SizedWorkloadResult(
                            workload_name=component.workload_name,
                            provider=provider,
                            selected_sku=None,
                            alternative_skus=[],
                            monthly_cost_usd=0.0,
                            fit_score=0.0,
                            rationale=(
                                f"Pricing search failed for "
                                f"{component.workload_name} on {provider.value}."
                            ),
                        )
                    )
                    continue

                comp_log.info(
                    "candidates_fetched",
                    count=len(candidates),
                    region=region,
                )

                # ── Enrich candidates for VM-based workloads ──
                # Both scored (COMPUTE/AI_ML) and binpacked (CONTAINER)
                # categories need real VM SKUs.
                _vm_categories = _SCORED_CATEGORIES | _BINPACKED_CATEGORIES
                if component.resolved_category in _vm_categories:
                    if provider == CloudProvider.GCP:
                        # GCP returns per-component pricing (per-vCPU,
                        # per-GB-RAM), not per-instance.  Inject synthetic
                        # VM instances with calculated hourly prices.
                        gcp_vms = compose_gcp_vm_instances(region=region or "us-central1")
                        candidates = gcp_vms
                        comp_log.info(
                            "gcp_synthetic_vms_injected",
                            synthetic_count=len(gcp_vms),
                        )
                    else:
                        # Filter to hourly-billed VM SKUs only (exclude
                        # storage/network meters that share the service)
                        candidates = [
                            c for c in candidates
                            if c.unit_of_measure in ("1 Hour", "1 hour")
                            and c.unit_price > 0
                        ]
                        # AWS: further exclude non-standard OS/license meters.
                        # EC2 pricing includes rows for SQL Server Enterprise,
                        # SQL Server Standard, Windows, RHEL, and "Unused
                        # Reservation" placeholders that have inflated prices.
                        # Bug 11b: `operatingSystem=Linux` filter removed SQL
                        # Enterprise (Windows/RHEL rows), but SQL Standard rows
                        # *also* report operatingSystem=Linux — they are only
                        # distinguishable by the meter_name/description field.
                        # Bug 12a fix: additionally filter on meter_name to
                        # exclude any row that bundles an OS/DB license.
                        if provider == CloudProvider.AWS:
                            _SQL_LICENSE_TOKENS = (
                                "sql std", "sql ent", "sql web",
                                "with sql", "sqlserver",
                                "windows", "rhel", "suse",
                            )
                            linux_only = [
                                c for c in candidates
                                if (
                                    c.attributes.get("operatingSystem", "Linux").lower()
                                    == "linux"
                                    and "unusedbox" not in c.attributes.get(
                                        "usagetype", ""
                                    ).lower()
                                    and "unusedded" not in c.attributes.get(
                                        "usagetype", ""
                                    ).lower()
                                    and not any(
                                        tok in (c.meter_name or "").lower()
                                        for tok in _SQL_LICENSE_TOKENS
                                    )
                                )
                            ]
                            if linux_only:
                                candidates = linux_only
                            else:
                                # No standard Linux on-demand rows in the candidate
                                # set — all non-$0 rows are non-Linux or licensed
                                # billing artifacts (UnusedBox, SQL, Windows, etc.).
                                # Clear candidates so the "no SKUs found" path fires
                                # rather than falling through to a garbage selection.
                                comp_log.warning(
                                    "no_standard_linux_rows",
                                    msg=(
                                        "All candidates are non-Linux/licensed billing "
                                        "artifacts.  Clearing to avoid garbage SKU selection."
                                    ),
                                )
                                candidates = []

                        # Exclude GPU instances for workloads that do not require a GPU.
                        # GPU SKUs (g*, p*, trn*, dl*) are order-of-magnitude more expensive
                        # than general-purpose instances and should never be selected for
                        # CPU-only workloads.
                        if provider == CloudProvider.AWS and not component.requires_gpu:
                            _GPU_PREFIXES = (
                                "g3.", "g4.", "g5.", "g6.", "g7.",
                                "p2.", "p3.", "p4.", "p5.",
                                "trn", "dl1",
                            )
                            no_gpu = [
                                c for c in candidates
                                if not any(
                                    c.sku_name.lower().startswith(pfx)
                                    for pfx in _GPU_PREFIXES
                                )
                            ]
                            if no_gpu:
                                candidates = no_gpu

                        comp_log.info(
                            "candidates_filtered_to_vms",
                            remaining=len(candidates),
                        )

                # ── Filter DATABASE to instance-hour rows only ─
                # RDS / ElastiCache pricing includes storage, IOPS, and backup
                # meters alongside instance-hour rows.  We need instance rows.
                if component.resolved_category == ServiceCategory.DATABASE:
                    hourly = [
                        c for c in candidates
                        if c.is_hourly and c.unit_price > 0
                    ]
                    if hourly:
                        candidates = hourly
                        comp_log.info(
                            "database_candidates_filtered_to_hourly",
                            remaining=len(hourly),
                        )

                    # ── Filter DATABASE to current-gen, resource-adequate SKUs ──
                    # The cheapest hourly row (e.g. db.t3.micro at $0.018/hr)
                    # may be massively undersized for the workload requirement.
                    # This filter removes deprecated instances (currentGeneration=No)
                    # and instances that cannot meet the memory/vCPU requirements.
                    candidates = _filter_database_candidates(
                        candidates,
                        required_memory_gb=component.estimated_memory_gb,
                        required_vcpus=component.estimated_vcpus,
                    )
                    comp_log.info(
                        "database_candidates_filtered_to_resource_fit",
                        required_memory_gb=component.estimated_memory_gb,
                        required_vcpus=component.estimated_vcpus,
                        remaining=len(candidates),
                    )
                    # ── Azure: exclude add-on / feature SKUs ──
                    # Azure PostgreSQL pricing includes add-on meters like
                    # "Auto Tune", "Extended Support", and "IOPS Scaling"
                    # alongside actual compute instance rows.  These are
                    # priced per-vCore at very low rates and look like the
                    # cheapest option but are not compute instances.
                    # We keep only rows whose product_name ends with "Compute"
                    # or explicitly identifies a compute tier.
                    if provider == CloudProvider.AZURE:
                        _AZURE_DB_ADDON_KEYWORDS = (
                            "autonomous", "auto tune", "extended support",
                            "backup", "iops", "tuning service", "throughput",
                        )
                        compute_only = [
                            c for c in candidates
                            if "compute" in c.product_name.lower()
                            and not any(
                                kw in c.product_name.lower()
                                for kw in _AZURE_DB_ADDON_KEYWORDS
                            )
                        ]
                        if compute_only:
                            candidates = compute_only
                            comp_log.info(
                                "azure_db_filtered_to_compute_skus",
                                remaining=len(candidates),
                            )

                        # ── Azure: exclude Burstable tier for production/critical ──
                        # "Burstable Compute" is the Azure equivalent of the "Basic"
                        # tier — it is designed for dev/test, not production workloads.
                        # For business-critical / mission-critical tiers, force
                        # General Purpose or Business Critical (Flexible Server).
                        is_production_critical = workload_profile.tier in (
                            WorkloadTier.BUSINESS_CRITICAL,
                            WorkloadTier.MISSION_CRITICAL,
                        )
                        if is_production_critical:
                            non_burstable = [
                                c for c in candidates
                                if "burstable" not in (c.sku_name + " " + c.product_name).lower()
                                and "basic" not in (c.sku_name + " " + c.product_name).lower()
                            ]
                            if non_burstable:
                                candidates = non_burstable
                                comp_log.info(
                                    "azure_db_filtered_burstable_excluded",
                                    remaining=len(candidates),
                                    reason="production_critical_tier",
                                )

                    # ── GCP: exclude IP address reservation rows ──
                    # Cloud SQL pricing includes a static-IP reservation row
                    # (~$0.01/hr = $7.30/mo) that looks like a cheap database
                    # SKU but is only a network fee, not a compute instance.
                    # Filter these out so the cheapest-picker selects a real
                    # Cloud SQL instance SKU instead.
                    if provider == CloudProvider.GCP:
                        _GCP_IP_PATTERNS = (
                            "ip address", "ipaddress", "ip-address",
                            "static ip", "network ip",
                        )
                        non_ip = [
                            c for c in candidates
                            if not any(
                                pat in (c.product_name + " " + c.sku_name).lower()
                                for pat in _GCP_IP_PATTERNS
                            )
                        ]
                        if non_ip:
                            candidates = non_ip
                            comp_log.info(
                                "gcp_db_filtered_ip_reservation_rows",
                                remaining=len(non_ip),
                            )


                # ── Filter STORAGE to standard-tier rows only ──
                # S3 / Blob / GCS return archival, Glacier early-delete,
                # and retrieval-fee rows that have misleadingly low prices.
                if component.resolved_category == ServiceCategory.STORAGE:
                    candidates = _filter_storage_candidates(candidates, provider)
                    comp_log.info(
                        "storage_candidates_filtered",
                        remaining=len(candidates),
                    )

                if not candidates:
                    # Apply fixed-cost fallback for Redis/Memcached when SKU lookup fails
                    # (Azure Cache for Redis and GCP Memorystore are not always returned
                    # by the pricing API for all regions).
                    if (
                        component.resolved_category == ServiceCategory.DATABASE
                        and _is_redis_workload(original_wl)
                    ):
                        fee = _REDIS_CACHE_COST_MONTHLY.get(provider.value, 55.0)
                        comp_log.info(
                            "redis_fixed_cost_fallback",
                            fee=fee,
                            reason="no_candidates_from_pricing_api",
                        )
                        all_results.append(
                            SizedWorkloadResult(
                                workload_name=component.workload_name,
                                provider=provider,
                                selected_sku=None,
                                alternative_skus=[],
                                monthly_cost_usd=fee,
                                fit_score=0.8,
                                rationale=(
                                    f"Fixed-cost estimate for managed Redis/Memcached "
                                    f"on {provider.value}: ${fee:.2f}/mo (pricing API "
                                    f"returned no SKUs for region {region})."
                                ),
                            )
                        )
                        continue

                    # Apply fixed-cost fallback for PostgreSQL/relational databases on AWS.
                    # The AWS Bulk Pricing API does not index RDS SKUs for all regions,
                    # causing zero candidates for PostgreSQL workloads.  Report a realistic
                    # baseline (Multi-AZ db.t3.medium) rather than $0.
                    if (
                        component.resolved_category == ServiceCategory.DATABASE
                        and _is_postgres_workload(original_wl)
                        and provider == CloudProvider.AWS
                    ):
                        fee = _RDS_COST_MONTHLY.get(provider.value, 185.0)
                        comp_log.info(
                            "rds_fixed_cost_fallback",
                            fee=fee,
                            reason="no_rds_candidates_from_pricing_api",
                        )
                        all_results.append(
                            SizedWorkloadResult(
                                workload_name=component.workload_name,
                                provider=provider,
                                selected_sku=None,
                                alternative_skus=[],
                                monthly_cost_usd=fee,
                                fit_score=0.7,
                                rationale=(
                                    f"Fixed-cost estimate for managed PostgreSQL (RDS) "
                                    f"on AWS: ${fee:.2f}/mo — Multi-AZ db.t3.medium "
                                    f"baseline (pricing API returned no RDS SKUs for "
                                    f"region {region})."
                                ),
                            )
                        )
                        continue

                    comp_log.warning("no_candidates_found")
                    all_results.append(
                        SizedWorkloadResult(
                            workload_name=component.workload_name,
                            provider=provider,
                            selected_sku=None,
                            alternative_skus=[],
                            monthly_cost_usd=0.0,
                            fit_score=0.0,
                            rationale=(
                                f"No SKU candidates found for "
                                f"{component.resolved_category.value} on "
                                f"{provider.value} in {region}."
                            ),
                        )
                    )
                    continue

                # ── Route to appropriate sizing strategy ──────
                if component.resolved_category in _SCORED_CATEGORIES:
                    # ── Cap vCPUs at 4× requested to prevent massively oversized SKUs ──
                    # The scoring engine ranks by cost + fit, but if the candidate pool
                    # contains only very large instances (e.g. c8a.8xlarge = 32 vCPU for
                    # a 6-vCPU workload), the least-bad oversized instance wins and inflates
                    # cost 5×.  Pre-filter to a reasonable ceiling so a genuinely well-fitting
                    # instance in the 8–16 vCPU range (if available) is preferred.
                    req_vcpu = component.estimated_vcpus or 0
                    if req_vcpu > 0:
                        max_vcpu = max(8, int(req_vcpu * 4))
                        vcpu_capped = [
                            c for c in candidates
                            if extract_vcpus(c) <= max_vcpu or extract_vcpus(c) == 0
                        ]
                        if vcpu_capped:
                            candidates = vcpu_capped
                            comp_log.info(
                                "compute_candidates_vcpu_capped",
                                req_vcpu=req_vcpu,
                                max_vcpu=max_vcpu,
                                remaining=len(vcpu_capped),
                            )
                    result = await _size_compute_workload(
                        component, original_wl, candidates, provider,
                    )
                    all_results.append(result)

                elif component.resolved_category == ServiceCategory.SERVERLESS:
                    result = await _size_serverless_workload(
                        component, original_wl, provider,
                    )
                    all_results.append(result)

                elif component.resolved_category in _BINPACKED_CATEGORIES:
                    pack_key = (component.workload_name, provider.value)
                    if pack_key in container_packed_for:
                        comp_log.debug("already_packed")
                        continue

                    result, packing = await _size_container_workload(
                        component,
                        original_wl,
                        container_workloads,
                        candidates,
                        provider,
                    )
                    all_results.append(result)

                    if packing is not None:
                        bin_packing_results[provider.value] = packing

                    # Mark all container workloads as packed for this provider
                    for cw in container_workloads:
                        container_packed_for.add((cw.name, provider.value))

                else:
                    result = await _size_generic_workload(
                        component, candidates, provider,
                    )
                    # ── STORAGE: multiply per-GB price by storage_gb ──
                    # The generic sizer returns ``unit_price × 1`` for
                    # GB-month priced items.  We need to scale by the
                    # actual storage volume from the workload requirement.
                    # Guard: only apply scaling when the selected SKU is
                    # truly per-GB capacity pricing (unit_of_measure contains
                    # "gb" or "gib").  Per-IOPS, per-disk, and per-request
                    # SKUs must not be multiplied by storage_gb.
                    _sku = result.selected_sku
                    _is_per_gb = _sku is not None and any(
                        u in _sku.unit_of_measure.lower() for u in ("gb", "gib")
                    )
                    if (
                        component.resolved_category == ServiceCategory.STORAGE
                        and _sku is not None
                        and _is_per_gb
                        and (_sku.is_monthly or _sku.is_hourly)
                        and _sku.unit_price > 0
                    ):
                        storage_gb = original_wl.resources.storage_gb or 100.0
                        per_gb = _sku.unit_price if _sku.is_monthly else round(_sku.unit_price * 730, 6)
                        monthly = round(per_gb * storage_gb, 2)
                        result = result.model_copy(update={
                            "monthly_cost_usd": monthly,
                            "rationale": (
                                f"Selected {_sku.sku_name} at "
                                f"${per_gb:.4f}/GB-Month. "
                                f"Est. ${monthly:.2f}/mo for "
                                f"{storage_gb:.0f} GB storage."
                            ),
                        })
                        comp_log.info(
                            "storage_cost_scaled",
                            storage_gb=storage_gb,
                            per_gb=per_gb,
                            monthly_cost=monthly,
                        )
                    all_results.append(result)

                comp_log.info(
                    "sizing_component_completed",
                    selected_sku=(
                        result.selected_sku.sku_name
                        if result.selected_sku else "none"
                    ),
                    fit_score=result.fit_score,
                    monthly_cost=result.monthly_cost_usd,
                )

        # ── Add ancillary infrastructure cost estimates ─────────
        has_containers = any(
            c.resolved_category in _BINPACKED_CATEGORIES
            for c in workload_profile.components
        )
        ancillary = _build_ancillary_results(target_providers, has_containers)
        all_results.extend(ancillary)
        if ancillary:
            log.info(
                "ancillary_costs_added",
                count=len(ancillary),
                total=round(sum(a.monthly_cost_usd for a in ancillary), 2),
            )

        # ── Generate LLM summary ─────────────────────────────
        sizer_summary = await _generate_sizer_summary(
            llm, all_results, workload_profile,
        )

        # ── Build summary message ─────────────────────────────
        total_cost_by_provider: dict[str, float] = {}
        for r in all_results:
            prov = r.provider.value
            total_cost_by_provider[prov] = (
                total_cost_by_provider.get(prov, 0.0) + r.monthly_cost_usd
            )

        cost_lines = "\n".join(
            f"  • **{prov}**: ${cost:.2f}/mo"
            for prov, cost in sorted(total_cost_by_provider.items())
        )

        result_lines = "\n".join(
            f"  • {r.workload_name} ({r.provider.value}): "
            f"{r.selected_sku.sku_name if r.selected_sku else 'N/A'} — "
            f"${r.monthly_cost_usd:.2f}/mo (fit: {r.fit_score:.2f})"
            for r in all_results
        )

        summary_content = (
            f"**Sizer Analysis Complete** — "
            f"{len(all_results)} workload-provider combinations sized:\n"
            f"{result_lines}\n\n"
            f"**Estimated monthly costs by provider:**\n{cost_lines}\n\n"
            f"{sizer_summary}"
        )

        summary_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=summary_content,
            agent_name="sizer",
            metadata={
                "result_count": len(all_results),
                "cost_by_provider": total_cost_by_provider,
                "bin_packing_providers": list(bin_packing_results.keys()),
            },
        )

        # ── Compute timing ────────────────────────────────────
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        execution = AgentExecution(
            agent_name="sizer",
            status=AgentStatus.COMPLETED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
        )

        log.info(
            "sizer_node_completed",
            result_count=len(all_results),
            cost_by_provider=total_cost_by_provider,
            duration_ms=round(duration_ms, 1),
        )

        # ── Return state update ───────────────────────────────
        return {
            "sized_results": all_results,
            "messages": [summary_message],
            "current_agent": "sizer",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "sizer": execution,
            },
        }

    except Exception:
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        log.error("sizer_node_failed", exc_info=True, duration_ms=round(duration_ms, 1))

        execution = AgentExecution(
            agent_name="sizer",
            status=AgentStatus.FAILED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
            error_message=str(Exception),
        )

        error_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="**Sizer Error** — Failed to complete SKU sizing. Check logs for details.",
            agent_name="sizer",
            metadata={"error": True},
        )

        return {
            "sized_results": [],
            "messages": [error_message],
            "current_agent": "sizer",
            "error": f"Sizer failed: {Exception}",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "sizer": execution,
            },
        }
