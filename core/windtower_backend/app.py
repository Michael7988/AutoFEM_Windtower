from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import create_router
from .api.security import install_api_security
from .config import ensure_runtime_directories, load_settings
from .core.pipeline import WindTowerPipeline


def create_app() -> FastAPI:
    ensure_runtime_directories()
    settings = load_settings()
    pipeline = WindTowerPipeline(settings)
    api = FastAPI(
        title="LLM-Assisted Parametric FEM Modeling",
        version="1.0.0",
        description=(
            "Heterogeneous input mapping, a single structured parameter layer, "
            "deterministic Abaqus generation, and explicit validation."
        ),
    )
    api.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_api_security(api, settings)

    api.include_router(create_router(pipeline))
    return api


app = create_app()
