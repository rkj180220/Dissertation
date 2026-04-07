"""FastAPI dependency-injection providers.

Exposes ``Depends()``-compatible callables that wire the LLM,
PricingService, and compiled LangGraph workflow into route handlers.

Lifecycle
---------
``lifespan()`` is an ASGI lifespan context manager registered on the
FastAPI ``app``.  It:

1. Loads settings and configures observability.
2. Creates the LLM via the factory.
3. Registers all three cloud-provider adapters with ``PricingService``.
4. Initialises the cache (``await pricing_service.initialize()``).
5. Compiles the LangGraph workflow.
6. Stores singletons in ``app.state`` for route access.
7. Closes the pricing-service cache on shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Request
from langchain_core.language_models import BaseChatModel

from src.config.logging_config import configure_observability
from src.config.settings import AppSettings, get_settings
from src.llm.factory import get_llm
from src.orchestrator.graph import build_graph
from src.providers.aws_provider import AWSPricingProvider
from src.providers.azure_provider import AzurePricingProvider
from src.providers.gcp_provider import GCPPricingProvider
from src.services.pricing_service import PricingService

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# ASGI lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application startup / shutdown lifecycle.

    Initialises all shared resources (settings, LLM, pricing service,
    graph) and stores them on ``app.state`` for route handlers.
    """
    log = logger.bind(component="lifespan")

    # --- Settings & observability -----------------------------------------
    settings = get_settings()
    configure_observability(settings)
    log.info("settings_loaded", env=settings.env.value)

    # --- LLM --------------------------------------------------------------
    llm = get_llm(settings.llm, settings.aws)
    log.info("llm_created", provider=settings.llm.provider.value)

    # --- Pricing service --------------------------------------------------
    pricing_service = PricingService(settings)
    pricing_service.register_provider(AWSPricingProvider())
    pricing_service.register_provider(AzurePricingProvider())
    pricing_service.register_provider(GCPPricingProvider())
    await pricing_service.initialize()
    log.info("pricing_service_initialized")

    # --- LangGraph workflow -----------------------------------------------
    graph = build_graph(llm=llm, pricing_service=pricing_service)
    log.info("langgraph_compiled")

    # --- Store singletons on app.state ------------------------------------
    app.state.settings = settings
    app.state.llm = llm
    app.state.pricing_service = pricing_service
    app.state.graph = graph

    log.info("startup_complete")
    yield

    # --- Shutdown ---------------------------------------------------------
    log.info("shutdown_started")
    await pricing_service.close()
    log.info("shutdown_complete")


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def get_app_settings(request: Request) -> AppSettings:
    """Return the cached ``AppSettings`` singleton."""
    return request.app.state.settings


def get_llm_dep(request: Request) -> BaseChatModel:
    """Return the shared ``BaseChatModel`` instance."""
    return request.app.state.llm


def get_pricing_service(request: Request) -> PricingService:
    """Return the initialised ``PricingService``."""
    return request.app.state.pricing_service


def get_compiled_graph(request: Request) -> Any:
    """Return the compiled LangGraph ``StateGraph``."""
    return request.app.state.graph
