from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from windtower_backend.config import load_settings
from windtower_backend.l0.solver_queue import SolverQueueService, StaleSolverSubmission
from windtower_backend.modeling import PROJECTS, ProjectState


def _state(tmp_path: Path, project_id: str = "wt-solver-queue") -> ProjectState:
    state = ProjectState(
        project_id=project_id,
        root=tmp_path / project_id,
        excel_path=tmp_path / "source.xlsx",
        ir={"parameter_set": {"revision": 1}},
        artifacts={},
    )
    state.root.mkdir(parents=True)
    PROJECTS[project_id] = state
    return state


def _build(tmp_path: Path, job_types: tuple[str, ...] = ("static_strength", "modal")) -> dict:
    jobs = {}
    for name in job_types:
        path = tmp_path / f"{name}.inp"
        path.write_text("*Heading\n", encoding="utf-8")
        jobs[name] = {"path": str(path), "status": "ready"}
    return {
        "status": "ready",
        "jobs": jobs,
    }


def _wait(service: SolverQueueService, state: ProjectState, submission_id: str) -> dict:
    deadline = time.time() + 5
    status = service.get(state, submission_id)
    while status["status"] not in {"completed", "partial", "failed", "cancelled", "interrupted"} and time.time() < deadline:
        time.sleep(0.02)
        status = service.get(state, submission_id)
    return status


def _settings(max_workers: int = 2):
    return replace(
        load_settings(),
        abaqus_cmd=sys.executable,
        abaqus_max_concurrent_jobs=max_workers,
        abaqus_min_free_gb=0,
    )


def test_explicit_submission_runs_datacheck_before_solve_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)
    build = _build(tmp_path)
    monkeypatch.setattr("windtower_backend.l0.solver_queue.build_l0", lambda _state: build)
    monkeypatch.setattr(SolverQueueService, "_abaqus_version", lambda self: "Abaqus test")
    calls: list[tuple[str, str]] = []

    def fake_stage(self, _state, _submission_id, job_type, stage, _event):
        calls.append((job_type, stage))
        return {"status": "pass", "categories": [], "log_excerpt": [], "artifacts": {}}

    monkeypatch.setattr(SolverQueueService, "_run_stage", fake_stage)
    service = SolverQueueService(_settings())
    first = service.submit(state, idempotency_key="same-request", run_postprocess=False)
    second = service.submit(state, idempotency_key="same-request", run_postprocess=False)
    status = _wait(service, state, first["submission_id"])

    assert second["submission_id"] == first["submission_id"]
    assert status["status"] == "completed"
    for job_type in ("static_strength", "modal"):
        assert calls.index((job_type, "datacheck")) < calls.index((job_type, "solve"))


def test_datacheck_failure_blocks_only_that_analysis(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, "wt-solver-partial")
    build = _build(tmp_path)
    monkeypatch.setattr("windtower_backend.l0.solver_queue.build_l0", lambda _state: build)
    monkeypatch.setattr(SolverQueueService, "_abaqus_version", lambda self: "Abaqus test")
    calls: list[tuple[str, str]] = []

    def fake_stage(self, _state, _submission_id, job_type, stage, _event):
        calls.append((job_type, stage))
        if job_type == "modal" and stage == "datacheck":
            return {
                "status": "failed",
                "categories": [{"category": "datacheck_failed", "severity": "fatal", "message": "invalid"}],
                "log_excerpt": [],
                "artifacts": {},
            }
        return {"status": "pass", "categories": [], "log_excerpt": [], "artifacts": {}}

    monkeypatch.setattr(SolverQueueService, "_run_stage", fake_stage)
    service = SolverQueueService(_settings())
    submitted = service.submit(state, run_postprocess=False)
    status = _wait(service, state, submitted["submission_id"])

    assert status["status"] == "partial"
    assert ("modal", "solve") not in calls
    assert status["tasks"]["static_strength"]["status"] == "completed"
    assert status["tasks"]["modal"]["status"] == "failed"


def test_two_analysis_jobs_can_run_concurrently(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, "wt-solver-parallel")
    build = _build(tmp_path)
    monkeypatch.setattr("windtower_backend.l0.solver_queue.build_l0", lambda _state: build)
    monkeypatch.setattr(SolverQueueService, "_abaqus_version", lambda self: "Abaqus test")
    lock = threading.Lock()
    active = 0
    maximum = 0

    def fake_stage(self, _state, _submission_id, _job_type, _stage, _event):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return {"status": "pass", "categories": [], "log_excerpt": [], "artifacts": {}}

    monkeypatch.setattr(SolverQueueService, "_run_stage", fake_stage)
    service = SolverQueueService(_settings(max_workers=2))
    submitted = service.submit(state, run_postprocess=False)
    status = _wait(service, state, submitted["submission_id"])

    assert status["status"] == "completed"
    assert maximum == 2


def test_stale_revision_is_rejected_before_queueing(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, "wt-solver-stale")
    monkeypatch.setattr("windtower_backend.l0.solver_queue.build_l0", lambda _state: _build(tmp_path, ("static_strength",)))
    monkeypatch.setattr(SolverQueueService, "_abaqus_version", lambda self: "Abaqus test")
    service = SolverQueueService(_settings())

    with pytest.raises(StaleSolverSubmission):
        service.submit(state, base_revision=0, run_postprocess=False)
