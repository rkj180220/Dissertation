"""Shared pytest fixtures for Cloud Orchestrator IDSS tests.

Provides lightweight mocks for:
- LLM (BaseChatModel) — returns configurable canned responses
- PricingService — returns a fixed list of NormalizedPriceItem
- Common data objects (WorkloadRequirement, NormalizedPriceItem, …)
- Pre-built OrchestratorState for agent unit tests
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.conversation import (
    ChatMessage,
    ConversationState,
    MessageRole,
)
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.models.workload import (
    EnvironmentType,
    ResourceSpec,
    WorkloadRequirement,
    WorkloadRequest,
    WorkloadTier,
)
from src.orchestrator.state import AgentExecution, AgentStatus, OrchestratorState, SizedWorkloadResult

_EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# NormalizedPriceItem helpers
# ---------------------------------------------------------------------------


def make_price_item(
    *,
    sku_name: str = "m5.large",
    provider: CloudProvider = CloudProvider.AWS,
    service_name: str = "AmazonEC2",
    service_category: ServiceCategory = ServiceCategory.COMPUTE,
    region: str = "us-east-1",
    unit_price: float = 0.096,
    pricing_tier: PricingTier = PricingTier.ON_DEMAND,
    vcpus: int = 2,
    memory_gb: float = 8.0,
) -> NormalizedPriceItem:
    """Build a minimal NormalizedPriceItem for tests."""
    return NormalizedPriceItem(
        provider=provider,
        service_name=service_name,
        service_category=service_category,
        sku_id=f"test-{sku_name}",
        sku_name=sku_name,
        product_name=f"Test {sku_name}",
        region=region,
        retail_price=unit_price,
        unit_price=unit_price,
        unit_of_measure="1 Hour",
        pricing_tier=pricing_tier,
        effective_date=_EPOCH,
        attributes={"vcpus": vcpus, "memory_gb": memory_gb},
    )


# ---------------------------------------------------------------------------
# Fixtures — pricing service
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_price_item() -> NormalizedPriceItem:
    """A single on-demand compute SKU (m5.large equivalent)."""
    return make_price_item()


@pytest.fixture()
def sample_price_items() -> list[NormalizedPriceItem]:
    """Three candidate compute SKUs with varying specs and prices."""
    return [
        make_price_item(sku_name="m5.large",  vcpus=2,  memory_gb=8.0,  unit_price=0.096),
        make_price_item(sku_name="m5.xlarge", vcpus=4,  memory_gb=16.0, unit_price=0.192),
        make_price_item(sku_name="m5.2xlarge",vcpus=8,  memory_gb=32.0, unit_price=0.384),
    ]


@pytest.fixture()
def mock_pricing_service(sample_price_items: list[NormalizedPriceItem]) -> MagicMock:
    """Mock PricingService: search_prices returns sample_price_items."""
    svc = MagicMock()
    svc.search_prices = AsyncMock(return_value=sample_price_items)
    svc.get_sku_prices = AsyncMock(return_value=sample_price_items)
    svc.compare_across_providers = AsyncMock(
        return_value={p: sample_price_items for p in CloudProvider}
    )
    svc._initialized = True
    return svc


# ---------------------------------------------------------------------------
# Fixtures — LLM
# ---------------------------------------------------------------------------


def _make_llm_response(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    return msg


@pytest.fixture()
def mock_llm() -> MagicMock:
    """Mock BaseChatModel that returns a short canned response."""
    llm = MagicMock()
    llm.ainvoke = AsyncMock(
        return_value=_make_llm_response(
            "This is a mock LLM response for testing. "
            "The recommendation is to use the provided cloud resources efficiently."
        )
    )
    return llm


# ---------------------------------------------------------------------------
# Fixtures — workload objects
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_workload_requirement() -> WorkloadRequirement:
    """A standard COMPUTE workload requirement."""
    return WorkloadRequirement(
        name="Web API",
        description="REST API backend",
        suggested_category=ServiceCategory.COMPUTE,
        resources=ResourceSpec(vcpus=2, memory_gb=4.0, storage_gb=50.0),
    )


@pytest.fixture()
def container_workload_requirement() -> WorkloadRequirement:
    """A CONTAINER workload with millicore/MB specs (K8s microservice)."""
    return WorkloadRequirement(
        name="Auth Service",
        description="Authentication microservice",
        suggested_category=ServiceCategory.CONTAINER,
        resources=ResourceSpec(
            cpu_request_millicores=500,
            memory_request_mb=512,
            replicas=3,
        ),
    )


@pytest.fixture()
def k8s_mgmt_workload() -> WorkloadRequirement:
    """Kubernetes cluster management fee workload."""
    return WorkloadRequirement(
        name="Kubernetes Cluster Management",
        description="EKS/AKS/GKE control-plane fee",
        suggested_category=ServiceCategory.KUBERNETES,
        notes="cluster_management_fee",
        spot_eligible=False,
        resources=ResourceSpec(),
    )


@pytest.fixture()
def sample_workload_request(
    sample_workload_requirement: WorkloadRequirement,
) -> WorkloadRequest:
    """Minimal WorkloadRequest with one COMPUTE workload."""
    return WorkloadRequest(
        project_name="Test Project",
        environment=EnvironmentType.PRODUCTION,
        tier=WorkloadTier.BUSINESS_CRITICAL,
        providers=[CloudProvider.AWS],
        workloads=[sample_workload_requirement],
    )


# ---------------------------------------------------------------------------
# Fixtures — OrchestratorState
# ---------------------------------------------------------------------------


@pytest.fixture()
def initial_state() -> OrchestratorState:
    """Minimal OrchestratorState for testing individual agents."""
    from src.orchestrator.state import create_initial_state

    return create_initial_state(
        request_id="test-req-001",
        project_name="Test Project",
        raw_user_input="I need a web API on AWS with 2 vCPUs and 4 GB RAM.",
    )


@pytest.fixture()
def state_with_workload_request(
    initial_state: OrchestratorState,
    sample_workload_request: WorkloadRequest,
) -> OrchestratorState:
    """OrchestratorState pre-populated with a WorkloadRequest (post-Clarifier)."""
    state = dict(initial_state)
    state["workload_request"] = sample_workload_request
    state["conversation"] = ConversationState(
        requirements_complete=True,
        messages=[],
    )
    return state  # type: ignore[return-value]


@pytest.fixture()
def sized_result_aws(sample_price_item: NormalizedPriceItem) -> SizedWorkloadResult:
    """A single SizedWorkloadResult for AWS compute."""
    return SizedWorkloadResult(
        workload_name="Web API",
        provider=CloudProvider.AWS,
        selected_sku=sample_price_item,
        monthly_cost_usd=70.08,
        fit_score=0.85,
        rationale="Best fit for 2 vCPU / 4 GB workload.",
    )

