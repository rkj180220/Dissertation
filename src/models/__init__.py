"""Pydantic data models package.

New models (use these for all new code):
    ``ServiceCategory``     — normalised cloud-service taxonomy
    ``PricingTier``         — commitment / discount tier enum
    ``NormalizedPriceItem`` — the universal provider-agnostic price record
    ``WorkloadRequirement`` — generic, category-agnostic workload spec
    ``ResourceSpec``        — concrete resource needs
    ``ConversationState``   — multi-turn clarification dialogue
    ``ChatMessage``         — single chat turn

Legacy models (kept for backward compatibility with engines/):
    ``ComputeSKU``, ``StorageSKU``, ``SKUPricing``
    ``VMWorkload``, ``ContainerWorkload``, ``StorageRequirement``
"""

from src.models.cloud_resource import (
    CloudProvider,
    ComputeSKU,
    ServiceCategory,
    StorageSKU,
)
from src.models.conversation import (
    ChatMessage,
    ClarificationQuestion,
    ClarificationStatus,
    ConversationState,
    MessageRole,
)
from src.models.pricing import (
    HOURS_PER_MONTH,
    NormalizedPriceItem,
    PricingTier,
    SKUPricing,
)
from src.models.recommendation import (
    AncillaryCost,
    BinPackingResult,
    CloudRecommendation,
    ComplianceCheckResult,
    ComplianceReport,
    CostComparison,
    PackedNode,
    ProviderCostBreakdown,
)
from src.models.workload import (
    ComponentProfile,
    ContainerWorkload,
    EnvironmentType,
    ResourceSpec,
    ScalingPattern,
    StorageRequirement,
    VMWorkload,
    WorkloadProfile,
    WorkloadRequest,
    WorkloadRequirement,
    WorkloadTier,
)

__all__ = [
    # --- Enums ---
    "CloudProvider",
    "ServiceCategory",
    "PricingTier",
    "EnvironmentType",
    "WorkloadTier",
    "ScalingPattern",
    "MessageRole",
    "ClarificationStatus",
    # --- New normalised models ---
    "NormalizedPriceItem",
    "HOURS_PER_MONTH",
    # --- Workloads (new) ---
    "WorkloadRequirement",
    "ResourceSpec",
    "ComponentProfile",
    "WorkloadProfile",
    "WorkloadRequest",
    # --- Conversation ---
    "ChatMessage",
    "ClarificationQuestion",
    "ConversationState",
    # --- Recommendations ---
    "AncillaryCost",
    "PackedNode",
    "BinPackingResult",
    "ProviderCostBreakdown",
    "CostComparison",
    "ComplianceCheckResult",
    "ComplianceReport",
    "CloudRecommendation",
    # --- Legacy (backward compat) ---
    "ComputeSKU",
    "StorageSKU",
    "SKUPricing",
    "VMWorkload",
    "ContainerWorkload",
    "StorageRequirement",
]
