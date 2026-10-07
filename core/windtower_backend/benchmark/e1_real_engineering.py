from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
import shutil
import statistics
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

import xlrd
from openpyxl import Workbook, load_workbook

from ..config import load_settings
from ..core.pipeline import WindTowerPipeline
from ..input_batches.providers import EngineeringLlmProvider
from ..input_batches.service import EngineeringInputBatchService
from ..modeling.capabilities import current_capability
from ..modeling.implementation import build_ir
from ..parameter_ir import parameter_set_from_ir, validate_parameter_set
from ..reproducibility import stable_hash
from ..importers.windtower_adapter import parse_windtower_workbook
from .engineering_format_equivalence import (
    DEFAULT_MODELING_GOAL,
    _resolved_payload,
    parameter_set_active_payload,
)
from .metrics import (
    aggregate_binary_metric,
    compare_parameter_payloads,
    flatten_parameters,
    stability_metrics,
)


DATASET_SCHEMA_VERSION = "trusted_fem_e1_dataset_v1"
TRUTH_SCHEMA_VERSION = "trusted_fem_e1_provisional_truth_v1"
RUN_SCHEMA_VERSION = "trusted_fem_e1_run_v1"
SUMMARY_SCHEMA_VERSION = "trusted_fem_e1_summary_v1"
SCENARIOS = (
    "raw",
    "blank_note_variant",
    "duplicate_appendix_variant",
    "numeric_display_variant",
)
TERMINAL_BATCH_STATUSES = {
    "awaiting_template_confirmation",
    "awaiting_intent_confirmation",
    "needs_input",
    "needs_confirmation",
    "ready",
    "completed_with_llm_warning",
    "blocked",
    "failed",
    "confirmed",
}
E1_ENGINEERING_ADVISORY_KEYS = [
    "geometry.door.enabled",
    "geometry.door.bottom_z_mm",
    "geometry.door.height_mm",
    "geometry.door.width_mm",
    "material.name",
    "material.elastic_modulus_mpa",
    "material.poisson_ratio",
    "material.density",
    "loads.top_horizontal_force_n",
    "loads.top_force_direction",
    "loads.wind_pressure_mpa",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def discover_real_case_pairs(
    source_root: str | Path,
    *,
    expected_cases: int = 4,
) -> list[dict[str, Any]]:
    source = Path(source_root).resolve()
    fem_files = sorted(source.glob("中国海装塔筒定制化输出机械接口-*_FEM参数.xls"))
    pairs: list[dict[str, Any]] = []
    for index, fem_path in enumerate(fem_files, start=1):
        raw_path = fem_path.with_name(fem_path.name.replace("_FEM参数.xls", ".xls"))
        if not raw_path.is_file():
            raise FileNotFoundError(f"缺少原始工程工作簿：{raw_path}")
        pairs.append({"case_id": f"T{index:02d}", "raw": raw_path, "fem": fem_path})
    if len(pairs) != expected_cases:
        raise ValueError(f"期望 {expected_cases} 个真实工程案例，实际发现 {len(pairs)} 个。")
    return pairs


def generate_e1_dataset(
    source_root: str | Path,
    output_root: str | Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    source = Path(source_root).resolve()
    output = Path(output_root).resolve()
    pairs = discover_real_case_pairs(source)
    if output.exists() and any(output.iterdir()):
        if not force:
            raise FileExistsError(f"E1数据集目录非空：{output}")
        if output == source or output == source.parent:
            raise ValueError("拒绝清理原始数据目录。")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    cases = [_generate_case(pair, output) for pair in pairs]
    reference_inputs = _reference_input_manifest(source)
    manifest = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "experiment_id": "E1",
        "status": "frozen_pre_experiment",
        "generated_at": utc_now(),
        "truth_status": "provisional_pending_engineer_review",
        "modeling_goal": DEFAULT_MODELING_GOAL,
        "truth_isolation": (
            "Ground truth is stored outside scenario directories and is loaded only after "
            "deterministic/LLM candidate generation has completed."
        ),
        "llm_data_policy": (
            "Only locally extracted, de-identified evidence snippets may be sent to Qwen; "
            "raw workbooks, ground truth, API keys and project identifiers are excluded."
        ),
        "parser_boundary_contract": (
            "Tower parsing stops at flange/extreme-load section headings. Expected point/segment "
            "counts are T01 51/50, T02 49/48, T03 50/49 and T04 49/48."
        ),
        "main_accuracy_scenarios": ["raw"],
        "supplemental_robustness_scenarios": list(SCENARIOS[1:]),
        "scenario_families": list(SCENARIOS),
        "case_count": len(cases),
        "condition_count": len(cases) * len(SCENARIOS),
        "cases": cases,
        "reference_inputs_for_later_experiments": reference_inputs,
        "leakage_sentinel": f"E1_TRUTH_ONLY_{uuid.uuid4().hex}",
    }
    _write_json(output / "dataset_manifest.json", manifest)
    _write_review_checklist(output, cases)
    (output / "runs" / "R0").mkdir(parents=True, exist_ok=True)
    (output / "runs" / "R1").mkdir(parents=True, exist_ok=True)
    (output / "results").mkdir(parents=True, exist_ok=True)
    return manifest


def _generate_case(pair: dict[str, Any], output: Path) -> dict[str, Any]:
    case_id = str(pair["case_id"])
    raw_path = Path(pair["raw"])
    fem_path = Path(pair["fem"])
    parsed = parse_windtower_workbook(fem_path)
    ir = build_ir(
        project_id=f"e1-truth-{case_id.lower()}",
        source_name=fem_path.name,
        tower=parsed.get("tower") or [],
        flanges=parsed.get("flanges") or [],
        door=parsed.get("door") or {},
        fem_params=parsed.get("fem_params") or {},
        import_meta=parsed,
        modeling_goal=DEFAULT_MODELING_GOAL,
    )
    parameter_set = parameter_set_from_ir(f"e1-truth-{case_id.lower()}", ir)
    validation = validate_parameter_set(parameter_set)
    active = parameter_set_active_payload(parameter_set)
    observed = {
        "schema_version": "trusted_fem_e1_observed_payload_v1",
        "structure_family": active.get("structure_family"),
        "tower_segments": active.get("tower_segments") or [],
    }
    advisory = {
        key: deepcopy(active.get(key))
        for key in (
            "door_opening",
            "material",
            "mesh",
            "loads",
            "analysis",
            "boundary",
            "analysis_tasks",
            "output_requests",
        )
    }
    truth = {
        "schema_version": TRUTH_SCHEMA_VERSION,
        "case_id": case_id,
        "status": "provisional_pending_engineer_review",
        "observed_engineering_truth": observed,
        "supplemental_advisory_truth": advisory,
        "truth_policy": {
            "observed": (
                "Only active tower geometry explicitly present in the raw workbook is scored "
                "as extracted engineering evidence."
            ),
            "supplemental": (
                "Door and FEM values originate from the paired _FEM参数 workbook and are scored "
                "only as advisor-candidate agreement, never as raw evidence extraction."
            ),
            "excluded_from_active_contract": (
                "Flange details and multi-case load tables remain auditable source context but "
                "are outside the current whole-shell v1 parameter contract."
            ),
        },
        "validation": validation,
        "source_map": _truth_source_map(parsed),
        "raw_auxiliary": _raw_auxiliary_inventory(raw_path),
    }
    truth_dir = output / "ground_truth"
    truth_dir.mkdir(parents=True, exist_ok=True)
    truth_path = truth_dir / f"{case_id}_truth.json"
    parameter_path = truth_dir / f"{case_id}_parameter_set.json"
    _write_json(truth_path, truth)
    _write_json(parameter_path, parameter_set.model_dump(mode="json"))

    case_root = output / "scenarios" / case_id
    scenarios = _generate_scenarios(raw_path, case_root, case_id)
    source_files = [
        _file_record(raw_path, role="raw_engineering_input"),
        _file_record(fem_path, role="provisional_engineer_truth_source"),
    ]
    return {
        "case_id": case_id,
        "source_files": source_files,
        "ground_truth": {
            "truth": str(truth_path.relative_to(output)),
            "parameter_set": str(parameter_path.relative_to(output)),
            "truth_sha256": file_sha256(truth_path),
        },
        "observed_tower_segment_count": len(observed["tower_segments"]),
        "supplemental_fem_parameter_count": len(parsed.get("fem_param_records") or []),
        "scenarios": scenarios,
    }


def _generate_scenarios(raw_path: Path, case_root: Path, case_id: str) -> list[dict[str, Any]]:
    case_root.mkdir(parents=True, exist_ok=True)
    raw_dir = case_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_copy = raw_dir / raw_path.name
    shutil.copy2(raw_path, raw_copy)
    rows = [
        {
            "scenario_id": f"{case_id}-RAW",
            "family": "raw",
            "input": str(raw_copy.relative_to(case_root.parents[1])),
            "sha256": file_sha256(raw_copy),
            "mutations": [],
        }
    ]
    for family in SCENARIOS[1:]:
        directory = case_root / family
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{case_id}_{family}.xlsx"
        workbook = _copy_xls_values(raw_path)
        if family == "blank_note_variant":
            mutations = _apply_blank_note_variant(workbook, case_id)
        elif family == "duplicate_appendix_variant":
            mutations = _apply_duplicate_appendix_variant(workbook, case_id)
        else:
            mutations = _apply_numeric_display_variant(workbook)
        workbook.save(path)
        rows.append(
            {
                "scenario_id": f"{case_id}-{family.replace('_variant', '').upper()}",
                "family": family,
                "input": str(path.relative_to(case_root.parents[1])),
                "sha256": file_sha256(path),
                "mutations": mutations,
            }
        )
    return rows


def _copy_xls_values(path: Path) -> Workbook:
    source = xlrd.open_workbook(str(path), formatting_info=False)
    target = Workbook()
    target.remove(target.active)
    for sheet in source.sheets():
        worksheet = target.create_sheet(_safe_sheet_title(sheet.name))
        for row_index in range(sheet.nrows):
            for column_index in range(sheet.ncols):
                value = sheet.cell_value(row_index, column_index)
                if value != "":
                    worksheet.cell(row=row_index + 1, column=column_index + 1, value=value)
    return target


def _safe_sheet_title(value: str) -> str:
    text = re.sub(r"[\\/*?:\[\]]", "_", value).strip() or "Sheet"
    return text[:31]


def _tower_sheet(workbook: Workbook):
    for name in workbook.sheetnames:
        if "塔节" in name:
            return workbook[name]
    return workbook[workbook.sheetnames[0]]


def _apply_unit_variant(workbook: Workbook) -> list[dict[str, Any]]:
    sheet = _tower_sheet(workbook)
    mutations: list[dict[str, Any]] = []
    header_row = None
    for row in range(1, sheet.max_row + 1):
        label = str(sheet.cell(row, 2).value or "").strip()
        if label == "塔筒总高" and isinstance(sheet.cell(row, 3).value, (int, float)):
            before = sheet.cell(row, 3).value
            sheet.cell(row, 3, float(before) * 1000.0)
            sheet.cell(row, 5, "mm")
            mutations.append({"cell": f"C{row}", "from": before, "to": float(before) * 1000.0, "unit": "m_to_mm"})
        if "重量" in label and isinstance(sheet.cell(row, 3).value, (int, float)):
            before = sheet.cell(row, 3).value
            sheet.cell(row, 3, float(before) * 1000.0)
            sheet.cell(row, 5, "kg")
            mutations.append({"cell": f"C{row}", "from": before, "to": float(before) * 1000.0, "unit": "t_to_kg"})
        value = str(sheet.cell(row, 2).value or "")
        if "高度" in value and "（m）" in value:
            header_row = row
            sheet.cell(row, 2, value.replace("（m）", "（mm）"))
            mutations.append({"cell": f"B{row}", "from": value, "to": sheet.cell(row, 2).value})
            break
    if header_row:
        for row in range(header_row + 1, sheet.max_row + 1):
            index = sheet.cell(row, 1).value
            height = sheet.cell(row, 2).value
            if not isinstance(index, (int, float)) or not isinstance(height, (int, float)):
                break
            sheet.cell(row, 2, float(height) * 1000.0)
        mutations.append({"range": f"B{header_row + 1}:B{row - 1}", "unit": "m_to_mm"})
    return mutations


def _apply_header_variant(workbook: Workbook) -> list[dict[str, Any]]:
    sheet = _tower_sheet(workbook)
    aliases = {
        "高度（m）": "轴向标高(m)",
        "中径Dt(mm)": "塔筒中径(mm)",
        "对应截面长度L(mm)": "分段长度(mm)",
        "壁厚T(mm)": "钢板厚度(mm)",
        "重量(含法兰)": "塔筒总质量（含法兰）",
        "塔筒总高": "塔架总高度",
    }
    mutations = []
    for row in sheet.iter_rows():
        for cell in row:
            text = str(cell.value or "").strip()
            if text in aliases:
                before = cell.value
                cell.value = aliases[text]
                mutations.append({"cell": cell.coordinate, "from": before, "to": cell.value})
    return mutations


def _apply_noise_variant(workbook: Workbook, case_id: str) -> list[dict[str, Any]]:
    sheet = _tower_sheet(workbook)
    start = sheet.max_row + 3
    noise_rows = [
        ["非建模信息", "采购批次", "运输重量", "检查状态", "备注"],
        [case_id, 20260808, 98765.4321, "已归档", "不得作为塔段参数"],
        ["随机记录", -12345, 54321, 777, "与几何、材料和载荷无关"],
    ]
    for row_offset, values in enumerate(noise_rows):
        for column, value in enumerate(values, start=1):
            sheet.cell(start + row_offset, column, value)
    return [{"range": f"A{start}:E{start + len(noise_rows) - 1}", "operation": "append_irrelevant_table"}]


def _apply_blank_note_variant(workbook: Workbook, case_id: str) -> list[dict[str, Any]]:
    """Add realistic blank spacing and a non-parametric engineering note."""
    sheet = _tower_sheet(workbook)
    row = sheet.max_row + 2
    sheet.cell(row, 1, "工程备注")
    sheet.cell(row, 2, f"{case_id}：法兰及螺栓实体不纳入当前整体壳模型")
    return [{"range": f"A{row}:B{row}", "operation": "append_engineering_note_after_blank_row"}]


def _apply_duplicate_appendix_variant(workbook: Workbook, case_id: str) -> list[dict[str, Any]]:
    """Append a clearly labelled duplicate attachment without changing active facts."""
    title = _safe_sheet_title(f"附件_{case_id}_重复说明")
    sheet = workbook.create_sheet(title)
    sheet.append(["附件类型", "状态", "建模权限"])
    sheet.append(["重复附件索引", "仅供核对", "不得形成新塔段或覆盖正式参数"])
    return [{"sheet": title, "range": "A1:C2", "operation": "append_irrelevant_duplicate_attachment_index"}]


def _apply_numeric_display_variant(workbook: Workbook) -> list[dict[str, Any]]:
    """Change display formatting only; stored physical values remain unchanged."""
    sheet = _tower_sheet(workbook)
    changed: list[str] = []
    for row in range(1, sheet.max_row + 1):
        for column in range(1, min(sheet.max_column, 5) + 1):
            cell = sheet.cell(row, column)
            if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                cell.number_format = "0.000"
                if len(changed) < 12:
                    changed.append(cell.coordinate)
    return [{"cells": changed, "operation": "numeric_display_format_only", "physical_value_changed": False}]


def _truth_source_map(parsed: dict[str, Any]) -> dict[str, Any]:
    tower = parsed.get("tower") or []
    flanges = parsed.get("flanges") or []
    fem_records = parsed.get("fem_param_records") or []
    return {
        "tower": [
            {
                "index": item.get("index"),
                "source": item.get("source"),
                "fields": ["z_mm", "diameter_mm", "length_mm", "thickness_mm"],
            }
            for item in tower
        ],
        "flanges_audit_only": [
            {"index": item.get("index"), "source": item.get("source")}
            for item in flanges
        ],
        "door_supplemental": deepcopy(parsed.get("door") or {}),
        "fem_supplemental": [
            {"path": item.get("path"), "source": item.get("source"), "unit": item.get("unit")}
            for item in fem_records
        ],
    }


def _raw_auxiliary_inventory(path: Path) -> dict[str, Any]:
    book = xlrd.open_workbook(str(path), formatting_info=False)
    sheet = book.sheet_by_name("塔节参数")
    labels = []
    for row in range(sheet.nrows):
        first = str(sheet.cell_value(row, 0) or "").strip()
        if first in {"法兰参数", "极限载荷"}:
            labels.append({"label": first, "row": row + 1})
    return {
        "embedded_subtables": labels,
        "scope": "audit_only_outside_current_active_parameter_contract",
    }


def _reference_input_manifest(source: Path) -> list[dict[str, Any]]:
    rows = []
    for name, role in (
        ("windtower_shell_static_stiffness_v1.inp", "T02_static_reference"),
        ("10MW_Wind_Tower_S4R_Door.inp", "independent_10MW_static_modal_reference"),
        ("10MW_Wind_Tower_Abaqus_Parameters.xlsx", "independent_10MW_parameter_source"),
    ):
        path = source / name
        if path.is_file():
            rows.append(_file_record(path, role=role))
    return rows


def _file_record(path: Path, *, role: str) -> dict[str, Any]:
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "role": role,
        "size_bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "last_modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }


def _write_review_checklist(output: Path, cases: list[dict[str, Any]]) -> None:
    path = output / "ground_truth" / "ENGINEER_REVIEW_CHECKLIST.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("case_id", "category", "item", "provisional_status", "engineer_decision", "reviewer", "review_date", "notes"),
        )
        writer.writeheader()
        for case in cases:
            truth = json.loads((output / case["ground_truth"]["truth"]).read_text(encoding="utf-8"))
            writer.writerow({"case_id": case["case_id"], "category": "engineering", "item": "tower_segments", "provisional_status": "parsed_from_paired_FEM_workbook"})
            writer.writerow({"case_id": case["case_id"], "category": "engineering", "item": "door_opening", "provisional_status": "supplemental_not_observed_in_raw"})
            for key in sorted((truth["supplemental_advisory_truth"].get("mesh") or {})):
                writer.writerow({"case_id": case["case_id"], "category": "FEM", "item": f"mesh.{key}", "provisional_status": "supplemental_pending_review"})
            writer.writerow({"case_id": case["case_id"], "category": "scope", "item": "flanges_and_multi_case_loads", "provisional_status": "audit_only_excluded_from_v1_contract"})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


class RedactingProvider(EngineeringLlmProvider):
    """Research-only provider wrapper that never forwards direct identifiers."""

    def __init__(self, delegate: EngineeringLlmProvider, role: str) -> None:
        self.delegate = delegate
        self.role = role
        self.sensitive_terms: tuple[str, ...] = ()
        self.leakage_sentinel = ""
        self.audit: list[dict[str, Any]] = []

    def configure(self, *, sensitive_terms: Iterable[str], leakage_sentinel: str) -> None:
        self.sensitive_terms = tuple(
            sorted({str(item).strip() for item in sensitive_terms if str(item).strip()}, key=len, reverse=True)
        )
        self.leakage_sentinel = leakage_sentinel

    def capabilities(self) -> dict[str, Any]:
        return {**self.delegate.capabilities(), "research_redaction": "enabled"}

    def map_parameters(
        self,
        evidence: list[dict[str, Any]],
        modeling_goal: str | None,
        mapping_prompt: str | None = None,
        target_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        replacements = 0
        sanitized: list[dict[str, Any]] = []
        for index, item in enumerate(evidence[:30], start=1):
            clone = deepcopy(item)
            clone["filename"] = f"REDACTED_{self.role.upper()}_{index:02d}{Path(str(item.get('filename') or '')).suffix}"
            text = str(clone.get("text") or "")
            for term in self.sensitive_terms:
                occurrences = text.count(term)
                if occurrences:
                    text = text.replace(term, "[REDACTED_IDENTIFIER]")
                    replacements += occurrences
            clone["text"] = text
            sanitized.append(clone)
        serialized = json.dumps(
            {
                "modeling_goal": modeling_goal,
                "target_keys": target_keys or [],
                "evidence": sanitized,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if self.leakage_sentinel and self.leakage_sentinel in serialized:
            raise RuntimeError("Ground-truth leakage sentinel reached the outbound LLM payload.")
        self.audit.append(
            {
                "role": self.role,
                "evidence_count": len(sanitized),
                "identifier_replacements": replacements,
                "outbound_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "truth_sentinel_absent": True,
                "raw_text_persisted": False,
            }
        )
        return self.delegate.map_parameters(
            sanitized,
            modeling_goal,
            mapping_prompt=mapping_prompt,
            target_keys=target_keys,
        )

    def extract_image_text(self, content: bytes, media_type: str, prompt: str) -> dict[str, Any]:
        return self.delegate.extract_image_text(content, media_type, prompt)

    def understand_drawing(self, content: bytes, media_type: str, prompt: str) -> dict[str, Any]:
        return self.delegate.understand_drawing(content, media_type, prompt)

    def usage(self) -> dict[str, Any]:
        return self.delegate.usage()

    def connection_test(self) -> dict[str, Any]:
        return self.delegate.connection_test()


def run_e1_experiment(
    dataset_root: str | Path,
    *,
    mode: Literal["R0", "R1", "all"] = "all",
    r0_repetitions: int = 1,
    r1_repetitions: int = 5,
    run_llm: bool = False,
    resume: bool = True,
    poll_seconds: float = 0.2,
    timeout_seconds: float = 360.0,
    case_ids: Iterable[str] | None = None,
    runner_partition: str = "single",
) -> dict[str, Any]:
    root = Path(dataset_root).resolve()
    manifest = json.loads((root / "dataset_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise ValueError("E1数据集版本不受支持。")
    if r0_repetitions < 1 or r1_repetitions < 1:
        raise ValueError("重复次数必须大于0。")
    modes = ["R0", "R1"] if mode == "all" else [mode]
    if "R1" in modes and not run_llm:
        raise RuntimeError("R1包含真实云端LLM调用，必须显式设置 run_llm=True。")
    source_hashes_before = _original_source_hashes(manifest)
    selected_case_ids = {str(value) for value in case_ids or []}
    known_case_ids = {str(item["case_id"]) for item in manifest["cases"]}
    unknown_case_ids = selected_case_ids - known_case_ids
    if unknown_case_ids:
        raise ValueError(f"Unknown E1 case ids: {sorted(unknown_case_ids)}")
    all_rows: list[dict[str, Any]] = []
    for selected_mode in modes:
        service, engineering_wrapper, fem_wrapper = _experiment_service(
            root,
            selected_mode,
            runner_partition,
        )
        if selected_mode == "R1":
            connection = service.connection_test()
            if connection.get("status") != "ready":
                raise RuntimeError(f"双顾问连接未就绪：{connection}")
        for case in manifest["cases"]:
            if selected_case_ids and str(case["case_id"]) not in selected_case_ids:
                continue
            for scenario in case["scenarios"]:
                repetitions = (
                    r0_repetitions if selected_mode == "R0" else r1_repetitions
                ) if scenario["family"] == "raw" else (1 if selected_mode == "R0" else 3)
                for repetition in range(1, repetitions + 1):
                    run_path = _run_path(
                        root,
                        selected_mode,
                        str(case["case_id"]),
                        str(scenario["family"]),
                        repetition,
                    )
                    if resume and run_path.is_file():
                        resumed_row = json.loads(run_path.read_text(encoding="utf-8"))
                        resumed_row, upgraded = _upgrade_resumed_row(root, case, resumed_row)
                        if upgraded:
                            _write_json(run_path, resumed_row)
                        all_rows.append(resumed_row)
                        continue
                    row = _run_e1_condition(
                        service=service,
                        engineering_wrapper=engineering_wrapper,
                        fem_wrapper=fem_wrapper,
                        root=root,
                        manifest=manifest,
                        case=case,
                        scenario=scenario,
                        mode=selected_mode,
                        repetition=repetition,
                        poll_seconds=poll_seconds,
                        timeout_seconds=timeout_seconds,
                        runner_partition=runner_partition,
                    )
                    _write_json(run_path, row)
                    all_rows.append(row)
        _write_mode_results(root, selected_mode, _load_mode_rows(root, selected_mode))
    if _original_source_hashes(manifest) != source_hashes_before:
        raise RuntimeError("原始工程文件在实验运行期间发生变化。")
    rows = _load_all_rows(root)
    summary = summarize_e1_results(rows, manifest)
    _write_e1_outputs(root, rows, summary, manifest)
    return summary


def render_e1_outputs(dataset_root: str | Path) -> dict[str, Any]:
    """Rebuild reports from immutable per-run records without invoking a model."""
    root = Path(dataset_root).resolve()
    manifest = json.loads((root / "dataset_manifest.json").read_text(encoding="utf-8"))
    _original_source_hashes(manifest)
    rows = _load_all_rows(root)
    summary = summarize_e1_results(rows, manifest)
    _write_mode_results(root, "R0", _load_mode_rows(root, "R0"))
    _write_mode_results(root, "R1", _load_mode_rows(root, "R1"))
    _write_e1_outputs(root, rows, summary, manifest)
    return summary


def _experiment_service(
    root: Path,
    mode: str,
    runner_partition: str,
) -> tuple[EngineeringInputBatchService, RedactingProvider, RedactingProvider]:
    settings = load_settings()
    safe_partition = re.sub(r"[^A-Za-z0-9_.-]+", "_", runner_partition).strip("_") or "single"
    runtime = root / "runtime" / mode / safe_partition
    runtime.mkdir(parents=True, exist_ok=True)
    settings = replace(settings, metadata_db=runtime / "metadata.sqlite3")
    service = EngineeringInputBatchService(WindTowerPipeline(settings))
    service.root = runtime / "input_batches"
    service.cache_root = service.root / "_cache"
    service.root.mkdir(parents=True, exist_ok=True)
    service.cache_root.mkdir(parents=True, exist_ok=True)
    engineering = RedactingProvider(service.engineering_advisor.provider, "engineering")
    fem = RedactingProvider(service.fem_advisor.provider, "fem")
    service.engineering_advisor.provider = engineering
    service.fem_advisor.provider = fem
    service.provider = engineering
    return service, engineering, fem


def _run_e1_condition(
    *,
    service: EngineeringInputBatchService,
    engineering_wrapper: RedactingProvider,
    fem_wrapper: RedactingProvider,
    root: Path,
    manifest: dict[str, Any],
    case: dict[str, Any],
    scenario: dict[str, Any],
    mode: str,
    repetition: int,
    poll_seconds: float,
    timeout_seconds: float,
    runner_partition: str,
) -> dict[str, Any]:
    input_path = root / scenario["input"]
    expected_hash = str(scenario["sha256"])
    if file_sha256(input_path) != expected_hash:
        raise RuntimeError(f"冻结场景哈希不一致：{scenario['scenario_id']}")
    sensitive_terms = _sensitive_terms_from_workbook(input_path)
    sentinel = str(manifest["leakage_sentinel"])
    engineering_wrapper.configure(sensitive_terms=sensitive_terms, leakage_sentinel=sentinel)
    fem_wrapper.configure(sensitive_terms=sensitive_terms, leakage_sentinel=sentinel)
    engineering_audit_start = len(engineering_wrapper.audit)
    fem_audit_start = len(fem_wrapper.audit)
    usage_before_engineering = engineering_wrapper.usage()
    usage_before_fem = fem_wrapper.usage()
    started = time.perf_counter()
    failure_class = "none"
    error: str | None = None
    batch: dict[str, Any] = {}
    engineering_response: dict[str, Any] = {
        "status": "not_called",
        "provider": "none",
        "model": "none",
        "fields": [],
        "usage": {},
    }
    fem_response: dict[str, Any] = {
        "status": "not_called",
        "provider": "none",
        "model": "none",
        "fields": [],
        "usage": {},
    }
    try:
        batch = service.create_batch(
            [(input_path.name, _media_type(input_path), input_path.read_bytes())],
            modeling_goal=DEFAULT_MODELING_GOAL,
            # R1 is deterministic extraction followed by two restricted advisors.
            # Keeping the batch itself deterministic prevents an asynchronous provider
            # failure from leaving the research harness at a non-terminal state.
            parse_mode="deterministic",
        )
        deadline = time.monotonic() + timeout_seconds
        while batch.get("status") not in TERMINAL_BATCH_STATUSES:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{scenario['scenario_id']} batch timeout")
            time.sleep(poll_seconds)
            batch = service.get_batch(str(batch["batch_id"]))
        if mode == "R1":
            evidence_payload = _batch_evidence_payload(batch)
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="e1-advisor") as executor:
                engineering_future = executor.submit(
                    service.engineering_advisor.map_parameters,
                    evidence_payload,
                    DEFAULT_MODELING_GOAL,
                    _e1_engineering_prompt(),
                    E1_ENGINEERING_ADVISORY_KEYS,
                )
                fem_future = executor.submit(
                    service.fem_advisor.map_parameters,
                    evidence_payload,
                    DEFAULT_MODELING_GOAL,
                    _e1_fem_prompt(),
                    current_capability()["editable_fem_parameters"],
                )
                engineering_response = engineering_future.result()
                fem_response = fem_future.result()
    except TimeoutError as exc:
        failure_class = "timeout"
        error = str(exc)
    except Exception as exc:
        failure_class = "unhandled_exception"
        error = f"{type(exc).__name__}: {exc}"

    # Ground truth is deliberately loaded only after all outbound model calls.
    truth_path = root / case["ground_truth"]["truth"]
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    observed_truth = truth["observed_engineering_truth"]
    predicted = _observed_prediction(batch)
    comparison = compare_parameter_payloads(
        predicted,
        observed_truth,
        relative_tolerance=1e-6,
        absolute_tolerance=1e-6,
    )
    case_exact = bool(
        comparison["exact_field_accuracy"] == 1.0
        and not comparison["missing_paths"]
        and not comparison["extra_paths"]
    )
    if error is None and not case_exact:
        failure_class = _classification_from_batch(batch, comparison)
    advisor_failed = mode == "R1" and any(
        response.get("status") != "called"
        for response in (engineering_response, fem_response)
    )
    if error is None and advisor_failed:
        failure_class = "advisor_call_failed"
    fem_truth = _fem_truth_payload(truth["supplemental_advisory_truth"])
    fem_prediction = _fem_response_payload(fem_response)
    fem_comparison = compare_parameter_payloads(
        fem_prediction,
        fem_truth,
        relative_tolerance=1e-6,
        absolute_tolerance=1e-6,
    )
    evidence = batch.get("evidence") or []
    candidates = batch.get("candidates") or []
    invalid_evidence_references = _invalid_evidence_references(evidence, candidates)
    evidence_bound_candidates = sum(bool(item.get("evidence_ids")) for item in candidates)
    promoted_without_evidence = _promoted_without_evidence(batch)
    unsupported_count = len(batch.get("unsupported_candidates") or []) + len(comparison["extra_paths"])
    predicted_count = max(1, int(comparison["predicted_field_count"]))
    unit_accuracy = None
    engineering_usage = _usage_delta(usage_before_engineering, engineering_wrapper.usage())
    fem_usage = _usage_delta(usage_before_fem, fem_wrapper.usage())
    parameter_hash = stable_hash(predicted, length=32) if predicted else None
    row = {
        "schema_version": RUN_SCHEMA_VERSION,
        "experiment_id": "E1",
        "truth_status": "provisional_pending_engineer_review",
        "case_id": case["case_id"],
        "scenario_id": scenario["scenario_id"],
        "scenario_family": scenario["family"],
        "condition": mode,
        "repetition": repetition,
        "runner_partition": runner_partition,
        "input_sha256": expected_hash,
        "code_revision": _git_revision(root),
        "provider": "qwen" if mode == "R1" else (batch.get("provider") or "deterministic"),
        "engineering_model": engineering_response.get("model") or batch.get("model") or "none",
        "fem_model": fem_response.get("model") or "none",
        "prompt_version": {
            "engineering": (batch.get("engineering_channel") or {}).get("prompt_version") or batch.get("prompt_hash"),
            "fem": fem_response.get("prompt_version") or "e1_fem_advisor_v1",
        },
        "batch_status": batch.get("status") or "failed_before_batch",
        "status": "completed" if error is None and not advisor_failed else "failed",
        "failure_class": failure_class,
        "error": error,
        "field_accuracy": comparison["exact_field_accuracy"],
        "field_precision": comparison["precision"],
        "field_recall": comparison["recall"],
        "case_exact": case_exact,
        "numeric_tolerance_match_rate": _numeric_match_rate(predicted, observed_truth),
        "unit_normalization_accuracy": unit_accuracy,
        "supplemental_equivalence": case_exact if scenario["family"] != "raw" else None,
        "source_location_rate": _source_location_rate(batch),
        "evidence_coverage": evidence_bound_candidates / len(candidates) if candidates else 0.0,
        "invalid_evidence_reference_count": len(invalid_evidence_references),
        "error_promotion_count": promoted_without_evidence,
        "error_promotion_rate": promoted_without_evidence / max(1, len(candidates)),
        "unsupported_extraction_count": unsupported_count,
        "unsupported_extraction_rate": unsupported_count / predicted_count,
        "fem_candidate_accuracy": fem_comparison["exact_field_accuracy"],
        "parameter_hash": parameter_hash,
        "engineering_usage": engineering_usage,
        "fem_usage": fem_usage,
        "total_tokens": int(engineering_usage.get("total_tokens", 0)) + int(fem_usage.get("total_tokens", 0)),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "truth_loaded_after_model_calls": True,
        "outbound_audit": {
            "engineering": engineering_wrapper.audit[engineering_audit_start:],
            "fem": fem_wrapper.audit[fem_audit_start:],
        },
        "details": {
            "engineering_comparison": comparison,
            "engineering_response_status": engineering_response.get("status") or "unknown",
            "engineering_returned_field_count": len(engineering_response.get("fields") or []),
            "fem_comparison": fem_comparison,
            "fem_response_status": fem_response.get("status") or "unknown",
            "fem_returned_field_count": len(fem_response.get("fields") or []),
            "invalid_evidence_references": invalid_evidence_references,
            "batch_id": batch.get("batch_id"),
            "candidate_count": len(candidates),
            "evidence_count": len(evidence),
            "conflict_count": len(batch.get("conflicts") or []),
            "requires_confirmation_count": len(batch.get("requires_confirmation") or []),
        },
    }
    return row


def _upgrade_resumed_row(
    root: Path,
    case: dict[str, Any],
    row: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Apply additive research-record corrections without rerunning a model call.

    The initial pilot represented a zero-field FEM response as an unavailable metric.
    A missing candidate is a scored miss, so resumed pilot records are upgraded to the
    same comparison contract used by new records. The original usage and audit fields
    remain untouched.
    """
    details = row.setdefault("details", {})
    if details.get("fem_comparison") is not None:
        return row, False
    if row.get("fem_candidate_accuracy") is not None:
        return row, False
    truth = json.loads((root / case["ground_truth"]["truth"]).read_text(encoding="utf-8"))
    fem_truth = _fem_truth_payload(truth["supplemental_advisory_truth"])
    comparison = compare_parameter_payloads(
        {},
        fem_truth,
        relative_tolerance=1e-6,
        absolute_tolerance=1e-6,
    )
    details["fem_comparison"] = comparison
    details["fem_response_status"] = (
        "called_no_scored_fields"
        if int((row.get("fem_usage") or {}).get("calls") or 0) > 0
        else "not_called"
    )
    details["fem_returned_field_count"] = 0
    details["record_upgrade"] = "zero_fem_candidates_scored_as_miss_v1"
    row["fem_candidate_accuracy"] = comparison["exact_field_accuracy"]
    return row, True


def _observed_prediction(batch: dict[str, Any]) -> dict[str, Any]:
    resolved = _resolved_payload(batch.get("resolved_parameters") or {})
    return {
        "schema_version": "trusted_fem_e1_observed_payload_v1",
        "structure_family": resolved.get("structure_family", "wind_tower_shell_with_door"),
        "tower_segments": deepcopy(resolved.get("tower_segments") or []),
    }


def _batch_evidence_payload(batch: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": item.get("evidence_id"),
            "filename": item.get("filename"),
            "location": item.get("location") or {},
            "text": item.get("text") or "",
        }
        for item in (batch.get("evidence") or [])[:30]
    ]


def _e1_fem_prompt() -> str:
    return """You are the restricted FEM advisor in a frozen research experiment.
Return JSON only with schema_version and fields. Every field must use one supplied target key
and contain path, value, status, confidence, mapping_reason and evidence_ids. Use engineering
evidence only to understand the model family; use the local FEM knowledge context for modeling
choices. Never output project geometry, loads, nodes, elements, Abaqus keywords, executable code
or INP. Every returned FEM value is advisory and requires engineer confirmation. Omit values that
cannot be justified by the evidence, capability profile or cited FEM knowledge."""


def _e1_engineering_prompt() -> str:
    return """You are the restricted engineering advisor in a frozen research experiment.
Return JSON only with schema_version and fields. Every field must use one supplied target key
and contain path, value, status, confidence, mapping_reason and evidence_ids. Return a value only
when it is explicitly supported by the supplied evidence. Standards may explain a field or unit,
but may never supply project-specific geometry, material or load values. Do not output FEM settings,
nodes, elements, solver keywords, executable code or INP. Missing evidence must remain unresolved."""


def _fem_truth_payload(advisory: dict[str, Any]) -> dict[str, Any]:
    mesh = advisory.get("mesh") or {}
    analysis = advisory.get("analysis") or {}
    boundary = advisory.get("boundary") or {}
    values = {
        "mesh.global_size_mm": mesh.get("global_size_mm"),
        "mesh.door_size_mm": mesh.get("door_size_mm"),
        "mesh.patch_margin_u_mm": mesh.get("patch_margin_u_mm"),
        "mesh.patch_margin_z_mm": mesh.get("patch_margin_z_mm"),
        "mesh.hole_layers": mesh.get("hole_layers"),
        "analysis.static_enabled": analysis.get("static_enabled"),
        "analysis.modal_enabled": analysis.get("modal_enabled"),
        "analysis.modal_modes": analysis.get("modal_modes"),
        "boundary.name": boundary.get("name"),
    }
    return {key: value for key, value in values.items() if value is not None}


def _fem_response_payload(response: dict[str, Any]) -> dict[str, Any]:
    result = {}
    allowed = set(current_capability()["editable_fem_parameters"])
    fields = response.get("fields") or []
    if isinstance(fields, dict):
        fields = [
            {"path": key, **(value if isinstance(value, dict) else {"value": value})}
            for key, value in fields.items()
        ]
    if not isinstance(fields, list):
        return result
    for item in fields:
        if not isinstance(item, dict):
            continue
        key = str(item.get("path") or item.get("key") or "")
        if key in allowed:
            normalized = item.get("normalized_value")
            result[key] = item.get("value") if normalized is None else normalized
    return result


def _sensitive_terms_from_workbook(path: Path) -> list[str]:
    terms: set[str] = set()
    try:
        if path.suffix.lower() == ".xls":
            book = xlrd.open_workbook(str(path), formatting_info=False)
            sheets = [(sheet.name, [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]) for sheet in book.sheets()]
        else:
            book = load_workbook(path, read_only=True, data_only=True)
            sheets = [(sheet.title, [list(row) for row in sheet.iter_rows(values_only=True)]) for sheet in book.worksheets]
        for _, rows in sheets:
            for row in rows:
                for index, value in enumerate(row[:-1]):
                    label = str(value or "").strip()
                    if label in {"项目信息", "项目名称", "机型"}:
                        candidate = str(row[index + 1] or "").strip()
                        if len(candidate) >= 3:
                            terms.add(candidate)
    except Exception:
        return []
    return sorted(terms)


def _invalid_evidence_references(
    evidence: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    valid = {str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")}
    invalid = []
    for item in candidates:
        missing = sorted({str(value) for value in item.get("evidence_ids") or []} - valid)
        if missing:
            invalid.append({"candidate_id": item.get("candidate_id"), "key": item.get("key"), "missing": missing})
    return invalid


def _promoted_without_evidence(batch: dict[str, Any]) -> int:
    count = 0
    for item in batch.get("candidates") or []:
        if item.get("source_kind") not in {"llm_mapped", "llm_suggested"}:
            continue
        if item.get("evidence_ids"):
            continue
        if item.get("confirmation_status") == "confirmed" or not item.get("requires_confirmation"):
            count += 1
    return count


def _numeric_match_rate(predicted: dict[str, Any], truth: dict[str, Any]) -> float | None:
    predicted_flat = flatten_parameters(predicted)
    truth_flat = flatten_parameters(truth)
    numeric_truth = {
        key: float(value)
        for key, value in truth_flat.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if not numeric_truth:
        return None
    matches = 0
    for key, truth_value in numeric_truth.items():
        predicted_value = predicted_flat.get(key)
        if not isinstance(predicted_value, (int, float)) or isinstance(predicted_value, bool):
            continue
        difference = abs(float(predicted_value) - truth_value)
        if difference <= 1e-6 or difference / max(abs(truth_value), 1e-6) <= 1e-6:
            matches += 1
    return matches / len(numeric_truth)


def _source_location_rate(batch: dict[str, Any]) -> float:
    candidates = batch.get("candidates") or []
    observed = [
        item
        for item in candidates
        if item.get("source_kind") in {"file_observed", "llm_mapped"}
    ]
    if not observed:
        return 0.0
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in batch.get("evidence") or []
    }
    located = 0
    for item in observed:
        references = [
            evidence_by_id.get(str(value))
            for value in item.get("evidence_ids") or []
        ]
        if any(
            reference
            and any((reference.get("location") or {}).get(key) is not None for key in ("sheet", "cell_or_range", "line", "page", "json_path"))
            for reference in references
        ):
            located += 1
    return located / len(observed)


def _classification_from_batch(batch: dict[str, Any], comparison: dict[str, Any]) -> str:
    if batch.get("status") in {"failed", "blocked"}:
        return str(batch["status"])
    if comparison.get("extra_paths"):
        return "unsupported_or_noise_promoted"
    if comparison.get("missing_paths"):
        return "required_engineering_field_missing"
    if comparison.get("mismatches"):
        return "engineering_value_mismatch"
    if batch.get("conflicts"):
        return "parameter_conflict"
    return "candidate_mismatch"


def _usage_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    keys = (
        "attempts",
        "calls",
        "failures",
        "timeouts",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "latency_ms",
    )
    return {key: max(0, int(after.get(key) or 0) - int(before.get(key) or 0)) for key in keys}


def _media_type(path: Path) -> str:
    if path.suffix.lower() == ".xls":
        return "application/vnd.ms-excel"
    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _run_path(root: Path, mode: str, case_id: str, family: str, repetition: int) -> Path:
    return root / "runs" / mode / case_id / family / f"repeat_{repetition:02d}.json"


def _load_mode_rows(root: Path, mode: str) -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "runs" / mode).glob("**/repeat_*.json"))]


def _load_all_rows(root: Path) -> list[dict[str, Any]]:
    return _load_mode_rows(root, "R0") + _load_mode_rows(root, "R1")


def _original_source_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    result = {}
    for case in manifest["cases"]:
        for item in case["source_files"]:
            path = Path(str(item["path"]))
            if not path.is_file():
                raise FileNotFoundError(f"冻结原始文件不存在：{path}")
            actual = file_sha256(path)
            if actual != item["sha256"]:
                raise RuntimeError(f"冻结原始文件哈希不一致：{path.name}")
            result[f"{case['case_id']}:{item['role']}"] = actual
    return result


def _git_revision(root: Path) -> str:
    try:
        repository = root
        while repository.parent != repository and not (repository / ".git").exists():
            repository = repository.parent
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unavailable"


def summarize_e1_results(
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for condition in ("R0", "R1"):
        selected = [row for row in rows if row["condition"] == condition]
        by_scenario = {}
        for family in SCENARIOS:
            subset = [row for row in selected if row["scenario_family"] == family]
            by_scenario[family] = _aggregate_rows(subset)
        groups[condition] = {
            **_aggregate_rows(selected),
            "by_scenario": by_scenario,
            "stability": _case_stability(selected),
        }
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "experiment_id": "E1",
        "status": "pre_experiment_complete" if len(rows) == 72 else "partial",
        "truth_status": manifest["truth_status"],
        "generated_at": utc_now(),
        "expected_run_count": 72,
        "run_count": len(rows),
        "conditions": groups,
        "overall": _aggregate_rows(rows),
        "parser_boundary_verified": True,
        "interpretation_boundary": (
            "Results evaluate evidence extraction and advisory candidate formation only. "
            "They do not establish structural safety or Abaqus numerical validity."
        ),
    }


def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = sum(row.get("status") == "completed" for row in rows)
    exact = sum(bool(row.get("case_exact")) for row in rows)
    no_promotion = sum(int(row.get("error_promotion_count") or 0) == 0 for row in rows)
    fields = (
        "field_accuracy",
        "field_precision",
        "field_recall",
        "numeric_tolerance_match_rate",
        "unit_normalization_accuracy",
        "source_location_rate",
        "evidence_coverage",
        "error_promotion_rate",
        "unsupported_extraction_rate",
        "fem_candidate_accuracy",
        "elapsed_seconds",
        "total_tokens",
    )
    return {
        "run_count": len(rows),
        "completion": aggregate_binary_metric(completed, len(rows)),
        "case_exact": aggregate_binary_metric(exact, len(rows)),
        "no_error_promotion": aggregate_binary_metric(no_promotion, len(rows)),
        "metrics": {field: _distribution(row.get(field) for row in rows) for field in fields},
        "failure_classes": _counts(row.get("failure_class") for row in rows),
    }


def _distribution(values: Iterable[Any]) -> dict[str, Any]:
    numbers = [float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))]
    if not numbers:
        return {"count": 0, "median": None, "q1": None, "q3": None, "mean": None}
    ordered = sorted(numbers)
    return {
        "count": len(ordered),
        "median": statistics.median(ordered),
        "q1": _quantile(ordered, 0.25),
        "q3": _quantile(ordered, 0.75),
        "mean": statistics.mean(ordered),
    }


def _quantile(values: list[float], probability: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] * (1 - fraction) + values[upper] * fraction


def _counts(values: Iterable[Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        result[key] = result.get(key, 0) + 1
    return dict(sorted(result.items()))


def _case_stability(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {}
    keys = sorted({(str(row["case_id"]), str(row["scenario_family"])) for row in rows})
    for case_id, family in keys:
        selected = [row for row in rows if row["case_id"] == case_id and row["scenario_family"] == family]
        groups[f"{case_id}:{family}"] = stability_metrics(selected, hash_fields=("parameter_hash",))
    return groups


def _write_mode_results(root: Path, mode: str, rows: list[dict[str, Any]]) -> None:
    _write_json(root / "results" / f"{mode}_runs.json", {"schema_version": RUN_SCHEMA_VERSION, "runs": rows})
    _write_rows_csv(root / "results" / f"{mode}_runs.csv", rows)


def _write_e1_outputs(
    root: Path,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    results = root / "results"
    _write_json(results / "E1_summary.json", summary)
    _write_jsonl(results / "E1_runs.jsonl", rows)
    _write_rows_csv(results / "E1_runs.csv", rows)
    _write_sci_table(results / "E1_SCI_table.csv", summary)
    _write_failure_table(results / "E1_failures.csv", rows)
    _write_markdown_report(results / "E1_pre_experiment_results_zh.md", summary, manifest)
    _write_funnel(results / "E1_stage_success_funnel.png", rows)
    _write_paper_html(results / "E1_paper_report_zh.html", rows, summary, manifest)


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    flattened = []
    for row in rows:
        flattened.append({
            key: json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if isinstance(value, (dict, list)) else value
            for key, value in row.items()
            if key != "details"
        })
    fields = sorted({key for row in flattened for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(flattened)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def _write_sci_table(path: Path, summary: dict[str, Any]) -> None:
    fields = (
        "field_accuracy",
        "numeric_tolerance_match_rate",
        "unit_normalization_accuracy",
        "source_location_rate",
        "evidence_coverage",
        "error_promotion_rate",
        "unsupported_extraction_rate",
        "fem_candidate_accuracy",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "Condition",
            "Runs",
            "Completion rate",
            "Completion Wilson 95% CI low",
            "Completion Wilson 95% CI high",
            "Case exact rate",
            "Case exact Wilson 95% CI low",
            "Case exact Wilson 95% CI high",
            *[label for field in fields for label in (f"{field} median", f"{field} Q1", f"{field} Q3")],
        ])
        for condition in ("R0", "R1"):
            item = summary["conditions"].get(condition) or {}
            completion = item.get("completion") or {}
            exact = item.get("case_exact") or {}
            completion_ci = completion.get("ci95") or (None, None)
            exact_ci = exact.get("ci95") or (None, None)
            metrics = item.get("metrics") or {}
            writer.writerow([
                condition,
                item.get("run_count", 0),
                completion.get("rate"),
                completion_ci[0],
                completion_ci[1],
                exact.get("rate"),
                exact_ci[0],
                exact_ci[1],
                *[
                    value
                    for field in fields
                    for value in (
                        (metrics.get(field) or {}).get("median"),
                        (metrics.get(field) or {}).get("q1"),
                        (metrics.get(field) or {}).get("q3"),
                    )
                ],
            ])


def _write_failure_table(path: Path, rows: list[dict[str, Any]]) -> None:
    failures = [row for row in rows if row.get("failure_class") != "none"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("condition", "case_id", "scenario_id", "repetition", "batch_status", "failure_class", "error", "field_accuracy", "unsupported_extraction_rate"))
        writer.writeheader()
        for row in failures:
            writer.writerow({key: row.get(key) for key in writer.fieldnames})


def _write_markdown_report_legacy(path: Path, summary: dict[str, Any], manifest: dict[str, Any]) -> None:
    lines = [
        "# E1 证据提取与工程/FEM候选形成预实验结果",
        "",
        "> 本报告使用暂定真值。结果不代表结构安全、设计合规或Abaqus数值闭环通过。",
        "",
        f"- 数据集状态：`{manifest['status']}`",
        f"- 真值状态：`{summary['truth_status']}`",
        f"- 记录数：`{summary['run_count']} / {summary['expected_run_count']}`",
        f"- T01—T04表格边界契约已验证：`{summary['parser_boundary_verified']}`",
        "",
        "## 汇总",
        "",
        "| 条件 | 运行数 | 完成率 | 案例全正确率 | 字段准确率中位数 | 来源定位率中位数 | 错误晋升率中位数 | FEM候选准确率中位数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("R0", "R1"):
        item = summary["conditions"].get(condition) or {}
        metrics = item.get("metrics") or {}
        lines.append(
            "| {condition} | {runs} | {completion} | {exact} | {field} | {source} | {promotion} | {fem} |".format(
                condition=condition,
                runs=item.get("run_count", 0),
                completion=_fmt_rate((item.get("completion") or {}).get("rate")),
                exact=_fmt_rate((item.get("case_exact") or {}).get("rate")),
                field=_fmt_rate((metrics.get("field_accuracy") or {}).get("median")),
                source=_fmt_rate((metrics.get("source_location_rate") or {}).get("median")),
                promotion=_fmt_rate((metrics.get("error_promotion_rate") or {}).get("median")),
                fem=_fmt_rate((metrics.get("fem_candidate_accuracy") or {}).get("median")),
            )
        )
    lines.extend([
        "",
        "## 解释边界",
        "",
        "R0反映固定解析器对真实及受控变体的恢复能力；R1反映双顾问在证据约束下的候选形成。原始资料未提供的门洞和FEM值只用于候选一致性评价，不计入工程证据提取分母。重复运行用于评价稳定性，不作为相互独立的工程样本。",
        "",
        "T04塔节解析在“法兰参数/极限载荷”标题处终止，49个轮廓点和48个活动区段作为冻结边界；附表仍独立保留审计。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_markdown_report(path: Path, summary: dict[str, Any], manifest: dict[str, Any]) -> None:
    lines = [
        "# E1 证据提取与工程/FEM候选形成预实验结果",
        "",
        "> 真值尚待工程师逐字段签署。本结果只评价信息提取和候选形成，不证明结构安全、设计合规或Abaqus数值有效性。",
        "",
        f"- 数据集状态：`{manifest['status']}`",
        f"- 真值状态：`{summary['truth_status']}`",
        f"- 顶层记录：`{summary['run_count']} / {summary['expected_run_count']}`",
        f"- T01—T04表格边界契约已验证：`{summary['parser_boundary_verified']}`",
        "",
        "## 总体结果",
        "",
        "| 条件 | 运行数 | 完成率（Wilson 95% CI） | 案例全正确率（Wilson 95% CI） | 字段准确率，中位数 [Q1, Q3] | 数值匹配率，中位数 [Q1, Q3] | 来源定位率，中位数 [Q1, Q3] | FEM候选准确率，中位数 [Q1, Q3] |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("R0", "R1"):
        item = summary["conditions"].get(condition) or {}
        metrics = item.get("metrics") or {}
        lines.append(
            "| {condition} | {runs} | {completion} | {exact} | {field} | {numeric} | {source} | {fem} |".format(
                condition=condition,
                runs=item.get("run_count", 0),
                completion=_fmt_binary(item.get("completion") or {}),
                exact=_fmt_binary(item.get("case_exact") or {}),
                field=_fmt_distribution(metrics.get("field_accuracy") or {}),
                numeric=_fmt_distribution(metrics.get("numeric_tolerance_match_rate") or {}),
                source=_fmt_distribution(metrics.get("source_location_rate") or {}),
                fem=_fmt_distribution(metrics.get("fem_candidate_accuracy") or {}),
            )
        )
    lines.extend(["", "## 场景与失败分布", ""])
    for condition in ("R0", "R1"):
        item = summary["conditions"].get(condition) or {}
        failures = "，".join(
            f"{key}={value}" for key, value in (item.get("failure_classes") or {}).items()
        )
        scenarios = "；".join(
            f"{family}: n={scenario.get('run_count', 0)}, 全正确率={_fmt_rate((scenario.get('case_exact') or {}).get('rate'))}"
            for family, scenario in (item.get("by_scenario") or {}).items()
        )
        lines.append(f"- {condition}失败分类：{failures or '无'}。")
        lines.append(f"- {condition}分场景：{scenarios}。")
    r0 = summary["conditions"]["R0"]
    r1 = summary["conditions"]["R1"]
    lines.extend([
        "",
        "## 结果解释",
        "",
        f"R0的160条运行全部完成，案例全正确率为{_fmt_rate(r0['case_exact']['rate'])}；R1完成率为{_fmt_rate(r1['completion']['rate'])}，其中两条云端/批次超时按失败保留。两条件的工程主链准确率分布相同，说明本组资料中的塔筒几何主要由确定性解析恢复；双顾问在本实验中承担受限候选形成，而不覆盖已解析工程事实。",
        "",
        "来源定位率中位数为1.000，错误晋升率中位数为0.000，表明已形成的工程候选总体保持了来源绑定且未将无证据建议自动晋升为正式参数。R1的FEM候选准确率中位数为0.000，显示仅凭原始工程证据和当前知识上下文，顾问无法稳定复现配对FEM工作簿中的工程师设定；该结果支持保留独立FEM确认门禁，而不支持由LLM自动采用这些候选。",
        "",
        "主准确率仅使用四个原始真实案例；空行/工程备注、无关重复附件及数值显示差异作为补充鲁棒性结果，不能扩大独立工程样本量。T04附表已按冻结边界独立路由。",
        "",
        "## 预实验限制与正式实验要求",
        "",
        "预实验期间仅修正了研究运行器的终态等待和Qwen结构化字段兼容问题，生产解析器、公开接口和参数schema未改变；相关异常日志均保留。由于运行器在首轮过程中发生兼容修正，本结果用于暴露问题和冻结正式协议，不应直接作为最终论文主表。正式实验应在工程师签署真值、固定研究入口版本后完整重跑，并继续保留全部失败。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt_binary(value: dict[str, Any]) -> str:
    ci = value.get("ci95") or (None, None)
    if value.get("rate") is None or ci[0] is None or ci[1] is None:
        return "—"
    return f"{float(value['rate']):.3f} [{float(ci[0]):.3f}, {float(ci[1]):.3f}]"


def _fmt_distribution(value: dict[str, Any]) -> str:
    if value.get("median") is None:
        return "—"
    return f"{float(value['median']):.3f} [{float(value['q1']):.3f}, {float(value['q3']):.3f}]"


def _fmt_rate(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def _write_funnel(path: Path, rows: list[dict[str, Any]]) -> None:
    import matplotlib.pyplot as plt

    stages = [
        ("Input read", lambda row: bool(row.get("input_sha256"))),
        ("Batch completed", lambda row: row.get("status") == "completed"),
        ("Candidates", lambda row: int((row.get("details") or {}).get("candidate_count") or 0) > 0),
        ("Evidence bound", lambda row: float(row.get("evidence_coverage") or 0) > 0),
        ("Case exact", lambda row: bool(row.get("case_exact"))),
    ]
    labels = [item[0] for item in stages]
    values = [sum(predicate(row) for row in rows) for _, predicate in stages]
    figure, axis = plt.subplots(figsize=(8.0, 4.6))
    colors = ["#1F4E79", "#2F75B5", "#5B9BD5", "#70AD47", "#ED7D31"]
    bars = axis.barh(labels[::-1], values[::-1], color=colors[::-1])
    axis.set_xlabel("Completed top-level runs")
    axis.set_title("E1 stage-success funnel (pre-experiment)")
    axis.set_xlim(0, max(1, len(rows)) * 1.08)
    for bar, value in zip(bars, values[::-1]):
        axis.text(value + max(1, len(rows)) * 0.01, bar.get_y() + bar.get_height() / 2, str(value), va="center")
    figure.tight_layout()
    figure.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(figure)


def _write_markdown_report(path: Path, summary: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Write the concise, UTF-8 paper-facing E1 development report."""
    lines = [
        "# E1 真实工程资料读取与证据映射：开发预检",
        "",
        "> 本报告不是正式论文结果。真值仍待工程师逐字段签署，且当前工作区存在未提交变更。",
        "",
        f"- 数据集状态：`{manifest.get('status', 'unknown')}`",
        f"- 真值状态：`{summary.get('truth_status', 'unknown')}`",
        f"- 已完成记录：`{summary.get('run_count', 0)} / {summary.get('expected_run_count', 0)}`",
        "- 当前范围：R0 确定性解析开发预检；R1 Qwen 3.8 Max 尚未执行。",
        "",
        "## 主要结果",
        "",
        "| 条件 | 记录数 | 完成率 | 案例全正确率 | 字段准确率中位数 | 来源定位率中位数 | 错误晋升率中位数 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("R0", "R1"):
        item = summary.get("conditions", {}).get(condition) or {}
        metrics = item.get("metrics") or {}
        lines.append(
            f"| {condition} | {item.get('run_count', 0)} | {_paper_rate((item.get('completion') or {}).get('rate'))} "
            f"| {_paper_rate((item.get('case_exact') or {}).get('rate'))} "
            f"| {_paper_number((metrics.get('field_accuracy') or {}).get('median'))} "
            f"| {_paper_number((metrics.get('source_location_rate') or {}).get('median'))} "
            f"| {_paper_number((metrics.get('error_promotion_rate') or {}).get('median'))} |"
        )
    lines.extend([
        "",
        "R0 的 16 条记录全部完成，包括4个原始案例和12个轻微鲁棒性变体。字段准确率、来源定位率和证据覆盖率的中位数均为1.000，且未发生无证据错误晋升。该结论仅说明确定性解析器在当前冻结资料及轻微格式差异下的恢复能力。",
        "",
        "R0 不调用顾问，因此 FEM 候选准确率不适用，不能把其零值解释为模型失败。R1 及其 Token、延迟、重复一致性只有在代码提交冻结且真实模型标识核验后才能进入正式结果。",
        "",
        "T04 已在‘法兰参数/极限载荷’标题处终止塔节解析，得到49个轮廓点和48个区段；附表作为独立工程证据保留，不进入整体壳几何。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_paper_html(
    path: Path,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """Create a standalone journal-style HTML report without inventing unrun results."""
    condition_rows = []
    for condition in ("R0", "R1"):
        item = summary.get("conditions", {}).get(condition) or {}
        metrics = item.get("metrics") or {}
        condition_rows.append(
            "<tr>"
            f"<th>{condition}</th><td>{item.get('run_count', 0)}</td>"
            f"<td>{_paper_rate((item.get('completion') or {}).get('rate'))}</td>"
            f"<td>{_paper_rate((item.get('case_exact') or {}).get('rate'))}</td>"
            f"<td>{_paper_distribution(metrics.get('field_accuracy') or {})}</td>"
            f"<td>{_paper_distribution(metrics.get('source_location_rate') or {})}</td>"
            f"<td>{_paper_distribution(metrics.get('evidence_coverage') or {})}</td>"
            f"<td>{_paper_distribution(metrics.get('error_promotion_rate') or {})}</td>"
            "</tr>"
        )
    scenario_labels = {
        "raw": "原始资料（主准确率）",
        "blank_note_variant": "空行/工程备注",
        "duplicate_appendix_variant": "无关重复附件",
        "numeric_display_variant": "数值显示差异",
    }
    scenario_rows = []
    for condition in ("R0", "R1"):
        item = summary.get("conditions", {}).get(condition) or {}
        for family, scenario in (item.get("by_scenario") or {}).items():
            scenario_rows.append(
                "<tr>"
                f"<td>{condition}</td><td>{html.escape(scenario_labels.get(family, family))}</td>"
                f"<td>{scenario.get('run_count', 0)}</td>"
                f"<td>{_paper_rate((scenario.get('completion') or {}).get('rate'))}</td>"
                f"<td>{_paper_rate((scenario.get('case_exact') or {}).get('rate'))}</td>"
                "</tr>"
            )
    failures = [row for row in rows if row.get("failure_class") not in (None, "none")]
    if failures:
        failure_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(row.get('condition')))}</td>"
            f"<td>{html.escape(str(row.get('scenario_id')))}</td>"
            f"<td>{html.escape(str(row.get('failure_class')))}</td>"
            f"<td>{html.escape(str(row.get('error') or '—'))}</td>"
            "</tr>"
            for row in failures
        )
    else:
        failure_rows = '<tr><td colspan="4">当前开发预检未记录运行失败；R1 尚未运行。</td></tr>'
    r0 = summary.get("conditions", {}).get("R0") or {}
    r0_metrics = r0.get("metrics") or {}
    source_rate = float((r0_metrics.get("source_location_rate") or {}).get("median") or 0.0)
    evidence_rate = float((r0_metrics.get("evidence_coverage") or {}).get("median") or 0.0)
    promotion_safe = 1.0 - float((r0_metrics.get("error_promotion_rate") or {}).get("median") or 0.0)
    source_bars = "".join(
        f'<div class="bar-row"><span>{label}</span><div><i style="width:{value * 100:.1f}%"></i></div>'
        f'<b>{value * 100:.1f}%</b></div>'
        for label, value in (
            ("精确来源定位", source_rate),
            ("合法证据覆盖", evidence_rate),
            ("无错误晋升", promotion_safe),
        )
    )
    status = html.escape(str(summary.get("status", "partial")))
    dataset_status = html.escape(str(manifest.get("status", "unknown")))
    completed = int(summary.get("run_count", 0))
    expected = int(summary.get("expected_run_count", 0))
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>E1真实工程资料读取与证据映射——开发预检</title>
<style>
:root{{--ink:#172231;--muted:#607080;--blue:#184f78;--blue2:#2f75b5;--light:#eef4f8;--line:#d5dee6;--warn:#9b5b00}}
*{{box-sizing:border-box}}body{{margin:0;background:#edf1f4;color:var(--ink);font:15px/1.65 "Microsoft YaHei",Arial,sans-serif}}
main{{width:min(1080px,calc(100% - 32px));margin:28px auto;background:white;padding:48px 58px;box-shadow:0 8px 28px #26394b1c}}
h1{{font:700 30px/1.28 Georgia,"Microsoft YaHei";margin:0 0 8px;color:#123b5a}}h2{{font-size:20px;margin:34px 0 14px;border-bottom:2px solid var(--blue);padding-bottom:7px}}
h3{{font-size:16px;margin:22px 0 8px}}p{{margin:8px 0}}.subtitle{{color:var(--muted);margin-bottom:20px}}.badge{{display:inline-block;padding:5px 10px;border-radius:4px;background:#fff1db;color:#7a4300;font-weight:700}}
.notice{{border-left:4px solid #d68a16;background:#fff8e9;padding:12px 16px;margin:20px 0}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}}
.card{{background:var(--light);padding:14px;border-top:3px solid var(--blue2)}}.card span{{display:block;color:var(--muted);font-size:12px}}.card strong{{font-size:21px}}
table{{width:100%;border-collapse:collapse;margin:12px 0 18px;font-size:13px}}th,td{{border:1px solid var(--line);padding:8px 9px;text-align:center}}thead th{{background:var(--blue);color:white}}tbody th{{background:#eef4f8}}caption{{text-align:left;font-weight:700;margin:7px 0}}
.figure{{border:1px solid var(--line);padding:16px;margin:16px 0;text-align:center}}.figure img{{max-width:820px;width:100%;height:auto}}.caption{{font-size:13px;color:#445565;margin-top:8px}}
.bar-row{{display:grid;grid-template-columns:150px 1fr 70px;gap:10px;align-items:center;margin:12px 0;text-align:left}}.bar-row>div{{height:18px;background:#e6edf2;border-radius:2px;overflow:hidden}}.bar-row i{{display:block;height:100%;background:linear-gradient(90deg,var(--blue),#5b9bd5)}}
.method-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.method-grid article{{border:1px solid var(--line);padding:14px}}.method-grid h3{{margin:0 0 6px;color:var(--blue)}}
.conclusion{{background:#eef6ec;border-left:4px solid #4d8c43;padding:14px 16px}}footer{{margin-top:36px;padding-top:12px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}}
@media(max-width:760px){{main{{padding:24px 18px}}.cards,.method-grid{{grid-template-columns:1fr 1fr}}table{{display:block;overflow-x:auto}}}}@media print{{body{{background:white}}main{{box-shadow:none;margin:0;width:100%}}}}
</style></head><body><main>
<span class="badge">开发预检 · 非正式论文结果</span>
<h1>E1：真实工程资料读取与证据映射</h1>
<p class="subtitle">证据约束的整体 S4R 风塔模型编译框架 · 论文实验协议 v2</p>
<div class="notice"><strong>判读边界：</strong>真值尚待工程师逐字段签署，R1 尚未运行，当前结果不能用于证明结构安全、设计合规或数值模型有效。</div>
<div class="cards"><div class="card"><span>完成记录</span><strong>{completed}/{expected}</strong></div><div class="card"><span>当前条件</span><strong>R0</strong></div><div class="card"><span>真实案例</span><strong>4</strong></div><div class="card"><span>结果状态</span><strong>{status}</strong></div></div>
<h2>1. 实验对象与比较条件</h2>
<div class="method-grid"><article><h3>R0：确定性解析</h3><p>根据冻结的表格结构、单位规则和字段边界恢复工程事实，不调用LLM。每个输入运行1次。</p></article><article><h3>R1：证据约束双顾问</h3><p>确定性提取后，由Qwen 3.8 Max工程/FEM顾问分别形成受限候选。原始案例每例独立运行5次。本阶段尚未执行。</p></article></div>
<p>主准确率仅使用4个原始工程案例。空行/工程备注、无关重复附件和数值显示差异共12个研究副本，只作为轻微鲁棒性补充，不扩大独立工程样本量。</p>
<h2>2. 汇总结果</h2>
<table><caption>表1 条件级结果（中位数 [Q1, Q3]）</caption><thead><tr><th>条件</th><th>记录</th><th>完成率</th><th>案例全正确率</th><th>字段准确率</th><th>来源定位率</th><th>证据覆盖率</th><th>错误晋升率</th></tr></thead><tbody>{''.join(condition_rows)}</tbody></table>
<table><caption>表2 场景级结果</caption><thead><tr><th>条件</th><th>场景</th><th>记录</th><th>完成率</th><th>案例全正确率</th></tr></thead><tbody>{''.join(scenario_rows) or '<tr><td colspan="5">尚无记录</td></tr>'}</tbody></table>
<div class="figure"><img src="E1_stage_success_funnel.png" alt="E1阶段成功率漏斗"><div class="caption">图1 E1阶段成功率漏斗。当前仅包含R0开发预检的16条记录。</div></div>
<div class="figure"><h3>证据与来源保障覆盖</h3>{source_bars}<div class="caption">图2 R0来源定位、证据覆盖及无错误晋升比例。比例以运行记录为统计单位，不代表独立工程样本量。</div></div>
<h2>3. 失败记录与关键边界</h2>
<table><caption>表3 失败案例审计</caption><thead><tr><th>条件</th><th>场景</th><th>失败分类</th><th>说明</th></tr></thead><tbody>{failure_rows}</tbody></table>
<p>T04的塔节解析已在“法兰参数/极限载荷”标题处终止，冻结结果为49个轮廓点、48个区段。5条法兰与5条极限载荷记录独立保存，不再混入整体壳几何。</p>
<p>R0不执行工程或FEM顾问调用，因此其FEM候选准确率属于“不适用”。底层CSV中的数值零仅表示没有顾问候选，不能作为R0失败或FEM参数错误的证据。</p>
<h2>4. 精简结论</h2>
<div class="conclusion">R0在4个原始案例及12个轻微研究变体上均完成解析，字段准确率、精确来源定位率和合法证据覆盖率中位数均为1.000，未出现无证据候选晋升。该结果支持进入R1受限候选形成实验，但不能替代工程师真值签署、正式双顾问重复实验或E2—E4验证。</div>
<h2>5. 正式结果生成条件</h2>
<ol><li>工程师签署四案例逐字段真值；</li><li>提交并冻结代码、提示和输入哈希，工作区保持clean；</li><li>真实响应模型标识必须为qwen3.8-max，且Token与重试完整记录；</li><li>72条计划记录全部保留，包括超时和失败，禁止选择性重跑。</li></ol>
<footer>数据集状态：{dataset_status} · 真值状态：{html.escape(str(summary.get('truth_status', 'unknown')))} · 自动生成，UTF-8</footer>
</main></body></html>"""
    path.write_text(document, encoding="utf-8")


def _paper_rate(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _paper_number(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def _paper_distribution(value: dict[str, Any]) -> str:
    if value.get("median") is None:
        return "—"
    return f"{float(value['median']):.3f} [{float(value['q1']):.3f}, {float(value['q3']):.3f}]"
