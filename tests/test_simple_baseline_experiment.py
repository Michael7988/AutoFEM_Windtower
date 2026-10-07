from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.windtower_backend.benchmark import simple_baseline_experiment as experiment


VALID_INP = """*HEADING
minimal
*NODE
1, 0., 0., 0.
2, 1., 0., 0.
3, 1., 1., 0.
4, 0., 1., 0.
*ELEMENT, TYPE=S4R, ELSET=ALL_ELEMENTS
1, 1, 2, 3, 4
*MATERIAL, NAME=STEEL
*ELASTIC
210000., 0.3
*STEP
*STATIC
1., 1.
*END STEP
"""


def test_frozen_payload_disables_thinking() -> None:
    payload = experiment._responses_payload("system", {"case": "T02"}, 123)
    assert payload["model"] == experiment.MODEL
    assert payload["temperature"] == 0
    assert payload["enable_thinking"] is False
    assert "reasoning" not in payload


def test_direct_inp_gate_accepts_self_contained_model() -> None:
    gate = experiment.validate_direct_inp(VALID_INP)
    assert gate["status"] == "pass"
    assert gate["node_count"] == 4
    assert gate["element_count"] == 1


@pytest.mark.parametrize(
    ("addition", "expected"),
    [
        ("*INCLUDE, INPUT=other.inp\n", "forbidden_keyword"),
        ("** C:\\private\\model.inp\n", "external_or_absolute_path"),
        ("*CONTACT\n", "forbidden_keyword"),
    ],
)
def test_direct_inp_gate_blocks_unsafe_content(addition: str, expected: str) -> None:
    gate = experiment.validate_direct_inp(addition + VALID_INP)
    assert gate["status"] == "blocked"
    assert expected in {issue["code"] for issue in gate["issues"]}


def test_direct_inp_gate_classifies_truncation() -> None:
    truncated = VALID_INP.rsplit("*END STEP", 1)[0]
    gate = experiment.validate_direct_inp(truncated)
    assert experiment.classify_failure(gate) == "output_truncated"


def test_empty_provider_output_has_separate_failure_class() -> None:
    gate = experiment.validate_direct_inp("")
    gate["issues"].append({"code": "empty_provider_output"})
    assert experiment.classify_failure(gate) == "provider_or_protocol"


def test_extract_inp_signature(tmp_path: Path) -> None:
    path = tmp_path / "model_static.inp"
    path.write_text(VALID_INP + "** BASE_FIXED TOP_RP_NSET DOOR_PATCH_ELEMENTS\n", encoding="utf-8")
    signature = experiment.extract_inp_signature(path)
    assert signature["node_count"] == 4
    assert signature["element_count"] == 1
    assert signature["step_count"] == 1
    assert signature["has_base_fixed"] is True


def test_immutable_call_cache_and_request_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []

    def fake_call(settings: object, payload: dict, *, expect_json: bool) -> dict:
        calls.append(payload)
        return {
            "status": "success",
            "data": {"decision": "reject"},
            "raw_text": '{"decision":"reject"}',
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            "latency_ms": 7,
            "http_request_count": 1,
        }

    monkeypatch.setattr(experiment, "_single_http_request", fake_call)
    monkeypatch.setattr(experiment, "load_settings", lambda: object())
    kwargs = dict(
        stage="pilot",
        case_id="T02",
        method="b1_script",
        repeat=1,
        role="script",
        attempt=1,
        payload={"model": experiment.MODEL, "input": ["same"]},
        expect_json=True,
    )
    first = experiment._execute_call(tmp_path, **kwargs)
    second = experiment._execute_call(tmp_path, **kwargs)
    assert first == second
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="Request changed"):
        experiment._execute_call(tmp_path, **{**kwargs, "payload": {"model": experiment.MODEL, "input": ["changed"]}})


def test_call_metrics_are_isolated_by_method(tmp_path: Path) -> None:
    for method, tokens in (("b1_script", 20), ("structured", 30)):
        path = tmp_path / "private_responses" / method / "call.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "simple_baseline_call_v1",
                    "stage": "formal",
                    "case_id": "T02",
                    "method": method,
                    "repeat": 1,
                    "outcome": {
                        "usage": {"total_tokens": tokens},
                        "http_request_count": 1,
                        "latency_ms": tokens,
                    },
                }
            ),
            encoding="utf-8",
        )
    metrics = experiment._call_metrics(tmp_path)
    assert metrics[("formal", "T02", "b1_script", 1)]["total_tokens"] == 20
    assert metrics[("formal", "T02", "structured", 1)]["total_tokens"] == 30


def test_inp_lifecycle_does_not_solve_after_failed_datacheck(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inp = tmp_path / "model_static.inp"
    inp.write_text(VALID_INP, encoding="utf-8")
    monkeypatch.setattr(experiment, "_run_datacheck", lambda *args: {"status": "failed"})
    monkeypatch.setattr(
        experiment,
        "_solve_job",
        lambda *args: pytest.fail("solver must not run after a failed Datacheck"),
    )
    lifecycle = experiment._evaluate_inps(tmp_path / "run", "T02", [inp], execute_solver=True)
    assert lifecycle["status"] == "blocked_datacheck"
    assert lifecycle["solves"]["static"]["status"] == "not_run"


def test_structured_summary_preserves_actual_retry_count() -> None:
    attempts = [
        {"gate": {"status": "blocked"}, "lifecycle": {"status": "not_run"}},
        {"gate": {"status": "pass"}, "lifecycle": {"status": "datacheck_passed"}},
    ]
    summary = experiment._run_summary("pilot", "T02", "structured", 1, attempts)
    assert summary["attempt_count"] == 2
    assert summary["first_attempt_success"] is False
    assert summary["within_three_success"] is True


def test_fem_schema_normalizes_boundary_dofs_without_inference() -> None:
    candidate = {
        "parameters": [
            {"path": "boundary.dofs", "value": "1,2,3,4,5,6"},
            {"path": "loads.top_force_direction", "value": 1},
        ]
    }
    normalized = experiment._canonicalize_fem_candidate(candidate)
    assert normalized["parameters"][0]["value"] == [1, 2, 3, 4, 5, 6]
    assert candidate["parameters"][0]["value"] == "1,2,3,4,5,6"


def test_fem_schema_normalizes_signed_profile_and_wind_vector() -> None:
    candidate = {
        "parameters": [
            {"path": "loads.wind_direction_vector", "value": "-1,0,0"},
            {
                "path": "loads.wind_pressure_profile",
                "value": "0-20m:-0.00089;20-40m:-0.00114",
            },
        ]
    }
    normalized = experiment._canonicalize_fem_candidate(candidate)
    assert normalized["parameters"][0]["value"] == [-1, 0, 0]
    profile = normalized["parameters"][1]["value"]
    assert len(profile) == 2
    assert profile[0]["pressure_mpa"] == pytest.approx(-0.00089)
    assert candidate["parameters"][1]["value"].startswith("0-20m")


def test_successful_lifecycle_has_no_failure_class() -> None:
    assert experiment.classify_failure({"status": "pass"}, {"status": "solved"}) == ""
    assert experiment.classify_failure({"status": "pass"}, {"status": "blocked_no_inp"}) == "no_inp"
