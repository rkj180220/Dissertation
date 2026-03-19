"""Abstract base class for cloud provider pricing adapters.

Defines the interface that AWS, Azure, and GCP adapters must
implement.  Every concrete adapter transforms provider-native
API responses into ``NormalizedPriceItem`` instances — the only
model that downstream agents and engines see.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import structlog

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier

logger = structlog.get_logger()


class BaseCloudProvider(ABC):
    """Abstract cloud provider pricing adapter.

    Concrete providers must implement:

    * ``search_prices`` — flexible pricing search by service / region / filters.
    * ``get_sku_prices`` — all tiers for a single SKU.
    * ``list_regions``   — available regions.

    Common helpers (logging, pagination wrappers) live in this base.
    """

    provider: CloudProvider

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def search_prices(
        self,
        *,
        service_name: str | None = None,
        service_category: ServiceCategory | None = None,
        region: str | None = None,
        sku_name: str | None = None,
        pricing_tier: PricingTier | None = None,
        max_results: int = 100,
    ) -> list[NormalizedPriceItem]:
        """Search for pricing items matching the given filters.

        At least one of ``service_name`` or ``service_category`` should
        be provided to avoid unbounded result sets.

        Args:
            service_name: Provider-native service name (e.g. "Virtual Machines").
            service_category: Normalised service taxonomy filter.
            region: Provider-native region identifier (e.g. "eastus").
            sku_name: Restrict to a specific SKU name pattern.
            pricing_tier: Filter by commitment tier.
            max_results: Maximum items to return (after pagination).

        Returns:
            List of normalised price items.
        """
        ...

    @abstractmethod
    async def get_sku_prices(
        self,
        sku_name: str,
        region: str,
    ) -> list[NormalizedPriceItem]:
        """Get all pricing tiers for a specific SKU in a region.

        Args:
            sku_name: Provider-native SKU / ARM SKU name.
            region: Provider-native region identifier.

        Returns:
            All pricing rows for that SKU (on-demand, reserved, spot…).
        """
        ...

    @abstractmethod
    async def list_regions(self) -> list[str]:
        """Return available deployment regions for this provider.

        Returns:
            Sorted list of region identifiers.
        """
        ...

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # noqa: D105
        return f"<{type(self).__name__} provider={self.provider.value}>"
