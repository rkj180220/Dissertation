"""Application settings loaded from environment variables.

Uses pydantic-settings to provide validated, typed configuration
sourced from a .env file or OS environment variables.

Sub-configs use isolated env-var prefixes so there are no collisions:
    APP_*       → AppSettings (top-level)
    LLM_*       → LLMSettings (provider, model, temperature, …)
    AWS_*       → AWSSettings (standard boto3 vars + bedrock_region)
    AZURE_*     → AzureSettings
    GCP_*       → GCPSettings
    LANGFUSE_*  → LangFuseSettings
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Environment(str, Enum):
    """Application runtime environment."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LLMProvider(str, Enum):
    """Supported LLM back-end providers."""

    BEDROCK = "bedrock"
    GEMINI = "gemini"


# ---------------------------------------------------------------------------
# Sub-configs  (each reads .env independently with its own prefix)
# ---------------------------------------------------------------------------


class LLMSettings(BaseSettings):
    """LLM provider configuration.

    Env vars: LLM_PROVIDER, LLM_MODEL, LLM_TEMPERATURE, LLM_MAX_TOKENS.
    """

    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )

    provider: LLMProvider = Field(
        default=LLMProvider.BEDROCK,
        description="Active LLM provider (bedrock | gemini)",
    )
    model: str = Field(
        default="anthropic.claude-3-5-sonnet-20241022-v2:0",
        description="Provider-specific model identifier",
    )
    temperature: float = Field(
        default=0.1, ge=0.0, le=2.0,
        description="Sampling temperature for LLM responses",
    )
    max_tokens: int = Field(
        default=4096, gt=0,
        description="Maximum tokens in LLM response",
    )


class AWSSettings(BaseSettings):
    """AWS configuration for Bedrock LLM and Pricing API.

    Standard boto3 env vars (AWS_ACCESS_KEY_ID, …) are also read by the
    SDK directly, so these serve double duty: pydantic validation **and**
    native SDK consumption.
    """

    model_config = SettingsConfigDict(
        env_prefix="AWS_", env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )

    access_key_id: str = Field(default="", description="AWS access key ID")
    secret_access_key: str = Field(default="", description="AWS secret access key")
    session_token: str = Field(
        default="",
        description="AWS session token (for temporary / SSO credentials)",
    )
    default_region: str = Field(
        default="us-east-1",
        description="Default AWS region for pricing API calls",
    )
    bedrock_region: str = Field(
        default="",
        description="AWS region for Bedrock (falls back to default_region)",
    )

    @property
    def resolved_bedrock_region(self) -> str:
        """Return the effective region for Bedrock API calls."""
        return self.bedrock_region or self.default_region


class AzureSettings(BaseSettings):
    """Azure configuration for Pricing API access."""

    model_config = SettingsConfigDict(
        env_prefix="AZURE_", env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )

    subscription_id: str = Field(default="", description="Azure subscription ID")
    tenant_id: str = Field(default="", description="Azure AD tenant ID")
    client_id: str = Field(default="", description="Service principal client ID")
    client_secret: str = Field(default="", description="Service principal client secret")


class GCPSettings(BaseSettings):
    """GCP configuration for Pricing API access."""

    model_config = SettingsConfigDict(
        env_prefix="GCP_", env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )

    project_id: str = Field(default="", description="GCP project ID")
    credentials_path: str = Field(
        default="",
        description="Path to service-account JSON key file",
    )


class LangFuseSettings(BaseSettings):
    """LangFuse observability / LLM-tracing configuration.

    The LangFuse SDK also reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY,
    and LANGFUSE_HOST from the OS environment.  We mirror them here so
    pydantic validates them at startup and we can gate tracing with
    LANGFUSE_ENABLED.
    """

    model_config = SettingsConfigDict(
        env_prefix="LANGFUSE_", env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )

    public_key: str = Field(default="", description="LangFuse public key")
    secret_key: str = Field(default="", description="LangFuse secret key")
    host: str = Field(
        default="https://cloud.langfuse.com",
        description="LangFuse server URL",
    )
    enabled: bool = Field(
        default=True,
        description="Master switch for LangFuse tracing",
    )

    @property
    def is_configured(self) -> bool:
        """Return True when both API keys are present."""
        return bool(self.public_key and self.secret_key)


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------


class AppSettings(BaseSettings):
    """Root application settings — aggregates all sub-configs.

    Top-level fields use the ``APP_`` prefix (APP_ENV, APP_LOG_LEVEL, …).
    Each nested sub-config loads its own prefix independently so there are
    no collisions.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        extra="ignore",
    )

    # --- Application ----------------------------------------------------------
    env: Environment = Field(
        default=Environment.DEVELOPMENT, description="Runtime environment",
    )
    log_level: str = Field(default="INFO", description="Minimum log level")
    host: str = Field(default="0.0.0.0", description="API server bind address")
    port: int = Field(
        default=8000, ge=1, le=65535, description="API server bind port",
    )

    # --- SKU Cache ------------------------------------------------------------
    sku_cache_db_path: str = Field(
        default="data/sku_cache.db",
        description="Path to SQLite SKU-cache database",
    )
    sku_cache_ttl_hours: int = Field(
        default=24, gt=0,
        description="Hours before cached SKU data is considered stale",
    )

    # --- Sub-configs (each reads .env with its own prefix) --------------------
    llm: LLMSettings = Field(default_factory=LLMSettings)
    aws: AWSSettings = Field(default_factory=AWSSettings)
    azure: AzureSettings = Field(default_factory=AzureSettings)
    gcp: GCPSettings = Field(default_factory=GCPSettings)
    langfuse: LangFuseSettings = Field(default_factory=LangFuseSettings)

    # --- Computed helpers -----------------------------------------------------

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.env == Environment.DEVELOPMENT

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.env == Environment.PRODUCTION

    @property
    def sku_cache_path(self) -> Path:
        """Resolved ``Path`` to the SKU cache database."""
        return Path(self.sku_cache_db_path)


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Create (or return cached) validated settings instance.

    Uses ``lru_cache`` so every call site shares the same object.
    Call ``get_settings.cache_clear()`` in tests to reset.

    Returns:
        Fully-validated ``AppSettings`` instance.
    """
    return AppSettings()
