#!/usr/bin/env python3
"""Multi-Scenario Benchmark — P16d Dissertation Evaluation Data.

Runs three representative workload scenarios through the full agent pipeline
(Profiler → Sizer → FinOps → Validator → RFP Writer) without a live Clarifier
turn by injecting pre-built WorkloadRequest objects directly.

### Scenarios
1. **Cal Fire** — State government wildfire platform (StateRAMP, WCAG, AWS single-cloud)
2. **Healthcare SaaS** — HIPAA-covered patient data platform (multi-cloud, HA)
3. **E-Commerce** — High-traffic retail platform (burst scaling, cost-optimised)

### Output
- Prints a Markdown benchmark table to stdout
- Writes `data/benchmark_results.json` with full per-scenario metrics
- Writes `data/benchmark_report.md` with the Markdown report

### Usage
    uv run python scripts/benchmark_scenarios.py

    # Dry run (skip LLM-heavy RFP summary — uses heuristic):
    uv run python scripts/benchmark_scenarios.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

SCENARIOS: list[dict[str, Any]] = [
    # ─── Scenario 1: Cal Fire (Government / StateRAMP) ───────────────────────
    {
        "id": "cal_fire",
        "name": "Cal Fire Wildfire Platform",
        "domain": "Government / Public Safety",
        "description": (
            "Real-time wildfire incident platform for the California Department of "
            "Forestry and Fire Protection (Cal Fire). Serves 50K normal / 2M peak "
            "concurrent users during major fire events. StateRAMP Moderate + WCAG 2.2 AA."
        ),
        "workload_request": {
            "project_name": "Cal Fire Wildfire Incident Platform",
            "environment": "production",
            "tier": "mission_critical",
            "target_providers": ["aws"],
            "preferred_region": "us-west-2",
            "provider_regions": {
                "aws": "us-west-2",
                "azure": "westus2",
                "gcp": "us-west1",
            },
            "budget_monthly_usd": 266_667.0,
            "compliance_frameworks": ["stateramp-moderate", "wcag-2.2-aa", "waf"],
            "raw_user_input": (
                "State government wildfire incident platform for Cal Fire. "
                "50K normal / 2M peak users. StateRAMP Moderate required. "
                "AWS preferred. Cross-region DR: us-west-2 → us-east-1."
            ),
            "workloads": [
                {
                    "name": "Incident API (EKS Microservices)",
                    "description": "Containerised REST API for real-time incident data",
                    "suggested_category": "container",
                    "scaling_pattern": "bursty",
                    "resources": {
                        "cpu_request_millicores": 2000,
                        "memory_request_mb": 4096,
                        "replicas": 6,
                        "storage_gb": 50.0,
                    },
                    "concurrent_users": 2_000_000,
                    "throughput_rps": 50_000,
                    "latency_p99_ms": 200,
                    "uptime_sla": 99.99,
                    "rpo_minutes": 15,
                    "rto_minutes": 30,
                    "spot_eligible": False,
                    "compliance_tags": ["stateramp-moderate", "wcag-2.2-aa"],
                },
                {
                    "name": "Incident Database (RDS PostgreSQL Multi-AZ)",
                    "description": "Primary PostgreSQL database with Multi-AZ HA and read replicas",
                    "suggested_category": "database",
                    "scaling_pattern": "growing",
                    "resources": {
                        "storage_gb": 2000.0,
                        "database_engine": "postgresql",
                    },
                    "uptime_sla": 99.99,
                    "rpo_minutes": 5,
                    "rto_minutes": 15,
                    "spot_eligible": False,
                    "compliance_tags": ["stateramp-moderate"],
                },
                {
                    "name": "Redis Cache (ElastiCache)",
                    "description": "Hot incident data caching layer",
                    "suggested_category": "database",
                    "scaling_pattern": "bursty",
                    "resources": {
                        "storage_gb": 50.0,
                        "database_engine": "redis",
                    },
                    "uptime_sla": 99.9,
                    "spot_eligible": False,
                },
                {
                    "name": "Geospatial Tile Storage (S3)",
                    "description": "S3 bucket for fire perimeter maps and geospatial tiles",
                    "suggested_category": "storage",
                    "scaling_pattern": "growing",
                    "resources": {
                        "storage_gb": 10_000.0,
                    },
                    "spot_eligible": True,
                },
                {
                    "name": "CDN (CloudFront)",
                    "description": "Global CDN for public-facing incident portal",
                    "suggested_category": "networking",
                    "scaling_pattern": "bursty",
                    "notes": "cdn",
                    "resources": {
                        "storage_gb": 0.0,
                    },
                    "spot_eligible": False,
                },
            ],
        },
        "reference": {
            "architecture": "3-tier container + managed PostgreSQL + CDN",
            "aws_monthly_usd": 45_000,
            "description": "AWS GovCloud-adjacent (us-west-2) with EKS + RDS Multi-AZ + CloudFront",
        },
    },

    # ─── Scenario 2: Healthcare SaaS (HIPAA) ─────────────────────────────────
    {
        "id": "healthcare",
        "name": "Healthcare SaaS Patient Platform",
        "domain": "Healthcare / Life Sciences",
        "description": (
            "Multi-tenant SaaS platform for patient data management across 200 hospitals. "
            "Handles EHR integration, FHIR APIs, ML-based diagnostics. HIPAA-covered. "
            "Multi-cloud evaluation: AWS + Azure + GCP."
        ),
        "workload_request": {
            "project_name": "HealthOS Patient Data Platform",
            "environment": "production",
            "tier": "mission_critical",
            "target_providers": ["aws", "azure", "gcp"],
            "preferred_region": "us-east-1",
            "provider_regions": {
                "aws": "us-east-1",
                "azure": "eastus",
                "gcp": "us-east4",
            },
            "budget_monthly_usd": 80_000.0,
            "compliance_frameworks": ["hipaa", "waf"],
            "raw_user_input": (
                "Multi-tenant HIPAA healthcare SaaS platform. 200 hospital clients. "
                "EHR integration + FHIR APIs + ML diagnostics. Multi-cloud."
            ),
            "workloads": [
                {
                    "name": "FHIR API Gateway",
                    "description": "HL7 FHIR R4 API for EHR integrations",
                    "suggested_category": "container",
                    "scaling_pattern": "steady",
                    "resources": {
                        "cpu_request_millicores": 1000,
                        "memory_request_mb": 2048,
                        "replicas": 4,
                        "storage_gb": 20.0,
                    },
                    "throughput_rps": 5000,
                    "latency_p99_ms": 500,
                    "uptime_sla": 99.99,
                    "spot_eligible": False,
                    "compliance_tags": ["hipaa"],
                },
                {
                    "name": "Patient Records Database (PostgreSQL)",
                    "description": "Multi-tenant PostgreSQL for PHI storage",
                    "suggested_category": "database",
                    "scaling_pattern": "growing",
                    "resources": {
                        "storage_gb": 5_000.0,
                        "database_engine": "postgresql",
                    },
                    "uptime_sla": 99.99,
                    "rpo_minutes": 0,
                    "rto_minutes": 15,
                    "spot_eligible": False,
                    "compliance_tags": ["hipaa"],
                },
                {
                    "name": "ML Diagnostics Engine",
                    "description": "Managed ML inference for diagnostic support models",
                    "suggested_category": "ai_ml",
                    "scaling_pattern": "batch",
                    "resources": {
                        "cpu_request_millicores": 8000,
                        "memory_request_mb": 32768,
                        "storage_gb": 500.0,
                    },
                    "spot_eligible": True,
                },
                {
                    "name": "PHI Audit Log Storage (S3/ADLS/GCS)",
                    "description": "Immutable audit log bucket with 7-year retention",
                    "suggested_category": "storage",
                    "scaling_pattern": "growing",
                    "resources": {
                        "storage_gb": 20_000.0,
                    },
                    "spot_eligible": True,
                    "compliance_tags": ["hipaa"],
                },
            ],
        },
        "reference": {
            "architecture": "Container + managed PostgreSQL + managed ML + immutable audit storage",
            "aws_monthly_usd": 28_000,
            "description": "AWS us-east-1 with EKS + RDS Aurora PostgreSQL + SageMaker + S3",
        },
    },

    # ─── Scenario 3: E-Commerce (Burst Scaling / Cost-Optimised) ─────────────
    {
        "id": "ecommerce",
        "name": "High-Traffic E-Commerce Platform",
        "domain": "Retail / E-Commerce",
        "description": (
            "B2C retail platform with 10K baseline / 500K peak concurrent users "
            "(Black Friday). Microservices on Kubernetes, managed MySQL, Redis caching, "
            "CDN for static assets. Cost-optimised with spot + RI mix."
        ),
        "workload_request": {
            "project_name": "ShopFast E-Commerce Platform",
            "environment": "production",
            "tier": "business_critical",
            "target_providers": ["aws", "azure", "gcp"],
            "preferred_region": "us-east-1",
            "provider_regions": {
                "aws": "us-east-1",
                "azure": "eastus",
                "gcp": "us-central1",
            },
            "budget_monthly_usd": 35_000.0,
            "compliance_frameworks": ["pci-dss", "waf"],
            "raw_user_input": (
                "B2C e-commerce platform. 10K normal / 500K peak users (Black Friday). "
                "Microservices + MySQL + Redis + CDN. Cost-optimised."
            ),
            "workloads": [
                {
                    "name": "Storefront API (EKS/AKS/GKE)",
                    "description": "Core product catalogue and checkout microservices",
                    "suggested_category": "container",
                    "scaling_pattern": "bursty",
                    "resources": {
                        "cpu_request_millicores": 1000,
                        "memory_request_mb": 2048,
                        "replicas": 4,
                        "storage_gb": 20.0,
                    },
                    "concurrent_users": 500_000,
                    "throughput_rps": 20_000,
                    "latency_p99_ms": 300,
                    "uptime_sla": 99.95,
                    "spot_eligible": True,
                },
                {
                    "name": "Product Catalogue DB (MySQL)",
                    "description": "MySQL database for products, inventory, orders",
                    "suggested_category": "database",
                    "scaling_pattern": "bursty",
                    "resources": {
                        "storage_gb": 1_000.0,
                        "database_engine": "mysql",
                    },
                    "uptime_sla": 99.99,
                    "rpo_minutes": 5,
                    "rto_minutes": 60,
                    "spot_eligible": False,
                },
                {
                    "name": "Session Cache (Redis)",
                    "description": "User session and cart caching",
                    "suggested_category": "database",
                    "scaling_pattern": "bursty",
                    "resources": {
                        "storage_gb": 100.0,
                        "database_engine": "redis",
                    },
                    "spot_eligible": False,
                },
                {
                    "name": "Static Asset Storage (S3/Blob/GCS)",
                    "description": "Product images, CSS/JS bundles",
                    "suggested_category": "storage",
                    "scaling_pattern": "growing",
                    "resources": {
                        "storage_gb": 5_000.0,
                    },
                    "spot_eligible": True,
                },
                {
                    "name": "CDN (CloudFront/Azure CDN/Cloud CDN)",
                    "description": "Global CDN for static assets",
                    "suggested_category": "networking",
                    "scaling_pattern": "bursty",
                    "notes": "cdn",
                    "resources": {
                        "storage_gb": 0.0,
                    },
                    "spot_eligible": False,
                },
            ],
        },
        "reference": {
            "architecture": "Container + managed MySQL + Redis + CDN",
            "aws_monthly_usd": 12_000,
            "description": "AWS us-east-1 EKS + RDS MySQL + ElastiCache + CloudFront with Spot mix",
        },
    },
]

# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

# List fields that LangGraph merges via operator.add (append-only).
# When calling nodes directly we must extend rather than replace these.
_APPEND_FIELDS: frozenset[str] = frozenset({"messages", "sized_results"})


def _merge_state(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial agent-node state update back into the full state dict.

    LangGraph nodes return only the keys they modified. When invoking them
    directly (outside the graph) we must manually propagate the unchanged
    fields and apply the same list-append semantics that LangGraph uses for
    Annotated[list, operator.add] fields.
    """
    result = dict(base)
    for key, val in update.items():
        if key in _APPEND_FIELDS and isinstance(val, list) and isinstance(result.get(key), list):
            result[key] = result[key] + val
        else:
            result[key] = val
    return result


async def run_scenario(
    scenario: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a single benchmark scenario through the full agent pipeline.

    Bypasses the Clarifier by injecting a pre-built WorkloadRequest into
    initial state. The pipeline starts at Profiler → Sizer → FinOps →
    Validator → RFP Writer.

    Args:
        scenario: Scenario definition dict (see SCENARIOS).
        dry_run: If True, skip LLM calls — use mocked LLM with heuristic fallbacks.

    Returns:
        Result dict with metrics, timing, and status.
    """
    from src.agents.finops import run_finops_node
    from src.agents.profiler import run_profiler_node
    from src.agents.rfp_writer import run_rfp_writer_node
    from src.agents.sizer import run_sizer_node
    from src.agents.validator import run_validator_node
    from src.config.settings import get_settings
    from src.llm.factory import get_llm
    from src.models.conversation import ConversationState
    from src.models.workload import WorkloadProfile, WorkloadRequest
    from src.models.cloud_resource import CloudProvider
    from src.orchestrator.state import (
        AgentExecution,
        AgentStatus,
        ExecutionPlan,
        OrchestratorState,
        create_initial_state,
    )
    from src.providers.aws_provider import AWSPricingProvider
    from src.providers.azure_provider import AzurePricingProvider
    from src.providers.gcp_provider import GCPPricingProvider
    from src.services.pricing_service import PricingService

    scenario_id = scenario["id"]
    print(f"\n{'='*70}")
    print(f"  Scenario: {scenario['name']}")
    print(f"  Domain  : {scenario['domain']}")
    print(f"{'='*70}")

    t_start = time.perf_counter()
    result: dict[str, Any] = {
        "scenario_id": scenario_id,
        "scenario_name": scenario["name"],
        "domain": scenario["domain"],
        "status": "pending",
        "error": None,
        "timing_s": {},
        "costs": {},
        "architecture_winner": None,
        "compliance_score_pct": None,
        "waf_checks_passed": None,
        "waf_checks_total": None,
        "sized_components": 0,
        "rfp_length_chars": 0,
        "rfp_sections": 0,
        "stateramp_gap_appended": False,
        "budget_monthly_usd": scenario["workload_request"].get("budget_monthly_usd"),
        "reference_aws_usd": scenario["reference"]["aws_monthly_usd"],
        "validation_passed": None,
        "pricing_errors": 0,
    }

    try:
        # ── Settings + pricing service ─────────────────────────
        settings = get_settings()
        if dry_run:
            # In dry-run mode skip all live API calls — use a mock that
            # returns empty lists so agents use their heuristic fallbacks.
            pricing_service = MagicMock()
            pricing_service.initialize = AsyncMock()
            pricing_service.search_prices = AsyncMock(return_value=[])
            pricing_service.get_sku_prices = AsyncMock(return_value=[])
            pricing_service.compare_across_providers = AsyncMock(return_value={})
            pricing_service.registered_providers = [
                CloudProvider.AWS, CloudProvider.AZURE, CloudProvider.GCP
            ]
        else:
            pricing_service = PricingService()
            pricing_service.register_provider(AWSPricingProvider(
                access_key_id=settings.aws.access_key_id,
                secret_access_key=settings.aws.secret_access_key,
                session_token=settings.aws.session_token,
            ))
            pricing_service.register_provider(AzurePricingProvider())
            pricing_service.register_provider(GCPPricingProvider())

        # ── LLM: real or mock ──────────────────────────────────
        if dry_run:
            llm = MagicMock()
            # Heuristic fallback triggers when LLM raises (returns empty mock)
            llm.ainvoke = AsyncMock(
                side_effect=RuntimeError("dry_run: LLM disabled")
            )
        else:
            llm = get_llm(settings.llm, settings.aws)

        # ── Build initial state with pre-injected WorkloadRequest ─
        req_data = scenario["workload_request"]
        workload_request = WorkloadRequest(**req_data)

        request_id = str(uuid.uuid4())
        state: OrchestratorState = create_initial_state(
            request_id=request_id,
            project_name=workload_request.project_name,
        )
        # Override workload_request with our pre-built one (skip clarifier)
        state["workload_request"] = workload_request

        print(f"  → {len(workload_request.workloads)} workload components | "
              f"providers: {[p.value for p in workload_request.target_providers]}")
        if workload_request.budget_monthly_usd:
            print(f"  → Budget: ${workload_request.budget_monthly_usd:,.0f}/mo | "
                  f"Compliance: {workload_request.compliance_frameworks}")

        # ── Profiler ───────────────────────────────────────────
        print("  [1/5] Profiler...", end=" ", flush=True)
        t0 = time.perf_counter()
        state = _merge_state(state, await run_profiler_node(state, llm))
        result["timing_s"]["profiler"] = round(time.perf_counter() - t0, 2)
        profile = state.get("workload_profile")
        print(f"done ({result['timing_s']['profiler']}s) — "
              f"{len(profile.components) if profile else 0} components")

        # ── Sizer ──────────────────────────────────────────────
        print("  [2/5] Sizer...", end=" ", flush=True)
        t0 = time.perf_counter()
        state = _merge_state(state, await run_sizer_node(state, llm, pricing_service))
        result["timing_s"]["sizer"] = round(time.perf_counter() - t0, 2)
        sized = state.get("sized_results", [])
        result["sized_components"] = len(sized)
        print(f"done ({result['timing_s']['sizer']}s) — {len(sized)} SKU selections")

        # ── FinOps ─────────────────────────────────────────────
        print("  [3/5] FinOps...", end=" ", flush=True)
        t0 = time.perf_counter()
        state = _merge_state(state, await run_finops_node(state, llm, pricing_service))
        result["timing_s"]["finops"] = round(time.perf_counter() - t0, 2)
        cost_data = state.get("cost_comparison", {})
        _extract_costs(result, cost_data)
        print(f"done ({result['timing_s']['finops']}s) — "
              f"cheapest: {result.get('cheapest_provider', '?')} "
              f"${result.get('cheapest_monthly_usd', 0):,.0f}/mo")

        # ── Validator ──────────────────────────────────────────
        print("  [4/5] Validator...", end=" ", flush=True)
        t0 = time.perf_counter()
        state = _merge_state(state, await run_validator_node(state, llm))
        result["timing_s"]["validator"] = round(time.perf_counter() - t0, 2)
        val_report = state.get("validation_report", {})
        arch_alts = state.get("architecture_alternatives", [])
        result["validation_passed"] = not val_report.get("has_critical_issues", True)
        result["architecture_winner"] = arch_alts[0]["name"] if arch_alts else "containers"
        result["pricing_errors"] = len(val_report.get("pricing_issues", []))
        print(f"done ({result['timing_s']['validator']}s) — "
              f"winner: {result['architecture_winner']} | "
              f"pass: {result['validation_passed']}")

        # ── RFP Writer ─────────────────────────────────────────
        print("  [5/5] RFP Writer...", end=" ", flush=True)
        t0 = time.perf_counter()
        state = _merge_state(state, await run_rfp_writer_node(state, llm))
        result["timing_s"]["rfp_writer"] = round(time.perf_counter() - t0, 2)
        rfp_doc = state.get("rfp_document", "")
        comp_report = state.get("compliance_report", {})
        result["rfp_length_chars"] = len(rfp_doc)
        result["rfp_sections"] = rfp_doc.count("\n## ")
        result["stateramp_gap_appended"] = "Appendix A" in rfp_doc and "StateRAMP Moderate" in rfp_doc
        result["compliance_score_pct"] = comp_report.get("compliance_score_pct", 0)
        result["waf_checks_passed"] = comp_report.get("passed_checks", 0)
        result["waf_checks_total"] = comp_report.get("total_checks", 0)
        print(f"done ({result['timing_s']['rfp_writer']}s) — "
              f"{result['rfp_length_chars']:,} chars | "
              f"WAF {result['compliance_score_pct']:.0f}%")

        result["status"] = "success"
        result["timing_s"]["total"] = round(time.perf_counter() - t_start, 2)

    except Exception:
        result["status"] = "error"
        result["error"] = traceback.format_exc()
        result["timing_s"]["total"] = round(time.perf_counter() - t_start, 2)
        print(f"\n  ❌ FAILED: {result['error'][:200]}")

    return result


def _extract_costs(result: dict[str, Any], cost_data: dict) -> None:
    """Populate cost fields in result from cost_comparison dict."""
    providers = cost_data.get("providers", [])
    if not providers:
        return

    for pb in providers:
        prov = pb.get("provider", "unknown")
        if isinstance(prov, dict):
            prov = prov.get("value", str(prov))
        result["costs"][prov] = {
            "monthly_usd": pb.get("total_monthly_usd", 0),
            "annual_usd": pb.get("total_annual_usd", 0),
            "ri_1yr_savings_pct": pb.get("reserved_1yr_savings_pct", 0),
        }

    cheapest = cost_data.get("cheapest_provider")
    if cheapest:
        cheapest_val = cheapest.get("value", str(cheapest)) if isinstance(cheapest, dict) else cheapest
        result["cheapest_provider"] = cheapest_val
        cheapest_bd = next(
            (pb for pb in providers
             if (pb.get("provider", {}).get("value", str(pb.get("provider"))) == cheapest_val
                 if isinstance(pb.get("provider"), dict)
                 else str(pb.get("provider")) == cheapest_val)),
            None,
        )
        if cheapest_bd:
            result["cheapest_monthly_usd"] = cheapest_bd.get("total_monthly_usd", 0)
            result["cheapest_annual_usd"] = cheapest_bd.get("total_annual_usd", 0)

    result["savings_vs_expensive_pct"] = cost_data.get("savings_vs_most_expensive_pct", 0)
    result["budget_exceeded"] = cost_data.get("budget_exceeded", False)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_markdown_report(results: list[dict[str, Any]]) -> str:
    """Render benchmark results as a Markdown report.

    Args:
        results: List of result dicts from run_scenario().

    Returns:
        Full Markdown report string.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Cloud Orchestrator IDSS — Multi-Scenario Benchmark Report\n",
        f"> Generated: {now}  ",
        f"> Scenarios: {len(results)}  ",
        f"> Status: {sum(1 for r in results if r['status'] == 'success')}/{len(results)} succeeded\n",
        "---\n",
    ]

    # ── Summary table ──────────────────────────────────────────────────────
    lines.append("## Summary\n")
    lines.append(
        "| Scenario | Domain | Cheapest Provider | Monthly Cost (USD) | "
        "WAF Score | Arch Winner | RFP Size | Pipeline (s) | Status |\n"
        "|----------|--------|-------------------|-------------------|"
        "----------|-------------|----------|-------------|--------|\n"
    )
    for r in results:
        status_icon = "✅" if r["status"] == "success" else "❌"
        cheapest = r.get("cheapest_provider", "N/A").upper()
        monthly = f"${r.get('cheapest_monthly_usd', 0):,.0f}" if r.get("cheapest_monthly_usd") else "N/A"
        waf = f"{r.get('compliance_score_pct', 0):.0f}%" if r.get("compliance_score_pct") is not None else "N/A"
        arch = r.get("architecture_winner", "N/A").replace("_", " ")
        rfp_kb = f"{r.get('rfp_length_chars', 0) // 1024}KB"
        total_s = r.get("timing_s", {}).get("total", 0)
        lines.append(
            f"| {r['scenario_name']} | {r['domain']} | {cheapest} | {monthly} | "
            f"{waf} | {arch} | {rfp_kb} | {total_s:.1f}s | {status_icon} |"
        )

    lines.append("")

    # ── Per-scenario detail ────────────────────────────────────────────────
    lines.append("---\n")
    lines.append("## Per-Scenario Detail\n")

    for r in results:
        lines.append(f"### {r['scenario_name']}\n")
        lines.append(f"**Domain**: {r['domain']}  ")
        lines.append(f"**Status**: {'✅ Success' if r['status'] == 'success' else '❌ Error'}\n")

        if r["status"] == "error":
            lines.append(f"```\n{r.get('error', '')[:500]}\n```\n")
            continue

        # Cost breakdown table
        lines.append("#### Cost Comparison\n")
        lines.append(
            "| Provider | Monthly (USD) | Annual (USD) | 1-yr RI Savings |\n"
            "|----------|--------------|--------------|----------------|\n"
        )
        for prov, costs in (r.get("costs") or {}).items():
            monthly = f"${costs['monthly_usd']:,.0f}"
            annual = f"${costs['annual_usd']:,.0f}"
            ri = f"{costs['ri_1yr_savings_pct']:.0f}%"
            lines.append(f"| {prov.upper()} | {monthly} | {annual} | {ri} |")
        lines.append("")

        # Budget + reference
        if r.get("budget_monthly_usd"):
            cheapest = r.get("cheapest_monthly_usd", 0)
            budget = r["budget_monthly_usd"]
            budget_status = "✅ Within budget" if cheapest <= budget else "⚠️ Over budget"
            lines.append(
                f"**Budget**: ${budget:,.0f}/mo | "
                f"**Cheapest**: ${cheapest:,.0f}/mo | "
                f"{budget_status}\n"
            )
        ref_usd = r.get("reference_aws_usd", 0)
        cheapest_usd = r.get("cheapest_monthly_usd", 0)
        delta_pct = ((cheapest_usd - ref_usd) / ref_usd * 100) if ref_usd else 0
        delta_str = f"+{delta_pct:.0f}% above" if delta_pct > 0 else f"{abs(delta_pct):.0f}% below"
        lines.append(f"**Reference (manual estimate)**: ${ref_usd:,.0f}/mo | "
                     f"AI estimate is **{delta_str}** reference\n")

        # Pipeline metrics
        lines.append("#### Pipeline Metrics\n")
        timing = r.get("timing_s", {})
        lines.append(
            "| Stage | Duration (s) |\n"
            "|-------|-------------|\n"
        )
        for stage in ["profiler", "sizer", "finops", "validator", "rfp_writer"]:
            t = timing.get(stage, 0)
            lines.append(f"| {stage.title()} | {t:.2f}s |")
        lines.append(f"| **Total** | **{timing.get('total', 0):.2f}s** |")
        lines.append("")

        # Compliance + architecture
        lines.append("#### Compliance & Architecture\n")
        lines.append(
            f"| Metric | Value |\n"
            f"|--------|-------|\n"
            f"| WAF Compliance Score | {r.get('compliance_score_pct', 0):.0f}% "
            f"({r.get('waf_checks_passed', 0)}/{r.get('waf_checks_total', 0)} checks) |\n"
            f"| Architecture Winner | {r.get('architecture_winner', 'N/A').replace('_', ' ')} |\n"
            f"| Validation Passed | {'✅ Yes' if r.get('validation_passed') else '⚠️ No'} |\n"
            f"| Pricing Errors | {r.get('pricing_errors', 0)} |\n"
            f"| Sized Components | {r.get('sized_components', 0)} |\n"
            f"| RFP Document Size | {r.get('rfp_length_chars', 0):,} chars "
            f"({r.get('rfp_sections', 0)} sections) |\n"
            f"| StateRAMP Gap Analysis | {'✅ Appended' if r.get('stateramp_gap_appended') else '➖ N/A'} |\n"
        )
        lines.append("")

    # ── Aggregate analysis ─────────────────────────────────────────────────
    lines.append("---\n")
    lines.append("## Aggregate Analysis\n")

    successful = [r for r in results if r["status"] == "success"]
    if successful:
        avg_waf = sum(r.get("compliance_score_pct", 0) for r in successful) / len(successful)
        avg_total = sum(r.get("timing_s", {}).get("total", 0) for r in successful) / len(successful)
        avg_rfp_kb = sum(r.get("rfp_length_chars", 0) for r in successful) / len(successful) / 1024
        lines.append(
            f"| Metric | Value |\n"
            f"|--------|-------|\n"
            f"| Avg WAF compliance score | {avg_waf:.1f}% |\n"
            f"| Avg pipeline duration | {avg_total:.1f}s |\n"
            f"| Avg RFP document size | {avg_rfp_kb:.0f} KB |\n"
            f"| Scenarios with StateRAMP gap analysis | "
            f"{sum(1 for r in successful if r.get('stateramp_gap_appended'))}/{len(successful)} |\n"
        )

    lines.append("\n*All costs are on-demand monthly estimates from live cloud pricing APIs. "
                 "Reserved instance and spot discounts shown separately. "
                 "Actual costs depend on usage patterns, negotiated discounts, and data transfer.*\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def main(dry_run: bool = False) -> None:
    """Run all benchmark scenarios and write reports.

    Args:
        dry_run: Skip live LLM calls; use heuristic fallbacks only.
    """
    print("\n" + "=" * 70)
    print("  Cloud Orchestrator IDSS — Multi-Scenario Benchmark")
    print(f"  Mode: {'DRY RUN (no LLM)' if dry_run else 'LIVE (with LLM)'}")
    print("=" * 70)

    started_at = datetime.now(timezone.utc)
    all_results: list[dict[str, Any]] = []

    for scenario in SCENARIOS:
        result = await run_scenario(scenario, dry_run=dry_run)
        all_results.append(result)

    # ── Write JSON output ──────────────────────────────────────────────────
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)

    json_path = data_dir / "benchmark_results.json"
    json_path.write_text(json.dumps({
        "generated_at": started_at.isoformat(),
        "dry_run": dry_run,
        "scenarios_run": len(all_results),
        "scenarios_success": sum(1 for r in all_results if r["status"] == "success"),
        "results": all_results,
    }, indent=2, default=str))
    print(f"\n✅ JSON results written to: {json_path}")

    # ── Write Markdown report ──────────────────────────────────────────────
    md_report = render_markdown_report(all_results)
    md_path = data_dir / "benchmark_report.md"
    md_path.write_text(md_report)
    print(f"✅ Markdown report written to: {md_path}")

    # ── Print summary table to stdout ──────────────────────────────────────
    print("\n" + "=" * 70)
    print("  BENCHMARK SUMMARY")
    print("=" * 70)
    header = f"{'Scenario':<35} {'Provider':<8} {'Monthly':>12} {'WAF':>7} {'Total':>8}"
    print(header)
    print("-" * 70)
    for r in all_results:
        if r["status"] == "success":
            cheapest = r.get("cheapest_provider", "N/A").upper()[:6]
            monthly = f"${r.get('cheapest_monthly_usd', 0):,.0f}"
            waf = f"{r.get('compliance_score_pct', 0):.0f}%"
            total = f"{r.get('timing_s', {}).get('total', 0):.1f}s"
            print(f"{r['scenario_name']:<35} {cheapest:<8} {monthly:>12} {waf:>7} {total:>8}")
        else:
            print(f"{r['scenario_name']:<35} {'ERROR':<8} {'N/A':>12} {'N/A':>7} "
                  f"{r.get('timing_s', {}).get('total', 0):.1f}s")
    print("=" * 70)

    success_count = sum(1 for r in all_results if r["status"] == "success")
    print(f"\n{'✅' if success_count == len(all_results) else '⚠️'} "
          f"{success_count}/{len(all_results)} scenarios completed successfully.")

    if any(r["status"] == "error" for r in all_results):
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cloud Orchestrator IDSS — Multi-Scenario Benchmark")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Skip live LLM calls; use heuristic fallbacks only (much faster)",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
