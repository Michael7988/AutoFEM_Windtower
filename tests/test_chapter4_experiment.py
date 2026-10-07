from pathlib import Path

import pytest

from core.windtower_backend.benchmark.chapter4_experiment import (
    _classify_campaign,
    audit_experiments,
)


def test_history_classification_is_conservative() -> None:
    assert _classify_campaign(Path("docling_evidence_20260901"))[0] == "incomplete"
    assert _classify_campaign(Path("paper_current_framework_20260828"))[0] == "regression_reference"
    assert _classify_campaign(Path("legacy"))[0] == "excluded"


def test_audit_keeps_zero_token_calls_out_of_model_failure(tmp_path: Path) -> None:
    campaign = tmp_path / "three_path_four_case_demo"
    campaign.mkdir()
    (campaign / "runs.csv").write_text("calls,total_tokens\n1,0\n", encoding="utf-8")
    result = audit_experiments(tmp_path, tmp_path / "out")
    row = next(item for item in result["rows"] if item["campaign"] == campaign.name)
    assert row["zero_token_called_rows"] == 1
    assert result["policy"]["zero_token_called_rows_are_model_failures"] is False


def test_prepare_refuses_nonempty_root(tmp_path: Path) -> None:
    from core.windtower_backend.benchmark.chapter4_experiment import prepare_chapter4_campaign

    root = tmp_path / "occupied"
    root.mkdir()
    (root / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(FileExistsError):
        prepare_chapter4_campaign(root, tmp_path)


def test_direct_runner_has_explicit_attempt_limit() -> None:
    import inspect
    from core.windtower_backend.benchmark.three_path_experiment import run_direct_inp

    assert inspect.signature(run_direct_inp).parameters["max_attempts"].default == 3
