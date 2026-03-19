"""Structured logging and LLM-tracing configuration.

Sets up **structlog** for structured logging and **LangFuse** for LLM
observability.  Call ``configure_observability`` once at application
startup (before any request is served).

Development  → coloured console renderer
Production   → JSON renderer (machine-parseable)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.config.settings import AppSettings, LangFuseSettings

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def configure_observability(settings: AppSettings) -> None:
    """Initialise all observability sub-systems.

    Must be called **once** at application startup before any log statement
    or LLM call is executed.

    Args:
        settings: Root application settings (carries sub-configs for
            log level, environment, and LangFuse).
    """
    _configure_structlog(
        log_level=settings.log_level,
        json_format=settings.is_production,
    )
    _configure_langfuse(settings.langfuse)

    log = structlog.get_logger().bind(component="observability")
    log.info(
        "observability_configured",
        env=settings.env.value,
        log_level=settings.log_level,
        langfuse_enabled=settings.langfuse.enabled,
        langfuse_configured=settings.langfuse.is_configured,
    )


# ---------------------------------------------------------------------------
# structlog
# ---------------------------------------------------------------------------


def _configure_structlog(log_level: str, *, json_format: bool = False) -> None:
    """Wire structlog processors, renderer, and stdlib bridge.

    Args:
        log_level: Minimum log level (DEBUG / INFO / WARNING / ERROR / CRITICAL).
        json_format: When *True* use ``JSONRenderer`` (production); otherwise
            use the coloured ``ConsoleRenderer`` (development).
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor
    if json_format:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Quieten chatty third-party loggers that pollute DEBUG output
    for noisy in ("httpx", "httpcore", "boto3", "botocore", "urllib3", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# LangFuse
# ---------------------------------------------------------------------------


def _configure_langfuse(langfuse_settings: LangFuseSettings) -> None:
    """Initialise the LangFuse SDK so ``@observe()`` decorators work.

    The SDK reads ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, and
    ``LANGFUSE_HOST`` from the OS environment.  Because *pydantic-settings*
    reads ``.env`` into Python objects (but does **not** export them), we
    explicitly push the validated values into ``os.environ`` so the SDK
    picks them up automatically.

    Args:
        langfuse_settings: Validated LangFuse configuration block.
    """
    log = structlog.get_logger().bind(component="langfuse_init")

    if not langfuse_settings.enabled:
        log.info("langfuse_disabled", reason="LANGFUSE_ENABLED=false")
        # Ensure the SDK won't accidentally initialise
        os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
        os.environ.pop("LANGFUSE_SECRET_KEY", None)
        return

    if not langfuse_settings.is_configured:
        log.warning(
            "langfuse_not_configured",
            reason="Missing LANGFUSE_PUBLIC_KEY and/or LANGFUSE_SECRET_KEY",
            hint="Add keys to .env or disable with LANGFUSE_ENABLED=false",
        )
        return

    # Push validated values into the OS environment for the SDK
    os.environ["LANGFUSE_PUBLIC_KEY"] = langfuse_settings.public_key
    os.environ["LANGFUSE_SECRET_KEY"] = langfuse_settings.secret_key
    os.environ["LANGFUSE_HOST"] = langfuse_settings.host

    try:
        from langfuse import Langfuse

        # Create a client to validate the connection eagerly.
        # The @observe() decorator will use its own singleton internally.
        _client = Langfuse()
        log.info("langfuse_initialized", host=langfuse_settings.host)
    except Exception:
        log.error("langfuse_init_failed", exc_info=True)
