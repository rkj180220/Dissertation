"""Workload requirement models.

Defines the input schemas that describe an organisation's cloud
infrastructure needs.  The models are **service-category-agnostic**:
instead of separate classes for VMs vs. containers vs. databases,
a single ``WorkloadRequirement`` captures what the user *needs*
(e.g. "4 vCPUs, 16 GB RAM, PostgreSQL-compatible database") and
lets the downstream Profiler and Sizer agents decide *which*
cloud service category best fits.

Hierarchy::

    WorkloadRequest             ← top-level user submission
      └── WorkloadRequirement[] ← one per logical component
              └── ResourceSpec  ← concrete resource needs

    WorkloadProfile             ← aggregated analysis (Profiler output)
      └── ComponentProfile[]

Legacy models (``VMWorkload``, ``ContainerWorkload``,
``StorageRequirement``) are retained at the bottom of this file for
backward compatibility with the old ``engines/`` layer.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.models.cloud_resource import CloudProvider, ServiceCategory


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnvironmentType(str, Enum):
    """Deployment environment classification."""

    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"
    DR = "disaster_recovery"


class WorkloadTier(str, Enum):
    """Workload criticality — influences HA, redundancy, and WAF constraints."""

    MISSION_CRITICAL = "mission_critical"
    BUSINESS_CRITICAL = "business_critical"
    NON_CRITICAL = "non_critical"


class ScalingPattern(str, Enum):
    """Expected traffic / usage pattern for capacity planning."""

    STEADY = "steady"
    """Constant load — suits reserved instances."""

    BURSTY = "bursty"
    """Periodic spikes — suits auto-scaling / spot."""

    GROWING = "growing"
    """Gradual ramp-up — suits savings plans."""

    UNPREDICTABLE = "unpredictable"
    """Highly variable — suits on-demand / serverless."""

    BATCH = "batch"
    """Periodic batch jobs — suits spot / preemptible."""


# ---------------------------------------------------------------------------
# Resource specification (generic, category-agnostic)
# ---------------------------------------------------------------------------


class ResourceSpec(BaseModel):
    """Concrete resource requirements for a single workload component.

    Not all fields are relevant for every ``ServiceCategory`` — agents
    inspect the parent ``WorkloadRequirement.suggested_category`` to
    decide which fields matter.

    Examples:
        Compute VM::

            ResourceSpec(vcpus=4, memory_gb=16, storage_gb=100,
                         os="linux", gpu_count=0)

        Managed Database::

            ResourceSpec(vcpus=2, memory_gb=8, storage_gb=500,
                         database_engine="postgresql", iops=3000)

        Object Storage::

            ResourceSpec(storage_gb=5000, storage_type="object",
                         redundancy="geo")
    """

    # --- Compute ---
    vcpus: int | None = Field(default=None, ge=1, description="Required vCPU count")
    memory_gb: float | None = Field(default=None, gt=0, description="Required RAM in GiB")
    gpu_count: int = Field(default=0, ge=0, description="Number of GPUs required")
    gpu_type: str | None = Field(default=None, description="GPU model hint (e.g. 'A100')")
    architecture: str = Field(
        default="x86_64",
        description="CPU architecture: x86_64 | arm64",
    )
    os: str = Field(default="linux", description="Operating system: linux | windows")

    # --- Storage ---
    storage_gb: float | None = Field(default=None, ge=0, description="Storage capacity in GiB")
    storage_type: str | None = Field(
        default=None,
        description="ssd | hdd | nvme | object | archive",
    )
    iops: int | None = Field(default=None, ge=0, description="Required IOPS (block storage)")
    throughput_mbps: float | None = Field(
        default=None, ge=0, description="Required throughput in MiB/s",
    )
    redundancy: str | None = Field(
        default=None,
        description="local | zone | geo (storage redundancy tier)",
    )

    # --- Database ---
    database_engine: str | None = Field(
        default=None,
        description="Engine hint: postgresql | mysql | mongodb | redis | dynamodb",
    )
    database_version: str | None = Field(default=None, description="Engine version hint")
    high_availability: bool = Field(
        default=False,
        description="Requires HA / multi-AZ deployment",
    )
    read_replicas: int = Field(default=0, ge=0, description="Number of read replicas")

    # --- Container / Kubernetes ---
    cpu_request_millicores: int | None = Field(
        default=None, ge=50, description="K8s CPU request in millicores",
    )
    cpu_limit_millicores: int | None = Field(
        default=None, ge=50, description="K8s CPU limit in millicores",
    )
    memory_request_mb: int | None = Field(
        default=None, ge=64, description="K8s memory request in MiB",
    )
    memory_limit_mb: int | None = Field(
        default=None, ge=64, description="K8s memory limit in MiB",
    )
    replicas: int = Field(default=1, ge=1, description="Desired replica count")

    # --- Networking ---
    network_bandwidth_gbps: float | None = Field(
        default=None, ge=0, description="Required network bandwidth in Gbps",
    )
    public_endpoint: bool = Field(default=False, description="Requires a public IP / endpoint")

    # --- Serverless ---
    invocations_per_month: int | None = Field(
        default=None, ge=0, description="Expected monthly invocations (Lambda / Functions)",
    )
    avg_duration_ms: int | None = Field(
        default=None, ge=0, description="Average execution duration in ms",
    )
    memory_mb: int | None = Field(
        default=None, ge=128, description="Function memory allocation in MB",
    )


# ---------------------------------------------------------------------------
# Workload requirement (one per logical component)
# ---------------------------------------------------------------------------


class WorkloadRequirement(BaseModel):
    """A single logical component of the user's cloud workload.

    One ``WorkloadRequirement`` represents one piece of the puzzle —
    e.g. "API gateway", "PostgreSQL database", "ML training pipeline".
    The ``suggested_category`` is a *hint* from the Clarifier/Profiler;
    the Sizer may override it if a better fit is found.
    """

    name: str = Field(..., description="Descriptive name (e.g. 'API Gateway')")
    description: str = Field(
        default="",
        description="Free-text description of what this component does",
    )
    suggested_category: ServiceCategory = Field(
        default=ServiceCategory.COMPUTE,
        description="Best-guess service category (may be refined by agents)",
    )
    scaling_pattern: ScalingPattern = Field(
        default=ScalingPattern.STEADY,
        description="Expected usage pattern for capacity planning",
    )
    count: int = Field(default=1, ge=1, description="Number of identical instances")

    resources: ResourceSpec = Field(
        default_factory=ResourceSpec,
        description="Concrete resource needs for this component",
    )

    # --- Constraints ---
    region_affinity: str | None = Field(
        default=None,
        description="Preferred region (overrides top-level preference)",
    )
    provider_preference: CloudProvider | None = Field(
        default=None,
        description="Lock this component to a specific provider",
    )
    compliance_tags: list[str] = Field(
        default_factory=list,
        description="E.g. ['hipaa', 'pci-dss', 'sox']",
    )
    notes: str = Field(
        default="",
        description="Free-text notes from the user or Clarifier agent",
    )

    # --- SLA & Performance ---
    latency_p99_ms: int | None = Field(
        default=None,
        ge=0,
        description="P99 latency target in milliseconds",
    )
    throughput_rps: int | None = Field(
        default=None,
        ge=0,
        description="Required peak throughput in requests per second",
    )
    concurrent_users: int | None = Field(
        default=None,
        ge=0,
        description="Expected peak concurrent users",
    )
    uptime_sla: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Required availability SLA as a percentage (e.g. 99.9)",
    )
    rpo_minutes: int | None = Field(
        default=None,
        ge=0,
        description="Recovery Point Objective in minutes (0 = zero RPO)",
    )
    rto_minutes: int | None = Field(
        default=None,
        ge=0,
        description="Recovery Time Objective in minutes",
    )
    data_growth_rate_pct: float | None = Field(
        default=None,
        ge=0,
        description="Annual data/traffic growth rate as a percentage (used for TCO projection)",
    )
    spot_eligible: bool = Field(
        default=True,
        description=(
            "Whether this workload can tolerate spot/preemptible interruptions. "
            "Set to False to force on-demand pricing regardless of category."
        ),
    )


# ---------------------------------------------------------------------------
# Top-level request
# ---------------------------------------------------------------------------


class WorkloadRequest(BaseModel):
    """Top-level request submitted by the user via the API or dashboard.

    Contains all workload requirements plus metadata that the
    orchestrator passes through the agent pipeline.
    """

    project_name: str = Field(..., description="Customer / project identifier")
    environment: EnvironmentType = Field(default=EnvironmentType.PRODUCTION)
    tier: WorkloadTier = Field(default=WorkloadTier.BUSINESS_CRITICAL)
    target_providers: list[CloudProvider] = Field(
        default_factory=lambda: [CloudProvider.AWS, CloudProvider.AZURE, CloudProvider.GCP],
        description="Cloud providers to evaluate",
    )
    preferred_region: str = Field(
        default="us-east-1",
        description="Preferred deployment region (provider-neutral hint)",
    )
    provider_regions: dict[str, str] = Field(
        default_factory=lambda: {
            "aws": "us-east-1",
            "azure": "eastus",
            "gcp": "us-central1",
        },
        description="Provider → preferred region mapping",
    )

    workloads: list[WorkloadRequirement] = Field(
        default_factory=list,
        description="List of workload components to size and price",
    )

    budget_monthly_usd: float | None = Field(
        default=None, ge=0, description="Optional monthly budget ceiling in USD",
    )
    compliance_frameworks: list[str] = Field(
        default_factory=lambda: ["waf"],
        description="Compliance frameworks to enforce (e.g. 'waf', 'hipaa')",
    )

    # --- Free-text input (from chat) ---
    raw_user_input: str = Field(
        default="",
        description="Original unstructured text from the user's chat message",
    )


# ---------------------------------------------------------------------------
# Component profile (Profiler output per workload)
# ---------------------------------------------------------------------------


class ComponentProfile(BaseModel):
    """Profiler's analysis of a single ``WorkloadRequirement``.

    Enriches the raw user requirement with compute-level detail
    that the Sizer agent needs for SKU matching.
    """

    workload_name: str = Field(description="References WorkloadRequirement.name")
    resolved_category: ServiceCategory = Field(
        description="Final category after Profiler analysis",
    )
    estimated_vcpus: int = Field(default=0, ge=0)
    estimated_memory_gb: float = Field(default=0, ge=0)
    estimated_storage_gb: float = Field(default=0, ge=0)
    estimated_iops: int | None = Field(default=None, ge=0)
    requires_gpu: bool = Field(default=False)
    recommended_instance_families: list[str] = Field(
        default_factory=list,
        description="E.g. ['m5', 'Standard_D', 'n2-standard']",
    )
    rationale: str = Field(
        default="",
        description="LLM-generated reasoning for this profile",
    )


class WorkloadProfile(BaseModel):
    """Aggregated workload analysis produced by the Profiler Agent.

    Contains both per-component profiles and rolled-up totals
    that downstream agents use for sizing and cost estimation.
    """

    components: list[ComponentProfile] = Field(
        default_factory=list,
        description="Per-workload analysis",
    )

    # --- Aggregated totals ---
    total_vcpus: int = Field(default=0, ge=0)
    total_memory_gb: float = Field(default=0, ge=0)
    total_storage_gb: float = Field(default=0, ge=0)
    total_gpu_count: int = Field(default=0, ge=0)
    requires_gpu: bool = Field(default=False)

    # --- Context ---
    environment: EnvironmentType = Field(default=EnvironmentType.PRODUCTION)
    tier: WorkloadTier = Field(default=WorkloadTier.BUSINESS_CRITICAL)

    # --- Profiler metadata ---
    profiler_notes: str = Field(
        default="",
        description="LLM-generated summary of overall workload characteristics",
    )


# ---------------------------------------------------------------------------
# LEGACY MODELS — backward compatibility with engines/
# Will be removed when engines/ is refactored.
# ---------------------------------------------------------------------------


class VMWorkload(BaseModel):
    """A single virtual-machine workload specification.

    .. deprecated::
        Use ``WorkloadRequirement`` with
        ``suggested_category=ServiceCategory.COMPUTE`` instead.
    """

    name: str = Field(..., description="Descriptive name (e.g., 'API Gateway')")
    vcpus: int = Field(..., ge=1, description="Required vCPU count")
    memory_gb: float = Field(..., gt=0, description="Required RAM in GiB")
    storage_gb: float = Field(default=0, ge=0, description="Attached disk in GiB")
    gpu_required: bool = Field(default=False)
    gpu_count: int = Field(default=0, ge=0)
    os: str = Field(default="linux", description="Operating system (linux/windows)")
    count: int = Field(default=1, ge=1, description="Number of identical instances")


class ContainerWorkload(BaseModel):
    """A containerised workload destined for a Kubernetes node pool.

    .. deprecated::
        Use ``WorkloadRequirement`` with
        ``suggested_category=ServiceCategory.CONTAINER`` instead.
    """

    name: str = Field(..., description="Workload / microservice name")
    cpu_request_millicores: int = Field(..., ge=50, description="CPU request in millicores")
    cpu_limit_millicores: int = Field(..., ge=50, description="CPU limit in millicores")
    memory_request_mb: int = Field(..., ge=64, description="Memory request in MiB")
    memory_limit_mb: int = Field(..., ge=64, description="Memory limit in MiB")
    replicas: int = Field(default=1, ge=1, description="Desired replica count")
    gpu_required: bool = Field(default=False)


class StorageRequirement(BaseModel):
    """Block / object storage requirement.

    .. deprecated::
        Use ``WorkloadRequirement`` with
        ``suggested_category=ServiceCategory.STORAGE`` instead.
    """

    name: str = Field(..., description="Logical volume name")
    capacity_gb: float = Field(..., gt=0, description="Required capacity in GiB")
    iops: Optional[int] = Field(default=None, ge=0, description="Required IOPS (block only)")
    throughput_mbps: Optional[float] = Field(
        default=None, ge=0, description="Required throughput in MiB/s",
    )
    storage_type: str = Field(
        default="ssd",
        description="Storage media type: ssd | hdd | nvme",
    )
