"""Cloud resource enums and SKU models.

Defines provider-agnostic enumerations (``CloudProvider``,
``ServiceCategory``) and normalised SKU representations used
throughout the system.

The older ``ComputeSKU`` / ``StorageSKU`` classes are still here
because the scoring and bin-packing engines reference them.
They will be refactored when those layers are rebuilt.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CloudProvider(str, Enum):
    """Supported cloud hyperscalers."""

    AWS = "aws"
    AZURE = "azure"
    GCP = "gcp"


class ServiceCategory(str, Enum):
    """Normalised cloud-service taxonomy.

    Maps each provider's native classification to a unified set::

        Azure  serviceFamily / serviceName  →  ServiceCategory
        AWS    productFamily / ServiceCode  →  ServiceCategory
        GCP    category.resourceFamily      →  ServiceCategory
    """

    COMPUTE = "compute"
    """VMs, EC2, Compute Engine — dedicated instance-based compute."""

    SERVERLESS_COMPUTE = "serverless_compute"
    """App Service, Cloud Run, App Runner — managed platform compute."""

    CONTAINER = "container"
    """AKS, EKS, GKE, Container Instances, Fargate."""

    SERVERLESS_FUNCTION = "serverless_function"
    """Azure Functions, AWS Lambda, Cloud Functions."""

    DATABASE = "database"
    """SQL Database, RDS, Cloud SQL, Cosmos DB, DynamoDB."""

    STORAGE = "storage"
    """Blob, S3, Cloud Storage, block / file / object."""

    NETWORKING = "networking"
    """Load Balancer, VPN, CDN, Application Gateway."""

    AI_ML = "ai_ml"
    """Azure ML, SageMaker, Vertex AI."""

    ANALYTICS = "analytics"
    """Synapse, Redshift, BigQuery, Databricks."""

    MANAGEMENT = "management"
    """Monitor, CloudWatch, Operations Suite."""

    SECURITY = "security"
    """WAF, Key Vault, IAM, Security Hub."""

    INTEGRATION = "integration"
    """API Gateway, Event Grid, Pub/Sub, SNS/SQS."""

    IOT = "iot"
    """IoT Hub, IoT Core."""

    OTHER = "other"
    """Catch-all for uncategorised services."""


# ---------------------------------------------------------------------------
# Legacy SKU models (used by engines/ and recommendation.py — will refactor)
# ---------------------------------------------------------------------------


class ComputeSKU(BaseModel):
    """Normalised compute instance / VM SKU.

    .. deprecated::
        Will be replaced by the ``NormalizedPriceItem`` + resource-spec
        approach when the engines layer is rebuilt.
    """

    provider: CloudProvider
    sku_id: str = Field(..., description="Provider-native SKU identifier")
    family: str = Field(..., description="Instance family (e.g., 'm5', 'Standard_D')")
    display_name: str = Field(..., description="Human-readable name (e.g., 'm5.xlarge')")
    region: str = Field(..., description="Deployment region")

    vcpus: int = Field(..., ge=1)
    memory_gb: float = Field(..., gt=0)
    gpu_count: int = Field(default=0, ge=0)
    gpu_type: Optional[str] = Field(default=None)

    architecture: str = Field(default="x86_64", description="CPU arch: x86_64 | arm64")
    generation: Optional[str] = Field(default=None, description="Processor generation tag")

    local_storage_gb: float = Field(default=0, ge=0)
    network_bandwidth_gbps: Optional[float] = Field(default=None, ge=0)

    # --- Pricing (on-demand, hourly USD) ---
    price_per_hour_usd: float = Field(default=0.0, ge=0)

    # --- Metadata ---
    is_burstable: bool = Field(default=False)
    is_spot_available: bool = Field(default=False)
    spot_price_per_hour_usd: Optional[float] = Field(default=None, ge=0)

    @property
    def price_per_month_usd(self) -> float:
        """Approximate monthly cost (730 hours)."""
        return round(self.price_per_hour_usd * 730, 2)


class StorageSKU(BaseModel):
    """Normalised block-storage SKU.

    .. deprecated::
        Will be replaced by the ``NormalizedPriceItem`` + resource-spec
        approach when the engines layer is rebuilt.
    """

    provider: CloudProvider
    sku_id: str
    display_name: str
    region: str

    storage_type: str = Field(description="ssd | hdd | nvme | premium-ssd")
    max_capacity_gb: float = Field(gt=0)
    max_iops: Optional[int] = Field(default=None, ge=0)
    max_throughput_mbps: Optional[float] = Field(default=None, ge=0)

    price_per_gb_month_usd: float = Field(default=0.0, ge=0)
    price_per_iops_month_usd: float = Field(default=0.0, ge=0)
