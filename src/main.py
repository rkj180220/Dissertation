"""FastAPI application entry point for Cloud Orchestrator IDSS."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.dependencies import lifespan
from src.api.routes import router
from src.config.settings import get_settings

app = FastAPI(
    title="Cloud Orchestrator IDSS",
    description=(
        "Agentic AI-Driven Intelligent Decision Support System "
        "for Cloud-Agnostic Resource Orchestration and Automated Procurement"
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


def run() -> None:
    """Launch the API server."""
    settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.is_development,
    )


if __name__ == "__main__":
    run()
