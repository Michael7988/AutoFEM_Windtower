from __future__ import annotations

import json

from core.windtower_backend.benchmark import mapping_code_benchmark as subject


def test_ast_gate_accepts_contract_and_blocks_io() -> None:
    good = "def compile_module(parameter_set, semantic_plan, regions, context):\n    return {'module_id':'x','data':{},'blocks':[],'consumed_parameters':[]}\n"
    assert subject.ast_gate(good)["status"] == "pass"
    bad = "import os\ndef compile_module(a,b,c,d):\n    return open('x').read()\n"
    result = subject.ast_gate(bad)
    assert result["status"] == "blocked"
    assert {item["code"] for item in result["issues"]} >= {"forbidden_import", "forbidden_name"}


def test_inp_gate_requires_real_entities_and_forbids_mass() -> None:
    valid = """*Heading
*Node
1,0,0,0
*Element, type=S4R
1,1,1,1,1
*Material, name=S
*Elastic
2.1e5,0.3
*Shell Section, elset=E, material=S
10
*Boundary
B,1,6
*Step
*Static
1,1
*End Step
"""
    assert subject.inp_gate(valid, "static")["status"] == "pass"
    assert subject.inp_gate(valid + "*Mass\n1,1\n", "static")["status"] == "blocked"


def test_semantic_gate_blocks_empty_set() -> None:
    text = """*Node
1,0,0,0
*Element, type=S4R
1,1,1,1,1
*Elset, elset=E
1
*Elset, elset=EMPTY
*Shell Section, elset=E, material=S
10
"""
    result = subject.semantic_inp_gate(text, "modal")
    assert result["status"] == "blocked"
    assert any(issue["code"] == "empty_set" for issue in result["issues"])


def test_exact_logical_call_registry() -> None:
    assert len(subject.LOGICAL_CALLS) == 10
    assert len([item for item in subject.LOGICAL_CALLS if item.startswith("qwen:")]) == 8
    assert f"cross:{subject.GPT_MODEL}" in subject.LOGICAL_CALLS
    assert f"cross:{subject.DEEPSEEK_MODEL}" in subject.LOGICAL_CALLS


def test_projection_bounds_bulk_records(tmp_path) -> None:
    source = tmp_path / "x.inp"
    source.write_text("*Node\n" + "\n".join(f"{i},0,0,{i}" for i in range(100)) + "\n*Material, name=S\n*Elastic\n210000,0.3\n", encoding="utf-8")
    projection = subject._project_inp(source)
    node = next(item for item in projection["blocks"] if item["keyword"] == "*NODE")
    assert node["line_count"] == 101
    assert "DATA LINES OMITTED" in node["text"]
    assert projection["source_sha256"] == subject._sha_file(source)
