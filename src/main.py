"""FastAPI application entry point for Cloud Orchestrator IDSS."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.logging_config import configure_logging
from config.settings import get_settings
from src.api.routes import router

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(
    title="Cloud Orchestrator IDSS",
    description=(
        "Agentic AI-Driven Intelligent Decision Support System "
        "for Cloud-Agnostic Resource Orchestration and Automated Procurement"
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "service": "cloud-orchestrator-idss"}


def run() -> None:
    """Launch the API server."""
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.is_development,
    )


if __name__ == "__main__":
    run()
