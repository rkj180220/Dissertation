"""Unit tests for clarifier parsing logic (all pure-function, no LLM calls)."""
from __future__ import annotations

import pytest

from src.agents.clarifier import (
    _extract_workloads_from_text,
    _parse_budget,
    _parse_compliance,
    _parse_count,
    _parse_environment,
    _parse_providers,
    _parse_tier,
)
from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.workload import EnvironmentType, WorkloadTier


# ---------------------------------------------------------------------------
# _parse_environment
# ---------------------------------------------------------------------------


class TestParseEnvironment:
    def test_production(self) -> None:
        assert _parse_environment("production") == EnvironmentType.PRODUCTION

    def test_prod_abbreviation(self) -> None:
        assert _parse_environment("prod environment") == EnvironmentType.PRODUCTION

    def test_staging(self) -> None:
        assert _parse_environment("staging") == EnvironmentType.STAGING

    def test_development(self) -> None:
        assert _parse_environment("development") == EnvironmentType.DEVELOPMENT

    def test_dev_abbreviation(self) -> None:
        assert _parse_environment("dev") == EnvironmentType.DEVELOPMENT

    def test_disaster_recovery(self) -> None:
        assert _parse_environment("disaster recovery") == EnvironmentType.DR

    def test_dr_abbreviation(self) -> None:
        assert _parse_environment("dr site") == EnvironmentType.DR

    def test_default_is_production(self) -> None:
        assert _parse_environment("unknown") == EnvironmentType.PRODUCTION


# ---------------------------------------------------------------------------
# _parse_tier
# ---------------------------------------------------------------------------


class TestParseTier:
    def test_mission_critical(self) -> None:
        assert _parse_tier("mission critical workload") == WorkloadTier.MISSION_CRITICAL

    def test_business_critical(self) -> None:
        assert _parse_tier("business critical") == WorkloadTier.BUSINESS_CRITICAL

    def test_non_critical(self) -> None:
        assert _parse_tier("non-critical") == WorkloadTier.NON_CRITICAL

    def test_non_keyword(self) -> None:
        assert _parse_tier("non critical") == WorkloadTier.NON_CRITICAL

    def test_default_is_business_critical(self) -> None:
        assert _parse_tier("standard workload") == WorkloadTier.BUSINESS_CRITICAL


# ---------------------------------------------------------------------------
# _parse_providers
# ---------------------------------------------------------------------------


class TestParseProviders:
    def test_aws_only(self) -> None:
        assert _parse_providers("deploy on AWS") == [CloudProvider.AWS]

    def test_azure_only(self) -> None:
        assert _parse_providers("we use Azure") == [CloudProvider.AZURE]

    def test_gcp_only(self) -> None:
        assert _parse_providers("GCP preferred") == [CloudProvider.GCP]

    def test_multiple_providers(self) -> None:
        providers = _parse_providers("AWS and Azure and GCP")
        assert CloudProvider.AWS in providers
        assert CloudProvider.AZURE in providers
        assert CloudProvider.GCP in providers

    def test_default_all_three(self) -> None:
        providers = _parse_providers("no preference")
        assert set(providers) == {CloudProvider.AWS, CloudProvider.AZURE, CloudProvider.GCP}

    def test_case_insensitive(self) -> None:
        providers = _parse_providers("aws gcp azure")
        assert len(providers) == 3


# ---------------------------------------------------------------------------
# _parse_budget
# ---------------------------------------------------------------------------


class TestParseBudget:
    def test_dollar_sign_amount(self) -> None:
        assert _parse_budget("$5000") == 5000.0

    def test_dollar_with_commas(self) -> None:
        assert _parse_budget("$5,000") == 5000.0

    def test_dollar_with_per_month(self) -> None:
        assert _parse_budget("$2,500/mo") == 2500.0

    def test_standalone_number(self) -> None:
        assert _parse_budget("5000") == 5000.0

    def test_standalone_with_per_month(self) -> None:
        assert _parse_budget("3000/month") == 3000.0

    def test_skip_returns_none(self) -> None:
        assert _parse_budget("skip") is None

    def test_none_returns_none(self) -> None:
        assert _parse_budget("none") is None

    def test_empty_returns_none(self) -> None:
        assert _parse_budget("") is None

    def test_inline_number_not_matched(self) -> None:
        # "3 microservices" should NOT be parsed as a budget
        assert _parse_budget("3 microservices on k8s") is None

    def test_decimal_amount(self) -> None:
        assert _parse_budget("$1500.00") == 1500.0


# ---------------------------------------------------------------------------
# _parse_compliance
# ---------------------------------------------------------------------------


class TestParseCompliance:
    def test_hipaa(self) -> None:
        frameworks = _parse_compliance("HIPAA required")
        assert "hipaa" in frameworks

    def test_pci(self) -> None:
        frameworks = _parse_compliance("PCI-DSS compliance needed")
        assert "pci-dss" in frameworks

    def test_sox(self) -> None:
        frameworks = _parse_compliance("SOX audit trail")
        assert "sox" in frameworks

    def test_waf_keyword(self) -> None:
        frameworks = _parse_compliance("WAF only")
        assert "waf" in frameworks

    def test_none_returns_waf_default(self) -> None:
        frameworks = _parse_compliance("none")
        assert frameworks == ["waf"]

    def test_empty_returns_waf_default(self) -> None:
        frameworks = _parse_compliance("")
        assert frameworks == ["waf"]

    def test_no_match_returns_waf(self) -> None:
        frameworks = _parse_compliance("just standard stuff")
        assert frameworks == ["waf"]

    def test_multiple_frameworks(self) -> None:
        frameworks = _parse_compliance("HIPAA and PCI-DSS required")
        assert "hipaa" in frameworks
        assert "pci-dss" in frameworks


# ---------------------------------------------------------------------------
# _parse_count
# ---------------------------------------------------------------------------


class TestParseCount:
    def test_count_microservices(self) -> None:
        count = _parse_count(
            "we have 3 microservices",
            [r"(\d+)\s*(?:micro[\-\s]?services?)"],
        )
        assert count == 3

    def test_no_match_returns_zero(self) -> None:
        count = _parse_count("no services here", [r"(\d+)\s*microservices"])
        assert count == 0

    def test_larger_count(self) -> None:
        count = _parse_count(
            "12 microservices",
            [r"(\d+)\s*(?:micro[\-\s]?services?)"],
        )
        assert count == 12

    def test_first_match_wins_multiple_patterns(self) -> None:
        count = _parse_count(
            "5 services on kubernetes",
            [
                r"(\d+)\s*(?:micro[\-\s]?services?)",
                r"(\d+)\s*(?:services?\s+on\s+kubernetes)",
            ],
        )
        assert count == 5


# ---------------------------------------------------------------------------
# _extract_workloads_from_text
# ---------------------------------------------------------------------------


class TestExtractWorkloads:
    def test_microservices_on_k8s_creates_correct_workloads(self) -> None:
        text = "3 microservices on kubernetes with a postgres database"
        workloads = _extract_workloads_from_text(text)

        names = [w.name for w in workloads]
        categories = {w.suggested_category for w in workloads}

        # Should have 3 CONTAINER microservices
        container_count = sum(1 for w in workloads if w.suggested_category == ServiceCategory.CONTAINER)
        assert container_count == 3

        # Should have a KUBERNETES cluster management entry
        assert ServiceCategory.KUBERNETES in categories

        # Should have a DATABASE entry
        assert ServiceCategory.DATABASE in categories

    def test_kubernetes_mgmt_workload_has_correct_flags(self) -> None:
        text = "3 microservices on k8s"
        workloads = _extract_workloads_from_text(text)
        k8s_mgmt = next(
            (w for w in workloads if w.suggested_category == ServiceCategory.KUBERNETES),
            None,
        )
        assert k8s_mgmt is not None
        assert "cluster_management_fee" in (k8s_mgmt.notes or "")
        assert k8s_mgmt.spot_eligible is False

    def test_database_engine_propagation_postgresql(self) -> None:
        workloads = _extract_workloads_from_text("web api with postgres database")
        db = next((w for w in workloads if w.suggested_category == ServiceCategory.DATABASE), None)
        assert db is not None
        assert db.resources.database_engine == "postgresql"

    def test_database_engine_propagation_mysql(self) -> None:
        workloads = _extract_workloads_from_text("app using mysql database")
        db = next((w for w in workloads if w.suggested_category == ServiceCategory.DATABASE), None)
        assert db is not None
        assert db.resources.database_engine == "mysql"

    def test_api_server_extracted(self) -> None:
        workloads = _extract_workloads_from_text("I need a backend api server")
        categories = {w.suggested_category for w in workloads}
        assert ServiceCategory.COMPUTE in categories

    def test_serverless_extracted(self) -> None:
        workloads = _extract_workloads_from_text("Use lambda for serverless processing")
        categories = {w.suggested_category for w in workloads}
        assert ServiceCategory.SERVERLESS_FUNCTION in categories

    def test_object_storage_extracted(self) -> None:
        workloads = _extract_workloads_from_text("Store files in S3 object storage")
        categories = {w.suggested_category for w in workloads}
        assert ServiceCategory.STORAGE in categories

    def test_load_balancer_added_for_k8s(self) -> None:
        workloads = _extract_workloads_from_text("3 microservices on k8s")
        lb = next(
            (w for w in workloads if w.suggested_category == ServiceCategory.NETWORKING),
            None,
        )
        assert lb is not None

    def test_fallback_for_empty_input(self) -> None:
        workloads = _extract_workloads_from_text("")
        assert len(workloads) >= 1
        assert workloads[0].suggested_category == ServiceCategory.COMPUTE

    def test_ai_ml_workload_extracted(self) -> None:
        workloads = _extract_workloads_from_text("GPU-based machine learning training")
        categories = {w.suggested_category for w in workloads}
        assert ServiceCategory.AI_ML in categories
