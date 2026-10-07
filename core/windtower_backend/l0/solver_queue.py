from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..config import ABAQUSWORK, METADATA_DB, STORAGE_ROOT, Settings
from ..capability_profile import L2_CAPABILITY_ID
from ..l2.postprocess import postprocess_l2
from ..modeling import ProjectState, get_state, write_json
from ..persistence import MetadataStore
from .implementation import build_l0, parse_abaqus_log, postprocess_l0
from .logs import (
    collect_stage_logs as _collect_stage_logs,
    important_lines as _important_lines,
    stage_has_fatal_error as _stage_has_fatal_error,
)


TERMINAL_TASK_STATES = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_SUBMISSION_STATES = {"completed", "partial", "failed", "cancelled", "interrupted"}
ACTIVE_SUBMISSION_STATES = {"queued", "datacheck", "solving", "postprocessing"}


class SolverSubmissionError(ValueError):
    """Base class for explicit solver-submission failures."""


class StaleSolverSubmission(SolverSubmissionError):
    """Raised when the client attempts to solve an obsolete parameter revision."""


class SolverQueueService:
    """Durable, explicit-only local Abaqus queue.

    Building or previewing a model never reaches this service.  A submission is
    created only by the solve endpoint, frozen to copied INPs and then executed
    as datacheck -> solve -> aggregate ODB post-processing.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._executor = ThreadPoolExecutor(
            max_workers=settings.abaqus_max_concurrent_jobs,
            thread_name_prefix="abaqus-solver",
        )
        self._locks: dict[str, threading.RLock] = {}
        self._cancel_events: dict[tuple[str, str], threading.Event] = {}
        self._processes: dict[tuple[str, str], subprocess.Popen[Any]] = {}
        self._worker_id = f"solver-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._lease_seconds = 90
        self.metadata = MetadataStore(settings.metadata_db or METADATA_DB)
        self._recover()

    def submit(
        self,
        state: ProjectState,
        *,
        job_type: str = "all",
        job_types: list[str] | None = None,
        base_revision: int | None = None,
        idempotency_key: str | None = None,
        run_postprocess: bool = True,
        build_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._preflight_environment(state)
        build = build_report or build_l0(state)
        if build.get("status") != "ready":
            raise SolverSubmissionError("Abaqus INP is not ready; generate and check the calculation files first.")

        current_revision = int((state.ir.get("parameter_set") or {}).get("revision") or 1)
        if base_revision is not None and base_revision != current_revision:
            raise StaleSolverSubmission(
                "The parameter revision changed after solve confirmation."
            )

        selected = self._selected_jobs(build, job_type, job_types)
        if not selected:
            raise SolverSubmissionError("No active static or modal analysis is available for solving.")

        normalized_key = str(idempotency_key or "").strip()
        existing = self._find_existing(
            state,
            normalized_key,
            current_revision,
            selected,
        )
        if existing:
            return existing

        submission_id = f"sub-{uuid.uuid4().hex[:12]}"
        root = self._submission_root(state, submission_id)
        root.mkdir(parents=True, exist_ok=False)
        tasks: dict[str, Any] = {}
        for name in selected:
            source = Path(build["jobs"][name]["path"])
            task_root = root / name
            task_root.mkdir(parents=True, exist_ok=True)
            frozen_inp = task_root / "input.inp"
            shutil.copy2(source, frozen_inp)
            tasks[name] = {
                "job_type": name,
                "status": "queued",
                "stage": "queued",
                "attempt": 1,
                "created_at": time.time(),
                "updated_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "elapsed_seconds": 0.0,
                "inp_path": str(frozen_inp.relative_to(root)).replace("\\", "/"),
                "inp_sha256": _sha256(frozen_inp),
                "job_id": None,
                "pid": None,
                "categories": [],
                "log_excerpt": [],
                "artifacts": {},
                "datacheck_reused": False,
            }

        now = time.time()
        payload = {
            "schema_version": "abaqus_submission_v2",
            "submission_id": submission_id,
            "project_id": state.project_id,
            "job_type": job_type,
            "job_types": selected,
            "run_postprocess": bool(run_postprocess),
            "status": "queued",
            "stage": "queued",
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "elapsed_seconds": 0.0,
            "parameter_revision": current_revision,
            "idempotency_key": normalized_key or None,
            "abaqus_version": self._abaqus_version(),
            "max_concurrent_jobs": self.settings.abaqus_max_concurrent_jobs,
            "job_cpus": self.settings.abaqus_job_cpus,
            "tasks": tasks,
            # Compatibility with the v1 UI contract.
            "jobs": {},
            "categories": [],
            "artifacts": {},
            "postprocess": None,
            "results": None,
            "cancel_requested": False,
            "postprocess_started": False,
        }
        self._write_status(state, payload)
        for name in selected:
            self._schedule_task(state.project_id, submission_id, name)
        return self.get(state, submission_id)

    def get(self, state: ProjectState, submission_id: str) -> dict[str, Any]:
        with self._lock(submission_id):
            payload = self.metadata.get_submission(submission_id, state.project_id)
            if payload is None:
                path = self._status_path(state, submission_id)
                if not path.exists():
                    legacy = state.root / "jobs" / "submissions" / f"{submission_id}.json"
                    if legacy.exists():
                        return json.loads(legacy.read_text(encoding="utf-8"))
                    raise FileNotFoundError(f"L0 submission not found: {submission_id}")
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.metadata.record_submission(payload)
        payload["queue_position"] = self._queue_position(payload)
        return payload

    def list(self, state: ProjectState, limit: int = 30) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        base = state.root / "jobs" / "submissions"
        if base.exists():
            for path in base.glob("*/submission.json"):
                try:
                    items.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
            for path in base.glob("sub-*.json"):
                try:
                    items.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
        items.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        active = [item for item in items if item.get("status") in ACTIVE_SUBMISSION_STATES]
        return {"project_id": state.project_id, "items": items[: max(1, limit)], "active": active}

    def cancel(self, state: ProjectState, submission_id: str) -> dict[str, Any]:
        with self._lock(submission_id):
            payload = self.get(state, submission_id)
            if payload.get("status") in TERMINAL_SUBMISSION_STATES:
                return payload
            payload["cancel_requested"] = True
            payload["updated_at"] = time.time()
            for name, task in payload.get("tasks", {}).items():
                event = self._cancel_events.setdefault((submission_id, name), threading.Event())
                event.set()
                if task.get("status") == "queued":
                    task.update(
                        status="cancelled",
                        stage="cancelled",
                        finished_at=time.time(),
                        updated_at=time.time(),
                    )
                process = self._processes.get((submission_id, name))
                if process and process.poll() is None:
                    self._terminate_abaqus(task.get("job_id"), process)
            self._write_status(state, payload)
        self._maybe_finalize(state.project_id, submission_id)
        return self.get(state, submission_id)

    def retry(self, state: ProjectState, submission_id: str) -> dict[str, Any]:
        with self._lock(submission_id):
            payload = self.get(state, submission_id)
            retry_names = [
                name
                for name, task in payload.get("tasks", {}).items()
                if task.get("status") in {"failed", "cancelled", "interrupted"}
            ]
            if not retry_names:
                raise SolverSubmissionError("This submission has no failed, cancelled or interrupted analysis to retry.")
            payload.update(
                status="queued",
                stage="queued",
                updated_at=time.time(),
                started_at=None,
                finished_at=None,
                elapsed_seconds=0.0,
                cancel_requested=False,
                postprocess_started=False,
            )
            for name in retry_names:
                task = payload["tasks"][name]
                task.update(
                    status="queued",
                    stage="queued",
                    attempt=int(task.get("attempt") or 1) + 1,
                    updated_at=time.time(),
                    started_at=None,
                    finished_at=None,
                    elapsed_seconds=0.0,
                    pid=None,
                    job_id=None,
                    categories=[],
                    log_excerpt=[],
                    artifacts={},
                    datacheck_reused=False,
                )
                self._cancel_events[(submission_id, name)] = threading.Event()
            self._write_status(state, payload)
        for name in retry_names:
            self._schedule_task(state.project_id, submission_id, name)
        return self.get(state, submission_id)

    def results(self, state: ProjectState, submission_id: str) -> dict[str, Any]:
        payload = self.get(state, submission_id)
        summary_path = self._submission_root(state, submission_id) / "results" / "results_summary.json"
        if summary_path.exists():
            result = json.loads(summary_path.read_text(encoding="utf-8"))
            result["submission_id"] = submission_id
            result["submission_status"] = payload.get("status")
            return result
        return {
            "status": "missing",
            "submission_id": submission_id,
            "submission_status": payload.get("status"),
            "message": "ODB results are not available for this submission.",
            "artifacts": {},
        }

    def _schedule_task(self, project_id: str, submission_id: str, job_type: str) -> None:
        self._cancel_events.setdefault((submission_id, job_type), threading.Event())
        self._executor.submit(self._run_task, project_id, submission_id, job_type)

    def _run_task(self, project_id: str, submission_id: str, job_type: str) -> None:
        state = get_state(project_id)
        if not self.metadata.claim_task(
            submission_id,
            job_type,
            self._worker_id,
            lease_seconds=self._lease_seconds,
        ):
            return
        event = self._cancel_events.setdefault((submission_id, job_type), threading.Event())
        try:
            payload = self.get(state, submission_id)
            task = payload["tasks"][job_type]
            if event.is_set() or task.get("status") == "cancelled":
                self._finish_cancelled_task(state, submission_id, job_type)
                return

            cached = self._find_cached_datacheck(state, payload, job_type)
            if cached:
                self._update_task(
                    state,
                    submission_id,
                    job_type,
                    {
                        "status": "datacheck",
                        "stage": "datacheck",
                        "datacheck_reused": True,
                        "datacheck_status": "pass",
                        "datacheck_cache": cached,
                        "started_at": time.time(),
                    },
                )
            else:
                datacheck = self._run_stage(state, submission_id, job_type, "datacheck", event)
                if datacheck["status"] != "pass":
                    self._update_task(
                        state,
                        submission_id,
                        job_type,
                        {
                            "status": "cancelled" if datacheck["status"] == "cancelled" else "failed",
                            "stage": datacheck["status"],
                            "finished_at": time.time(),
                            "categories": datacheck["categories"],
                            "log_excerpt": datacheck["log_excerpt"],
                            "artifacts": {"datacheck": datacheck["artifacts"]},
                        },
                    )
                    return
                self._update_task(
                    state,
                    submission_id,
                    job_type,
                    {
                        "datacheck_status": "pass",
                        "datacheck_categories": datacheck["categories"],
                        "artifacts": {"datacheck": datacheck["artifacts"]},
                    },
                )

            if event.is_set():
                self._finish_cancelled_task(state, submission_id, job_type)
                return
            solve = self._run_stage(state, submission_id, job_type, "solve", event)
            final_status = "completed" if solve["status"] == "pass" else ("cancelled" if solve["status"] == "cancelled" else "failed")
            self._update_task(
                state,
                submission_id,
                job_type,
                {
                    "status": final_status,
                    "stage": final_status,
                    "finished_at": time.time(),
                    "categories": solve["categories"],
                    "log_excerpt": solve["log_excerpt"],
                    "artifacts": {"solve": solve["artifacts"]},
                    "odb_path": solve.get("odb_path"),
                    "odb_sha256": solve.get("odb_sha256"),
                },
            )
        except Exception as exc:
            self._update_task(
                state,
                submission_id,
                job_type,
                {
                    "status": "failed",
                    "stage": "failed",
                    "finished_at": time.time(),
                    "categories": [{"category": "submission_error", "severity": "fatal", "message": str(exc)}],
                },
            )
        finally:
            self._processes.pop((submission_id, job_type), None)
            try:
                final_state = str(self.get(state, submission_id)["tasks"][job_type].get("status") or "failed")
            except Exception:
                final_state = "failed"
            self.metadata.release_task(
                submission_id,
                job_type,
                self._worker_id,
                final_state,
            )
            self._maybe_finalize(project_id, submission_id)

    def _run_stage(
        self,
        state: ProjectState,
        submission_id: str,
        job_type: str,
        stage: str,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        payload = self.get(state, submission_id)
        task = payload["tasks"][job_type]
        attempt = int(task.get("attempt") or 1)
        task_root = self._submission_root(state, submission_id) / job_type
        artifact_dir = task_root / stage / f"attempt-{attempt}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stage_dir = _short_work_dir(submission_id, job_type, stage, attempt)
        if stage_dir.exists():
            stage_dir = stage_dir.with_name(f"{stage_dir.name}_{int(time.time())}")
        stage_dir.mkdir(parents=True, exist_ok=False)
        local_inp = stage_dir / "input.inp"
        shutil.copy2(task_root / "input.inp", local_inp)
        job_id = _abaqus_job_name(state.project_id, submission_id, job_type, stage, attempt)
        command = [
            self.settings.abaqus_cmd,
            f"job={job_id}",
            f"input={local_inp.name}",
            f"cpus={self.settings.abaqus_job_cpus}",
        ]
        if stage == "datacheck":
            command.append("datacheck")
        command.append("interactive")
        started = time.time()
        last_heartbeat = started
        public_stage = "solving" if stage == "solve" else stage
        self._update_task(
            state,
            submission_id,
            job_type,
            {
                "status": public_stage,
                "stage": public_stage,
                "started_at": task.get("started_at") or started,
                "updated_at": started,
                "job_id": job_id,
                "command": " ".join(command),
            },
        )
        console_path = stage_dir / "command.log"
        with console_path.open("w", encoding="utf-8", errors="ignore") as console:
            process = subprocess.Popen(
                command,
                cwd=stage_dir,
                stdout=console,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._processes[(submission_id, job_type)] = process
            self._update_task(state, submission_id, job_type, {"pid": process.pid})
            timeout = self.settings.abaqus_job_timeout_seconds
            status = "pass"
            while process.poll() is None:
                if time.time() - last_heartbeat >= 10:
                    if not self.metadata.heartbeat_task(
                        submission_id,
                        job_type,
                        self._worker_id,
                        lease_seconds=self._lease_seconds,
                    ):
                        self._terminate_abaqus(job_id, process)
                        status = "failed"
                        break
                    last_heartbeat = time.time()
                if cancel_event.wait(0.5):
                    self._terminate_abaqus(job_id, process)
                    status = "cancelled"
                    break
                if time.time() - started > timeout:
                    self._terminate_abaqus(job_id, process)
                    status = "failed"
                    break
            returncode = process.poll()

        log_text = _collect_stage_logs(stage_dir, job_id, console_path)
        (stage_dir / "combined.log").write_text(log_text, encoding="utf-8", errors="ignore")
        categories = parse_abaqus_log(log_text, int(returncode or 0))
        if status == "cancelled":
            categories = [{"category": "cancelled", "severity": "warning", "message": "The engineer cancelled this Abaqus job."}]
        elif time.time() - started > self.settings.abaqus_job_timeout_seconds:
            categories = [{"category": "timeout", "severity": "fatal", "message": "The Abaqus job exceeded the configured time limit."}]
            status = "failed"
        elif _stage_has_fatal_error(log_text):
            status = "failed"
            if not any(item.get("severity") == "fatal" for item in categories):
                categories.append({"category": "abaqus_error", "severity": "fatal", "message": "Abaqus reported a fatal analysis error."})
        elif any(item.get("severity") == "fatal" for item in categories):
            status = "failed"

        odb_path = stage_dir / f"{job_id}.odb"
        sta_path = stage_dir / f"{job_id}.sta"
        if stage == "solve" and status == "pass":
            if not odb_path.exists():
                status = "failed"
                categories.append({"category": "odb_missing", "severity": "fatal", "message": "Abaqus finished without generating an ODB file."})
            elif not sta_path.exists() or "THE ANALYSIS HAS COMPLETED SUCCESSFULLY" not in sta_path.read_text(encoding="utf-8", errors="ignore").upper():
                status = "failed"
                categories.append({"category": "solve_incomplete", "severity": "fatal", "message": "Abaqus did not record a successful analysis completion in the STA file."})

        for path in stage_dir.iterdir():
            if path.is_file():
                shutil.copy2(path, artifact_dir / path.name)
        artifact_odb = artifact_dir / odb_path.name
        artifacts = {
            str(path.relative_to(artifact_dir)).replace("\\", "/"): str(path)
            for path in artifact_dir.iterdir()
            if path.is_file()
        }
        return {
            "status": status,
            "returncode": returncode,
            "categories": categories,
            "log_excerpt": _important_lines(log_text),
            "artifacts": artifacts,
            "odb_path": str(artifact_odb) if artifact_odb.exists() and status == "pass" else None,
            "odb_sha256": _sha256(artifact_odb) if artifact_odb.exists() and status == "pass" else None,
        }

    def _maybe_finalize(self, project_id: str, submission_id: str) -> None:
        state = get_state(project_id)
        with self._lock(submission_id):
            payload = self.get(state, submission_id)
            tasks = payload.get("tasks", {})
            if not tasks or any(task.get("status") not in TERMINAL_TASK_STATES for task in tasks.values()):
                payload["status"] = _aggregate_active_stage(tasks)
                payload["stage"] = payload["status"]
                self._write_status(state, payload)
                return
            if payload.get("postprocess_started"):
                return
            payload["postprocess_started"] = True
            completed = [task for task in tasks.values() if task.get("status") == "completed"]
            if completed and payload.get("run_postprocess"):
                payload.update(status="postprocessing", stage="postprocessing", updated_at=time.time())
                self._write_status(state, payload)
            else:
                self._finish_submission_without_postprocess(state, payload)
                return

        try:
            results_root = self._submission_root(state, submission_id) / "results"
            odb_paths = self._successful_odbs(payload)
            capability_id = str((state.ir.get("modeling_capability") or {}).get("capability_id") or "")
            post = (
                postprocess_l2(state, settings=self.settings, odb_paths=odb_paths, results_dir_override=results_root)
                if capability_id == L2_CAPABILITY_ID
                else postprocess_l0(
                    state,
                    self.settings,
                    odb_paths=odb_paths,
                    results_dir_override=results_root,
                    expected_job_types=set(payload.get("tasks") or {}),
                )
            )
            post["submission_id"] = submission_id
            post["parameter_revision"] = payload.get("parameter_revision")
            write_json(results_root / "results_summary.json", post)
            write_json(results_root / "result_summary.json", post)
            compatibility_results = state.root / "results"
            shutil.copytree(results_root, compatibility_results, dirs_exist_ok=True)
            with self._lock(submission_id):
                payload = self.get(state, submission_id)
                payload["postprocess"] = post
                payload["results"] = post
                summary_path = results_root / "results_summary.json"
                if summary_path.exists():
                    payload["artifacts"]["results_summary"] = str(summary_path.relative_to(state.root)).replace("\\", "/")
                completed_count = sum(task.get("status") == "completed" for task in payload["tasks"].values())
                failed_count = len(payload["tasks"]) - completed_count
                if post.get("status") == "blocked":
                    payload["status"] = "partial" if completed_count else "failed"
                else:
                    payload["status"] = "completed" if failed_count == 0 else "partial"
                payload["stage"] = payload["status"]
                payload["finished_at"] = time.time()
                self._write_status(state, payload)
        except Exception as exc:
            with self._lock(submission_id):
                payload = self.get(state, submission_id)
                completed_count = sum(task.get("status") == "completed" for task in payload["tasks"].values())
                payload["status"] = "partial" if completed_count else "failed"
                payload["stage"] = payload["status"]
                payload["finished_at"] = time.time()
                payload["categories"].append({"category": "postprocess_failed", "severity": "fatal", "message": str(exc)})
                self._write_status(state, payload)

    def _finish_submission_without_postprocess(self, state: ProjectState, payload: dict[str, Any]) -> None:
        tasks = payload.get("tasks", {})
        completed = sum(task.get("status") == "completed" for task in tasks.values())
        cancelled = sum(task.get("status") == "cancelled" for task in tasks.values())
        interrupted = sum(task.get("status") == "interrupted" for task in tasks.values())
        if completed == len(tasks):
            status = "completed"
        elif completed:
            status = "partial"
        elif cancelled == len(tasks):
            status = "cancelled"
        elif interrupted == len(tasks):
            status = "interrupted"
        else:
            status = "failed"
        payload.update(status=status, stage=status, finished_at=time.time(), updated_at=time.time())
        self._write_status(state, payload)

    def _successful_odbs(self, payload: dict[str, Any]) -> dict[str, Path | None]:
        result: dict[str, Path | None] = {
            "static_strength": None,
            "modal": None,
            "buckling_screening": None,
        }
        for name, task in payload.get("tasks", {}).items():
            odb_path = task.get("odb_path")
            if task.get("status") != "completed" or not odb_path or not Path(odb_path).exists():
                continue
            result[name] = Path(odb_path)
        return result

    def _update_task(
        self,
        state: ProjectState,
        submission_id: str,
        job_type: str,
        changes: dict[str, Any],
    ) -> None:
        with self._lock(submission_id):
            payload = self.get(state, submission_id)
            task = payload["tasks"][job_type]
            task.update(changes)
            now = time.time()
            task["updated_at"] = now
            if task.get("started_at"):
                task["elapsed_seconds"] = max(0.0, now - float(task["started_at"]))
            payload["updated_at"] = now
            if payload.get("started_at") is None and task.get("started_at"):
                payload["started_at"] = task["started_at"]
            payload["status"] = _aggregate_active_stage(payload["tasks"])
            payload["stage"] = payload["status"]
            payload["jobs"][job_type] = deepcopy(task)
            self._write_status(state, payload)

    def _finish_cancelled_task(self, state: ProjectState, submission_id: str, job_type: str) -> None:
        self._update_task(
            state,
            submission_id,
            job_type,
            {"status": "cancelled", "stage": "cancelled", "finished_at": time.time()},
        )

    def _find_cached_datacheck(
        self,
        state: ProjectState,
        payload: dict[str, Any],
        job_type: str,
    ) -> dict[str, Any] | None:
        current = payload["tasks"][job_type]
        for item in self.list(state, limit=200)["items"]:
            if item.get("submission_id") == payload.get("submission_id"):
                continue
            task = (item.get("tasks") or {}).get(job_type) or {}
            if (
                item.get("abaqus_version") == payload.get("abaqus_version")
                and task.get("inp_sha256") == current.get("inp_sha256")
                and task.get("datacheck_status") == "pass"
            ):
                return {
                    "submission_id": item.get("submission_id"),
                    "inp_sha256": task.get("inp_sha256"),
                    "abaqus_version": item.get("abaqus_version"),
                }
        return None

    def _selected_jobs(
        self,
        build: dict[str, Any],
        job_type: str,
        job_types: list[str] | None,
    ) -> list[str]:
        available = [name for name in ("static_strength", "modal") if name in (build.get("jobs") or {})]
        requested = job_types or ([] if job_type == "all" else [job_type])
        if not requested:
            return available
        normalized = []
        for item in requested:
            value = "static_strength" if item in {"static", "static_strength"} else ("modal" if item in {"modal", "frequency"} else item)
            if value in available and value not in normalized:
                normalized.append(value)
        return normalized

    def _find_existing(
        self,
        state: ProjectState,
        idempotency_key: str,
        parameter_revision: int,
        selected: list[str],
    ) -> dict[str, Any] | None:
        for item in self.list(state, limit=200)["items"]:
            if idempotency_key and item.get("idempotency_key") == idempotency_key:
                return item
            if (
                item.get("status") in ACTIVE_SUBMISSION_STATES
                and item.get("parameter_revision") == parameter_revision
                and sorted(item.get("job_types") or []) == sorted(selected)
            ):
                return item
        return None

    def _preflight_environment(self, state: ProjectState) -> None:
        command_available = Path(self.settings.abaqus_cmd).exists() or shutil.which(self.settings.abaqus_cmd) is not None
        if not command_available:
            raise SolverSubmissionError(f"Abaqus command not found: {self.settings.abaqus_cmd}")
        free_gb = shutil.disk_usage(state.root).free / (1024 ** 3)
        if free_gb < self.settings.abaqus_min_free_gb:
            raise SolverSubmissionError(
                f"Insufficient disk space for Abaqus: {free_gb:.2f} GB free; "
                f"{self.settings.abaqus_min_free_gb:.2f} GB required."
            )

    def _abaqus_version(self) -> str:
        try:
            result = subprocess.run(
                [self.settings.abaqus_cmd, "information=release"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=30,
            )
            match = next((line.strip() for line in (result.stdout or "").splitlines() if line.strip().startswith("Abaqus 20")), None)
            return match or "Abaqus (version unavailable)"
        except Exception:
            return "Abaqus (version unavailable)"

    def _terminate_abaqus(self, job_id: str | None, process: subprocess.Popen[Any]) -> None:
        if job_id:
            try:
                subprocess.run(
                    [self.settings.abaqus_cmd, "terminate", f"job={job_id}"],
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                pass
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _recover(self) -> None:
        for status_path in STORAGE_ROOT.glob("*/jobs/submissions/*/submission.json"):
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                project_id = str(payload.get("project_id") or "")
                submission_id = str(payload.get("submission_id") or "")
                if not project_id or not submission_id:
                    continue
                state = get_state(project_id)
                changed = False
                for task in (payload.get("tasks") or {}).values():
                    if task.get("status") in {"datacheck", "solving", "postprocessing"}:
                        task.update(
                            status="interrupted",
                            stage="interrupted",
                            finished_at=time.time(),
                            updated_at=time.time(),
                        )
                        changed = True
                    elif task.get("status") == "completed" and not _completed_task_is_valid(task):
                        task.update(
                            status="failed",
                            stage="failed",
                            finished_at=time.time(),
                            updated_at=time.time(),
                        )
                        task.setdefault("categories", []).append({
                            "category": "solve_incomplete",
                            "severity": "fatal",
                            "message": "The stored task did not contain a successful STA completion and usable ODB.",
                        })
                        changed = True
                if payload.get("status") in {"datacheck", "solving", "postprocessing"}:
                    payload.update(status="interrupted", stage="interrupted", finished_at=time.time())
                    changed = True
                if changed and all(task.get("status") in TERMINAL_TASK_STATES for task in (payload.get("tasks") or {}).values()):
                    completed = sum(task.get("status") == "completed" for task in payload["tasks"].values())
                    payload["status"] = "partial" if completed else "failed"
                    payload["stage"] = payload["status"]
                    payload["postprocess_started"] = False
                if changed:
                    self._write_status(state, payload)
                else:
                    self.metadata.record_submission(payload)
                for name, task in (payload.get("tasks") or {}).items():
                    if task.get("status") == "queued":
                        self._schedule_task(project_id, submission_id, name)
            except Exception:
                continue

    def _queue_position(self, payload: dict[str, Any]) -> int | None:
        if payload.get("status") != "queued":
            return None
        queued: list[tuple[float, str]] = []
        for path in STORAGE_ROOT.glob("*/jobs/submissions/*/submission.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if item.get("status") == "queued":
                queued.append((float(item.get("created_at") or 0), str(item.get("submission_id") or "")))
        queued.sort()
        for index, (_, submission_id) in enumerate(queued, start=1):
            if submission_id == payload.get("submission_id"):
                return index
        return None

    def _submission_root(self, state: ProjectState, submission_id: str) -> Path:
        return state.root / "jobs" / "submissions" / submission_id

    def _status_path(self, state: ProjectState, submission_id: str) -> Path:
        return self._submission_root(state, submission_id) / "submission.json"

    def _write_status(self, state: ProjectState, payload: dict[str, Any]) -> None:
        now = time.time()
        payload["updated_at"] = now
        if payload.get("started_at"):
            end = payload.get("finished_at") or now
            payload["elapsed_seconds"] = max(0.0, float(end) - float(payload["started_at"]))
        write_json(self._status_path(state, payload["submission_id"]), payload)
        self.metadata.record_submission(payload)

    def _lock(self, submission_id: str) -> threading.RLock:
        return self._locks.setdefault(submission_id, threading.RLock())


def _aggregate_active_stage(tasks: dict[str, Any]) -> str:
    states = {str(task.get("status") or "queued") for task in tasks.values()}
    if states and states.issubset(TERMINAL_TASK_STATES):
        return "postprocessing"
    if "solving" in states:
        return "solving"
    if "datacheck" in states:
        return "datacheck"
    return "queued"


def _abaqus_job_name(
    project_id: str,
    submission_id: str,
    job_type: str,
    stage: str,
    attempt: int,
) -> str:
    job_code = "st" if job_type == "static_strength" else ("mo" if job_type == "modal" else "ex")
    stage_code = "dc" if stage == "datacheck" else "sv"
    raw = f"wt_{project_id[-8:]}_{submission_id[-8:]}_{job_code}_{stage_code}_a{attempt}"
    safe = "".join(char if char.isalnum() or char == "_" else "_" for char in raw)
    return safe[:48]


def _short_work_dir(submission_id: str, job_type: str, stage: str, attempt: int) -> Path:
    job_code = "st" if job_type == "static_strength" else ("mo" if job_type == "modal" else "ex")
    stage_code = "dc" if stage == "datacheck" else "sv"
    return ABAQUSWORK / "solver_jobs" / submission_id[-8:] / job_code / stage_code / f"a{attempt}"


def _completed_task_is_valid(task: dict[str, Any]) -> bool:
    odb_path = Path(str(task.get("odb_path") or ""))
    if not odb_path.exists() or odb_path.stat().st_size <= 0:
        return False
    solve_artifacts = (task.get("artifacts") or {}).get("solve") or {}
    sta_paths = [
        Path(str(path))
        for name, path in solve_artifacts.items()
        if str(name).lower().endswith(".sta")
    ]
    return any(
        path.exists()
        and "THE ANALYSIS HAS COMPLETED SUCCESSFULLY" in path.read_text(encoding="utf-8", errors="ignore").upper()
        for path in sta_paths
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
