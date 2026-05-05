"""Quick verification that P1e RFP Writer produces 15k+ chars."""
import sys
sys.path.insert(0, ".")

from src.agents.rfp_writer import (
    _build_toc_section, _build_header_section, _build_workload_summary_section,
    _build_sku_selection_section, _build_cost_comparison_section,
    _build_compliance_section, _build_vendor_shortlist_section,
    _build_architecture_section, _build_tech_specs_section,
    _build_sla_section, _build_security_section, _build_migration_section,
    _build_dr_section, _build_tco_section, _build_certifications_section,
    _build_assumptions_section,
)
from src.models.recommendation import (
    CostComparison, ProviderCostBreakdown, ComplianceReport, ComplianceCheckResult,
)
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.workload import WorkloadProfile, ComponentProfile
from src.models.pricing import NormalizedPriceItem, PricingTier
from datetime import datetime, timezone
from src.orchestrator.state import SizedWorkloadResult
from unittest.mock import MagicMock

req = MagicMock()
req.project_name = "E-Commerce Platform"
req.tier.value = "production"
req.environment.value = "production"
req.budget_monthly_usd = 2000.0
req.compliance_requirements = ["GDPR", "PCI DSS"]
req.target_providers = [CloudProvider.AWS, CloudProvider.GCP]

sku = NormalizedPriceItem(
    provider=CloudProvider.AWS,
    service_name="AmazonEC2",
    service_category=ServiceCategory.COMPUTE,
    sku_id="ABCD1234",
    sku_name="m5.xlarge",
    product_name="Amazon EC2 m5.xlarge",
    region="us-east-1",
    pricing_tier=PricingTier.ON_DEMAND,
    retail_price=0.192,
    unit_price=0.192,
    unit_of_measure="1 Hour",
    effective_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
    attributes={"vcpus": "4", "memory_gb": "16"},
)

sized = [
    SizedWorkloadResult(workload_name="API Service", provider=CloudProvider.AWS,
                        monthly_cost_usd=138.24, fit_score=0.85,
                        selected_sku=sku, alternatives=[], sizing_notes=[], rationale="Best fit"),
    SizedWorkloadResult(workload_name="PostgreSQL DB", provider=CloudProvider.AWS,
                        monthly_cost_usd=175.0, fit_score=0.80,
                        selected_sku=sku, alternatives=[], sizing_notes=[], rationale="RDS fit"),
]

c1 = ComponentProfile(workload_name="API Service", resolved_category=ServiceCategory.CONTAINER,
                      estimated_vcpus=2, estimated_memory_gb=4.0, estimated_storage_gb=20.0,
                      requires_gpu=False, recommended_instance_families=["m5", "c5"], rationale="K8s container")
c2 = ComponentProfile(workload_name="PostgreSQL DB", resolved_category=ServiceCategory.DATABASE,
                      estimated_vcpus=2, estimated_memory_gb=8.0, estimated_storage_gb=100.0,
                      requires_gpu=False, recommended_instance_families=["db.m5"], rationale="Managed PG")

profile = WorkloadProfile(components=[c1, c2], total_vcpus=4, total_memory_gb=12.0,
                          total_storage_gb=120.0, requires_gpu=False)

cost = CostComparison(
    providers=[
        ProviderCostBreakdown(provider=CloudProvider.AWS, compute_monthly_usd=138.24,
                              database_monthly_usd=175.0, networking_monthly_usd=32.40,
                              total_monthly_usd=345.64, total_annual_usd=4147.68,
                              reserved_1yr_monthly_usd=242.0, reserved_1yr_savings_pct=30.0,
                              reserved_3yr_monthly_usd=190.0, reserved_3yr_savings_pct=45.0),
        ProviderCostBreakdown(provider=CloudProvider.GCP, compute_monthly_usd=145.0,
                              database_monthly_usd=180.0, networking_monthly_usd=32.40,
                              total_monthly_usd=357.40, total_annual_usd=4288.80,
                              reserved_1yr_monthly_usd=250.0, reserved_1yr_savings_pct=25.0),
    ],
    cheapest_provider=CloudProvider.AWS, savings_vs_most_expensive_pct=3.3,
)

waf = ComplianceReport(
    framework="WAF",
    checks=[
        ComplianceCheckResult(pillar="Security", check_name="Encryption at Rest",
                              passed=True, severity="high", finding="AES-256", recommendation=""),
        ComplianceCheckResult(pillar="Reliability", check_name="Multi-AZ",
                              passed=False, severity="high", finding="Single-AZ",
                              recommendation="Enable Multi-AZ for production databases"),
    ],
    total_checks=2, passed_checks=1, compliance_score_pct=50.0,
)

kpis = {"tco_projections": {"growth_pct_per_year": 15.0}}

sections = [
    ("Header",        _build_header_section(req)),
    ("ToC",           _build_toc_section()),
    ("ExecSummary",   "PLACEHOLDER EXECUTIVE SUMMARY (2000-4000 chars from LLM in production)"),
    ("Workload",      _build_workload_summary_section(profile, req)),
    ("Architecture",  _build_architecture_section(profile, req, sized)),
    ("TechSpecs",     _build_tech_specs_section(profile, sized, req)),
    ("SKU",           _build_sku_selection_section(sized)),
    ("Costs",         _build_cost_comparison_section(cost)),
    ("TCO",           _build_tco_section(cost, kpis)),
    ("SLA",           _build_sla_section(req, cost)),
    ("Security",      _build_security_section(req, waf)),
    ("Migration",     _build_migration_section(profile, req)),
    ("DR",            _build_dr_section(req, cost)),
    ("Certs",         _build_certifications_section(cost, req)),
    ("Vendor",        _build_vendor_shortlist_section(cost, sized)),
    ("Assumptions",   _build_assumptions_section(req, profile)),
    ("WAF",           _build_compliance_section(waf)),
]

total = sum(len(s) for _, s in sections)
print("=" * 60)
print("RFP Writer P1e — Section Size Report")
print("=" * 60)
for name, s in sections:
    print(f"  {name:<15}: {len(s):>6,} chars")
print("-" * 60)
print(f"  {'TOTAL':<15}: {total:>6,} chars  ({total // 1000}k)")
print("=" * 60)
print(f"  Target: 15,000-30,000 chars")
target_met = total >= 15000
print(f"  Result: {'✅ TARGET MET' if target_met else '❌ NEEDS EXPANSION'}")
if not target_met:
    sys.exit(1)
print("\nAll assertions passed.")
