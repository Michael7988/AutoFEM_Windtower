from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from ..config import INPUT_BATCH_ROOT
from ..audit import append_audit_event
from ..importers.standard_input import validate_standard_input
from ..modeling import create_l2_project, create_project, get_state, write_json
from ..modeling.capabilities import current_capability, get_capability
from ..capability_profile import L2_CAPABILITY_ID
from ..capability_profile import EXPLORATORY_INPUT_EXTENSIONS
from ..parameter_normalization import normalize_parameter_value
from ..parameter_ir import (
    CAPABILITY_ID,
    ParameterChange,
    SourceReference,
    apply_changes,
    apply_parameter_set_to_ir,
    parameter_set_from_ir,
)
from ..knowledge import KnowledgeBaseManager
from ..model_library import ModelLibraryService
from ..reference_models import (
    ReferenceModelService,
    capability_fem_specification,
    fem_specification_from_goal,
)
from .advisors import EngineeringAdvisor, FemAdvisor
from .docling_client import DoclingClient, DoclingUnavailable, DoclingArtifactError
from .document_evidence import (
    cross_validate_fragments,
    fragments_from_docling,
    reconcile_evidence,
    sha256_file,
    stabilize_fragment,
    stable_evidence_id,
    write_document_artifacts,
)
from .evidence_retrieval import build_catalog, select_relevant_evidence
from .extractors import (
    BATCH_SIZE_LIMIT,
    DOCUMENT_SUFFIXES,
    EXTRACTOR_VERSION,
    MAX_FILES,
    classify_file,
    extract_file,
    validate_upload,
)
from .models import (
    BatchFile,
    CompleteParametersRequest,
    ConfirmAndCreateRequest,
    ConfirmBatchReviewRequest,
    ConfirmFemSpecificationRequest,
    ConfirmIntentRequest,
    ConfirmTemplateRequest,
    EvidenceFragment,
    InputBatch,
    ParameterCandidate,
    ParameterConflict,
    ReparseRequest,
    ResolveConflictsRequest,
)
from .lifecycle import (
    ensure_fem_state,
    recover_interrupted_batch,
    refresh_status,
    utc_now,
)
from .model_templates import ModelTemplateRegistry
from .providers import create_engineering_provider, create_fem_provider
from .reference_storage import (
    commit_reference,
    discard_staged_reference,
    stage_reference,
)
from ..persistence import MetadataStore


LEGACY_PARAMETER_LABEL_SPECS = (
    ("geometry.tower_sections", "塔筒分段", None),
    ("geometry.door.enabled", "门洞启用状态", None),
    ("material.name", "材料名称", None),
    ("material.elastic_modulus_mpa", "弹性模量", "MPa"),
    ("material.poisson_ratio", "泊松比", None),
    ("material.density", "材料密度", "tonne/mm³"),
    ("material.yield_strength_mpa", "屈服强度", "MPa"),
    ("mesh.global_size_mm", "全局网格尺寸", "mm"),
    ("loads.top_mass_tonne", "顶部质量", "tonne"),
    ("loads.center_of_mass_offset_mm", "顶部质心偏心距", "mm"),
    ("loads.top_horizontal_force_n", "顶部水平力", "N"),
    ("loads.top_force_direction", "顶部力方向", None),
    ("loads.wind_pressure_mpa", "风压", "MPa"),
    ("loads.wind_direction_vector", "风向矢量", None),
    ("loads.wind_pressure_profile", "分段风压", "MPa"),
    ("loads.top_force_parallel_n", "顺风向顶部力", "N"),
    ("loads.top_force_transverse_n", "横风向顶部力", "N"),
    ("loads.top_force_vertical_n", "顶部竖向力", "N"),
    ("loads.top_moment_roll_nmm", "局部滚转矩", "N·mm"),
    ("loads.top_moment_pitch_nmm", "局部俯仰矩", "N·mm"),
    ("loads.top_moment_yaw_nmm", "偏航矩", "N·mm"),
    ("loads.hub_vertical_offset_mm", "轮毂竖向力臂", "mm"),
    ("geometry.door.reinforcement_enabled", "门洞加强圈", None),
    ("geometry.door.outer_width_mm", "加强圈外宽", "mm"),
    ("geometry.door.outer_height_mm", "加强圈外高", "mm"),
    ("geometry.door.frame_thickness_mm", "加强圈厚度", "mm"),
    ("geometry.door.reinforcement_material", "加强圈材料", None),
    ("analysis.static_enabled", "静力分析开关", None),
    ("analysis.modal_enabled", "模态分析开关", None),
    ("boundary.name", "边界条件", None),
)

LLM_PARSE_MODES = {"llm", "llm_full"}


def _uses_llm(parse_mode: str) -> bool:
    return parse_mode in LLM_PARSE_MODES


class EngineeringInputBatchService:
    def __init__(
        self,
        pipeline: Any,
        model_library: ModelLibraryService | None = None,
    ):
        self.pipeline = pipeline
        self.knowledge = KnowledgeBaseManager()
        self.engineering_advisor = EngineeringAdvisor(
            create_engineering_provider(pipeline.settings),
            self.knowledge.engineering,
        )
        self.fem_advisor = FemAdvisor(
            create_fem_provider(pipeline.settings),
            self.knowledge.fem,
        )
        self.provider = self.engineering_advisor.provider
        self.docling = DoclingClient(
            getattr(pipeline.settings, "docling_service_url", None),
            getattr(pipeline.settings, "docling_timeout_seconds", 300),
        )
        self.reference_models = ReferenceModelService(self.fem_advisor)
        self.templates = ModelTemplateRegistry()
        self.model_library = model_library or ModelLibraryService()
        self.root = INPUT_BATCH_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_root = self.root / "_cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._batches: dict[str, InputBatch] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="engineering-input")
        self.metadata = MetadataStore(pipeline.settings.metadata_db)
        self._load_existing()

    def capabilities(self) -> dict[str, Any]:
        engineering = self.engineering_advisor.capabilities()
        return {
            **engineering,
            "schema_version": "engineering_llm_capabilities_v1",
            "architecture_version": "dual_advisor_v1",
            "advisors": {
                "engineering": engineering,
                "fem": self.fem_advisor.capabilities(),
            },
            "knowledge_bases": self.knowledge.status(),
            "document_evidence": {
                "schema_version": "document_evidence_v1",
                "docling": self.docling.health(),
                "markdown_and_json": True,
            },
        }

    def connection_test(self) -> dict[str, Any]:
        results = {
            "engineering": self.engineering_advisor.connection_test(),
            "fem": self.fem_advisor.connection_test(),
        }
        return {
            "schema_version": "dual_llm_connection_test_v1",
            "status": "ready" if all(item.get("status") == "ready" for item in results.values()) else "partial",
            "advisors": results,
        }

    def knowledge_status(self) -> dict[str, Any]:
        return self.knowledge.status()

    def select_library_model(self, batch_id: str, model_id: str) -> dict[str, Any]:
        entry = self.model_library.get_entry(model_id)
        reference_path = entry["reference_path"]
        batch = self._get(batch_id)
        result = self.reference_models.analyze(
            reference_path.name,
            reference_path.read_bytes(),
            modeling_goal=batch.modeling_goal,
            run_llm=_uses_llm(batch.parse_mode),
        )
        stored_name = f"library-{model_id}-reference.inp"
        staged: Path | None = stage_reference(
            self.root / batch_id / "reference",
            reference_path.read_bytes(),
        )
        try:
            with self._lock:
                batch = self._get(batch_id)
                if batch.status == "confirmed":
                    raise ValueError("项目已创建，不能替换其准备阶段参考模型。")
                assert staged is not None
                commit_reference(staged, stored_name)
                staged = None
                manifest = entry["manifest"]
                batch.selected_library_model = manifest
                batch.reference_model = {
                    "summary": result["summary"],
                    "stored_name": stored_name,
                    "llm_advice": result["llm_advice"],
                    "engineering_candidates": result["reference_engineering_candidates"],
                    "library_model_id": model_id,
                }
                specification = result["fem_specification"]
                for key, record in entry["fem_profile"].items():
                    if key not in specification["parameters"]:
                        continue
                    current = specification["parameters"][key]
                    current["alternatives"] = [dict(current)]
                    current.update({
                        "value": record.get("value"),
                        "origin": "model_library",
                        "confirmation_status": "pending",
                        "requires_confirmation": True,
                        "model_id": model_id,
                    })
                specification["reference_model_id"] = result["summary"].get("reference_model_id")
                batch.fem_specification = specification
                batch.fem_review_revision += 1
                batch.fem_review = {"status": "pending", "confirmed_at": None, "operator": None}
                batch.capability_match = _capability_match(result["summary"])
                self._add_library_engineering_candidates(batch, entry["parameter_set"], model_id)
                self._sync_reference_engineering_candidates(batch)
                self._merge_candidates(batch)
                self._build_readiness(batch)
                self._refresh_status(batch)
                self._save(batch)
        finally:
            discard_staged_reference(staged)
        return self.get_batch(batch_id)

    def _add_library_engineering_candidates(
        self,
        batch: InputBatch,
        parameter_set: Any,
        model_id: str,
    ) -> None:
        existing_keys = {
            item.key
            for item in batch.candidates
            if item.source_kind in {"file_observed", "engineer_input"}
            and item.normalized_value is not None
        }
        batch.candidates = [
            item for item in batch.candidates
            if not (
                item.source_kind == "model_library_reference"
                and item.source_file_id == model_id
            )
        ]
        for key, record in parameter_set.parameters.items():
            if record.domain != "engineering" or key in existing_keys or record.value is None:
                continue
            batch.candidates.append(ParameterCandidate(
                candidate_id=f"library-{hashlib.sha256((model_id + key).encode()).hexdigest()[:12]}",
                key=key,
                label=record.label,
                raw_value=record.value,
                raw_unit=record.unit,
                normalized_value=record.value,
                normalized_unit=record.unit,
                source_file_id=model_id,
                evidence_ids=[],
                extraction_method="model_library_profile",
                provider="model_library",
                model=model_id,
                confidence=1.0,
                priority="P1",
                requires_confirmation=True,
                confirmation_reason="参考项目值仅用于补充缺失候选，必须由工程师确认。",
                source_kind="model_library_reference",
                confirmation_status="pending",
            ))

    def _sync_reference_engineering_candidates(self, batch: InputBatch) -> None:
        batch.candidates = [
            item for item in batch.candidates
            if item.source_kind != "reference_model_value"
        ]
        reference = batch.reference_model or {}
        summary = reference.get("summary") or {}
        reference_id = str(summary.get("reference_model_id") or "reference-model")
        observed_keys = {
            item.key
            for item in batch.candidates
            if item.source_kind in {"file_observed", "engineer_input", "goal_explicit"}
            and item.normalized_value is not None
        }
        for record in reference.get("engineering_candidates") or []:
            key = str(record.get("key") or "")
            if not key or key in observed_keys or record.get("value") is None:
                continue
            normalized, conversion, error = _safe_normalize_parameter(
                key,
                record["value"],
            )
            if error:
                continue
            locator = str(record.get("source_locator") or "Abaqus reference model")
            source_lines = [int(line) for line in record.get("source_lines") or []]
            if source_lines:
                locator = f"{locator}; line {source_lines[0]}"
            batch.candidates.append(ParameterCandidate(
                candidate_id=f"reference-{hashlib.sha256((reference_id + key).encode()).hexdigest()[:12]}",
                key=key,
                label=_parameter_label(key),
                raw_value=record["value"],
                raw_unit=record.get("unit"),
                normalized_value=normalized,
                normalized_unit=record.get("unit"),
                conversion=conversion,
                source_file_id=reference_id,
                evidence_ids=[],
                extraction_method="deterministic_reference_inp",
                provider="deterministic",
                model="none",
                confidence=float(record.get("confidence") or 1.0),
                priority="P1",
                requires_confirmation=True,
                confirmation_reason="参考模型值仅补充工程资料缺失项，必须由工程师确认。",
                source_kind="reference_model_value",
                confirmation_status="pending",
                assumption_basis=locator,
            ))

    def model_templates(self) -> dict[str, Any]:
        return self.templates.list_public()

    def create_batch(
        self,
        uploads: list[tuple[str, str, bytes]],
        modeling_goal: str | None = None,
        parse_mode: str = "deterministic",
        llm_execution_mode: str = "single",
        reference_model: tuple[str, bytes] | None = None,
        model_library_id: str | None = None,
        capability_id: str = CAPABILITY_ID,
    ) -> dict[str, Any]:
        get_capability(capability_id)
        if capability_id == L2_CAPABILITY_ID and (reference_model is not None or model_library_id):
            raise ValueError("L2 专家基线是只读研究资产，不能作为建项时的参数迁移来源。")
        if parse_mode not in {"llm", "llm_full", "deterministic"}:
            raise ValueError("parse_mode 必须是 llm、llm_full 或 deterministic")
        if llm_execution_mode not in {"single", "chunked_parallel"}:
            raise ValueError("llm_execution_mode 必须是 single 或 chunked_parallel")
        modeling_goal = (modeling_goal or "").strip() or (
            "建立带门洞风电塔筒 S4R 壳模型，执行线弹性静力与模态分析。"
        )
        if reference_model is not None and model_library_id:
            raise ValueError("参考 INP 与已验证模型库只能选择一种 FEM 规范来源。")
        capabilities = self.provider.capabilities()
        if _uses_llm(parse_mode) and not capabilities.get("configured"):
            raise RuntimeError("LLM 尚未配置：请在后端 .env 中填写 QWEN_API_KEY 后重启服务")
        if len(uploads) > MAX_FILES:
            raise ValueError(f"一次最多上传 {MAX_FILES} 个文件")
        if sum(len(item[2]) for item in uploads) > BATCH_SIZE_LIMIT:
            raise ValueError("整批文件超过 200 MB 限制")
        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        now = _now()
        batch_root = self.root / batch_id
        source_root = batch_root / "source"
        source_root.mkdir(parents=True, exist_ok=True)
        files = []
        seen_hashes: dict[str, str] = {}
        for index, (filename, media_type, content) in enumerate(uploads):
            if Path(filename).suffix.lower() == ".inp":
                raise ValueError("INP 请通过“参考模型”入口单独上传。")
            validate_upload(filename, content)
            digest = hashlib.sha256(content).hexdigest()
            if digest in seen_hashes:
                raise ValueError(f"{filename} 与 {seen_hashes[digest]} 内容重复")
            seen_hashes[digest] = filename
            file_id = f"file-{index + 1:02d}-{digest[:8]}"
            method, cost = classify_file(filename, media_type)
            safe_name = _safe_name(filename)
            (source_root / f"{file_id}-{safe_name}").write_bytes(content)
            files.append(BatchFile(
                file_id=file_id,
                filename=filename,
                suffix=Path(filename).suffix.lower(),
                media_type=media_type or "application/octet-stream",
                size=len(content),
                sha256=digest,
                processing_method=method,
                cost_level=cost,
                support_tier="exploratory" if Path(filename).suffix.lower() in EXPLORATORY_INPUT_EXTENSIONS else "formal",
            ))
        batch = InputBatch(
            batch_id=batch_id,
            capability_id=capability_id,
            modeling_goal=modeling_goal,
            parse_mode=parse_mode,
            llm_execution_mode=llm_execution_mode,
            provider=str(capabilities.get("provider") if _uses_llm(parse_mode) else "deterministic"),
            model=str(capabilities.get("main_model") if _uses_llm(parse_mode) else "none"),
            files=files,
            fem_specification=capability_fem_specification(capability_id),
            capability_match=_capability_match(None, capability_id),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._batches[batch_id] = batch
            self._save(batch)
        if reference_model is not None:
            self.put_reference_model(
                batch_id,
                reference_model[0],
                reference_model[1],
            )
        elif model_library_id:
            self.select_library_model(batch_id, model_library_id)
        elif capability_id != L2_CAPABILITY_ID:
            goal_fem = fem_specification_from_goal(
                self.fem_advisor,
                modeling_goal,
                # llm_full performs its FEM mapping after document evidence is
                # available. Calling the goal advisor here would mix a third,
                # non-evidence-grounded path into the pure-LLM mode.
                run_llm=parse_mode == "llm",
            )
            with self._lock:
                current = self._get(batch_id)
                current.fem_specification = goal_fem["fem_specification"]
                current.fem_llm_usage = self.fem_advisor.usage()
                current.capability_match = _capability_match(None)
                current.fem_channel["source"] = "modeling_goal"
                self._save(current)
        self._executor.submit(self._process_batch, batch_id)
        return self.get_batch(batch_id)

    def create_batch_from_paths(
        self,
        uploads: list[tuple[str, str, Path]],
        modeling_goal: str | None = None,
        parse_mode: str = "deterministic",
        llm_execution_mode: str = "single",
        reference_model: tuple[str, Path] | None = None,
        model_library_id: str | None = None,
        capability_id: str = CAPABILITY_ID,
    ) -> dict[str, Any]:
        return self.create_batch(
            [(filename, media_type, path.read_bytes()) for filename, media_type, path in uploads],
            modeling_goal=modeling_goal,
            parse_mode=parse_mode,
            llm_execution_mode=llm_execution_mode,
            reference_model=(
                (reference_model[0], reference_model[1].read_bytes())
                if reference_model is not None
                else None
            ),
            model_library_id=model_library_id,
            capability_id=capability_id,
        )

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            self._upgrade_legacy_door_axis_candidates(batch)
            self._upgrade_reference_summary(batch)
            self._refresh_channel_snapshots(batch)
            return batch.model_dump()

    def list_documents(self, batch_id: str) -> dict[str, Any]:
        batch = self._get(batch_id)
        rows = []
        for file in batch.files:
            root = self.root / batch_id / "documents" / file.file_id
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
            rows.append({
                "file_id": file.file_id,
                "filename": file.filename,
                "status": file.document_status,
                "manifest": manifest,
            })
        return {"schema_version": "document_collection_v1", "batch_id": batch_id, "documents": rows}

    def get_document_markdown(self, batch_id: str, file_id: str) -> str:
        self._get(batch_id)
        path = self.root / batch_id / "documents" / file_id / "document.md"
        if not path.is_file():
            raise FileNotFoundError(file_id)
        return path.read_text(encoding="utf-8")

    def query_evidence(
        self,
        batch_id: str,
        *,
        evidence_id: str | None = None,
        field: str | None = None,
        status: str | None = None,
        file_id: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        batch = self._get(batch_id)
        rows = []
        for item in batch.evidence:
            if evidence_id and item.evidence_id != evidence_id:
                continue
            if field and item.field != field:
                continue
            if status and item.status != status:
                continue
            if file_id and item.file_id != file_id:
                continue
            if location:
                locator = json.dumps(item.location.model_dump(mode="json"), ensure_ascii=False)
                if location.lower() not in locator.lower():
                    continue
            rows.append(item.model_dump(mode="json"))
        return {
            "schema_version": "document_evidence_query_v1",
            "batch_id": batch_id,
            "count": len(rows),
            "evidence": rows,
        }

    def _upgrade_legacy_door_axis_candidates(self, batch: InputBatch) -> None:
        if batch.status == "confirmed":
            return
        evidence_by_id = {item.evidence_id: item for item in batch.evidence}
        affected_sources = {
            item.source_file_id
            for item in batch.candidates
            if item.key in {"geometry.door.width_mm", "geometry.door.height_mm"}
            and item.normalized_value == 2.0
            and any(
                (evidence.location.cell_or_range or "") in {"R4C1", "R5C1"}
                for evidence_id in item.evidence_ids
                for evidence in [evidence_by_id.get(evidence_id)]
                if evidence is not None
            )
        }
        if not affected_sources:
            return
        affected_keys = {"geometry.door.width_mm", "geometry.door.height_mm"}
        batch.candidates = [
            item
            for item in batch.candidates
            if not (
                item.key in affected_keys
                and item.normalized_value == 2.0
                and item.source_file_id in affected_sources
            )
        ]
        for key in affected_keys:
            if batch.resolved_parameters.get(key) == 2.0:
                batch.resolved_parameters.pop(key, None)
        warning = "已移除旧解析器将门洞字段 X2/Y2 行号误识别为 2 mm 的候选；请补充真实门洞尺寸。"
        if warning not in batch.warnings:
            batch.warnings.append(warning)
        batch.engineering_review = {
            "status": "pending",
            "confirmed_at": None,
            "operator": None,
        }
        self._merge_candidates(batch)
        if batch.selected_template_id:
            self._build_readiness(batch)
            self._refresh_status(batch)
        self._save(batch)

    def _upgrade_reference_summary(self, batch: InputBatch) -> None:
        reference = batch.reference_model or {}
        summary = reference.get("summary") or {}
        if (
            batch.status == "confirmed"
            or not reference
            or (
                summary.get("schema_version") == "reference_model_summary_v2"
                and "reference_transfer" in summary
            )
        ):
            return
        stored_name = str(reference.get("stored_name") or "")
        source = self.root / batch.batch_id / "reference" / stored_name
        if not stored_name or not source.is_file():
            return
        result = self.reference_models.analyze(
            str(summary.get("filename") or source.name),
            source.read_bytes(),
            modeling_goal=batch.modeling_goal,
            run_llm=False,
        )
        batch.reference_model = {
            **reference,
            "summary": result["summary"],
            "llm_advice": result["llm_advice"],
            "engineering_candidates": result["reference_engineering_candidates"],
        }
        batch.fem_specification = result["fem_specification"]
        batch.fem_review_revision += 1
        batch.fem_review = {
            "status": "pending",
            "confirmed_at": None,
            "operator": None,
        }
        batch.capability_match = _capability_match(result["summary"])
        self._sync_reference_engineering_candidates(batch)
        if batch.selected_template_id:
            self._build_readiness(batch)
            self._refresh_status(batch)
        self._save(batch)

    def modeling_capability(self) -> dict[str, Any]:
        return current_capability()

    def put_reference_model(
        self,
        batch_id: str,
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            if batch.status == "confirmed":
                raise ValueError("项目已创建，不能替换其准备阶段参考模型。")
        result = self.reference_models.analyze(
            filename,
            content,
            modeling_goal=batch.modeling_goal,
            run_llm=_uses_llm(batch.parse_mode),
        )
        safe_name = _safe_name(filename)
        stored_name = f"reference-{result['summary']['sha256'][:12]}-{safe_name}"
        staged: Path | None = stage_reference(
            self.root / batch_id / "reference",
            content,
        )
        try:
            with self._lock:
                batch = self._get(batch_id)
                if batch.status == "confirmed":
                    raise ValueError("项目已创建，不能替换其准备阶段参考模型。")
                assert staged is not None
                commit_reference(staged, stored_name)
                staged = None
                batch.selected_library_model = None
                batch.reference_model = {
                    "summary": result["summary"],
                    "stored_name": stored_name,
                    "llm_advice": result["llm_advice"],
                    "engineering_candidates": result["reference_engineering_candidates"],
                }
                batch.fem_specification = result["fem_specification"]
                batch.fem_review_revision += 1
                batch.fem_review = {
                    "status": "pending",
                    "confirmed_at": None,
                    "operator": None,
                }
                batch.capability_match = _capability_match(result["summary"])
                batch.fem_llm_usage = self.fem_advisor.usage()
                self._sync_reference_engineering_candidates(batch)
                if batch.selected_template_id:
                    self._build_readiness(batch)
                    self._refresh_status(batch)
                self._save(batch)
        finally:
            discard_staged_reference(staged)
        return self.get_batch(batch_id)

    def put_reference_model_from_path(
        self,
        batch_id: str,
        filename: str,
        path: Path,
    ) -> dict[str, Any]:
        return self.put_reference_model(batch_id, filename, path.read_bytes())

    def delete_reference_model(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            if batch.status == "confirmed":
                raise ValueError("项目已创建，不能移除其准备阶段参考模型。")
            reference_root = self.root / batch_id / "reference"
            if reference_root.is_dir():
                for existing in reference_root.glob("*.inp"):
                    existing.unlink()
            batch.reference_model = None
            batch.selected_library_model = None
            batch.fem_specification = capability_fem_specification(batch.capability_id)
            batch.fem_review_revision += 1
            batch.fem_review = {
                "status": "pending",
                "confirmed_at": None,
                "operator": None,
            }
            batch.capability_match = _capability_match(None, batch.capability_id)
            batch.candidates = [
                item for item in batch.candidates
                if item.source_kind != "reference_model_value"
            ]
            self._merge_candidates(batch)
            if batch.selected_template_id:
                self._build_readiness(batch)
                self._refresh_status(batch)
            self._save(batch)
        return self.get_batch(batch_id)

    def confirm_fem_specification(
        self,
        batch_id: str,
        request: ConfirmFemSpecificationRequest,
    ) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            if self._fem_review_is_replay(batch, request):
                return batch.model_dump()
            self._apply_fem_review(batch, request)
            self._refresh_status(batch)
            self._save(batch)
        return self.get_batch(batch_id)

    @staticmethod
    def _fem_review_is_replay(
        batch: InputBatch,
        request: ConfirmFemSpecificationRequest,
    ) -> bool:
        if (
            batch.fem_review.get("status") != "confirmed"
            or request.review_revision != batch.fem_review_revision
        ):
            return False
        parameters = batch.fem_specification.get("parameters") or {}
        for submitted in request.parameters:
            current = parameters.get(submitted.key)
            if current is None:
                return False
            normalized, _, error = _safe_normalize_parameter(
                submitted.key,
                submitted.value,
            )
            if error or not _same_value(current.get("value"), normalized):
                return False
        return True

    def _apply_fem_review(
        self,
        batch: InputBatch,
        request: ConfirmFemSpecificationRequest,
    ) -> None:
        self._ensure_fem_state(batch)
        if request.review_revision != batch.fem_review_revision:
            raise ValueError("FEM 参数候选已经更新，请刷新后重新确认。")
        compatibility = str(batch.fem_specification.get("compatibility") or "not_provided")
        if compatibility == "incompatible":
            raise ValueError("参考模型不符合当前建模能力，请先移除参考模型。")
        allowed = set(get_capability(batch.capability_id)["editable_fem_parameters"])
        parameters = batch.fem_specification.setdefault("parameters", {})
        submitted: dict[str, Any] = {}
        for item in request.parameters:
            if item.key not in allowed:
                current = parameters.get(item.key)
                normalized, _, error = _safe_normalize_parameter(item.key, item.value)
                # Older confirmation clients may echo the complete FEM
                # specification. Ignore fixed rules only when unchanged.
                if current is not None and not error and _same_value(
                    current.get("value"), normalized
                ):
                    continue
                raise ValueError(f"不允许修改的 FEM 参数：{item.key}")
            normalized, _, error = _safe_normalize_parameter(item.key, item.value)
            if error:
                raise ValueError(error)
            submitted[item.key] = normalized
        for key, item in parameters.items():
            if key in submitted:
                previous = {
                    "branch": _fem_source_branch(str(item.get("origin") or "capability_default")),
                    "origin": item.get("origin"),
                    "value": deepcopy(item.get("value")),
                    "evidence_ids": list(item.get("evidence_ids") or []),
                }
                item["value"] = submitted[key]
                item["origin"] = "engineer_input"
                sources = list(item.get("sources") or [])
                if previous not in sources:
                    sources.append(previous)
                sources.append({
                    "branch": "engineer_input",
                    "origin": "engineer_input",
                    "operator": request.operator,
                    "value": deepcopy(submitted[key]),
                })
                item["sources"] = sources
            item["confirmation_status"] = "confirmed"
            item["requires_confirmation"] = False
        batch.fem_specification["status"] = "confirmed"
        batch.fem_specification["requires_engineer_confirmation"] = False
        batch.fem_review = {
            "status": "confirmed",
            "confirmed_at": _now(),
            "operator": request.operator,
            "confirmed_parameter_keys": sorted(parameters),
        }
        self._build_readiness(batch)

    def confirm_and_create(
        self,
        batch_id: str,
        request: ConfirmAndCreateRequest,
    ) -> dict[str, Any]:
        with self._lock:
            original = deepcopy(self._get(batch_id))
            if original.status == "confirmed":
                return self._confirmed_project_response(original)
            staged = deepcopy(original)
            self._apply_fem_review(staged, ConfirmFemSpecificationRequest(
                review_revision=request.fem_review_revision,
                parameters=request.fem_parameters,
                operator=request.operator,
            ))
            self._apply_final_review(staged, ConfirmBatchReviewRequest(
                review_revision=request.engineering_review_revision,
                parameters=request.engineering_parameters,
                operator=request.operator,
            ))
            self._batches[batch_id] = staged
        try:
            return self.confirm(batch_id)
        except Exception:
            with self._lock:
                self._batches[batch_id] = original
                self._save(original)
            raise

    def confirm_template(self, batch_id: str, request: ConfirmTemplateRequest) -> dict[str, Any]:
        template = self.templates.get(request.template_id, request.version)
        with self._lock:
            batch = self._get(batch_id)
            if batch.status not in {
                "awaiting_template_confirmation", "awaiting_intent_confirmation",
                "ready", "completed_with_llm_warning", "blocked", "needs_input",
            }:
                raise ValueError("当前批次还不能确认模型模板")
            batch.selected_template_id = request.template_id
            batch.selected_template_version = str(template["version"])
            batch.prompt_hash = str(template["prompt_hash"])
            batch.intent_contract = _infer_modeling_intent(
                batch.modeling_goal,
                batch.resolved_parameters,
                template_id=request.template_id,
            )
            batch.status = "awaiting_intent_confirmation"
            batch.progress = 85
            self._save(batch)
        return self.get_batch(batch_id)

    def confirm_intent(self, batch_id: str, request: ConfirmIntentRequest) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            if request.model_family != "wind_tower":
                raise ValueError("首版仅安装风塔模型适配器")
            supported_tasks = {"static", "modal"}
            unsupported_tasks = set(request.analysis_tasks) - supported_tasks
            if unsupported_tasks:
                raise ValueError(f"当前适配器不支持分析任务：{', '.join(sorted(unsupported_tasks))}")
            batch.intent_contract = {
                "schema_version": "modeling_intent_contract_v1",
                "model_family": request.model_family,
                "fidelity": request.fidelity,
                "analysis_tasks": list(dict.fromkeys(request.analysis_tasks or ["static", "modal"])),
                "requested_features": list(dict.fromkeys(request.requested_features or ["tower_shell"])),
                "discretization": {
                    "dimension": "shell",
                    "element_family": "S4R",
                    "mesh_strategy": "auto_structured",
                },
                "output_targets": list(dict.fromkeys(request.output_targets)),
                "solver": "abaqus",
                "original_goal": batch.modeling_goal or "",
                "source": "engineer_confirmed",
                "confirmation_status": "confirmed",
            }
            self._update_composition(batch)
            batch.status = "queued"
            batch.progress = 85
            self._save(batch)
        self._executor.submit(self._process_batch, batch_id, set())
        return self.get_batch(batch_id)

    def parameter_diff(self, batch_id: str) -> dict[str, Any]:
        batch = self._get(batch_id)
        rows = []
        for key in sorted(set(item.key for item in batch.candidates)):
            deterministic = [
                item for item in batch.candidates
                if item.key == key and item.source_kind in {"file_observed", "goal_explicit", "engineer_input"}
            ]
            llm = [
                item for item in batch.candidates
                if item.key == key and item.source_kind in {"llm_mapped", "llm_suggested"}
            ]
            baseline = sorted(deterministic, key=_candidate_rank, reverse=True)[0] if deterministic else None
            supplement = sorted(llm, key=_candidate_rank, reverse=True)[0] if llm else None
            final = batch.resolved_parameters.get(key)
            if baseline and supplement and _same_value(baseline.normalized_value, supplement.normalized_value):
                difference = "same_value_additional_evidence"
            elif baseline and supplement:
                difference = "conflict"
            elif supplement:
                difference = "llm_only"
            else:
                difference = "deterministic_only"
            conflict = next(
                (item for item in batch.conflicts if item.key == key),
                None,
            )
            rows.append({
                "key": key,
                "label": _parameter_label(key),
                "deterministic_value": baseline.normalized_value if baseline else None,
                "llm_value": supplement.normalized_value if supplement else None,
                "final_value": final,
                "difference": difference,
                "deterministic_source": (
                    {
                        "candidate_id": baseline.candidate_id,
                        "source_kind": baseline.source_kind,
                        "source_file_id": baseline.source_file_id,
                        "evidence_ids": baseline.evidence_ids,
                        "raw_value": baseline.raw_value,
                        "raw_unit": baseline.raw_unit,
                        "conversion": baseline.conversion,
                    }
                    if baseline else None
                ),
                "llm_source": (
                    {
                        "candidate_id": supplement.candidate_id,
                        "source_kind": supplement.source_kind,
                        "provider": supplement.provider,
                        "model": supplement.model,
                        "evidence_ids": supplement.evidence_ids,
                        "assumption_basis": supplement.assumption_basis,
                    }
                    if supplement else None
                ),
                "conflict_reason": (
                    "不同来源提供了不同规范值；最终值按固定候选优先级选择。"
                    if conflict else None
                ),
                "resolved_candidate_id": (
                    conflict.resolved_candidate_id if conflict else None
                ),
                "activation_status": next((
                    item.get("activation_status")
                    for item in batch.model_composition.get("parameter_states") or []
                    if item.get("key") == key
                ), "unsupported"),
            })
        return {
            "schema_version": "parameter_parse_diff_v1",
            "batch_id": batch_id,
            "parse_mode": batch.parse_mode,
            "rows": rows,
            "summary": {
                "same": sum(item["difference"] == "same_value_additional_evidence" for item in rows),
                "conflicts": sum(item["difference"] == "conflict" for item in rows),
                "llm_only": sum(item["difference"] == "llm_only" for item in rows),
                "deterministic_only": sum(item["difference"] == "deterministic_only" for item in rows),
            },
        }

    def retry_llm(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            if not _uses_llm(batch.parse_mode):
                raise ValueError("当前批次不是 LLM 解析模式")
            if not self.provider.capabilities().get("configured"):
                raise RuntimeError("LLM 尚未配置")
            batch.status = "processing"
            batch.progress = 85
            batch.llm_partial_failure = None
            self._save(batch)
        self._executor.submit(self._process_batch, batch_id, set())
        return self.get_batch(batch_id)

    def resolve_conflicts(self, batch_id: str, request: ResolveConflictsRequest) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            by_id = {item.conflict_id: item for item in batch.conflicts}
            candidates = {item.candidate_id: item for item in batch.candidates}
            for resolution in request.resolutions:
                conflict = by_id.get(resolution.conflict_id)
                if conflict is None:
                    raise ValueError(f"未知冲突项：{resolution.conflict_id}")
                if resolution.candidate_id:
                    if resolution.candidate_id not in conflict.candidate_ids:
                        raise ValueError("所选候选不属于该冲突")
                    candidate = candidates[resolution.candidate_id]
                    conflict.resolved_candidate_id = candidate.candidate_id
                    conflict.resolved_value = candidate.normalized_value
                    conflict.resolved_unit = candidate.normalized_unit
                elif resolution.value is not None:
                    conflict.resolved_value, _ = normalize_parameter_value(
                        conflict.key, resolution.value
                    )
                    conflict.resolved_unit = resolution.unit
                else:
                    raise ValueError("请选择来源或填写确认值")
                conflict.confirmation_status = "confirmed"
                batch.resolved_parameters[conflict.key] = conflict.resolved_value
                for item in batch.candidates:
                    if item.key == conflict.key:
                        item.confirmation_status = "confirmed"
                        item.requires_confirmation = False
            self._build_readiness(batch)
            self._refresh_status(batch)
            self._save(batch)
            return batch.model_dump()

    def complete_parameters(self, batch_id: str, request: CompleteParametersRequest) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            for change in request.changes:
                if not change.key.strip():
                    raise ValueError("参数 key 不能为空")
                normalized_value, conversion = normalize_parameter_value(
                    change.key, change.value
                )
                batch.resolved_parameters[change.key] = normalized_value
                batch.candidates = [item for item in batch.candidates if not (item.key == change.key and item.extraction_method == "manual")]
                batch.candidates.append(ParameterCandidate(
                    candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
                    key=change.key,
                    label=_parameter_label(change.key),
                    raw_value=change.value,
                    raw_unit=change.unit,
                    normalized_value=normalized_value,
                    normalized_unit=change.unit,
                    source_file_id="engineer",
                    evidence_ids=[],
                    extraction_method="manual",
                    provider="engineer",
                    model="none",
                    confidence=1.0,
                    priority="P0",
                    requires_confirmation=False,
                    source_kind="engineer_input",
                    confirmation_status="confirmed",
                    template_id=batch.selected_template_id,
                    prompt_hash=batch.prompt_hash,
                    confirmation_reason="工程师手动补充",
                    conversion=conversion,
                ))
            self._merge_candidates(batch)
            self._build_readiness(batch)
            self._refresh_status(batch)
            self._save(batch)
            return batch.model_dump()

    def append_files(self, batch_id: str, uploads: list[tuple[str, str, bytes]]) -> dict[str, Any]:
        if not uploads:
            raise ValueError("请至少追加一个文件")
        with self._lock:
            batch = self._get(batch_id)
            if len(batch.files) + len(uploads) > MAX_FILES:
                raise ValueError(f"一批最多上传 {MAX_FILES} 个文件")
            if sum(item.size for item in batch.files) + sum(len(item[2]) for item in uploads) > BATCH_SIZE_LIMIT:
                raise ValueError("整批文件超过 200 MB 限制")
            seen = {item.sha256: item.filename for item in batch.files}
            new_ids: set[str] = set()
            source_root = self.root / batch_id / "source"
            for filename, media_type, content in uploads:
                if Path(filename).suffix.lower() == ".inp":
                    raise ValueError("INP 请通过“参考模型”入口单独上传。")
                validate_upload(filename, content)
                digest = hashlib.sha256(content).hexdigest()
                if digest in seen:
                    raise ValueError(f"{filename} 与 {seen[digest]} 内容重复")
                index = len(batch.files) + 1
                file_id = f"file-{index:02d}-{digest[:8]}"
                method, cost = classify_file(filename, media_type)
                (source_root / f"{file_id}-{_safe_name(filename)}").write_bytes(content)
                batch.files.append(BatchFile(
                    file_id=file_id,
                    filename=filename,
                    suffix=Path(filename).suffix.lower(),
                    media_type=media_type or "application/octet-stream",
                    size=len(content),
                    sha256=digest,
                    processing_method=method,
                    cost_level=cost,
                ))
                seen[digest] = filename
                new_ids.add(file_id)
            batch.status = "queued"
            batch.progress = 0
            self._save(batch)
        self._executor.submit(self._process_batch, batch_id, new_ids)
        return self.get_batch(batch_id)

    def append_files_from_paths(
        self,
        batch_id: str,
        uploads: list[tuple[str, str, Path]],
    ) -> dict[str, Any]:
        return self.append_files(
            batch_id,
            [(filename, media_type, path.read_bytes()) for filename, media_type, path in uploads],
        )

    def reparse(self, batch_id: str, request: ReparseRequest) -> dict[str, Any]:
        capabilities = self.provider.capabilities()
        if _uses_llm(request.parse_mode) and not capabilities.get("configured"):
            raise RuntimeError("LLM 尚未配置：请在后端 .env 中填写 QWEN_API_KEY 后重启服务")
        with self._lock:
            batch = self._get(batch_id)
            manual = [item for item in batch.candidates if item.extraction_method == "manual"]
            batch.parse_mode = request.parse_mode
            if request.llm_execution_mode is not None:
                batch.llm_execution_mode = request.llm_execution_mode
            batch.provider = str(capabilities.get("provider") if _uses_llm(request.parse_mode) else "deterministic")
            batch.model = str(capabilities.get("main_model") if _uses_llm(request.parse_mode) else "none")
            batch.evidence = []
            batch.candidates = manual
            batch.conflicts = []
            batch.candidate_mapping_ir = None
            batch.standard_input_candidate = None
            batch.completeness_report = {}
            batch.parameter_advisories = []
            batch.active_parameter_readiness = {}
            batch.unsupported_candidates = []
            batch.llm_partial_failure = None
            batch.llm_retry_groups = []
            batch.readiness = {
                "preview_allowed": False,
                "compute_allowed": False,
                "technical_blockers": [],
                "pending_confirmation_keys": [],
            }
            batch.resolved_parameters = {item.key: item.normalized_value for item in manual}
            for item in batch.files:
                item.status = "queued"
                item.progress = 0
                item.error = None
            batch.status = "queued"
            batch.progress = 0
            self._save(batch)
        self._executor.submit(self._process_batch, batch_id)
        return self.get_batch(batch_id)

    def retry(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            failed = [item for item in batch.files if item.status == "failed"]
            if not failed:
                interrupted = any(
                    item.get("code") == "PROCESS_INTERRUPTED"
                    and item.get("retryable")
                    for item in batch.diagnostics
                )
                if batch.status != "failed" or not interrupted:
                    return batch.model_dump()
                batch.status = "queued"
                batch.progress = 0
                self._save(batch)
                retry_ids = None
            else:
                for item in failed:
                    item.status = "queued"
                    item.progress = 0
                    item.error = None
                batch.status = "queued"
                batch.updated_at = _now()
                self._save(batch)
                retry_ids = {item.file_id for item in failed}
        self._executor.submit(self._process_batch, batch_id, retry_ids)
        return self.get_batch(batch_id)

    def confirm(
        self,
        batch_id: str,
        request: ConfirmBatchReviewRequest | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            if batch.status == "confirmed":
                return self._confirmed_project_response(batch)
            if request is not None:
                self._apply_final_review(batch, request)
            elif batch.engineering_review.get("status") != "confirmed":
                raise ValueError("请先完成工程参数审查并提交确认。")
            if batch.fem_review.get("status") != "confirmed":
                raise ValueError("请先确认 FEM 建模参数。")
            if batch.capability_id == L2_CAPABILITY_ID:
                return self._confirm_l2_batch(batch_id, batch, request)
            if not batch.readiness.get("preview_allowed") or not batch.standard_input_candidate:
                blockers = "；".join(str(item) for item in batch.readiness.get("technical_blockers") or [])
                raise ValueError(f"当前参数无法构造预览模型：{blockers or '缺少有效候选模型'}")
            # The compatibility field stays empty for v2 batches. Technical
            # feasibility is represented exclusively by readiness.
            batch.completeness_report = {}
            for conflict in batch.conflicts:
                conflict.blocking = False
            unresolved = [item for item in batch.conflicts if item.blocking and item.resolved_value is None]
            if unresolved:
                raise ValueError("仍有关键参数冲突需要确认")
            package = deepcopy(batch.standard_input_candidate)
            validation = validate_standard_input({
                "standard_input_package": package,
                "fem_params": package.get("fem_settings") or {},
            })
            if validation.get("blocked"):
                raise ValueError("候选 StandardInputPackage 未通过确定性校验")
            source_name = f"{batch.batch_id}-standard-input.json"
            content = json.dumps({"standard_input_package": package}, ensure_ascii=False, indent=2).encode("utf-8")
            imported = _import_from_package(source_name, package, validation)
        project = create_project(source_name, content, modeling_goal=batch.modeling_goal, imported=imported)
        if project.get("validation_report", {}).get("blocked") or not project.get("ir"):
            raise ValueError("基础工程参数未通过风塔建模校验")
        project_id = str(project["project_id"])
        state = get_state(project_id)
        parameter_set = parameter_set_from_ir(project_id, state.ir)
        catalog_keys = set(parameter_set.parameters)
        effective_values = batch.resolved_parameters
        parameter_changes = [
            ParameterChange(key=key, value=value)
            for key, value in effective_values.items()
            if key in catalog_keys and value is not None
        ]
        if parameter_changes:
            parameter_set, _, parameter_validation = apply_changes(
                parameter_set,
                parameter_changes,
                increment_revision=False,
            )
            if parameter_validation["status"] != "pass":
                messages = "；".join(
                    str(item.get("message") or item)
                    for item in parameter_validation["issues"]
                )
                raise ValueError(f"参数集未通过门禁：{messages}")
        parameter_set.confirmed_at = _now()
        parameter_set.confirmed_by = str(
            (request.operator if request is not None else batch.engineering_review.get("operator"))
            or "engineer"
        )
        parameter_set.capability_id = CAPABILITY_ID
        reference_summary = (batch.reference_model or {}).get("summary") or {}
        parameter_set.reference_model_id = reference_summary.get("reference_model_id")
        parameter_set.reference_model_compatibility = cast(
            Any,
            str(reference_summary.get("compatibility") or "not_provided"),
        )
        fem_parameters = batch.fem_specification.get("parameters") or {}
        for key, specification in fem_parameters.items():
            record = parameter_set.parameters.get(key)
            if record is None:
                continue
            record.domain = "fem"
            origin = str(specification.get("origin") or "capability_default")
            record.origin = cast(
                Any,
                "fem_evidence" if origin == "fem_file" else origin,
            )
            record.confirmation_status = "confirmed"
            record.requires_confirmation = False
            if parameter_set.reference_model_id and record.origin == "reference_model":
                record.sources = [SourceReference(
                    filename=str(reference_summary.get("filename") or "reference.inp"),
                    locator="Abaqus keyword summary",
                    extraction_method="deterministic_inp_parser",
                    source_type="reference_inp",
                )]
            elif record.origin == "model_library":
                record.sources = [SourceReference(
                    filename=str((batch.selected_library_model or {}).get("label") or "validated model"),
                    locator=str((batch.selected_library_model or {}).get("model_id") or "model_library"),
                    extraction_method="validated_model_profile",
                    source_type="model_library",
                )]
            elif record.origin == "fem_evidence":
                source_file_id = str(specification.get("source_file_id") or "")
                source_file = next(
                    (item for item in batch.files if item.file_id == source_file_id),
                    None,
                )
                record.sources = [SourceReference(
                    filename=source_file.filename if source_file else "fem-input",
                    locator="FEM 参数区域",
                    extraction_method="deterministic_fem_file_router",
                    evidence_ids=list(specification.get("evidence_ids") or []),
                    source_type="fem_file",
                )]
        fem_keys = set(fem_parameters)
        for key, value in effective_values.items():
            record = parameter_set.parameters.get(key)
            if record is None or key in fem_keys:
                continue
            matches = sorted(
                (
                    candidate
                    for candidate in batch.candidates
                    if candidate.key == key
                    and _same_value(candidate.normalized_value, value)
                ),
                key=_candidate_rank,
                reverse=True,
            )
            if not matches:
                continue
            candidate = matches[0]
            origin_map = {
                "reference_model_value": "reference_model",
                "model_library_reference": "model_library",
                "llm_mapped": "llm_suggestion",
                "llm_suggested": "llm_suggestion",
                "engineer_input": "engineer_input",
            }
            record.origin = cast(
                Any,
                origin_map.get(candidate.source_kind, "engineering_evidence"),
            )
            if candidate.source_kind == "reference_model_value":
                record.sources = [SourceReference(
                    filename=str(reference_summary.get("filename") or "reference.inp"),
                    locator=candidate.assumption_basis,
                    extraction_method="deterministic_reference_inp",
                    source_type="reference_inp",
                )]
            elif candidate.source_kind == "model_library_reference":
                record.sources = [SourceReference(
                    filename=str((batch.selected_library_model or {}).get("label") or "validated model"),
                    locator=str((batch.selected_library_model or {}).get("model_id") or candidate.source_file_id),
                    extraction_method="validated_model_profile",
                    source_type="model_library",
                )]
            elif candidate.source_kind == "engineer_input":
                record.sources = [SourceReference(
                    filename="工程师参数审查",
                    locator="统一参数确认",
                    extraction_method="engineer_review",
                    source_type="engineer",
                )]
        state.ir = apply_parameter_set_to_ir(parameter_set, state.ir)
        write_json(state.root / "parameter_set.json", parameter_set.model_dump(mode="json"))
        audit = {
            "schema_version": "engineering_input_batch_v2",
            "batch_id": batch.batch_id,
            "template_id": batch.selected_template_id,
            "template_version": batch.selected_template_version,
            "prompt_hash": batch.prompt_hash,
            "source_files": [item.filename for item in batch.files],
            "candidates": [item.model_dump() for item in batch.candidates],
            "conflicts": [item.model_dump() for item in batch.conflicts],
            "usage": batch.usage,
            "engineering_llm_usage": batch.engineering_llm_usage,
            "fem_llm_usage": batch.fem_llm_usage,
            "engineering_channel": batch.engineering_channel,
            "fem_channel": batch.fem_channel,
            "selected_library_model": batch.selected_library_model,
            "intent_contract": batch.intent_contract,
            "unsupported_candidates": batch.unsupported_candidates,
            "llm_partial_failure": batch.llm_partial_failure,
            "final_review": batch.final_review,
            "fem_review": batch.fem_review,
            "capability_match": batch.capability_match,
            "reference_model": reference_summary,
        }
        state.ir["engineering_input_batch"] = audit
        state.ir["modeling_intent_contract"] = deepcopy(batch.intent_contract)
        state.ir["unsupported_candidates"] = deepcopy(batch.unsupported_candidates)
        state.ir["parameter_evidence_map"] = self._parameter_evidence_map(
            batch,
            effective_values,
        )
        pending_keys = [] if request is not None else list(
            batch.readiness.get("pending_confirmation_keys") or []
        )
        state.ir["engineering_review"] = {
            "schema_version": "engineering_parameter_review_v1",
            "status": "pending" if pending_keys else "confirmed",
            "template_id": batch.selected_template_id,
            "template_version": batch.selected_template_version,
            "prompt_hash": batch.prompt_hash,
            "pending_parameters": [
                {
                    "key": key,
                    "label": _parameter_label(key),
                    "reason": "参数来自LLM建议、OCR/图纸识别或未确认冲突",
                }
                for key in pending_keys
            ],
            "confirmations": [],
        }
        state.ir["fem_review"] = deepcopy(batch.fem_review)
        state.ir["modeling_capability"] = current_capability()
        state.ir["reference_model_summary"] = deepcopy(reference_summary) or None
        if batch.reference_model:
            source = self.root / batch.batch_id / "reference" / str(
                batch.reference_model.get("stored_name") or ""
            )
            if source.is_file():
                target_root = state.root / "input"
                target_root.mkdir(parents=True, exist_ok=True)
                target = target_root / "reference_model.inp"
                shutil.copyfile(source, target)
                state.artifacts["input/reference_model.inp"] = target
        state.ir.setdefault("project_meta_ir", {})["source_files"] = audit["source_files"]
        state.ir.setdefault("raw_excel_ir", {})["source_files"] = audit["source_files"]
        write_json(state.root / "input_batch_audit.json", audit)
        write_json(state.root / "ir.json", state.ir)
        with self._lock:
            batch = self._get(batch_id)
            batch.status = "confirmed"
            batch.project_id = project_id
            batch.updated_at = _now()
            self._save(batch)
        response = self.pipeline.project(project_id)
        response["input_batch"] = batch.model_dump()
        return response

    def _confirm_l2_batch(
        self,
        batch_id: str,
        batch: InputBatch,
        request: ConfirmBatchReviewRequest | None,
    ) -> dict[str, Any]:
        specification = batch.fem_specification.get("parameters") or {}
        values = {
            key: item.get("value")
            for key, item in specification.items()
            if isinstance(item, dict) and item.get("value") is not None
        }
        operator = str(
            (request.operator if request is not None else batch.engineering_review.get("operator"))
            or "engineer"
        )
        response = create_l2_project(
            str(batch.modeling_goal or "局部单法兰螺栓接触预紧模型"),
            values,
            source_name=f"{batch.batch_id}-l2-intent.json",
            operator=operator,
        )
        project_id = str(response["project_id"])
        state = get_state(project_id)
        audit = batch.model_dump(mode="json")
        state.ir["engineering_input_batch"] = audit
        state.ir["engineering_review"] = deepcopy(batch.engineering_review)
        state.ir["fem_review"] = deepcopy(batch.fem_review)
        write_json(state.root / "input_batch_audit.json", audit)
        write_json(state.root / "ir.json", state.ir)
        batch.status = "confirmed"
        batch.project_id = project_id
        batch.updated_at = _now()
        self._save(batch)
        response = self.pipeline.project(project_id)
        response["input_batch"] = batch.model_dump()
        return response

    def _confirmed_project_response(self, batch: InputBatch) -> dict[str, Any]:
        if not batch.project_id:
            raise ValueError("批次已确认，但缺少项目标识；请从恢复快照检查该批次。")
        response = self.pipeline.project(batch.project_id)
        response["input_batch"] = batch.model_dump()
        return response

    def confirm_engineering_review(
        self,
        batch_id: str,
        request: ConfirmBatchReviewRequest,
    ) -> dict[str, Any]:
        with self._lock:
            batch = self._get(batch_id)
            if batch.status == "confirmed":
                return batch.model_dump()
            if self._engineering_review_is_replay(batch, request):
                return batch.model_dump()
            self._apply_final_review(batch, request)
            self._refresh_status(batch)
            self._save(batch)
        return self.get_batch(batch_id)

    @staticmethod
    def _engineering_review_is_replay(
        batch: InputBatch,
        request: ConfirmBatchReviewRequest,
    ) -> bool:
        if (
            batch.engineering_review.get("status") != "confirmed"
            or request.review_revision != batch.review_revision
        ):
            return False
        submitted = {item.key: item for item in request.parameters}
        if len(submitted) != len(request.parameters):
            return False
        rows = {str(item.get("key")): item for item in batch.review_parameters}
        if set(submitted) != set(rows):
            return False
        for key, row in rows.items():
            try:
                value = _coerce_review_value(row, submitted[key].value)
                if not key.startswith("geometry.tower_sections."):
                    value, _ = normalize_parameter_value(key, value)
            except (TypeError, ValueError):
                return False
            if not _same_value(row.get("value"), value):
                return False
        return True

    def _apply_final_review(
        self,
        batch: InputBatch,
        request: ConfirmBatchReviewRequest,
    ) -> None:
        if request.review_revision != batch.review_revision:
            raise ValueError("参数确认页已经更新，请刷新后重新确认")
        submitted = {item.key: item for item in request.parameters}
        if len(submitted) != len(request.parameters):
            raise ValueError("确认参数中存在重复 key")
        expected = {str(item.get("key")) for item in batch.review_parameters}
        missing = expected - set(submitted)
        unknown = set(submitted) - expected
        if missing or unknown:
            details = []
            if missing:
                details.append(f"缺少：{', '.join(sorted(missing))}")
            if unknown:
                details.append(f"未知：{', '.join(sorted(unknown))}")
            raise ValueError("确认参数集合与当前模型不一致；" + "；".join(details))

        section_values = deepcopy(
            batch.model_composition.get("effective_parameters", {}).get(
                "geometry.tower_sections"
            )
            or batch.resolved_parameters.get("geometry.tower_sections")
            or []
        )
        scalar_values: dict[str, Any] = {}
        for row in batch.review_parameters:
            key = str(row["key"])
            raw = submitted[key].value
            value = _coerce_review_value(row, raw)
            if key.startswith("geometry.tower_sections."):
                parts = key.split(".")
                index, field = int(parts[2]), parts[3]
                if index >= len(section_values):
                    raise ValueError(f"塔筒分段索引无效：{key}")
                section_values[index][field] = value
            else:
                value, _ = normalize_parameter_value(key, value)
                scalar_values[key] = value
        scalar_values["geometry.tower_sections"] = section_values

        for key, value in scalar_values.items():
            matching = sorted(
                (
                    item
                    for item in batch.candidates
                    if item.key == key and _same_value(item.normalized_value, value)
                ),
                key=_candidate_rank,
                reverse=True,
            )
            if matching:
                matching[0].requires_confirmation = False
                matching[0].confirmation_status = "confirmed"
                continue
            batch.candidates = [
                item for item in batch.candidates
                if not (item.key == key and item.extraction_method == "final_review")
            ]
            batch.candidates.append(ParameterCandidate(
                candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
                key=key,
                label=_parameter_label(key),
                raw_value=value,
                raw_unit=submitted[key].unit if key in submitted else "mm",
                normalized_value=value,
                normalized_unit=submitted[key].unit if key in submitted else "mm",
                source_file_id="engineer-final-review",
                evidence_ids=[],
                extraction_method="final_review",
                provider="engineer",
                model="none",
                confidence=1.0,
                priority="P0",
                requires_confirmation=False,
                source_kind="engineer_input",
                confirmation_status="confirmed",
                template_id=batch.selected_template_id,
                prompt_hash=batch.prompt_hash,
                confirmation_reason="工程师一次性确认全部活动参数",
            ))
        self._merge_candidates(batch)

        tasks = []
        if scalar_values.get("analysis.static_enabled"):
            tasks.append("static")
        if scalar_values.get("analysis.modal_enabled"):
            tasks.append("modal")
        batch.intent_contract["analysis_tasks"] = tasks
        requested = set(batch.intent_contract.get("requested_features") or [])
        if scalar_values.get("geometry.door.enabled"):
            requested.add("door_opening")
        else:
            requested.discard("door_opening")
            requested.discard("local_refinement")
        batch.intent_contract["requested_features"] = sorted(requested | {"tower_shell"})
        batch.intent_contract["confirmation_status"] = "confirmed"
        batch.intent_contract["source"] = "engineer_final_review"
        for item in batch.candidates:
            if item.key in scalar_values:
                item.requires_confirmation = False
                item.confirmation_status = "confirmed"
        for conflict in batch.conflicts:
            if conflict.key in scalar_values:
                conflict.resolved_value = scalar_values[conflict.key]
                conflict.confirmation_status = "confirmed"
        batch.engineering_review = {
            "status": "confirmed",
            "confirmed_at": _now(),
            "operator": request.operator,
            "confirmed_parameter_keys": sorted(expected),
        }
        self._build_readiness(batch)
        if not batch.readiness.get("preview_allowed"):
            raise ValueError("确认参数未通过预览模型技术校验")
        engineering_blockers = [
            item
            for item in batch.readiness.get("compute_blockers") or []
            if item != "FEM 建模参数尚未由工程师确认"
        ]
        if engineering_blockers:
            raise ValueError(
                "确认参数仍不满足计算要求："
                + "；".join(engineering_blockers)
            )
        batch.final_review = {
            "schema_version": "batch_final_parameter_review_v1",
            "review_revision": request.review_revision,
            "operator": request.operator,
            "confirmed_at": _now(),
            "confirmed_parameter_keys": sorted(expected),
        }

    def _process_batch(self, batch_id: str, only: set[str] | None = None) -> None:
        with self._lock:
            batch = self._get(batch_id)
            batch.status = "processing"
            self._save(batch)
        for file_index, file in enumerate(list(batch.files)):
            if only is not None and file.file_id not in only:
                continue
            try:
                self._process_file(batch_id, file.file_id)
            except Exception as exc:
                with self._lock:
                    current = self._get(batch_id)
                    target = next(item for item in current.files if item.file_id == file.file_id)
                    target.status = "failed"
                    target.error = str(exc)
                    target.progress = 100
                    current.warnings.append(f"{target.filename}: {exc}")
                    self._save(current)
            with self._lock:
                current = self._get(batch_id)
                completed = sum(1 for item in current.files if item.status in {"completed", "needs_confirmation", "failed"})
                current.progress = round(completed / len(current.files) * 85)
                self._save(current)
        with self._lock:
            batch = self._get(batch_id)
            if batch.parse_mode == "llm_full":
                # File conversion is allowed to build evidence, but its
                # deterministic field mappings must not enter the pure-LLM arm.
                batch.candidates = [
                    item for item in batch.candidates
                    if item.extraction_method == "manual"
                ]
                batch.resolved_parameters = {
                    item.key: item.normalized_value for item in batch.candidates
                }
            if not batch.selected_template_id:
                # Intent selection must see the deterministic baseline.
                # Otherwise an explicit feature in a structured source can be
                # lost before the user confirms the model composition.
                self._ensure_goal_evidence_and_candidates(batch)
                self._merge_candidates(batch)
                batch.template_recommendation = self.templates.recommend(
                    batch.modeling_goal,
                    [item.filename for item in batch.files],
                )
                batch.template_recommendation["requires_confirmation"] = False
                recommended_id = str(
                    batch.template_recommendation["recommended_template_id"]
                )
                recommended = self.templates.get(recommended_id)
                batch.selected_template_id = recommended_id
                batch.selected_template_version = str(recommended["version"])
                batch.prompt_hash = str(recommended["prompt_hash"])
                batch.intent_contract = _infer_modeling_intent(
                    batch.modeling_goal,
                    batch.resolved_parameters,
                    template_id=recommended_id,
                    confirmed=True,
                )
                batch.intent_contract["source"] = "automatic_template_and_goal_inference"
            template = self.templates.get(batch.selected_template_id, batch.selected_template_version)
            self._ensure_goal_evidence_and_candidates(batch)
            self._merge_candidates(batch)
            self._route_fem_file_candidates(batch)
            self._sync_reference_engineering_candidates(batch)
            if not batch.intent_contract:
                batch.intent_contract = _infer_modeling_intent(
                    batch.modeling_goal,
                    batch.resolved_parameters,
                    template_id=batch.selected_template_id,
                )
            self._update_composition(batch)
            if _uses_llm(batch.parse_mode):
                if batch.parse_mode == "llm_full":
                    target_keys = [
                        str(item.get("key"))
                        for item in template.get("parameters") or []
                        if item.get("key")
                    ]
                else:
                    target_keys = [
                        str(item.get("key"))
                        for item in batch.model_composition.get("unresolved_parameters") or []
                        if item.get("key")
                        and not str(item.get("key")).startswith(
                            ("mesh.", "analysis.", "boundary.")
                        )
                    ]
                batch.llm_retry_groups = sorted({
                    key.split(".", 1)[0] for key in target_keys
                })
                if target_keys:
                    relevant_evidence = self._relevant_evidence(batch, target_keys)
                    if batch.parse_mode == "llm_full":
                        engineering_keys = [
                            key for key in target_keys
                            if not key.startswith(("mesh.", "analysis.", "boundary."))
                        ]
                        fem_keys = [
                            key for key in target_keys
                            if key.startswith(("mesh.", "analysis.", "boundary."))
                        ]
                        engineering_response = self._map_advisor_evidence(
                            self.engineering_advisor, batch, relevant_evidence,
                            self.templates.mapping_prompt(template), engineering_keys,
                        )
                        fem_response = self._map_advisor_evidence(
                            self.fem_advisor, batch, relevant_evidence,
                            self.templates.mapping_prompt(template), fem_keys,
                        )
                        responses = [engineering_response, fem_response]
                        successful = [item for item in responses if item.get("status") == "called"]
                        llm_response = {
                            "status": "called" if len(successful) == 2 else "failed",
                            "fields": [
                                field for item in successful
                                for field in item.get("fields") or []
                            ],
                            "request_count": sum(
                                int(item.get("request_count") or 0) for item in responses
                            ),
                            "provider": successful[0].get("provider") if successful else batch.provider,
                            "model": successful[0].get("model") if successful else batch.model,
                            "error": None if len(successful) == 2 else "工程与FEM顾问未全部成功返回",
                            "advisor_status": {
                                "engineering": engineering_response.get("status"),
                                "fem": fem_response.get("status"),
                            },
                        }
                    else:
                        llm_response = self._map_engineering_evidence(
                            batch,
                            relevant_evidence,
                            self.templates.mapping_prompt(template),
                            target_keys,
                        )
                    if llm_response.get("status") != "called":
                        technical_error = str(
                            llm_response.get("error") or "模型未返回结果"
                        )
                        message = "AI 辅助暂不可用，已保留本地确定性解析结果。"
                        batch.warnings = [
                            item for item in batch.warnings
                            if not item.startswith(("LLM 参数映射失败：", "LLM 参数补充失败："))
                            and item != message
                        ]
                        batch.warnings.append(message)
                        batch.llm_partial_failure = {
                            "status": "failed",
                            "message": message,
                            "technical_error": technical_error,
                            "target_keys": target_keys,
                            "execution_mode": batch.llm_execution_mode,
                            "local_parameters_preserved": len(batch.resolved_parameters),
                            "retryable": True,
                        }
                    else:
                        batch.llm_partial_failure = None
                        batch.llm_retry_groups = []
                        batch.warnings = [
                            item for item in batch.warnings
                            if not item.startswith(("LLM 参数映射失败：", "LLM 参数补充失败："))
                        ]
                        batch.candidate_mapping_ir = {
                            "schema_version": "candidate_mapping_ir_v2",
                            "template_id": batch.selected_template_id,
                            "template_version": batch.selected_template_version,
                            "prompt_hash": batch.prompt_hash,
                            "target_keys": target_keys,
                            "fields": llm_response.get("fields") or [],
                            "execution_mode": batch.llm_execution_mode,
                            "request_count": int(llm_response.get("request_count") or 1),
                        }
                        batch.candidates = [
                            item for item in batch.candidates
                            if item.extraction_method != "llm_mapping"
                        ]
                        batch.candidates.extend(self._llm_batch_candidates(batch, llm_response))
                else:
                    batch.llm_partial_failure = None
                    batch.llm_retry_groups = []
                    batch.candidate_mapping_ir = {
                        "schema_version": "candidate_mapping_ir_v2",
                        "template_id": batch.selected_template_id,
                        "template_version": batch.selected_template_version,
                        "prompt_hash": batch.prompt_hash,
                        "target_keys": [],
                        "fields": [],
                        "status": "not_needed_all_active_parameters_resolved_locally",
                    }
            self._merge_candidates(batch)
            if batch.parse_mode == "llm_full":
                self._route_fem_file_candidates(batch)
            self._update_composition(batch)
            batch.engineering_llm_usage = self.engineering_advisor.usage()
            batch.fem_llm_usage = self.fem_advisor.usage()
            batch.usage = {
                "engineering": batch.engineering_llm_usage,
                "fem": batch.fem_llm_usage,
            }
            batch.progress = 100
            self._build_readiness(batch)
            self._refresh_status(batch)
            if batch.llm_partial_failure and batch.readiness.get("preview_allowed"):
                batch.status = "completed_with_llm_warning"
            self._save(batch)
            append_audit_event(
                self.root / batch.batch_id / "audit" / "events.jsonl",
                "input_batch_processed",
                {
                    "batch_id": batch.batch_id,
                    "status": batch.status,
                    "parse_mode": batch.parse_mode,
                    "provider": batch.provider,
                    "model": batch.model,
                    "template_version": batch.selected_template_version,
                    "prompt_hash": batch.prompt_hash,
                    "usage": batch.usage,
                    "llm_execution_mode": batch.llm_execution_mode,
                    "file_count": len(batch.files),
                    "evidence_count": len(batch.evidence),
                    "candidate_count": len(batch.candidates),
                    "llm_partial_failure": batch.llm_partial_failure,
                },
            )

    def _update_composition(self, batch: InputBatch) -> None:
        self._ensure_fem_state(batch)
        if batch.capability_id == L2_CAPABILITY_ID:
            values = {
                key: item.get("value")
                for key, item in (batch.fem_specification.get("parameters") or {}).items()
                if isinstance(item, dict)
            }
            missing = [key for key, value in values.items() if value is None]
            batch.intent_contract = {
                "schema_version": "modeling_intent_v1",
                "model_family": "wind_tower_local_bolted_flange",
                "analysis_tasks": ["nonlinear_static"],
                "requested_features": ["single_flange", "surface_contact", "b31_bolts", "pretension"],
                "confirmation_status": "confirmed",
                "capability_id": L2_CAPABILITY_ID,
            }
            batch.model_composition = {
                "effective_parameters": values,
                "active_parameter_keys": sorted(values),
                "parameter_states": [
                    {"key": key, "activation_status": "active", "value_status": "missing" if value is None else "resolved"}
                    for key, value in sorted(values.items())
                ],
                "derived_parameters": [], "defaulted_parameters": [],
                "unresolved_parameters": [{"key": key, "reason": "missing_required_parameter"} for key in missing],
                "degradations": [],
                "preview_blockers": [{"key": key, "reason": f"缺少参数：{key}"} for key in missing],
                "compute_blockers": [{"key": key, "reason": f"缺少参数：{key}"} for key in missing],
                "active_modules": ["tower_segments", "single_flange", "bolt_group", "contact", "pretension"],
                "module_manifests": [],
            }
            return
        if not batch.intent_contract:
            goal = (batch.modeling_goal or "").lower()
            tasks = []
            if "模态" in goal or "modal" in goal or "frequency" in goal:
                tasks.append("modal")
            if "静力" in goal or "static" in goal or not tasks:
                tasks.append("static")
            door_enabled = bool(
                batch.resolved_parameters.get("geometry.door.enabled", True)
            )
            batch.intent_contract = {
                "schema_version": "modeling_intent_v1",
                "model_family": "wind_tower",
                "analysis_tasks": tasks,
                "requested_features": [
                    "tower_shell",
                    *(["door_opening"] if door_enabled else []),
                ],
                "confirmation_status": "pending",
                "template_id": batch.selected_template_id,
            }
        composition_values = dict(batch.resolved_parameters)
        for key, item in (batch.fem_specification.get("parameters") or {}).items():
            if isinstance(item, dict) and item.get("value") is not None:
                composition_values[key] = item["value"]
        analysis_tasks = set(batch.intent_contract.get("analysis_tasks") or [])
        composition_values.setdefault(
            "analysis.static_enabled", "static" in analysis_tasks
        )
        composition_values.setdefault(
            "analysis.modal_enabled", "modal" in analysis_tasks
        )
        requested_features = set(batch.intent_contract.get("requested_features") or [])
        if "door_opening" not in requested_features:
            composition_values["geometry.door.enabled"] = False
        template = self.templates.get(
            batch.selected_template_id or "wind_tower_quick_v1",
            batch.selected_template_version,
        )
        active_keys = [
            str(item["key"])
            for item in template.get("parameters") or []
            if not (
                str(item["key"]).startswith("geometry.door.")
                and item["key"] != "geometry.door.enabled"
                and not composition_values.get("geometry.door.enabled", False)
            )
        ]
        if float(composition_values.get("loads.top_mass_tonne") or 0) > 0:
            if "loads.center_of_mass_offset_mm" not in active_keys:
                active_keys.append("loads.center_of_mass_offset_mm")
        required = {
            "geometry.tower_sections",
            "material.name",
            "material.elastic_modulus_mpa",
            "material.poisson_ratio",
            "material.density",
            "mesh.global_size_mm",
            "boundary.name",
        }
        if composition_values.get("loads.wind_direction_vector") is None:
            required.add("loads.top_force_direction")
        if composition_values.get("geometry.door.enabled"):
            required.update(
                {
                    "geometry.door.width_mm",
                    "geometry.door.height_mm",
                    "geometry.door.bottom_z_mm",
                }
            )
        if float(composition_values.get("loads.top_mass_tonne") or 0) > 0:
            required.add("loads.center_of_mass_offset_mm")
        unresolved = [
            {"key": key, "reason": "missing_required_parameter"}
            for key in sorted(required)
            if composition_values.get(key) is None
        ]
        evidence_by_id = {item.evidence_id: item for item in batch.evidence}
        parameter_states = []
        evidence_blockers = []
        for key in active_keys:
            matching = [
                item for item in batch.candidates
                if item.key == key and _same_value(item.normalized_value, composition_values.get(key))
            ]
            chosen = sorted(matching, key=_candidate_rank, reverse=True)[0] if matching else None
            file_origin = bool(chosen and chosen.source_kind in {
                "file_observed", "llm_mapped", "goal_explicit", "fem_supplement",
            })
            linked = [evidence_by_id[value] for value in (chosen.evidence_ids if chosen else []) if value in evidence_by_id]
            conflict = any(item.status == "conflict" for item in linked)
            evidence_status = "conflict" if conflict else (
                "linked" if linked else ("missing" if file_origin else "non_file_origin")
            )
            parameter_states.append({
                "key": key,
                "activation_status": "active",
                "value_status": "resolved" if composition_values.get(key) is not None else "missing",
                "evidence_status": evidence_status,
                "evidence_ids": [item.evidence_id for item in linked],
            })
            if evidence_status in {"missing", "conflict"}:
                evidence_blockers.append({
                    "key": key,
                    "reason": "活动文件参数缺少有效Evidence ID" if evidence_status == "missing" else "活动参数证据存在解析冲突",
                })
        blockers = [
            {
                "key": item["key"],
                "reason": f"缺少参数：{item['key']}",
            }
            for item in unresolved
        ] + evidence_blockers
        batch.model_composition = {
            "effective_parameters": composition_values,
            "active_parameter_keys": active_keys,
            "parameter_states": parameter_states,
            "derived_parameters": [],
            "defaulted_parameters": [],
            "unresolved_parameters": unresolved,
            "degradations": [],
            "preview_blockers": blockers,
            "compute_blockers": blockers,
            "active_modules": ["tower_shell", *(
                ["door_opening"]
                if composition_values.get("geometry.door.enabled")
                else []
            )],
            "module_manifests": [],
        }
        batch.active_parameter_readiness = {
            "schema_version": "active_parameter_readiness_v1",
            "active_parameter_keys": batch.model_composition.get("active_parameter_keys") or [],
            "parameter_states": batch.model_composition.get("parameter_states") or [],
            "derived_parameters": batch.model_composition.get("derived_parameters") or [],
            "defaulted_parameters": batch.model_composition.get("defaulted_parameters") or [],
            "unresolved_parameters": batch.model_composition.get("unresolved_parameters") or [],
            "degradations": batch.model_composition.get("degradations") or [],
        }

    def _route_fem_file_candidates(self, batch: InputBatch) -> None:
        fem_prefixes = ("mesh.", "analysis.", "boundary.")
        parameters = batch.fem_specification.setdefault("parameters", {})
        # A goal/file candidate may only enter the FEM review when the selected
        # capability declares that key.  This prevents L1 switches such as
        # analysis.static_enabled from leaking into the fixed three-step L2
        # contract (and later appearing as an unmodifiable parameter).
        capability_keys = set(parameters)
        changed = False
        keys = {
            item.key
            for item in batch.candidates
            if item.key.startswith(fem_prefixes) and item.key in capability_keys
        }
        for key in sorted(keys):
            matching = [
                item
                for item in batch.candidates
                if item.key == key
                and item.source_kind in {"file_observed", "goal_explicit", "llm_mapped"}
                and item.normalized_value is not None
            ]
            if not matching:
                continue
            selected = sorted(matching, key=_candidate_rank, reverse=True)[0]
            current = parameters.get(key)
            if (
                isinstance(current, dict)
                and current.get("value") == selected.normalized_value
                and current.get("origin") == "fem_file"
            ):
                continue
            alternatives = []
            if isinstance(current, dict) and current.get("value") != selected.normalized_value:
                alternatives.append(deepcopy(current))
            parameters[key] = {
                "value": selected.normalized_value,
                "origin": "llm_mapped" if selected.source_kind == "llm_mapped" else "fem_file",
                "confirmation_status": "pending",
                "requires_confirmation": True,
                "source_file_id": selected.source_file_id,
                "evidence_ids": list(selected.evidence_ids),
                "alternatives": alternatives,
                "sources": [{
                    "branch": "source_data",
                    "origin": "llm_mapped" if selected.source_kind == "llm_mapped" else "fem_file",
                    "source_file_id": selected.source_file_id,
                    "evidence_ids": list(selected.evidence_ids),
                }],
            }
            changed = True
        if changed:
            batch.fem_specification["status"] = "candidate"
            batch.fem_specification["requires_engineer_confirmation"] = True
            batch.fem_review_revision += 1
            batch.fem_review = {
                "status": "pending",
                "confirmed_at": None,
                "operator": None,
            }

    def _relevant_evidence(
        self,
        batch: InputBatch,
        target_keys: list[str],
    ) -> list[EvidenceFragment]:
        return select_relevant_evidence(
            batch.evidence,
            target_keys,
            _parameter_label,
            self.root / batch.batch_id / "index" / "evidence.sqlite3",
            top_k_per_field=getattr(self.pipeline.settings, "evidence_top_k_per_field", 5),
            max_fragments=getattr(self.pipeline.settings, "evidence_max_fragments", 30),
            token_budget=getattr(self.pipeline.settings, "evidence_token_budget", 12000),
        )

    def _map_engineering_evidence(
        self,
        batch: InputBatch,
        evidence: list[EvidenceFragment],
        mapping_prompt: str,
        target_keys: list[str],
    ) -> dict[str, Any]:
        return self._map_advisor_evidence(
            self.engineering_advisor, batch, evidence, mapping_prompt, target_keys
        )

    def _map_advisor_evidence(
        self,
        advisor: EngineeringAdvisor | FemAdvisor,
        batch: InputBatch,
        evidence: list[EvidenceFragment],
        mapping_prompt: str,
        target_keys: list[str],
    ) -> dict[str, Any]:
        """Run one advisory request by default; split only when explicitly selected."""
        serialized = [item.model_dump() for item in evidence]
        catalog = build_catalog(batch.evidence, getattr(self.pipeline.settings, "evidence_catalog_token_budget", 1500))
        mapping_prompt = f"{mapping_prompt}\n\n{catalog}"
        if batch.llm_execution_mode == "single" or len(serialized) <= 8:
            result = advisor.map_parameters(
                serialized,
                batch.modeling_goal,
                mapping_prompt,
                target_keys,
            )
            result["request_count"] = 1 if serialized else 0
            result["execution_mode"] = "single"
            return result

        groups = [serialized[index:index + 8] for index in range(0, len(serialized), 8)]
        with ThreadPoolExecutor(max_workers=min(3, len(groups))) as executor:
            responses = list(executor.map(
                lambda group: advisor.map_parameters(
                    group,
                    batch.modeling_goal,
                    mapping_prompt,
                    target_keys,
                ),
                groups,
            ))
        successful = [item for item in responses if item.get("status") == "called"]
        fields: list[dict[str, Any]] = []
        seen: set[str] = set()
        for response in successful:
            raw_fields = response.get("fields") or []
            if isinstance(raw_fields, dict):
                raw_fields = [{"path": key, **(value if isinstance(value, dict) else {"value": value})} for key, value in raw_fields.items()]
            for field in raw_fields if isinstance(raw_fields, list) else []:
                if not isinstance(field, dict):
                    continue
                key = json.dumps(field, ensure_ascii=False, sort_keys=True, default=str)
                if key not in seen:
                    fields.append(field)
                    seen.add(key)
        return {
            "status": "called" if successful else "failed",
            "provider": successful[0].get("provider") if successful else batch.provider,
            "model": successful[0].get("model") if successful else batch.model,
            "schema_version": "candidate_mapping_ir_v1",
            "fields": fields,
            "request_count": len(groups),
            "execution_mode": "chunked_parallel",
            "partial_failures": [item.get("error") for item in responses if item.get("status") != "called"],
            "error": None if successful else "所有分块LLM请求均失败",
        }

    def _ensure_goal_evidence_and_candidates(self, batch: InputBatch) -> None:
        batch.evidence = [item for item in batch.evidence if item.file_id != "modeling-goal"]
        batch.candidates = [item for item in batch.candidates if item.source_file_id != "modeling-goal"]
        goal = (batch.modeling_goal or "").strip()
        if not goal:
            return
        evidence_id = f"goal-{hashlib.sha256(goal.encode('utf-8')).hexdigest()[:12]}"
        evidence = EvidenceFragment(
            evidence_id=evidence_id,
            file_id="modeling-goal",
            filename="建模目标",
            kind="text",
            text=goal,
            location={"json_path": "$.modeling_goal"},
            extraction_method="user_goal",
            confidence=1.0,
            content_hash=hashlib.sha256(goal.encode("utf-8")).hexdigest(),
        )
        batch.evidence.append(evidence)
        if batch.parse_mode == "llm_full":
            return
        lower = goal.lower()
        intents: list[tuple[str, str, bool]] = []
        if "静力" in goal or "static" in lower:
            disabled = any(token in goal for token in ("不进行静力", "无需静力", "关闭静力"))
            intents.append(("analysis.static_enabled", "静力分析", not disabled))
        if "模态" in goal or "modal" in lower or "frequency" in lower:
            disabled = any(token in goal for token in ("不进行模态", "无需模态", "关闭模态"))
            intents.append(("analysis.modal_enabled", "模态分析", not disabled))
        for key, label, value in intents:
            batch.candidates.append(ParameterCandidate(
                candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
                key=key,
                label=label,
                raw_value=value,
                normalized_value=value,
                source_file_id="modeling-goal",
                evidence_ids=[evidence_id],
                extraction_method="goal_intent",
                provider="user",
                model="none",
                confidence=1.0,
                requires_confirmation=False,
                source_kind="goal_explicit",
                confirmation_status="not_required",
                template_id=batch.selected_template_id,
                prompt_hash=batch.prompt_hash,
            ))

    def _process_file(self, batch_id: str, file_id: str) -> None:
        with self._lock:
            batch = self._get(batch_id)
            file = next(item for item in batch.files if item.file_id == file_id)
            file.status = "extracting"
            file.progress = 15
            self._save(batch)
        path = self._source_path(batch_id, file_id)
        content = path.read_bytes()
        docling_document: dict[str, Any] = {}
        docling_markdown = ""
        docling_meta: dict[str, Any] = {
            "service": self.docling.health(),
            "warnings": [],
        }
        parser_status = "degraded_native_only"
        docling_options = {"output": ["json", "markdown"], "preserve_provenance": True}
        docling_meta["options_hash"] = hashlib.sha256(
            json.dumps(docling_options, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        try:
            conversion = self.docling.convert(
                path,
                file.sha256,
                options=docling_options,
            )
            docling_document = conversion.document
            docling_markdown = conversion.markdown
            docling_meta.update(conversion.manifest)
            parser_status = "converted"
        except (DoclingUnavailable, DoclingArtifactError) as exc:
            docling_meta["warnings"].append(f"{type(exc).__name__}: {exc}")
        provider_caps = self.provider.capabilities()
        cache_variant = hashlib.sha256(json.dumps({
            "extractor": EXTRACTOR_VERSION,
            "parse_mode": batch.parse_mode,
            "provider": provider_caps.get("provider"),
            "ocr_model": provider_caps.get("ocr_model"),
            "vision_model": provider_caps.get("vision_model"),
            "configured": provider_caps.get("configured"),
        }, sort_keys=True).encode()).hexdigest()[:12]
        cache_path = self.cache_root / f"{file.sha256}-{cache_variant}.json"
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            fragments = [EvidenceFragment.model_validate(item) for item in payload["evidence"]]
            meta = payload["meta"]
            cache_hit = 1
        else:
            ocr = self.engineering_advisor.extract_image_text if _uses_llm(batch.parse_mode) else None
            fragments, meta = extract_file(file.file_id, file.filename, content, ocr)
            cache_path.write_text(json.dumps({"evidence": [item.model_dump() for item in fragments], "meta": meta}, ensure_ascii=False, indent=2), encoding="utf-8")
            cache_hit = 0
        fragments = [stabilize_fragment(item, file.sha256) for item in fragments]
        candidates = self._deterministic_candidates(file, fragments)
        if file.suffix in {".xls", ".xlsx", ".xlsm"}:
            normalization = self.pipeline.normalize_input_from_upload(file.filename, content, enable_llm=False, include_import=False)
            excel_candidates, excel_evidence = self._excel_candidates(file, normalization)
            candidates.extend(excel_candidates)
            fragments.extend(excel_evidence)
            package = normalization.get("standard_input_candidate") or normalization.get("standard_input_package")
            if isinstance(package, dict):
                package_candidates, package_evidence = self._package_candidates(file, package)
                candidates.extend(package_candidates)
                fragments.extend(package_evidence)
            normalized_path = self.root / batch_id / f"{file.file_id}-normalization.json"
            normalized_path.write_text(json.dumps(normalization, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        elif file.suffix == ".json":
            try:
                raw_json = json.loads(content.decode("utf-8-sig"))
                package = raw_json.get("standard_input_package") or raw_json.get("standard_input_candidate")
                if isinstance(package, dict):
                    package_candidates, package_evidence = self._package_candidates(file, package)
                    candidates.extend(package_candidates)
                    fragments.extend(package_evidence)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        docling_fragments = (
            fragments_from_docling(file, docling_document, docling_markdown)
            if parser_status == "converted"
            else []
        )
        fragments = reconcile_evidence([*docling_fragments, *fragments], docling_markdown, file.sha256)
        fragments = cross_validate_fragments(fragments)
        document_root = self.root / batch_id / "documents" / file_id
        manifest = write_document_artifacts(
            document_root=document_root,
            file=file,
            evidence=fragments,
            docling_json=docling_document,
            docling_markdown=docling_markdown,
            parser_status=parser_status,
            parser_meta={
                **docling_meta,
                **meta,
                "native_parser_version": EXTRACTOR_VERSION,
                "parse_mode": batch.parse_mode,
                "native_options_hash": cache_variant,
            },
            source_path=path,
        )
        if conversion_assets := getattr(locals().get("conversion"), "assets", None):
            for relative, asset in conversion_assets.items():
                name = Path(relative).name
                if name:
                    (document_root / "assets" / name).write_bytes(asset)
        with self._lock:
            batch = self._get(batch_id)
            target = next(item for item in batch.files if item.file_id == file_id)
            target.status = "mapping"
            target.progress = 65
            self._save(batch)
        with self._lock:
            batch = self._get(batch_id)
            batch.evidence = [item for item in batch.evidence if item.file_id != file_id] + fragments
            batch.candidates = [item for item in batch.candidates if item.source_file_id != file_id] + _dedupe_candidates(candidates)
            target = next(item for item in batch.files if item.file_id == file_id)
            target.evidence_count = len(fragments)
            target.candidate_count = len(candidates)
            target.ocr_pages = int(meta.get("ocr_pages") or 0)
            if file.suffix == ".pdf" and target.ocr_pages:
                target.support_tier = "exploratory"
            target.cache_hits = cache_hit
            document_requires_review = parser_status == "degraded_native_only" and file.suffix in DOCUMENT_SUFFIXES
            target.document_status = cast(Any, "needs_review" if document_requires_review else parser_status)
            target.document_manifest = str((document_root / "manifest.json").relative_to(self.root / batch_id))
            target.document_manifest_sha256 = sha256_file(document_root / "manifest.json")
            missing_ocr = any(item.requires_ocr and not item.from_ocr for item in fragments)
            useful_local_content = bool(docling_markdown.strip()) or any(
                item.text and not item.text.startswith("[") for item in fragments
            )
            needs = any(item.requires_confirmation for item in candidates) or document_requires_review
            if missing_ocr and not useful_local_content:
                target.status = "failed"
                target.error = "该文件需要 OCR 或视觉识别，但当前模型未配置或识别失败；配置后可单独重试。"
            else:
                target.status = "needs_confirmation" if needs else "completed"
                if missing_ocr:
                    target.error = "部分页面需要 OCR，但识别未完成；已保留其他可读取内容。"
                elif document_requires_review:
                    target.error = "Docling不可用；已保留本地提取结果，但文档类输入需要人工复核。"
            target.progress = 100
            self._save(batch)

    def _deterministic_candidates(self, file: BatchFile, fragments: list[EvidenceFragment]) -> list[ParameterCandidate]:
        result: list[ParameterCandidate] = []
        aliases = {
            "door.height_mm": ("geometry.door.height_mm", "门洞高度", "mm"),
            "door_opening.height_mm": ("geometry.door.height_mm", "门洞高度", "mm"),
            "door.width_mm": ("geometry.door.width_mm", "门洞宽度", "mm"),
            "door_opening.width_mm": ("geometry.door.width_mm", "门洞宽度", "mm"),
            "mesh.global_size_mm": ("mesh.global_size_mm", "全局网格尺寸", "mm"),
            "mesh.door_size_mm": ("mesh.door_size_mm", "门洞局部网格", "mm"),
            "material.elastic_modulus_mpa": ("material.elastic_modulus_mpa", "弹性模量", "MPa"),
            "material.poisson_ratio": ("material.poisson_ratio", "泊松比", None),
            "material.yield_strength_mpa": ("material.yield_strength_mpa", "屈服强度", "MPa"),
            "loads.wind_pressure_mpa": ("loads.wind_pressure_mpa", "风压", "MPa"),
            "loads.top_mass_tonne": ("loads.top_mass_tonne", "顶部等效质量", "tonne"),
            "loads.center_of_mass_offset_mm": ("loads.center_of_mass_offset_mm", "顶部质心偏心距", "mm"),
            "analysis.modal_modes": ("analysis.modal_modes", "模态阶数", None),
        }
        for fragment in fragments:
            if fragment.kind == "structured" and fragment.location.json_path:
                path = fragment.location.json_path.lower().replace("$.", "")
                for alias, (key, label, unit) in aliases.items():
                    if path.endswith(alias):
                        try:
                            value = json.loads(fragment.text)
                        except Exception:
                            value = fragment.text
                        result.append(self._candidate(file, key, label, value, unit, fragment, 0.98, "deterministic"))
            for change in _instruction_parameter_changes(fragment.text):
                label = change.key.rsplit(".", 1)[-1].replace("_", " ")
                result.append(self._candidate(file, change.key, label, change.value, None, fragment, 0.82, "local_pattern"))
        return result

    def _excel_candidates(self, file: BatchFile, normalization: dict[str, Any]) -> tuple[list[ParameterCandidate], list[EvidenceFragment]]:
        package = normalization.get("standard_input_candidate") or {}
        result = []
        evidence_fragments = []
        door = package.get("door_opening") or {}
        mapping = {
            "enabled": ("geometry.door.enabled", "启用门洞", None),
            "width_mm": ("geometry.door.width_mm", "门洞宽度", "mm"),
            "height_mm": ("geometry.door.height_mm", "门洞高度", "mm"),
            "z_bottom_mm": ("geometry.door.bottom_z_mm", "门洞底标高", "mm"),
            "theta_center_deg": ("geometry.door.theta_center_deg", "门洞环向角度", "deg"),
        }
        for source_key, (key, label, unit) in mapping.items():
            item = door.get(source_key)
            value = item.get("value") if isinstance(item, dict) else item
            if value is not None:
                evidence = self._excel_evidence(file, item, f"door_opening.{source_key}", value)
                evidence_fragments.append(evidence)
                result.append(self._candidate(file, key, label, value, unit, evidence, 1.0, "deterministic_excel"))
        materials = package.get("materials") or []
        if materials:
            material = materials[0]
            for source_key, key, label, unit in (
                ("elastic_modulus_mpa", "material.elastic_modulus_mpa", "弹性模量", "MPa"),
                ("poisson_ratio", "material.poisson_ratio", "泊松比", None),
                ("density_tonne_mm3", "material.density", "材料密度", "tonne/mm³"),
                ("yield_strength_mpa", "material.yield_strength_mpa", "屈服强度", "MPa"),
            ):
                value = material.get(source_key)
                if value is not None:
                    evidence = self._excel_evidence(file, {"source_cell_or_range": material.get("source")}, f"materials[0].{source_key}", value)
                    evidence_fragments.append(evidence)
                    result.append(self._candidate(file, key, label, value, unit, evidence, 1.0, "deterministic_excel"))
        fem = package.get("fem_settings") or {}
        for source_key, key, label, unit in (
            ("mesh.global_size_mm", "mesh.global_size_mm", "全局网格尺寸", "mm"),
            ("mesh.door_size_mm", "mesh.door_size_mm", "门洞局部网格", "mm"),
            ("loads.top_mass_tonne", "loads.top_mass_tonne", "顶部等效质量", "tonne"),
            ("loads.center_of_mass_offset_mm", "loads.center_of_mass_offset_mm", "顶部质心偏心距", "mm"),
            ("loads.top_total_force_n", "loads.top_horizontal_force_n", "顶部水平力", "N"),
            ("loads.wind_pressure_mpa", "loads.wind_pressure_mpa", "风压", "MPa"),
            ("modal.modes", "analysis.modal_modes", "模态阶数", None),
        ):
            item = fem.get(source_key)
            value = item.get("value") if isinstance(item, dict) else item
            if value is not None:
                evidence = self._excel_evidence(file, item, f"fem_settings.{source_key}", value)
                evidence_fragments.append(evidence)
                result.append(self._candidate(file, key, label, value, unit, evidence, 1.0, "deterministic_excel"))
        return result, evidence_fragments

    def _excel_evidence(self, file: BatchFile, item: Any, path: str, value: Any) -> EvidenceFragment:
        data = item if isinstance(item, dict) else {}
        field = _evidence_field_from_path(path)
        location = {
            "sheet": data.get("source_sheet"),
            "cell_or_range": data.get("source_cell_or_range") or data.get("source"),
            "json_path": f"$.{path}",
        }
        content_hash = hashlib.sha256(f"{file.sha256}|{path}|{value}".encode()).hexdigest()
        return EvidenceFragment(
            evidence_id=stable_evidence_id(file.sha256, location, field, content_hash),
            file_id=file.file_id,
            filename=file.filename,
            kind="structured",
            text=json.dumps(value, ensure_ascii=False),
            location=location,
            extraction_method="deterministic_excel",
            extraction_methods=["deterministic_excel"],
            confidence=float(data.get("confidence") or 1.0),
            content_hash=content_hash,
            field=field,
            label=_parameter_label(field) if field else None,
            raw_value=value,
            value=value,
            raw_unit=data.get("unit"),
            unit=data.get("unit"),
            source_sha256=file.sha256,
        )

    def _package_candidates(self, file: BatchFile, package: dict[str, Any]) -> tuple[list[ParameterCandidate], list[EvidenceFragment]]:
        candidates: list[ParameterCandidate] = []
        evidence: list[EvidenceFragment] = []
        for key, value, unit in _package_values(package):
            if value is None:
                continue
            source_field = _package_source_field(package, key)
            fragment = self._excel_evidence(file, source_field, f"standard_input.{key}", value)
            fragment.extraction_method = "deterministic_standard_input"
            evidence.append(fragment)
            defaulted = str(source_field.get("status") or "") == "defaulted"
            candidates.append(
                self._candidate(
                    file,
                    key,
                    _parameter_label(key),
                    value,
                    unit,
                    fragment,
                    float(source_field.get("confidence") or 1.0),
                    "deterministic",
                    source_kind="capability_default" if defaulted else "file_observed",
                    requires_confirmation=defaulted,
                )
            )
        return candidates, evidence

    def _llm_batch_candidates(self, batch: InputBatch, response: dict[str, Any]) -> list[ParameterCandidate]:
        evidence_by_id = {item.evidence_id: item for item in batch.evidence}
        result: list[ParameterCandidate] = []
        for field in response.get("fields") or []:
            path = str(field.get("path") or field.get("canonical_path") or "")
            evidence_ids = [str(value) for value in field.get("evidence_ids") or [] if str(value) in evidence_by_id]
            source_kind = str(field.get("source_kind") or "")
            suggested = source_kind == "llm_suggested" or str(field.get("status") or "") in {"suggested", "inferred"}
            if not evidence_ids and not suggested:
                continue
            value = field.get("normalized_value", field.get("value"))
            first_evidence = evidence_by_id[evidence_ids[0]] if evidence_ids else None
            inferred_evidence = bool(first_evidence and (first_evidence.from_ocr or first_evidence.kind == "drawing"))
            expanded = _expand_candidate_field(path, value, field.get("normalized_unit") or field.get("unit"))
            if not expanded and path:
                isolated = {
                    "candidate_path": path,
                    "raw_value": value,
                    "unit": field.get("normalized_unit") or field.get("unit"),
                    "status": "unsupported",
                    "mapping_confidence": float(field.get("confidence") or 0.5),
                    "evidence_ids": evidence_ids,
                    "reason": "参数未在当前模板和特征模块注册，已隔离且不会进入正式IR或INP",
                    "source": first_evidence.filename if first_evidence else "LLM工程建议",
                }
                marker = (path, json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
                existing = {
                    (
                        str(item.get("candidate_path")),
                        json.dumps(item.get("raw_value"), ensure_ascii=False, sort_keys=True, default=str),
                    )
                    for item in batch.unsupported_candidates
                }
                if marker not in existing:
                    batch.unsupported_candidates.append(isolated)
            for key, expanded_value, unit in expanded:
                if expanded_value is None:
                    continue
                normalized_value, conversion, normalization_error = _safe_normalize_parameter(
                    key, expanded_value
                )
                result.append(ParameterCandidate(
                    candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
                    key=key,
                    label=_parameter_label(key),
                    raw_value=expanded_value,
                    raw_unit=unit,
                    normalized_value=normalized_value,
                    normalized_unit=unit,
                    conversion=conversion or str(field.get("mapping_reason") or ""),
                    source_file_id=first_evidence.file_id if first_evidence else "llm-suggestion",
                    evidence_ids=evidence_ids,
                    extraction_method="llm_mapping",
                    provider=str(response.get("provider") or "qwen"),
                    model=str(response.get("model") or batch.model),
                    confidence=float(field.get("confidence") or 0.5),
                    priority="P0",
                    requires_confirmation=suggested or inferred_evidence or bool(normalization_error),
                    source_kind="llm_suggested" if suggested else (
                        "goal_explicit" if first_evidence and first_evidence.file_id == "modeling-goal" else "llm_mapped"
                    ),
                    confirmation_status="pending" if suggested or inferred_evidence or normalization_error else "not_required",
                    assumption_basis=str(field.get("assumption_basis") or "") or None,
                    template_id=batch.selected_template_id,
                    prompt_hash=batch.prompt_hash,
                    confirmation_reason=normalization_error or "LLM 识别的关键建模参数需要工程师确认",
                ))
        return result

    def _build_readiness(self, batch: InputBatch) -> None:
        self._update_composition(batch)
        if batch.capability_id == L2_CAPABILITY_ID:
            values = dict(batch.model_composition.get("effective_parameters") or {})
            l2_pending = sorted(
                key for key, item in (batch.fem_specification.get("parameters") or {}).items()
                if isinstance(item, dict)
                and (item.get("requires_confirmation") or item.get("confirmation_status") == "pending")
            )
            compute_blockers: list[str] = []
            if batch.fem_review.get("status") != "confirmed":
                compute_blockers.append("FEM 建模参数尚未由工程师确认")
            if batch.engineering_review.get("status") != "confirmed":
                compute_blockers.append("工程建模目标尚未由工程师确认")
            batch.standard_input_candidate = {
                "schema_version": "l2_parameter_candidate_v1",
                "capability_id": L2_CAPABILITY_ID,
                "parameters": values,
            }
            batch.review_parameters = []
            batch.review_revision = max(1, batch.review_revision)
            batch.parameter_advisories = [
                {
                    "key": key, "label": key, "kind": "capability_default",
                    "message": "L2 能力建议值，必须由工程师确认后才能编译。",
                    "value": item.get("value"),
                    "confirmation_status": item.get("confirmation_status"),
                }
                for key, item in (batch.fem_specification.get("parameters") or {}).items()
                if isinstance(item, dict)
            ]
            batch.readiness = {
                "schema_version": "project_preparation_readiness_v2",
                "preview_allowed": True,
                "compute_allowed": not l2_pending and not compute_blockers,
                "technical_blockers": [], "compute_blockers": compute_blockers,
                "pending_confirmation_keys": l2_pending,
                "engineering_status": batch.engineering_review.get("status", "pending"),
                "fem_status": batch.fem_review.get("status", "pending"),
                "capability_status": "compatible",
                "system_modeling_settings": {
                    "shell_element": {"value": "S4R", "source": "l2_capability"},
                    "bolt_element": {"value": "B31", "source": "l2_capability"},
                    "step_order": {"value": ["preload", "hold", "service"], "source": "fixed_rule"},
                },
            }
            return
        initial_values = dict(
            batch.model_composition.get("effective_parameters")
            or batch.resolved_parameters
        )
        if (
            float(initial_values.get("loads.top_mass_tonne") or 0) > 0
            and initial_values.get("loads.center_of_mass_offset_mm") is None
        ):
            key = "loads.center_of_mass_offset_mm"
            if not any(item.key == key for item in batch.candidates):
                batch.candidates.append(ParameterCandidate(
                    candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
                    key=key,
                    label="顶部质心偏心距",
                    raw_value=None,
                    raw_unit="mm",
                    normalized_value=0.0,
                    normalized_unit="mm",
                    conversion="Excel 未提供 -> 建议 0 mm",
                    source_file_id="capability-profile",
                    evidence_ids=[],
                    extraction_method="capability_default",
                    provider="deterministic",
                    model="none",
                    confidence=1.0,
                    priority="P0",
                    requires_confirmation=True,
                    confirmation_reason="顶部质量大于 0，质心偏心距必须由工程师确认。",
                    source_kind="capability_default",
                    confirmation_status="pending",
                    assumption_basis="缺失时仅建议 0 mm，不自动确认。",
                ))
            batch.resolved_parameters[key] = 0.0
            self._update_composition(batch)
        values = dict(
            batch.model_composition.get("effective_parameters")
            or batch.resolved_parameters
        )
        package = _standard_package_from_values(values, batch)
        validation = validate_standard_input({
            "standard_input_package": package,
            "fem_params": package.get("fem_settings") or {},
        })
        validation_errors = [
            str(item.get("message") or item.get("field") or item.get("path"))
            for item in validation.get("issues") or []
            if item.get("severity") == "error"
        ]
        blockers = [
            str(item.get("reason") or item.get("key"))
            for item in batch.model_composition.get("preview_blockers") or []
        ]
        if not _valid_parameter_value("geometry.tower_sections", values.get("geometry.tower_sections")):
            blockers.insert(0, "无法形成至少一个尺寸有效且连续的塔筒分段")
        pending = {
            item.key
            for item in batch.candidates
            if item.confirmation_status == "pending" or item.requires_confirmation
        }
        pending.update(item.key for item in batch.conflicts if item.confirmation_status == "pending")
        pending.update(
            str(item.get("key"))
            for item in batch.model_composition.get("defaulted_parameters") or []
            if item.get("key") and item.get("requires_confirmation")
        )
        pending.update(
            key
            for key, item in (batch.fem_specification.get("parameters") or {}).items()
            if isinstance(item, dict)
            and (
                item.get("requires_confirmation")
                or item.get("confirmation_status") == "pending"
            )
        )
        advisories = [
            {
                "key": item.key,
                "label": item.label,
                "kind": item.source_kind,
                "message": "LLM给出的工程建议，需要工程师确认" if item.source_kind == "llm_suggested" else "根据建模目标或识别证据确定",
                "value": item.normalized_value,
                "unit": item.normalized_unit,
                "assumption_basis": item.assumption_basis,
                "confirmation_status": item.confirmation_status,
            }
            for item in batch.candidates
            if item.source_kind == "goal_explicit" or item.confirmation_status == "pending"
        ]
        advisories.extend({
            "key": item.key,
            "label": item.label,
            "kind": "conflict",
            "message": "不同资料给出了不同值，预览采用优先级最高的候选",
            "value": item.resolved_value,
            "confirmation_status": item.confirmation_status,
        } for item in batch.conflicts)
        advisories.extend({
            "key": item.get("key"),
            "label": _parameter_label(str(item.get("key"))),
            "kind": "defaulted",
            "message": "按模型模板采用的预览值，需要工程师确认后才能计算",
            "value": item.get("value"),
            "confirmation_status": "pending",
        } for item in batch.model_composition.get("defaulted_parameters") or [] if item.get("requires_confirmation"))
        compute_blockers = [
            *validation_errors,
            *[
                str(item.get("reason") or item.get("key"))
                for item in batch.model_composition.get("compute_blockers") or []
            ],
        ]
        if batch.capability_match.get("status") == "incompatible":
            compute_blockers.append("参考模型与当前风塔 S4R 建模能力不兼容")
        if batch.fem_review.get("status") != "confirmed":
            compute_blockers.append("FEM 建模参数尚未由工程师确认")
        if batch.engineering_review.get("status") != "confirmed":
            compute_blockers.append("工程参数尚未由工程师确认")
        unsupported_active = [
            item for item in batch.unsupported_candidates
            if str(item.get("candidate_path") or "").startswith("feature.")
        ]
        compute_blockers.extend(
            str(item.get("reason") or item.get("candidate_path"))
            for item in unsupported_active
        )
        batch.completeness_report = {}
        batch.parameter_advisories = advisories
        batch.standard_input_candidate = package
        batch.readiness = {
            "schema_version": "project_preparation_readiness_v2",
            "preview_allowed": not blockers,
            "compute_allowed": not blockers and not pending and not compute_blockers,
            "technical_blockers": list(dict.fromkeys(blockers)),
            "compute_blockers": list(dict.fromkeys(compute_blockers)),
            "pending_confirmation_keys": sorted(pending),
            "engineering_status": batch.engineering_review.get("status", "pending"),
            "fem_status": batch.fem_review.get("status", "pending"),
            "capability_status": batch.capability_match.get("status", "compatible"),
            "active_parameter_readiness": batch.active_parameter_readiness,
            "system_modeling_settings": {
                "element_type": {"value": "S4R", "source": "wind_tower_adapter"},
                "section_type": {"value": "Shell Section", "source": "wind_tower_adapter"},
            },
        }
        self._build_review_parameters(batch, values)

    @staticmethod
    def _parameter_evidence_map(
        batch: InputBatch,
        values: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        evidence_by_id = {item.evidence_id: item for item in batch.evidence}
        result: dict[str, dict[str, Any]] = {}
        for key, value in values.items():
            matching = [
                item
                for item in batch.candidates
                if item.key == key and _same_value(item.normalized_value, value)
            ]
            if not matching:
                continue
            chosen = sorted(matching, key=_candidate_rank, reverse=True)[0]
            result[key] = {
                "candidate_id": chosen.candidate_id,
                "evidence_ids": list(chosen.evidence_ids),
                "raw_value": chosen.raw_value,
                "raw_unit": chosen.raw_unit,
                "normalized_value": chosen.normalized_value,
                "normalized_unit": chosen.normalized_unit,
                "conversion": chosen.conversion,
                "extraction_method": chosen.extraction_method,
                "provider": chosen.provider,
                "model": chosen.model,
                "mapping_confidence": chosen.confidence,
                "confidence": chosen.confidence,
                "source_kind": chosen.source_kind,
                "confirmation_status": chosen.confirmation_status,
                "requires_confirmation": chosen.requires_confirmation,
                "evidence": [
                    evidence_by_id[evidence_id].model_dump(mode="json")
                    for evidence_id in chosen.evidence_ids
                    if evidence_id in evidence_by_id
                ],
                "trust_level": (
                    "high"
                    if chosen.confirmation_status == "confirmed"
                    or (
                        chosen.provider == "deterministic"
                        and not chosen.requires_confirmation
                    )
                    else (
                        "low"
                        if chosen.requires_confirmation
                        or chosen.source_kind == "llm_suggested"
                        else "medium"
                    )
                ),
            }
        return result

    def _build_review_parameters(
        self, batch: InputBatch, values: dict[str, Any]
    ) -> None:
        active_keys = list(
            batch.model_composition.get("active_parameter_keys") or []
        )
        required_keys = {
            str(key)
            for manifest in batch.model_composition.get("module_manifests") or []
            for key in manifest.get("requires") or []
        }
        rows: list[dict[str, Any]] = []
        conflicts = {item.key: item for item in batch.conflicts}
        pending = set(batch.readiness.get("pending_confirmation_keys") or [])
        for key in active_keys:
            if key.startswith(("mesh.", "analysis.", "boundary.")):
                continue
            if key == "geometry.tower_sections":
                for index, section in enumerate(
                    values.get("geometry.tower_sections") or []
                ):
                    for field, label in (
                        ("z_bottom_mm", "底部标高"),
                        ("z_top_mm", "顶部标高"),
                        ("diameter_bottom_mm", "底部直径"),
                        ("diameter_top_mm", "顶部直径"),
                        ("thickness_mm", "厚度"),
                    ):
                        rows.append(_review_parameter_row(
                            batch,
                            f"geometry.tower_sections.{index}.{field}",
                            section.get(field),
                            label=f"第{index + 1}段{label}",
                            group="tower_sections",
                            unit="mm",
                            control="number",
                            required=True,
                            attention=False,
                        ))
                continue
            candidate = _selected_candidate(batch, key, values.get(key))
            normalization_error = None
            _, _, normalization_error = _safe_normalize_parameter(
                key, values.get(key)
            )
            rows.append(_review_parameter_row(
                batch,
                key,
                values.get(key),
                label=_parameter_label(key),
                group=_review_group(key),
                unit=(candidate.normalized_unit if candidate else _parameter_unit(key)),
                control=_review_control(key, values.get(key)),
                options=["+X", "-X", "+Y", "-Y", "+Z", "-Z"] if key == "loads.top_force_direction" else None,
                raw_value=candidate.raw_value if candidate else values.get(key),
                source_kind=candidate.source_kind if candidate else "system_setting",
                source_file_id=candidate.source_file_id if candidate else "system",
                conversion=candidate.conversion if candidate else None,
                confidence=candidate.confidence if candidate else 1.0,
                attention=(
                    key in pending
                    or key in conflicts
                    or bool(normalization_error)
                    or bool(candidate and (
                        candidate.source_kind in {"llm_suggested", "llm_mapped"}
                        or candidate.requires_confirmation
                        or candidate.conversion
                    ))
                ),
                validation_error=normalization_error,
                required=key in required_keys,
            ))
        current_values = [
            {"key": item["key"], "value": item.get("value")}
            for item in rows
        ]
        previous_values = [
            {"key": item["key"], "value": item.get("value")}
            for item in batch.review_parameters
        ]
        if current_values != previous_values:
            batch.review_revision = max(1, batch.review_revision + 1)
        batch.review_parameters = rows

    def _llm_candidates(self, file: BatchFile, response: dict[str, Any]) -> list[ParameterCandidate]:
        result = []
        for item in response.get("candidates") or []:
            key = str(item.get("key") or "")
            if not key:
                continue
            normalized_value, conversion, normalization_error = _safe_normalize_parameter(
                key, item.get("value")
            )
            result.append(ParameterCandidate(
                candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
                key=key,
                label=str(item.get("label") or key),
                raw_value=item.get("value"),
                raw_unit=item.get("unit"),
                normalized_value=normalized_value,
                normalized_unit=item.get("unit"),
                source_file_id=file.file_id,
                evidence_ids=[str(value) for value in item.get("evidence_ids") or []],
                extraction_method="llm_mapping",
                provider=str(response.get("provider") or "qwen"),
                model=str(response.get("model") or "none"),
                confidence=float(item.get("confidence") or 0.5),
                priority="P1" if item.get("priority") == "P1" else "P0",
                conversion=conversion,
                requires_confirmation=bool(item.get("requires_confirmation")) or file.cost_level in {"full_ocr", "complex_drawing"} or bool(normalization_error),
                confirmation_reason=normalization_error or ("OCR或图纸推断必须确认" if file.cost_level in {"full_ocr", "complex_drawing"} else None),
            ))
        return result

    def _candidate(
        self,
        file: BatchFile,
        key: str,
        label: str,
        value: Any,
        unit: str | None,
        evidence: EvidenceFragment,
        confidence: float,
        method: str,
        *,
        source_kind: str = "file_observed",
        requires_confirmation: bool = False,
    ) -> ParameterCandidate:
        inferred = evidence.from_ocr or evidence.kind == "drawing"
        normalized_value, conversion, normalization_error = _safe_normalize_parameter(
            key, value
        )
        return ParameterCandidate(
            candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
            key=key,
            label=label,
            raw_value=value,
            raw_unit=unit,
            normalized_value=normalized_value,
            normalized_unit=unit,
            source_file_id=file.file_id,
            evidence_ids=[evidence.evidence_id],
            extraction_method=method,
            confidence=confidence,
            conversion=conversion,
            requires_confirmation=requires_confirmation or inferred or bool(normalization_error),
            source_kind=source_kind,
            confirmation_status="pending" if requires_confirmation or inferred or normalization_error else "not_required",
            confirmation_reason=normalization_error or ("OCR或图纸推断必须确认" if inferred else None),
        )

    def _merge_candidates(self, batch: InputBatch) -> None:
        conflicts = []
        resolved = {}
        for key in dict.fromkeys(item.key for item in batch.candidates):
            items = [item for item in batch.candidates if item.key == key]
            observed = [item for item in items if item.source_kind != "llm_suggested"]
            if observed:
                for suggestion in items:
                    if suggestion.source_kind == "llm_suggested":
                        suggestion.requires_confirmation = False
                        suggestion.confirmation_status = "not_required"
                items = observed
            manual = [item for item in items if item.extraction_method == "manual"]
            if manual:
                resolved[key] = manual[-1].normalized_value
                continue
            groups: list[list[ParameterCandidate]] = []
            for item in items:
                group = next((group for group in groups if _same_value(group[0].normalized_value, item.normalized_value)), None)
                if group is not None:
                    group.append(item)
                else:
                    groups.append([item])
            if len(groups) == 1:
                chosen = sorted(items, key=_candidate_rank, reverse=True)[0]
                resolved[key] = chosen.normalized_value
                continue
            chosen = sorted(items, key=_candidate_rank, reverse=True)[0]
            resolved[key] = chosen.normalized_value
            candidate_ids = [item.candidate_id for item in items]
            priority = "P0" if any(item.priority == "P0" for item in items) else "P1"
            conflicts.append(ParameterConflict(
                conflict_id=f"conflict-{hashlib.sha256((batch.batch_id + key).encode()).hexdigest()[:10]}",
                key=key,
                label=items[0].label,
                priority=priority,
                candidate_ids=candidate_ids,
                blocking=False,
                resolved_candidate_id=chosen.candidate_id,
                resolved_value=chosen.normalized_value,
                resolved_unit=chosen.normalized_unit,
                confirmation_status="pending",
            ))
        previous = {item.key: item for item in batch.conflicts if item.resolved_value is not None}
        for conflict in conflicts:
            if conflict.key in previous:
                old = previous[conflict.key]
                conflict.resolved_candidate_id = old.resolved_candidate_id
                conflict.resolved_value = old.resolved_value
                conflict.resolved_unit = old.resolved_unit
                conflict.confirmation_status = old.confirmation_status
                resolved[conflict.key] = old.resolved_value
        batch.conflicts = conflicts
        manual_values = {
            item.key: item.normalized_value
            for item in batch.candidates
            if item.extraction_method == "manual"
        }
        confirmed_values = {
            key: item.resolved_value
            for key, item in previous.items()
            if item.resolved_value is not None
        }
        batch.resolved_parameters = {**resolved, **confirmed_values, **manual_values}

    def _confirmed_changes(self, batch: InputBatch, project_id: str) -> list[ParameterChange]:
        model = self.pipeline.model_parameters(project_id)
        current = {item["key"]: item["value"] for item in model["parameters"]}
        result = []
        for key, value in batch.resolved_parameters.items():
            if key in current and not _same_value(current[key], value):
                result.append(ParameterChange(key=key, value=value))
        return result

    def _refresh_status(self, batch: InputBatch) -> None:
        refresh_status(batch)

    def _get(self, batch_id: str) -> InputBatch:
        batch = self._batches.get(batch_id)
        if batch is None:
            path = self.root / batch_id / "batch.json"
            if not path.exists():
                raise KeyError(batch_id)
            batch = InputBatch.model_validate_json(path.read_text(encoding="utf-8"))
            interrupted = recover_interrupted_batch(batch)
            self._upgrade_parameter_values(batch)
            self._ensure_fem_state(batch)
            self._batches[batch_id] = batch
            if interrupted:
                self._save(batch)
        else:
            self._upgrade_parameter_values(batch)
            self._ensure_fem_state(batch)
        if (
            batch.selected_template_id
            and batch.status not in {"queued", "processing", "confirmed"}
            and (
                not batch.review_parameters
                or (batch.standard_input_candidate or {}).get(
                    "fem_settings", {}
                ).get("loads.top_force_direction")
                != batch.resolved_parameters.get("loads.top_force_direction")
            )
        ):
            self._merge_candidates(batch)
            self._build_readiness(batch)
            self._refresh_status(batch)
            self._save(batch)
        return batch

    def _ensure_fem_state(self, batch: InputBatch) -> None:
        ensure_fem_state(
            batch,
            lambda: capability_fem_specification(batch.capability_id),
            lambda summary: _capability_match(summary, batch.capability_id),
        )

    def _upgrade_parameter_values(self, batch: InputBatch) -> None:
        for candidate in batch.candidates:
            normalized, conversion, error = _safe_normalize_parameter(
                candidate.key, candidate.normalized_value
            )
            if not error:
                candidate.normalized_value = normalized
                candidate.conversion = conversion or candidate.conversion
            elif candidate.key == "loads.top_force_direction":
                candidate.requires_confirmation = True
                candidate.confirmation_status = "pending"
                candidate.confirmation_reason = error
        for key, value in list(batch.resolved_parameters.items()):
            normalized, _, error = _safe_normalize_parameter(key, value)
            if not error:
                batch.resolved_parameters[key] = normalized
        for conflict in batch.conflicts:
            if conflict.resolved_value is None:
                continue
            normalized, _, error = _safe_normalize_parameter(
                conflict.key, conflict.resolved_value
            )
            if not error:
                conflict.resolved_value = normalized

    def _save(self, batch: InputBatch) -> None:
        self._refresh_channel_snapshots(batch)
        batch.updated_at = _now()
        path = self.root / batch.batch_id / "batch.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(batch.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)
        self.metadata.upsert_input_batch(
            batch.batch_id,
            batch.status,
            batch.model_dump_json(),
        )

    def _refresh_channel_snapshots(self, batch: InputBatch) -> None:
        batch.model_library_recommendations = self.model_library.recommend(
            _batch_model_signature(batch),
            capability_id=str(batch.fem_specification.get("capability_id") or CAPABILITY_ID),
        )
        batch.engineering_channel = {
            "status": batch.engineering_review.get("status", "pending"),
            "provider": self.engineering_advisor.capabilities().get("provider"),
            "model": self.engineering_advisor.capabilities().get("main_model"),
            "knowledge_base": self.knowledge.engineering.status(),
            "file_count": len(batch.files),
            "candidate_count": len(batch.candidates),
            "usage": batch.engineering_llm_usage,
            "warnings": [item for item in batch.warnings if not item.startswith("FEM:")],
        }
        reference = (batch.reference_model or {}).get("summary") or {}
        batch.reference_transfer = deepcopy(
            reference.get("reference_transfer")
            or batch.fem_specification.get("reference_transfer")
            or {}
        )
        batch.fem_channel = {
            "status": batch.fem_review.get("status", "pending"),
            "provider": self.fem_advisor.capabilities().get("provider"),
            "model": self.fem_advisor.capabilities().get("main_model"),
            "knowledge_base": self.knowledge.fem.status(),
            "source": "model_library" if batch.selected_library_model else (
                "reference_inp" if reference else (
                    "modeling_goal"
                    if batch.fem_specification.get("source_mode")
                    == "modeling_goal_and_capability"
                    else "capability_default"
                )
            ),
            "reference_model_id": reference.get("reference_model_id"),
            "compatibility": batch.capability_match.get("status", "not_provided"),
            "usage": batch.fem_llm_usage,
        }
        batch.merge_readiness = {
            "engineering_confirmed": batch.engineering_review.get("status") == "confirmed",
            "fem_confirmed": batch.fem_review.get("status") == "confirmed",
            "capability_accepted": batch.capability_match.get("status") != "incompatible",
            "ready": bool(batch.readiness.get("compute_allowed")),
            "blockers": list(batch.readiness.get("compute_blockers") or []),
        }

    def _load_existing(self) -> None:
        for path in self.root.glob("batch-*/batch.json"):
            try:
                batch = InputBatch.model_validate_json(path.read_text(encoding="utf-8"))
                interrupted = recover_interrupted_batch(batch)
                self._upgrade_parameter_values(batch)
                self._ensure_fem_state(batch)
                self._batches[batch.batch_id] = batch
                if interrupted:
                    self._save(batch)
            except Exception:
                continue

    def _source_path(self, batch_id: str, file_id: str) -> Path:
        matches = list((self.root / batch_id / "source").glob(f"{file_id}-*"))
        if not matches:
            raise FileNotFoundError(file_id)
        return matches[0]


def _dedupe_candidates(items: list[ParameterCandidate]) -> list[ParameterCandidate]:
    result = []
    seen = set()
    for item in items:
        marker = (item.key, json.dumps(item.normalized_value, sort_keys=True, default=str), item.source_file_id, item.extraction_method)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def _evidence_field_from_path(path: str) -> str:
    field = str(path)
    for prefix in ("standard_input.", "fem_settings.", "door_opening."):
        if field.startswith(prefix):
            field = field[len(prefix):]
            break
    aliases = {
        "height_mm": "geometry.door.height_mm",
        "width_mm": "geometry.door.width_mm",
        "bottom_z_mm": "geometry.door.bottom_z_mm",
        "enabled": "geometry.door.enabled",
        "top_total_force_n": "loads.top_horizontal_force_n",
        "modal.modes": "analysis.modal_modes",
    }
    if field in aliases:
        return aliases[field]
    if field.startswith(("geometry.", "material.", "mesh.", "loads.", "analysis.", "boundary.", "interaction.")):
        return field
    return path


def _batch_model_signature(batch: InputBatch) -> dict[str, Any]:
    effective = batch.model_composition.get("effective_parameters", {})
    sections = (
        effective.get("geometry.tower_sections")
        or batch.resolved_parameters.get("geometry.tower_sections")
        or []
    )
    ordered = sorted(
        (item for item in sections if isinstance(item, dict)),
        key=lambda item: float(item.get("z_bottom_mm") or 0.0),
    )
    heights = [
        float(item["z_top_mm"])
        for item in ordered
        if isinstance(item.get("z_top_mm"), (int, float))
    ]
    return {
        "height_mm": max(heights) if heights else None,
        "base_diameter_mm": ordered[0].get("diameter_bottom_mm") if ordered else None,
        "top_diameter_mm": ordered[-1].get("diameter_top_mm") if ordered else None,
        "door_enabled": effective.get(
            "geometry.door.enabled",
            batch.resolved_parameters.get("geometry.door.enabled"),
        ),
    }


def _safe_normalize_parameter(
    key: str, value: Any
) -> tuple[Any, str | None, str | None]:
    try:
        normalized, conversion = normalize_parameter_value(key, value)
        return normalized, conversion, None
    except ValueError as exc:
        return value, None, str(exc)


def _selected_candidate(
    batch: InputBatch, key: str, value: Any
) -> ParameterCandidate | None:
    matching = [
        item for item in batch.candidates
        if item.key == key and _same_value(item.normalized_value, value)
    ]
    return sorted(matching, key=_candidate_rank, reverse=True)[0] if matching else None


def _review_parameter_row(
    batch: InputBatch,
    key: str,
    value: Any,
    *,
    label: str,
    group: str,
    unit: str | None = None,
    control: str = "text",
    options: list[str] | None = None,
    raw_value: Any = None,
    source_kind: str = "file_observed",
    source_file_id: str = "",
    conversion: str | None = None,
    confidence: float = 1.0,
    attention: bool = False,
    validation_error: str | None = None,
    required: bool = False,
) -> dict[str, Any]:
    source_file = next(
        (
            item.filename for item in batch.files
            if item.file_id == source_file_id
        ),
        source_file_id,
    )
    return {
        "key": key,
        "label": label,
        "group": group,
        "value": value,
        "unit": unit,
        "control": control,
        "options": options or [],
        "raw_value": value if raw_value is None else raw_value,
        "source_kind": source_kind,
        "source_file": source_file,
        "conversion": conversion,
        "confidence": confidence,
        "attention": attention,
        "validation_error": validation_error,
        "required": required,
    }


def _review_group(key: str) -> str:
    if key.startswith("geometry."):
        return "geometry"
    if key.startswith("material."):
        return "material"
    if key.startswith("mesh."):
        return "mesh"
    if key.startswith("loads."):
        return "loads"
    return "analysis"


def _review_control(key: str, value: Any) -> str:
    if key == "loads.top_force_direction":
        return "select"
    if isinstance(value, bool) or key.endswith("_enabled"):
        return "toggle"
    if isinstance(value, (int, float)) or key.endswith((
        "_mm", "_mpa", "_tonne", "_n", "_modes", "_layers", "_deg"
    )):
        return "number"
    return "text"


def _parameter_unit(key: str) -> str | None:
    return {
        "material.elastic_modulus_mpa": "MPa",
        "material.density": "tonne/mm³",
        "material.yield_strength_mpa": "MPa",
        "mesh.global_size_mm": "mm",
        "mesh.door_size_mm": "mm",
        "mesh.patch_margin_u_mm": "mm",
        "mesh.patch_margin_z_mm": "mm",
        "loads.top_mass_tonne": "tonne",
        "loads.center_of_mass_offset_mm": "mm",
        "loads.top_horizontal_force_n": "N",
        "loads.wind_pressure_mpa": "MPa",
        "loads.wind_pressure_profile": "MPa",
        "loads.top_force_parallel_n": "N",
        "loads.top_force_transverse_n": "N",
        "loads.top_force_vertical_n": "N",
        "loads.top_moment_roll_nmm": "N·mm",
        "loads.top_moment_pitch_nmm": "N·mm",
        "loads.top_moment_yaw_nmm": "N·mm",
        "loads.hub_vertical_offset_mm": "mm",
        "geometry.door.width_mm": "mm",
        "geometry.door.height_mm": "mm",
        "geometry.door.bottom_z_mm": "mm",
        "geometry.door.corner_radius_mm": "mm",
        "geometry.door.theta_center_deg": "deg",
        "geometry.door.outer_width_mm": "mm",
        "geometry.door.outer_height_mm": "mm",
        "geometry.door.frame_thickness_mm": "mm",
    }.get(key)


def _coerce_review_value(row: dict[str, Any], value: Any) -> Any:
    control = row.get("control")
    if control == "toggle":
        if isinstance(value, str):
            return value.strip().lower() in {
                "true", "1", "yes", "on", "是", "启用"
            }
        return bool(value)
    if control == "number":
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{row.get('label')}必须是数字") from exc
        if str(row.get("key")).endswith(("_modes", "_layers")):
            if not number.is_integer():
                raise ValueError(f"{row.get('label')}必须是整数")
            return int(number)
        return number
    return value


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    return left == right


def _canonical_parameter_key(path: str) -> str | None:
    mapping = {
        "domain.wind_tower.tower_segments": "geometry.tower_sections",
        "domain.wind_tower.door_opening": "geometry.door",
        "generic.materials[0]": "material",
        "generic.fem_settings": "fem_settings",
    }
    return mapping.get(path)


def _expand_candidate_field(path: str, value: Any, unit: str | None) -> list[tuple[str, Any, str | None]]:
    if path in {"domain.wind_tower.tower_segments", "geometry.tower_sections"} and isinstance(value, list):
        return [("geometry.tower_sections", value, "mm")]
    if path == "domain.wind_tower.door_opening" and isinstance(value, dict):
        return [
            ("geometry.door.enabled", value.get("enabled"), None),
            ("geometry.door.width_mm", value.get("width_mm"), "mm"),
            ("geometry.door.height_mm", value.get("height_mm"), "mm"),
            ("geometry.door.bottom_z_mm", value.get("z_bottom_mm"), "mm"),
            ("geometry.door.theta_center_deg", value.get("azimuth_deg"), "deg"),
            ("geometry.door.corner_radius_mm", value.get("corner_radius_mm"), "mm"),
            ("geometry.door.reinforcement_enabled", value.get("reinforcement_enabled"), None),
            ("geometry.door.outer_width_mm", value.get("outer_width_mm"), "mm"),
            ("geometry.door.outer_height_mm", value.get("outer_height_mm"), "mm"),
            ("geometry.door.frame_thickness_mm", value.get("frame_thickness_mm"), "mm"),
            ("geometry.door.reinforcement_material", value.get("reinforcement_material"), None),
        ]
    if path == "generic.materials[0]" and isinstance(value, dict):
        return [
            ("material.name", value.get("name"), None),
            ("material.elastic_modulus_mpa", value.get("elastic_modulus_mpa"), "MPa"),
            ("material.poisson_ratio", value.get("poisson_ratio"), None),
            ("material.density", value.get("density_tonne_mm3"), "tonne/mm³"),
            ("material.yield_strength_mpa", value.get("yield_strength_mpa"), "MPa"),
        ]
    if path == "generic.fem_settings" and isinstance(value, dict):
        return [
            ("mesh.global_size_mm", value.get("mesh.global_size_mm"), "mm"),
            ("mesh.door_size_mm", value.get("mesh.door_size_mm"), "mm"),
            ("loads.top_mass_tonne", value.get("loads.top_mass_tonne"), "tonne"),
            ("loads.center_of_mass_offset_mm", value.get("loads.center_of_mass_offset_mm"), "mm"),
            ("loads.top_horizontal_force_n", value.get("loads.top_total_force_n"), "N"),
            ("loads.top_force_direction", value.get("loads.top_force_direction"), None),
            ("loads.wind_pressure_mpa", value.get("loads.wind_pressure_mpa"), "MPa"),
            ("loads.wind_direction_vector", value.get("loads.wind_direction_vector"), None),
            ("loads.wind_pressure_profile", value.get("loads.wind_pressure_profile"), "MPa"),
            ("loads.top_force_parallel_n", value.get("loads.top_force_parallel_n"), "N"),
            ("loads.top_force_transverse_n", value.get("loads.top_force_transverse_n"), "N"),
            ("loads.top_force_vertical_n", value.get("loads.top_force_vertical_n"), "N"),
            ("loads.top_moment_roll_nmm", value.get("loads.top_moment_roll_nmm"), "N·mm"),
            ("loads.top_moment_pitch_nmm", value.get("loads.top_moment_pitch_nmm"), "N·mm"),
            ("loads.top_moment_yaw_nmm", value.get("loads.top_moment_yaw_nmm"), "N·mm"),
            ("loads.hub_vertical_offset_mm", value.get("loads.hub_vertical_offset_mm"), "mm"),
            ("analysis.static_enabled", value.get("analysis.static_enabled"), None),
            ("analysis.modal_enabled", value.get("analysis.modal_enabled"), None),
            ("analysis.modal_modes", value.get("modal.modes"), None),
            ("analysis.static_nlgeom", value.get("analysis.static_nlgeom"), None),
            ("analysis.modal_prestress", value.get("analysis.modal_prestress"), None),
            ("boundary.name", value.get("boundary.name"), None),
            ("mesh.patch_margin_u_mm", value.get("mesh.patch_margin_u_mm"), "mm"),
            ("mesh.patch_margin_z_mm", value.get("mesh.patch_margin_z_mm"), "mm"),
            ("mesh.hole_layers", value.get("mesh.hole_layers"), None),
            ("analysis.output_fields", value.get("outputs.field"), None),
        ]
    direct_keys = {
        "geometry.door.enabled", "geometry.door.width_mm", "geometry.door.height_mm",
        "geometry.door.bottom_z_mm", "geometry.door.theta_center_deg",
        "geometry.door.corner_radius_mm", "material.name",
        "geometry.door.reinforcement_enabled", "geometry.door.outer_width_mm",
        "geometry.door.outer_height_mm", "geometry.door.frame_thickness_mm",
        "geometry.door.reinforcement_material",
        "material.elastic_modulus_mpa", "material.poisson_ratio", "material.density",
        "material.yield_strength_mpa", "mesh.global_size_mm", "mesh.door_size_mm",
        "mesh.patch_margin_u_mm", "mesh.patch_margin_z_mm", "mesh.hole_layers",
        "loads.top_mass_tonne", "loads.center_of_mass_offset_mm", "loads.top_horizontal_force_n",
        "loads.top_force_direction", "loads.wind_pressure_mpa",
        "loads.wind_direction_vector", "loads.wind_pressure_profile",
        "loads.top_force_parallel_n", "loads.top_force_transverse_n",
        "loads.top_force_vertical_n", "loads.top_moment_roll_nmm",
        "loads.top_moment_pitch_nmm", "loads.top_moment_yaw_nmm",
        "loads.hub_vertical_offset_mm",
        "analysis.static_enabled", "analysis.modal_enabled", "analysis.modal_modes",
        "analysis.static_nlgeom", "analysis.modal_prestress",
        "analysis.output_fields", "boundary.name",
    }
    if path in direct_keys:
        return [(path, value, unit)]
    key = _canonical_parameter_key(path)
    return [(key, value, unit)] if key else []


def _parameter_label(key: str) -> str:
    labels = {item[0]: item[1] for item in LEGACY_PARAMETER_LABEL_SPECS}
    labels.update({
        "geometry.door": "门洞参数",
        "material": "材料参数",
        "fem_settings": "有限元设置",
        "geometry.door.width_mm": "门洞宽度",
        "geometry.door.height_mm": "门洞高度",
        "geometry.door.bottom_z_mm": "门洞底标高",
        "geometry.door.theta_center_deg": "门洞角度",
        "mesh.door_size_mm": "门洞局部网格",
        "analysis.modal_modes": "模态阶数",
    })
    return labels.get(key, key)


def _package_source_field(package: dict[str, Any], key: str) -> dict[str, Any]:
    """Return the structured source object so defaults retain pending status."""
    fem_key_by_parameter = {
        "mesh.global_size_mm": "mesh.global_size_mm",
        "mesh.door_size_mm": "mesh.door_size_mm",
        "loads.top_mass_tonne": "loads.top_mass_tonne",
        "loads.center_of_mass_offset_mm": "loads.center_of_mass_offset_mm",
        "loads.top_horizontal_force_n": "loads.top_total_force_n",
        "loads.top_force_direction": "loads.top_force_direction",
        "loads.wind_pressure_mpa": "loads.wind_pressure_mpa",
        "loads.wind_direction_vector": "loads.wind_direction_vector",
        "loads.wind_pressure_profile": "loads.wind_pressure_profile",
        "loads.top_force_parallel_n": "loads.top_force_parallel_n",
        "loads.top_force_transverse_n": "loads.top_force_transverse_n",
        "loads.top_force_vertical_n": "loads.top_force_vertical_n",
        "loads.top_moment_roll_nmm": "loads.top_moment_roll_nmm",
        "loads.top_moment_pitch_nmm": "loads.top_moment_pitch_nmm",
        "loads.top_moment_yaw_nmm": "loads.top_moment_yaw_nmm",
        "loads.hub_vertical_offset_mm": "loads.hub_vertical_offset_mm",
        "analysis.static_enabled": "analysis.static_enabled",
        "analysis.modal_enabled": "analysis.modal_enabled",
        "analysis.modal_modes": "modal.modes",
        "analysis.static_nlgeom": "analysis.static_nlgeom",
        "analysis.modal_prestress": "analysis.modal_prestress",
        "boundary.name": "boundary.name",
    }
    fem_key = fem_key_by_parameter.get(key)
    if fem_key:
        value = (package.get("fem_settings") or {}).get(fem_key)
        return value if isinstance(value, dict) else {}
    door_key_by_parameter = {
        "geometry.door.enabled": "enabled",
        "geometry.door.width_mm": "width_mm",
        "geometry.door.height_mm": "height_mm",
        "geometry.door.bottom_z_mm": "z_bottom_mm",
        "geometry.door.theta_center_deg": "azimuth_deg",
        "geometry.door.reinforcement_enabled": "reinforcement_enabled",
        "geometry.door.outer_width_mm": "outer_width_mm",
        "geometry.door.outer_height_mm": "outer_height_mm",
        "geometry.door.frame_thickness_mm": "frame_thickness_mm",
        "geometry.door.reinforcement_material": "reinforcement_material",
    }
    door_key = door_key_by_parameter.get(key)
    if door_key:
        value = (package.get("door_opening") or {}).get(door_key)
        return value if isinstance(value, dict) else {}
    if key.startswith("material."):
        material_key = {
            "material.name": "name",
            "material.elastic_modulus_mpa": "elastic_modulus_mpa",
            "material.poisson_ratio": "poisson_ratio",
            "material.density": "density_tonne_mm3",
            "material.yield_strength_mpa": "yield_strength_mpa",
        }.get(key)
        materials = package.get("materials") or []
        if material_key and materials:
            value = materials[0].get(material_key)
            if isinstance(value, dict):
                return value
            source = str(materials[0].get("source") or "")
            if source.endswith("_default"):
                return {
                    "status": "defaulted",
                    "confidence": 0.85,
                    "source_sheet": source,
                    "source_cell_or_range": None,
                }
    return {}


def _package_values(package: dict[str, Any]) -> list[tuple[str, Any, str | None]]:
    values: list[tuple[str, Any, str | None]] = []
    segments = []
    for raw in package.get("tower_segments") or []:
        segments.append({
            key: _source_value(raw.get(key))
            for key in ("z_bottom_mm", "z_top_mm", "diameter_bottom_mm", "diameter_top_mm", "thickness_mm")
        })
    if segments:
        values.append(("geometry.tower_sections", segments, "mm"))
    door = package.get("door_opening") or {}
    if door:
        values.extend((
            ("geometry.door.enabled", _source_value(door.get("enabled")), None),
            ("geometry.door.width_mm", _source_value(door.get("width_mm")), "mm"),
            ("geometry.door.height_mm", _source_value(door.get("height_mm")), "mm"),
            ("geometry.door.bottom_z_mm", _source_value(door.get("z_bottom_mm")), "mm"),
        ("geometry.door.theta_center_deg", _source_value(door.get("azimuth_deg", door.get("theta_center_deg"))), "deg"),
            ("geometry.door.reinforcement_enabled", _source_value(door.get("reinforcement_enabled")), None),
            ("geometry.door.outer_width_mm", _source_value(door.get("outer_width_mm")), "mm"),
            ("geometry.door.outer_height_mm", _source_value(door.get("outer_height_mm")), "mm"),
            ("geometry.door.frame_thickness_mm", _source_value(door.get("frame_thickness_mm")), "mm"),
            ("geometry.door.reinforcement_material", _source_value(door.get("reinforcement_material")), None),
        ))
    materials = package.get("materials") or []
    if materials:
        material = materials[0]
        values.extend((
            ("material.name", _source_value(material.get("name")), None),
            ("material.elastic_modulus_mpa", _source_value(material.get("elastic_modulus_mpa")), "MPa"),
            ("material.poisson_ratio", _source_value(material.get("poisson_ratio")), None),
            ("material.density", _source_value(material.get("density_tonne_mm3", material.get("density"))), "tonne/mm³"),
            ("material.yield_strength_mpa", _source_value(material.get("yield_strength_mpa")), "MPa"),
        ))
    fem = {key: _source_value(value) for key, value in (package.get("fem_settings") or {}).items()}
    for source, target, unit in (
        ("mesh.global_size_mm", "mesh.global_size_mm", "mm"),
        ("mesh.door_size_mm", "mesh.door_size_mm", "mm"),
        ("loads.top_mass_tonne", "loads.top_mass_tonne", "tonne"),
        ("loads.center_of_mass_offset_mm", "loads.center_of_mass_offset_mm", "mm"),
        ("loads.top_total_force_n", "loads.top_horizontal_force_n", "N"),
        ("loads.top_force_direction", "loads.top_force_direction", None),
        ("loads.wind_pressure_mpa", "loads.wind_pressure_mpa", "MPa"),
        ("loads.wind_direction_vector", "loads.wind_direction_vector", None),
        ("loads.wind_pressure_profile", "loads.wind_pressure_profile", "MPa"),
        ("loads.top_force_parallel_n", "loads.top_force_parallel_n", "N"),
        ("loads.top_force_transverse_n", "loads.top_force_transverse_n", "N"),
        ("loads.top_force_vertical_n", "loads.top_force_vertical_n", "N"),
        ("loads.top_moment_roll_nmm", "loads.top_moment_roll_nmm", "N·mm"),
        ("loads.top_moment_pitch_nmm", "loads.top_moment_pitch_nmm", "N·mm"),
        ("loads.top_moment_yaw_nmm", "loads.top_moment_yaw_nmm", "N·mm"),
        ("loads.hub_vertical_offset_mm", "loads.hub_vertical_offset_mm", "mm"),
        ("analysis.static_enabled", "analysis.static_enabled", None),
        ("analysis.modal_enabled", "analysis.modal_enabled", None),
        ("modal.modes", "analysis.modal_modes", None),
        ("analysis.static_nlgeom", "analysis.static_nlgeom", None),
        ("analysis.modal_prestress", "analysis.modal_prestress", None),
        ("boundary.name", "boundary.name", None),
    ):
        values.append((target, fem.get(source), unit))
    return values


def _valid_parameter_value(key: str, value: Any) -> bool:
    if key == "geometry.tower_sections":
        if not isinstance(value, list) or not value:
            return False
        previous_top = None
        for section in value:
            if not isinstance(section, dict):
                return False
            try:
                z0 = float(section["z_bottom_mm"])
                z1 = float(section["z_top_mm"])
                d0 = float(section["diameter_bottom_mm"])
                d1 = float(section["diameter_top_mm"])
                thickness = float(section["thickness_mm"])
            except (KeyError, TypeError, ValueError):
                return False
            if z1 <= z0 or min(d0, d1, thickness) <= 0:
                return False
            if previous_top is not None and not math.isclose(z0, previous_top, abs_tol=1e-6):
                return False
            previous_top = z1
        return True
    if key in {"geometry.door.enabled", "geometry.door.reinforcement_enabled", "analysis.static_enabled", "analysis.modal_enabled"}:
        return isinstance(value, bool)
    if key == "loads.wind_direction_vector":
        return isinstance(value, list) and len(value) >= 2 and any(abs(float(item)) > 1e-12 for item in value[:2])
    if key == "loads.wind_pressure_profile":
        return isinstance(value, list) and bool(value)
    if key == "material.poisson_ratio":
        return isinstance(value, (int, float)) and 0 <= float(value) < 0.5
    if key in {"loads.top_mass_tonne", "loads.center_of_mass_offset_mm", "loads.top_horizontal_force_n", "loads.wind_pressure_mpa"}:
        return isinstance(value, (int, float)) and float(value) >= 0
    if key.endswith(("_mm", "_mpa")) or key in {"material.density", "analysis.modal_modes"}:
        return isinstance(value, (int, float)) and float(value) > 0
    return value is not None and str(value).strip() != ""


def _standard_package_from_values(values: dict[str, Any], batch: InputBatch) -> dict[str, Any]:
    active_modules = set(batch.model_composition.get("active_modules") or [])
    door_enabled = values.get("geometry.door.enabled") if "door_opening" in active_modules else False
    door = {"enabled": door_enabled}
    if door_enabled is True:
        door.update({
            "shape": "rounded_rectangle",
            "width_mm": values.get("geometry.door.width_mm"),
            "height_mm": values.get("geometry.door.height_mm"),
            "z_bottom_mm": values.get("geometry.door.bottom_z_mm"),
            "azimuth_deg": values.get("geometry.door.theta_center_deg"),
            "corner_radius_mm": values.get("geometry.door.corner_radius_mm") or 0,
            "reinforcement_enabled": values.get("geometry.door.reinforcement_enabled"),
            "outer_width_mm": values.get("geometry.door.outer_width_mm"),
            "outer_height_mm": values.get("geometry.door.outer_height_mm"),
            "frame_thickness_mm": values.get("geometry.door.frame_thickness_mm"),
            "reinforcement_material": values.get("geometry.door.reinforcement_material"),
        })
    material = {
        "name": values.get("material.name"),
        "elastic_modulus_mpa": values.get("material.elastic_modulus_mpa"),
        "poisson_ratio": values.get("material.poisson_ratio"),
        "density_tonne_mm3": values.get("material.density"),
        "yield_strength_mpa": values.get("material.yield_strength_mpa"),
    }
    fem = {
        "material.name": material["name"],
        "material.elastic_modulus_mpa": material["elastic_modulus_mpa"],
        "material.poisson_ratio": material["poisson_ratio"],
        "material.density_tonne_mm3": material["density_tonne_mm3"],
        "material.yield_strength_mpa": material["yield_strength_mpa"],
        "section.type": "Shell Section",
        "section.thickness_source": "tower_sections",
        "section.material": material["name"],
        "element.element_type": "S4R",
        "mesh.strategy": "structured_quad_shell",
        "mesh.global_size_mm": values.get("mesh.global_size_mm"),
        "mesh.door_size_mm": values.get("mesh.door_size_mm") if door_enabled is True else 120.0,
        "mesh.patch_margin_u_mm": values.get("mesh.patch_margin_u_mm"),
        "mesh.patch_margin_z_mm": values.get("mesh.patch_margin_z_mm"),
        "mesh.hole_layers": values.get("mesh.hole_layers"),
        "loads.top_mass_tonne": values.get("loads.top_mass_tonne"),
        "loads.center_of_mass_offset_mm": values.get("loads.center_of_mass_offset_mm"),
        "loads.top_total_force_n": values.get("loads.top_horizontal_force_n"),
        "loads.top_force_direction": values.get("loads.top_force_direction"),
        "loads.wind_pressure_mpa": values.get("loads.wind_pressure_mpa"),
        "loads.wind_direction_vector": values.get("loads.wind_direction_vector"),
        "loads.wind_pressure_profile": values.get("loads.wind_pressure_profile"),
        "loads.top_force_parallel_n": values.get("loads.top_force_parallel_n"),
        "loads.top_force_transverse_n": values.get("loads.top_force_transverse_n"),
        "loads.top_force_vertical_n": values.get("loads.top_force_vertical_n"),
        "loads.top_moment_roll_nmm": values.get("loads.top_moment_roll_nmm"),
        "loads.top_moment_pitch_nmm": values.get("loads.top_moment_pitch_nmm"),
        "loads.top_moment_yaw_nmm": values.get("loads.top_moment_yaw_nmm"),
        "loads.hub_vertical_offset_mm": values.get("loads.hub_vertical_offset_mm"),
        "analysis.static_enabled": values.get("analysis.static_enabled"),
        "analysis.modal_enabled": values.get("analysis.modal_enabled"),
        "modal.modes": values.get("analysis.modal_modes") if values.get("analysis.modal_enabled") is True else 12,
        "boundary.name": values.get("boundary.name"),
        "boundary.target": "BASE_FIXED",
        "boundary.dofs": [1, 2, 3, 4, 5, 6],
        "outputs.field": values.get("analysis.output_fields"),
    }
    fem = {key: value for key, value in fem.items() if value is not None}
    return {
        "schema_version": "standard_input_package_v1",
        "source_file": f"{batch.batch_id}-evidence",
        "input_format": f"{batch.parse_mode}_multi_file",
        "unit_system": "mm-N-MPa-tonne-s",
        "tower_segments": deepcopy(values.get("geometry.tower_sections") or []),
        "door_opening": door,
        "materials": [material],
        "fem_settings": fem,
        "source_trace": {
            "parser_name": f"engineering_input_batch_{batch.parse_mode}_v2",
            "source_files": [item.filename for item in batch.files],
            "batch_id": batch.batch_id,
        },
    }


def _standard_json_import(filename: str, content: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    package = payload.get("standard_input_package") or payload.get("standard_input_candidate")
    if not isinstance(package, dict):
        return None
    fem_source = payload.get("fem_params") or package.get("fem_settings") or {}
    fem_params = {key: _source_value(value) for key, value in fem_source.items()}
    validation = validate_standard_input({"standard_input_package": package, "fem_params": fem_params})
    if validation.get("blocked"):
        issues = "; ".join(str(item.get("field") or item.get("path") or item.get("message")) for item in validation.get("issues", [])[:5])
        raise ValueError(f"JSON 中的 StandardInputPackage 未通过校验：{issues}")
    return _import_from_package(filename, package, validation)


def _import_from_package(filename: str, package: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    fem_source = package.get("fem_settings") or {}
    fem_params = {key: _source_value(value) for key, value in fem_source.items()}
    segments = []
    for index, raw in enumerate(package.get("tower_segments") or []):
        segment = {key: _source_value(raw.get(key)) for key in (
            "z_bottom_mm", "z_top_mm", "diameter_bottom_mm", "diameter_top_mm", "thickness_mm"
        )}
        segment["source"] = str(raw.get("source") or f"{filename}:$.standard_input_package.tower_segments[{index}]")
        segments.append(segment)
    tower = []
    if segments:
        first = segments[0]
        tower.append({
            "index": 0,
            "z_mm": first["z_bottom_mm"],
            "diameter_mm": first["diameter_bottom_mm"],
            "thickness_mm": first["thickness_mm"],
            "source": first["source"],
        })
        for index, segment in enumerate(segments, start=1):
            tower.append({
                "index": index,
                "z_mm": segment["z_top_mm"],
                "diameter_mm": segment["diameter_top_mm"],
                "thickness_mm": segment["thickness_mm"],
                "source": segment["source"],
            })
    door_source = package.get("door_opening") or {}
    if _source_value(door_source.get("enabled")) is False:
        door = {}
    else:
        door = {
            "X2": _source_value(door_source.get("width_mm")),
            "Y2": _source_value(door_source.get("height_mm")),
            "H": _source_value(door_source.get("z_bottom_mm")),
            "R": _source_value(door_source.get("corner_radius_mm")) or 0,
            "theta_center_deg": _source_value(door_source.get("azimuth_deg")) or 0,
            "shape": str(_source_value(door_source.get("shape")) or "rounded_rectangle").lower(),
            "enabled": True,
        }
    materials = package.get("materials") or []
    if materials:
        material = materials[0]
        aliases = {
            "name": "material.name",
            "elastic_modulus_mpa": "material.elastic_modulus_mpa",
            "poisson_ratio": "material.poisson_ratio",
            "density_tonne_mm3": "material.density_tonne_mm3",
            "yield_strength_mpa": "material.yield_strength_mpa",
        }
        for source_key, target_key in aliases.items():
            value = _source_value(material.get(source_key))
            if value is not None:
                fem_params.setdefault(target_key, value)
    loads_inventory = {
        "source": f"{filename}:standard_input_package",
        "top_total_force_n": fem_params.get("loads.top_total_force_n"),
        "wind_pressure_mpa": fem_params.get("loads.wind_pressure_mpa"),
        "ultimate_cases": package.get("load_cases") or [],
        "warnings": [],
    }
    return {
        "source_file": filename,
        "source_files": [filename],
        "source_file_original": filename,
        "source_file_storage": filename,
        "detected_format": "standard_input_json",
        "input_format": "standard_input_json",
        "format_confidence": 1.0,
        "parse_strategy": "deterministic_standard_json",
        "standard_input_package": package,
        "standard_input_candidate": package,
        "standard_input_validation": validation,
        "tower": tower,
        "flanges": package.get("flanges") or [],
        "door": door,
        "fem_params": fem_params,
        "fem_param_records": [],
        "materials": materials,
        "loads_inventory": loads_inventory,
        "missing_items": validation.get("missing_items") or [],
        "warnings": [item.get("message") for item in validation.get("warnings") or []],
        "parse_diagnostics": {"parser_name": "standard_input_json_v1", "validation": validation},
    }


def _source_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _infer_modeling_intent(
    modeling_goal: str | None,
    parameters: dict[str, Any],
    *,
    template_id: str,
    confirmed: bool = False,
) -> dict[str, Any]:
    goal = (modeling_goal or "").lower()
    tasks = []
    if "模态" in goal or "modal" in goal or "frequency" in goal:
        tasks.append("modal")
    if "静力" in goal or "static" in goal or not tasks:
        tasks.append("static")
    door_enabled = bool(parameters.get("geometry.door.enabled", True))
    return {
        "schema_version": "modeling_intent_v1",
        "model_family": "wind_tower",
        "analysis_tasks": tasks,
        "requested_features": [
            "tower_shell",
            *(["door_opening"] if door_enabled else []),
        ],
        "output_targets": ["inp", "odb"],
        "confirmation_status": "confirmed" if confirmed else "pending",
        "template_id": template_id,
    }


def _method_rank(method: str) -> int:
    return {
        "deterministic_excel": 5,
        "deterministic_reference_inp": 4,
        "deterministic": 3,
        "local_pattern": 2,
        "llm_mapping": 1,
    }.get(method, 0)


def _candidate_rank(candidate: ParameterCandidate) -> tuple[int, int, float]:
    source_rank = {
        "engineer_input": 7,
        "goal_explicit": 6,
        "file_observed": 5,
        "reference_model_value": 4,
        "model_library_reference": 3,
        "llm_mapped": 2,
        "llm_suggested": 1,
        "system_setting": 0,
    }.get(candidate.source_kind, 0)
    return source_rank, _method_rank(candidate.extraction_method), candidate.confidence


def _fem_source_branch(origin: str) -> str:
    if origin in {"reference_model", "model_library"}:
        return "reference_model"
    if origin in {"llm_suggestion", "llm_mapped", "llm_suggested"}:
        return "llm_recommendation"
    if origin == "engineer_input":
        return "engineer_input"
    if origin == "capability_default":
        return "capability_profile"
    return "source_data"


def _safe_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in Path(name).name)[:180]


def _now() -> str:
    return utc_now()


def _capability_match(
    summary: dict[str, Any] | None,
    capability_id: str = CAPABILITY_ID,
) -> dict[str, Any]:
    capability = get_capability(capability_id)
    if not summary:
        return {
            "schema_version": "capability_match_v1",
            "capability_id": capability["capability_id"],
            "status": "compatible",
            "mode": "capability_default",
            "reasons": [],
            "warnings": ["未提供参考 INP，FEM 参数采用能力配置候选并等待工程师确认。"],
        }
    compatibility = str(summary.get("compatibility") or "incompatible")
    return {
        "schema_version": "capability_match_v1",
        "capability_id": capability["capability_id"],
        "status": compatibility,
        "mode": "reference_model",
        "reasons": list(summary.get("compatibility_reasons") or []),
        "warnings": list(summary.get("warnings") or []),
    }


def _instruction_parameter_changes(text: str) -> list[ParameterChange]:
    patterns = (
        ("geometry.door.height_mm", r"(?:门洞|door).{0,12}(?:高度|height).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("geometry.door.width_mm", r"(?:门洞|door).{0,12}(?:宽度|width).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("mesh.global_size_mm", r"(?:全局网格|global\s+mesh).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("mesh.door_size_mm", r"(?:门洞网格|door\s+(?:local\s+)?mesh).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("material.elastic_modulus_mpa", r"(?:弹性模量|elastic\s+modulus).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("material.poisson_ratio", r"(?:泊松比|poisson(?:'s)?\s+ratio).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("loads.top_mass_tonne", r"(?:顶部质量|top\s+mass).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("loads.center_of_mass_offset_mm", r"(?:质心偏心距|偏心距|center\s+of\s+mass\s+offset|mass\s+offset).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("loads.wind_pressure_mpa", r"(?:风压|wind\s+pressure).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("analysis.modal_modes", r"(?:模态阶数|modal\s+modes?).{0,12}?(\d+)"),
    )
    changes = []
    for key, pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            changes.append(ParameterChange(key=key, value=float(match.group(1))))
    return changes
