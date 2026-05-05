#!/usr/bin/env python3
"""Master validation script — audits every module in the codebase."""

from __future__ import annotations

import sys

results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        results.append((name, True, ""))
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {e}"))


# ── CONFIG ───────────────────────────────────────────────────────────────────

def t_config_settings():
    from src.config.settings import get_settings
    s = get_settings()
    assert s is not None

def t_config_logging():
    from src.config.logging_config import configure_observability
    assert callable(configure_observability)

def t_config_init():
    from src.config import get_settings, configure_observability
    assert get_settings is not None

check("config.settings — loads + singleton", t_config_settings)
check("config.logging_config — importable", t_config_logging)
check("config.__init__ — re-exports", t_config_init)


# ── LLM FACTORY ──────────────────────────────────────────────────────────────

def t_llm_factory():
    from src.llm.factory import get_llm
    from langchain_core.language_models import BaseChatModel
    assert callable(get_llm)

def t_llm_init():
    from src.llm import get_llm
    assert callable(get_llm)

check("llm.factory — get_llm importable", t_llm_factory)
check("llm.__init__ — re-exports get_llm", t_llm_init)


# ── MODELS ───────────────────────────────────────────────────────────────────

def t_models_cloud_resource():
    from src.models.cloud_resource import CloudProvider, ServiceCategory, ComputeSKU, StorageSKU
    assert len(CloudProvider) == 3
    assert len(ServiceCategory) == 15  # added KUBERNETES in P2
    assert CloudProvider.AWS.value == "aws"
    assert ServiceCategory.COMPUTE.value == "compute"
    assert ServiceCategory.KUBERNETES.value == "kubernetes"  # P2 addition

def t_models_pricing():
    from src.models.pricing import NormalizedPriceItem, PricingTier, SKUPricing
    assert len(PricingTier) == 8
    from datetime import datetime, timezone
    from src.models.cloud_resource import CloudProvider, ServiceCategory
    item = NormalizedPriceItem(
        provider=CloudProvider.AZURE,
        service_name="Virtual Machines",
        service_category=ServiceCategory.COMPUTE,
        sku_id="test-sku",
        sku_name="Standard_D4s_v5",
        product_name="VMs Dsv5",
        meter_name="D4s v5",
        region="eastus",
        retail_price=0.192,
        unit_price=0.192,
        currency="USD",
        unit_of_measure="1 Hour",
        pricing_tier=PricingTier.ON_DEMAND,
        effective_date=datetime.now(timezone.utc),
        is_primary_meter=True,
        attributes={},
    )
    assert abs(item.monthly_cost_estimate - 140.16) < 0.01

def t_models_workload():
    from src.models.workload import (
        WorkloadRequest, WorkloadRequirement, ResourceSpec, WorkloadProfile,
        ComponentProfile, EnvironmentType, WorkloadTier, ScalingPattern,
        VMWorkload, ContainerWorkload, StorageRequirement,  # legacy
    )
    from src.models.cloud_resource import ServiceCategory
    assert len(EnvironmentType) == 4
    assert len(WorkloadTier) == 3
    assert len(ScalingPattern) == 5
    req = WorkloadRequirement(
        name="DB",
        description="PostgreSQL",
        suggested_category=ServiceCategory.DATABASE,
        resources=ResourceSpec(database_engine="postgresql", high_availability=True),
    )
    assert req.name == "DB"

def t_models_conversation():
    from src.models.conversation import (
        ChatMessage, ClarificationQuestion, ConversationState,
        MessageRole, ClarificationStatus, ClarificationPriority,
    )
    from datetime import datetime, timezone
    msg = ChatMessage(role=MessageRole.USER, content="Hello")
    assert msg.role == MessageRole.USER
    conv = ConversationState()
    assert conv.should_continue_clarifying  # by default

def t_models_recommendation():
    from src.models.recommendation import (
        CloudRecommendation, CostComparison, ProviderCostBreakdown,
        BinPackingResult, PackedNode, ComplianceReport, ComplianceCheckResult,
        AncillaryCost,
    )
    from src.models.cloud_resource import CloudProvider, ServiceCategory
    cbd = ProviderCostBreakdown(
        provider=CloudProvider.AWS,
        compute_monthly_usd=100.0,
        total_monthly_usd=100.0,  # total is explicit, not auto-computed
    )
    assert cbd.total_monthly_usd == 100.0
    # P2: verify AncillaryCost and ancillary_costs field
    ac = AncillaryCost(
        provider=CloudProvider.AWS,
        category=ServiceCategory.NETWORKING,
        item_name="[Infra] NAT Gateway",
        monthly_cost_usd=32.0,
    )
    assert ac.monthly_cost_usd == 32.0
    assert cbd.ancillary_costs == []

def t_models_init():
    import src.models as m
    for sym in ["CloudProvider", "ServiceCategory", "NormalizedPriceItem", "PricingTier",
                "WorkloadRequest", "WorkloadRequirement", "ResourceSpec", "EnvironmentType",
                "ChatMessage", "ConversationState", "CloudRecommendation", "CostComparison"]:
        assert hasattr(m, sym), f"Missing export: {sym}"

check("models.cloud_resource — CloudProvider(3) + ServiceCategory(15) + legacy SKUs", t_models_cloud_resource)
check("models.pricing — NormalizedPriceItem + monthly_cost_estimate", t_models_pricing)
check("models.workload — new models + legacy backward-compat", t_models_workload)
check("models.conversation — ChatMessage + ConversationState", t_models_conversation)
check("models.recommendation — ProviderCostBreakdown + CloudRecommendation", t_models_recommendation)
check("models.__init__ — all key exports present", t_models_init)


# ── PROVIDERS ────────────────────────────────────────────────────────────────

def t_providers_import():
    from src.providers import (
        BaseCloudProvider, AzurePricingProvider,
        AWSPricingProvider, GCPPricingProvider,
    )
    assert issubclass(AzurePricingProvider, BaseCloudProvider)
    assert issubclass(AWSPricingProvider, BaseCloudProvider)
    assert issubclass(GCPPricingProvider, BaseCloudProvider)

def t_providers_azure_methods():
    from src.providers.azure_provider import AzurePricingProvider
    import inspect
    methods = [m for m in dir(AzurePricingProvider) if not m.startswith("_")]
    assert "search_prices" in methods
    assert "get_sku_prices" in methods
    assert "list_regions" in methods

def t_providers_aws_mappings():
    from src.providers.aws_provider import AWSPricingProvider
    p = AWSPricingProvider()
    assert len(p._SERVICE_CATEGORY_MAP) >= 45
    assert len(p._REGION_MAP) >= 26

def t_providers_gcp_mappings():
    from src.providers.gcp_provider import GCPPricingProvider
    p = GCPPricingProvider()
    assert len(p._SERVICE_CATEGORY_MAP) >= 38

check("providers — all 3 adapters inherit BaseCloudProvider", t_providers_import)
check("providers.azure — search_prices/get_sku_prices/list_regions present", t_providers_azure_methods)
check("providers.aws — 45 service mappings + 26 region mappings", t_providers_aws_mappings)
check("providers.gcp — 38 service mappings", t_providers_gcp_mappings)


# ── SERVICES ─────────────────────────────────────────────────────────────────

def t_services_import():
    from src.services import PricingCache, PricingService
    from src.services.pricing_cache import PricingCache as PC
    from src.services.pricing_service import PricingService as PS
    assert PricingCache is PC
    assert PricingService is PS

def t_services_interface():
    from src.services.pricing_service import PricingService
    import inspect
    methods = [m for m in dir(PricingService) if not m.startswith("_")]
    for expected in ["search_prices", "get_sku_prices", "compare_across_providers",
                     "register_provider", "initialize", "cache_stats", "close"]:
        assert expected in methods, f"Missing method: {expected}"

check("services — PricingCache + PricingService import", t_services_import)
check("services.pricing_service — all facade methods present", t_services_interface)


# ── ORCHESTRATOR ─────────────────────────────────────────────────────────────

def t_orchestrator_state():
    from src.orchestrator.state import (
        OrchestratorState, AgentStatus, AgentExecution,
        SizedWorkloadResult, create_initial_state,
    )
    s = create_initial_state("req-001", "TestProject", "I need a Kubernetes cluster")
    assert s["request_id"] == "req-001"
    assert s["project_name"] == "TestProject"
    assert isinstance(s["messages"], list)
    assert isinstance(s["agent_executions"], dict)
    assert s["conversation"]["requirements_complete"] is False

def t_orchestrator_init():
    from src.orchestrator import (
        OrchestratorState, create_initial_state,
        AgentStatus, AgentExecution, SizedWorkloadResult,
    )

def t_agent_execution_lifecycle():
    from src.orchestrator.state import AgentExecution, AgentStatus
    from datetime import datetime, timezone
    exec = AgentExecution(
        agent_name="clarifier",
        status=AgentStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    assert exec.elapsed_ms >= 0
    exec.status = AgentStatus.COMPLETED
    assert exec.status == AgentStatus.COMPLETED

check("orchestrator.state — OrchestratorState + create_initial_state", t_orchestrator_state)
check("orchestrator.__init__ — all symbols exported", t_orchestrator_init)
check("orchestrator — AgentExecution lifecycle (RUNNING→COMPLETED)", t_agent_execution_lifecycle)


# ── AGENTS ───────────────────────────────────────────────────────────────────

def t_clarifier_imports():
    from src.agents.clarifier import (
        run_clarifier_node,
        _parse_environment, _parse_tier, _parse_providers,
        _parse_budget, _parse_compliance, _extract_workloads_from_text,
        _generate_clarification_questions, _apply_clarification_to_request,
        REQUIRED_QUESTIONS, RECOMMENDED_QUESTIONS,
    )
    assert len(REQUIRED_QUESTIONS) == 4
    assert len(RECOMMENDED_QUESTIONS) == 4

def t_clarifier_parse_environment():
    from src.agents.clarifier import _parse_environment
    from src.models.workload import EnvironmentType
    assert _parse_environment("production") == EnvironmentType.PRODUCTION
    assert _parse_environment("staging") == EnvironmentType.STAGING
    assert _parse_environment("dev") == EnvironmentType.DEVELOPMENT
    assert _parse_environment("disaster") == EnvironmentType.DR
    assert _parse_environment("unknown") == EnvironmentType.PRODUCTION

def t_clarifier_parse_utils():
    from src.agents.clarifier import _parse_tier, _parse_providers, _parse_budget, _parse_compliance
    from src.models.workload import WorkloadTier
    from src.models.cloud_resource import CloudProvider
    assert _parse_tier("mission critical") == WorkloadTier.MISSION_CRITICAL
    assert _parse_tier("business critical") == WorkloadTier.BUSINESS_CRITICAL
    assert _parse_tier("non-critical") == WorkloadTier.NON_CRITICAL
    p = _parse_providers("aws and azure")
    assert CloudProvider.AWS in p and CloudProvider.AZURE in p
    assert _parse_budget("$5,000") == 5000.0
    assert _parse_budget("skip") is None
    assert _parse_budget("") is None
    c = _parse_compliance("hipaa, pci-dss")
    assert "hipaa" in c and "pci-dss" in c
    assert _parse_compliance("none") == ["waf"]

def t_clarifier_extract_workloads():
    from src.agents.clarifier import _extract_workloads_from_text
    from src.models.cloud_resource import ServiceCategory
    wls = _extract_workloads_from_text("Need kubernetes cluster and postgres database")
    categories = [w.suggested_category for w in wls]
    # K8s cluster management fee now uses KUBERNETES category (P2 change)
    assert ServiceCategory.KUBERNETES in categories, f"Expected KUBERNETES in {categories}"
    assert ServiceCategory.DATABASE in categories, f"Expected DATABASE in {categories}"

def t_profiler_placeholder():
    import src.agents.profiler  # noqa — just confirm importable

def t_sizer_placeholder():
    import src.agents.sizer  # noqa

def t_finops_placeholder():
    import src.agents.finops  # noqa

def t_rfp_placeholder():
    import src.agents.rfp_writer  # noqa

check("agents.clarifier — imports + question templates (4+4)", t_clarifier_imports)
check("agents.clarifier — _parse_environment all 4 cases", t_clarifier_parse_environment)
check("agents.clarifier — _parse_tier/providers/budget/compliance", t_clarifier_parse_utils)
check("agents.clarifier — _extract_workloads_from_text (k8s + db detected)", t_clarifier_extract_workloads)
check("agents.profiler — placeholder (not yet implemented)", t_profiler_placeholder)
check("agents.sizer — placeholder (not yet implemented)", t_sizer_placeholder)
check("agents.finops — placeholder (not yet implemented)", t_finops_placeholder)
check("agents.rfp_writer — placeholder (not yet implemented)", t_rfp_placeholder)


# ── ENGINES ──────────────────────────────────────────────────────────────────

def t_engines_bin_packing():
    from src.engines.bin_packing import pack_workloads, PackingAlgorithm
    assert PackingAlgorithm.FIRST_FIT_DECREASING is not None
    assert PackingAlgorithm.BEST_FIT_DECREASING is not None

def t_engines_scoring():
    from src.engines.scoring import score_skus, ScoringWeights
    w = ScoringWeights()
    assert abs(w.cost + w.cpu_fit + w.memory_fit + w.generation - 1.0) < 0.001

def t_engines_waf():
    from src.engines.waf_compliance import run_waf_checks
    assert callable(run_waf_checks)

check("engines.bin_packing — pack_workloads + PackingAlgorithm (FFD/BFD)", t_engines_bin_packing)
check("engines.scoring — score_skus + ScoringWeights (sum=1.0)", t_engines_scoring)
check("engines.waf_compliance — run_waf_checks callable", t_engines_waf)


# ── ORCHESTRATOR GRAPH ───────────────────────────────────────────────────────

def t_graph_placeholder():
    import src.orchestrator.graph  # noqa — placeholder

check("orchestrator.graph — placeholder (not yet implemented)", t_graph_placeholder)


# ── API ───────────────────────────────────────────────────────────────────────

def t_api_placeholders():
    import src.api.dependencies
    import src.api.routes.health
    import src.api.routes.orchestration

check("api — all placeholders importable", t_api_placeholders)


# ── PRINT RESULTS ────────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in results if ok)
failed = [(n, e) for n, ok, e in results if not ok]
total = len(results)

print(f"\n{'=' * 65}")
print(f"  Master Codebase Validation — {passed}/{total} checks passed")
print(f"{'=' * 65}")

sections = {
    "Config": [],
    "LLM Factory": [],
    "Models": [],
    "Providers": [],
    "Services": [],
    "Orchestrator": [],
    "Agents": [],
    "Engines": [],
    "API / Graph": [],
}

for name, ok, err in results:
    icon = "✅" if ok else "❌"
    line = f"  {icon} {name}"
    if not ok:
        line += f"\n       ↳ {err}"
    if name.startswith("config"):
        sections["Config"].append(line)
    elif name.startswith("llm"):
        sections["LLM Factory"].append(line)
    elif name.startswith("models"):
        sections["Models"].append(line)
    elif name.startswith("providers"):
        sections["Providers"].append(line)
    elif name.startswith("services"):
        sections["Services"].append(line)
    elif name.startswith("orchestrator"):
        sections["Orchestrator"].append(line)
    elif name.startswith("agents"):
        sections["Agents"].append(line)
    elif name.startswith("engines"):
        sections["Engines"].append(line)
    else:
        sections["API / Graph"].append(line)

for section, lines in sections.items():
    if lines:
        print(f"\n  ── {section} ──")
        for line in lines:
            print(line)

if failed:
    print(f"\n  {'─' * 60}")
    print(f"  ❌ {len(failed)} FAILED:")
    for name, err in failed:
        print(f"     • {name}")
        print(f"       {err}")

print(f"\n{'=' * 65}\n")
sys.exit(0 if not failed else 1)
