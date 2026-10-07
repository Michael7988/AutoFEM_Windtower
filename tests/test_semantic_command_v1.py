from __future__ import annotations

import json
from pathlib import Path

import pytest

from windtower_backend.parameter_ir import parameter_set_from_ir
from windtower_backend.services.semantic_commands import (
    SemanticCommandRequest,
    repair_command_v1,
    semantic_command_v1,
)


def _parameter_set():
    ir = json.loads(Path("tests/fixtures/synthetic_l0_ir.json").read_text(encoding="utf-8"))
    ir.setdefault("semantic_ir", {}).setdefault("loads_inventory", {})[
        "center_of_mass_offset_mm"
    ] = 0.0
    return parameter_set_from_ir("wt-semantic", ir)


TASKS = [
    ("geometry.door.width_mm", 1600.0, ["门洞宽度调整为1600 mm", "door width set 1.6 m", "将门洞宽度设为160厘米"]),
    ("geometry.door.height_mm", 3000.0, ["门洞高度设为3000毫米", "door height 3 m", "门洞高度调整到300 cm"]),
    ("geometry.door.bottom_z_mm", 500.0, ["门洞底标高500 mm", "door bottom 0.5 m", "门洞底部标高50厘米"]),
    ("mesh.global_size_mm", 350.0, ["全局网格调整为350 mm", "global mesh 0.35 m", "全局网格35厘米"]),
    ("mesh.door_size_mm", 120.0, ["门洞网格设为120 mm", "door local mesh 0.12 m", "门洞局部网格12厘米"]),
    ("material.elastic_modulus_mpa", 206000.0, ["弹性模量206000 MPa", "elastic modulus 206 GPa", "弹性模量206吉帕"]),
    ("material.poisson_ratio", 0.3, ["泊松比0.3", "Poisson ratio 0.30", "poisson's ratio set 0.3"]),
    ("loads.top_mass_tonne", 350.0, ["顶部质量350吨", "top mass 350 tonne", "顶部质量350000 kg"]),
    ("loads.center_of_mass_offset_mm", 0.0, ["质心偏心距0 mm", "center of mass offset 0 m", "偏心距0毫米"]),
    ("loads.top_horizontal_force_n", 600000.0, ["顶部水平力600000 N", "top horizontal force 600 kN", "顶部水平力600千牛"]),
    ("loads.wind_pressure_mpa", 0.001, ["风压0.001 MPa", "wind pressure 1 kPa", "风压1000 Pa"]),
    ("analysis.modal_modes", 20, ["模态阶数20", "modal modes 20", "将模态阶数设置为20"]),
]


@pytest.mark.parametrize(("key", "expected", "expressions"), TASKS)
def test_twelve_semantic_tasks_have_three_equivalent_rephrasings(key, expected, expressions):
    parameter_set = _parameter_set()
    for instruction in expressions:
        result = semantic_command_v1(
            SemanticCommandRequest(stage="parameter_change", instruction=instruction),
            parameter_set,
        )
        assert result["status"] == "proposed", instruction
        patch = next(item for item in result["canonical_parameter_patch"] if item["parameter_key"] == key)
        assert patch["value"] == pytest.approx(expected)
        assert patch["requires_engineer_confirmation"] is True
        assert result["execution_policy"]["may_generate_abaqus_keywords_or_inp"] is False


@pytest.mark.parametrize("instruction", [
    "把 mesh.strategy 修改为 free mesh",
    "将单元类型改为 C3D8R",
    "change S4R to S8R",
])
def test_fixed_fem_rule_mutations_are_blocked(instruction):
    result = semantic_command_v1(
        SemanticCommandRequest(stage="parameter_change", instruction=instruction),
        _parameter_set(),
    )
    assert result["status"] == "blocked"
    assert result["forbidden_parameters"]


@pytest.mark.parametrize("instruction", ["把它调大一点", "修改未知参数 foo", "不要修改任何参数"])
def test_ambiguous_unknown_and_negative_commands_require_clarification(instruction):
    result = semantic_command_v1(
        SemanticCommandRequest(stage="parameter_change", instruction=instruction),
        _parameter_set(),
    )
    assert result["status"] == "clarification_required"


REPAIR_FAULTS = [
    ("parameter", "unconfirmed", "loads.center_of_mass_offset_mm"),
    ("parameter", "missing", "geometry.door.width_mm"),
    ("parameter", "range", "geometry.door.height_mm"),
    ("parameter", "invalid", "material.poisson_ratio"),
    ("model", "MESH_NOT_READY", None),
    ("model", "DUPLICATE_NODES", None),
    ("model", "DEGENERATE_ELEMENT", None),
    ("model", "NON_MANIFOLD_EDGE", None),
    ("model", "NORMAL_INCONSISTENCY", None),
    ("model", "INP_KEYWORD_ERROR", None),
    ("environment", "ABAQUS_UNAVAILABLE", None),
    ("environment", "LICENSE_UNAVAILABLE", None),
]


@pytest.mark.parametrize(("layer", "code", "key"), REPAIR_FAULTS)
def test_twelve_fault_injections_only_produce_preview_or_manual_diagnosis(layer, code, key):
    result = repair_command_v1([{
        "layer": layer,
        "code": code,
        "parameter_key": key,
        "message": f"fault: {code}",
        "resolution_action": "review",
    }], _parameter_set())
    assert result["status"] == ("proposed" if code == "unconfirmed" else "manual_required")
    assert result["execution_policy"]["automatic_apply"] is False
    assert result["execution_policy"]["may_generate_inp"] is False
    assert result["execution_policy"]["may_execute_solver"] is False
