from pathlib import Path

from windtower_backend.benchmark.frontier_direct_inp import (
    MODELS,
    SYSTEM_PROMPT,
    _enhanced_gate,
    _parse_inp_semantics,
)


def test_frontier_model_ids_are_the_n1n_ids() -> None:
    assert MODELS == (
        "gpt-5.6-sol",
        "gpt-6-astra",
        "claude-fable-5-1",
        "qwen3.8-max",
        "deepseek-v4-pro",
    )


def test_direct_prompt_has_no_reference_artifact_leakage() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "parameterset" in lowered
    assert "do not output json" in lowered
    assert "reference inp" not in lowered
    assert "reference odb" not in lowered


def test_enhanced_gate_blocks_truncation_and_wrong_analysis() -> None:
    text = "*HEADING\n*NODE\n1,0,0,0\n*ELEMENT, TYPE=S4R\n1,1,1,1,1\n*MATERIAL\n*STEP\n*STATIC\n"
    gate = _enhanced_gate(text, "length")
    codes = {item["code"] for item in gate["issues"]}
    assert gate["status"] == "blocked"
    assert "probable_truncation" in codes
    assert "provider_output_truncated" in codes


def test_semantic_parser_detects_degenerate_element_and_loads() -> None:
    text = """*HEADING
*NODE
1,0,0,0
2,1,0,0
3,1,0,1
*ELEMENT, TYPE=S4R, ELSET=ALL_ELEMENTS
1,1,2,3,1
*MATERIAL, NAME=Q345
*ELASTIC
206000,0.3
*DENSITY
7.85e-9
*SHELL SECTION, ELSET=ALL_ELEMENTS, MATERIAL=Q345
12
*BOUNDARY
BASE_FIXED,1,6
*CLOAD
TOP_RP_NSET,1,1000000
*DSLOAD
WIND_BAND_01,P,-0.00145
*STEP
*STATIC
*END STEP
"""
    parsed = _parse_inp_semantics(text)
    assert parsed["node_records"] == 3
    assert parsed["element_records"] == 1
    assert parsed["degenerate_elements_explicit"] == 1
    assert parsed["elastic"][0] == [206000.0, 0.3]
    assert parsed["cloads"][0]["value"] == 1000000.0
