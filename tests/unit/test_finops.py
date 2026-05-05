"""Unit tests for FinOps agent — pure-function logic only (no LLM, no I/O)."""
from __future__ import annotations

import pytest

from src.agents.finops import (
    _CATEGORY_TO_COST_FIELD,
    _SPOT_INELIGIBLE,
    _compute_tco,
    _group_results_by_provider,
    _resolve_category_for_result,
)
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier
from src.orchestrator.state import SizedWorkloadResult


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _sku(provider: CloudProvider = CloudProvider.AWS) -> NormalizedPriceItem:
    from datetime import datetime, timezone
    return NormalizedPriceItem(
        provider=provider,
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


def _result(
    workload_name: str = "Web API",
    provider: CloudProvider = CloudProvider.AWS,
    monthly_cost: float = 70.08,
) -> SizedWorkloadResult:
    return SizedWorkloadResult(
        workload_name=workload_name,
        provider=provider,
        selected_sku=_sku(provider),
        monthly_cost_usd=monthly_cost,
        fit_score=0.85,
        rationale="test",
    )


# ---------------------------------------------------------------------------
# _compute_tco
# ---------------------------------------------------------------------------


class TestComputeTco:
    def test_zero_growth_flat_projection(self) -> None:
        # $1000/mo × 12 months × 1 year = $12,000
        assert _compute_tco(1000.0, years=1, annual_growth_pct=0.0) == 12000.0

    def test_zero_growth_multi_year(self) -> None:
        # $1000/mo × 12 × 3 = $36,000
        assert _compute_tco(1000.0, years=3, annual_growth_pct=0.0) == 36000.0

    def test_growth_increases_total(self) -> None:
        flat = _compute_tco(1000.0, years=3, annual_growth_pct=0.0)
        growing = _compute_tco(1000.0, years=3, annual_growth_pct=15.0)
        assert growing > flat

    def test_15pct_growth_1yr_equals_flat(self) -> None:
        """First year is always at the base rate regardless of growth."""
        flat_1yr = _compute_tco(1000.0, years=1, annual_growth_pct=0.0)
        growth_1yr = _compute_tco(1000.0, years=1, annual_growth_pct=15.0)
        assert flat_1yr == growth_1yr

    def test_5yr_tco_is_reasonable(self) -> None:
        """$1000/mo with 15% growth over 5 years should be $60k–$90k."""
        tco = _compute_tco(1000.0, years=5, annual_growth_pct=15.0)
        assert 60_000 < tco < 100_000

    def test_zero_monthly_returns_zero(self) -> None:
        assert _compute_tco(0.0, years=5, annual_growth_pct=15.0) == 0.0

    def test_result_is_rounded_to_cents(self) -> None:
        tco = _compute_tco(999.99, years=1, annual_growth_pct=0.0)
        # Should be rounded to 2 decimal places
        assert tco == round(tco, 2)


# ---------------------------------------------------------------------------
# _CATEGORY_TO_COST_FIELD
# ---------------------------------------------------------------------------


class TestCategoryToCostField:
    def test_compute_maps_to_compute_field(self) -> None:
        assert _CATEGORY_TO_COST_FIELD[ServiceCategory.COMPUTE] == "compute_monthly_usd"

    def test_kubernetes_maps_to_kubernetes_field(self) -> None:
        assert _CATEGORY_TO_COST_FIELD[ServiceCategory.KUBERNETES] == "kubernetes_monthly_usd"

    def test_container_maps_to_kubernetes_field(self) -> None:
        # Container node pools are also bucketed under kubernetes
        assert _CATEGORY_TO_COST_FIELD[ServiceCategory.CONTAINER] == "kubernetes_monthly_usd"

    def test_database_maps_to_database_field(self) -> None:
        assert _CATEGORY_TO_COST_FIELD[ServiceCategory.DATABASE] == "database_monthly_usd"

    def test_storage_maps_to_storage_field(self) -> None:
        assert _CATEGORY_TO_COST_FIELD[ServiceCategory.STORAGE] == "storage_monthly_usd"

    def test_networking_maps_to_networking_field(self) -> None:
        assert _CATEGORY_TO_COST_FIELD[ServiceCategory.NETWORKING] == "networking_monthly_usd"

    def test_all_categories_mapped(self) -> None:
        """Every ServiceCategory value must have a cost field mapping."""
        for category in ServiceCategory:
            assert category in _CATEGORY_TO_COST_FIELD, f"Missing: {category}"


# ---------------------------------------------------------------------------
# _SPOT_INELIGIBLE
# ---------------------------------------------------------------------------


class TestSpotIneligible:
    def test_kubernetes_is_spot_ineligible(self) -> None:
        assert ServiceCategory.KUBERNETES in _SPOT_INELIGIBLE

    def test_database_is_spot_ineligible(self) -> None:
        assert ServiceCategory.DATABASE in _SPOT_INELIGIBLE

    def test_storage_is_spot_ineligible(self) -> None:
        assert ServiceCategory.STORAGE in _SPOT_INELIGIBLE

    def test_networking_is_spot_ineligible(self) -> None:
        assert ServiceCategory.NETWORKING in _SPOT_INELIGIBLE

    def test_compute_is_spot_eligible(self) -> None:
        assert ServiceCategory.COMPUTE not in _SPOT_INELIGIBLE

    def test_ai_ml_is_spot_eligible(self) -> None:
        assert ServiceCategory.AI_ML not in _SPOT_INELIGIBLE


# ---------------------------------------------------------------------------
# _group_results_by_provider
# ---------------------------------------------------------------------------


class TestGroupResultsByProvider:
    def test_groups_correctly(self) -> None:
        results = [
            _result("API", CloudProvider.AWS),
            _result("DB",  CloudProvider.AWS),
            _result("API", CloudProvider.AZURE),
        ]
        grouped = _group_results_by_provider(results)
        assert len(grouped[CloudProvider.AWS]) == 2
        assert len(grouped[CloudProvider.AZURE]) == 1

    def test_empty_returns_empty_dict(self) -> None:
        assert _group_results_by_provider([]) == {}

    def test_single_provider(self) -> None:
        results = [_result("API", CloudProvider.GCP)]
        grouped = _group_results_by_provider(results)
        assert CloudProvider.GCP in grouped
        assert len(grouped[CloudProvider.GCP]) == 1


# ---------------------------------------------------------------------------
# _resolve_category_for_result
# ---------------------------------------------------------------------------


class TestResolveCategoryForResult:
    def test_infra_prefix_maps_to_networking(self) -> None:
        r = _result(workload_name="[Infra] NAT Gateway")
        result = _resolve_category_for_result(r, workload_components={})
        assert result == ServiceCategory.NETWORKING

    def test_known_workload_uses_profile_category(self) -> None:
        r = _result(workload_name="Web API")
        components = {"Web API": ServiceCategory.DATABASE}
        result = _resolve_category_for_result(r, workload_components=components)
        assert result == ServiceCategory.DATABASE

    def test_unknown_workload_defaults_to_compute(self) -> None:
        r = _result(workload_name="Unknown Service")
        result = _resolve_category_for_result(r, workload_components={})
        assert result == ServiceCategory.COMPUTE
