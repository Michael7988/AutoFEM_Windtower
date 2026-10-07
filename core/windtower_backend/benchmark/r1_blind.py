"""Truth-isolated R1 campaign operating on raw workbook cells only.

This module deliberately does not import the production wind-tower parser, model
library, reference-INP code, or any experiment truth/confirmation module.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any
import uuid

import requests

from ..config import load_settings
from ..importers.workbook_reader import read_workbook_data
from ..input_batches.providers import QwenEngineeringProvider, _json_text, _responses_text


BLIND_SCHEMA = "trusted_fem_r1_blind_campaign_v1"
EVIDENCE_SCHEMA = "blind_evidence_v1"
ENGINEERING_SCHEMA = "blind_engineering_candidate_v1"
FEM_SCHEMA = "blind_fem_candidate_v1"
CONSENSUS_SCHEMA = "r1_consensus_v1"
ALIASES = ("CASE_A", "CASE_B", "CASE_C", "CASE_D")
MODEL_ID = "qwen3.8-max"
ROW_CHUNK_SIZE = 12
ROW_OVERLAP = 1

ENGINEERING_PROMPT = """You are the engineering extraction agent in a blind experiment.
The evidence is a row/cell transcription from one workbook chunk. Return JSON only:
{"schema_version":"blind_engineering_candidate_v1","fields":[{"path":"engineering.records",
"value":[...]}]}. Use one compact tower_profile record with points encoded as
[z_mm,diameter_mm,thickness_mm,z_cell,diameter_cell,thickness_cell]. Cell references must
include the sheet alias, for example S01:R8C3. A compact flange_table uses rows encoded as
[outer_diameter_mm,bolt_circle_mm,inner_diameter_mm,total_thickness_mm,bolt_count,
bolt_diameter_mm,source_row]. Other records may be bolt, load, or project_meta. Only include
explicit values; omit unresolved labels that have no value instead of explaining them. Preserve evidence_ids. Never infer missing values, materials,
door geometry, FEM settings, or defaults. Do not output nodes, elements, Abaqus keywords,
code, INP, reasoning, prose, duplicated evidence text, or null-valued fields."""

FEM_PROMPT = """You are the FEM advisory agent in a blind experiment. The evidence is
the frozen engineering candidate, never a reference model. Return JSON only with
schema_version blind_fem_candidate_v1 and fields. Use only supplied target paths.
Every value is an unconfirmed recommendation and must cite evidence_ids or generic
capability:S4R-shell. Do not output geometry, nodes, elements, Abaqus keywords, code,
or INP. Do not invent a door when engineering evidence does not contain one."""

FEM_TARGET_KEYS = [
    "mesh.element_type",
    "mesh.global_size_mm",
    "mesh.door_size_mm",
    "mesh.patch_margin_u_mm",
    "mesh.patch_margin_z_mm",
    "mesh.hole_layers",
    "analysis.static_enabled",
    "analysis.modal_enabled",
    "analysis.modal_modes",
    "analysis.static_nlgeom",
    "analysis.modal_prestress",
    "boundary.name",
]


def prepare_r1_blind(source_root: str | Path, campaign_root: str | Path) -> dict[str, Any]:
    source = Path(source_root).resolve()
    root = Path(campaign_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"R1 campaign directory must be empty: {root}")
    raw_files = sorted(path for path in source.glob("*.xls") if "_FEM" not in path.name.upper())
    if len(raw_files) != 4:
        raise ValueError(f"Expected four raw engineering workbooks, found {len(raw_files)}")
    for directory in ("blind_input", "evidence", "runs", "consensus", "outbound", "responses", "results"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    cases = []
    for alias, source_path in zip(ALIASES, raw_files):
        target = root / "blind_input" / f"{alias}{source_path.suffix.lower()}"
        shutil.copyfile(source_path, target)
        evidence = extract_blind_evidence(target, alias)
        evidence_path = root / "evidence" / f"{alias}.json"
        _write_json(evidence_path, evidence)
        cases.append({
            "alias": alias,
            "input": target.relative_to(root).as_posix(),
            "sha256": _sha256(target),
            "evidence": evidence_path.relative_to(root).as_posix(),
            "chunk_count": len(evidence["chunks"]),
            "nonblank_row_count": evidence["nonblank_row_count"],
        })
    manifest = {
        "schema_version": BLIND_SCHEMA,
        "created_at": _now(),
        "status": "prepared",
        "model": MODEL_ID,
        "reasoning_effort": "low",
        "temperature": 0.6,
        "max_output_tokens": 6000,
        "request_timeout_seconds": 240,
        "prompt_hashes": {
            "engineering": hashlib.sha256(ENGINEERING_PROMPT.encode("utf-8")).hexdigest(),
            "fem": hashlib.sha256(FEM_PROMPT.encode("utf-8")).hexdigest(),
        },
        "code_snapshot": _git_snapshot(),
        "repetitions": 1,
        "llm_execution_mode": "single",
        "blind_input_policy": "raw_engineering_workbooks_only",
        "forbidden_inputs": ["FEM_parameter_workbooks", "reference_INP", "ground_truth", "model_library"],
        "cases": cases,
    }
    _write_json(root / "blind_manifest.json", manifest)
    return manifest


def extract_blind_evidence(path: str | Path, alias: str) -> dict[str, Any]:
    workbook = read_workbook_data(path)
    sensitive = _identifier_cells(workbook)
    chunks: list[dict[str, Any]] = []
    all_rows: list[str] = []
    for sheet_index, sheet in enumerate(workbook.sheets.values(), start=1):
        rows: list[tuple[str, str]] = []
        for row_index, row in enumerate(sheet.rows, start=1):
            cells = []
            for column_index, value in enumerate(row, start=1):
                if value is None or str(value).strip() == "":
                    continue
                cell = f"R{row_index}C{column_index}"
                if (sheet.name, row_index, column_index) in sensitive:
                    rendered = "[REDACTED_IDENTIFIER]"
                else:
                    rendered = _render(value)
                cells.append(f"{cell}={rendered}")
            if cells:
                row_id = f"S{sheet_index:02d}:R{row_index:04d}"
                rows.append((row_id, " | ".join(cells)))
                all_rows.append(row_id)
        start = 0
        chunk_index = 1
        while start < len(rows):
            selected = rows[start : start + ROW_CHUNK_SIZE]
            if not selected:
                break
            chunks.append({
                "evidence_id": f"{alias}:S{sheet_index:02d}:C{chunk_index:02d}",
                "filename": f"{alias}{Path(path).suffix.lower()}",
                "location": {
                    "sheet_alias": f"SHEET_{sheet_index:02d}",
                    "row_ids": [item[0] for item in selected],
                },
                "text": "\n".join(f"{row_id} | {text}" for row_id, text in selected),
            })
            if start + ROW_CHUNK_SIZE >= len(rows):
                break
            start += ROW_CHUNK_SIZE - ROW_OVERLAP
            chunk_index += 1
    return {
        "schema_version": EVIDENCE_SCHEMA,
        "case_alias": alias,
        "input_sha256": _sha256(Path(path)),
        "chunks": chunks,
        "nonblank_row_count": len(set(all_rows)),
        "covered_row_ids": sorted(set(all_rows)),
    }


def run_r1_blind(
    campaign_root: str | Path,
    repetitions: int = 1,
    execution_mode: str = "single",
) -> dict[str, Any]:
    root = Path(campaign_root).resolve()
    manifest = _load_manifest(root)
    if repetitions < 1:
        raise ValueError("repetitions must be at least one")
    if execution_mode not in {"single", "chunked_parallel"}:
        raise ValueError("execution_mode must be single or chunked_parallel")
    settings = load_settings()
    if settings.qwen_engineering_model != MODEL_ID or settings.qwen_fem_model != MODEL_ID:
        raise RuntimeError("Both advisors must be configured as qwen3.8-max.")
    engineering_provider = QwenEngineeringProvider(settings, model=MODEL_ID, role="engineering")
    fem_provider = QwenEngineeringProvider(settings, model=MODEL_ID, role="fem")
    engineering_provider.timeout = 240
    fem_provider.timeout = 240
    records = []
    for case in manifest["cases"]:
        alias = str(case["alias"])
        evidence = json.loads((root / case["evidence"]).read_text(encoding="utf-8"))
        runs_by_repetition: dict[int, dict[str, Any]] = {}
        pending: list[int] = []
        for repetition in range(1, repetitions + 1):
            run_path = root / "runs" / alias / f"run_{repetition:02d}.json"
            if run_path.is_file():
                runs_by_repetition[repetition] = json.loads(run_path.read_text(encoding="utf-8"))
            else:
                pending.append(repetition)
        with ThreadPoolExecutor(max_workers=min(5, len(pending) or 1)) as executor:
            futures = {
                executor.submit(
                    _run_case,
                    root,
                    alias,
                    repetition,
                    evidence["chunks"],
                    engineering_provider,
                    fem_provider,
                    execution_mode,
                ): repetition
                for repetition in pending
            }
            for future in as_completed(futures):
                repetition = futures[future]
                try:
                    run = future.result()
                except Exception as exc:
                    run = _failed_run(root, alias, repetition, exc)
                run_path = root / "runs" / alias / f"run_{repetition:02d}.json"
                _write_json(run_path, run)
                runs_by_repetition[repetition] = run
        case_runs = [runs_by_repetition[repetition] for repetition in range(1, repetitions + 1)]
        records.extend(case_runs)
        _write_json(root / "consensus" / f"{alias}.json", build_blind_consensus(alias, case_runs))
        _write_jsonl(root / "runs" / alias / "engineering_candidate_runs.jsonl", case_runs, "engineering")
        _write_jsonl(root / "runs" / alias / "fem_candidate_runs.jsonl", case_runs, "fem")
    expected_records = len(manifest["cases"]) * repetitions
    if len(records) != expected_records:
        raise RuntimeError(f"Expected {expected_records} top-level R1 records, found {len(records)}")
    seal = {
        "schema_version": "r1_blind_seal_v1",
        "sealed_at": _now(),
        "top_level_run_count": len(records),
        "successful_run_count": sum(item.get("status") == "completed" for item in records),
        "all_records_retained": True,
        "confirmation_inputs_accessed": False,
        "repetitions": repetitions,
        "llm_execution_mode": execution_mode,
        "files": (
            _hash_tree(root / "runs")
            + _hash_tree(root / "consensus")
            + _hash_tree(root / "outbound")
            + _hash_tree(root / "responses")
        ),
    }
    _write_json(root / "blind_complete.json", seal)
    return seal


def build_blind_consensus(alias: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    engineering: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    fem: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for run in runs:
        repetition = int(run.get("repetition") or 0)
        seen: set[str] = set()
        for record in run.get("engineering", {}).get("records") or []:
            evidence_ids = sorted(str(item) for item in record.get("evidence_ids") or [])
            source_cells = sorted(str(item) for item in record.get("source_cells") or [])
            # Cell coordinates identify an engineering fact. Chunk IDs are only
            # transport/audit metadata and vary when overlapping evidence is used.
            key = "|".join([str(record.get("record_type") or "unknown"), *(source_cells or evidence_ids)])
            candidate_key = f"{key}|{_candidate_key(record, False)}"
            if candidate_key not in seen:
                engineering[key].append((repetition, record))
                seen.add(candidate_key)
        seen = set()
        for field in _normalize_fields(run.get("fem", {}).get("fields")):
            key = str(field.get("path") or "unknown")
            candidate_key = f"{key}|{_candidate_key(field, True)}"
            if candidate_key not in seen:
                fem[key].append((repetition, field))
                seen.add(candidate_key)
    expected = max(1, len(runs))
    accepted_engineering, unresolved_engineering = _majority_groups(engineering, expected=expected)
    accepted_fem, unresolved_fem = _majority_groups(fem, expected=expected, value_only=True)
    return {
        "schema_version": CONSENSUS_SCHEMA,
        "case_alias": alias,
        "run_count": len(runs),
        "truth_accessed": False,
        "selection_policy": f"strict_majority_{expected}_runs_without_truth",
        "engineering_records": accepted_engineering,
        "unresolved_engineering": unresolved_engineering,
        "fem_fields": accepted_fem,
        "unresolved_fem": unresolved_fem,
    }


def _run_case(
    root: Path,
    alias: str,
    repetition: int,
    chunks: list[dict[str, Any]],
    engineering_provider: QwenEngineeringProvider,
    fem_provider: QwenEngineeringProvider,
    execution_mode: str = "single",
) -> dict[str, Any]:
    def call_chunk(index: int, chunk: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        request = _mapping_request(
            engineering_provider,
            [chunk],
            "Extract all explicit wind-tower engineering records in this workbook chunk without filling missing data.",
            ENGINEERING_PROMPT,
            ["engineering.records"],
        )
        _write_json(root / "outbound" / alias / f"r{repetition:02d}_engineering_{index:02d}.json", request)
        result = _call_streaming_response(engineering_provider, request)
        _write_json(root / "responses" / alias / f"r{repetition:02d}_engineering_{index:02d}.json", result)
        return index, result

    if execution_mode == "single":
        request = _mapping_request(
            engineering_provider,
            chunks,
            "Extract all explicit wind-tower engineering records from this workbook without filling missing data.",
            ENGINEERING_PROMPT,
            ["engineering.records"],
        )
        _write_json(root / "outbound" / alias / f"r{repetition:02d}_engineering_01.json", request)
        result = _call_streaming_response(engineering_provider, request)
        _write_json(root / "responses" / alias / f"r{repetition:02d}_engineering_01.json", result)
        engineering_results = [result]
        chunk_sources = [{"evidence_id": "combined-workbook"}]
    else:
        indexed_results: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(2, len(chunks) or 1)) as executor:
            futures = {executor.submit(call_chunk, index, chunk): index for index, chunk in enumerate(chunks, start=1)}
            for future in as_completed(futures):
                index, result = future.result()
                indexed_results[index] = result
        engineering_results = [indexed_results[index] for index in range(1, len(chunks) + 1)]
        chunk_sources = chunks
    records = []
    for chunk, result in zip(chunk_sources, engineering_results):
        _assert_response_model(result)
        records.extend(_engineering_records(result, chunk["evidence_id"]))
    records = _dedupe_engineering_records(records)
    frozen_engineering = {
        "schema_version": ENGINEERING_SCHEMA,
        "case_alias": alias,
        "records": records,
        "chunk_responses": engineering_results,
    }
    fem_evidence = _engineering_as_evidence(alias, frozen_engineering)
    fem_request = _mapping_request(
        fem_provider,
        fem_evidence,
        (
            "Recommend pending FEM candidates for family wind_tower_shell_with_door. "
            "Capability allows S4R and static/modal analyses; all numerical defaults are withheld."
        ),
        FEM_PROMPT,
        FEM_TARGET_KEYS,
    )
    _write_json(root / "outbound" / alias / f"r{repetition:02d}_fem.json", fem_request)
    fem_result = _call_streaming_response(fem_provider, fem_request)
    _write_json(root / "responses" / alias / f"r{repetition:02d}_fem.json", fem_result)
    _assert_response_model(fem_result)
    return {
        "schema_version": "trusted_fem_r1_top_level_run_v1",
        "case_alias": alias,
        "repetition": repetition,
        "completed_at": _now(),
        "status": "completed",
        "engineering": frozen_engineering,
        "fem": {"schema_version": FEM_SCHEMA, "fields": _normalize_fields(fem_result.get("fields")), "response": fem_result},
        "model": MODEL_ID,
        "total_tokens": _run_tokens(engineering_results) + _usage_tokens(fem_result),
        "truth_accessed": False,
        "llm_execution_mode": execution_mode,
    }


def _failed_run(root: Path, alias: str, repetition: int, exc: Exception) -> dict[str, Any]:
    response_paths = sorted((root / "responses" / alias).glob(f"r{repetition:02d}_engineering*.json"))
    engineering_results = [json.loads(path.read_text(encoding="utf-8")) for path in response_paths]
    records = []
    for result in engineering_results:
        records.extend(_engineering_records(result, "response-audit"))
    fem_path = root / "responses" / alias / f"r{repetition:02d}_fem.json"
    fem_result = json.loads(fem_path.read_text(encoding="utf-8")) if fem_path.is_file() else {}
    return {
        "schema_version": "trusted_fem_r1_top_level_run_v1",
        "case_alias": alias,
        "repetition": repetition,
        "completed_at": _now(),
        "status": "failed",
        "error": f"{type(exc).__name__}: {exc}",
        "engineering": {
            "schema_version": ENGINEERING_SCHEMA,
            "records": records,
            "chunk_responses": engineering_results,
        },
        "fem": {"schema_version": FEM_SCHEMA, "fields": _normalize_fields(fem_result.get("fields")), "response": fem_result},
        "model": MODEL_ID,
        "total_tokens": _run_tokens(engineering_results) + _usage_tokens(fem_result),
        "truth_accessed": False,
    }


def _mapping_request(
    provider: QwenEngineeringProvider,
    evidence: list[dict[str, Any]],
    modeling_goal: str,
    system_prompt: str,
    target_keys: list[str],
) -> dict[str, Any]:
    compact = [
        {
            "evidence_id": item["evidence_id"],
            "filename": item["filename"],
            "location": item["location"],
            "text": item["text"][:4000],
        }
        for item in evidence[:30]
    ]
    return {
        "model": provider.main_model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": json.dumps({
                "modeling_goal": modeling_goal,
                "target_parameter_keys": target_keys,
                "instruction": "Return compact JSON only; omit unavailable fields and all explanatory prose.",
                "evidence": compact,
            }, ensure_ascii=False)}]},
        ],
        "temperature": min(provider.temperature, 0.1),
        "reasoning": {"effort": "low"},
        "max_output_tokens": 6000,
        "stream": True,
    }


def _call_streaming_response(
    provider: QwenEngineeringProvider,
    payload: dict[str, Any],
) -> dict[str, Any]:
    started = time.time()
    last_error: Exception | None = None
    last_usage: dict[str, Any] = {}
    for attempt in range(3):
        text_deltas: list[str] = []
        final_response: dict[str, Any] = {}
        terminal_failure: str | None = None
        try:
            response = requests.post(
                f"{provider.settings.qwen_base_url.rstrip('/')}/responses",
                headers={
                    "Authorization": f"Bearer {provider.settings.qwen_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(10, 300),
                stream=True,
            )
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=True):
                line = str(raw_line or "").strip()
                if not line.startswith("data:"):
                    continue
                encoded = line[5:].strip()
                if not encoded or encoded == "[DONE]":
                    continue
                event = json.loads(encoded)
                event_type = str(event.get("type") or "")
                if event_type == "response.output_text.delta" and event.get("delta"):
                    text_deltas.append(str(event["delta"]))
                if isinstance(event.get("response"), dict):
                    final_response = event["response"]
                if event_type in {"response.failed", "response.incomplete"}:
                    terminal_failure = json.dumps(event.get("response") or event, ensure_ascii=False)[:2000]
            last_usage = final_response.get("usage") or {}
            if terminal_failure:
                return {
                    "status": "failed",
                    "provider": "qwen",
                    "model": str(final_response.get("model") or payload["model"]),
                    "fields": [],
                    "usage": last_usage,
                    "latency_ms": int((time.time() - started) * 1000),
                    "attempts": attempt + 1,
                    "error": f"terminal_stream_status: {terminal_failure}",
                }
            response_model = str(final_response.get("model") or payload["model"])
            if response_model != str(payload["model"]):
                raise RuntimeError(
                    f"LLM response model mismatch: requested={payload['model']} returned={response_model}"
                )
            text = _responses_text(final_response) or "".join(text_deltas)
            data = _json_text(text)
            return {
                "status": "called",
                "provider": "qwen",
                "model": response_model,
                "schema_version": data.get("schema_version"),
                "fields": data.get("fields", []),
                "usage": last_usage,
                "latency_ms": int((time.time() - started) * 1000),
                "attempts": attempt + 1,
            }
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    return {
        "status": "failed",
        "provider": "qwen",
        "model": payload.get("model", provider.main_model),
        "fields": [],
        "usage": last_usage,
        "latency_ms": int((time.time() - started) * 1000),
        "attempts": 3,
        "error": str(last_error or "Streaming LLM request failed"),
    }


def _engineering_records(result: dict[str, Any], fallback_evidence_id: str) -> list[dict[str, Any]]:
    records = []
    for field in _normalize_fields(result.get("fields")):
        if field.get("path") != "engineering.records" or not isinstance(field.get("value"), list):
            continue
        for value in field["value"]:
            if isinstance(value, dict):
                record = deepcopy(value)
                record.setdefault("evidence_ids", field.get("evidence_ids") or [fallback_evidence_id])
                if record.get("record_type") == "tower_profile":
                    records.extend(_expand_tower_profile(record))
                elif record.get("record_type") == "flange_table":
                    records.extend(_expand_flange_table(record))
                else:
                    records.append(record)
    return records


def _normalize_fields(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    fields = []
    for path, item in value.items():
        if isinstance(item, dict):
            fields.append({"path": str(path), **item})
        else:
            fields.append({"path": str(path), "value": item})
    return fields


def _expand_tower_profile(record: dict[str, Any]) -> list[dict[str, Any]]:
    expanded = []
    for point in record.get("points") or []:
        if not isinstance(point, list) or len(point) < 6:
            continue
        expanded.append({
            "record_type": "tower_point",
            "z_mm": point[0],
            "diameter_mm": point[1],
            "thickness_mm": point[2],
            "source_cells": [str(point[3]), str(point[4]), str(point[5])],
            "evidence_ids": list(record.get("evidence_ids") or []),
        })
    return expanded


def _expand_flange_table(record: dict[str, Any]) -> list[dict[str, Any]]:
    expanded = []
    for row in record.get("rows") or []:
        if not isinstance(row, list) or len(row) < 7:
            continue
        expanded.append({
            "record_type": "flange",
            "outer_diameter_mm": row[0],
            "bolt_circle_mm": row[1],
            "inner_diameter_mm": row[2],
            "total_thickness_mm": row[3],
            "bolt_count": row[4],
            "bolt_diameter_mm": row[5],
            "source_cells": [str(row[6])],
            "evidence_ids": list(record.get("evidence_ids") or []),
        })
    return expanded


def _dedupe_engineering_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        payload = _candidate_payload(record, False)
        payload.pop("evidence_ids", None)
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        if key in unique:
            unique[key]["evidence_ids"] = sorted(set(
                list(unique[key].get("evidence_ids") or []) + list(record.get("evidence_ids") or [])
            ))
        else:
            unique[key] = record
    return list(unique.values())


def _engineering_as_evidence(alias: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records") or []
    tower = [item for item in records if item.get("record_type") == "tower_point"]
    other = [item for item in records if item.get("record_type") != "tower_point"]
    summary = {
        "tower_point_count": len(tower),
        "z_mm_range": _numeric_range(tower, "z_mm"),
        "diameter_mm_range": _numeric_range(tower, "diameter_mm"),
        "thickness_mm_range": _numeric_range(tower, "thickness_mm"),
        "other_engineering_records": other,
        "door_status": "unresolved_unless_explicit_engineering_record_exists",
    }
    return [{
        "evidence_id": f"{alias}:ENGINEERING:SUMMARY",
        "filename": f"{alias}.json",
        "location": {"derived_from_candidate_records": len(records)},
        "text": json.dumps(summary, ensure_ascii=False, sort_keys=True),
    }] if records else [{
        "evidence_id": f"{alias}:ENGINEERING:EMPTY",
        "filename": f"{alias}.json",
        "location": {},
        "text": "No supported engineering records were resolved.",
    }]


def _numeric_range(records: list[dict[str, Any]], key: str) -> list[float] | None:
    values = [float(item[key]) for item in records if isinstance(item.get(key), (int, float))]
    return [min(values), max(values)] if values else None


def _majority_groups(
    groups: dict[str, list[tuple[int, dict[str, Any]]]],
    *,
    expected: int,
    value_only: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for key, values in sorted(groups.items()):
        repetitions_by_candidate: dict[str, set[int]] = defaultdict(set)
        payloads: dict[str, dict[str, Any]] = {}
        for repetition, value in values:
            payload = _candidate_payload(value, value_only)
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            repetitions_by_candidate[canonical].add(repetition)
            payloads[canonical] = payload
        counts = Counter({candidate: len(repetitions) for candidate, repetitions in repetitions_by_candidate.items()})
        winner, count = counts.most_common(1)[0]
        item = {**payloads[winner], "support": count, "expected_runs": expected, "consensus_key": key}
        threshold = expected // 2 + 1
        (accepted if count >= threshold else unresolved).append(item)
    return accepted, unresolved


def _candidate_payload(value: dict[str, Any], value_only: bool) -> dict[str, Any]:
    if value_only:
        return {"path": value.get("path"), "value": value.get("value")}
    stable_keys = {
        "record_type", "z_mm", "diameter_mm", "thickness_mm", "elevation_mm",
        "outer_diameter_mm", "inner_diameter_mm", "bolt_circle_mm",
        "total_thickness_mm", "bolt_count", "bolt_diameter_mm", "load_name",
        "value", "unit", "source_cells", "field", "reason",
    }
    return {key: value[key] for key in sorted(stable_keys) if key in value}


def _candidate_key(value: dict[str, Any], value_only: bool) -> str:
    return json.dumps(_candidate_payload(value, value_only), ensure_ascii=False, sort_keys=True, default=str)


def _identifier_cells(workbook: Any) -> set[tuple[str, int, int]]:
    result: set[tuple[str, int, int]] = set()
    tokens = ("项目", "机型", "客户", "业主", "公司", "project", "client", "model")
    for sheet in workbook.sheets.values():
        for row_index, row in enumerate(sheet.rows, start=1):
            for column_index, value in enumerate(row, start=1):
                label = str(value or "").lower()
                if any(token in label for token in tokens) and column_index < len(row):
                    result.add((sheet.name, row_index, column_index + 1))
    return result


def _assert_response_model(result: dict[str, Any]) -> None:
    if result.get("status") != "called":
        raise RuntimeError(str(result.get("error") or "LLM call failed"))
    if result.get("model") != MODEL_ID:
        raise RuntimeError(f"Unexpected response model: {result.get('model')}")
    if _usage_tokens(result) <= 0:
        raise RuntimeError("LLM response did not include non-zero token usage.")


def _usage_tokens(result: dict[str, Any]) -> int:
    return int((result.get("usage") or {}).get("total_tokens") or 0)


def _run_tokens(results: list[dict[str, Any]]) -> int:
    return sum(_usage_tokens(item) for item in results)


def _render(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()[:240]


def _load_manifest(root: Path) -> dict[str, Any]:
    payload = json.loads((root / "blind_manifest.json").read_text(encoding="utf-8"))
    if payload.get("schema_version") != BLIND_SCHEMA:
        raise ValueError("Unsupported blind campaign manifest.")
    forbidden = (
        tuple(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".xls", ".xlsx", ".xlsm"}
            and "_FEM" in path.name.upper()
        )
        + tuple(root.rglob("*.inp"))
        + tuple(root.rglob("*truth*"))
    )
    if forbidden:
        raise RuntimeError(f"Forbidden blind inputs found: {[str(path) for path in forbidden]}")
    for case in payload["cases"]:
        if _sha256(root / case["input"]) != case["sha256"]:
            raise RuntimeError(f"Blind input hash changed: {case['alias']}")
    return payload


def _hash_tree(root: Path) -> list[dict[str, str]]:
    return [
        {"path": path.relative_to(root.parent).as_posix(), "sha256": _sha256(path)}
        for path in sorted(root.rglob("*.json*"))
        if path.is_file()
    ]


def _git_snapshot() -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        return {"commit": commit, "dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"commit": None, "dirty": None, "error": str(exc)}


def _write_jsonl(path: Path, runs: list[dict[str, Any]], key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(run.get(key) or {}, ensure_ascii=False, sort_keys=True, default=str) + "\n" for run in runs),
        encoding="utf-8",
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
