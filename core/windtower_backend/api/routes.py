"""GitHub v1 API composition."""

from fastapi import APIRouter

from ..core.pipeline import WindTowerPipeline
from ..input_batches import EngineeringInputBatchService
from .desktop import create_desktop_router
from .input_batches import create_input_batches_router
from .model_library import create_model_library_router
from .v1 import create_v1_router
from ..model_library import ModelLibraryService
from ..services import ModelGenerationService, ParameterService, ValidationService


def create_router(pipeline: WindTowerPipeline) -> APIRouter:
    router = APIRouter()
    parameters = ParameterService()
    models = ModelGenerationService(pipeline.settings, parameters)
    validation = ValidationService(pipeline.settings, models)
    model_library = ModelLibraryService()
    input_batches = EngineeringInputBatchService(pipeline, model_library)
    router.include_router(create_desktop_router(pipeline.settings))
    router.include_router(create_input_batches_router(input_batches), tags=["multi-file-input"])
    router.include_router(create_model_library_router(model_library), tags=["model-library"])
    router.include_router(
        create_v1_router(pipeline.settings, parameters, models, validation),
        tags=["windtower-v1"],
    )
    return router
