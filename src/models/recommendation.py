"""Recommendation output models.

Defines the structured output produced by the agent pipeline:
bin-packing results, cost comparisons, compliance reports,
and the final consolidated recommendation.

All SKU references now use ``NormalizedPriceItem`` — the universal
provider-agnostic price record — instead of the legacy
``ComputeSKU`` / ``StorageSKU`` types.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem
from src.models.workload import WorkloadProfile


# ─── Ancillary Costs ────────────────────────────────────────


class AncillaryCost(BaseModel):
    """A typed record for ancillary infrastructure cost items.

    Used to represent fixed or usage-based costs that sit outside the main
    SKU catalog — e.g. NAT gateways, load balancers, data transfer, and
    the K8s managed control-plane fee.  The Sizer generates these as part
    of ``_build_ancillary_results()`` and they are surfaced in the
    ``ProviderCostBreakdown`` for downstream agents and the RFP Writer.
    """

    provider: CloudProvider
    category: ServiceCategory = Field(
        description="Service category this cost falls under (NETWORKING, KUBERNETES, …)",
    )
    item_name: str = Field(
        description="Human-readable label (e.g. '[Infra] NAT Gateway', 'K8s Cluster Management')",
    )
    monthly_cost_usd: float = Field(
        ge=0,
        description="Estimated monthly cost in USD",
    )
    unit: str = Field(
        default="fixed",
        description="Pricing unit: 'fixed' | 'per_gb' | 'per_request' | …",
    )
    quantity: float = Field(
        default=1.0,
        ge=0,
        description="Quantity of units (e.g. 1 NAT GW, 500 GB data transfer)",
    )
    notes: str = Field(
        default="",
        description="Optional pricing rationale or source reference",
    )


# ─── Bin-Packing ────────────────────────────────────────────


class PackedNode(BaseModel):
    """A single node in the bin-packing solution."""

    node_sku: NormalizedPriceItem = Field(
        description="The VM SKU selected for this node",
    )
    assigned_workloads: list[str] = Field(
        default_factory=list,
        description="Names of container workloads assigned to this node",
    )
    cpu_utilization_pct: float = Field(ge=0, le=100)
    memory_utilization_pct: float = Field(ge=0, le=100)
    wasted_cpu_millicores: int = Field(default=0, ge=0)
    wasted_memory_mb: int = Field(default=0, ge=0)


class BinPackingResult(BaseModel):
    """Output of the bin-packing engine for a single provider."""

    provider: CloudProvider
    node_pool_name: str = Field(default="default-pool")
    nodes: list[PackedNode] = Field(default_factory=list)
    total_nodes: int = Field(default=0, ge=0)
    packing_efficiency_pct: float = Field(
        default=0.0, ge=0, le=100,
        description="Overall resource utilization across all nodes",
    )
    total_monthly_cost_usd: float = Field(default=0.0, ge=0)
    algorithm_used: str = Field(default="first-fit-decreasing")


# ─── Cost Comparison ────────────────────────────────────────


class ProviderCostBreakdown(BaseModel):
    """Cost summary for a single cloud provider."""

    provider: CloudProvider
    compute_monthly_usd: float = Field(default=0.0, ge=0)
    database_monthly_usd: float = Field(default=0.0, ge=0)
    storage_monthly_usd: float = Field(default=0.0, ge=0)
    kubernetes_monthly_usd: float = Field(default=0.0, ge=0)
    networking_monthly_usd: float = Field(default=0.0, ge=0)
    serverless_monthly_usd: float = Field(default=0.0, ge=0)
    other_monthly_usd: float = Field(default=0.0, ge=0)
    total_monthly_usd: float = Field(default=0.0, ge=0)
    total_annual_usd: float = Field(default=0.0, ge=0)

    # --- Savings opportunities ---
    reserved_1yr_monthly_usd: float | None = Field(default=None, ge=0)
    reserved_1yr_savings_pct: float | None = Field(default=None, ge=0, le=100)
    reserved_3yr_monthly_usd: float | None = Field(default=None, ge=0)
    reserved_3yr_savings_pct: float | None = Field(default=None, ge=0, le=100)
    spot_monthly_usd: float | None = Field(default=None, ge=0)
    spot_savings_pct: float | None = Field(default=None, ge=0, le=100)

    selected_skus: list[NormalizedPriceItem] = Field(
        default_factory=list,
        description="SKUs chosen by the Sizer for this provider",
    )
    ancillary_costs: list[AncillaryCost] = Field(
        default_factory=list,
        description=(
            "Typed breakdown of ancillary costs: NAT gateways, load balancers, "
            "data transfer, K8s cluster management fees, etc."
        ),
    )


class CostComparison(BaseModel):
    """Multi-provider cost comparison produced by the FinOps Agent."""

    providers: list[ProviderCostBreakdown] = Field(default_factory=list)
    cheapest_provider: CloudProvider | None = Field(default=None)
    savings_vs_most_expensive_pct: float = Field(default=0.0, ge=0, le=100)
    budget_monthly_usd: float | None = Field(
        default=None, ge=0, description="User's stated budget (if any)",
    )
    budget_exceeded: bool = Field(
        default=False,
        description="True if cheapest option exceeds the budget",
    )
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─── Compliance ─────────────────────────────────────────────


class ComplianceCheckResult(BaseModel):
    """Result of a single WAF compliance check."""

    pillar: str = Field(description="WAF pillar (e.g. 'Security', 'Reliability')")
    check_name: str
    passed: bool
    severity: str = Field(default="medium", description="low | medium | high | critical")
    finding: str = Field(default="")
    recommendation: str = Field(default="")


class ComplianceReport(BaseModel):
    """Aggregated compliance report across all WAF pillars."""

    framework: str = Field(default="Well-Architected Framework")
    checks: list[ComplianceCheckResult] = Field(default_factory=list)
    total_checks: int = Field(default=0, ge=0)
    passed_checks: int = Field(default=0, ge=0)
    compliance_score_pct: float = Field(default=0.0, ge=0, le=100)


# ─── Final Recommendation ──────────────────────────────────


class CloudRecommendation(BaseModel):
    """Top-level recommendation produced by the full agent pipeline.

    This is the primary output of the orchestrator, aggregating
    results from all agents into a single actionable response.
    """

    request_id: str = Field(..., description="Unique request correlation ID")
    project_name: str
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    workload_profile: WorkloadProfile
    bin_packing_results: dict[str, BinPackingResult] = Field(
        default_factory=dict,
        description="Provider → BinPackingResult for K8s workloads",
    )
    sku_selections: dict[str, list[NormalizedPriceItem]] = Field(
        default_factory=dict,
        description="Provider → selected SKUs (all service categories)",
    )
    cost_comparison: CostComparison = Field(default_factory=CostComparison)
    compliance_report: ComplianceReport = Field(default_factory=ComplianceReport)

    recommended_provider: CloudProvider | None = Field(default=None)
    executive_summary: str = Field(
        default="",
        description="LLM-generated executive summary",
    )
    rfp_document: str = Field(
        default="",
        description="Generated RFP / procurement document (Markdown)",
    )

    # --- KPI Metrics (for dissertation evaluation) ---
    kpis: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Measurable KPIs: cost_reduction_pct, packing_efficiency, "
            "compliance_score, decision_latency_ms, llm_calls, "
            "cache_hit_rate"
        ),
    )
