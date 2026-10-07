from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any


def extract_l2_odb(state: Any, settings: Any, odb_path: Path) -> dict[str, Any]:
    """Run a sealed Abaqus-Python ODB extractor and return its JSON result."""
    results_root = state.root / "results"
    results_root.mkdir(parents=True, exist_ok=True)
    output_path = results_root / "l2_solver_extract.json"
    request_path = results_root / "l2_extract_request.json"
    script_path = results_root / "extract_l2_odb.py"
    values = {
        key: record.get("value")
        for key, record in ((state.ir.get("parameter_set") or {}).get("parameters") or {}).items()
        if isinstance(record, dict)
    }
    request_path.write_text(json.dumps({"parameters": values}, ensure_ascii=False, indent=2), encoding="utf-8")
    script_path.write_text(_ABAQUS_SCRIPT, encoding="utf-8")
    command = [str(settings.abaqus_cmd), "python", str(script_path), str(odb_path), str(output_path), str(request_path)]
    completed = subprocess.run(
        command, cwd=results_root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    (results_root / "l2_extract_stdout.log").write_text(completed.stdout or "", encoding="utf-8")
    (results_root / "l2_extract_stderr.log").write_text(completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0 or not output_path.is_file():
        return {
            "status": "blocked", "code": "ODB_EXTRACTION_FAILED",
            "returncode": completed.returncode,
            "message": (completed.stderr or completed.stdout or "Abaqus ODB extraction failed.")[-2000:],
        }
    return json.loads(output_path.read_text(encoding="utf-8"))


def latest_l2_odb(state: Any) -> Path | None:
    candidates = list((state.root / "jobs").rglob("*.odb"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


_ABAQUS_SCRIPT = r'''from __future__ import print_function
from odbAccess import openOdb
import json, math, sys

odb_path, output_path, request_path = sys.argv[-3:]
request = json.load(open(request_path, 'r'))
params = request.get('parameters', {})
odb = openOdb(path=odb_path, readOnly=True)
assembly = odb.rootAssembly

def repo_get(repository, key):
    return repository[key] if key in repository.keys() else None

instance = repo_get(assembly.instances, 'L2-1') or repo_get(assembly.instances, 'L2-1'.upper())

def field(frame, name):
    return repo_get(frame.fieldOutputs, name)

def subset_values(frame, name, region):
    value = field(frame, name)
    return list(value.getSubset(region=region).values) if value is not None and region is not None else []

def vector_sum(values):
    result = [0.0, 0.0, 0.0]
    for value in values:
        data = list(value.data)
        for index in range(min(3, len(data))): result[index] += float(data[index])
    return result

def norm(value): return math.sqrt(sum(float(item) ** 2 for item in value))

def component_values(frame, prefix, component):
    result = []
    for key, output in frame.fieldOutputs.items():
        if not str(key).upper().startswith(prefix): continue
        labels = [str(item).upper() for item in getattr(output, 'componentLabels', ())]
        index = labels.index(component) if component in labels else None
        for value in output.values:
            data = value.data
            if index is not None and hasattr(data, '__len__'): result.append(float(data[index]))
            elif not hasattr(data, '__len__'): result.append(float(data))
    return result

def direct_values(frame, prefix):
    result = []
    for key, output in frame.fieldOutputs.items():
        if not str(key).upper().startswith(prefix): continue
        for value in output.values:
            data = value.data
            if hasattr(data, '__len__'): result.extend(float(item) for item in data)
            else: result.append(float(data))
    return result

steps = [odb.steps[key] for key in odb.steps.keys()]
hold_step = steps[1] if len(steps) > 1 else steps[-1]
service_step = steps[-1]
hold = hold_step.frames[-1]
service = service_step.frames[-1]
bolt_set = repo_get(instance.elementSets, 'BOLT_ELEMENTS') if instance else None
base_set = repo_get(assembly.nodeSets, 'BASE_FIXED') or (repo_get(instance.nodeSets, 'BASE_FIXED') if instance else None)
top_set = repo_get(assembly.nodeSets, 'TOP_REFERENCE_POINT') or (repo_get(instance.nodeSets, 'TOP_REFERENCE_POINT') if instance else None)

bolt_values = subset_values(hold, 'SF', bolt_set)
sf_output = field(hold, 'SF')
sf_labels = [str(item) for item in getattr(sf_output, 'componentLabels', ())] if sf_output is not None else []
sf_component_max = []
if bolt_values:
    component_count = len(list(bolt_values[0].data))
    for component_index in range(component_count):
        sf_component_max.append(max(abs(float(list(value.data)[component_index])) for value in bolt_values))
bolts = []
for value in bolt_values:
    data = list(value.data)
    bolts.append({'element_label': int(value.elementLabel), 'axial_force_n': float(data[0]) if data else None})
target = float(params.get('interaction.pretension.force_per_bolt_n') or 0.0)
errors = [abs(abs(item['axial_force_n']) - target) / max(abs(target), 1.0) for item in bolts if item['axial_force_n'] is not None]

base_rf_values = subset_values(service, 'RF', base_set)
base_rm_values = subset_values(service, 'RM', base_set)
base_rf = vector_sum(base_rf_values)
base_rm = vector_sum(base_rm_values)
coordinates = {}
if instance:
    for node in instance.nodes: coordinates[int(node.label)] = tuple(node.coordinates)
for value in base_rf_values:
    xyz = coordinates.get(int(value.nodeLabel), (0.0, 0.0, 0.0)); force = list(value.data)
    base_rm[0] += xyz[1] * force[2] - xyz[2] * force[1]
    base_rm[1] += xyz[2] * force[0] - xyz[0] * force[2]
    base_rm[2] += xyz[0] * force[1] - xyz[1] * force[0]

mode = str(params.get('loads.service_control_mode') or 'section_loads')
if mode == 'section_displacement':
    applied_force = vector_sum(subset_values(service, 'RF', top_set))
    applied_moment = vector_sum(subset_values(service, 'RM', top_set))
else:
    applied_force = [float(params.get('loads.shear_x_n') or 0), float(params.get('loads.shear_y_n') or 0), float(params.get('loads.section_force_n') or 0)]
    applied_moment = [float(params.get('loads.moment_x_nmm') or 0), float(params.get('loads.moment_y_nmm') or 0), float(params.get('loads.torsion_nmm') or 0)]
eta_force = norm([base_rf[i] + applied_force[i] for i in range(3)]) / max(norm(applied_force), 1.0)
eta_moment = norm([base_rm[i] + applied_moment[i] for i in range(3)]) / max(norm(applied_moment), 1.0)

cpress = direct_values(service, 'CPRESS') or component_values(service, 'CSTRESS', 'CPRESS')
copen = direct_values(service, 'COPEN') or component_values(service, 'CDISP', 'COPEN')
stress = field(service, 'S')
mises = [float(value.mises) for value in (stress.values if stress is not None else []) if getattr(value, 'mises', None) is not None]
payload = {
 'status': 'pass', 'step_names': [step.name for step in steps],
 'bolts': bolts, 'sf_component_labels': sf_labels, 'sf_component_max_abs': sf_component_max,
 'max_pretension_error_ratio': max(errors) if errors else None,
 'contact': {'max_pressure_mpa': max(cpress) if cpress else None, 'max_opening_mm': max(copen) if copen else None, 'contact_value_count': len(cpress)},
 'local_stress': {'max_mises_mpa': max(mises) if mises else None},
 'base_reaction_force': base_rf, 'base_reaction_moment': base_rm,
 'applied_force': applied_force, 'applied_moment': applied_moment,
 'eta_force': eta_force, 'eta_moment': eta_moment,
 'increments': sum(max(0, len(step.frames) - 1) for step in steps),
 'available_fields': sorted([str(key) for key in service.fieldOutputs.keys()]),
}
json.dump(payload, open(output_path, 'w'), indent=2)
odb.close()
'''
