"""Cloud provider pricing adapters package.

Usage::

    from src.providers import AzurePricingProvider, AWSPricingProvider, GCPPricingProvider

    azure = AzurePricingProvider()
    items = await azure.search_prices(service_name="Virtual Machines", region="eastus")

    aws = AWSPricingProvider()
    items = await aws.search_prices(service_name="AmazonEC2", region="us-east-1")

    gcp = GCPPricingProvider()
    items = await gcp.search_prices(service_name="Compute Engine", region="us-central1")
"""

from src.providers.aws_provider import AWSPricingProvider
from src.providers.azure_provider import AzurePricingProvider
from src.providers.base_provider import BaseCloudProvider
from src.providers.gcp_provider import GCPPricingProvider

__all__ = [
    "BaseCloudProvider",
    "AWSPricingProvider",
    "AzurePricingProvider",
    "GCPPricingProvider",
]
