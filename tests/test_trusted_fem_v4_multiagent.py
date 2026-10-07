from __future__ import annotations

import json
from pathlib import Path

import pytest

from windtower_backend.benchmark import trusted_fem_multiagent_v4 as v4


def _truth() -> dict:
    return {
        "engineering": {"tower_points": [
            {"index": 1, "z_mm": 0.0, "diameter_mm": 4000.0, "thickness_mm": 30.0, "source": "塔节参数!R8"},
            {"index": 2, "z_mm": 1000.0, "diameter_mm": 3900.0, "thickness_mm": 28.0, "source": "塔节参数!R9"},
        ]},
        "fem": {"parameters": [
            {"path": "mesh.global_size_mm", "value": 650.0, "unit": "mm", "source": "FEM模型参数!R17C3"},
        ]},
    }


def test_v4_schema_drift_and_empty_tower_are_blocked() -> None:
    payload = {
        "schema_version": "old_polymorphic_schema",
        "tower_points": [],
    }
    result = v4.validate_agent_output("engineering", payload, _truth())
    assert result["status"] == "blocked"
    codes = {item["code"] for item in result["issues"]}
    assert {"schema_mismatch", "tower_empty_or_short"} <= codes


def test_v4_engineering_contract_accepts_strict_evidence() -> None:
    payload = {
        "schema_version": v4.ENGINEERING_SCHEMA,
        "tower_points": [
            {"z_mm": 0, "diameter_mm": 4000, "thickness_mm": 30, "source_cells": ["S01!R8C2", "S01!R8C3", "S01!R8C5"]},
            {"z_mm": 1000, "diameter_mm": 3900, "thickness_mm": 28, "source_cells": ["S01!R9C2", "S01!R9C3", "S01!R9C5"]},
        ],
    }
    assert v4.validate_agent_output("engineering", payload, _truth())["status"] == "pass"


def test_v4_fem_unknown_missing_and_duplicate_paths_are_blocked() -> None:
    payload = {
        "schema_version": v4.FEM_SCHEMA,
        "parameters": [
            {"path": "unknown.path", "value": 1, "source_cells": ["S01!R1C1"]},
            {"path": "unknown.path", "value": 2, "source_cells": ["S01!R2C1"]},
        ],
    }
    result = v4.validate_agent_output("fem", payload, _truth())
    assert result["status"] == "blocked"
    codes = {item["code"] for item in result["issues"]}
    assert {"unknown_paths", "missing_required_paths", "duplicate_paths"} <= codes


def test_v4_attempt_selection_never_merges_and_prefers_pass_then_first() -> None:
    blocked = {"attempt": 1, "gate": {"status": "blocked", "completeness": 0.9}}
    passed_second = {"attempt": 2, "gate": {"status": "pass", "completeness": 1.0}}
    assert v4._select_attempt([blocked, passed_second]) is passed_second
    passed_first = {"attempt": 1, "gate": {"status": "pass", "completeness": 1.0}}
    assert v4._select_attempt([passed_second, passed_first]) is passed_first


def test_v4_prepare_reconstructs_valid_truth_and_isolates_inputs(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "core" / "example"
    if len(list(source.glob("*.xls"))) < 8:
        pytest.skip("Four paired example workbooks are unavailable")
    payload = v4.prepare_v4_multiagent(source, tmp_path)
    root = Path(payload["campaign_root"])
    manifest = json.loads((root / "campaign_manifest.json").read_text(encoding="utf-8"))
    assert manifest["formal_result_eligible"] is False
    assert manifest["truth_status"] == v4.TRUTH_STATUS
    assert len(manifest["cases"]) == 4
    for case in manifest["cases"]:
        engineering = json.loads((root / case["engineering_evidence"]).read_text(encoding="utf-8"))
        fem = json.loads((root / case["fem_evidence"]).read_text(encoding="utf-8"))
        truth = json.loads((root / case["truth"]).read_text(encoding="utf-8"))
        assert engineering["role"] == "engineering"
        assert fem["role"] == "fem"
        assert truth["validation"]["status"] == "pass"
        assert len(truth["engineering"]["tower_points"]) >= 49
        assert len(truth["fem"]["parameters"]) >= 49
        fem_text = json.dumps(fem, ensure_ascii=False)
        assert "塔节参数" not in fem_text


def test_v4_budget_rejects_odd_or_out_of_range(tmp_path: Path) -> None:
    with pytest.raises((FileNotFoundError, ValueError)):
        v4.run_v4_multiagent(tmp_path, max_llm_calls=11)
