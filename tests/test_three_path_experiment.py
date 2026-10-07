from pathlib import Path

from windtower_backend.benchmark.three_path_experiment import (
    DIRECT_CONTRACT,
    SKILL_FILES,
    prepare_three_path_campaign,
    status_campaign,
)


def test_prepare_three_path_campaign_is_append_only_and_records_frozen_inputs(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    result = prepare_three_path_campaign(
        tmp_path, Path(__file__).parents[1] / "core" / "example", root
    )
    assert result["schema_version"] == "three_path_four_case_campaign_v1"
    assert result["repeats"] == {"local": 2, "llm_ir": 5, "direct_inp": 5}
    assert len(result["cases"]) == 4
    assert set(result["skill_hashes"]) == set(SKILL_FILES)
    assert "ParameterSet" in DIRECT_CONTRACT
    assert status_campaign(root)["expected_run_records"] == 68


def test_three_path_prompts_do_not_embed_reference_artifacts() -> None:
    prompt = "\n".join(SKILL_FILES.values()).lower()
    assert "reference inp" not in prompt
    assert "reference odb" not in prompt
    assert "another method's artifacts" in prompt
