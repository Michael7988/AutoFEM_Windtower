from __future__ import annotations

import json
from pathlib import Path

import pytest

from windtower_backend.benchmark import hybrid_paper_experiment as hybrid


@pytest.fixture(scope="module")
def prepared_campaign(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = Path(__file__).resolve().parents[1]
    root = tmp_path_factory.mktemp("hybrid") / "trusted_fem_hybrid_test"
    payload = hybrid.prepare_hybrid_campaign(project / "core" / "example", root.parent, campaign_root=root)
    assert payload["formal_call_count"] == 64
    return root


def test_prepare_freezes_schedule_inputs_and_blocked_report(prepared_campaign: Path) -> None:
    root = prepared_campaign
    schedule = json.loads((root / "manifests" / "formal_schedule.json").read_text(encoding="utf-8"))
    assert len(schedule) == 64
    assert {phase: sum(1 for row in schedule if row["phase"] == phase) for phase in ("E1", "E2", "E3", "E4")} == {
        "E1": 24,
        "E2": 16,
        "E3": 16,
        "E4": 8,
    }
    assert len(json.loads((root / "manifests" / "e2_artifacts.json").read_text(encoding="utf-8"))) == 8
    assert len(json.loads((root / "manifests" / "e3_faults.json").read_text(encoding="utf-8"))) == 8
    summary = json.loads((root / "metrics" / "results_summary.json").read_text(encoding="utf-8"))
    assert summary["formal_result_eligible"] is False
    assert (root / "report" / "experiment_report_zh.html").is_file()
    assert (root / "paper" / "实验部分_中文.md").is_file()
    for name in (
        "e1_success_funnel",
        "e2_format_consistency",
        "e3_safety_classification",
        "e4_modification_stability",
        "llm_usage",
    ):
        assert (root / "figures" / f"{name}.svg").is_file()
        assert (root / "figures" / f"{name}.png").is_file()


def test_preflight_uses_three_calls_records_usage_and_resumes_without_duplicates(
    prepared_campaign: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_request(settings: object, payload: dict, expect_json: bool = True) -> dict:
        calls.append(payload)
        if not expect_json:
            data = "OK"
        else:
            system = payload["input"][0]["content"][0]["text"]
            if "engineering-data parsing" in system:
                data = {
                    "schema_version": "engineering_parse_v2",
                    "case_alias": "PREFLIGHT",
                    "tower_points": [
                        {
                            "index": 1,
                            "z_mm": 0,
                            "diameter_mm": 4000,
                            "thickness_mm": 30,
                            "source_cells": ["S01!R1C1", "S01!R1C2", "S01!R1C3"],
                        },
                        {
                            "index": 2,
                            "z_mm": 10000,
                            "diameter_mm": 3800,
                            "thickness_mm": 26,
                            "source_cells": ["S01!R2C1", "S01!R2C2", "S01!R2C3"],
                        },
                    ],
                    "flanges": [],
                    "door": {},
                    "unresolved": [],
                    "confidence_status": "evidence_only",
                }
            else:
                data = {
                    "schema_version": "fem_parse_v2",
                    "case_alias": "PREFLIGHT",
                    "parameters": [
                        {
                            "path": "mesh.global_size_mm",
                            "value": 650,
                            "unit": "mm",
                            "source_cells": ["S01!R5C3"],
                            "status": "observed",
                        }
                    ],
                    "unresolved": [],
                    "confidence_status": "evidence_only",
                }
        return {
            "status": "called",
            "reason": "",
            "provider": "qwen",
            "model": "qwen3.8-max",
            "http_status": 200,
            "request_id": f"req-{len(calls)}",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "latency_ms": 20,
            "http_request_count": 1,
            "retryable": False,
            "raw_text": json.dumps(data, ensure_ascii=False) if expect_json else str(data),
            "data": data,
        }

    monkeypatch.setattr(hybrid, "_single_http_request", fake_request)
    monkeypatch.setattr(hybrid, "_ssl_status", lambda: {"status": "pass", "openssl": "test"})
    monkeypatch.setattr(
        hybrid,
        "load_settings",
        lambda: type(
            "Settings",
            (),
            {
                "qwen_api_key": "test",
                "qwen_engineering_model": "qwen3.8-max",
                "qwen_fem_model": "qwen3.8-max",
            },
        )(),
    )
    first = hybrid.run_hybrid_preflight(prepared_campaign)
    assert first["status"] == "pass"
    assert len(calls) == 3
    assert first["budget"]["actual_http_request_count"] == 3
    assert first["budget"]["total_tokens"] == 45
    second = hybrid.run_hybrid_preflight(prepared_campaign)
    assert second["status"] == "pass"
    assert len(calls) == 3
    usage = json.loads((prepared_campaign / "metrics" / "llm_usage_summary.json").read_text(encoding="utf-8"))
    assert usage["totals"]["actual_http_request_count"] == 3
    assert usage["totals"]["total_tokens"] == 45


def test_formal_run_stops_at_admission_gate_without_llm_call(
    prepared_campaign: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_call(*args: object, **kwargs: object) -> dict:
        raise AssertionError("formal LLM call must not happen when gates are blocked")

    monkeypatch.setattr(hybrid, "_execute_logical_call", forbidden_call)
    result = hybrid.run_hybrid_formal(prepared_campaign)
    assert result["status"] == "blocked_formal_gate"
    assert result["formal_result_eligible"] is False
    assert "truth_review" in result["blockers"]


def test_report_numbers_match_frozen_usage_files(prepared_campaign: Path) -> None:
    hybrid.render_hybrid_report(prepared_campaign)
    usage = json.loads((prepared_campaign / "metrics" / "llm_usage_summary.json").read_text(encoding="utf-8"))
    events = hybrid._read_jsonl(prepared_campaign / "metrics" / "llm_events.jsonl")
    assert usage["totals"]["actual_http_request_count"] == sum(int(item.get("http_request_count") or 0) for item in events)
    assert usage["totals"]["total_tokens"] == sum(
        int(item.get("total_tokens") or 0) for item in events if item.get("usage_reported")
    )
    html_text = (prepared_campaign / "report" / "experiment_report_zh.html").read_text(encoding="utf-8")
    paper_text = (prepared_campaign / "paper" / "实验部分_中文.md").read_text(encoding="utf-8")
    assert f"<strong>{usage['totals']['actual_http_request_count']}</strong>" in html_text
    assert f"实际 HTTP 请求为 {usage['totals']['actual_http_request_count']} 次" in paper_text
    assert "不生成预期性实验结论" in html_text
