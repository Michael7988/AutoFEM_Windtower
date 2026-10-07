from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from .config import MODEL_LIBRARY_ROOT
from .modeling import get_state
from .parameter_ir import ParameterSet, validate_parameter_set


VALIDATION_LEVELS = {"engineer_reviewed", "datacheck_passed", "solver_verified"}


class ModelLibraryService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or MODEL_LIBRARY_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def list_entries(self) -> dict[str, Any]:
        items = []
        for path in sorted(self.root.glob("*/manifest.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("status") == "published":
                items.append(_public_manifest(payload))
        return {
            "schema_version": "validated_model_library_v1",
            "items": sorted(items, key=lambda item: (item.get("label", ""), item["model_id"])),
        }

    def recommend(
        self,
        signature: dict[str, Any],
        *,
        structure_family: str = "wind_tower_shell_with_door",
        capability_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for item in self.list_entries()["items"]:
            score = 0.0
            reasons: list[str] = []
            if item.get("structure_family") == structure_family:
                score += 0.35
                reasons.append("结构族一致")
            if capability_id and item.get("capability_id") == capability_id:
                score += 0.25
                reasons.append("建模能力一致")
            candidate = item.get("signature") or {}
            if signature.get("door_enabled") is not None and candidate.get("door_enabled") == signature.get("door_enabled"):
                score += 0.15
                reasons.append("门洞状态一致")
            for key, weight, label in (
                ("height_mm", 0.1, "塔高相近"),
                ("base_diameter_mm", 0.075, "底部直径相近"),
                ("top_diameter_mm", 0.075, "顶部直径相近"),
            ):
                closeness = _numeric_closeness(signature.get(key), candidate.get(key))
                score += weight * closeness
                if closeness >= 0.8:
                    reasons.append(label)
            ranked.append({
                **item,
                "similarity_score": round(score, 4),
                "recommendation_reasons": reasons,
            })
        return sorted(
            ranked,
            key=lambda item: (-float(item["similarity_score"]), str(item.get("label") or "")),
        )[:max(1, limit)]

    def publish_project(
        self,
        project_id: str,
        *,
        label: str,
        operator: str,
    ) -> dict[str, Any]:
        state = get_state(project_id)
        parameter_path = state.root / "parameter_set.json"
        if not parameter_path.is_file():
            raise ValueError("项目尚未生成正式 ParameterSet。")
        parameter_set = ParameterSet.model_validate_json(
            parameter_path.read_text(encoding="utf-8")
        )
        validation = validate_parameter_set(parameter_set)
        if validation.get("status") != "pass":
            raise ValueError("正式参数集未通过参数门禁，不能发布模型。")
        inp_paths = sorted(state.root.glob("inp/**/*.inp"))
        if not inp_paths:
            raise ValueError("项目尚未生成 INP，不能发布模型。")
        reference = next(
            (path for path in inp_paths if "static" in path.parts),
            inp_paths[0],
        )
        parameter_bytes = parameter_path.read_bytes()
        reference_bytes = reference.read_bytes()
        version_hash = hashlib.sha256(parameter_bytes + reference_bytes).hexdigest()
        model_id = f"mdl-{version_hash[:16]}"
        target = self.root / model_id
        if (target / "manifest.json").is_file():
            return self.get_entry(model_id)["manifest"]
        target.mkdir(parents=True, exist_ok=False)
        shutil.copy2(parameter_path, target / "parameter_set.json")
        shutil.copy2(reference, target / "reference.inp")
        fem_profile = {
            key: record.model_dump(mode="json")
            for key, record in parameter_set.parameters.items()
            if record.domain == "fem"
        }
        (target / "fem_profile.json").write_text(
            json.dumps(fem_profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        validation_level = _validation_level(state.root)
        validation_summary = {
            "level": validation_level,
            "parameter_gate": validation,
            "has_datacheck_artifacts": validation_level in {"datacheck_passed", "solver_verified"},
            "has_solver_results": validation_level == "solver_verified",
            "result_summary": _result_summary(state.root),
        }
        (target / "validation_summary.json").write_text(
            json.dumps(validation_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest = {
            "schema_version": "validated_model_entry_v1",
            "model_id": model_id,
            "version": 1,
            "status": "published",
            "label": label.strip() or project_id,
            "source_project_id": project_id,
            "structure_family": parameter_set.structure_family,
            "capability_id": parameter_set.capability_id,
            "parameter_revision": parameter_set.revision,
            "validation_level": validation_level,
            "published_by": operator.strip() or "engineer",
            "files": {
                "parameter_set": "parameter_set.json",
                "reference_inp": "reference.inp",
                "fem_profile": "fem_profile.json",
                "validation_summary": "validation_summary.json",
            },
            "checksums": {
                "parameter_set": hashlib.sha256(parameter_bytes).hexdigest(),
                "reference_inp": hashlib.sha256(reference_bytes).hexdigest(),
            },
            "signature": _parameter_signature(parameter_set),
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return _public_manifest(manifest)

    def import_bundle(
        self,
        parameter_path: Path,
        inp_path: Path,
        *,
        label: str,
        operator: str,
        validation_level: str = "engineer_reviewed",
    ) -> dict[str, Any]:
        parameter_path = Path(parameter_path).resolve()
        inp_path = Path(inp_path).resolve()
        if not parameter_path.is_file():
            raise ValueError("ParameterSet 文件不存在。")
        if not inp_path.is_file() or inp_path.suffix.lower() != ".inp":
            raise ValueError("参考模型必须是存在的 INP 文件。")
        if validation_level not in VALIDATION_LEVELS:
            raise ValueError("验证等级无效。")
        parameter_set = ParameterSet.model_validate_json(
            parameter_path.read_text(encoding="utf-8")
        )
        validation = validate_parameter_set(parameter_set)
        if validation.get("status") != "pass":
            raise ValueError("正式参数集未通过参数门禁，不能导入模型库。")
        parameter_bytes = parameter_path.read_bytes()
        reference_bytes = inp_path.read_bytes()
        version_hash = hashlib.sha256(
            parameter_bytes + reference_bytes + validation_level.encode("utf-8")
        ).hexdigest()
        model_id = f"mdl-{version_hash[:16]}"
        target = self.root / model_id
        if (target / "manifest.json").is_file():
            return self.get_entry(model_id)["manifest"]
        target.mkdir(parents=True, exist_ok=False)
        shutil.copy2(parameter_path, target / "parameter_set.json")
        shutil.copy2(inp_path, target / "reference.inp")
        fem_profile = {
            key: record.model_dump(mode="json")
            for key, record in parameter_set.parameters.items()
            if record.domain == "fem"
        }
        (target / "fem_profile.json").write_text(
            json.dumps(fem_profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        validation_summary = {
            "level": validation_level,
            "parameter_gate": validation,
            "has_datacheck_artifacts": validation_level in {"datacheck_passed", "solver_verified"},
            "has_solver_results": validation_level == "solver_verified",
            "evidence_statement": "由工程师在一次性导入流程中确认。",
        }
        (target / "validation_summary.json").write_text(
            json.dumps(validation_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest = {
            "schema_version": "validated_model_entry_v1",
            "model_id": model_id,
            "version": 1,
            "status": "published",
            "label": label.strip() or inp_path.stem,
            "structure_family": parameter_set.structure_family,
            "capability_id": parameter_set.capability_id,
            "parameter_revision": parameter_set.revision,
            "validation_level": validation_level,
            "published_by": operator.strip() or "engineer",
            "files": {
                "parameter_set": "parameter_set.json",
                "reference_inp": "reference.inp",
                "fem_profile": "fem_profile.json",
                "validation_summary": "validation_summary.json",
            },
            "checksums": {
                "parameter_set": hashlib.sha256(parameter_bytes).hexdigest(),
                "reference_inp": hashlib.sha256(reference_bytes).hexdigest(),
            },
            "signature": _parameter_signature(parameter_set),
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return _public_manifest(manifest)

    def get_entry(self, model_id: str) -> dict[str, Any]:
        if not model_id.startswith("mdl-") or any(item in model_id for item in ("/", "\\", "..")):
            raise KeyError(model_id)
        root = (self.root / model_id).resolve()
        try:
            root.relative_to(self.root.resolve())
        except ValueError as exc:
            raise KeyError(model_id) from exc
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise KeyError(model_id)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "published":
            raise KeyError(model_id)
        parameter_set = ParameterSet.model_validate_json(
            (root / "parameter_set.json").read_text(encoding="utf-8")
        )
        fem_profile = json.loads((root / "fem_profile.json").read_text(encoding="utf-8"))
        return {
            "manifest": _public_manifest(manifest),
            "parameter_set": parameter_set,
            "fem_profile": fem_profile,
            "reference_path": root / "reference.inp",
        }


def _validation_level(root: Path) -> str:
    if any(root.rglob("*.odb")) and any(root.glob("results/*summary*.json")):
        return "solver_verified"
    if any(root.rglob("*.dat")) or any(root.rglob("datacheck*.log")):
        return "datacheck_passed"
    return "engineer_reviewed"


def _result_summary(root: Path) -> dict[str, Any]:
    allowed = {
        "mass", "total_mass", "max_displacement", "max_u_mag", "u_max",
        "max_mises", "mises_max", "reaction_balance", "force_balance",
        "first_frequency", "frequency_1", "frequencies",
    }
    result: dict[str, Any] = {}
    candidates = sorted((root / "results").glob("*.json")) if (root / "results").is_dir() else []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        _collect_result_metrics(payload, allowed, result)
    return result


def _collect_result_metrics(value: Any, allowed: set[str], result: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        return
    for key, nested in value.items():
        normalized = str(key).lower()
        if normalized in allowed and isinstance(nested, (int, float, str, list)):
            result.setdefault(normalized, nested[:20] if isinstance(nested, list) else nested)
        elif isinstance(nested, dict):
            _collect_result_metrics(nested, allowed, result)


def _parameter_signature(parameter_set: ParameterSet) -> dict[str, Any]:
    values = {key: record.value for key, record in parameter_set.parameters.items()}
    top_values = [
        float(value)
        for key, value in values.items()
        if key.startswith("geometry.tower_sections.") and key.endswith(".z_top_mm")
        and isinstance(value, (int, float))
    ]
    bottom_diameters = [
        float(value)
        for key, value in values.items()
        if key.startswith("geometry.tower_sections.") and key.endswith(".diameter_bottom_mm")
        and isinstance(value, (int, float))
    ]
    top_diameters = [
        float(value)
        for key, value in values.items()
        if key.startswith("geometry.tower_sections.") and key.endswith(".diameter_top_mm")
        and isinstance(value, (int, float))
    ]
    return {
        "height_mm": max(top_values) if top_values else None,
        "base_diameter_mm": bottom_diameters[0] if bottom_diameters else None,
        "top_diameter_mm": top_diameters[-1] if top_diameters else None,
        "door_enabled": values.get("geometry.door.enabled"),
        "analysis_tasks": parameter_set.analysis_tasks,
    }


def _public_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version", "model_id", "version", "status", "label",
        "structure_family", "capability_id", "parameter_revision",
        "validation_level", "published_by", "signature",
    }
    return {key: payload.get(key) for key in allowed if key in payload}


def _numeric_closeness(first: Any, second: Any) -> float:
    if not isinstance(first, (int, float)) or not isinstance(second, (int, float)):
        return 0.0
    scale = max(abs(float(first)), abs(float(second)), 1.0)
    return max(0.0, 1.0 - abs(float(first) - float(second)) / scale)
