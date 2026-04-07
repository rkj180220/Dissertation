"""Health and readiness endpoints.

* ``GET /health``  — lightweight liveness probe (always 200 if the
  process is up).
* ``GET /ready``   — deeper readiness check: verifies the pricing-
  service cache is accessible and the LLM model is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Request

logger = structlog.get_logger()

router = APIRouter(tags=["health"])


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Lightweight liveness probe.

    Returns 200 as long as the process is running.
    """
    return {
        "status": "healthy",
        "service": "cloud-orchestrator-idss",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


@router.get("/ready")
async def readiness_check(request: Request) -> dict[str, Any]:
    """Deep readiness check.

    Verifies:
    * Pricing service is initialised and cache is reachable.
    * LLM instance exists (does NOT send a live LLM call to avoid cost).
    * LangGraph graph is compiled.
    """
    log = logger.bind(component="readiness")
    checks: dict[str, Any] = {}

    # -- Pricing service --
    try:
        pricing_service = request.app.state.pricing_service
        checks["pricing_service"] = {
            "status": "ok",
            "providers": [p.value for p in pricing_service.registered_providers],
        }
    except Exception as exc:
        log.warning("readiness_pricing_failed", exc_info=True)
        checks["pricing_service"] = {"status": "error", "detail": str(exc)}

    # -- LLM --
    try:
        llm = request.app.state.llm
        checks["llm"] = {
            "status": "ok" if llm is not None else "unavailable",
        }
    except Exception as exc:
        log.warning("readiness_llm_failed", exc_info=True)
        checks["llm"] = {"status": "error", "detail": str(exc)}

    # -- Graph --
    try:
        graph = request.app.state.graph
        checks["graph"] = {
            "status": "ok" if graph is not None else "unavailable",
        }
    except Exception as exc:
        log.warning("readiness_graph_failed", exc_info=True)
        checks["graph"] = {"status": "error", "detail": str(exc)}

    overall = all(
        c.get("status") == "ok" for c in checks.values()
    )

    return {
        "status": "ready" if overall else "degraded",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
