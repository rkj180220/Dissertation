"""Model-agnostic LLM factory.

Returns a ``BaseChatModel`` instance based on the active configuration.
Agent / node code **never** imports a provider-specific class — it
receives the model via dependency injection from this factory.

Switching the LLM provider is a **config-only** change (zero code edits
in agents or orchestrator).

Supported providers
-------------------
* **bedrock** — AWS Bedrock Claude via ``langchain-aws[anthropic]`` (primary)
* **gemini**  — Google Gemini via ``langchain-google-genai`` (optional backup)

Usage
-----
::

    from src.llm.factory import get_llm
    from src.config import get_settings

    settings = get_settings()
    llm = get_llm(settings.llm, settings.aws)
    response = await llm.ainvoke("Hello!")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from langchain_core.language_models import BaseChatModel

if TYPE_CHECKING:
    from src.config.settings import AWSSettings, GCPSettings, LLMSettings

logger = structlog.get_logger()


def get_llm(
    llm_settings: LLMSettings,
    aws_settings: AWSSettings | None = None,
    gcp_settings: GCPSettings | None = None,
) -> BaseChatModel:
    """Create a ``BaseChatModel`` for the configured provider.

    Uses **lazy imports** so that optional provider packages (e.g.
    ``langchain-google-genai``) are only required when actually selected.

    Args:
        llm_settings: LLM configuration (provider, model, temperature, …).
        aws_settings: AWS configuration — required when provider is *bedrock*.
        gcp_settings: GCP configuration — used for project ID when provider is *vertexai*.

    Returns:
        A concrete ``BaseChatModel`` ready for ``.invoke()`` / ``.ainvoke()``.

    Raises:
        ValueError: If the provider is unknown or required config is missing.
    """
    log = logger.bind(
        component="llm_factory",
        provider=llm_settings.provider.value,
        model=llm_settings.model,
    )
    log.info(
        "creating_llm",
        temperature=llm_settings.temperature,
        max_tokens=llm_settings.max_tokens,
    )

    provider = llm_settings.provider.value

    if provider == "bedrock":
        model = _create_bedrock(llm_settings, aws_settings)
    elif provider == "gemini":
        model = _create_gemini(llm_settings)
    elif provider == "vertexai":
        model = _create_vertexai(llm_settings, gcp_settings)
    else:
        log.error("unsupported_llm_provider", provider=provider)
        raise ValueError(
            f"Unsupported LLM provider: {provider!r}. "
            "Supported: 'bedrock', 'gemini', 'vertexai'."
        )

    log.info("llm_created", model_type=type(model).__name__)
    return model


# ---------------------------------------------------------------------------
# Provider constructors (lazy imports)
# ---------------------------------------------------------------------------


def _create_bedrock(
    llm_settings: LLMSettings,
    aws_settings: AWSSettings | None,
) -> BaseChatModel:
    """Instantiate an AWS Bedrock Claude chat model.

    Args:
        llm_settings: LLM configuration.
        aws_settings: AWS credentials and region configuration.

    Returns:
        ``ChatBedrockConverse`` instance.
    """
    from langchain_aws import ChatBedrockConverse  # lazy import

    if aws_settings is None:
        raise ValueError(
            "AWSSettings is required when using the 'bedrock' LLM provider."
        )

    kwargs: dict[str, object] = {
        "model": llm_settings.model,
        "temperature": llm_settings.temperature,
        "max_tokens": llm_settings.max_tokens,
        "region_name": aws_settings.resolved_bedrock_region,
    }

    # Only pass explicit credentials when they are set (allows IAM role / SSO fallback)
    if aws_settings.access_key_id and aws_settings.secret_access_key:
        kwargs["credentials_profile_name"] = None  # explicit keys take precedence
        kwargs["aws_access_key_id"] = aws_settings.access_key_id
        kwargs["aws_secret_access_key"] = aws_settings.secret_access_key
        if aws_settings.session_token:
            kwargs["aws_session_token"] = aws_settings.session_token

    return ChatBedrockConverse(**kwargs)  # type: ignore[arg-type]


def _create_gemini(llm_settings: LLMSettings) -> BaseChatModel:
    """Instantiate a Google Gemini chat model.

    Requires the optional ``langchain-google-genai`` package
    (install with ``pip install .[gemini]``).

    The API key is read from the ``GOOGLE_API_KEY`` environment variable
    by the underlying library.

    Args:
        llm_settings: LLM configuration.

    Returns:
        ``ChatGoogleGenerativeAI`` instance.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI  # lazy import
    except ImportError as exc:
        raise ImportError(
            "The 'gemini' LLM provider requires the langchain-google-genai "
            "package.  Install it with:  pip install .[gemini]"
        ) from exc

    return ChatGoogleGenerativeAI(
        model=llm_settings.model,
        temperature=llm_settings.temperature,
        max_output_tokens=llm_settings.max_tokens,
    )


def _create_vertexai(
    llm_settings: LLMSettings,
    gcp_settings: GCPSettings | None = None,
) -> BaseChatModel:
    """Instantiate a Gemini model via Google Cloud Vertex AI.

    Uses Application Default Credentials (ADC) — no API key required.
    The GCP project is sourced from ``gcp_settings.project_id`` (preferred)
    or the ``VERTEXAI_PROJECT`` env var as fallback.

    Uses ``ChatGoogleGenerativeAI`` from ``langchain-google-genai`` with ADC
    credentials (recommended over the deprecated ``ChatVertexAI``).

    Args:
        llm_settings: LLM configuration.
        gcp_settings: GCP configuration — used for project ID validation.

    Returns:
        ``ChatGoogleGenerativeAI`` instance backed by Vertex AI ADC.
    """
    import os

    # Prefer settings object; fall back to raw env var
    project = (
        (gcp_settings.project_id if gcp_settings else None)
        or os.environ.get("VERTEXAI_PROJECT", "")
    )

    if not project:
        raise ValueError(
            "GCP project ID must be set (GCP_PROJECT_ID in .env or VERTEXAI_PROJECT) "
            "when using the 'vertexai' LLM provider."
        )

    try:
        import google.auth
        from langchain_google_genai import ChatGoogleGenerativeAI  # lazy import
    except ImportError as exc:
        raise ImportError(
            "The 'vertexai' LLM provider requires the langchain-google-genai and "
            "google-auth packages.  Install with:  uv sync --extra gemini"
        ) from exc

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )

    return ChatGoogleGenerativeAI(
        model=llm_settings.model,
        credentials=creds,
        temperature=llm_settings.temperature,
        max_output_tokens=llm_settings.max_tokens,
    )
