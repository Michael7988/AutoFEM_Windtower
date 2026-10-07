"""Desktop-runtime API without coupling the modeling pipeline to Electron."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr

from ..config import (
    ABAQUSWORK,
    BACKUP_ROOT,
    CACHE_ROOT,
    CONFIG_ROOT,
    DATA_ROOT,
    INPUT_BATCH_ROOT,
    LOG_ROOT,
    STORAGE_ROOT,
    Settings,
    load_desktop_settings,
    save_desktop_settings,
)
from ..parameter_ir import PARAMETER_SCHEMA_VERSION


APP_VERSION = "1.0.0"
DESKTOP_PROTOCOL_VERSION = "windtower_desktop_v1"
ACTIVE_SOLVER_STATES = {"queued", "datacheck", "solving", "postprocessing"}


class DesktopSettingsUpdate(BaseModel):
    abaqus_command: str | None = Field(default=None, max_length=1024)
    qwen_model: str | None = Field(default=None, max_length=200)
    qwen_engineering_model: str | None = Field(default=None, max_length=200)
    qwen_fem_model: str | None = Field(default=None, max_length=200)
    qwen_base_url: str | None = Field(default=None, max_length=1000)


class DesktopLlmKeyUpdate(BaseModel):
    api_key: SecretStr


class DesktopImportRequest(BaseModel):
    source_path: str = Field(min_length=1, max_length=4096)


def create_desktop_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/desktop", tags=["desktop-runtime"])

    @router.get("/health")
    def desktop_health() -> dict[str, Any]:
        active = _solver_activity()
        return {
            "ok": True,
            "desktop_mode": settings.desktop_mode,
            "app_version": APP_VERSION,
            "protocol_version": DESKTOP_PROTOCOL_VERSION,
            "schema_version": PARAMETER_SCHEMA_VERSION,
            "data_directories": _public_directories(),
            "abaqus": _abaqus_status(settings.abaqus_cmd),
            "llm": {
                "provider": "qwen",
                "model": settings.qwen_model,
                "engineering_model": settings.qwen_engineering_model,
                "fem_model": settings.qwen_fem_model,
                "configured": bool(settings.qwen_api_key),
            },
            "solver": active,
        }

    @router.get("/version")
    def desktop_version() -> dict[str, Any]:
        return {
            "app": APP_VERSION,
            "desktop_protocol": DESKTOP_PROTOCOL_VERSION,
            "parameter_ir": PARAMETER_SCHEMA_VERSION,
            "generator": "wind_tower_s4r_adapter_v1",
            "validation": "three_layer_validation_v1",
        }

    @router.get("/settings")
    def desktop_settings() -> dict[str, Any]:
        stored = load_desktop_settings()
        return {
            "abaqus_command": settings.abaqus_cmd,
            "qwen_model": settings.qwen_model,
            "qwen_engineering_model": settings.qwen_engineering_model,
            "qwen_fem_model": settings.qwen_fem_model,
            "qwen_base_url": settings.qwen_base_url,
            "llm_key_configured": bool(settings.qwen_api_key),
            "stored_keys": sorted(key for key in stored if key in {
                "abaqus_command", "qwen_model", "qwen_engineering_model",
                "qwen_fem_model", "qwen_base_url",
            }),
            "restart_required": False,
        }

    @router.put("/settings")
    def update_desktop_settings(request: DesktopSettingsUpdate) -> dict[str, Any]:
        stored = load_desktop_settings()
        updates = request.model_dump(exclude_none=True)
        if "abaqus_command" in updates:
            updates["abaqus_command"] = str(updates["abaqus_command"]).strip()
            if not updates["abaqus_command"]:
                raise HTTPException(status_code=422, detail="Abaqus命令不能为空。")
            settings.abaqus_cmd = updates["abaqus_command"]
        if "qwen_model" in updates:
            updates["qwen_model"] = str(updates["qwen_model"]).strip()
            settings.qwen_model = updates["qwen_model"]
            settings.qwen_engineering_model = updates["qwen_model"]
            settings.qwen_fem_model = updates["qwen_model"]
        if "qwen_engineering_model" in updates:
            updates["qwen_engineering_model"] = str(updates["qwen_engineering_model"]).strip()
            settings.qwen_engineering_model = updates["qwen_engineering_model"]
        if "qwen_fem_model" in updates:
            updates["qwen_fem_model"] = str(updates["qwen_fem_model"]).strip()
            settings.qwen_fem_model = updates["qwen_fem_model"]
        if "qwen_base_url" in updates:
            updates["qwen_base_url"] = str(updates["qwen_base_url"]).strip().rstrip("/")
            settings.qwen_base_url = updates["qwen_base_url"]
        stored.update(updates)
        save_desktop_settings(stored)
        return {
            "saved": True,
            "restart_required": False,
            "abaqus": _abaqus_status(settings.abaqus_cmd),
        }

    @router.post("/settings/llm-key")
    def set_runtime_llm_key(request: DesktopLlmKeyUpdate) -> dict[str, Any]:
        key = request.api_key.get_secret_value().strip()
        if not key:
            raise HTTPException(status_code=422, detail="API Key不能为空。")
        # Persistence is deliberately owned by Electron safeStorage/DPAPI.
        # The backend retains only the decrypted value for this process.
        settings.qwen_api_key = key
        return {"saved": False, "configured": True, "storage": "desktop_safe_storage"}

    @router.post("/data/import")
    def import_projects(request: DesktopImportRequest) -> dict[str, Any]:
        source = Path(request.source_path).expanduser().resolve()
        if not source.is_dir():
            raise HTTPException(status_code=404, detail="历史项目目录不存在。")
        candidates = sorted(path for path in source.iterdir() if path.is_dir() and (path / "ir.json").is_file())
        imported: list[dict[str, Any]] = []
        for candidate in candidates:
            destination = STORAGE_ROOT / candidate.name
            if destination.exists():
                suffix = hashlib.sha256(str(candidate).encode("utf-8")).hexdigest()[:8]
                destination = STORAGE_ROOT / f"{candidate.name}-imported-{suffix}"
                counter = 2
                while destination.exists():
                    destination = STORAGE_ROOT / f"{candidate.name}-imported-{suffix}-{counter}"
                    counter += 1
            shutil.copytree(candidate, destination)
            imported.append({
                "source": str(candidate),
                "project_id": destination.name,
                "destination": str(destination),
                "manifest_hash": _directory_hash(destination),
            })
        return {"imported_count": len(imported), "items": imported}

    @router.get("/shutdown-readiness")
    def shutdown_readiness() -> dict[str, Any]:
        active = _solver_activity()
        return {
            "safe_to_exit": active["active_count"] == 0,
            "active_count": active["active_count"],
            "active_submissions": active["items"],
            "message": (
                "当前没有正在执行的计算。"
                if active["active_count"] == 0
                else "仍有Abaqus任务正在运行，关闭窗口后应保持程序在托盘中。"
            ),
        }

    return router


def _public_directories() -> dict[str, str]:
    return {
        "data": str(DATA_ROOT or STORAGE_ROOT),
        "projects": str(STORAGE_ROOT),
        "input_batches": str(INPUT_BATCH_ROOT),
        "work": str(ABAQUSWORK),
        "logs": str(LOG_ROOT),
        "cache": str(CACHE_ROOT),
        "config": str(CONFIG_ROOT),
        "backups": str(BACKUP_ROOT),
    }


def _abaqus_status(command: str) -> dict[str, Any]:
    path = Path(command)
    resolved = str(path.resolve()) if path.exists() else shutil.which(command)
    return {
        "command": command,
        "available": bool(resolved),
        "resolved_path": resolved,
        "work_directory": str(ABAQUSWORK),
        "work_directory_writable": _is_writable_directory(ABAQUSWORK),
    }


def _is_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".desktop-write-test-{time.time_ns()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def _solver_activity() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for path in STORAGE_ROOT.glob("*/jobs/submissions/*/submission.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("status") in ACTIVE_SOLVER_STATES:
            items.append({
                "project_id": payload.get("project_id"),
                "submission_id": payload.get("submission_id"),
                "status": payload.get("status"),
                "stage": payload.get("stage"),
                "created_at": payload.get("created_at"),
            })
    items.sort(key=lambda item: float(item.get("created_at") or 0))
    return {"active_count": len(items), "items": items}


def _directory_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()
