"""Configuration package for Cloud Orchestrator IDSS.

Quick imports::

    from src.config import get_settings, configure_observability, AppSettings
"""

from src.config.logging_config import configure_observability
from src.config.settings import (
    AppSettings,
    AWSSettings,
    AzureSettings,
    Environment,
    GCPSettings,
    LangFuseSettings,
    LLMProvider,
    LLMSettings,
    get_settings,
)

__all__ = [
    "AppSettings",
    "AWSSettings",
    "AzureSettings",
    "Environment",
    "GCPSettings",
    "LangFuseSettings",
    "LLMProvider",
    "LLMSettings",
    "configure_observability",
    "get_settings",
]
