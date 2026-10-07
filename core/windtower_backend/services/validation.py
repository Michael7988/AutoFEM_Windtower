from __future__ import annotations

from typing import Any

from ..config import Settings
from ..capability_profile import L2_CAPABILITY_ID
from ..l0 import (
    datacheck_l0,
    postprocess_l0,
    results_l0,
)
from ..l0.solver_queue import SolverQueueService
from ..modeling import get_state
from ..parameter_ir import ParameterSet
from ..l2 import postprocess_l2, results_l2
from .model_generation import ModelGenerationService


class ValidationService:
    def __init__(self, settings: Settings, models: ModelGenerationService) -> None:
        self.settings = settings
        self.models = models
        self.solver_queue = SolverQueueService(settings)

    def datacheck(self, project_id: str, job_type: str = "all") -> dict[str, Any]:
        prepared = self.models.build(project_id)
        if prepared.get("status") == "blocked":
            return prepared
        return datacheck_l0(
            get_state(project_id), self.settings, job_type=job_type,
            build_report=prepared,
        )

    def submit(
        self,
        project_id: str,
        *,
        job_types: list[str],
        run_postprocess: bool,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        prepared = self.models.build(project_id)
        if prepared.get("status") == "blocked":
            return prepared
        return self.solver_queue.submit(
            get_state(project_id),
            job_types=job_types,
            run_postprocess=run_postprocess,
            idempotency_key=idempotency_key,
            build_report=prepared,
        )

    def submission(self, project_id: str, submission_id: str) -> dict[str, Any]:
        return self.solver_queue.get(get_state(project_id), submission_id)

    def postprocess(self, project_id: str) -> dict[str, Any]:
        state = get_state(project_id)
        parameter_set = ParameterSet.model_validate((state.ir.get("parameter_set") or {}))
        if parameter_set.capability_id == L2_CAPABILITY_ID:
            return postprocess_l2(state, settings=self.settings)
        return postprocess_l0(state, self.settings)

    def results(self, project_id: str) -> dict[str, Any]:
        state = get_state(project_id)
        parameter_set = ParameterSet.model_validate((state.ir.get("parameter_set") or {}))
        if parameter_set.capability_id == L2_CAPABILITY_ID:
            return results_l2(state)
        return results_l0(state)
