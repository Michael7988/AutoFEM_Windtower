from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from windtower_backend.benchmark import case_study_application as study


def test_chat_payload_disables_thinking_and_has_no_output_limit() -> None:
    payload = study._payload("fem", "CASE_A", {"sheets": []}, ["mesh.global_size_mm"])
    assert payload["model"] == "qwen3.8-max"
    assert payload["stream"] is False
    assert payload["temperature"] == 0
    assert payload["enable_thinking"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in payload
    assert "max_output_tokens" not in payload


def test_compact_projection_preserves_cells_and_is_used_by_request() -> None:
    evidence = {
        "case_alias": "CASE_A",
        "role": "engineering",
        "source_sha256": "abc",
        "sheets": [{"sheet_alias": "S01", "cells": [{"cell": "S01!R1C1", "value": "塔筒", "formula": None}]}],
    }
    evidence["llm_projection"] = study._compact_projection(evidence)
    payload = study._payload("engineering", "CASE_A", evidence, [])
    user = json.loads(payload["messages"][1]["content"])
    assert user["evidence"]["sheets"] == [["S01", [["S01!R1C1", "塔筒"]]]]
    assert "formula" not in payload["messages"][1]["content"]


def test_fixed_array_contract_normalizes_without_shape_drift() -> None:
    engineering = study._normalize_engineering(
        {
            "schema_version": study.ENGINEERING_SCHEMA,
            "tower_points": [[1, 0, 4000, 30, "S1!R2C2", "S1!R2C3", "S1!R2C4"]],
            "flanges": [],
            "unresolved": [],
        },
        "CASE_A",
    )
    assert engineering["tower_points"][0]["diameter_mm"] == 4000
    assert study.validate_case_output("engineering", engineering, {"tower_points": 1}, [])["status"] == "pass"


def test_empty_and_unknown_fem_paths_are_blocked() -> None:
    empty = study._normalize_fem({"schema_version": study.FEM_SCHEMA, "parameters": []}, "CASE_A")
    assert study.validate_case_output("fem", empty, {}, ["mesh.global_size_mm"])["status"] == "blocked"
    unknown = study._normalize_fem(
        {"schema_version": study.FEM_SCHEMA, "parameters": [["bad.path", 1, "", "S01!R1C1"]]}, "CASE_A"
    )
    issues = study.validate_case_output("fem", unknown, {}, ["mesh.global_size_mm"])["issues"]
    assert {item["code"] for item in issues} == {"unknown_paths", "missing_paths"}


def test_dimensionless_parameter_still_requires_evidence() -> None:
    candidate = study._normalize_fem(
        {
            "schema_version": study.FEM_SCHEMA,
            "parameters": [["material.poisson_ratio", 0.3, "", "S01!R2C3"]],
        },
        "CASE_A",
    )
    assert study.validate_case_output("fem", candidate, {}, ["material.poisson_ratio"])["status"] == "pass"
    candidate["parameters"][0]["source_cells"] = []
    assert study.validate_case_output("fem", candidate, {}, ["material.poisson_ratio"])["status"] == "blocked"


def test_chat_request_records_real_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 200
        headers = {"x-request-id": "req-test"}

        @staticmethod
        def json() -> dict:
            return {
                "id": "chat-test",
                "model": study.MODEL,
                "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            }

    monkeypatch.setattr(study.requests, "post", lambda *args, **kwargs: Response())
    settings = SimpleNamespace(qwen_base_url="https://example.test/v1", qwen_api_key="secret", qwen_timeout=60)
    result = study._single_chat_request(settings, {"model": study.MODEL})
    assert result["status"] == "called"
    assert result["usage"]["total_tokens"] == 13
    assert result["http_request_count"] == 1


def test_sealed_call_is_resumed_without_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence, targets, expected = study._synthetic("fem")
    calls = {"count": 0}

    def fake_request(settings: object, payload: dict) -> dict:
        calls["count"] += 1
        return {
            "status": "called",
            "http_request_count": 1,
            "transient": False,
            "data": {
                "schema_version": study.FEM_SCHEMA,
                "parameters": [["mesh.global_size_mm", 650, "mm", "S01!R2C2"]],
                "unresolved_paths": [],
            },
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(study, "_single_chat_request", fake_request)
    monkeypatch.setattr(study, "load_settings", lambda: object())
    first, sent_first = study._execute_call(tmp_path, "preflight", "PREFLIGHT", "fem", evidence, targets, expected, 1)
    second, sent_second = study._execute_call(tmp_path, "preflight", "PREFLIGHT", "fem", evidence, targets, expected, 1)
    assert first["record_sha256"] == second["record_sha256"]
    assert sent_first is True and sent_second is False and calls["count"] == 1


def test_solver_never_substitutes_r0_for_missing_agent(tmp_path: Path) -> None:
    manifest = {"cases": [{"case_id": "T04"}]}
    seal = {"selected": {"T04": {"engineering": {"gate": {"status": "pass"}}, "fem": {"gate": {"status": "blocked"}}}}}
    study._write_json(tmp_path / "campaign_manifest.json", manifest)
    study._write_json(tmp_path / "seals" / "formal.json", seal)
    result = study.solve_case_study(tmp_path)
    assert result["cases"]["T04"] == {"status": "method_blocked", "stage": "agent_gate", "r0_substitution": False}


def test_authorized_completion_is_idempotent_when_role_already_sealed(tmp_path: Path) -> None:
    study._write_json(tmp_path / "campaign_manifest.json", {"cases": [{"case_id": "T04"}]})
    seal = {"selected": {"T04": {"fem": {"record": "private_responses/formal/CASE_D/fem.json"}}}}
    study._write_json(tmp_path / "seals" / "formal.json", seal)
    result = study.complete_missing_case_role(tmp_path)
    assert result["new_http_requests"] == 0
    assert result["selected"] == seal["selected"]


def test_subset_solve_preserves_existing_case_results(tmp_path: Path) -> None:
    study._write_json(tmp_path / "campaign_manifest.json", {"cases": [{"case_id": "T04"}]})
    study._write_json(
        tmp_path / "seals" / "formal.json",
        {"selected": {"T04": {"engineering": {"gate": {"status": "pass"}}, "fem": {"gate": {"status": "blocked"}}}}},
    )
    study._write_json(
        tmp_path / "results" / "numerical_summary.json",
        {"schema_version": "case_study_numerical_summary_v1", "cases": {"T01": {"status": "completed"}}},
    )
    result = study.solve_case_study(tmp_path, case_ids={"T04"})
    assert result["cases"]["T01"]["status"] == "completed"
    assert result["cases"]["T04"]["r0_substitution"] is False
