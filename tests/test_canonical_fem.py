from windtower_backend.canonical_fem import build_signature, evaluate_impact, parse_inp
from windtower_backend.parameter_patch_experiment import apply_patch_to_parameter_set, summarize, validate_patch


BASE = """** ignored\n*NODE\n10,0,0,0\n20,1,0,0\n30,1,1,0\n40,0,1,0\n*ELEMENT, TYPE=S4R\n99,10,20,30,40\n*NSET,NSET=BASE\n10,20\n*ELSET,ELSET=SHELL\n99\n*SURFACE,NAME=FACE\nSHELL,S1\n*MATERIAL,NAME=STEEL\n*ELASTIC\n206000,0.3\n*SHELL SECTION,ELSET=SHELL,MATERIAL=STEEL\n20\n*BOUNDARY\nBASE,1,3\n*STEP,NAME=STATIC,NLGEOM=NO\n*STATIC\n*DLOAD\nSHELL,P,1.0\n*OUTPUT,FIELD\n*NODE OUTPUT\nU\n*END STEP\n"""


def test_renumbered_equivalent_inp_has_no_changed_objects():
    renumbered = BASE.replace("10,0,0,0", "101,0,0,0").replace("20,1,0,0", "102,1,0,0").replace("30,1,1,0", "103,1,1,0").replace("40,0,1,0", "104,0,1,0").replace("99,10,20,30,40", "909,101,102,103,104").replace("10,20", "101,102").replace("99\n*SURFACE", "909\n*SURFACE")
    baseline = build_signature(parse_inp(BASE))
    assert evaluate_impact(baseline, build_signature(parse_inp(renumbered)), []) ["changed"] == []


def test_material_change_is_reported_as_leaf_change():
    baseline = build_signature(parse_inp(BASE))
    candidate = build_signature(parse_inp(BASE.replace("206000,0.3", "210000,0.3")))
    result = evaluate_impact(baseline, candidate, ["M.materials"])
    assert result["pass"] and result["impact_recall"] == 1.0


def test_output_request_is_part_of_analysis_signature():
    baseline = build_signature(parse_inp(BASE))
    candidate = build_signature(parse_inp(BASE.replace("*NODE OUTPUT\nU", "*NODE OUTPUT\nU, RF")))
    assert evaluate_impact(baseline, candidate, ["A.steps"])["pass"]


def test_patch_gate_and_summary_are_local_and_deterministic():
    allowed = ["material.elastic_modulus_mpa"]
    ranges = {allowed[0]: {"min": 1.0, "max": 300000.0}}
    assert validate_patch({"operations": [{"op": "replace", "path": allowed[0], "value": 210000, "unit": "MPa"}]}, allowed, ranges) == []
    assert "PATCH_PATH_FORBIDDEN" in validate_patch({"operations": [{"op": "replace", "path": "loads.x", "value": 1, "unit": "N"}]}, allowed, ranges)
    patched = apply_patch_to_parameter_set({"parameters": {allowed[0]: {"key": allowed[0], "unit": "MPa", "value": 206000}}}, {"operations": [{"op": "replace", "path": allowed[0], "value": 210000, "unit": "MPa"}]})
    assert patched["parameters"][allowed[0]]["value"] == 210000
    assert summarize([{ "structural_pass": True, "contract_pass": True, "compile_status": "PASS", "semantic_pass": True}])["semantic_pass"] == 1
