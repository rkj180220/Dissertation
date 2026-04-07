"""API route modules — aggregate all sub-routers into one."""

from fastapi import APIRouter

from src.api.routes.health import router as health_router
from src.api.routes.orchestration import router as orchestration_router

router = APIRouter()
router.include_router(health_router)
router.include_router(orchestration_router)
