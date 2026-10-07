from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid
from typing import Any

from pydantic import BaseModel, Field

from ..capability_profile import CAPABILITY_ID, L2_CAPABILITY_ID, get_capability
from ..modeling import get_state, write_json
from ..modeling.repository import ArtifactRepository
from ..parameter_ir import (
    ParameterChange,
    ParameterChangePreview,
    ParameterSet,
    SourceReference,
    apply_changes,
    apply_parameter_set_to_ir,
    instruction_changes,
    parameter_set_from_ir,
    l2_parameter_set,
    validate_parameter_set,
)


_DEPENDENCY_IMPACT: dict[str, tuple[list[str], list[str]]] = {
    "geometry": (["Geometry", "Mesh", "INP", "Validation", "Solver", "Results"], []),
    "material": (["Material / Section", "INP", "Validation", "Solver", "Results"], ["Geometry", "Mesh"]),
    "mesh": (["Mesh", "INP", "Validation", "Solver", "Results"], ["Geometry", "Material"]),
    "loads": (["INP", "Validation", "Solver", "Results"], ["Geometry", "Mesh", "Material"]),
    "boundary": (["INP", "Validation", "Solver", "Results"], ["Geometry", "Mesh", "Material"]),
    "analysis": (["INP", "Validation", "Solver", "Results"], ["Geometry", "Mesh", "Material"]),
    "interaction": (["INP", "Validation", "Solver", "Results"], ["Geometry", "Material"]),
}


class PreviewRequest(BaseModel):
    base_revision: int
    instruction: str | None = None
    changes: list[ParameterChange] = Field(default_factory=list)
    confirm_keys: list[str] = Field(default_factory=list)


class ConfirmPreviewRequest(BaseModel):
    preview_id: str
    base_revision: int
    operator: str = "engineer"


class ParameterService:
    def __init__(self) -> None:
        self._previews: dict[str, tuple[ParameterChangePreview, ParameterSet]] = {}
        self._artifact_repository = ArtifactRepository()

    def get(self, project_id: str) -> dict[str, Any]:
        state = get_state(project_id)
        parameter_set = self._load(state.root, state.ir)
        self._ensure_revision_snapshot(state.root, parameter_set, state.ir.get("parameter_evidence_map"))
        return {
            "parameter_set": parameter_set.model_dump(mode="json"),
            "validation": validate_parameter_set(parameter_set),
        }

    def preview(self, project_id: str, request: PreviewRequest) -> dict[str, Any]:
        state = get_state(project_id)
        current = self._load(state.root, state.ir)
        if request.base_revision != current.revision:
            raise ValueError(
                f"参数版本已更新：请求 revision={request.base_revision}，当前 revision={current.revision}。"
            )
        changes = list(request.changes)
        seen = {change.key for change in changes}
        for key in request.confirm_keys:
            if key in seen:
                continue
            record = current.parameters.get(key)
            if record is None:
                raise ValueError(f"未知参数：{key}")
            if not record.requires_confirmation and record.confirmation_status != "pending":
                raise ValueError(f"参数无需确认：{key}")
            changes.append(ParameterChange(key=key, value=record.value, unit=record.unit))
            seen.add(key)
        if request.instruction:
            changes.extend(instruction_changes(request.instruction, current))
        if not changes:
            raise ValueError("没有识别到允许的参数修改。")
        candidate, diff, validation = apply_changes(current, changes, increment_revision=False)
        preview_id = f"preview-{uuid.uuid4().hex[:12]}"
        preview = ParameterChangePreview(
            preview_id=preview_id,
            project_id=project_id,
            base_revision=current.revision,
            diff=diff,
            validation=validation,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._previews[preview_id] = (preview, candidate)
        return preview.model_dump(mode="json")

    def confirm(self, project_id: str, request: ConfirmPreviewRequest) -> dict[str, Any]:
        state = get_state(project_id)
        current = self._load(state.root, state.ir)
        stored = self._previews.get(request.preview_id)
        if stored is None or stored[0].project_id != project_id:
            raise KeyError("参数修改预览不存在或已失效。")
        preview, candidate = stored
        if request.base_revision != current.revision or preview.base_revision != current.revision:
            raise ValueError("参数版本已更新，请重新预览修改。")
        if preview.validation.get("status") != "pass":
            raise ValueError("参数修改未通过参数门禁，不能确认。")
        confirmed = candidate.model_copy(deep=True)
        confirmed.revision = current.revision + 1
        confirmed.confirmed_at = datetime.now(timezone.utc).isoformat()
        confirmed.confirmed_by = request.operator
        for item in preview.diff:
            record = confirmed.parameters.get(str(item.get("key") or ""))
            if record is not None:
                record.sources = [
                    SourceReference(
                        filename="工程师参数审查",
                        locator=f"Revision R{confirmed.revision}",
                        extraction_method="engineer_review",
                        raw_value=item.get("new_value"),
                        raw_unit=item.get("unit"),
                        mapping_confidence=1.0,
                        confidence=1.0,
                        source_type="engineer",
                        source_category="engineer_change",
                        locator_status="exact",
                        consistency_status="resolved",
                        validity_status="valid",
                        human_confirmation="confirmed",
                        downstream_validation="not_run",
                        formal_use_allowed=True,
                    )
                ]
        self._ensure_revision_snapshot(state.root, current, state.ir.get("parameter_evidence_map"))
        state.ir = apply_parameter_set_to_ir(confirmed, state.ir)
        trace_map = state.ir.setdefault("parameter_evidence_map", {})
        for item in preview.diff:
            key = str(item.get("key") or "")
            trace_map[key] = {
                "raw_value": item.get("new_value"),
                "raw_unit": item.get("unit"),
                "normalized_value": item.get("new_value"),
                "normalized_unit": item.get("unit"),
                "conversion": None,
                "extraction_method": "engineer_review",
                "mapping_confidence": 1.0,
                "confidence": 1.0,
                "source_kind": "engineer_input",
                "confirmation_status": "confirmed",
                "requires_confirmation": False,
                "formal_use_allowed": True,
                "evidence_ids": [],
                "evidence": [],
            }
        self._save(state.root, confirmed)
        self._write_revision_snapshot(
            state.root,
            confirmed,
            summary=self._revision_summary(preview.diff),
            diff=preview.diff,
            trace_map=trace_map,
        )
        write_json(state.root / "ir.json", state.ir)
        state.artifacts["parameter_set.json"] = state.root / "parameter_set.json"
        state.artifacts["ir.json"] = state.root / "ir.json"
        state.artifacts = _without_generated_artifacts(state.artifacts, state.root)
        self._artifact_repository.replace(
            project_id,
            state.root,
            state.artifacts,
        )
        del self._previews[request.preview_id]
        return {
            "status": "confirmed",
            "parameter_set": confirmed.model_dump(mode="json"),
            "validation": validate_parameter_set(confirmed),
            "requires_rebuild": True,
        }

    def trace(self, project_id: str, key: str) -> dict[str, Any]:
        state = get_state(project_id)
        parameter_set = self._load(state.root, state.ir)
        record = parameter_set.parameters.get(key)
        if record is None:
            raise KeyError(f"Parameter not found: {key}")
        evidence_map = state.ir.get("parameter_evidence_map") or {}
        mapped_value = evidence_map.get(key) if isinstance(evidence_map, dict) else None
        mapped = mapped_value if isinstance(mapped_value, dict) else {}
        evidence = list(mapped.get("evidence") or [])
        sources: list[dict[str, Any]] = []
        for item in evidence:
            location = item.get("location") or {}
            sources.append(
                {
                    "filename": item.get("filename"),
                    "locator": _format_location(location),
                    "location": location,
                    "excerpt": item.get("text"),
                    "extraction_method": item.get("extraction_method") or mapped.get("extraction_method"),
                    "mapping_confidence": item.get(
                        "mapping_confidence",
                        item.get("confidence", mapped.get("mapping_confidence", mapped.get("confidence"))),
                    ),
                }
            )
        if not sources:
            sources = [source.model_dump(mode="json") for source in record.sources]
        mapping_confidence = mapped.get("mapping_confidence", mapped.get("confidence"))
        return {
            "schema_version": "parameter_trace_v1",
            "project_id": project_id,
            "revision": parameter_set.revision,
            "key": key,
            "label": record.label,
            "status": record.status,
            "confirmation_status": record.confirmation_status,
            "confirmed_at": parameter_set.confirmed_at,
            "confirmed_by": parameter_set.confirmed_by,
            "raw_value": mapped.get("raw_value", sources[0].get("raw_value") if sources else None),
            "raw_unit": mapped.get("raw_unit", sources[0].get("raw_unit") if sources else None),
            "normalized_value": record.value,
            "normalized_unit": record.unit,
            "conversion": mapped.get("conversion", sources[0].get("conversion") if sources else None),
            "acquisition_method": mapped.get("extraction_method")
            or (sources[0].get("extraction_method") if sources else None),
            "mapping_confidence": mapping_confidence,
            "confidence": mapping_confidence,
            "assurance": {
                "source_category": mapped.get("source_kind"),
                "locator_status": "exact" if any(source.get("locator") for source in sources) else "missing",
                "consistency_status": mapped.get("consistency_status", "not_checked"),
                "validity_status": mapped.get("validity_status", "not_checked"),
                "human_confirmation": mapped.get("confirmation_status", record.confirmation_status),
                "downstream_validation": mapped.get("downstream_validation", "not_run"),
                "formal_use_allowed": bool(mapped.get("formal_use_allowed", record.confirmation_status == "confirmed")),
            },
            "sources": sources,
            "consumed_by": _runtime_consumers(state.root, state.ir, key),
        }

    def revisions(self, project_id: str) -> dict[str, Any]:
        state = get_state(project_id)
        current = self._load(state.root, state.ir)
        self._ensure_revision_snapshot(state.root, current, state.ir.get("parameter_evidence_map"))
        manifest = self._revision_manifest(state.root)
        return {
            "schema_version": "parameter_revision_list_v1",
            "project_id": project_id,
            "items": manifest["items"],
        }

    def revision_diff(self, project_id: str, from_revision: int, to_revision: int) -> dict[str, Any]:
        state = get_state(project_id)
        current = self._load(state.root, state.ir)
        self._ensure_revision_snapshot(state.root, current, state.ir.get("parameter_evidence_map"))
        before = self._load_revision(state.root, from_revision)
        after = self._load_revision(state.root, to_revision)
        diff: list[dict[str, Any]] = []
        groups: set[str] = set()
        for key in sorted(set(before.parameters) | set(after.parameters)):
            left = before.parameters.get(key)
            right = after.parameters.get(key)
            if left is not None and right is not None and left.value == right.value:
                continue
            record = right or left
            if record is None:
                continue
            groups.add(record.group)
            diff.append(
                {
                    "key": key,
                    "label": record.label,
                    "unit": record.unit,
                    "old_value": left.value if left else None,
                    "new_value": right.value if right else None,
                    "group": record.group,
                }
            )
        rebuild: set[str] = set()
        unchanged: set[str] | None = None
        for group in groups:
            impacted, stable = _DEPENDENCY_IMPACT.get(group, ([], []))
            rebuild.update(impacted)
            unchanged = set(stable) if unchanged is None else unchanged.intersection(stable)
        return {
            "schema_version": "parameter_revision_diff_v1",
            "project_id": project_id,
            "from_revision": from_revision,
            "to_revision": to_revision,
            "diff": diff,
            "rebuild_required": sorted(rebuild),
            "unchanged": sorted((unchanged or set()) - rebuild),
        }

    def _load(self, root: Path, ir: dict[str, Any]) -> ParameterSet:
        path = root / "parameter_set.json"
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            parameter_set = ParameterSet.model_validate(raw)
            if raw.get("schema_version") != parameter_set.schema_version:
                self._save(root, parameter_set)
            if (
                parameter_set.capability_id == CAPABILITY_ID
                and "loads.center_of_mass_offset_mm" not in parameter_set.parameters
            ):
                parameter_set = self._migrate_center_of_mass_offset(root, ir, parameter_set)
            if parameter_set.capability_id == L2_CAPABILITY_ID:
                generated_l2 = l2_parameter_set(parameter_set.project_id)
                missing_l2 = sorted(set(generated_l2.parameters) - set(parameter_set.parameters))
                if missing_l2:
                    for key in missing_l2:
                        parameter_set.parameters[key] = generated_l2.parameters[key]
                    parameter_set.revision += 1
                    parameter_set.confirmed_at = datetime.now(timezone.utc).isoformat()
                    parameter_set.confirmed_by = "capability_registry_migration"
                    next_ir = apply_parameter_set_to_ir(parameter_set, ir)
                    ir.clear()
                    ir.update(next_ir)
                    self._save(root, parameter_set)
                    write_json(root / "ir.json", ir)
                    self._write_revision_snapshot(
                        root, parameter_set,
                        summary="L2 能力注册表新增参数待工程师确认",
                        diff=[{"key": key, "old_value": None, "new_value": parameter_set.parameters[key].value} for key in missing_l2],
                        trace_map=ir.get("parameter_evidence_map"),
                    )
            expected_capability = get_capability(parameter_set.capability_id)
            expected_revision = int(expected_capability["capability_revision"])
            expected_hash = str(expected_capability["capability_hash_sha256"])
            if (
                parameter_set.capability_revision != expected_revision
                or parameter_set.capability_hash_sha256 != expected_hash
            ):
                parameter_set.capability_revision = expected_revision
                parameter_set.capability_hash_sha256 = expected_hash
                parameter_set.revision += 1
                parameter_set.confirmed_at = datetime.now(timezone.utc).isoformat()
                parameter_set.confirmed_by = "capability_registry_migration"
                next_ir = apply_parameter_set_to_ir(parameter_set, ir)
                ir.clear()
                ir.update(next_ir)
                self._save(root, parameter_set)
                write_json(root / "ir.json", ir)
                self._write_revision_snapshot(
                    root,
                    parameter_set,
                    summary=f"能力注册表升级至 revision {expected_revision}",
                    diff=[],
                    trace_map=ir.get("parameter_evidence_map"),
                )
            return parameter_set
        embedded = ir.get("parameter_set")
        if isinstance(embedded, dict):
            parameter_set = ParameterSet.model_validate(embedded)
        else:
            project_id = str((ir.get("project_meta_ir") or {}).get("project_id") or root.name)
            parameter_set = parameter_set_from_ir(project_id, ir)
        self._save(root, parameter_set)
        return parameter_set

    def _migrate_center_of_mass_offset(
        self,
        root: Path,
        ir: dict[str, Any],
        parameter_set: ParameterSet,
    ) -> ParameterSet:
        """One-time registry migration; proposed defaults remain unconfirmed."""
        previous = parameter_set.model_copy(deep=True)
        self._ensure_revision_snapshot(root, previous, ir.get("parameter_evidence_map"))
        backup_root = root / "migration_backups" / f"registry-v2-r{previous.revision:04d}"
        backup_root.mkdir(parents=True, exist_ok=True)
        for name in ("parameter_set.json", "ir.json"):
            source = root / name
            target = backup_root / name
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)

        generated = parameter_set_from_ir(parameter_set.project_id, ir)
        migrated = previous.model_copy(deep=True)
        migrated.parameters["loads.center_of_mass_offset_mm"] = generated.parameters[
            "loads.center_of_mass_offset_mm"
        ]
        migrated.capability_revision = generated.capability_revision
        migrated.capability_hash_sha256 = generated.capability_hash_sha256
        migrated.revision = previous.revision + 1
        migrated.confirmed_at = datetime.now(timezone.utc).isoformat()
        migrated.confirmed_by = "registry_migration"
        next_ir = apply_parameter_set_to_ir(migrated, ir)
        trace_map = next_ir.setdefault("parameter_evidence_map", {})
        record = migrated.parameters["loads.center_of_mass_offset_mm"]
        trace_map[record.key] = {
            "raw_value": None,
            "raw_unit": "mm",
            "normalized_value": record.value,
            "normalized_unit": "mm",
            "conversion": "missing -> proposed 0 mm",
            "extraction_method": "registry_migration",
            "mapping_confidence": 1.0,
            "source_kind": "capability_default",
            "confirmation_status": record.confirmation_status,
            "requires_confirmation": record.requires_confirmation,
            "formal_use_allowed": False,
            "evidence_ids": [],
            "evidence": [],
        }
        ir.clear()
        ir.update(next_ir)
        self._save(root, migrated)
        write_json(root / "ir.json", ir)
        artifacts = self._artifact_repository.load(migrated.project_id, root)
        artifacts["parameter_set.json"] = root / "parameter_set.json"
        artifacts["ir.json"] = root / "ir.json"
        self._artifact_repository.replace(
            migrated.project_id,
            root,
            _without_generated_artifacts(artifacts, root),
        )
        diff = [{
            "key": record.key,
            "label": record.label,
            "unit": record.unit,
            "old_value": None,
            "new_value": record.value,
            "group": record.group,
            "confirmation_status": record.confirmation_status,
        }]
        self._write_revision_snapshot(
            root,
            migrated,
            summary="注册表迁移：新增顶部质心偏心距候选",
            diff=diff,
            trace_map=trace_map,
        )
        _atomic_json(backup_root / "migration_manifest.json", {
            "schema_version": "parameter_registry_migration_v1",
            "from_revision": previous.revision,
            "to_revision": migrated.revision,
            "parameter_key": record.key,
            "confirmation_status": record.confirmation_status,
            "generated_artifacts_invalidated": True,
        })
        return migrated

    @staticmethod
    def _save(root: Path, parameter_set: ParameterSet) -> None:
        payload = parameter_set.model_dump(mode="json")
        temporary = root / ".parameter_set.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(root / "parameter_set.json")

    def _ensure_revision_snapshot(self, root: Path, parameter_set: ParameterSet, trace_map: Any = None) -> None:
        if not self._revision_path(root, parameter_set.revision).is_file():
            self._write_revision_snapshot(
                root,
                parameter_set,
                summary="项目参数基线",
                diff=[],
                trace_map=trace_map,
            )

    def _write_revision_snapshot(
        self,
        root: Path,
        parameter_set: ParameterSet,
        *,
        summary: str,
        diff: list[dict[str, Any]],
        trace_map: Any = None,
    ) -> None:
        revisions_root = root / "revisions"
        revisions_root.mkdir(parents=True, exist_ok=True)
        path = self._revision_path(root, parameter_set.revision)
        if not path.exists():
            _atomic_json(path, parameter_set.model_dump(mode="json"))
        trace_path = revisions_root / f"R{parameter_set.revision:04d}.trace.json"
        if isinstance(trace_map, dict) and not trace_path.exists():
            _atomic_json(
                trace_path,
                {"schema_version": "parameter_trace_snapshot_v1", "parameters": trace_map},
            )
        manifest = self._revision_manifest(root)
        item = {
            "revision": parameter_set.revision,
            "created_at": parameter_set.confirmed_at,
            "operator": parameter_set.confirmed_by or "engineer",
            "summary": summary,
            "change_count": len(diff),
        }
        manifest["items"] = [
            entry for entry in manifest["items"] if entry.get("revision") != parameter_set.revision
        ] + [item]
        manifest["items"].sort(key=lambda entry: int(entry.get("revision") or 0))
        _atomic_json(revisions_root / "manifest.json", manifest)

    @staticmethod
    def _revision_manifest(root: Path) -> dict[str, Any]:
        path = root / "revisions" / "manifest.json"
        if not path.is_file():
            return {"schema_version": "parameter_revision_manifest_v1", "items": []}
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("items", [])
        return payload

    @staticmethod
    def _revision_path(root: Path, revision: int) -> Path:
        return root / "revisions" / f"R{revision:04d}.json"

    def _load_revision(self, root: Path, revision: int) -> ParameterSet:
        path = self._revision_path(root, revision)
        if not path.is_file():
            raise KeyError(f"Revision not found: R{revision}")
        return ParameterSet.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _revision_summary(diff: list[dict[str, Any]]) -> str:
        labels = [str(item.get("label") or item.get("key")) for item in diff[:2]]
        suffix = f" 等 {len(diff)} 项" if len(diff) > 2 else ""
        return f"修改 {'、'.join(labels)}{suffix}" if labels else "参数确认"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _format_location(location: dict[str, Any]) -> str | None:
    parts = []
    if location.get("page") is not None:
        parts.append(f"Page {location['page']}")
    if location.get("sheet"):
        parts.append(f"Sheet {location['sheet']}")
    if location.get("cell_or_range"):
        parts.append(str(location["cell_or_range"]))
    if location.get("line") is not None:
        parts.append(f"Line {location['line']}")
    if location.get("slide") is not None:
        parts.append(f"Slide {location['slide']}")
    if location.get("json_path"):
        parts.append(str(location["json_path"]))
    return " · ".join(parts) or None


def _runtime_consumers(root: Path, ir: dict[str, Any], key: str) -> list[str]:
    report = ir.get("parameter_consumption")
    path = root / "reports" / "parameter_consumption.json"
    if path.is_file():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    entry = ((report or {}).get("parameters") or {}).get(key) or {}
    return [str(value) for value in entry.get("consumers") or []]


def _without_generated_artifacts(
    artifacts: dict[str, Path],
    root: Path,
) -> dict[str, Path]:
    generated_prefixes = (
        "mesh/",
        "inp/",
        "jobs/",
        "datacheck/",
        "results/",
        "reports/",
    )
    generated_names = {
        "build_report.json",
        "validation_report.json",
        "mesh_report.json",
    }
    retained: dict[str, Path] = {}
    for name, path in artifacts.items():
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix().lower()
        except ValueError:
            continue
        if relative.startswith(generated_prefixes) or relative in generated_names:
            continue
        retained[name] = path
    return retained
