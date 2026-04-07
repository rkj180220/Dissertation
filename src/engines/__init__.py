"""Algorithmic engines package.

Provides shared helper utilities for extracting compute specifications
from ``NormalizedPriceItem.attributes`` — used by the bin-packing,
scoring, and WAF compliance engines.

The ``attributes`` dict is populated by cloud provider adapters.
Standardised keys (``vcpus``, ``memory_gb``, ``gpu_count``,
``generation``) are preferred; AWS-native keys (``vcpu``, ``memory``,
``gpu``, ``currentGeneration``) are supported as fallbacks.
"""

from __future__ import annotations

import re
import structlog

from src.models.pricing import NormalizedPriceItem

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Attribute extraction helpers
# ---------------------------------------------------------------------------


def extract_vcpus(item: NormalizedPriceItem) -> int:
    """Extract vCPU count from a ``NormalizedPriceItem``'s attributes.

    Lookup order:
        1. ``vcpus`` (standardised key)
        2. ``vcpu``  (AWS product attribute)

    Args:
        item: The price item to inspect.

    Returns:
        vCPU count, or ``0`` if the value cannot be determined.
    """
    attrs = item.attributes
    for key in ("vcpus", "vcpu"):
        if key in attrs:
            try:
                return int(attrs[key])
            except (ValueError, TypeError):
                pass
    logger.debug(
        "vcpus_not_extractable",
        sku_name=item.sku_name,
        provider=item.provider.value,
    )
    return 0


def extract_memory_gb(item: NormalizedPriceItem) -> float:
    """Extract memory in GiB from a ``NormalizedPriceItem``'s attributes.

    Lookup order:
        1. ``memory_gb`` (standardised key — numeric)
        2. ``memory``    (AWS format, e.g. ``"16 GiB"``)

    Args:
        item: The price item to inspect.

    Returns:
        Memory in GiB, or ``0.0`` if the value cannot be determined.
    """
    attrs = item.attributes
    if "memory_gb" in attrs:
        try:
            return float(attrs["memory_gb"])
        except (ValueError, TypeError):
            pass

    # AWS-style: "16 GiB", "0.5 GiB", etc.
    if "memory" in attrs:
        raw = str(attrs["memory"])
        match = re.search(r"([\d.]+)", raw)
        if match:
            val = float(match.group(1))
            lower = raw.lower()
            if "mib" in lower or "mb" in lower:
                return round(val / 1024, 2)
            return val  # assume GiB when no unit or GiB/GB suffix

    logger.debug(
        "memory_gb_not_extractable",
        sku_name=item.sku_name,
        provider=item.provider.value,
    )
    return 0.0


def extract_gpu_count(item: NormalizedPriceItem) -> int:
    """Extract GPU count from a ``NormalizedPriceItem``'s attributes.

    Lookup order:
        1. ``gpu_count`` (standardised key)
        2. ``gpu``       (AWS product attribute)

    Args:
        item: The price item to inspect.

    Returns:
        GPU count, or ``0`` if the value cannot be determined.
    """
    attrs = item.attributes
    for key in ("gpu_count", "gpu"):
        if key in attrs:
            try:
                return int(attrs[key])
            except (ValueError, TypeError):
                pass
    return 0


def extract_generation(item: NormalizedPriceItem) -> str:
    """Extract processor generation hint from a ``NormalizedPriceItem``.

    Lookup order:
        1. ``generation``        (standardised key — free-text)
        2. ``currentGeneration`` (AWS: ``"Yes"`` / ``"No"``)

    Args:
        item: The price item to inspect.

    Returns:
        Generation string, or ``""`` if not available.
    """
    attrs = item.attributes
    if "generation" in attrs:
        return str(attrs["generation"])
    if "currentGeneration" in attrs:
        return "current" if attrs["currentGeneration"] == "Yes" else "previous"
    return ""
