"""VM specification enrichment for cloud SKU scoring.

Cloud pricing APIs return varying levels of VM metadata:
- **AWS**: Full specs (``vcpu``, ``memory``, ``instanceType``)
- **Azure**: No CPU/memory in API response — must be parsed from SKU name
- **GCP**: Per-component pricing (per-vCPU, per-GB-RAM) — no per-instance SKUs

This module provides:
1. ``parse_azure_vm_specs`` — extract vCPU/memory from Azure ARM SKU names
2. ``compose_gcp_vm_instances`` — synthesize per-instance SKUs from GCP
   component pricing rates

Both are called during SKU scoring to ensure all providers have
comparable ``vcpus`` / ``memory_gb`` attributes for the scoring engine.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import structlog

from src.models.cloud_resource import CloudProvider, ServiceCategory
from src.models.pricing import NormalizedPriceItem, PricingTier

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Azure VM spec parsing
# ---------------------------------------------------------------------------

# Family prefix → memory GB per vCPU (approximate Azure standard ratios)
_AZURE_FAMILY_MEM_RATIO: dict[str, float] = {
    "B":  3.5,    # Burstable
    "D":  4.0,    # General purpose
    "E":  8.0,    # Memory optimized
    "F":  2.0,    # Compute optimized
    "FX": 5.25,   # Compute optimized (large memory)
    "L":  8.0,    # Storage optimized
    "M":  16.0,   # Memory intensive
    "H":  8.0,    # HPC
    "NC": 6.0,    # GPU — compute
    "NV": 4.0,    # GPU — visualization
    "ND": 6.0,    # GPU — deep learning
    "DC": 4.0,    # Confidential compute
    "EC": 8.0,    # Confidential memory-optimized
    "FC": 2.0,    # Confidential compute-optimized
}

# Regex to parse Azure ARM SKU name  e.g. Standard_D4s_v3, Standard_E8-2ads_v7
_AZURE_SKU_RE = re.compile(
    r"^Standard_"
    r"(?P<family>[A-Z]{1,3})"            # Family prefix (D, E, FX, NC, etc.)
    r"(?P<vcpus>\d+)"                    # vCPU count
    r"(?:-(?P<constrained>\d+))?"        # Optional constrained count (E8-2 = 2 active)
    r"(?P<suffixes>[a-z]*)"              # Suffix flags (a=AMD, d=disk, s=storage, etc.)
    r"(?:_(?:cc_)?v(?P<gen>\d+))?"       # Optional generation (v3, v5, cc_v5)
    r"(?:_Promo)?$",                     # Optional promo suffix
    re.IGNORECASE,
)


def parse_azure_vm_specs(arm_sku_name: str) -> dict[str, int | float | str]:
    """Parse vCPU/memory from an Azure ARM SKU name.

    Args:
        arm_sku_name: e.g. ``"Standard_D4s_v3"``, ``"Standard_E8-2ads_v7"``

    Returns:
        Dict with ``vcpus``, ``memory_gb``, ``generation`` keys.
        Empty dict if the name cannot be parsed.
    """
    if not arm_sku_name:
        return {}

    match = _AZURE_SKU_RE.match(arm_sku_name)
    if not match:
        logger.debug("azure_sku_parse_failed", arm_sku_name=arm_sku_name)
        return {}

    family = match.group("family").upper()
    vcpus = int(match.group("vcpus"))
    gen = match.group("gen") or ""

    # Memory: use family ratio × vCPU count
    mem_ratio = _AZURE_FAMILY_MEM_RATIO.get(family, 4.0)
    memory_gb = round(vcpus * mem_ratio, 1)

    result: dict[str, int | float | str] = {
        "vcpus": vcpus,
        "memory_gb": memory_gb,
        "instance_family": family,
    }
    if gen:
        result["generation"] = f"v{gen}"

    return result


# ---------------------------------------------------------------------------
# GCP synthetic VM composition
# ---------------------------------------------------------------------------

# Standard GCP machine type definitions: family → list of (name, vcpus, memory_gb)
_GCP_MACHINE_TYPES: list[tuple[str, str, int, float]] = [
    # family, machine_type_name, vcpus, memory_gb
    ("E2", "e2-micro",        1,   1.0),
    ("E2", "e2-small",        1,   2.0),
    ("E2", "e2-medium",       1,   4.0),
    ("E2", "e2-standard-2",   2,   8.0),
    ("E2", "e2-standard-4",   4,  16.0),
    ("E2", "e2-standard-8",   8,  32.0),
    ("E2", "e2-standard-16", 16,  64.0),
    ("N2", "n2-standard-2",   2,   8.0),
    ("N2", "n2-standard-4",   4,  16.0),
    ("N2", "n2-standard-8",   8,  32.0),
    ("N2", "n2-standard-16", 16,  64.0),
    ("N2", "n2-standard-32", 32, 128.0),
    ("N2", "n2-highmem-2",    2,  16.0),
    ("N2", "n2-highmem-4",    4,  32.0),
    ("N2", "n2-highmem-8",    8,  64.0),
    ("N2", "n2-highcpu-2",    2,   2.0),
    ("N2", "n2-highcpu-4",    4,   4.0),
    ("N2", "n2-highcpu-8",    8,   8.0),
    ("C3", "c3-standard-4",   4,  16.0),
    ("C3", "c3-standard-8",   8,  32.0),
    ("C3", "c3-highmem-4",    4,  32.0),
    ("C3", "c3-highmem-8",    8,  64.0),
    ("C3", "c3-highcpu-4",    4,   8.0),
    ("C3", "c3-highcpu-8",    8,  16.0),
    ("N4", "n4-standard-2",   2,   8.0),
    ("N4", "n4-standard-4",   4,  16.0),
    ("N4", "n4-standard-8",   8,  32.0),
    ("N4", "n4-highmem-2",    2,  16.0),
    ("N4", "n4-highmem-4",    4,  32.0),
    ("N4", "n4-highcpu-2",    2,   2.0),
    ("N4", "n4-highcpu-4",    4,   4.0),
]

# GCP pricing rates by family (per-vCPU/hr, per-GB-RAM/hr) — us-central1 on-demand
# These are official GCP published rates as of 2025.
_GCP_COMPONENT_RATES: dict[str, tuple[float, float]] = {
    # family: (per_vcpu_hour, per_gb_ram_hour)
    "E2": (0.021811, 0.002923),
    "N2": (0.031611, 0.004237),
    "C3": (0.037310, 0.004996),
    "N4": (0.027880, 0.003736),
}


def compose_gcp_vm_instances(
    region: str = "us-central1",
) -> list[NormalizedPriceItem]:
    """Create synthetic GCP VM instance SKUs from component pricing.

    GCP Compute Engine prices VMs as separate vCPU and RAM components.
    This function composes predefined machine types with calculated
    per-instance prices.

    Args:
        region: GCP region for the instances.

    Returns:
        List of ``NormalizedPriceItem`` representing complete VM instances.
    """
    items: list[NormalizedPriceItem] = []
    now = datetime.now(timezone.utc)

    for family, name, vcpus, memory_gb in _GCP_MACHINE_TYPES:
        rates = _GCP_COMPONENT_RATES.get(family)
        if not rates:
            continue
        cpu_rate, ram_rate = rates
        hourly_price = round(vcpus * cpu_rate + memory_gb * ram_rate, 6)

        # Determine sub-family from name
        if "highmem" in name:
            inst_family = "Memory optimized"
        elif "highcpu" in name:
            inst_family = "Compute optimized"
        else:
            inst_family = "General purpose"

        items.append(
            NormalizedPriceItem(
                provider=CloudProvider.GCP,
                service_name="Compute Engine",
                service_category=ServiceCategory.COMPUTE,
                sku_id=f"synthetic-{name}",
                sku_name=name,
                product_name="Compute Engine",
                meter_name="",
                region=region,
                retail_price=hourly_price,
                unit_price=hourly_price,
                currency="USD",
                unit_of_measure="1 Hour",
                pricing_tier=PricingTier.ON_DEMAND,
                reservation_term=None,
                effective_date=now,
                attributes={
                    "vcpus": vcpus,
                    "memory_gb": memory_gb,
                    "instance_family": inst_family,
                    "machine_family": family,
                    "resource_family": "Compute",
                    "resource_group": "VirtualMachine",
                    "generation": family.lower(),
                },
            )
        )

    logger.info("gcp_synthetic_vms_composed", count=len(items), region=region)
    return items
