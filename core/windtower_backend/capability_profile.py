from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any


CAPABILITY_ID = "WT-SHELL-DOOR-STATIC-MODAL-V1"
L2_CAPABILITY_ID = "WT-FLANGE-BOLT-CONTACT-PRELOAD-V1"
CAPABILITY_REVISION = 3
CAPABILITY_SCHEMA_VERSION = "modeling_capability_profile_v2"
MESH_STRATEGY = "V3_CONFORMING_DOOR_MESH"
MESH_STRATEGY_FAMILY = "structured_door_refinement_v1"

FORMAL_INPUT_EXTENSIONS = (
    ".xls", ".xlsx", ".xlsm", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".txt", ".md", ".docx", ".pdf",
)
EXPLORATORY_INPUT_EXTENSIONS = (
    ".pptx", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
)
REFERENCE_ONLY_INPUT_EXTENSIONS = (".inp",)

CANONICAL_DEFAULTS: dict[str, Any] = {
    "mesh.strategy": MESH_STRATEGY,
    "mesh.strategy_family": MESH_STRATEGY_FAMILY,
    "mesh.element_type": "S4R",
    "mesh.global_size_mm": 650.0,
    "mesh.door_size_mm": 120.0,
    "mesh.patch_margin_u_mm": 1450.0,
    "mesh.patch_margin_z_mm": 900.0,
    "mesh.hole_layers": 32,
    "analysis.static_enabled": True,
    "analysis.modal_enabled": True,
    "analysis.modal_modes": 20,
    "boundary.name": "BASE_FIXED",
}

L2_CANONICAL_DEFAULTS: dict[str, Any] = {
    "geometry.lower_segment_height_mm": 1500.0,
    "geometry.upper_segment_height_mm": 1500.0,
    "geometry.tower_diameter_mm": 4244.0,
    "geometry.tower_thickness_mm": 21.5,
    "geometry.flange.outer_diameter_mm": 4244.0,
    "geometry.flange.inner_diameter_mm": 3818.0,
    "geometry.flange.thickness_mm": 75.0,
    "geometry.flange.contact_inner_radius_mm": 1909.0,
    "geometry.flange.contact_outer_radius_mm": 2122.0,
    "geometry.bolt.count": 96,
    "geometry.bolt.circle_diameter_mm": 4054.0,
    "geometry.bolt.diameter_mm": 64.0,
    "geometry.bolt.hole_diameter_mm": 67.0,
    "geometry.bolt.effective_area_mm2": 2676.0,
    "material.elastic_modulus_mpa": 206000.0,
    "material.poisson_ratio": 0.3,
    "material.density": 7.85e-9,
    "material.bolt_elastic_modulus_mpa": 210000.0,
    "material.bolt_poisson_ratio": 0.3,
    "interaction.contact.normal_behavior": "hard",
    "interaction.contact.allow_separation": True,
    "interaction.contact.friction_coefficient": 0.2,
    "interaction.pretension.formulation": "native_pretension",
    "interaction.pretension.force_per_bolt_n": 1688400.0,
    "interaction.pretension.initial_increment": 0.05,
    "mesh.element_type": "S4R",
    "mesh.bolt_element_type": "B31",
    "mesh.global_size_mm": 62.5,
    "mesh.flange_size_mm": 35.0,
    "mesh.bolt_pad_size_mm": 43.5,
    "mesh.circumferential_divisions": 384,
    "mesh.flange_hole_segments": 16,
    "mesh.flange_radial_layers": 5,
    "loads.section_force_n": 0.0,
    "loads.shear_x_n": 0.0,
    "loads.shear_y_n": 0.0,
    "loads.torsion_nmm": 0.0,
    "loads.moment_x_nmm": 0.0,
    "loads.moment_y_nmm": 0.0,
    "loads.service_control_mode": "section_displacement",
    "boundary.service.u1_mm": 2.22298,
    "boundary.service.u2_mm": 0.0,
    "boundary.service.u3_mm": -1.13985,
    "boundary.service.ur1_rad": 0.0,
    "boundary.service.ur2_rad": 0.0,
    "boundary.service.ur3_rad": 0.0,
    "analysis.preload_step_name": "STEP_1_BOLT_PRELOAD",
    "analysis.hold_step_name": "STEP_2_PRELOAD_HOLD",
    "analysis.service_step_name": "STEP_3_SERVICE_LOAD",
    "analysis.nlgeom": True,
}

INPUT_FORMAT_REGISTRY: dict[str, Any] = {
    "formal": {
        "extensions": list(FORMAL_INPUT_EXTENSIONS),
        "constraints": {".pdf": "text_layer_required"},
        "public_entry_allowed": True,
    },
    "exploratory": {
        "extensions": list(EXPLORATORY_INPUT_EXTENSIONS),
        "constraints": {".pdf": "scanned_pdf_is_exploratory"},
        "public_entry_allowed": True,
    },
    "reference_only": {
        "extensions": list(REFERENCE_ONLY_INPUT_EXTENSIONS),
        "public_entry_allowed": False,
    },
}

_CAPABILITY_PROFILE: dict[str, Any] = {
    "schema_version": CAPABILITY_SCHEMA_VERSION,
    "capability_id": CAPABILITY_ID,
    "capability_revision": CAPABILITY_REVISION,
    "label": "带门洞钢制风塔整体壳模型",
    "structure_family": "wind_tower_shell_with_door",
    "model_dimension": "3D_shell",
    "geometry_representation": "continuous_shell",
    "element_types": ["S4R"],
    "supported_analyses": ["static", "modal"],
    "required_engineering_parameters": [
        "geometry.tower_sections",
        "geometry.door.enabled",
        "material.elastic_modulus_mpa",
        "material.poisson_ratio",
        "material.density",
        "loads.top_horizontal_force_n",
        "loads.top_force_direction",
        "loads.wind_pressure_mpa",
    ],
    "conditional_parameters": {
        "loads.center_of_mass_offset_mm": {
            "active_when": {"loads.top_mass_tonne": {"gt": 0}},
            "unit": "mm",
            "minimum": 0,
            "missing_value_proposal": 0.0,
            "requires_confirmation": True,
        }
    },
    "fixed_rules": {
        "mesh_strategy": MESH_STRATEGY,
        "mesh_strategy_family": MESH_STRATEGY_FAMILY,
        "section_type": "Shell Section",
        "assembly": "continuous_whole_tower",
        "top_load_transfer": "distributing_coupling",
        "base_boundary": "BASE_FIXED",
        "compiler": "deterministic_windtower_adapter_v1",
    },
    "editable_fem_parameters": [
        "mesh.global_size_mm",
        "mesh.door_size_mm",
        "mesh.patch_margin_u_mm",
        "mesh.patch_margin_z_mm",
        "mesh.hole_layers",
        "analysis.static_enabled",
        "analysis.modal_enabled",
        "analysis.modal_modes",
        "boundary.name",
    ],
    "default_fem_parameters": CANONICAL_DEFAULTS,
    "input_format_registry": INPUT_FORMAT_REGISTRY,
    "output_requests": ["U", "UR", "RF", "RM", "S", "E", "NFORC", "FREQUENCY"],
    "unsupported_features": [
        "solid_or_beam_primary_model", "bolted_flange_detail", "contact", "pretension",
        "user_material", "material_nonlinearity", "weld_solid_detail", "buckling", "fatigue",
    ],
    "scope_note": "仅适用于带门洞钢制风电塔筒的整体连续 S4R 壳模型；支持静力和模态分析。",
}

_L2_CAPABILITY_PROFILE: dict[str, Any] = {
    "schema_version": CAPABILITY_SCHEMA_VERSION,
    "capability_id": L2_CAPABILITY_ID,
    "capability_revision": 1,
    "label": "局部塔节单法兰螺栓接触预紧模型",
    "structure_family": "wind_tower_local_bolted_flange",
    "complexity_level": "L2",
    "model_dimension": "3D_shell_beam",
    "geometry_representation": "two_shell_segments_single_flange",
    "element_types": ["S4R", "B31"],
    "supported_analyses": ["nonlinear_static"],
    "required_engineering_parameters": [
        "geometry.lower_segment_height_mm", "geometry.upper_segment_height_mm",
        "geometry.tower_diameter_mm", "geometry.tower_thickness_mm",
        "geometry.flange.outer_diameter_mm", "geometry.flange.inner_diameter_mm",
        "geometry.flange.thickness_mm", "geometry.bolt.count",
        "geometry.bolt.circle_diameter_mm", "geometry.bolt.diameter_mm",
        "interaction.pretension.force_per_bolt_n",
        "loads.service_control_mode",
    ],
    "conditional_parameters": {},
    "fixed_rules": {
        "scope": "local_two_segment_single_flange",
        "shell_element": "S4R",
        "bolt_element": "B31",
        "contact": "surface_to_surface_hard_frictional",
        "step_order": ["preload", "hold", "service"],
        "compiler": "deterministic_l2_flange_compiler_v1",
        "llm_may_generate_solver_syntax": False,
    },
    "editable_fem_parameters": sorted(L2_CANONICAL_DEFAULTS),
    "default_fem_parameters": L2_CANONICAL_DEFAULTS,
    "input_format_registry": INPUT_FORMAT_REGISTRY,
    "output_requests": ["U", "RF", "S", "E", "NFORC", "CPRESS", "COPEN", "CFORCE"],
    "unsupported_features": [
        "multiple_detailed_flanges", "solid_bolts", "thread_geometry",
        "shell_to_solid_transition", "material_nonlinearity", "fatigue",
    ],
    "scope_note": "仅适用于局部上下塔节的单法兰、S4R 壳体、B31 梁螺栓、接触及三步预紧静力分析。",
}


def _sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


CAPABILITY_HASH_SHA256 = _sha256(_CAPABILITY_PROFILE)
L2_CAPABILITY_HASH_SHA256 = _sha256(_L2_CAPABILITY_PROFILE)

_CAPABILITY_REGISTRY = {
    CAPABILITY_ID: _CAPABILITY_PROFILE,
    L2_CAPABILITY_ID: _L2_CAPABILITY_PROFILE,
}


def current_capability() -> dict[str, Any]:
    return get_capability(CAPABILITY_ID)


def get_capability(capability_id: str) -> dict[str, Any]:
    try:
        profile = deepcopy(_CAPABILITY_REGISTRY[capability_id])
    except KeyError as exc:
        raise ValueError(f"未知建模能力：{capability_id}") from exc
    profile["capability_hash_sha256"] = _sha256(_CAPABILITY_REGISTRY[capability_id])
    return profile


def list_capabilities() -> list[dict[str, Any]]:
    return [get_capability(capability_id) for capability_id in _CAPABILITY_REGISTRY]


def capability_exists(capability_id: str) -> bool:
    return capability_id in _CAPABILITY_REGISTRY


def default_fem_parameters(capability_id: str = CAPABILITY_ID) -> dict[str, Any]:
    return deepcopy(get_capability(capability_id)["default_fem_parameters"])


def input_format_registry() -> dict[str, Any]:
    return deepcopy(INPUT_FORMAT_REGISTRY)
