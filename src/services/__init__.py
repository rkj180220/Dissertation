"""Services package — business-logic facades for agents and engines.

``PricingService`` is the primary entry point for all pricing data.
It wraps the cloud provider adapters with a transparent SQLite cache.

Usage::

    from src.services import PricingService

    service = PricingService()
    service.register_provider(AzurePricingProvider())
    await service.initialize()

    items = await service.search_prices(
        CloudProvider.AZURE,
        service_name="Virtual Machines",
        region="eastus",
    )
"""

from src.services.pricing_cache import PricingCache
from src.services.pricing_service import PricingService

__all__ = [
    "PricingCache",
    "PricingService",
]
