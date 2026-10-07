"""File-contract adapter for the independently developed shell solver used in E4."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any

from ..l0 import enrich_l0_ir
from ..l0.implementation import build_l0_sets
from ..modeling import ProjectState, build_preview_mesh, find_flange_ranges, geometry_from_ir


NATIVE_INPUT_SCHEMA = "windtower_native_solver_input_v1"
NATIVE_OUTPUT_SCHEMA = "windtower_native_solver_output_v1"


def export_native_solver_input(
    state: ProjectState,
    path: str | Path,
    *,
    analysis_profile: str = "linear_verification",
) -> dict[str, Any]:
    if analysis_profile != "linear_verification":
        raise ValueError("The native-solver comparison is frozen to linear_verification.")
    ir = enrich_l0_ir(state, state.ir)
    ir["analysis_profile"] = analysis_profile
    ir["analysis_cases"]["static_strength"]["nlgeom"] = False
    ir["analysis_cases"]["modal"]["prestress"] = False
    tower, _flanges, door = geometry_from_ir(ir)
    nodes, elements, door_nodes = build_preview_mesh(tower, door, ir["mesh_and_set_ir"])
    sets = build_l0_sets(nodes, elements, door_nodes, find_flange_ranges(tower, []), ir)
    native_sections = _native_sections(tower)
    section_by_element = _section_by_element(nodes, elements, native_sections)
    payload = {
        "schema_version": NATIVE_INPUT_SCHEMA,
        "project_id": state.project_id,
        "analysis_profile": analysis_profile,
        "unit_system": ir["unit_system"],
        "nodes": [{"id": node_id, "xyz": list(nodes[node_id])} for node_id in sorted(nodes)],
        "elements": [
            {
                "id": element_id,
                "type": "S4R-compatible-quad",
                "connectivity": elements[element_id],
                "section_id": section_by_element[element_id],
            }
            for element_id in sorted(elements)
        ],
        "sections": [
            {"id": index + 1, "thickness_mm": float(section["thickness_mm"])}
            for index, section in enumerate(native_sections)
        ],
        "material": {
            "elastic_modulus_mpa": float(ir["semantic_ir"]["materials"][0]["elastic_modulus_mpa"]),
            "poisson_ratio": float(ir["semantic_ir"]["materials"][0]["poisson_ratio"]),
            "density_tonne_mm3": float(ir["semantic_ir"]["materials"][0].get("density") or 0.0),
        },
        "boundary": {"fixed_node_ids": sorted(sets["base_nodes"]), "dofs": [1, 2, 3, 4, 5, 6]},
        "loads": {
            "top_node_ids": sorted(sets["top_nodes"]),
            "top_equivalent_system": ir["top_equivalent_system"],
            "static": ir["analysis_cases"]["static_strength"],
            "wind": ir["wind_load"],
        },
        "modal": {"modes": int(ir["analysis_cases"]["modal"]["modes"]), "prestress": False},
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(output), "sha256": _sha256(output), "payload": payload}


def run_native_solver(
    input_path: str | Path,
    output_path: str | Path,
    *,
    command: str | None = None,
    timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    command_template = command or os.getenv("WINDTOWER_NATIVE_SOLVER_COMMAND", "").strip()
    if not command_template:
        return {
            "status": "blocked",
            "reason": "WINDTOWER_NATIVE_SOLVER_COMMAND is not configured; E4 cannot be marked complete.",
        }
    source = Path(input_path).resolve()
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    formatted = command_template.format(input=str(source), output=str(target))
    if "{input}" not in command_template and "{output}" not in command_template:
        formatted = f'{formatted} "{source}" "{target}"'
    completed = subprocess.run(
        shlex.split(formatted, posix=False),
        cwd=target.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    audit = {
        "status": "completed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "input_sha256": _sha256(source),
    }
    if completed.returncode != 0:
        return audit
    if not target.is_file():
        return {**audit, "status": "failed", "reason": "Native solver did not create the declared output JSON."}
    result = json.loads(target.read_text(encoding="utf-8"))
    validation = validate_native_solver_output(result)
    return {
        **audit,
        "status": "completed" if validation["status"] == "pass" else "failed",
        "output_sha256": _sha256(target),
        "validation": validation,
        "result": result,
    }


def validate_native_solver_output(payload: dict[str, Any]) -> dict[str, Any]:
    missing = []
    if payload.get("schema_version") != NATIVE_OUTPUT_SCHEMA:
        missing.append("schema_version")
    for key in ("displacements", "stresses", "reactions", "frequencies_hz", "mode_shapes"):
        if key not in payload:
            missing.append(key)
    return {"status": "pass" if not missing else "blocked", "missing_fields": missing}


def _section_by_element(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    tower: list[dict[str, Any]],
) -> dict[int, int]:
    result: dict[int, int] = {}
    for element_id, connectivity in elements.items():
        center_z = sum(nodes[node_id][2] for node_id in connectivity) / len(connectivity)
        section_id = next(
            (
                index + 1
                for index, section in enumerate(tower)
                if float(section["z_bottom_mm"]) - 1.0e-6 <= center_z <= float(section["z_top_mm"]) + 1.0e-6
            ),
            len(tower),
        )
        result[element_id] = section_id
    return result


def _native_sections(tower: list[dict[str, Any]]) -> list[dict[str, float]]:
    """Normalize L0 profile points (or legacy sections) to element section spans."""
    if tower and "z_mm" in tower[0]:
        return [
            {
                "z_bottom_mm": float(tower[index]["z_mm"]),
                "z_top_mm": float(tower[index + 1]["z_mm"]),
                "thickness_mm": float(tower[index]["thickness_mm"]),
            }
            for index in range(len(tower) - 1)
        ]
    return [
        {
            "z_bottom_mm": float(section["z_bottom_mm"]),
            "z_top_mm": float(section["z_top_mm"]),
            "thickness_mm": float(section["thickness_mm"]),
        }
        for section in tower
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
