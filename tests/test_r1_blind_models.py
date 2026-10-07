from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from windtower_backend.benchmark import r1_blind
from windtower_backend.benchmark.r1_blind import (
    _hash_tree,
    _write_json,
    _write_jsonl,
    build_blind_consensus,
    prepare_r1_blind,
)
from windtower_backend.benchmark.r1_model_packages import (
    build_r1_models,
    render_r1_report,
    unlock_r1_confirmation,
)


EXAMPLE_ROOT = Path(__file__).resolve().parents[1] / "core" / "example"


def test_prepare_r1_blind_contains_only_neutral_raw_inputs(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    manifest = prepare_r1_blind(EXAMPLE_ROOT, root)

    assert [item["alias"] for item in manifest["cases"]] == list(r1_blind.ALIASES)
    assert sorted(path.name for path in (root / "blind_input").iterdir()) == [f"{alias}.xls" for alias in r1_blind.ALIASES]
    serialized = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.json"))
    assert "_FEM_parameters.xls" not in serialized
    assert ".inp" not in serialized.lower()
    assert "中国海装" not in serialized


def test_prepare_r1_blind_rejects_nonempty_or_incomplete_source(tmp_path: Path) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        prepare_r1_blind(EXAMPLE_ROOT, nonempty)

    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    source_file = next(path for path in EXAMPLE_ROOT.glob("*.xls") if "_FEM" not in path.name.upper())
    (incomplete / "single.xls").write_bytes(source_file.read_bytes())
    with pytest.raises(ValueError, match="Expected four"):
        prepare_r1_blind(incomplete, tmp_path / "new-campaign")


def test_blind_chunks_cover_every_nonblank_row_without_loss(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    manifest = prepare_r1_blind(EXAMPLE_ROOT, root)
    for case in manifest["cases"]:
        evidence = json.loads((root / case["evidence"]).read_text(encoding="utf-8"))
        covered = {
            row_id
            for chunk in evidence["chunks"]
            for row_id in chunk["location"]["row_ids"]
        }
        assert covered == set(evidence["covered_row_ids"])
        assert len(covered) == evidence["nonblank_row_count"]
        assert all(len(chunk["location"]["row_ids"]) <= r1_blind.ROW_CHUNK_SIZE for chunk in evidence["chunks"])


def test_blind_module_has_no_case_parser_or_truth_import() -> None:
    source = inspect.getsource(r1_blind)
    assert "parse_windtower_workbook" not in source
    assert "reference_models" not in source
    assert "e1_real_engineering" not in source
    assert "import ground_truth" not in source
    assert "from .e1_real_engineering" not in source


def test_consensus_counts_distinct_runs_not_duplicate_chunks() -> None:
    duplicate = {
        "record_type": "tower_point",
        "z_mm": 0,
        "diameter_mm": 4000,
        "thickness_mm": 40,
        "evidence_ids": ["CASE_A:S01:C01"],
        "source_cells": ["R8C3", "R8C4", "R8C6"],
    }
    runs = [
        {
            "repetition": 1,
            "engineering": {"records": [duplicate, duplicate, duplicate]},
            "fem": {"fields": []},
        },
        {"repetition": 2, "engineering": {"records": []}, "fem": {"fields": []}},
        {"repetition": 3, "engineering": {"records": []}, "fem": {"fields": []}},
        {"repetition": 4, "engineering": {"records": []}, "fem": {"fields": []}},
        {"repetition": 5, "engineering": {"records": []}, "fem": {"fields": []}},
    ]

    consensus = build_blind_consensus("CASE_A", runs)

    assert consensus["engineering_records"] == []
    assert consensus["unresolved_engineering"][0]["support"] == 1


def test_consensus_aligns_same_cells_across_overlapping_chunk_ids() -> None:
    runs = []
    for repetition, evidence_id in enumerate(("C01", "C02", "C01", "C02", "C03"), start=1):
        runs.append({
            "repetition": repetition,
            "engineering": {"records": [{
                "record_type": "tower_point",
                "z_mm": 0,
                "diameter_mm": 4000,
                "thickness_mm": 40,
                "evidence_ids": [f"CASE_A:S01:{evidence_id}"],
                "source_cells": ["S01:R8C2", "S01:R8C3", "S01:R8C5"],
            }]},
            "fem": {"fields": []},
        })

    consensus = build_blind_consensus("CASE_A", runs)

    assert len(consensus["engineering_records"]) == 1
    assert consensus["engineering_records"][0]["support"] == 5


def test_compact_tower_profile_is_deterministically_expanded() -> None:
    result = {
        "fields": [{
            "path": "engineering.records",
            "value": [{
                "record_type": "tower_profile",
                "evidence_ids": ["CASE_A:S01:C01"],
                "points": [[0, 4400, 44, "S01:R8C3", "S01:R8C4", "S01:R8C6"]],
            }],
        }],
    }

    records = r1_blind._engineering_records(result, "fallback")

    assert records == [{
        "record_type": "tower_point",
        "z_mm": 0,
        "diameter_mm": 4400,
        "thickness_mm": 44,
        "source_cells": ["S01:R8C3", "S01:R8C4", "S01:R8C6"],
        "evidence_ids": ["CASE_A:S01:C01"],
    }]


def test_compact_helpers_normalize_merge_and_summarize() -> None:
    assert r1_blind._normalize_fields(None) == []
    assert r1_blind._normalize_fields({"mesh.element_type": {"value": "S4R"}, "analysis.modal_modes": 20}) == [
        {"path": "mesh.element_type", "value": "S4R"},
        {"path": "analysis.modal_modes", "value": 20},
    ]
    flange = r1_blind._expand_flange_table({
        "record_type": "flange_table",
        "evidence_ids": ["E1"],
        "rows": [[1, 2, 3, 4, 5, 6, "S02:R3"], ["invalid"]],
    })
    assert flange[0]["record_type"] == "flange"
    assert flange[0]["bolt_count"] == 5
    duplicate = {
        "record_type": "tower_point",
        "z_mm": 0,
        "diameter_mm": 4400,
        "thickness_mm": 44,
        "source_cells": ["S01:R8C3", "S01:R8C4", "S01:R8C6"],
    }
    merged = r1_blind._dedupe_engineering_records([
        {**duplicate, "evidence_ids": ["C01"]},
        {**duplicate, "evidence_ids": ["C02"]},
    ])
    assert len(merged) == 1
    assert merged[0]["evidence_ids"] == ["C01", "C02"]
    evidence = r1_blind._engineering_as_evidence("CASE_A", {"records": merged + flange})
    summary = json.loads(evidence[0]["text"])
    assert summary["tower_point_count"] == 1
    assert summary["diameter_mm_range"] == [4400.0, 4400.0]


def test_formal_build_is_blocked_before_blind_seal_and_confirmation(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    prepare_r1_blind(EXAMPLE_ROOT, root)

    with pytest.raises(RuntimeError, match="not sealed"):
        build_r1_models(root)


def test_streaming_r1_call_reads_completed_response(monkeypatch) -> None:
    from dataclasses import replace

    from windtower_backend.benchmark.r1_blind import _call_streaming_response
    from windtower_backend.config import load_settings
    from windtower_backend.input_batches.providers import QwenEngineeringProvider

    completed = {
        "type": "response.completed",
        "response": {
            "model": "qwen3.8-max",
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": '{"schema_version":"blind_engineering_candidate_v1","fields":[]}',
                }],
            }],
            "usage": {"total_tokens": 21},
        },
    }

    class _StreamingResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def iter_lines(decode_unicode: bool = False):
            assert decode_unicode is True
            yield f"data: {json.dumps(completed)}"
            yield "data: [DONE]"

    monkeypatch.setattr(
        "windtower_backend.benchmark.r1_blind.requests.post",
        lambda *_args, **_kwargs: _StreamingResponse(),
    )
    provider = QwenEngineeringProvider(
        replace(load_settings(), qwen_api_key="test-key"),
        model="qwen3.8-max",
    )

    result = _call_streaming_response(provider, {"model": "qwen3.8-max"})

    assert result["status"] == "called"
    assert result["model"] == "qwen3.8-max"
    assert result["usage"]["total_tokens"] == 21


def test_post_blind_confirmation_build_and_report_are_reproducible(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "campaign"
    manifest = prepare_r1_blind(EXAMPLE_ROOT, root)
    for case in manifest["cases"]:
        alias = case["alias"]
        runs = []
        for repetition in range(1, 6):
            run = {
                "schema_version": "trusted_fem_r1_top_level_run_v1",
                "case_alias": alias,
                "repetition": repetition,
                "status": "completed",
                "engineering": {
                    "schema_version": "blind_engineering_candidate_v1",
                    "records": [{
                        "record_type": "tower_point",
                        "z_mm": 0,
                        "diameter_mm": 4400,
                        "thickness_mm": 44,
                        "source_cells": ["S01:R8C3", "S01:R8C4", "S01:R8C6"],
                        "evidence_ids": [f"{alias}:S01:C01"],
                    }],
                },
                "fem": {
                    "schema_version": "blind_fem_candidate_v1",
                    "fields": [{"path": "mesh.element_type", "value": "S4R"}],
                },
                "model": "qwen3.8-max",
                "total_tokens": 20,
                "truth_accessed": False,
            }
            _write_json(root / "runs" / alias / f"run_{repetition:02d}.json", run)
            runs.append(run)
        _write_jsonl(root / "runs" / alias / "engineering_candidate_runs.jsonl", runs, "engineering")
        _write_jsonl(root / "runs" / alias / "fem_candidate_runs.jsonl", runs, "fem")
        _write_json(root / "consensus" / f"{alias}.json", build_blind_consensus(alias, runs))
    seal = {
        "schema_version": "r1_blind_seal_v1",
        "sealed_at": "2026-08-09T00:00:00+00:00",
        "top_level_run_count": 20,
        "successful_run_count": 20,
        "all_records_retained": True,
        "confirmation_inputs_accessed": False,
        "files": _hash_tree(root / "runs") + _hash_tree(root / "consensus"),
    }
    _write_json(root / "blind_complete.json", seal)
    unlock_r1_confirmation(root, EXAMPLE_ROOT)
    monkeypatch.setattr(
        "windtower_backend.benchmark.r1_model_packages._run_datacheck",
        lambda *_args, **_kwargs: {"status": "pass", "errors": [], "warnings": ["test warning"]},
    )

    first = build_r1_models(root, datacheck=True)
    first_hashes = {
        package["model_id"]: package["inp_hashes"]
        for package in first["packages"]
    }
    second = build_r1_models(root, datacheck=False)
    second_hashes = {
        package["model_id"]: package["inp_hashes"]
        for package in second["packages"]
    }
    report = render_r1_report(root)

    assert first["package_count"] == 4
    assert first["inp_count"] == 8
    assert first_hashes == second_hashes
    assert Path(report["html"]).is_file()
    assert len(report["figures"]) == 2
    for model_id in ("T01", "T02", "T03", "T04"):
        package = root / "models" / model_id
        assert (package / "static_linear.inp").is_file()
        assert (package / "modal_linear.inp").is_file()
        assert "nlgeom=NO" in (package / "static_linear.inp").read_text(encoding="utf-8")
