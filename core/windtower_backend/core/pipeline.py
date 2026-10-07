from __future__ import annotations

from pathlib import Path
import uuid
from typing import Any

from ..config import STORAGE_ROOT, Settings
from ..importers.candidate_mapping import candidate_mapping_to_standard_input
from ..importers.standard_input import (
    normalize_import_to_standard_input,
    promote_standard_input,
    validate_standard_input,
)
from ..modeling import create_project, get_state, project_response, sanitize_filename, write_json
from ..parameter_ir import ParameterSet
from ..qwen_client import qwen_normalize_standard_input, qwen_review_standard_input
from ..services.parameters import ParameterService


class WindTowerPipeline:
    """Thin input-mapping facade retained for the multi-file batch service.

    Formal parameter management, deterministic generation and Abaqus
    validation live in responsibility-specific services under
    :mod:`windtower_backend.services`.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.parameters = ParameterService()

    def create_project_from_upload(
        self,
        filename: str,
        content: bytes,
        modeling_goal: str | None = None,
    ) -> dict[str, Any]:
        return create_project(filename, content, modeling_goal=modeling_goal)

    def normalize_input_from_upload(
        self,
        filename: str,
        content: bytes,
        enable_llm: bool = True,
        force_llm: bool = False,
        include_import: bool = False,
        llm_mode: str | None = None,
    ) -> dict[str, Any]:
        project_id = f"input-normalize-{uuid.uuid4().hex[:10]}"
        root = STORAGE_ROOT / project_id
        source_dir = root / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_path = source_dir / sanitize_filename(filename)
        source_path.write_bytes(content)

        mode = _resolve_llm_mode(
            llm_mode,
            enable_llm=enable_llm,
            force_llm=force_llm,
        )
        normalized = normalize_import_to_standard_input(
            source_path,
            enable_llm=mode != "off",
            force_llm=mode in {"forced_parse", "forced_review"},
        )
        normalized.setdefault("source_file_original", filename)
        normalized.setdefault("source_file_storage", source_path.name)
        profile = normalized.get("workbook_profile") or {}
        profile.setdefault("source_file_original", filename)
        profile.setdefault("source_file_storage", source_path.name)

        validation = normalized.get("validation_report") or {}
        llm_required = bool(
            validation.get("blocked")
            or normalized.get("input_format") == "unknown_llm_assisted"
        )
        should_call_llm = bool(
            mode in {"forced_parse", "forced_review"}
            or (mode == "adaptive" and llm_required)
        )
        if should_call_llm:
            llm_result = self._llm_mapping(
                mode,
                profile,
                validation,
                normalized,
                llm_required,
            )
            self._attach_llm_candidate(normalized, source_path, llm_result)
        else:
            normalized["llm_parse_assist"] = {
                "enabled": mode != "off",
                "triggered": False,
                "required": llm_required,
                "llm_mode": mode,
                "status": "not_called",
                "reason": "not_required" if not llm_required else "llm_disabled",
                "provider": "none",
                "model": "none",
                "candidate_available": False,
            }

        artifact_payloads = {
            "workbook_profile.json": profile,
            "standard_input_candidate.json": (
                normalized.get("standard_input_candidate")
                or normalized.get("standard_input_package")
            ),
            "standard_input_validation.json": normalized.get("validation_report"),
            "input_quality_scores.json": normalized.get("input_quality_scores"),
            "llm_mapping_audit.json": normalized.get("llm_parse_assist"),
            "llm_standard_input_candidate.json": normalized.get(
                "llm_standard_input_candidate"
            ),
            "llm_standard_input_validation.json": normalized.get(
                "llm_standard_input_validation"
            ),
            "candidate_mapping_ir.json": normalized.get("candidate_mapping_ir"),
            "llm_candidate_mapping_ir.json": normalized.get(
                "llm_candidate_mapping_ir"
            ),
        }
        artifact_paths: dict[str, str] = {}
        for name, payload in artifact_payloads.items():
            if payload is None:
                continue
            path = root / name
            write_json(path, payload)
            artifact_paths[name] = str(path)

        response = {
            "project_id": project_id,
            "status": validation.get("status", "warning"),
            "input_format": normalized.get("input_format"),
            "source_file_original": filename,
            "source_file_storage": source_path.name,
            "format_confidence": normalized.get("format_confidence"),
            "parse_strategy": normalized.get("parse_strategy"),
            "llm_required": normalized.get("llm_required", llm_required),
            "llm_status": (normalized.get("llm_parse_assist") or {}).get("status"),
            "promotion_status": normalized.get("promotion_status"),
            "standard_input_candidate": (
                normalized.get("standard_input_candidate")
                or normalized.get("standard_input_package")
            ),
            "validation_report": validation,
            "input_quality_scores": normalized.get("input_quality_scores"),
            "requires_user_confirmation": normalized.get(
                "requires_user_confirmation", False
            ),
            "llm_usage": normalized.get("llm_parse_assist"),
            "llm_standard_input_candidate": normalized.get(
                "llm_standard_input_candidate"
            ),
            "llm_standard_input_validation": normalized.get(
                "llm_standard_input_validation"
            ),
            "llm_promotion_status": normalized.get("llm_promotion_status"),
            "candidate_mapping_ir": normalized.get("candidate_mapping_ir"),
            "llm_candidate_mapping_ir": normalized.get("llm_candidate_mapping_ir"),
            "mapping_evidence": normalized.get("parse_diagnostics"),
            "artifacts": artifact_paths,
        }
        if include_import:
            response["_normalized_import"] = normalized
        return response

    def project(self, project_id: str) -> dict[str, Any]:
        return project_response(get_state(project_id))

    def model_parameters(self, project_id: str) -> dict[str, Any]:
        payload = self.parameters.get(project_id)
        parameter_set = ParameterSet.model_validate(payload["parameter_set"])
        return {
            "schema_version": parameter_set.schema_version,
            "revision": parameter_set.revision,
            "parameters": [
                record.model_dump(mode="json")
                for record in parameter_set.parameters.values()
            ],
            "validation": payload["validation"],
        }

    def _llm_mapping(
        self,
        mode: str,
        profile: dict[str, Any],
        validation: dict[str, Any],
        normalized: dict[str, Any],
        llm_required: bool,
    ) -> dict[str, Any]:
        if mode == "forced_review" and not llm_required and validation.get("status") == "pass":
            result = qwen_review_standard_input(
                self.settings,
                profile,
                validation,
                normalized.get("standard_input_candidate")
                or normalized.get("standard_input_package")
                or {},
            )
        else:
            result = qwen_normalize_standard_input(
                self.settings,
                profile,
                validation,
            )
        normalized["llm_parse_assist"] = {
            **(normalized.get("llm_parse_assist") or {}),
            **{
                key: value
                for key, value in result.items()
                if key not in {"candidate", "review"}
            },
            "enabled": True,
            "triggered": True,
            "required": llm_required,
            "llm_mode": mode,
            "candidate_available": bool(result.get("candidate")),
        }
        return result

    @staticmethod
    def _attach_llm_candidate(
        normalized: dict[str, Any],
        source_path: Path,
        llm_result: dict[str, Any],
    ) -> None:
        candidate = llm_result.get("candidate")
        if not candidate:
            return
        if candidate.get("schema_version") == "candidate_mapping_ir_v1" or candidate.get("fields"):
            normalized["llm_candidate_mapping_ir"] = candidate
            converted = candidate_mapping_to_standard_input(candidate, source_path)
            normalized["llm_standard_input_candidate"] = converted.get(
                "standard_input_package"
            )
            normalized["llm_standard_input_validation"] = (
                converted.get("validation_report")
                or converted.get("candidate_validation")
            )
            normalized["llm_promotion_status"] = converted.get(
                "promotion_status", "candidate_only"
            )
            return
        normalized["llm_standard_input_candidate"] = candidate
        candidate_payload = {
            "standard_input_package": candidate,
            "fem_params": normalized.get("fem_params", {}),
        }
        candidate_validation = validate_standard_input(candidate_payload)
        if not candidate.get("source_trace"):
            candidate_validation.setdefault("issues", []).append(
                {
                    "severity": "error",
                    "path": "source_trace",
                    "message": "LLM candidate lacks source evidence and cannot be promoted.",
                }
            )
            candidate_validation.update(
                status="blocked",
                blocked=True,
                requires_user_confirmation=True,
            )
        normalized["llm_standard_input_validation"] = candidate_validation
        normalized["llm_promotion_status"] = promote_standard_input(
            candidate_payload,
            candidate_validation,
        ).get("promotion_status")


def _resolve_llm_mode(
    llm_mode: str | None,
    *,
    enable_llm: bool,
    force_llm: bool,
) -> str:
    if llm_mode:
        normalized = llm_mode.strip().lower()
        if normalized not in {"off", "adaptive", "forced_parse", "forced_review"}:
            raise ValueError(f"Unsupported llm_mode: {llm_mode}")
        return normalized
    if force_llm:
        return "forced_parse"
    return "adaptive" if enable_llm else "off"
