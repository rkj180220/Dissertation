"""RFP Writer Agent — Procurement document generation.

The RFP Writer is the fifth and final stage of the orchestration
pipeline.  It consumes all upstream agent outputs and produces a
structured Markdown procurement document (RFP) along with an
executive summary for stakeholders.

### Responsibilities

1. **Executive summary** — LLM-generated high-level overview of the
   recommendation, costs, and trade-offs.
2. **Technical specification** — Tabulated SKU selections per
   provider with resource specifications.
3. **Cost comparison tables** — Per-provider cost breakdowns with
   RI/spot savings.
4. **WAF compliance section** — Compliance check results from the
   WAF engine.
5. **Vendor shortlist** — Recommended providers with justification.

### Flow

```
All upstream state (request, profile, sized_results, cost_comparison)
      │
      ▼
┌──────────────────────────────────────────┐
│  1. Run WAF compliance checks            │
│  2. Generate executive summary (LLM)     │
│  3. Build Markdown sections              │
│  4. Assemble full RFP document           │
└──────────────────┬───────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────┐
│  state['rfp_document']                   │
│  state['executive_summary']              │
│  state['compliance_report']              │
└──────────────────────────────────────────┘
```

### Usage

```python
from src.agents.rfp_writer import run_rfp_writer_node

state = await run_rfp_writer_node(state, llm)
# state['rfp_document'] contains the full Markdown RFP
# state['executive_summary'] contains the stakeholder summary
# state['compliance_report'] contains WAF check results
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

from src.engines.waf_compliance import evaluate_compliance
from src.models.cloud_resource import CloudProvider
from src.models.conversation import ChatMessage, MessageRole
from src.models.recommendation import (
    ComplianceReport,
    CostComparison,
    ProviderCostBreakdown,
)
from src.models.workload import WorkloadProfile, WorkloadRequest
from src.orchestrator.state import (
    AgentExecution,
    AgentStatus,
    OrchestratorState,
    SizedWorkloadResult,
)

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Document section builders
# ---------------------------------------------------------------------------


def _build_header_section(
    workload_request: WorkloadRequest,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _is_government_scenario(workload_request):
        title = f"Proposed Cloud Solution — {workload_request.project_name}"
        doc_type = "Solution Proposal"
    else:
        title = f"Cloud Infrastructure Procurement — {workload_request.project_name}"
        doc_type = "Procurement Analysis"
    return (
        f"# {title}\n\n"
        f"**Document Type**: {doc_type}  \n"
        f"**Generated**: {now}  \n"
        f"**Environment**: {workload_request.environment.value}  \n"
        f"**Criticality**: {workload_request.tier.value}  \n"
        f"**Target Providers**: "
        f"{', '.join(p.value.upper() for p in workload_request.target_providers)}  \n"
        f"**Region**: {workload_request.preferred_region}  \n"
    )


def _build_workload_summary_section(
    workload_profile: WorkloadProfile,
    workload_request: WorkloadRequest,
) -> str:
    """Build the workload summary section.

    Args:
        workload_profile: Profiler output.
        workload_request: Original request.

    Returns:
        Markdown section string.
    """
    lines = ["## 2. Workload Analysis & Requirements\n"]

    lines.append(
        f"| Component | Category | vCPUs | Memory (GB) | Storage (GB) | GPU |\n"
        f"|-----------|----------|-------|-------------|--------------|-----|\n"
    )
    for comp in workload_profile.components:
        gpu = "Yes" if comp.requires_gpu else "No"
        lines.append(
            f"| {comp.workload_name} | {comp.resolved_category.value} "
            f"| {comp.estimated_vcpus} | {comp.estimated_memory_gb} "
            f"| {comp.estimated_storage_gb} | {gpu} |"
        )

    lines.append(
        f"\n**Totals**: {workload_profile.total_vcpus} vCPUs, "
        f"{workload_profile.total_memory_gb} GB RAM, "
        f"{workload_profile.total_storage_gb} GB storage"
    )
    if workload_profile.requires_gpu:
        lines.append(f", {workload_profile.total_gpu_count} GPU(s)")

    if workload_request.budget_monthly_usd:
        lines.append(
            f"\n\n**Budget Ceiling**: ${workload_request.budget_monthly_usd:,.2f}/month"
        )

    return "\n".join(lines) + "\n"


def _build_sku_selection_section(
    sized_results: list[SizedWorkloadResult],
) -> str:
    """Build the SKU selection section with per-provider tables.

    Args:
        sized_results: All sizing results.

    Returns:
        Markdown section string.
    """
    lines = ["## 6. SKU Selection & Pricing\n"]

    # Group by provider
    by_provider: dict[str, list[SizedWorkloadResult]] = {}
    for r in sized_results:
        by_provider.setdefault(r.provider.value, []).append(r)

    for prov in sorted(by_provider.keys()):
        results = by_provider[prov]
        lines.append(f"### {prov.upper()}\n")
        lines.append(
            "| Workload | SKU | Monthly Cost | Fit Score | Rationale |\n"
            "|----------|-----|-------------|-----------|------------|"
        )
        for r in results:
            sku = r.selected_sku.sku_name if r.selected_sku else "N/A"
            cost = f"${r.monthly_cost_usd:,.2f}"
            fit = f"{r.fit_score:.2f}"
            # Truncate rationale for table readability
            rationale = r.rationale[:80] + "..." if len(r.rationale) > 80 else r.rationale
            lines.append(f"| {r.workload_name} | {sku} | {cost} | {fit} | {rationale} |")
        lines.append("")

    return "\n".join(lines) + "\n"


def _build_cost_comparison_section(
    cost_comparison: CostComparison,
) -> str:
    """Build the cost comparison section.

    Args:
        cost_comparison: FinOps comparison data.

    Returns:
        Markdown section string.
    """
    lines = ["## 7. Cost Comparison & Savings\n"]

    lines.append(
        "| Provider | Compute | Database | Storage | K8s | Networking | "
        "Serverless | Other | **Total/mo** | **Total/yr** |\n"
        "|----------|---------|----------|---------|-----|------------|"
        "-----------|-------|-------------|-------------|"
    )
    for pb in sorted(cost_comparison.providers, key=lambda p: p.total_monthly_usd):
        lines.append(
            f"| {pb.provider.value.upper()} "
            f"| ${pb.compute_monthly_usd:,.2f} "
            f"| ${pb.database_monthly_usd:,.2f} "
            f"| ${pb.storage_monthly_usd:,.2f} "
            f"| ${pb.kubernetes_monthly_usd:,.2f} "
            f"| ${pb.networking_monthly_usd:,.2f} "
            f"| ${pb.serverless_monthly_usd:,.2f} "
            f"| ${pb.other_monthly_usd:,.2f} "
            f"| **${pb.total_monthly_usd:,.2f}** "
            f"| **${pb.total_annual_usd:,.2f}** |"
        )

    # Savings opportunities
    lines.append("\n### Savings Opportunities\n")
    lines.append(
        "| Provider | Reserved 1yr | RI-1yr Savings | Reserved 3yr | "
        "RI-3yr Savings | Spot | Spot Savings |\n"
        "|----------|-------------|----------------|-------------|"
        "----------------|------|-------------|"
    )
    for pb in sorted(cost_comparison.providers, key=lambda p: p.total_monthly_usd):
        ri1 = f"${pb.reserved_1yr_monthly_usd:,.2f}" if pb.reserved_1yr_monthly_usd else "N/A"
        ri1s = f"{pb.reserved_1yr_savings_pct:.0f}%" if pb.reserved_1yr_savings_pct else "—"
        ri3 = f"${pb.reserved_3yr_monthly_usd:,.2f}" if pb.reserved_3yr_monthly_usd else "N/A"
        ri3s = f"{pb.reserved_3yr_savings_pct:.0f}%" if pb.reserved_3yr_savings_pct else "—"
        sp = f"${pb.spot_monthly_usd:,.2f}" if pb.spot_monthly_usd else "N/A"
        sps = f"{pb.spot_savings_pct:.0f}%" if pb.spot_savings_pct else "—"
        lines.append(
            f"| {pb.provider.value.upper()} | {ri1} | {ri1s} | {ri3} | {ri3s} | {sp} | {sps} |"
        )

    # Budget check
    if cost_comparison.budget_monthly_usd is not None:
        status = "**EXCEEDED**" if cost_comparison.budget_exceeded else "Within budget"
        lines.append(
            f"\n**Budget**: ${cost_comparison.budget_monthly_usd:,.2f}/mo — {status}"
        )

    if cost_comparison.cheapest_provider:
        lines.append(
            f"\n**Recommended Provider**: "
            f"{cost_comparison.cheapest_provider.value.upper()} "
            f"(saves {cost_comparison.savings_vs_most_expensive_pct:.1f}% "
            f"vs. most expensive)"
        )

    return "\n".join(lines) + "\n"


def _build_compliance_section(
    compliance_report: ComplianceReport,
) -> str:
    """Build the WAF compliance section.

    Args:
        compliance_report: WAF check results.

    Returns:
        Markdown section string.
    """
    lines = ["## 16. WAF Compliance Report (Well-Architected Framework)\n"]

    lines.append(
        f"**Overall Score**: {compliance_report.compliance_score_pct:.0f}% "
        f"({compliance_report.passed_checks}/{compliance_report.total_checks} checks passed)\n"
    )

    if compliance_report.checks:
        lines.append(
            "| Pillar | Check | Status | Severity | Finding |\n"
            "|--------|-------|--------|----------|---------|"
        )
        for check in compliance_report.checks:
            status = "PASS" if check.passed else "**FAIL**"
            finding = check.finding[:60] + "..." if len(check.finding) > 60 else check.finding
            lines.append(
                f"| {check.pillar} | {check.check_name} | {status} "
                f"| {check.severity} | {finding} |"
            )

        # List recommendations for failed checks
        failed = [c for c in compliance_report.checks if not c.passed]
        if failed:
            lines.append("\n### Recommendations\n")
            for check in failed:
                lines.append(
                    f"- **{check.pillar} — {check.check_name}**: "
                    f"{check.recommendation}"
                )

    return "\n".join(lines) + "\n"


def _build_vendor_shortlist_section(
    cost_comparison: CostComparison,
    sized_results: list[SizedWorkloadResult],
) -> str:
    """Build the vendor shortlist section.

    Args:
        cost_comparison: FinOps comparison data.
        sized_results: All sizing results.

    Returns:
        Markdown section string.
    """
    lines = ["## 14. Vendor Recommendation & Shortlist\n"]

    # Rank providers
    sorted_providers = sorted(
        cost_comparison.providers,
        key=lambda p: p.total_monthly_usd,
    )

    for rank, pb in enumerate(sorted_providers, 1):
        # Count workloads with good fit for this provider
        prov_results = [r for r in sized_results if r.provider == pb.provider]
        avg_fit = (
            sum(r.fit_score for r in prov_results) / len(prov_results)
            if prov_results else 0.0
        )
        good_fit = sum(1 for r in prov_results if r.fit_score >= 0.6)

        label = " **(Recommended)**" if rank == 1 else ""
        lines.append(
            f"### {rank}. {pb.provider.value.upper()}{label}\n\n"
            f"- **Monthly Cost**: ${pb.total_monthly_usd:,.2f}\n"
            f"- **Annual Cost**: ${pb.total_annual_usd:,.2f}\n"
            f"- **Avg. Fit Score**: {avg_fit:.2f}\n"
            f"- **SKUs with good fit (≥0.6)**: {good_fit}/{len(prov_results)}\n"
        )
        if pb.reserved_1yr_savings_pct:
            lines.append(
                f"- **RI 1yr Savings**: {pb.reserved_1yr_savings_pct:.0f}%\n"
            )
        if pb.spot_savings_pct:
            lines.append(
                f"- **Spot Savings**: {pb.spot_savings_pct:.0f}%\n"
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# NEW: Enterprise-grade section builders (P1e)
# ---------------------------------------------------------------------------

#: SLA targets by workload tier value
_SLA_TARGETS: dict[str, dict[str, str]] = {
    "mission_critical": {
        "availability": "99.99%",
        "downtime_per_year": "≤ 52 minutes",
        "rpo": "0 minutes (synchronous replication)",
        "rto": "≤ 15 minutes",
        "support": "24×7, 15-minute response, dedicated TAM",
    },
    "production": {
        "availability": "99.9%",
        "downtime_per_year": "≤ 8.76 hours",
        "rpo": "≤ 60 minutes",
        "rto": "≤ 4 hours",
        "support": "24×7, 1-hour response, Business Support",
    },
    "staging": {
        "availability": "99.5%",
        "downtime_per_year": "≤ 43.8 hours",
        "rpo": "≤ 24 hours",
        "rto": "≤ 24 hours",
        "support": "Business hours, 4-hour response",
    },
    "development": {
        "availability": "95%",
        "downtime_per_year": "Best effort",
        "rpo": "Best effort",
        "rto": "Best effort",
        "support": "Standard support",
    },
}

#: DR strategy by tier
_DR_STRATEGY: dict[str, dict[str, str]] = {
    "mission_critical": {
        "pattern": "Active-Active Multi-Region",
        "database_backup": "Continuous WAL streaming + point-in-time recovery (PITR) to 5-minute granularity",
        "snapshot_frequency": "Every 15 minutes",
        "retention": "90 days",
        "failover": "Automatic via health checks; DNS TTL ≤ 30s; zero manual intervention required",
        "test_cadence": "Monthly DR drills; quarterly full failover simulation",
    },
    "production": {
        "pattern": "Active-Passive Warm Standby",
        "database_backup": "Daily full + hourly incremental backups; PITR to 1-hour granularity",
        "snapshot_frequency": "Every 4 hours",
        "retention": "30 days",
        "failover": "Semi-automatic via runbook; DNS failover in ≤ 15 minutes",
        "test_cadence": "Quarterly DR drills",
    },
    "staging": {
        "pattern": "Backup & Restore",
        "database_backup": "Daily full backups",
        "snapshot_frequency": "Daily",
        "retention": "7 days",
        "failover": "Manual restore from snapshot",
        "test_cadence": "Annual review",
    },
    "development": {
        "pattern": "Backup & Restore (best effort)",
        "database_backup": "Weekly full backups",
        "snapshot_frequency": "Weekly",
        "retention": "7 days",
        "failover": "Manual restore from snapshot",
        "test_cadence": "Annual review",
    },
}

# ---------------------------------------------------------------------------
# Scenario detection helpers (P10)
# ---------------------------------------------------------------------------

_GOVERNMENT_KEYWORDS = frozenset([
    "government", "gov", "state", "county", "municipal", "city of", "department of",
    "agency", "public sector", "public safety", "fire", "police", "emergency",
    "911", "first responder", "wildfire", "disaster", "fema", "dhs", "cal fire",
])

_MOBILE_KEYWORDS = frozenset([
    "mobile", "ios", "android", "flutter", "react native", "app store", "play store",
    "smartphone", "tablet", "push notification", "fcm", "apns",
])

_MIGRATION_KEYWORDS = frozenset([
    "migration", "migrate", "lift and shift", "lift-and-shift", "re-platform",
    "replatform", "cutover", "datacenter", "data centre", "on-premises", "on-prem",
    "legacy", "move to cloud",
])


def _is_government_scenario(workload_request: WorkloadRequest) -> bool:
    text = (workload_request.raw_user_input + " " + workload_request.project_name).lower()
    compliance = " ".join(workload_request.compliance_frameworks).lower()
    return (
        any(kw in text for kw in _GOVERNMENT_KEYWORDS)
        or "stateramp" in compliance
        or "fedramp" in compliance
        or "cjis" in compliance
    )


def _is_mobile_scenario(workload_request: WorkloadRequest) -> bool:
    text = (workload_request.raw_user_input + " " + workload_request.project_name).lower()
    workload_text = " ".join(
        w.name + " " + w.description + " " + w.notes
        for w in workload_request.workloads
    ).lower()
    return any(kw in text or kw in workload_text for kw in _MOBILE_KEYWORDS)


def _is_greenfield_project(workload_request: WorkloadRequest) -> bool:
    text = workload_request.raw_user_input.lower()
    return not any(kw in text for kw in _MIGRATION_KEYWORDS)


#: Managed service recommendations per provider × category
_MANAGED_SERVICES: dict[str, dict[str, str]] = {
    "aws": {
        "COMPUTE": "Amazon EC2 Auto Scaling Groups (t3/m6i family)",
        "CONTAINER": "Amazon EKS (managed node groups) + AWS Fargate",
        "KUBERNETES": "Amazon EKS — managed control plane ($73/mo flat fee)",
        "DATABASE": "Amazon RDS Multi-AZ (PostgreSQL / MySQL) — automated failover",
        "CACHE": "Amazon ElastiCache for Redis (cluster mode enabled)",
        "STORAGE": "Amazon S3 Standard with S3 Intelligent-Tiering",
        "NETWORKING": "Amazon CloudFront CDN + AWS WAF + Application Load Balancer",
        "SERVERLESS_FUNCTION": "AWS Lambda (128 MB–10 GB RAM, 15-min max) + API Gateway",
        "AI_ML": "Amazon SageMaker (training + inference endpoints)",
        "SECURITY": "AWS IAM Identity Center + AWS KMS + AWS Shield Advanced",
        "MONITORING": "Amazon CloudWatch + AWS X-Ray + AWS CloudTrail",
        "MOBILE": "AWS AppSync (GraphQL API) + Amazon DynamoDB + Amazon SNS (push)",
        "STREAMING": "Amazon Kinesis Data Streams + Amazon Kinesis Firehose",
        "ANALYTICS": "Amazon Redshift + Amazon Athena + AWS Glue",
    },
    "azure": {
        "COMPUTE": "Azure Virtual Machine Scale Sets (Dsv5 / Bsv2 family)",
        "CONTAINER": "Azure Kubernetes Service (AKS) — free managed control plane",
        "KUBERNETES": "Azure Kubernetes Service — control plane at no extra charge",
        "DATABASE": "Azure Database for PostgreSQL Flexible Server (HA with zone redundancy)",
        "CACHE": "Azure Cache for Redis (Premium tier, cluster mode)",
        "STORAGE": "Azure Blob Storage (Hot tier, LRS/GRS)",
        "NETWORKING": "Azure Front Door + Azure WAF + Azure Application Gateway",
        "SERVERLESS_FUNCTION": "Azure Functions (Consumption or Premium plan) + API Management",
        "AI_ML": "Azure Machine Learning (compute clusters + managed endpoints)",
        "SECURITY": "Azure Entra ID + Azure Key Vault + Microsoft Defender for Cloud",
        "MONITORING": "Azure Monitor + Application Insights + Azure Log Analytics",
        "MOBILE": "Azure Notification Hubs + Azure API Management + Cosmos DB",
        "STREAMING": "Azure Event Hubs + Azure Stream Analytics",
        "ANALYTICS": "Azure Synapse Analytics + Azure Data Factory",
    },
    "gcp": {
        "COMPUTE": "Google Compute Engine (N2/E2 family) + Managed Instance Groups",
        "CONTAINER": "Google Kubernetes Engine (GKE Autopilot or Standard)",
        "KUBERNETES": "GKE — zonal cluster ($73/mo), regional cluster ($146/mo)",
        "DATABASE": "Cloud SQL for PostgreSQL (HA with automatic failover)",
        "CACHE": "Cloud Memorystore for Redis (Standard tier)",
        "STORAGE": "Cloud Storage (Standard class) + Cloud CDN",
        "NETWORKING": "Cloud Armor + Cloud Load Balancing + Cloud CDN",
        "SERVERLESS_FUNCTION": "Cloud Functions (2nd gen) + Cloud Run + API Gateway",
        "AI_ML": "Vertex AI (training pipelines + prediction endpoints)",
        "SECURITY": "Google Cloud Identity + Cloud KMS + Security Command Center",
        "MONITORING": "Cloud Monitoring + Cloud Trace + Cloud Logging",
        "MOBILE": "Firebase (Authentication + Realtime DB + Cloud Messaging)",
        "STREAMING": "Pub/Sub + Dataflow",
        "ANALYTICS": "BigQuery + Looker Studio + Dataproc",
    },
}


#: Cloud provider compliance certifications
_PROVIDER_CERTIFICATIONS: dict[str, list[tuple[str, str]]] = {
    "aws": [
        ("SOC 1, SOC 2, SOC 3", "AICPA / SSAE 18"),
        ("ISO 27001 / 27017 / 27018", "BSI Group"),
        ("FedRAMP Moderate & High", "US Federal Government"),
        ("PCI DSS Level 1", "Payment Card Industry"),
        ("HIPAA / HITECH", "US Dept. of Health & Human Services"),
        ("CSA STAR Level 2", "Cloud Security Alliance"),
        ("GDPR", "EU General Data Protection Regulation"),
        ("IRAP Protected", "Australian Government"),
    ],
    "azure": [
        ("SOC 1, SOC 2, SOC 3", "AICPA / SSAE 18"),
        ("ISO 27001 / 27017 / 27018", "BSI Group"),
        ("FedRAMP Moderate & High", "US Federal Government"),
        ("PCI DSS Level 1", "Payment Card Industry"),
        ("HIPAA / HITECH", "US Dept. of Health & Human Services"),
        ("CSA STAR Level 2", "Cloud Security Alliance"),
        ("GDPR", "EU General Data Protection Regulation"),
        ("UK Cyber Essentials Plus", "NCSC"),
    ],
    "gcp": [
        ("SOC 1, SOC 2, SOC 3", "AICPA / SSAE 18"),
        ("ISO 27001 / 27017 / 27018", "BSI Group"),
        ("FedRAMP Moderate", "US Federal Government"),
        ("PCI DSS Level 1", "Payment Card Industry"),
        ("HIPAA", "US Dept. of Health & Human Services"),
        ("CSA STAR Level 2", "Cloud Security Alliance"),
        ("GDPR", "EU General Data Protection Regulation"),
        ("IRAP Protected", "Australian Government"),
    ],
}


def _build_toc_section(
    include_traceability: bool = False,
    include_mobile: bool = False,
) -> str:
    base = (
        "## Table of Contents\n\n"
        "1. [Executive Summary](#executive-summary)\n"
        "2. [Workload Analysis & Requirements](#workload-summary)\n"
        "3. [Reference Architecture](#reference-architecture)\n"
        "4. [Technical Specifications](#technical-specifications)\n"
        "5. [Recommended Managed Services](#managed-services)\n"
        "6. [SKU Selection & Pricing](#sku-selection)\n"
        "7. [Cost Comparison & Savings](#cost-comparison)\n"
        "8. [Multi-Year Total Cost of Ownership](#tco)\n"
        "9. [Service Level Agreements](#sla)\n"
        "10. [Security Architecture](#security)\n"
        "11. [Delivery Plan](#implementation)\n"
        "12. [Disaster Recovery & Business Continuity](#dr)\n"
        "13. [Compliance & Certifications](#compliance)\n"
        "14. [Vendor Recommendation & Shortlist](#vendor)\n"
        "15. [Assumptions & Exclusions](#assumptions)\n"
        "16. [WAF Compliance Report](#waf-compliance)\n"
    )
    extras = ""
    if include_traceability:
        extras += "17. [Requirements Traceability Matrix](#traceability)\n"
    return base + extras


def _build_requirements_traceability_section(
    workload_request: WorkloadRequest,
    workload_profile: WorkloadProfile,
) -> str:
    """Gap 10b — Requirements traceability matrix mapping compliance/functional needs to solution."""
    from src.models.cloud_resource import ServiceCategory

    lines = ["## 17. Requirements Traceability Matrix\n"]
    lines.append(
        "The following matrix maps identified project requirements to the proposed "
        "solution components and their compliance status.\n"
    )

    # Functional requirements from workloads
    lines.append("### Functional Requirements\n")
    lines.append(
        "| Req ID | Requirement | Solution Component | Status |\n"
        "|--------|-------------|-------------------|--------|\n"
    )

    req_id = 1
    for comp in workload_profile.components:
        if comp.resolved_category in (ServiceCategory.MANAGEMENT, ServiceCategory.KUBERNETES):
            continue
        family_hint = comp.recommended_instance_families[0] if comp.recommended_instance_families else comp.resolved_category.value
        lines.append(
            f"| F-{req_id:02d} | {comp.resolved_category.value.replace('_', ' ').title()} "
            f"workload: {comp.workload_name} | "
            f"{family_hint} — "
            f"{comp.estimated_vcpus} vCPU / {comp.estimated_memory_gb} GB RAM | Compliant ✅ |"
        )
        req_id += 1

    # Compliance requirements
    frameworks = workload_request.compliance_frameworks
    if frameworks:
        lines.append("\n### Compliance Requirements\n")
        lines.append(
            "| Req ID | Framework | Requirement Description | Proposed Control | Status |\n"
            "|--------|-----------|------------------------|-----------------|--------|\n"
        )
        _COMPLIANCE_DESCRIPTIONS: dict[str, tuple[str, str]] = {
            "waf": ("AWS Well-Architected Framework", "5-pillar review: Security, Reliability, Performance, Cost, Ops Excellence"),
            "hipaa": ("HIPAA / HITECH", "PHI encryption at rest/in-transit, audit logging, BAA with provider"),
            "pci-dss": ("PCI DSS Level 1", "Card data isolation, tokenisation, quarterly ASV scans"),
            "fedramp": ("FedRAMP Moderate/High", "325+ NIST 800-53 controls, continuous monitoring, ATO pathway"),
            "stateramp": ("StateRAMP Moderate", "325 controls baseline (NIST 800-53 rev5), ATO from StateRAMP PMO"),
            "wcag-2.2-aa": ("WCAG 2.2 Level AA", "Colour contrast ≥4.5:1, keyboard navigation, screen reader support"),
            "wcag-2.1-aa": ("WCAG 2.1 Level AA", "Perceivable, Operable, Understandable, Robust — 50 success criteria"),
            "sox": ("SOX", "Financial data integrity, access controls, audit trails retained 7 years"),
            "gdpr": ("GDPR", "Data minimisation, right to erasure, DPA agreements, data residency"),
            "iso27001": ("ISO 27001", "ISMS certification, risk register, annual surveillance audit"),
            "cjis": ("CJIS Security Policy", "FBI CJIS encryption, MFA, audit logging, personnel screening"),
        }
        cid = 1
        for fw in frameworks:
            fw_lower = fw.lower().replace(" ", "-")
            desc = _COMPLIANCE_DESCRIPTIONS.get(fw_lower, (fw, "Controls to be defined during ATO process"))
            lines.append(
                f"| C-{cid:02d} | {desc[0]} | {desc[1]} | "
                f"Cloud provider baseline + customer controls | Acknowledged ✅ |"
            )
            cid += 1

    # Non-functional requirements
    lines.append("\n### Non-Functional Requirements\n")
    lines.append(
        "| Req ID | Category | Requirement | Solution | Status |\n"
        "|--------|----------|-------------|---------|--------|\n"
    )
    nfr_rows = [
        ("NFR-01", "Availability", f"Tier: {workload_request.tier.value}", "Multi-AZ deployment with health checks", "Compliant ✅"),
        ("NFR-02", "Scalability", "Auto-scaling on CPU/memory pressure", "HPA (K8s) / ASG (VM) with target tracking", "Compliant ✅"),
        ("NFR-03", "Security", "Encryption at rest and in transit", "AES-256 at rest, TLS 1.3 in transit", "Compliant ✅"),
        ("NFR-04", "Observability", "Centralised logging and metrics", "Provider-native monitoring + structured logs", "Compliant ✅"),
        ("NFR-05", "Cost", f"Budget ceiling: {'${:,.0f}/mo'.format(workload_request.budget_monthly_usd) if workload_request.budget_monthly_usd else 'Not specified'}", "FinOps agent cost optimisation with RI/Spot analysis", "Acknowledged ✅"),
    ]
    for row in nfr_rows:
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |")

    return "\n".join(lines) + "\n"


def _build_managed_services_section(
    workload_profile: WorkloadProfile,
    workload_request: WorkloadRequest,
    sized_results: list[SizedWorkloadResult],
) -> str:
    """Gap 10e — Recommended managed services table mapping workload categories to specific cloud services."""
    from src.models.cloud_resource import ServiceCategory

    lines = ["## 5. Recommended Managed Services\n"]
    lines.append(
        "The following table maps each workload component to the specific cloud-managed "
        "services recommended for this architecture. Managed services reduce operational "
        "overhead, include built-in HA, and are covered by the provider's SLA.\n"
    )

    # Collect categories present in this workload
    categories_present = {c.resolved_category for c in workload_profile.components}
    has_mobile = _is_mobile_scenario(workload_request)

    providers = workload_request.target_providers
    provider_values = [p.value for p in providers]

    # Build per-provider tables
    for provider_val in provider_values:
        prov_services = _MANAGED_SERVICES.get(provider_val, {})
        if not prov_services:
            continue

        lines.append(f"### {provider_val.upper()}\n")
        lines.append(
            "| Component | Recommended Service | Notes |\n"
            "|-----------|--------------------|---------|\n"
        )

        for comp in workload_profile.components:
            cat_key = comp.resolved_category.value.upper()
            # Override: cache/redis workloads are DATABASE-category but need
            # the "CACHE" service entry (ElastiCache/Azure Cache/Memorystore),
            # not the relational database entry (RDS/Azure DB/Cloud SQL).
            name_lower = comp.workload_name.lower()
            if comp.resolved_category == ServiceCategory.DATABASE and any(
                k in name_lower for k in ("cache", "redis", "elasticache", "memcache")
            ):
                cat_key = "CACHE"
            service = prov_services.get(cat_key, "")
            if not service:
                # Try CONTAINER for KUBERNETES
                if comp.resolved_category == ServiceCategory.KUBERNETES:
                    service = prov_services.get("KUBERNETES", "")
            if service:
                db_note = ""
                if comp.resolved_category == ServiceCategory.DATABASE and comp.workload_name:
                    db_note = f"Engine: {comp.workload_name}"
                lines.append(
                    f"| {comp.workload_name} | {service} | {db_note} |"
                )

        if has_mobile:
            mobile_service = prov_services.get("MOBILE", "")
            if mobile_service:
                lines.append(
                    f"| Mobile Application Backend | {mobile_service} | Push notifications + GraphQL API |"
                )

        streaming_present = any(
            "stream" in c.workload_name.lower() or "kinesis" in c.workload_name.lower()
            for c in workload_profile.components
        )
        if streaming_present:
            streaming_service = prov_services.get("STREAMING", "")
            if streaming_service:
                lines.append(
                    f"| Event Streaming | {streaming_service} | Real-time data ingestion |"
                )

        lines.append("")

    # Key benefits callout
    lines.append("### Key Benefits of Managed Services\n")
    lines.append(
        "| Benefit | Detail |\n"
        "|---------|--------|\n"
        "| **Reduced Ops Overhead** | No OS patching, no cluster management for managed tiers |\n"
        "| **Built-in HA** | Multi-AZ replication and automatic failover included |\n"
        "| **Provider SLA** | Each managed service carries a provider-backed uptime SLA |\n"
        "| **Auto-scaling** | Vertical/horizontal scaling without manual intervention |\n"
        "| **Security baseline** | Encryption, IAM integration, and audit logging built in |\n"
    )

    return "\n".join(lines) + "\n"


def _build_mobile_subsection(workload_request: WorkloadRequest) -> str:
    """Gap 10d — Mobile application architecture subsection (inserted into architecture section)."""
    provider = workload_request.target_providers[0].value if workload_request.target_providers else "aws"
    prov_services = _MANAGED_SERVICES.get(provider, {})
    mobile_service = prov_services.get("MOBILE", "AWS AppSync + DynamoDB + SNS")

    return (
        "\n### Mobile Application Architecture\n\n"
        "The solution includes a mobile-first data path designed for iOS and Android clients:\n\n"
        "```\n"
        "  iOS / Android App\n"
        "         │\n"
        "         ▼ (HTTPS + Certificate Pinning)\n"
        "  ┌─────────────────────────────────────────┐\n"
        f"  │   {mobile_service:<37}│\n"
        "  │   (Real-time sync + Push Notifications)  │\n"
        "  └────────────────────┬────────────────────┘\n"
        "                       │\n"
        "                       ▼\n"
        "  ┌─────────────────────────────────────────┐\n"
        "  │   Application Backend (API Gateway)      │\n"
        "  │   Auth: OAuth 2.0 / JWT token validation │\n"
        "  └─────────────────────────────────────────┘\n"
        "```\n\n"
        "**Mobile architecture principles:**\n\n"
        "- **Offline-first**: Local SQLite cache with conflict-resolution sync on reconnect\n"
        "- **Push notifications**: FCM (Android) + APNs (iOS) via provider notification hub\n"
        "- **Authentication**: OAuth 2.0 PKCE flow; tokens stored in OS secure keychain\n"
        "- **API**: GraphQL or REST over HTTPS with certificate pinning to prevent MITM\n"
        "- **Accessibility**: WCAG 2.2 AA compliance — VoiceOver (iOS) + TalkBack (Android) support\n"
        "- **App distribution**: App Store + Google Play Store; MDM support for enterprise devices\n"
    )


def _build_architecture_section(
    workload_profile: WorkloadProfile,
    workload_request: WorkloadRequest,
    sized_results: list[SizedWorkloadResult],
) -> str:
    """Build the reference architecture section with ASCII topology.

    Args:
        workload_profile: Profiler output.
        workload_request: Original request.
        sized_results: All sizing results.

    Returns:
        Markdown section string.
    """
    from src.models.cloud_resource import ServiceCategory

    lines = ["## 3. Reference Architecture\n"]

    # Detect which tiers exist in the profile
    categories = {c.resolved_category for c in workload_profile.components}
    has_container = ServiceCategory.CONTAINER in categories
    has_database = ServiceCategory.DATABASE in categories
    has_storage = ServiceCategory.STORAGE in categories
    has_compute = ServiceCategory.COMPUTE in categories
    has_serverless = ServiceCategory.SERVERLESS_FUNCTION in categories
    has_networking = ServiceCategory.NETWORKING in categories
    has_ai_ml = ServiceCategory.AI_ML in categories

    preferred_provider = workload_request.target_providers[0].value.upper() if workload_request.target_providers else "CLOUD"

    # ASCII architecture diagram
    lines.append("### Architecture Overview\n")
    lines.append("```")
    lines.append(f"  ┌─────────────────────────────────────────────────────────────┐")
    lines.append(f"  │                  {workload_request.project_name[:40]:<40}  │")
    lines.append(f"  │                  Cloud Target: {preferred_provider:<27}  │")
    lines.append(f"  └─────────────────────────────────────────────────────────────┘")
    lines.append(f"")
    lines.append(f"  Internet / Client Traffic")
    lines.append(f"         │")
    lines.append(f"         ▼")
    lines.append(f"  ┌─────────────────────────┐")
    lines.append(f"  │   Load Balancer / CDN   │  (Layer 7 — HTTPS/TLS termination)")
    lines.append(f"  │   WAF + DDoS Protection │")
    lines.append(f"  └────────────┬────────────┘")
    lines.append(f"               │")

    if has_container:
        lines.append(f"               ▼")
        lines.append(f"  ┌─────────────────────────┐")
        lines.append(f"  │  Kubernetes Cluster      │  (Managed K8s service)")
        container_comps = [c for c in workload_profile.components if c.resolved_category == ServiceCategory.CONTAINER]
        for comp in container_comps[:4]:
            lines.append(f"  │   ├─ {comp.workload_name[:35]:<35}│")
        lines.append(f"  │  (Auto-scaling node pools)│")
        lines.append(f"  └────────────┬────────────┘")
        lines.append(f"               │")
    elif has_compute:
        lines.append(f"               ▼")
        lines.append(f"  ┌─────────────────────────┐")
        lines.append(f"  │   Compute Layer          │  (VMs / Auto-scaling groups)")
        lines.append(f"  └────────────┬────────────┘")
        lines.append(f"               │")

    if has_ai_ml:
        lines.append(f"               ├─────────────────┐")
        lines.append(f"               │                 ▼")
        lines.append(f"               │   ┌─────────────────────────┐")
        lines.append(f"               │   │   AI/ML Inference Layer │")
        lines.append(f"               │   │   (GPU-accelerated)      │")
        lines.append(f"               │   └─────────────────────────┘")

    if has_serverless:
        lines.append(f"               ├─────────────────┐")
        lines.append(f"               │                 ▼")
        lines.append(f"               │   ┌─────────────────────────┐")
        lines.append(f"               │   │   Serverless Functions   │")
        lines.append(f"               │   └─────────────────────────┘")

    lines.append(f"               │")
    lines.append(f"               ▼")
    lines.append(f"  ┌──────────────────────────────────────┐")
    lines.append(f"  │              Data Layer               │")
    if has_database:
        db_comps = [c for c in workload_profile.components if c.resolved_category == ServiceCategory.DATABASE]
        for comp in db_comps[:3]:
            lines.append(f"  │   ├─ Database: {comp.workload_name[:28]:<28}│")
    if has_storage:
        lines.append(f"  │   ├─ Object Storage (S3/Blob/GCS)     │")
    lines.append(f"  └──────────────────────────────────────┘")
    lines.append(f"")
    lines.append(f"  ┌─────────────────────────────────────────────────────┐")
    lines.append(f"  │            Shared Infrastructure                      │")
    lines.append(f"  │   VPC / VNet │ NAT Gateway │ Private DNS │ Monitoring │")
    lines.append(f"  └─────────────────────────────────────────────────────┘")
    lines.append("```")

    # Data flow description
    lines.append("\n### Data Flow\n")
    lines.append(
        "1. **Ingress**: All client traffic enters through a managed load balancer "
        "with TLS 1.3 termination. A Web Application Firewall (WAF) inspects "
        "requests for OWASP Top-10 threats and applies rate limiting before "
        "forwarding to the application tier.\n"
    )

    if has_container:
        lines.append(
            "2. **Application Tier**: The Kubernetes cluster runs containerised "
            "microservices with horizontal pod autoscaling (HPA). Node pools scale "
            "automatically based on CPU/memory utilisation targets. Service mesh "
            "(Istio/Linkerd) provides mTLS between pods and circuit-breaking.\n"
        )
    elif has_compute:
        lines.append(
            "2. **Application Tier**: Auto-scaling groups of virtual machines serve "
            "application traffic. Instance health checks and rolling deployment "
            "strategies ensure zero-downtime updates.\n"
        )

    lines.append(
        "3. **Data Tier**: All data at rest is encrypted using AES-256. Database "
        "connections use TLS 1.2+ with certificate validation. Read replicas "
        "distribute query load; primary handles writes only.\n"
    )
    lines.append(
        "4. **Networking**: All inter-service communication uses private VPC "
        "addressing. Outbound internet traffic routes through a NAT gateway. "
        "No services expose direct public IPs; all public endpoints are behind "
        "the load balancer.\n"
    )

    # Network topology
    lines.append("### Network Topology\n")
    env = workload_request.environment.value
    lines.append(f"**Deployment Environment**: {env.title()}\n")
    lines.append(
        "| Subnet | CIDR (example) | Purpose |\n"
        "|--------|---------------|---------|"
    )
    lines.append("| Public | 10.0.0.0/24 | Load balancers, NAT gateways |")
    lines.append("| Private (App) | 10.0.1.0/24 | Compute / K8s node pools |")
    if has_database:
        lines.append("| Private (Data) | 10.0.2.0/24 | Databases, caches |")
    lines.append("| Management | 10.0.3.0/24 | Bastion hosts, monitoring |")

    # Gap 10d — mobile architecture subsection
    if _is_mobile_scenario(workload_request):
        lines.append(_build_mobile_subsection(workload_request))

    return "\n".join(lines) + "\n"


def _build_tech_specs_section(
    workload_profile: WorkloadProfile,
    sized_results: list[SizedWorkloadResult],
    workload_request: WorkloadRequest,
) -> str:
    """Build detailed technical specifications per component.

    Args:
        workload_profile: Profiler output.
        sized_results: All sizing results.
        workload_request: Original request.

    Returns:
        Markdown section string.
    """
    from src.models.cloud_resource import ServiceCategory

    lines = ["## 4. Technical Specifications\n"]
    lines.append(
        "The following specifications define the compute, storage, and networking "
        "requirements for each system component.  SKU selections are based on the "
        "best-fit scoring across vCPU, memory, storage, and price dimensions.\n"
    )

    # Build a lookup: workload_name → SizedWorkloadResult (one per provider per workload)
    # We'll show the recommended provider's results if available
    recommended_provider = None
    if workload_request.target_providers:
        recommended_provider = workload_request.target_providers[0]

    # Group results by workload name
    results_by_workload: dict[str, list[SizedWorkloadResult]] = {}
    for r in sized_results:
        results_by_workload.setdefault(r.workload_name, []).append(r)

    for comp in workload_profile.components:
        lines.append(f"### {comp.workload_name}\n")
        inst_family = comp.recommended_instance_families[0] if comp.recommended_instance_families else "General Purpose"
        lines.append(f"**Category**: {comp.resolved_category.value.replace('_', ' ').title()}  ")
        lines.append(f"**Instance Family**: {inst_family}  \n")

        # Resource requirements
        lines.append(
            "| Resource | Requirement | Notes |\n"
            "|----------|-------------|-------|"
        )
        lines.append(f"| vCPU | {comp.estimated_vcpus} cores | "
                     f"{'Burstable' if comp.estimated_vcpus <= 2 else 'Dedicated'} |")
        lines.append(f"| Memory | {comp.estimated_memory_gb} GB | "
                     f"{'Standard' if comp.estimated_memory_gb <= 16 else 'High-memory'} |")
        lines.append(f"| Storage | {comp.estimated_storage_gb} GB | "
                     f"{'SSD' if comp.resolved_category in (ServiceCategory.DATABASE, ServiceCategory.STORAGE) else 'Standard'} |")
        if comp.requires_gpu:
            lines.append(f"| GPU | 1+ unit(s) | NVIDIA/AMD accelerator |")
        lines.append("")

        # SKU selections for this workload
        wl_results = results_by_workload.get(comp.workload_name, [])
        if wl_results:
            lines.append(
                "| Provider | Selected SKU | Monthly Cost | Fit Score |\n"
                "|----------|-------------|-------------|-----------|"
            )
            for r in sorted(wl_results, key=lambda x: x.monthly_cost_usd):
                sku = r.selected_sku.sku_name if r.selected_sku else "Fixed cost"
                lines.append(
                    f"| {r.provider.value.upper()} | `{sku}` "
                    f"| ${r.monthly_cost_usd:,.2f} | {r.fit_score:.2f} |"
                )
            lines.append("")

            # Show SKU attributes for recommended provider
            for r in wl_results:
                if r.selected_sku and r.selected_sku.attributes:
                    attrs = r.selected_sku.attributes
                    attr_notes = []
                    for key in ("vcpus", "vcpu", "memory_gb", "memory",
                                "storage_gb", "network_performance", "generation"):
                        if key in attrs and attrs[key]:
                            attr_notes.append(f"**{key}**: {attrs[key]}")
                    if attr_notes:
                        lines.append(
                            f"*{r.provider.value.upper()} SKU attributes*: "
                            + " | ".join(attr_notes[:6]) + "  \n"
                        )
                    break  # Show first provider's attrs only

        if comp.rationale:
            lines.append(f"> **Sizing Rationale**: {comp.rationale[:300]}\n")

    # Aggregated summary
    lines.append("### Component Summary\n")
    lines.append(
        "| Metric | Value |\n"
        "|--------|-------|"
    )
    lines.append(f"| Total Components | {len(workload_profile.components)} |")
    lines.append(f"| Total vCPUs | {workload_profile.total_vcpus} |")
    lines.append(f"| Total Memory | {workload_profile.total_memory_gb} GB |")
    lines.append(f"| Total Storage | {workload_profile.total_storage_gb} GB |")
    if workload_profile.requires_gpu:
        lines.append(f"| Total GPUs | {workload_profile.total_gpu_count} |")
    lines.append(
        f"| Environment | {workload_request.environment.value.title()} |"
    )
    lines.append(
        f"| Workload Tier | {workload_request.tier.value.replace('_', ' ').title()} |"
    )

    return "\n".join(lines) + "\n"


def _build_sla_section(
    workload_request: WorkloadRequest,
    cost_comparison: CostComparison,
) -> str:
    """Build the SLA and service commitments section.

    Prefers explicit SLA values from ``WorkloadRequirement`` fields
    (``uptime_sla``, ``rpo_minutes``, ``rto_minutes``, ``latency_p99_ms``,
    ``throughput_rps``) over the tier-based defaults in ``_SLA_TARGETS``.

    Args:
        workload_request: Original request.
        cost_comparison: FinOps comparison (for provider-specific SLAs).

    Returns:
        Markdown section string.
    """
    tier = workload_request.tier.value.lower()
    sla = _SLA_TARGETS.get(tier, _SLA_TARGETS["production"])

    # --- Extract explicit SLA values from workload requirements ---
    workloads = workload_request.workloads

    # Most-stringent uptime across all workloads (highest % = most demanding)
    explicit_uptime = max(
        (w.uptime_sla for w in workloads if w.uptime_sla is not None),
        default=None,
    )
    # Strictest RPO = smallest value
    explicit_rpo_min = min(
        (w.rpo_minutes for w in workloads if w.rpo_minutes is not None),
        default=None,
    )
    # Strictest RTO = smallest value
    explicit_rto_min = min(
        (w.rto_minutes for w in workloads if w.rto_minutes is not None),
        default=None,
    )
    # Most demanding latency target
    explicit_latency = min(
        (w.latency_p99_ms for w in workloads if w.latency_p99_ms is not None),
        default=None,
    )
    # Peak throughput
    explicit_throughput = max(
        (w.throughput_rps for w in workloads if w.throughput_rps is not None),
        default=None,
    )
    explicit_users = max(
        (w.concurrent_users for w in workloads if w.concurrent_users is not None),
        default=None,
    )

    # --- Format availability & RPO/RTO strings ---
    availability_str = f"{explicit_uptime:.2f}%" if explicit_uptime is not None else sla["availability"]

    def _fmt_minutes(minutes: int | None) -> str:
        """Format minutes as 'Xh Ym' or '< 1 min'."""
        if minutes is None:
            return "N/A"
        if minutes == 0:
            return "Zero (continuous replication)"
        if minutes < 60:
            return f"< {minutes} min"
        h, m = divmod(minutes, 60)
        return f"< {h}h {m:02d}m" if m else f"< {h}h"

    rpo_str = _fmt_minutes(explicit_rpo_min) if explicit_rpo_min is not None else sla["rpo"]
    rto_str = _fmt_minutes(explicit_rto_min) if explicit_rto_min is not None else sla["rto"]

    lines = ["## 9. Service Level Agreements\n"]
    lines.append(
        f"Service level commitments are defined for the **{tier.replace('_', ' ').title()}** "
        f"workload tier and must be contractually agreed with the selected cloud provider.\n"
    )
    if explicit_uptime is not None or explicit_rpo_min is not None:
        lines.append(
            "_Note: SLA targets below reflect customer-specified requirements "
            "extracted from the workload definition and supersede tier defaults where provided._\n"
        )

    # Core SLA targets
    lines.append("### Core SLA Targets\n")
    lines.append(
        "| Metric | Target | Measurement Period |\n"
        "|--------|--------|-------------------|"
    )
    lines.append(f"| **Availability** | {availability_str} | Rolling 30-day |")
    lines.append(f"| **Maximum Downtime/Year** | {sla['downtime_per_year']} | Calendar year |")
    lines.append(f"| **Recovery Point Objective (RPO)** | {rpo_str} | Per incident |")
    lines.append(f"| **Recovery Time Objective (RTO)** | {rto_str} | Per incident |")
    lines.append(f"| **Support Response** | {sla['support']} | Business-hours or 24×7 |")
    if explicit_latency is not None:
        lines.append(f"| **P99 Latency Target** | < {explicit_latency} ms | Per request (95th pctile window) |")
    if explicit_throughput is not None:
        lines.append(f"| **Peak Throughput** | {explicit_throughput:,} RPS | Sustained peak load |")
    if explicit_users is not None:
        lines.append(f"| **Concurrent Users** | {explicit_users:,} | Peak simultaneous sessions |")
    lines.append("")

    # Provider-specific SLAs
    lines.append("### Provider-Specific Commitments\n")
    lines.append(
        "| Provider | Compute SLA | Database SLA | Support Tier | SLA Credit |\n"
        "|----------|-------------|-------------|-------------|------------|"
    )
    provider_slas = {
        "aws": ("99.99% (multi-AZ)", "99.95% (Multi-AZ RDS)", "Business/Enterprise", "10%–30% of monthly bill"),
        "azure": ("99.99% (Availability Zones)", "99.99% (Zone Redundant)", "Standard/Premier", "10%–25% of monthly bill"),
        "gcp": ("99.99% (multi-region)", "99.95% (HA Cloud SQL)", "Standard/Enhanced", "10%–50% of monthly bill"),
    }

    for pb in sorted(cost_comparison.providers, key=lambda p: p.total_monthly_usd):
        prov = pb.provider.value
        psla = provider_slas.get(prov, ("99.9%", "99.9%", "Standard", "10%"))
        lines.append(
            f"| {prov.upper()} | {psla[0]} | {psla[1]} | {psla[2]} | {psla[3]} |"
        )
    lines.append("")

    # SLA measurement methodology
    lines.append("### Measurement & Reporting\n")
    lines.append(
        "- **Measurement Method**: Availability is measured as the percentage of "
        "5-minute intervals in a calendar month during which the service is "
        "operational, excluding scheduled maintenance windows.\n"
        "- **Monitoring**: End-to-end synthetic transaction monitoring from "
        "at least 3 geographic regions; alerts triggered at < 99% availability "
        "in a 15-minute window.\n"
        "- **Reporting Cadence**: Monthly SLA reports delivered within 5 business "
        "days of month-end, including incident root-cause analysis for any "
        "availability breaches.\n"
        "- **Penalty Clauses**: SLA credits are applied as service credits to the "
        "next billing cycle; they do not exceed the total monthly service charge.\n"
        "- **Exclusions**: Force majeure events, user-initiated actions, third-party "
        "network failures outside the provider's backbone.\n"
    )

    return "\n".join(lines) + "\n"


def _build_security_section(
    workload_request: WorkloadRequest,
    compliance_report: ComplianceReport,
) -> str:
    """Build the security architecture section.

    Args:
        workload_request: Original request.
        compliance_report: WAF compliance results.

    Returns:
        Markdown section string.
    """
    lines = ["## 10. Security Architecture\n"]
    lines.append(
        "The security architecture follows a defence-in-depth approach aligned "
        "with the NIST Cybersecurity Framework and the cloud provider's "
        "Well-Architected security pillar.\n"
    )

    # Identity & Access Management
    lines.append("### Identity & Access Management (IAM)\n")
    lines.append(
        "| Control | Implementation | Standard |\n"
        "|---------|----------------|----------|\n"
        "| Authentication | Multi-Factor Authentication (MFA) enforced for all human identities | NIST 800-63B |\n"
        "| Service identities | Managed identities / IAM roles (no long-lived access keys) | CIS Benchmark |\n"
        "| Least privilege | IAM policies scoped to minimum required actions and resources | CIS Benchmark |\n"
        "| Privilege escalation | Separate break-glass accounts with usage alerting | NIST 800-53 |\n"
        "| Secrets management | All secrets stored in provider KMS / HashiCorp Vault | OWASP A02 |\n"
        "| Access reviews | Quarterly automated access certification reviews | SOC 2 CC6.1 |\n"
    )

    # Encryption
    lines.append("### Encryption\n")
    lines.append(
        "| Scope | Algorithm | Key Management |\n"
        "|-------|-----------|---------------|\n"
        "| Data at rest (databases) | AES-256 | Provider-managed KMS with BYOK option |\n"
        "| Data at rest (object storage) | AES-256-SSE | Provider-managed KMS |\n"
        "| Data in transit (external) | TLS 1.3 (TLS 1.2 minimum) | Certificate managed via ACM / Key Vault / GCM |\n"
        "| Data in transit (internal) | mTLS via service mesh | Per-workload certificate rotation (90-day) |\n"
        "| Encryption keys | AES-256-GCM | Annual rotation; HSM-backed for mission-critical |\n"
    )

    # Network security
    lines.append("### Network Security\n")
    lines.append(
        "| Control | Description |\n"
        "|---------|-------------|\n"
        "| WAF | OWASP CRS 3.3+; blocks SQLi, XSS, SSRF, Log4Shell signatures |\n"
        "| DDoS Protection | Layer 3/4 network DDoS mitigation (always-on); L7 rate limiting |\n"
        "| VPC isolation | All workloads in private subnets; no public IPs on compute |\n"
        "| Security Groups / NSG | Default-deny; ingress only from load balancer CIDRs |\n"
        "| Private endpoints | Databases accessible only via VPC private endpoints |\n"
        "| Network segmentation | App subnet isolated from data subnet via NACLs/NSGs |\n"
    )

    # Observability & Audit
    lines.append("### Security Observability & Audit Logging\n")
    lines.append(
        "- **CloudTrail / Activity Log / Cloud Audit Logs**: All API calls logged "
        "with actor, action, resource, timestamp, and source IP.\n"
        "- **Log retention**: Security logs retained for 1 year (hot) + 6 years (cold archive) "
        "per PCI DSS requirement 10.7.\n"
        "- **SIEM integration**: Log streams forwarded to SIEM (Splunk / Sentinel / Chronicle) "
        "within 5 minutes of event occurrence.\n"
        "- **Anomaly detection**: Cloud-native threat detection (GuardDuty / Defender / "
        "Security Command Center) enabled with high-severity alerting.\n"
        "- **Vulnerability scanning**: Container images scanned at build time (Trivy/Prisma); "
        "hosts scanned weekly (Inspector / Qualys).\n"
        "- **Penetration testing**: Annual third-party penetration test; findings remediated "
        "within SLA (Critical: 24h, High: 7 days, Medium: 30 days).\n"
    )

    # WAF compliance integration
    failed_checks = [c for c in compliance_report.checks if not c.passed]
    if failed_checks:
        lines.append("### Open Security Findings (WAF Compliance)\n")
        lines.append(
            "The following items from the Well-Architected compliance review "
            "require remediation before production go-live:\n"
        )
        lines.append(
            "| Pillar | Check | Severity | Remediation |\n"
            "|--------|-------|----------|-------------|"
        )
        for c in failed_checks[:10]:
            lines.append(
                f"| {c.pillar} | {c.check_name} | {c.severity.upper()} | "
                f"{c.recommendation[:100]} |"
            )
    else:
        lines.append(
            "> ✅ All WAF security pillar checks passed. No open security findings.\n"
        )

    return "\n".join(lines) + "\n"


def _build_migration_section(
    workload_profile: WorkloadProfile,
    workload_request: WorkloadRequest,
) -> str:
    from src.models.cloud_resource import ServiceCategory

    is_greenfield = _is_greenfield_project(workload_request)
    is_gov = _is_government_scenario(workload_request)

    lines = ["## 11. Delivery Plan\n"]

    if is_greenfield:
        # Gap 10c — contract-aligned phases for greenfield / public-sector projects
        frame = (
            "This solution follows a contract-aligned delivery approach structured "
            "into four phases aligned to stakeholder acceptance gates and go-live milestones."
            if is_gov else
            "This greenfield deployment follows an iterative delivery approach with "
            "clear milestone gates at each phase boundary."
        )
        lines.append(frame + "  All timelines assume a dedicated team of 3–5 engineers.\n")

        lines.append("### Programme Timeline\n")
        lines.append(
            "| Phase | Name | Duration | Key Deliverables |\n"
            "|-------|------|----------|------------------|\n"
            "| 1 | MVP — Core Architecture | Weeks 1–6 | Infrastructure provisioned, core services live, basic UI functional |\n"
            "| 2 | UAT — Full Feature Set | Weeks 7–14 | All features complete, user acceptance testing, accessibility audit |\n"
            "| 3 | Public Launch | Weeks 15–18 | Production go-live, performance validation, monitoring active |\n"
            "| 4 | Managed Operations | Ongoing | SLA enforcement, capacity reviews, quarterly security audits |\n"
        )

        lines.append("### Phase 1 — MVP: Core Architecture (Weeks 1–6)\n")
        lines.append(
            "**Objective**: Provision foundational infrastructure and deliver a working MVP with core functionality.\n\n"
            "| Activity | Owner | Exit Criterion |\n"
            "|----------|-------|---------------|\n"
            "| Cloud account provisioning & IAM baseline | Cloud Platform Team | Accounts with policies applied |\n"
            "| Core networking (VPC, subnets, NAT, DNS) | Network Engineer | Connectivity tests pass |\n"
            "| Managed database provisioning (primary + replica) | DBA | Database reachable, schema applied |\n"
            "| Application container deployment | DevOps | Services healthy, basic API functional |\n"
            "| CI/CD pipelines operational | DevOps | Automated deploy to staging on merge |\n"
            "| Core UI / mobile app build | Frontend / Mobile Team | App installs and authenticates |\n"
        )

        lines.append("### Phase 2 — UAT: Full Feature Set (Weeks 7–14)\n")
        lines.append(
            "**Objective**: Complete all features, conduct user acceptance testing, and satisfy compliance gates.\n\n"
            "| Activity | Owner | Exit Criterion |\n"
            "|----------|-------|---------------|\n"
            "| Full feature development complete | Engineering | All acceptance criteria pass |\n"
            "| Load & performance testing | QA / SRE | P99 latency within SLA targets |\n"
            "| Security penetration testing | Security Architect | No critical/high findings open |\n"
            "| Accessibility audit (WCAG 2.2 AA) | UX / QA | Screen reader + contrast review complete |\n"
            "| Stakeholder UAT sign-off | Product Owner / Sponsor | UAT sign-off documented |\n"
            "| Documentation & runbooks | Tech Lead | All runbooks peer-reviewed |\n"
        )

        lines.append("### Phase 3 — Public Launch (Weeks 15–18)\n")
        lines.append(
            "**Objective**: Production go-live with full traffic and monitoring in place.\n\n"
            "| Milestone | Description |\n"
            "|-----------|-------------|\n"
            "| Pre-launch smoke test | All critical user journeys validated in production env |\n"
            "| DNS / App Store go-live | Public endpoint activated; app published to stores |\n"
            "| Monitoring & alerting active | Error rates, latency, and capacity dashboards reviewed |\n"
            "| Post-launch hypercare | 10-business-day enhanced support with on-call escalation |\n"
        )

        lines.append("### Phase 4 — Managed Operations (Ongoing)\n")
        lines.append(
            "**Objective**: SLA-backed operations with regular review cadence.\n\n"
            "- **Monthly**: Cost review (FinOps), capacity planning, security patch cycle\n"
            "- **Quarterly**: SLA performance report, WAF rule update, DR drill\n"
            "- **Annually**: Architecture review, compliance re-assessment, contract renewal\n"
        )

    else:
        # Existing migration-frame phases
        lines.append(
            "The migration follows the AWS CAF / Azure CAF migration framework, "
            "structured into four phases with clear entry and exit criteria.  "
            "All timelines assume a dedicated migration team of 3–5 engineers.\n"
        )

        lines.append("### Programme Timeline\n")
        lines.append(
            "| Phase | Name | Duration | Key Deliverables |\n"
            "|-------|------|----------|------------------|\n"
            "| 1 | Discovery & Planning | Weeks 1–4 | Architecture design, runbooks, cost sign-off |\n"
            "| 2 | Foundation Build | Weeks 5–10 | VPC, IAM, networking, CI/CD pipelines |\n"
            "| 3 | Application Migration | Weeks 11–18 | Service-by-service migration with parallel run |\n"
            "| 4 | Production Cutover | Weeks 19–22 | DNS cutover, smoke tests, hypercare |\n"
        )

        lines.append("### Phase 1 — Discovery & Planning (Weeks 1–4)\n")
        lines.append(
            "**Objective**: Validate architecture, finalise SKU selections, and establish governance.\n\n"
            "| Activity | Owner | Exit Criterion |\n"
            "|----------|-------|---------------|\n"
            "| Workload dependency mapping | Solutions Architect | Dependency graph approved |\n"
            "| Cloud account provisioning | Cloud Platform Team | Accounts with SCPs/Policy applied |\n"
            "| Network design finalisation | Network Engineer | VPC CIDR plan signed off |\n"
            "| Security baseline definition | Security Architect | Security baseline document approved |\n"
            "| Cost sign-off | FinOps / Procurement | PO raised with cloud provider |\n"
            "| Runbook authoring | Migration Lead | All runbooks peer-reviewed |\n"
        )

        lines.append("### Phase 2 — Foundation Build (Weeks 5–10)\n")
        lines.append(
            "**Objective**: Provision core infrastructure so application teams can begin migration in parallel.\n\n"
            "| Activity | Owner | Exit Criterion |\n"
            "|----------|-------|---------------|\n"
            "| VPC / VNet + subnets provisioned | Platform Engineer | Connectivity tests pass |\n"
            "| IAM roles and policies applied | Security Architect | Access review approved |\n"
            "| CI/CD pipelines operational | DevOps Engineer | Canary deployment successful |\n"
            "| Monitoring & alerting configured | SRE | Dashboards reviewed and signed off |\n"
            "| DNS & certificate management | Network Engineer | Certificates issued and rotated |\n"
        )

        lines.append("### Phase 3 — Application Migration (Weeks 11–18)\n")
        lines.append(
            "**Objective**: Migrate each service component with zero data loss, "
            "running in parallel until parity is confirmed.\n\n"
            "Migration order (least-dependent → most-dependent):\n"
        )

        ordered_categories = [
            (ServiceCategory.STORAGE, "Object storage migration"),
            (ServiceCategory.DATABASE, "Database migration (DMS / pg_dump / logical replication)"),
            (ServiceCategory.CONTAINER, "Container workload deployment"),
            (ServiceCategory.COMPUTE, "VM migration (re-platform or lift-and-shift)"),
            (ServiceCategory.AI_ML, "AI/ML model and pipeline migration"),
            (ServiceCategory.SERVERLESS_FUNCTION, "Serverless function re-deployment"),
        ]

        step = 1
        for cat, description in ordered_categories:
            comps = [c for c in workload_profile.components if c.resolved_category == cat]
            if comps:
                lines.append(
                    f"{step}. **{description}**: "
                    + ", ".join(c.workload_name for c in comps[:3])
                )
                step += 1

        lines.append("\n")
        lines.append(
            "Each service runs in **parallel mode** (traffic mirrored or split) for "
            "a minimum of 5 business days before traffic is switched over.  "
            "Rollback procedure restores DNS to origin within 10 minutes.\n"
        )

        lines.append("### Phase 4 — Cutover & Hypercare (Weeks 19–22)\n")
        lines.append(
            "**Objective**: Full traffic cutover with 2-week hypercare period.\n\n"
            "| Milestone | Description |\n"
            "|-----------|-------------|\n"
            "| Pre-cutover smoke test | All critical user journeys validated in new env |\n"
            "| DNS TTL reduction | 48 hours before cutover: TTL reduced to 60 seconds |\n"
            "| Cutover window | Scheduled low-traffic period (weekend 02:00–06:00 UTC) |\n"
            "| Post-cutover validation | Error rates, latency, and database integrity checks |\n"
            "| Hypercare period | 10-business-day enhanced support with on-call escalation |\n"
            "| Legacy decommission | After 30-day monitoring, decommission source environment |\n"
        )

        lines.append("### Rollback Strategy\n")
        lines.append(
            "- **Trigger**: Error rate > 1% above baseline OR p99 latency > 2× baseline.\n"
            "- **Decision**: On-call SRE authorises rollback; no approval chain required during cutover.\n"
            "- **Execution**: DNS re-pointed to origin; maximum rollback duration ≤ 10 minutes.\n"
            "- **Data reconciliation**: Any writes to new environment replicated back to origin "
            "via database triggers before DNS switch.\n"
        )

    return "\n".join(lines) + "\n"


def _build_dr_section(
    workload_request: WorkloadRequest,
    cost_comparison: CostComparison,
) -> str:
    """Build the disaster recovery and business continuity section.

    Uses explicit ``rpo_minutes``/``rto_minutes`` from workload requirements
    when available, falling back to tier-based ``_SLA_TARGETS`` defaults.

    Args:
        workload_request: Original request.
        cost_comparison: FinOps comparison.

    Returns:
        Markdown section string.
    """
    tier = workload_request.tier.value.lower()
    dr = _DR_STRATEGY.get(tier, _DR_STRATEGY["production"])
    tier_sla = _SLA_TARGETS.get(tier, _SLA_TARGETS["production"])

    workloads = workload_request.workloads
    explicit_rpo_min = min(
        (w.rpo_minutes for w in workloads if w.rpo_minutes is not None),
        default=None,
    )
    explicit_rto_min = min(
        (w.rto_minutes for w in workloads if w.rto_minutes is not None),
        default=None,
    )

    def _fmt_minutes(minutes: int | None, fallback: str) -> str:
        if minutes is None:
            return fallback
        if minutes == 0:
            return "Zero (continuous replication)"
        if minutes < 60:
            return f"< {minutes} min"
        h, m = divmod(minutes, 60)
        return f"< {h}h {m:02d}m" if m else f"< {h}h"

    rpo_str = _fmt_minutes(explicit_rpo_min, tier_sla["rpo"])
    rto_str = _fmt_minutes(explicit_rto_min, tier_sla["rto"])

    lines = ["## 12. Disaster Recovery & Business Continuity\n"]
    lines.append(
        f"The DR strategy for **{tier.replace('_', ' ').title()}** tier workloads "
        f"follows the **{dr['pattern']}** pattern.\n"
    )

    # DR targets
    lines.append("### DR Targets\n")
    lines.append(
        "| Metric | Target | Rationale |\n"
        "|--------|--------|----------|\n"
        f"| **DR Pattern** | {dr['pattern']} | Tier-appropriate resilience |\n"
        f"| **Recovery Point Objective (RPO)** | {rpo_str} | Maximum tolerable data loss |\n"
        f"| **Recovery Time Objective (RTO)** | {rto_str} | Time to restore service |\n"
        f"| **Snapshot Frequency** | {dr['snapshot_frequency']} | Aligns with RPO target |\n"
        f"| **Backup Retention** | {dr['retention']} | Regulatory + operational needs |\n"
    )

    # Backup strategy
    lines.append("### Backup Strategy\n")
    lines.append(
        "| Component | Method | Frequency | Storage | Verification |\n"
        "|-----------|--------|-----------|---------|-------------|\n"
        f"| Database | {dr['database_backup']} | {dr['snapshot_frequency']} | Encrypted object storage, cross-region | Weekly restore test |\n"
        "| Application config | Git-versioned IaC (Terraform/ARM/CDM) | On every commit | Source control + encrypted S3/Blob/GCS | On PR merge |\n"
        "| Container images | Immutable image tags in container registry | On every build | Multi-region replication | Trivy scan on push |\n"
        "| Secrets | Automated KMS backup with replication | Daily | Cross-region KMS replica | Access verification |\n"
        "| Persistent volumes | Volume snapshots | {snapshot_freq} | Same region + cross-region copy | Monthly restore drill |\n".format(
            snapshot_freq=dr['snapshot_frequency'],
        )
    )

    # Failover procedure
    lines.append("### Failover Procedure\n")
    lines.append(f"{dr['failover']}\n")
    lines.append(
        "**Failover Runbook (abbreviated)**:\n"
        "1. On-call SRE acknowledges alert within SLA response time\n"
        "2. Automated health check confirms primary region failure (3 consecutive failures)\n"
        "3. DR runbook triggered: DNS weight shifted to DR region (or automatic for active-active)\n"
        "4. Database failover verified: replica promoted or read traffic redirected\n"
        "5. Application warm-up period monitored (latency + error rate)\n"
        "6. Stakeholder notification within 15 minutes of failover declaration\n"
        "7. Post-incident review within 48 hours\n"
    )

    # DR test cadence
    lines.append("### Test Cadence\n")
    lines.append(f"**Scheduled DR Drills**: {dr['test_cadence']}\n")
    lines.append(
        "Each DR test includes:\n"
        "- Full database restore from backup to isolated environment\n"
        "- Smoke test of all critical application endpoints\n"
        "- Measurement and documentation of actual RTO/RPO achieved\n"
        "- Update of runbooks based on findings\n"
    )

    return "\n".join(lines) + "\n"


def _build_tco_section(
    cost_comparison: CostComparison,
    kpis: dict[str, Any],
) -> str:
    """Build the multi-year Total Cost of Ownership section.

    Args:
        cost_comparison: FinOps comparison.
        kpis: KPI dict from state (may contain tco_projections).

    Returns:
        Markdown section string.
    """
    lines = ["## 8. Multi-Year Total Cost of Ownership\n"]

    tco_projections = kpis.get("tco_projections", {})
    growth_pct = tco_projections.get("growth_pct_per_year", 15.0)

    lines.append(
        f"TCO projections assume **{growth_pct:.0f}% annual cost growth** "
        f"(driven by data volume growth, additional features, and scaling). "
        f"Reserved instance commitments are modelled against on-demand baselines.  \n"
        f"All figures are in USD.\n"
    )

    # On-demand multi-year
    lines.append("### On-Demand TCO Projection\n")
    lines.append(
        "| Provider | Monthly | Year 1 | Year 2 | Year 3 | Year 5 |\n"
        "|----------|---------|--------|--------|--------|--------|\n"
    )

    for pb in sorted(cost_comparison.providers, key=lambda p: p.total_monthly_usd):
        monthly = pb.total_monthly_usd
        g = 1 + growth_pct / 100.0
        yr1 = monthly * 12
        yr2 = yr1 * g
        yr3 = yr1 * g ** 2 + yr2
        # Actually compute cumulative
        cum1 = monthly * 12
        cum2 = cum1 + monthly * 12 * g
        cum3 = cum2 + monthly * 12 * g ** 2
        cum5 = cum3 + monthly * 12 * g ** 3 + monthly * 12 * g ** 4
        lines.append(
            f"| {pb.provider.value.upper()} "
            f"| ${monthly:,.0f} "
            f"| ${cum1:,.0f} "
            f"| ${cum2:,.0f} "
            f"| ${cum3:,.0f} "
            f"| ${cum5:,.0f} |"
        )
    lines.append("")

    # RI savings
    ri_providers = [pb for pb in cost_comparison.providers if pb.reserved_3yr_monthly_usd]
    if ri_providers:
        lines.append("### 3-Year Reserved Instance TCO\n")
        lines.append(
            "| Provider | RI-1yr Monthly | RI-1yr 3yr TCO | RI-3yr Monthly | RI-3yr 3yr TCO | Total Savings (RI-3yr vs On-Demand) |\n"
            "|----------|---------------|---------------|---------------|---------------|------------------------------------|\n"
        )
        for pb in sorted(cost_comparison.providers, key=lambda p: p.total_monthly_usd):
            ri1_m = pb.reserved_1yr_monthly_usd
            ri3_m = pb.reserved_3yr_monthly_usd
            on_dem_3yr = sum(
                pb.total_monthly_usd * 12 * (1 + growth_pct / 100.0) ** yr
                for yr in range(3)
            )
            ri1_3yr = sum(
                (ri1_m or pb.total_monthly_usd) * 12 * (1 + growth_pct / 100.0) ** yr
                for yr in range(3)
            )
            ri3_3yr = sum(
                (ri3_m or pb.total_monthly_usd) * 12 * (1 + growth_pct / 100.0) ** yr
                for yr in range(3)
            )
            savings_usd = on_dem_3yr - ri3_3yr
            lines.append(
                f"| {pb.provider.value.upper()} "
                f"| {'${:,.0f}'.format(ri1_m) if ri1_m else 'N/A'} "
                f"| ${ri1_3yr:,.0f} "
                f"| {'${:,.0f}'.format(ri3_m) if ri3_m else 'N/A'} "
                f"| ${ri3_3yr:,.0f} "
                f"| **${savings_usd:,.0f}** |"
            )
        lines.append("")

    # Cost optimisation recommendations
    lines.append("### Cost Optimisation Roadmap\n")
    lines.append(
        "| Horizon | Recommendation | Estimated Saving |\n"
        "|---------|----------------|------------------|\n"
        "| Immediate (Day 0) | Right-size using Sizer recommendations | Baseline established |\n"
        "| 30 days | Enable auto-scaling on all stateless workloads | 10–20% compute cost |\n"
        "| 90 days | Convert to 1-year Reserved Instances / CUDs | ~30% compute cost |\n"
        "| 6 months | Identify and remove unused snapshots + EBS volumes | 5–10% storage cost |\n"
        "| 1 year | Convert long-running workloads to 3-year RIs | ~45% compute cost |\n"
        "| Ongoing | Weekly cost anomaly review; monthly right-sizing review | Prevents cost drift |\n"
    )

    return "\n".join(lines) + "\n"


def _build_certifications_section(
    cost_comparison: CostComparison,
    workload_request: WorkloadRequest,
) -> str:
    lines = ["## 13. Compliance & Certifications\n"]
    lines.append(
        "All shortlisted cloud providers maintain the following compliance "
        "certifications relevant to this procurement. Certificates are available "
        "on request from the respective provider's compliance portals.\n"
    )

    for pb in sorted(cost_comparison.providers, key=lambda p: p.total_monthly_usd):
        prov = pb.provider.value
        certs = _PROVIDER_CERTIFICATIONS.get(prov, [])
        lines.append(f"### {prov.upper()}\n")
        if certs:
            lines.append(
                "| Certification | Issuing Body |\n"
                "|---------------|--------------|\n"
            )
            for cert, body in certs:
                lines.append(f"| {cert} | {body} |")
        lines.append("")

    # Shared responsibility
    lines.append("### Shared Responsibility Model\n")
    lines.append(
        "Cloud security operates under a **shared responsibility** model:\n\n"
        "| Layer | Cloud Provider Responsible | Customer Responsible |\n"
        "|-------|---------------------------|---------------------|\n"
        "| Physical infrastructure | ✅ Data centres, hardware, hypervisor | ❌ |\n"
        "| Managed services (RDS, GKE, AKS) | ✅ Patching, availability | ❌ |\n"
        "| Operating system (IaaS) | ❌ | ✅ Patching, hardening |\n"
        "| Application runtime | ❌ | ✅ SDLC, dependency management |\n"
        "| Data classification | ❌ | ✅ Encryption keys, access control |\n"
        "| IAM & identity | ❌ | ✅ User management, MFA enforcement |\n"
        "| Network configuration | Partial (VPC primitives) | ✅ Security group rules, ACLs |\n"
    )

    # Gap 10f — WCAG/StateRAMP specifics
    frameworks = [f.lower().replace(" ", "-") for f in workload_request.compliance_frameworks]

    has_wcag = any("wcag" in f for f in frameworks)
    has_stateramp = any("stateramp" in f for f in frameworks)
    has_fedramp = any("fedramp" in f for f in frameworks)
    has_hipaa = any("hipaa" in f for f in frameworks)
    has_cjis = any("cjis" in f for f in frameworks)

    if has_wcag:
        lines.append("### WCAG 2.2 Level AA — Implementation Requirements\n")
        lines.append(
            "The application must meet Web Content Accessibility Guidelines (WCAG) 2.2 Level AA. "
            "The following success criteria apply to all user-facing interfaces:\n\n"
            "| Principle | Criterion | Implementation |\n"
            "|-----------|-----------|---------------|\n"
            "| **Perceivable** | 1.4.3 Contrast (Minimum) | Text/background contrast ratio ≥ 4.5:1 (normal text), ≥ 3:1 (large text) |\n"
            "| **Perceivable** | 1.1.1 Non-text Content | All images have descriptive `alt` attributes; icons have ARIA labels |\n"
            "| **Perceivable** | 1.3.1 Info & Relationships | Semantic HTML5 elements; ARIA roles for dynamic content |\n"
            "| **Perceivable** | 1.4.4 Resize text | UI fully functional at 200% browser zoom without horizontal scroll |\n"
            "| **Operable** | 2.1.1 Keyboard | All functionality operable via keyboard; no keyboard traps |\n"
            "| **Operable** | 2.4.3 Focus Order | Logical tab order throughout all workflows |\n"
            "| **Operable** | 2.4.7 Focus Visible | Visible focus indicator on all interactive elements |\n"
            "| **Understandable** | 3.1.1 Language of Page | `lang` attribute on `<html>` element |\n"
            "| **Understandable** | 3.3.1 Error Identification | Form errors described in text, not by colour alone |\n"
            "| **Robust** | 4.1.2 Name, Role, Value | All UI components have accessible name and role |\n"
            "| **Robust** | 4.1.3 Status Messages | Screen readers notified of status changes via ARIA live regions |\n\n"
            "**Testing approach**: Automated (axe-core / Lighthouse), manual screen reader testing "
            "(VoiceOver on macOS/iOS, NVDA on Windows, TalkBack on Android), and manual keyboard navigation audit.\n"
        )

    if has_stateramp:
        lines.append("### StateRAMP Moderate — Authorization Requirements\n")
        lines.append(
            "StateRAMP Moderate authorization requires compliance with **325 security controls** "
            "based on NIST SP 800-53 Revision 5. The following summarises the authorization pathway:\n\n"
            "| Step | Activity | Owner | Timeline |\n"
            "|------|----------|-------|----------|\n"
            "| 1 | Select and engage a StateRAMP-authorised 3PAO (Third-Party Assessment Organization) | Vendor | Month 1 |\n"
            "| 2 | Develop System Security Plan (SSP) covering all 325 Moderate controls | Vendor + 3PAO | Months 1–3 |\n"
            "| 3 | Implement and document all required controls | Engineering / Security | Months 2–5 |\n"
            "| 4 | 3PAO security assessment (SAR — Security Assessment Report) | 3PAO | Months 4–6 |\n"
            "| 5 | Submit Authorization Package to StateRAMP PMO | Vendor | Month 6 |\n"
            "| 6 | StateRAMP PMO review and Authorization to Operate (ATO) grant | StateRAMP PMO | Months 7–9 |\n"
            "| 7 | Continuous Monitoring (ConMon) — monthly vulnerability scans, annual re-assessment | Vendor + 3PAO | Ongoing |\n\n"
            "**Control baseline highlights** (NIST 800-53 Rev 5 Moderate):\n\n"
            "- **AC — Access Control**: Role-based access, least privilege, session timeouts, MFA required\n"
            "- **AU — Audit and Accountability**: Tamper-evident audit logs, 90-day retention minimum\n"
            "- **IA — Identification and Authentication**: MFA for all privileged access, PIV/CAC support\n"
            "- **SC — System and Communications Protection**: FIPS 140-2 validated encryption, TLS 1.2+\n"
            "- **SI — System and Information Integrity**: AV/EDR on all endpoints, patch SLAs (30-day critical)\n"
            "- **IR — Incident Response**: IR plan, 1-hour detection-to-notification for critical incidents\n"
        )

    if has_fedramp:
        lines.append("### FedRAMP Moderate — Key Requirements\n")
        lines.append(
            "- **NIST 800-53 Rev 5 Moderate baseline**: 325 controls across 18 control families\n"
            "- **Authorization types**: Agency ATO or Joint Authorization Board (JAB) P-ATO\n"
            "- **3PAO assessment**: Required; must be A2LA or NVLAP accredited\n"
            "- **Continuous monitoring**: Monthly vulnerability scans, annual pen test, POA&M tracking\n"
            "- **Data encryption**: FIPS 140-2 validated modules required; data at rest and in transit\n"
            "- **Incident reporting**: US-CERT notification within 1 hour of confirmed breach\n"
        )

    if has_hipaa:
        lines.append("### HIPAA / HITECH — Implementation Controls\n")
        lines.append(
            "- **PHI encryption**: AES-256 at rest, TLS 1.2+ in transit — all cloud providers offer HIPAA-eligible services\n"
            "- **BAA**: Business Associate Agreement required with all cloud providers handling PHI\n"
            "- **Audit logging**: All PHI access events logged with user, timestamp, and action\n"
            "- **Minimum necessary**: Role-based access; staff access limited to minimum data needed\n"
            "- **Breach notification**: 60-day notification to HHS and affected individuals (HITECH)\n"
        )

    if has_cjis:
        lines.append("### CJIS Security Policy — Requirements\n")
        lines.append(
            "- **Encryption**: FIPS 140-2 AES-128 minimum for CJI data at rest and in transit\n"
            "- **MFA**: Required for all remote access to systems containing CJI\n"
            "- **Personnel screening**: FBI fingerprint-based background checks for all personnel with CJI access\n"
            "- **Audit logging**: All CJI access logged; logs retained for minimum 3 years\n"
            "- **System hardening**: CIS Benchmark Level 2 hardening for all systems hosting CJI\n"
        )

    # General project-specific compliance
    if not any([has_wcag, has_stateramp, has_fedramp, has_hipaa, has_cjis]):
        lines.append("### Project-Specific Compliance Requirements\n")
        comp_reqs = workload_request.compliance_frameworks
        if comp_reqs and comp_reqs != ["waf"]:
            lines.append("Compliance frameworks identified from project requirements:\n")
            for req in comp_reqs:
                lines.append(f"- **{req}**: Applicable controls implemented per provider baseline.")
        else:
            lines.append(
                "No specific compliance frameworks stated beyond WAF. "
                "Recommend confirming data residency and GDPR applicability with legal counsel.\n"
            )

    return "\n".join(lines) + "\n"


def _build_assumptions_section(
    workload_request: WorkloadRequest,
    workload_profile: WorkloadProfile,
) -> str:
    """Build the assumptions and exclusions section.

    Args:
        workload_request: Original request.
        workload_profile: Profiler output.

    Returns:
        Markdown section string.
    """
    lines = ["## 15. Assumptions & Exclusions\n"]

    lines.append("### Assumptions\n")
    lines.append(
        "The following assumptions have been made in preparing this procurement document. "
        "Any deviation may require reassessment of cost and architecture recommendations:\n"
    )

    assumptions = [
        ("Network connectivity", "Reliable internet connectivity (≥ 1 Gbps) to the selected cloud provider's region is available or can be provisioned."),
        ("Data transfer baseline", "Outbound data transfer estimated at 100 GB/month unless otherwise stated. Significant increases will affect networking costs."),
        ("Workload stability", "Workload characteristics (vCPU, memory, storage) remain within ±30% of specified values. Major changes require re-sizing."),
        ("Licensing", "Third-party software licences (OS, database commercial editions, ISV software) are not included in cloud cost estimates unless explicitly stated."),
        ("Migration tooling", "Cloud provider native migration tools (DMS, Azure Migrate, Migrate for Compute Engine) will be used unless custom tooling is required."),
        ("CI/CD pipelines", "Existing CI/CD pipelines can be adapted to target cloud provider infrastructure with ≤ 2 weeks of engineering effort."),
        ("Team skills", "The operations team will receive provider-specific training (Solutions Architect or DevOps Engineer level) within 60 days of project kickoff."),
        ("Growth rate", "Annual infrastructure cost growth of 15% is assumed unless the organisation provides a more accurate forecast."),
        ("Compliance baseline", "The project is not subject to FedRAMP High or ITAR unless explicitly stated."),
    ]

    lines.append(
        "| # | Assumption | Detail |\n"
        "|---|-----------|--------|\n"
    )
    for i, (title, detail) in enumerate(assumptions, 1):
        lines.append(f"| {i} | **{title}** | {detail} |")
    lines.append("")

    lines.append("### Exclusions\n")
    exclusions = [
        "End-user device procurement, VPN client licences, and physical networking equipment",
        "Third-party SaaS tools (monitoring, logging, APM) unless hosted on target cloud",
        "Application development, refactoring, or re-architecting costs",
        "Staff recruitment, training, or change management costs",
        "Data egress costs resulting from migration (one-time transfer)",
        "Disaster recovery costs for non-production environments",
        "Multi-cloud management plane (e.g., Anthos, Azure Arc) unless explicitly scoped",
        "Professional services from cloud provider (engagement priced separately)",
    ]
    for excl in exclusions:
        lines.append(f"- {excl}")
    lines.append("")

    lines.append("### Out-of-Scope Items\n")
    lines.append(
        "The following items are out of scope for this procurement but should be "
        "addressed in separate work packages:\n"
        "- Application performance testing and load testing tooling\n"
        "- FinOps tooling implementation (CloudHealth, Apptio, Spot.io)\n"
        "- Data governance and cataloguing (AWS Glue Catalog, Azure Purview)\n"
        "- Internal developer platform / service mesh configuration\n"
    )

    return "\n".join(lines) + "\n"



_EXECUTIVE_SUMMARY_PROMPT = """\
You are drafting an executive summary for a formal cloud infrastructure \
procurement document. Write a substantive 4–6 paragraph summary that covers:

1. **Project context**: Project name, purpose, and key infrastructure requirements \
(components, vCPU, memory, scale expectations).
2. **Recommendation**: The recommended cloud provider, monthly and annual cost, \
and why it is the best fit (cost, availability, compliance, ecosystem).
3. **Cost optimisation**: Reserved instance / committed use discount savings \
available (1-year and 3-year), and estimated 3-year and 5-year TCO.
4. **Compliance & security**: WAF compliance score, key compliance certifications \
relevant to the project, and security posture highlights.
5. **Risk & next steps**: Key risks (vendor lock-in, skill gap, data transfer), \
recommended mitigations, and immediate procurement actions.

Target audience: IT Director / CTO / Procurement team. Use professional, \
formal language. Be specific about dollar amounts and percentages. \
Do NOT use markdown headers within the summary. Use plain paragraphs separated \
by blank lines.
"""


@observe()
async def _generate_executive_summary(
    llm: BaseChatModel,
    workload_request: WorkloadRequest,
    workload_profile: WorkloadProfile,
    cost_comparison: CostComparison,
    compliance_report: ComplianceReport,
) -> str:
    """Generate an executive summary using the LLM.

    Args:
        llm: The LLM model (BaseChatModel interface).
        workload_request: Original request.
        workload_profile: Profiler output.
        cost_comparison: FinOps comparison.
        compliance_report: WAF report.

    Returns:
        Executive summary string.
    """
    log = logger.bind(agent="rfp_writer", step="executive_summary")
    log.debug("executive_summary_started")

    provider_costs = {
        pb.provider.value: {
            "monthly_usd": pb.total_monthly_usd,
            "annual_usd": pb.total_annual_usd,
            "ri_1yr_savings_pct": pb.reserved_1yr_savings_pct,
            "ri_3yr_savings_pct": pb.reserved_3yr_savings_pct,
            "spot_savings_pct": pb.spot_savings_pct,
        }
        for pb in cost_comparison.providers
    }

    user_content = json.dumps({
        "project_name": workload_request.project_name,
        "environment": workload_request.environment.value,
        "tier": workload_request.tier.value,
        "total_vcpus": workload_profile.total_vcpus,
        "total_memory_gb": workload_profile.total_memory_gb,
        "total_storage_gb": workload_profile.total_storage_gb,
        "component_count": len(workload_profile.components),
        "components": [
            {
                "name": c.workload_name,
                "category": c.resolved_category.value,
                "vcpus": c.estimated_vcpus,
                "memory_gb": c.estimated_memory_gb,
            }
            for c in workload_profile.components[:8]
        ],
        "budget_monthly_usd": workload_request.budget_monthly_usd,
        "cheapest_provider": (
            cost_comparison.cheapest_provider.value
            if cost_comparison.cheapest_provider else "N/A"
        ),
        "savings_vs_most_expensive_pct": cost_comparison.savings_vs_most_expensive_pct,
        "provider_costs": provider_costs,
        "budget_exceeded": cost_comparison.budget_exceeded,
        "compliance_score": compliance_report.compliance_score_pct,
        "compliance_passed": compliance_report.passed_checks,
        "compliance_total": compliance_report.total_checks,
        "failed_checks": [c.check_name for c in compliance_report.checks if not c.passed][:5],
    }, indent=2)

    messages = [
        SystemMessage(content=_EXECUTIVE_SUMMARY_PROMPT),
        HumanMessage(
            content=f"Generate an executive summary for:\n\n{user_content}"
        ),
    ]

    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, "content") else str(response)
        log.debug("executive_summary_completed", length=len(content))
        return content[:4000]
    except Exception:
        log.warning("executive_summary_failed", exc_info=True)
        # Heuristic fallback
        cheapest = cost_comparison.cheapest_provider
        cheapest_bd = next(
            (p for p in cost_comparison.providers if p.provider == cheapest),
            None,
        ) if cheapest else None
        cheapest_cost = cheapest_bd.total_monthly_usd if cheapest_bd else 0.0
        ri1_savings = (
            f"  A 1-year reserved instance commitment reduces this to "
            f"${cheapest_bd.reserved_1yr_monthly_usd:,.2f}/month "
            f"({cheapest_bd.reserved_1yr_savings_pct:.0f}% savings)."
            if cheapest_bd and cheapest_bd.reserved_1yr_monthly_usd else ""
        )

        return (
            f"This document presents a cloud infrastructure procurement "
            f"recommendation for {workload_request.project_name}. "
            f"The analysis evaluates {len(workload_profile.components)} "
            f"workload components requiring {workload_profile.total_vcpus} vCPUs, "
            f"{workload_profile.total_memory_gb} GB of memory, and "
            f"{workload_profile.total_storage_gb} GB of storage across "
            f"{len(cost_comparison.providers)} cloud providers in the "
            f"{workload_request.environment.value} environment.\n\n"
            f"Based on cost-optimised sizing, "
            f"{cheapest.value.upper() if cheapest else 'N/A'} is recommended "
            f"at an estimated ${cheapest_cost:,.2f}/month (${cheapest_cost * 12:,.2f}/year), "
            f"saving {cost_comparison.savings_vs_most_expensive_pct:.1f}% compared to "
            f"the most expensive alternative.{ri1_savings}\n\n"
            f"WAF compliance score: {compliance_report.compliance_score_pct:.0f}% "
            f"({compliance_report.passed_checks}/{compliance_report.total_checks} checks passed). "
            f"{'All checks passed.' if not compliance_report.checks or all(c.passed for c in compliance_report.checks) else 'Remediation items identified — see Section 9.'} "
            f"The recommended provider holds SOC 2 Type II, ISO 27001, and relevant "
            f"industry compliance certifications. Immediate next step: issue cloud "
            f"provider purchase order and initiate the Phase 1 discovery engagement."
        )


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------


async def run_rfp_writer_node(
    state: OrchestratorState,
    llm: BaseChatModel,
) -> OrchestratorState:
    """Execute the RFP Writer agent — generate procurement document.

    This is a LangGraph node function.  It reads all upstream state
    and produces a Markdown RFP document, executive summary, and
    compliance report.

    Args:
        state: Current ``OrchestratorState`` (TypedDict).
        llm: LLM instance for generating narrative sections
            (``BaseChatModel`` interface).

    Returns:
        Updated ``OrchestratorState`` with ``rfp_document``,
        ``executive_summary``, ``compliance_report``, and a
        summary message appended to ``messages``.

    Raises:
        ValueError: If required upstream data is missing from state.
    """
    log = logger.bind(
        agent="rfp_writer",
        request_id=state.get("request_id", "unknown"),
    )

    start_time = datetime.now(timezone.utc)
    log.info("rfp_writer_node_started")

    try:
        # ── Validate inputs ───────────────────────────────────
        workload_request: WorkloadRequest | None = state.get("workload_request")
        if workload_request is None:
            raise ValueError("workload_request is missing from state")

        workload_profile: WorkloadProfile | None = state.get("workload_profile")
        if workload_profile is None:
            raise ValueError("workload_profile is missing from state")

        sized_results: list[SizedWorkloadResult] = state.get("sized_results", [])
        if not sized_results:
            raise ValueError("sized_results is empty — Sizer must run first")

        cost_comparison_data = state.get("cost_comparison", {})
        if cost_comparison_data:
            cost_comparison = CostComparison(**cost_comparison_data)
        else:
            cost_comparison = CostComparison()

        log.info(
            "generating_rfp",
            project=workload_request.project_name,
            components=len(workload_profile.components),
            sized_results=len(sized_results),
            providers=len(cost_comparison.providers),
        )

        # ── Run WAF compliance checks ─────────────────────────
        log.info("running_compliance_checks")
        compliance_report = evaluate_compliance(workload_request)
        log.info(
            "compliance_checks_completed",
            score=compliance_report.compliance_score_pct,
            passed=compliance_report.passed_checks,
            total=compliance_report.total_checks,
        )

        # ── Generate executive summary ────────────────────────
        executive_summary = await _generate_executive_summary(
            llm,
            workload_request,
            workload_profile,
            cost_comparison,
            compliance_report,
        )

        # ── Pull TCO projections from state KPIs ──────────────
        kpis: dict[str, Any] = state.get("kpis") or {}

        # ── Scenario flags ────────────────────────────────────
        include_traceability = bool(workload_request.compliance_frameworks)
        include_mobile = _is_mobile_scenario(workload_request)

        # ── Assemble the RFP document ─────────────────────────
        sections = [
            _build_header_section(workload_request),
            _build_toc_section(include_traceability=include_traceability, include_mobile=include_mobile),
            f"## 1. Executive Summary\n\n{executive_summary}\n",
            _build_workload_summary_section(workload_profile, workload_request),
            _build_architecture_section(workload_profile, workload_request, sized_results),
            _build_tech_specs_section(workload_profile, sized_results, workload_request),
            _build_managed_services_section(workload_profile, workload_request, sized_results),
            _build_sku_selection_section(sized_results),
            _build_cost_comparison_section(cost_comparison),
            _build_tco_section(cost_comparison, kpis),
            _build_sla_section(workload_request, cost_comparison),
            _build_security_section(workload_request, compliance_report),
            _build_migration_section(workload_profile, workload_request),
            _build_dr_section(workload_request, cost_comparison),
            _build_certifications_section(cost_comparison, workload_request),
            _build_vendor_shortlist_section(cost_comparison, sized_results),
            _build_assumptions_section(workload_request, workload_profile),
            _build_compliance_section(compliance_report),
        ]

        if include_traceability:
            sections.append(
                _build_requirements_traceability_section(workload_request, workload_profile)
            )

        rfp_document = "\n---\n\n".join(sections)

        log.info(
            "rfp_document_assembled",
            document_length=len(rfp_document),
            sections=len(sections),
        )

        # ── Build summary message ─────────────────────────────
        summary_content = (
            f"**RFP Document Generated** — "
            f"{len(rfp_document):,} characters, {len(sections)} sections.\n\n"
            f"**Compliance**: {compliance_report.compliance_score_pct:.0f}% "
            f"({compliance_report.passed_checks}/{compliance_report.total_checks} passed)\n\n"
            f"**Executive Summary**: {executive_summary[:200]}..."
        )

        summary_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=summary_content,
            agent_name="rfp_writer",
            metadata={
                "document_length": len(rfp_document),
                "compliance_score": compliance_report.compliance_score_pct,
                "sections": len(sections),
            },
        )

        # ── Compute timing ────────────────────────────────────
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        execution = AgentExecution(
            agent_name="rfp_writer",
            status=AgentStatus.COMPLETED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
        )

        log.info(
            "rfp_writer_node_completed",
            duration_ms=round(duration_ms, 1),
            document_length=len(rfp_document),
        )

        # ── Return state update ───────────────────────────────
        return {
            "rfp_document": rfp_document,
            "executive_summary": executive_summary,
            "compliance_report": compliance_report.model_dump(),
            "messages": [summary_message],
            "current_agent": "rfp_writer",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "rfp_writer": execution,
            },
        }

    except Exception:
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000

        log.error(
            "rfp_writer_node_failed",
            exc_info=True,
            duration_ms=round(duration_ms, 1),
        )

        execution = AgentExecution(
            agent_name="rfp_writer",
            status=AgentStatus.FAILED,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=round(duration_ms, 1),
            error_message=str(Exception),
        )

        error_message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=(
                "**RFP Writer Error** — Failed to generate procurement document. "
                "Check logs for details."
            ),
            agent_name="rfp_writer",
            metadata={"error": True},
        )

        return {
            "rfp_document": "",
            "executive_summary": "",
            "compliance_report": {},
            "messages": [error_message],
            "current_agent": "rfp_writer",
            "error": f"RFP Writer failed: {Exception}",
            "agent_executions": {
                **state.get("agent_executions", {}),
                "rfp_writer": execution,
            },
        }
