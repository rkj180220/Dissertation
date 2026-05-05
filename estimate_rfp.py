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
    CostComparison, ProviderCostBreakdown, ComplianceReport, ComplianceCheckResult
)
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.workload import WorkloadProfile, WorkloadRequest, ComponentProfile
from src.orchestrator.state import SizedWorkloadResult
from src.models.pricing import NormalizedPriceItem, PricingTier
from unittest.mock import MagicMock
from datetime import datetime, timezone

# Build minimal but realistic test data
req = MagicMock()
req.project_name = 'E-Commerce Platform Migration'
req.tier.value = 'production'
req.environment.value = 'production'
req.preferred_region = 'us-east-1'
req.budget_monthly_usd = 2000.0
req.compliance_requirements = ['GDPR', 'PCI DSS']
req.target_providers = [CloudProvider.AWS, CloudProvider.GCP]

sku = NormalizedPriceItem(
    provider=CloudProvider.AWS,
    service_name='AmazonEC2',
    service_category=ServiceCategory.COMPUTE,
    sku_id='aws-ec2-m5-xlarge-us-east-1',
    sku_name='m5.xlarge',
    product_name='EC2 m5.xlarge instance',
    region='us-east-1',
    retail_price=0.192,
    unit_price=0.192,
    currency='USD',
    unit_of_measure='Hrs',
    pricing_tier=PricingTier.ON_DEMAND,
    effective_date=datetime.now(timezone.utc),
    attributes={'vcpus': '4', 'memory_gb': '16', 'categories': [ServiceCategory.CONTAINER.value]},
)

sized_results = [
    SizedWorkloadResult(workload_name='API Service', provider=CloudProvider.AWS, monthly_cost_usd=138.24, fit_score=0.85, selected_sku=sku, alternatives=[], sizing_notes=[], rationale='Selected m5.xlarge for balanced vCPU/memory ratio'),
    SizedWorkloadResult(workload_name='PostgreSQL DB', provider=CloudProvider.AWS, monthly_cost_usd=175.0, fit_score=0.80, selected_sku=sku, alternatives=[], sizing_notes=[], rationale='RDS db.m5.large for production database'),
    SizedWorkloadResult(workload_name='[Infra] NAT Gateway', provider=CloudProvider.AWS, monthly_cost_usd=32.40, fit_score=1.0, selected_sku=None, alternatives=[], sizing_notes=[], rationale='Fixed infrastructure cost'),
]

comp_profile = ComponentProfile(
    workload_name='API Service',
    resolved_category=ServiceCategory.CONTAINER,
    estimated_vcpus=2,
    estimated_memory_gb=4.0,
    estimated_storage_gb=20.0,
    requires_gpu=False,
    recommended_instance_families=['m5'],
    rationale='Container workload on K8s',
)

db_profile = ComponentProfile(
    workload_name='PostgreSQL DB',
    resolved_category=ServiceCategory.DATABASE,
    estimated_vcpus=2,
    estimated_memory_gb=8.0,
    estimated_storage_gb=100.0,
    requires_gpu=False,
    recommended_instance_families=['db.m5'],
    rationale='Managed PostgreSQL database',
)

profile = WorkloadProfile(
    components=[comp_profile, db_profile],
    total_vcpus=4,
    total_memory_gb=12.0,
    total_storage_gb=120.0,
    requires_gpu=False,
    total_gpu_count=0,
)

comp = CostComparison(
    providers=[
        ProviderCostBreakdown(
            provider=CloudProvider.AWS,
            compute_monthly_usd=138.24,
            database_monthly_usd=175.0,
            networking_monthly_usd=32.40,
            total_monthly_usd=345.64,
            total_annual_usd=4147.68,
            reserved_1yr_monthly_usd=242.0,
            reserved_1yr_savings_pct=30.0,
            reserved_3yr_monthly_usd=190.0,
            reserved_3yr_savings_pct=45.0,
            spot_monthly_usd=104.0,
            spot_savings_pct=70.0,
            selected_skus=[sku],
        ),
        ProviderCostBreakdown(
            provider=CloudProvider.GCP,
            compute_monthly_usd=145.0,
            database_monthly_usd=180.0,
            networking_monthly_usd=32.40,
            total_monthly_usd=357.40,
            total_annual_usd=4288.80,
            reserved_1yr_monthly_usd=250.0,
            reserved_1yr_savings_pct=25.0,
            reserved_3yr_monthly_usd=214.0,
            reserved_3yr_savings_pct=40.0,
        ),
    ],
    cheapest_provider=CloudProvider.AWS,
    savings_vs_most_expensive_pct=3.3,
)

compliance = ComplianceReport(
    framework='WAF',
    checks=[
        ComplianceCheckResult(pillar='Security', check_name='Encryption at Rest', passed=True, severity='high', finding='AES-256 enabled', recommendation=''),
        ComplianceCheckResult(pillar='Reliability', check_name='Multi-AZ', passed=False, severity='high', finding='Single-AZ deployment', recommendation='Enable Multi-AZ for production databases'),
    ],
    total_checks=2,
    passed_checks=1,
    compliance_score_pct=50.0,
)

kpis = {'tco_projections': {'growth_pct_per_year': 15.0}}

sections = [
    _build_header_section(req),
    _build_toc_section(),
    'Executive Summary placeholder (normally 2000-4000 chars from LLM)',
    _build_workload_summary_section(profile, req),
    _build_architecture_section(profile, req, sized_results),
    _build_tech_specs_section(profile, sized_results, req),
    _build_sku_selection_section(sized_results),
    _build_cost_comparison_section(comp),
    _build_tco_section(comp, kpis),
    _build_sla_section(req, comp),
    _build_security_section(req, compliance),
    _build_migration_section(profile, req),
    _build_dr_section(req, comp),
    _build_certifications_section(comp, req),
    _build_vendor_shortlist_section(comp, sized_results),
    _build_assumptions_section(req, profile),
    _build_compliance_section(compliance),
]

total = sum(len(s) for s in sections)
print(f'Total estimated document length: {total:,} chars')
print(f'Section count: {len(sections)}')
print('Section lengths:')
names = ['Header','ToC','ExecSummary','Workload','Architecture','TechSpecs','SKU','Costs','TCO','SLA','Security','Migration','DR','Certs','Vendor','Assumptions','WAF']
for n, s in zip(names, sections):
    print(f'  {n}: {len(s):,} chars')
print(f'Target range: 15,000-30,000 chars')
if 15000 <= total <= 35000:
    print('Target met: YES')
elif total < 15000:
    print('Target met: EXPAND NEEDED')
else:
    print('Target met: GOOD (exceeds minimum)')
