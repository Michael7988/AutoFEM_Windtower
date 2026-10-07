from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .models import InputBatch


INTERRUPTED_WARNING = "服务重启中断了解析任务，可以点击重试。"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def recover_interrupted_batch(batch: InputBatch) -> bool:
    """Turn persisted in-flight work into an explicit, retryable failure."""
    if batch.status not in {"queued", "processing"}:
        return False
    for item in batch.files:
        if item.status in {"queued", "extracting", "mapping"}:
            item.status = "failed"
            item.progress = 100
            item.error = item.error or "服务重启中断了文件解析；可安全重试。"
    batch.status = "failed"
    if INTERRUPTED_WARNING not in batch.warnings:
        batch.warnings.append(INTERRUPTED_WARNING)
    diagnostic = {
        "code": "PROCESS_INTERRUPTED",
        "severity": "warning",
        "retryable": True,
        "message": INTERRUPTED_WARNING,
    }
    if not any(item.get("code") == diagnostic["code"] for item in batch.diagnostics):
        batch.diagnostics.append(diagnostic)
    batch.updated_at = utc_now()
    return True


def ensure_fem_state(
    batch: InputBatch,
    specification_factory: Callable[[], dict[str, Any]],
    capability_match_factory: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> bool:
    """Migrate legacy preparation state without silently making it computable."""
    changed = False
    if not batch.fem_specification:
        batch.fem_specification = specification_factory()
        changed = True
        if batch.status == "confirmed":
            # A confirmed legacy project keeps its historic status, but the
            # migrated defaults are explicitly labelled instead of masquerading
            # as observed FEM evidence.
            batch.fem_specification["status"] = "confirmed"
            batch.fem_specification["requires_engineer_confirmation"] = False
            for item in batch.fem_specification["parameters"].values():
                item["confirmation_status"] = "confirmed"
                item["requires_confirmation"] = False
                item["migration_note"] = "legacy capability default"
            batch.fem_review = {
                "status": "confirmed",
                "confirmed_at": batch.updated_at,
                "operator": "legacy_migration",
            }
            batch.diagnostics.append({
                "code": "LEGACY_FEM_STATE_MIGRATED",
                "severity": "warning",
                "retryable": False,
                "message": "旧项目缺少 FEM 规范快照；已显式标记为能力默认值迁移。",
            })
    if not batch.capability_match:
        summary = (batch.reference_model or {}).get("summary")
        batch.capability_match = capability_match_factory(summary)
        changed = True
    return changed


def refresh_status(batch: InputBatch) -> None:
    pending = list(batch.readiness.get("pending_confirmation_keys") or [])
    batch.requires_confirmation = pending
    if batch.files and all(item.status == "failed" for item in batch.files):
        batch.status = "failed"
    elif not batch.selected_template_id:
        batch.status = "awaiting_template_confirmation"
    elif (batch.intent_contract or {}).get("confirmation_status") != "confirmed":
        batch.status = "awaiting_intent_confirmation"
    elif batch.readiness.get("technical_blockers"):
        batch.status = "blocked"
    elif any(item.status in {"queued", "extracting", "mapping"} for item in batch.files):
        batch.status = "processing"
    elif batch.llm_partial_failure and batch.readiness.get("preview_allowed"):
        batch.status = "completed_with_llm_warning"
    else:
        batch.status = "ready"
    batch.updated_at = utc_now()
