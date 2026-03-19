"""Pricing models — normalised and legacy.

The primary model is ``NormalizedPriceItem``: a single provider-agnostic
price record that every cloud provider adapter returns.  One SKU can
produce multiple ``NormalizedPriceItem`` rows (one per pricing tier).

The older ``SKUPricing`` class is retained for backward compatibility
with ``recommendation.py`` and will be removed when that layer is
refactored.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.models.cloud_resource import CloudProvider, ServiceCategory


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PricingTier(str, Enum):
    """Pricing commitment / discount tier."""

    ON_DEMAND = "on_demand"
    SPOT = "spot"
    LOW_PRIORITY = "low_priority"
    RESERVED_1YR = "reserved_1yr"
    RESERVED_3YR = "reserved_3yr"
    SAVINGS_PLAN_1YR = "savings_plan_1yr"
    SAVINGS_PLAN_3YR = "savings_plan_3yr"
    DEV_TEST = "dev_test"


# ---------------------------------------------------------------------------
# The NEW normalised price model
# ---------------------------------------------------------------------------

HOURS_PER_MONTH = 730
"""Standard hours-per-month used across all cloud providers for cost estimation."""


class NormalizedPriceItem(BaseModel):
    """A single normalised price record from any cloud provider.

    Every provider adapter (Azure, AWS, GCP) transforms its raw API
    response into instances of this model.  Downstream agents and
    engines only ever see ``NormalizedPriceItem`` — never raw
    provider-specific shapes.

    One SKU may yield **multiple** rows — one per pricing tier
    (on-demand, 1-yr reserved, spot, etc.).
    """

    # --- Identity ---------------------------------------------------------
    provider: CloudProvider = Field(description="Source cloud provider")
    service_name: str = Field(
        description="Provider-native service name (e.g. 'Virtual Machines', 'AWSLambda')",
    )
    service_category: ServiceCategory = Field(
        description="Normalised service taxonomy",
    )
    sku_id: str = Field(description="Provider-native SKU identifier")
    sku_name: str = Field(description="Human-readable SKU name")
    product_name: str = Field(description="Full product / offering name")
    meter_name: str = Field(
        default="",
        description="Billing meter name (Azure-specific, empty for others)",
    )

    # --- Location ---------------------------------------------------------
    region: str = Field(description="Provider-native region identifier")

    # --- Pricing ----------------------------------------------------------
    retail_price: float = Field(ge=0, description="List / retail price per unit (USD)")
    unit_price: float = Field(ge=0, description="Effective price per unit (USD)")
    currency: str = Field(default="USD")
    unit_of_measure: str = Field(
        description=(
            "Billing unit as returned by the provider "
            "(e.g. '1 Hour', '1 GB Second', '1 GB/Month', '10K')"
        ),
    )
    pricing_tier: PricingTier = Field(description="Commitment / discount tier")

    # --- Commitment details (optional) ------------------------------------
    reservation_term: str | None = Field(
        default=None,
        description="'1 Year' / '3 Years' — present only for reserved / savings-plan tiers",
    )

    # --- Metadata ---------------------------------------------------------
    effective_date: datetime = Field(description="When this price became effective")
    effective_end_date: datetime | None = Field(
        default=None,
        description="Expiry (present for spot prices, otherwise None)",
    )
    is_primary_meter: bool = Field(
        default=True,
        description="True if this is the primary billing meter for the region",
    )

    # --- Provider-specific extras -----------------------------------------
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form dict for provider-specific metadata that doesn't fit "
            "the normalised schema (e.g. AWS product.attributes, GCP category)"
        ),
    )

    # --- Helpers ----------------------------------------------------------

    @property
    def is_hourly(self) -> bool:
        """True when the billing unit is per-hour."""
        unit = self.unit_of_measure.lower()
        return "hour" in unit or unit in {"hrs", "h"}

    @property
    def is_monthly(self) -> bool:
        """True when the billing unit is per-month."""
        return "month" in self.unit_of_measure.lower()

    @property
    def monthly_cost_estimate(self) -> float | None:
        """Best-effort monthly cost estimate.

        Returns:
            Estimated monthly cost in USD, or ``None`` if the billing unit
            cannot be automatically converted (e.g. per-request pricing
            requires usage volume which we don't know here).

        Note:
            Reservation prices are checked **first** because Azure (and
            others) return the *total upfront cost* with
            ``unitOfMeasure='1 Hour'``, which would otherwise be
            misinterpreted as an hourly rate.
        """
        # Reservations: total upfront → divide by months in the term
        if self.pricing_tier in (
            PricingTier.RESERVED_1YR,
            PricingTier.RESERVED_3YR,
        ):
            months = 12 if self.pricing_tier == PricingTier.RESERVED_1YR else 36
            return round(self.unit_price / months, 6)
        if self.is_hourly:
            return round(self.unit_price * HOURS_PER_MONTH, 6)
        if self.is_monthly:
            return round(self.unit_price, 6)
        return None


# ---------------------------------------------------------------------------
# LEGACY model (used by recommendation.py — will be removed when that
# layer is refactored)
# ---------------------------------------------------------------------------


class SKUPricing(BaseModel):
    """Detailed pricing for a single SKU across commitment tiers.

    .. deprecated::
        Use ``NormalizedPriceItem`` instead.  This class is retained
        only because ``recommendation.py`` references it.
    """

    provider: CloudProvider
    sku_id: str
    region: str
    currency: str = Field(default="USD")

    on_demand_hourly: float = Field(ge=0, description="On-demand hourly rate (USD)")
    reserved_1yr_hourly: Optional[float] = Field(default=None, ge=0)
    reserved_3yr_hourly: Optional[float] = Field(default=None, ge=0)
    spot_hourly: Optional[float] = Field(default=None, ge=0)

    on_demand_monthly: float = Field(ge=0, description="On-demand monthly (730h)")
    reserved_1yr_monthly: Optional[float] = Field(default=None, ge=0)
    reserved_3yr_monthly: Optional[float] = Field(default=None, ge=0)

    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    catalog_version: Optional[str] = Field(default=None)

    @property
    def best_monthly_price(self) -> float:
        """Return the lowest available monthly price across tiers."""
        candidates = [
            self.on_demand_monthly,
            self.reserved_1yr_monthly,
            self.reserved_3yr_monthly,
        ]
        valid = [p for p in candidates if p is not None]
        return min(valid) if valid else self.on_demand_monthly
