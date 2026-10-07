"""Evidence-backed parameters and deterministic checks for the L3 reference.

The reference mesh and full displacement history remain immutable fixtures.  The
IR carries their hashes and the physical parameters that a compiler must use.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


SCHEMA = "WT-L3-SOLID-TRANSFER-ELASTIC-V1"
REQUIRED = (
    "bolt.elastic_modulus_mpa", "bolt.poisson_ratio",
    "neck.elastic_modulus_mpa", "neck.poisson_ratio",
    "plate.elastic_modulus_mpa", "plate.poisson_ratio",
    "material.elastic_only", "contact.friction_coefficient",
    "bolt.pretension_n", "transfer.amplitude_count",
    "transfer.cut_node_count", "transfer.dof_per_node",
    "transfer.source_frame_count", "analysis.step_count",
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def reference_ir(path: Path) -> dict:
    lines = path.read_text(encoding="latin1").splitlines()
    fields = {}

    def add(key, value, line, unit):
        fields[key] = {"value": value, "unit": unit, "source": "INP:%d" % line}

    material_names = {"BOLT_10_9_EQ": "bolt", "Q355ND_NECK_T21": "neck", "Q355ND_PLATE_T75": "plate"}
    for i, line in enumerate(lines):
        m = re.match(r"\*Material, name=(\S+)", line, re.I)
        if m and m.group(1) in material_names:
            assert lines[i+1].lower().startswith("*elastic")
            e, nu = (float(x.strip()) for x in lines[i+2].split(",")[:2])
            prefix = material_names[m.group(1)]
            add(prefix + ".elastic_modulus_mpa", e, i+3, "MPa")
            add(prefix + ".poisson_ratio", nu, i+3, "1")
    amps = [(i+1, line) for i, line in enumerate(lines) if line.lower().startswith("*amplitude,")]
    steps = [(i+1, line) for i, line in enumerate(lines) if line.lower().startswith("*step,")]
    service = lines.index(next(x for x in lines if x.startswith("*Step, name=STEP_3_SERVICE_LOAD")))
    cut = set()
    cut_dofs = set()
    for line in lines[service:]:
        m = re.match(r"PART-1-1\.(\d+), ([123]), [123], 1\.0", line)
        if m:
            cut.add(int(m.group(1)))
            cut_dofs.add(int(m.group(2)))
    friction = next((i for i, line in enumerate(lines) if line.lower().startswith("*friction")), None)
    preload = next((i for i, line in enumerate(lines) if line.startswith("_SURF_BOLTLOAD-1_blrn_, 1,")), None)
    history_line = next((i for i, line in enumerate(lines) if "source frames x" in line and "cut DOFs" in line), None)
    history_frames = int(re.search(r"(\d+) source frames", lines[history_line]).group(1))
    add("material.elastic_only", not any(x.lower().startswith("*plastic") for x in lines), 52861, "bool")
    add("contact.friction_coefficient", float(lines[friction+1].strip().rstrip(",")), friction+2, "1")
    add("bolt.pretension_n", float(lines[preload].split(",")[-1]), preload+1, "N")
    add("transfer.amplitude_count", len(amps), amps[0][0], "count")
    add("transfer.cut_node_count", len(cut), service+1, "count")
    add("transfer.dof_per_node", len(cut_dofs), service+1, "count")
    add("transfer.source_frame_count", history_frames, history_line+1, "count")
    add("analysis.step_count", len(steps), steps[0][0], "count")
    return {"schema_version": SCHEMA, "reference_inp_sha256": sha256(path), "fields": fields}


def validate(candidate: dict, truth: dict) -> dict:
    fields = candidate.get("fields") if isinstance(candidate, dict) else None
    if not isinstance(fields, dict):
        fields = {}
    expected = truth["fields"]
    correct, units, sources = [], [], []
    for key in REQUIRED:
        record = fields.get(key)
        if not isinstance(record, dict):
            continue
        value = record.get("value")
        target = expected[key]["value"]
        if type(target) is bool:
            equal = type(value) is bool and value == target
        elif isinstance(value, (int, float)):
            equal = abs(float(value)-float(target)) <= max(1e-8, abs(float(target))*1e-8)
        else:
            equal = False
        if equal:
            correct.append(key)
        if record.get("unit") == expected[key]["unit"]:
            units.append(key)
        if re.match(r"^INP:\d+$", str(record.get("source") or "")):
            at = int(str(record["source"]).split(":")[1])
            if abs(at-int(expected[key]["source"].split(":")[1])) <= 2:
                sources.append(key)
    return {"required": len(REQUIRED), "present": len(set(REQUIRED)&set(fields)), "correct": len(correct),
            "unit_correct": len(units), "source_correct": len(sources), "unsupported": sorted(set(fields)-set(REQUIRED)),
            "missing": sorted(set(REQUIRED)-set(fields)), "pass": len(correct)==len(REQUIRED) and len(units)==len(REQUIRED)
            and len(sources)==len(REQUIRED) and not (set(fields)-set(REQUIRED))}
