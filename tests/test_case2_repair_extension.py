from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.windtower_backend.benchmark import case2_repair_extension as repair


def test_compact_diagnostics_deduplicates_machine_errors() -> None:
    attempt = {
        "gate": {"status": "pass", "issues": []},
        "isolated": {"stderr_tail": "line one\nline one\nBaseShellRevolve failed"},
        "lifecycle": {
            "status": "blocked_datacheck",
            "datachecks": {
                "static": {
                    "errors": ["***ERROR: missing section", "***ERROR: missing section"],
                    "warnings": ["***WARNING: empty set"],
                }
            },
        },
    }
    compact = repair.compact_machine_diagnostics(attempt)
    assert compact["abaqus_errors"] == ["***ERROR: missing section"]
    assert compact["lifecycle_status"] == "blocked_datacheck"
    assert "BaseShellRevolve failed" in compact["script_traceback_tail"]


def test_leakage_audit_blocks_structured_hash(tmp_path: Path) -> None:
    path = tmp_path / "pilot" / "T02" / "structured" / "repeat_01" / "run.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "selected_parameter_signature": "SECRET_PARAMETER_HASH",
                "attempts": [{"lifecycle": {"semantic_hash": "SECRET_SEMANTIC_HASH"}}],
            }
        ),
        encoding="utf-8",
    )
    passed = repair._leakage_audit(tmp_path, {"input": [{"text": "ordinary evidence"}]})
    blocked = repair._leakage_audit(tmp_path, {"input": [{"text": "SECRET_PARAMETER_HASH"}]})
    assert passed["status"] == "pass"
    assert blocked["status"] == "blocked"
    assert "selected_parameter_signature" in blocked["forbidden_matches"]


def test_repair_attempts_continue_at_four_and_assisted_only_after_strict_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(repair, "_previous_response", lambda *args: "previous")
    monkeypatch.setattr(
        repair,
        "_pilot_attempt",
        lambda *args: {"gate": {"status": "pass"}, "lifecycle": {"status": "blocked_datacheck"}},
    )
    monkeypatch.setattr(repair, "_repair_payload", lambda *args, **kwargs: {"input": ["safe"]})
    monkeypatch.setattr(
        repair,
        "_leakage_audit",
        lambda *args: {"status": "pass", "forbidden_matches": [], "payload_sha256": "safe"},
    )

    def fake_call(*args, **kwargs):
        attempt = kwargs["attempt"]
        return {
            "outcome": {
                "raw_text": f"candidate {attempt}",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                "latency_ms": 20,
                "http_request_count": 1,
            }
        }

    monkeypatch.setattr(repair, "_execute_call", fake_call)

    def fake_evaluate(root, method, mode, attempt, candidate, execute_solver):
        solved = mode == "assisted" and attempt == 7
        return {
            "gate": {"status": "pass", "issues": []},
            "isolated": {"status": "not_applicable"},
            "lifecycle": {"status": "solved" if solved else "blocked_datacheck"},
        }

    monkeypatch.setattr(repair, "_evaluate_inp_attempt", fake_evaluate)
    result = repair._run_method_repairs(
        tmp_path,
        {},
        "b1_inp",
        additional_attempts=3,
        assisted_attempts=2,
        execute_solver=True,
    )
    assert [item["attempt"] for item in result["attempts"]] == [4, 5, 6, 7]
    assert result["strict_success"] is False
    assert result["assisted_success"] is True
    assert all(item["leakage_audit"]["status"] == "pass" for item in result["attempts"])


def test_method_summary_excludes_assisted_from_primary_success() -> None:
    ledger = [
        {"method": "b1_inp", "mode": "strict", "attempt": 6, "odb": False, "http_interactions": 1, "total_tokens": 10, "latency_ms": 5},
        {"method": "b1_inp", "mode": "assisted", "attempt": 7, "odb": True, "http_interactions": 1, "total_tokens": 20, "latency_ms": 7},
        {"method": "structured", "mode": "strict", "attempt": 1, "odb": True, "http_interactions": 2, "total_tokens": 30, "latency_ms": 8},
    ]
    summary = repair._method_summary(ledger)
    assert summary["b1_inp"]["strict_success_within_six"] is False
    assert summary["b1_inp"]["assisted_success"] is True
    assert summary["b1_inp"]["strict_total_tokens"] == 10
    assert summary["b1_inp"]["assisted_total_tokens"] == 20


def test_b1_inp_modal_is_not_applicable() -> None:
    rows = repair._numerical_rows(
        {
            "b1_inp": {"status": "pass", "mode": "strict", "static": {}, "modal": {"frequencies_hz": []}},
            "b1_script": {"status": "no_valid_odb", "mode": None, "static": {}, "modal": {"frequencies_hz": []}},
            "structured": {"status": "pass", "mode": "strict", "static": {}, "modal": {"frequencies_hz": [1, 2]}},
        }
    )
    assert rows[0]["method"] == "b1_inp"
    assert rows[0]["frequencies_hz_first6"] == []


def test_inp_component_counter(tmp_path: Path) -> None:
    path = tmp_path / "two_components.inp"
    path.write_text(
        "*ELEMENT, TYPE=S4R\n1,1,2,3,4\n2,4,5,6,1\n3,10,11,12,13\n",
        encoding="utf-8",
    )
    assert repair._inp_connected_components(path) == 2
