"""Compiler-independent Abaqus INP semantic signatures.

This module deliberately treats an INP as solver input, not as a compiler
artifact.  It supports the keyword subset emitted by the wind-tower compiler
and keeps an explicit ``design`` slot for geometry that cannot be recovered
from a discretised mesh alone.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

SIGNATURE_VERSION = "canonical_fem_signature_v1"
DEFAULT_TOLERANCE = 1e-6


def _sha(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _number(value: str, tolerance: float) -> Any:
    try:
        number = float(value.strip())
    except ValueError:
        return value.strip().upper()
    if not math.isfinite(number):
        raise ValueError("non-finite numeric value in INP")
    rounded = round(number / tolerance) * tolerance
    return int(rounded) if abs(rounded - round(rounded)) < tolerance else rounded


def _header(line: str) -> Tuple[str, Dict[str, str]]:
    fields = [part.strip() for part in line[1:].split(",")]
    keyword = "*" + fields[0].upper()
    options: Dict[str, str] = {}
    for field in fields[1:]:
        if "=" in field:
            key, value = field.split("=", 1)
            options[key.strip().upper()] = value.strip().upper()
        elif field:
            options[field.upper()] = "YES"
    return keyword, options


def _records(lines: Iterable[str]) -> Iterable[Tuple[str, Dict[str, str], Sequence[str]]]:
    """Yield keyword blocks; comments and blank lines are not semantic records."""
    keyword = ""
    options: Dict[str, str] = {}
    body = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            if keyword:
                yield keyword, options, body
            keyword, options = _header(line)
            body = []
        elif keyword:
            body.append(line)
    if keyword:
        yield keyword, options, body


def _expand_ids(rows: Sequence[str], generate: bool) -> Sequence[int]:
    values = []
    for row in rows:
        values.extend(int(float(x.strip())) for x in row.split(",") if x.strip())
    if not generate:
        return values
    if len(values) % 3:
        raise ValueError("*NSET/*ELSET, GENERATE requires start,end,increment triples")
    expanded = []
    for start, end, increment in zip(values[0::3], values[1::3], values[2::3]):
        expanded.extend(range(start, end + (1 if increment > 0 else -1), increment))
    return expanded


def parse_inp(text: str, *, tolerance: float = DEFAULT_TOLERANCE,
              design: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Parse the supported Abaqus keyword subset into a neutral intermediate form."""
    nodes: Dict[int, Tuple[Any, Any, Any]] = {}
    elements: Dict[int, Tuple[str, Tuple[int, ...]]] = {}
    nsets: Dict[str, Sequence[int]] = {}
    elsets: Dict[str, Sequence[int]] = {}
    surfaces: Dict[str, Sequence[Tuple[str, str]]] = {}
    materials: Dict[str, Dict[str, Any]] = {}
    sections, boundaries, couplings, loads, steps = [], [], [], [], []
    current_material: Optional[str] = None
    current_step: Optional[Dict[str, Any]] = None

    for keyword, options, body in _records(text.splitlines()):
        if keyword == "*NODE":
            for row in body:
                values = [item.strip() for item in row.split(",")]
                if len(values) >= 4:
                    nodes[int(values[0])] = tuple(_number(x, tolerance) for x in values[1:4])  # type: ignore[assignment]
        elif keyword == "*ELEMENT":
            element_type = options.get("TYPE", "UNKNOWN")
            for row in body:
                values = [item.strip() for item in row.split(",") if item.strip()]
                if len(values) >= 2:
                    elements[int(values[0])] = (element_type, tuple(int(x) for x in values[1:]))
        elif keyword in ("*NSET", "*ELSET"):
            name = options.get("NSET") if keyword == "*NSET" else options.get("ELSET")
            if name:
                target = nsets if keyword == "*NSET" else elsets
                target[name] = _expand_ids(body, "GENERATE" in options)
        elif keyword == "*SURFACE":
            name = options.get("NAME")
            if name:
                surfaces[name] = [tuple(item.strip().upper() for item in row.split(",")[:2]) for row in body]
        elif keyword == "*MATERIAL":
            current_material = options.get("NAME", "UNNAMED")
            materials.setdefault(current_material, {})
        elif keyword in ("*ELASTIC", "*DENSITY", "*PLASTIC") and current_material:
            materials[current_material][keyword[1:]] = [[_number(x, tolerance) for x in row.split(",") if x.strip()] for row in body]
        elif keyword in ("*SHELL SECTION", "*SOLID SECTION", "*BEAM SECTION"):
            sections.append({"keyword": keyword, "elset": options.get("ELSET"), "material": options.get("MATERIAL"),
                             "data": [[_number(x, tolerance) for x in row.split(",") if x.strip()] for row in body]})
        elif keyword == "*STEP":
            current_step = {"name": options.get("NAME", "UNNAMED"), "nlgeom": options.get("NLGEOM", "NO"),
                            "procedure": None, "outputs": []}
            steps.append(current_step)
        elif keyword in ("*STATIC", "*FREQUENCY", "*BUCKLE", "*DYNAMIC") and current_step is not None:
            current_step["procedure"] = keyword[1:]
            current_step["procedure_data"] = [[_number(x, tolerance) for x in row.split(",") if x.strip()] for row in body]
        elif "OUTPUT" in keyword and current_step is not None:
            current_step["outputs"].append({"keyword": keyword, "options": options, "data": list(body)})
        elif keyword == "*BOUNDARY":
            boundaries.extend([[x.strip().upper() if i == 0 else _number(x, tolerance) for i, x in enumerate(row.split(",")) if x.strip()] for row in body])
        elif keyword in ("*COUPLING", "*KINEMATIC", "*DISTRIBUTING", "*EQUATION", "*MPC"):
            couplings.append({"keyword": keyword, "options": options, "data": list(body)})
        elif keyword in ("*CLOAD", "*DLOAD", "*DSLOAD", "*GRAVITY", "*MOMENT"):
            loads.append({"keyword": keyword, "step": current_step["name"] if current_step else None,
                          "options": options, "data": list(body)})

    return {"nodes": nodes, "elements": elements, "nsets": nsets, "elsets": elsets, "surfaces": surfaces,
            "materials": materials, "sections": sections, "boundaries": boundaries, "couplings": couplings,
            "loads": loads, "steps": steps, "design": dict(design or {})}


def _canonical_mesh(parsed: Mapping[str, Any]) -> Tuple[Dict[int, int], Dict[int, int], Sequence[Any], Sequence[Any]]:
    nodes = parsed["nodes"]
    degree = defaultdict(int)
    for _, (_, connection) in parsed["elements"].items():
        for node in connection:
            degree[node] += 1
    ordered = sorted(nodes, key=lambda label: (nodes[label], degree[label]))
    node_map = {label: index + 1 for index, label in enumerate(ordered)}
    canonical_nodes = [nodes[label] for label in ordered]
    ordered_elements = sorted(parsed["elements"], key=lambda label: (parsed["elements"][label][0], tuple(sorted(node_map[node] for node in parsed["elements"][label][1]))))
    element_map = {label: index + 1 for index, label in enumerate(ordered_elements)}
    canonical_elements = [(parsed["elements"][label][0], tuple(sorted(node_map[node] for node in parsed["elements"][label][1]))) for label in ordered_elements]
    return node_map, element_map, canonical_nodes, canonical_elements


def _bounds(labels: Sequence[int], nodes: Mapping[int, Any]) -> Optional[Sequence[Any]]:
    points = [nodes[label] for label in labels if label in nodes]
    if not points:
        return None
    return [[min(point[axis] for point in points), max(point[axis] for point in points)] for axis in range(3)]


def build_signature(parsed: Mapping[str, Any]) -> Dict[str, Any]:
    """Build S(I) and compact, leaf-addressable summaries for deterministic diffs."""
    node_map, element_map, node_coords, connectivity = _canonical_mesh(parsed)
    nsets = {name: sorted(node_map[x] for x in members if x in node_map) for name, members in parsed["nsets"].items()}
    elsets = {name: sorted(element_map[x] for x in members if x in element_map) for name, members in parsed["elsets"].items()}
    set_summary = {
        "nsets": {name: {"member_hash": _sha(members), "count": len(members), "bounds": _bounds(parsed["nsets"][name], parsed["nodes"])} for name, members in nsets.items()},
        "elsets": {name: {"member_hash": _sha(members), "count": len(members)} for name, members in elsets.items()},
        "surfaces": {name: {"member_hash": _sha(sorted(members)), "count": len(members)} for name, members in parsed["surfaces"].items()},
    }
    element_types = sorted(kind for kind, _ in connectivity)
    resultant = {"force": [0.0, 0.0, 0.0], "moment": [0.0, 0.0, 0.0]}
    for load in parsed["loads"]:
        if load["keyword"] == "*CLOAD":
            for row in load["data"]:
                values = [x.strip() for x in row.split(",")]
                if len(values) >= 3 and values[1].isdigit():
                    dof, value = int(values[1]), float(values[2])
                    if 1 <= dof <= 3:
                        resultant["force"][dof - 1] += value
                    if 4 <= dof <= 6:
                        resultant["moment"][dof - 4] += value
    layers = {
        "G": {"design": parsed["design"], "mesh_node_coordinates": {"hash": _sha(node_coords), "count": len(node_coords)},
              "mesh_bounds": _bounds(list(parsed["nodes"]), parsed["nodes"])},
        "E": {"element_types": element_types, "connectivity": {"hash": _sha(connectivity), "count": len(connectivity)},
              "topology_counts": {kind: element_types.count(kind) for kind in sorted(set(element_types))}},
        "R": set_summary,
        "M": {"materials": parsed["materials"], "sections": parsed["sections"]},
        "BC": {"boundary_dofs": sorted(parsed["boundaries"]), "couplings": sorted(parsed["couplings"], key=_sha)},
        "L": {"keyword_records": sorted(parsed["loads"], key=_sha), "resultants": resultant},
        "A": {"steps": parsed["steps"]},
    }
    leaves = {f"{group}.{name}": value for group, values in layers.items() for name, value in values.items()}
    return {"schema_version": SIGNATURE_VERSION, "layers": layers,
            "leaf_hashes": {path: _sha(value) for path, value in leaves.items()}}


def signature_from_file(path: Path, *, design: Optional[Mapping[str, Any]] = None,
                        tolerance: float = DEFAULT_TOLERANCE) -> Dict[str, Any]:
    return build_signature(parse_inp(path.read_text(encoding="utf-8", errors="replace"), tolerance=tolerance, design=design))


def changed_paths(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> Sequence[str]:
    before, after = baseline["leaf_hashes"], candidate["leaf_hashes"]
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def evaluate_impact(baseline: Mapping[str, Any], candidate: Mapping[str, Any], expected: Sequence[str]) -> Dict[str, Any]:
    changed, expected_set = set(changed_paths(baseline, candidate)), set(expected)
    matched = changed & expected_set
    missing, unexpected = sorted(expected_set - changed), sorted(changed - expected_set)
    return {"changed": sorted(changed), "expected": sorted(expected_set), "missing": missing, "unexpected": unexpected,
            "impact_recall": len(matched) / len(expected_set) if expected_set else 1.0,
            "unintended_change_rate": len(unexpected) / len(changed) if changed else 0.0,
            "pass": not missing and not unexpected}
