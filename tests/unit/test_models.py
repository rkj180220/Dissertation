"""Unit tests for Pydantic data models.

Covers: cloud_resource, workload, recommendation (P2 additions).
"""
from __future__ import annotations

import pytest

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.models.recommendation import AncillaryCost, ProviderCostBreakdown
from src.models.workload import ResourceSpec, WorkloadRequirement, WorkloadTier


# ---------------------------------------------------------------------------
# ServiceCategory
# ---------------------------------------------------------------------------


class TestServiceCategory:
    def test_has_16_values(self) -> None:
        assert len(ServiceCategory) == 16

    def test_kubernetes_value(self) -> None:
        assert ServiceCategory.KUBERNETES.value == "kubernetes"

    def test_kubernetes_distinct_from_container(self) -> None:
        assert ServiceCategory.KUBERNETES != ServiceCategory.CONTAINER

    def test_all_providers_exist(self) -> None:
        for name in ("aws", "azure", "gcp"):
            CloudProvider(name)  # must not raise

    def test_standard_categories_present(self) -> None:
        required = {
            ServiceCategory.COMPUTE,
            ServiceCategory.CONTAINER,
            ServiceCategory.KUBERNETES,
            ServiceCategory.DATABASE,
            ServiceCategory.STORAGE,
            ServiceCategory.NETWORKING,
            ServiceCategory.AI_ML,
        }
        assert required.issubset(set(ServiceCategory))


# ---------------------------------------------------------------------------
# WorkloadRequirement — P2 SLA fields
# ---------------------------------------------------------------------------


class TestWorkloadRequirementP2Fields:
    def test_defaults_all_none_or_true(self) -> None:
        wr = WorkloadRequirement(
            name="svc",
            description="",
            suggested_category=ServiceCategory.COMPUTE,
            resources=ResourceSpec(),
        )
        assert wr.latency_p99_ms is None
        assert wr.throughput_rps is None
        assert wr.concurrent_users is None
        assert wr.uptime_sla is None
        assert wr.rpo_minutes is None
        assert wr.rto_minutes is None
        assert wr.data_growth_rate_pct is None
        assert wr.spot_eligible is True  # default

    def test_sla_fields_accept_valid_values(self) -> None:
        wr = WorkloadRequirement(
            name="svc",
            description="",
            suggested_category=ServiceCategory.COMPUTE,
            resources=ResourceSpec(),
            latency_p99_ms=200,
            throughput_rps=5000,
            concurrent_users=10000,
            uptime_sla=99.99,
            rpo_minutes=60,
            rto_minutes=240,
            data_growth_rate_pct=15.0,
            spot_eligible=False,
        )
        assert wr.latency_p99_ms == 200
        assert wr.uptime_sla == 99.99
        assert wr.spot_eligible is False

    def test_uptime_sla_bounded_0_to_100(self) -> None:
        with pytest.raises(Exception):
            WorkloadRequirement(
                name="svc",
                description="",
                suggested_category=ServiceCategory.COMPUTE,
                resources=ResourceSpec(),
                uptime_sla=101.0,
            )

    def test_latency_non_negative(self) -> None:
        with pytest.raises(Exception):
            WorkloadRequirement(
                name="svc",
                description="",
                suggested_category=ServiceCategory.COMPUTE,
                resources=ResourceSpec(),
                latency_p99_ms=-1,
            )

    def test_kubernetes_spot_eligible_false_explicit(self) -> None:
        wr = WorkloadRequirement(
            name="K8s Cluster Management",
            description="EKS fee",
            suggested_category=ServiceCategory.KUBERNETES,
            notes="cluster_management_fee",
            spot_eligible=False,
            resources=ResourceSpec(),
        )
        assert wr.spot_eligible is False
        assert wr.suggested_category == ServiceCategory.KUBERNETES


# ---------------------------------------------------------------------------
# AncillaryCost (P2)
# ---------------------------------------------------------------------------


class TestAncillaryCost:
    def test_minimal_construction(self) -> None:
        from src.models.cloud_resource import CloudProvider, ServiceCategory

        ac = AncillaryCost(
            provider=CloudProvider.AWS,
            category=ServiceCategory.NETWORKING,
            item_name="NAT Gateway",
            monthly_cost_usd=32.0,
        )
        assert ac.monthly_cost_usd == 32.0
        assert ac.unit == "fixed"
        assert ac.quantity == 1.0
        assert ac.notes == ""

    def test_negative_cost_rejected(self) -> None:
        from src.models.cloud_resource import CloudProvider, ServiceCategory

        with pytest.raises(Exception):
            AncillaryCost(
                provider=CloudProvider.AWS,
                category=ServiceCategory.NETWORKING,
                item_name="NAT Gateway",
                monthly_cost_usd=-5.0,
            )

    def test_full_construction(self) -> None:
        from src.models.cloud_resource import CloudProvider, ServiceCategory

        ac = AncillaryCost(
            provider=CloudProvider.AZURE,
            category=ServiceCategory.NETWORKING,
            item_name="Data Transfer",
            monthly_cost_usd=9.0,
            unit="GB",
            quantity=100.0,
            notes="Egress only",
        )
        assert ac.quantity == 100.0
        assert ac.unit == "GB"


# ---------------------------------------------------------------------------
# ProviderCostBreakdown — ancillary_costs (P2)
# ---------------------------------------------------------------------------


class TestProviderCostBreakdown:
    def test_ancillary_costs_default_empty(self) -> None:
        from src.models.cloud_resource import CloudProvider

        pcd = ProviderCostBreakdown(
            provider=CloudProvider.AWS,
            total_monthly_usd=100.0,
            compute_monthly_usd=100.0,
        )
        assert pcd.ancillary_costs == []

    def test_ancillary_costs_accepts_list(self) -> None:
        from src.models.cloud_resource import CloudProvider, ServiceCategory

        ac = AncillaryCost(
            provider=CloudProvider.AWS,
            category=ServiceCategory.NETWORKING,
            item_name="NAT Gateway",
            monthly_cost_usd=32.0,
        )
        pcd = ProviderCostBreakdown(
            provider=CloudProvider.AWS,
            total_monthly_usd=132.0,
            compute_monthly_usd=100.0,
            ancillary_costs=[ac],
        )
        assert len(pcd.ancillary_costs) == 1
        assert pcd.ancillary_costs[0].item_name == "NAT Gateway"


# ---------------------------------------------------------------------------
# NormalizedPriceItem — monthly_cost_estimate
# ---------------------------------------------------------------------------


class TestNormalizedPriceItem:
    def test_on_demand_monthly_estimate(self) -> None:
        from datetime import datetime, timezone
        item = NormalizedPriceItem(
            provider=CloudProvider.AWS,
            service_name="AmazonEC2",
            service_category=ServiceCategory.COMPUTE,
            sku_id="m5.large",
            sku_name="m5.large",
            product_name="EC2 m5.large",
            region="us-east-1",
            retail_price=0.096,
            unit_price=0.096,
            unit_of_measure="1 Hour",
            pricing_tier=PricingTier.ON_DEMAND,
            effective_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            attributes={},
        )
        # 0.096 * 730 hours/month ≈ 70.08
        assert abs(item.monthly_cost_estimate - 70.08) < 0.01

    def test_attributes_stored(self) -> None:
        from datetime import datetime, timezone
        item = NormalizedPriceItem(
            provider=CloudProvider.AWS,
            service_name="AmazonEC2",
            service_category=ServiceCategory.COMPUTE,
            sku_id="m5.large",
            sku_name="m5.large",
            product_name="EC2 m5.large",
            region="us-east-1",
            retail_price=0.096,
            unit_price=0.096,
            unit_of_measure="1 Hour",
            pricing_tier=PricingTier.ON_DEMAND,
            effective_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            attributes={"vcpus": 2, "memory_gb": 8.0},
        )
        assert item.attributes["vcpus"] == 2
        assert item.attributes["memory_gb"] == 8.0
