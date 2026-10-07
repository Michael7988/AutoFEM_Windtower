from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .capability_profile import (
    CAPABILITY_HASH_SHA256,
    CAPABILITY_ID,
    CAPABILITY_REVISION,
    CANONICAL_DEFAULTS,
    L2_CANONICAL_DEFAULTS,
    L2_CAPABILITY_HASH_SHA256,
    L2_CAPABILITY_ID,
    get_capability,
)


PARAMETER_SCHEMA_VERSION: Literal["windtower_parameter_set_v2"] = (
    "windtower_parameter_set_v2"
)
STRUCTURE_FAMILY: Literal["wind_tower_shell_with_door"] = (
    "wind_tower_shell_with_door"
)
ParameterDomain = Literal["engineering", "fem"]
ParameterOrigin = Literal[
    "engineering_evidence",
    "fem_evidence",
    "reference_model",
    "model_library",
    "capability_default",
    "llm_suggestion",
    "engineer_input",
    "deterministic_derived",
    "legacy_confirmed",
]


def _domain_for_group(group: str) -> ParameterDomain:
    if group in {"mesh", "analysis", "boundary", "interaction"}:
        return "fem"
    return "engineering"


def _default_analysis_tasks() -> list[Literal["static", "modal"]]:
    return ["static", "modal"]


class SourceReference(BaseModel):
    filename: str
    locator: str | None = None
    extraction_method: str = "deterministic"
    evidence_ids: list[str] = Field(default_factory=list)
    raw_value: Any = None
    raw_unit: str | None = None
    conversion: str | None = None
    mapping_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Backward-compatible alias interpreted only as mapping confidence.",
    )
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    source_category: str | None = None
    locator_status: str = "unverified"
    consistency_status: str = "not_checked"
    validity_status: str = "not_checked"
    human_confirmation: str = "unconfirmed"
    downstream_validation: str = "not_run"
    formal_use_allowed: bool = False
    source_type: Literal[
        "engineering_file", "fem_file", "reference_inp", "model_library", "capability", "llm", "engineer", "legacy"
    ] = "engineering_file"


class ParameterRecord(BaseModel):
    key: str
    label: str
    group: Literal["geometry", "material", "mesh", "loads", "analysis", "boundary", "interaction"]
    value: Any
    unit: str | None = None
    status: Literal["candidate", "confirmed", "derived", "defaulted"] = "confirmed"
    sources: list[SourceReference] = Field(default_factory=list)
    note: str | None = None
    domain: ParameterDomain = "engineering"
    origin: ParameterOrigin = "engineering_evidence"
    confirmation_status: Literal["pending", "confirmed", "not_required"] = "confirmed"
    requires_confirmation: bool = False

    @model_validator(mode="before")
    @classmethod
    def migrate_record(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        group = str(migrated.get("group") or "geometry")
        migrated.setdefault("domain", _domain_for_group(group))
        status = str(migrated.get("status") or "confirmed")
        migrated.setdefault("origin", "legacy_confirmed")
        migrated.setdefault(
            "confirmation_status", "pending" if status == "candidate" else "confirmed"
        )
        migrated.setdefault("requires_confirmation", status == "candidate")
        return migrated


class ParameterSet(BaseModel):
    schema_version: Literal["windtower_parameter_set_v2"] = PARAMETER_SCHEMA_VERSION
    project_id: str
    structure_family: str = STRUCTURE_FAMILY
    capability_id: str = CAPABILITY_ID
    capability_revision: int = CAPABILITY_REVISION
    capability_hash_sha256: str = CAPABILITY_HASH_SHA256
    reference_model_id: str | None = None
    reference_model_compatibility: Literal[
        "not_provided", "compatible", "partial", "incompatible"
    ] = "not_provided"
    revision: int = Field(default=1, ge=1)
    parameters: dict[str, ParameterRecord]
    analysis_tasks: list[Literal["static", "modal"]] = Field(
        default_factory=_default_analysis_tasks
    )
    output_requests: list[str] = Field(
        default_factory=lambda: ["U", "UR", "RF", "RM", "S", "E", "NFORC", "FREQUENCY"]
    )
    confirmed_at: str | None = None
    confirmed_by: str | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_v1(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = deepcopy(value)
        if migrated.get("schema_version") == "windtower_parameter_set_v1":
            migrated["schema_version"] = PARAMETER_SCHEMA_VERSION
        migrated.setdefault("capability_id", CAPABILITY_ID)
        migrated.setdefault("capability_revision", CAPABILITY_REVISION)
        migrated.setdefault("capability_hash_sha256", CAPABILITY_HASH_SHA256)
        migrated.setdefault("reference_model_id", None)
        migrated.setdefault("reference_model_compatibility", "not_provided")
        parameters = migrated.get("parameters")
        if isinstance(parameters, dict):
            for record in parameters.values():
                if not isinstance(record, dict):
                    continue
                group = str(record.get("group") or "geometry")
                record.setdefault("domain", _domain_for_group(group))
                record.setdefault("origin", "legacy_confirmed")
                status = str(record.get("status") or "confirmed")
                record.setdefault(
                    "confirmation_status",
                    "pending" if status == "candidate" else "confirmed",
                )
                record.setdefault("requires_confirmation", status == "candidate")
        return migrated

    @model_validator(mode="after")
    def validate_capability(self) -> "ParameterSet":
        profile = get_capability(self.capability_id)
        if self.structure_family != profile["structure_family"]:
            raise ValueError("ParameterSet structure_family 与 capability 不一致。")
        return self


class ParameterChange(BaseModel):
    key: str
    value: Any
    unit: str | None = None


class ParameterChangePreview(BaseModel):
    preview_id: str
    project_id: str
    base_revision: int
    diff: list[dict[str, Any]]
    validation: dict[str, Any]
    created_at: str
    invalidated_artifacts: list[str] = Field(default_factory=lambda: [
        "mesh", "inp", "datacheck", "solver_jobs", "results"
    ])


class ParameterDefinition(BaseModel):
    key: str
    label: str
    group: Literal["geometry", "material", "mesh", "loads", "analysis", "boundary", "interaction"]
    unit: str | None = None
    value_type: Literal["number", "integer", "boolean", "string"] = "number"
    minimum: float | None = None
    maximum: float | None = None
    options: list[Any] = Field(default_factory=list)
    required: bool = True
    legacy_paths: list[str] = Field(default_factory=list)
    domain: ParameterDomain = "engineering"

    @model_validator(mode="after")
    def assign_domain(self) -> "ParameterDefinition":
        self.domain = _domain_for_group(self.group)
        return self


_BASE_DEFINITIONS = (
    ParameterDefinition(key="loads.center_of_mass_offset_mm", label="顶部质心偏心距", group="loads", unit="mm", minimum=0, required=False, legacy_paths=["/semantic_ir/loads_inventory/center_of_mass_offset_mm"]),
    ParameterDefinition(key="mesh.strategy", label="网格策略", group="mesh", value_type="string", options=["V3_CONFORMING_DOOR_MESH"], legacy_paths=["/mesh_and_set_ir/strategy"]),
    ParameterDefinition(key="geometry.door.enabled", label="启用门洞", group="geometry", value_type="boolean", legacy_paths=["/semantic_ir/door_openings/0/enabled"]),
    ParameterDefinition(key="geometry.door.width_mm", label="门洞宽度", group="geometry", unit="mm", minimum=100, required=False, legacy_paths=["/semantic_ir/door_openings/0/width_mm"]),
    ParameterDefinition(key="geometry.door.height_mm", label="门洞高度", group="geometry", unit="mm", minimum=100, required=False, legacy_paths=["/semantic_ir/door_openings/0/height_mm"]),
    ParameterDefinition(key="geometry.door.bottom_z_mm", label="门洞底标高", group="geometry", unit="mm", minimum=0, required=False, legacy_paths=["/semantic_ir/door_openings/0/bottom_z_mm"]),
    ParameterDefinition(key="geometry.door.theta_center_deg", label="门洞环向角度", group="geometry", unit="deg", minimum=-360, maximum=360, required=False, legacy_paths=["/semantic_ir/door_openings/0/theta_center_deg"]),
    ParameterDefinition(key="geometry.door.reinforcement_enabled", label="启用门洞加强圈", group="geometry", value_type="boolean", required=False, legacy_paths=["/semantic_ir/door_openings/0/reinforcement_enabled"]),
    ParameterDefinition(key="geometry.door.outer_width_mm", label="加强圈外椭圆宽度", group="geometry", unit="mm", minimum=100, required=False, legacy_paths=["/semantic_ir/door_openings/0/outer_width_mm"]),
    ParameterDefinition(key="geometry.door.outer_height_mm", label="加强圈外椭圆高度", group="geometry", unit="mm", minimum=100, required=False, legacy_paths=["/semantic_ir/door_openings/0/outer_height_mm"]),
    ParameterDefinition(key="geometry.door.frame_thickness_mm", label="加强圈替换厚度", group="geometry", unit="mm", minimum=1, required=False, legacy_paths=["/semantic_ir/door_openings/0/frame_thickness_mm"]),
    ParameterDefinition(key="geometry.door.reinforcement_material", label="加强圈材料", group="geometry", value_type="string", required=False, legacy_paths=["/semantic_ir/door_openings/0/reinforcement_material"]),
    ParameterDefinition(key="material.name", label="材料名称", group="material", value_type="string", legacy_paths=["/semantic_ir/materials/0/name"]),
    ParameterDefinition(key="material.elastic_modulus_mpa", label="弹性模量", group="material", unit="MPa", minimum=1, maximum=10_000_000, legacy_paths=["/semantic_ir/materials/0/elastic_modulus_mpa"]),
    ParameterDefinition(key="material.poisson_ratio", label="泊松比", group="material", minimum=0, maximum=0.499, legacy_paths=["/semantic_ir/materials/0/poisson_ratio"]),
    ParameterDefinition(key="material.density", label="材料密度", group="material", unit="tonne/mm³", minimum=1e-12, maximum=1, legacy_paths=["/semantic_ir/materials/0/density"]),
    ParameterDefinition(key="material.yield_strength_mpa", label="屈服强度", group="material", unit="MPa", minimum=1, maximum=100_000, required=False, legacy_paths=["/semantic_ir/materials/0/yield_strength_mpa"]),
    ParameterDefinition(key="mesh.element_type", label="壳单元类型", group="mesh", value_type="string", options=["S4R"], legacy_paths=["/abaqus_assembly_ir/elements/0/type"]),
    ParameterDefinition(key="mesh.global_size_mm", label="全局网格尺寸", group="mesh", unit="mm", minimum=1, maximum=100_000, legacy_paths=["/mesh_and_set_ir/global_size_mm"]),
    ParameterDefinition(key="mesh.door_size_mm", label="门洞局部网格尺寸", group="mesh", unit="mm", minimum=1, maximum=100_000, legacy_paths=["/mesh_and_set_ir/door_size_mm"]),
    ParameterDefinition(key="mesh.patch_margin_u_mm", label="门洞环向细化边界", group="mesh", unit="mm", minimum=1, required=False, legacy_paths=["/mesh_and_set_ir/patch_margin_u_mm"]),
    ParameterDefinition(key="mesh.patch_margin_z_mm", label="门洞高度向细化边界", group="mesh", unit="mm", minimum=1, required=False, legacy_paths=["/mesh_and_set_ir/patch_margin_z_mm"]),
    ParameterDefinition(key="mesh.hole_layers", label="门洞环向层数", group="mesh", value_type="integer", minimum=4, maximum=1000, required=False, legacy_paths=["/mesh_and_set_ir/hole_layers"]),
    ParameterDefinition(key="loads.top_mass_tonne", label="顶部等效质量", group="loads", unit="tonne", minimum=0, required=False, legacy_paths=["/semantic_ir/loads_inventory/top_mass_tonne"]),
    ParameterDefinition(key="loads.top_horizontal_force_n", label="顶部水平力", group="loads", unit="N", minimum=0, legacy_paths=["/semantic_ir/loads_inventory/top_horizontal_force_n", "/semantic_ir/loads_inventory/top_total_force_n"]),
    ParameterDefinition(key="loads.top_force_direction", label="顶部力方向", group="loads", value_type="string", options=["+X", "-X", "+Y", "-Y", "+Z", "-Z"], legacy_paths=["/semantic_ir/loads_inventory/top_force_direction"]),
    ParameterDefinition(key="loads.wind_direction_vector", label="迎风侧单位矢量", group="loads", value_type="string", required=False, legacy_paths=["/semantic_ir/loads_inventory/wind_direction_vector"]),
    ParameterDefinition(key="loads.top_force_parallel_n", label="风向局部纵向力", group="loads", unit="N", minimum=0, required=False, legacy_paths=["/semantic_ir/loads_inventory/top_force_parallel_n"]),
    ParameterDefinition(key="loads.top_force_transverse_n", label="风向局部横向力", group="loads", unit="N", required=False, legacy_paths=["/semantic_ir/loads_inventory/top_force_transverse_n"]),
    ParameterDefinition(key="loads.top_force_vertical_n", label="塔顶竖向力", group="loads", unit="N", required=False, legacy_paths=["/semantic_ir/loads_inventory/top_force_vertical_n"]),
    ParameterDefinition(key="loads.top_moment_roll_nmm", label="风向局部滚转矩", group="loads", unit="N·mm", required=False, legacy_paths=["/semantic_ir/loads_inventory/top_moment_roll_nmm"]),
    ParameterDefinition(key="loads.top_moment_pitch_nmm", label="风向局部外部俯仰矩", group="loads", unit="N·mm", required=False, legacy_paths=["/semantic_ir/loads_inventory/top_moment_pitch_nmm"]),
    ParameterDefinition(key="loads.top_moment_yaw_nmm", label="偏航矩", group="loads", unit="N·mm", required=False, legacy_paths=["/semantic_ir/loads_inventory/top_moment_yaw_nmm"]),
    ParameterDefinition(key="loads.wind_pressure_mpa", label="风压", group="loads", unit="MPa", minimum=0, legacy_paths=["/semantic_ir/loads_inventory/wind_pressure_mpa"]),
    # The active T02 wind load is height-piecewise.  Expose the third band as a
    # scalar so a controlled patch can affect one physical load region without
    # accepting an unchecked arbitrary JSON profile.
    ParameterDefinition(key="loads.wind_pressure_band_03_mpa", label="第3段风压", group="loads", unit="MPa", required=False, legacy_paths=["/semantic_ir/loads_inventory/wind_pressure_profile/2/pressure_mpa", "/analysis_cases/static_strength/wind_pressure_profile/2/pressure_mpa"]),
    ParameterDefinition(key="analysis.static_enabled", label="静力分析", group="analysis", value_type="boolean", legacy_paths=[]),
    ParameterDefinition(key="analysis.modal_enabled", label="模态分析", group="analysis", value_type="boolean", legacy_paths=[]),
    ParameterDefinition(key="analysis.modal_modes", label="模态阶数", group="analysis", value_type="integer", minimum=1, maximum=1000, required=False, legacy_paths=["/analysis_cases/modal/modes"]),
    ParameterDefinition(key="analysis.static_nlgeom", label="静力几何非线性", group="analysis", value_type="boolean", required=False, legacy_paths=["/analysis_cases/static_strength/nlgeom"]),
    ParameterDefinition(key="analysis.modal_prestress", label="预应力模态", group="analysis", value_type="boolean", required=False, legacy_paths=["/analysis_cases/modal/prestress"]),
    ParameterDefinition(key="boundary.name", label="底部边界条件", group="boundary", value_type="string", options=["BASE_FIXED"], legacy_paths=["/abaqus_assembly_ir/boundary_conditions/0/name"]),
)

_L2_DEFINITION_SPECS: tuple[tuple[str, str, str, str | None, str, float | None, float | None, list[Any]], ...] = (
    ("geometry.lower_segment_height_mm", "下塔节高度", "geometry", "mm", "number", 100.0, None, []),
    ("geometry.upper_segment_height_mm", "上塔节高度", "geometry", "mm", "number", 100.0, None, []),
    ("geometry.tower_diameter_mm", "塔节直径", "geometry", "mm", "number", 500.0, None, []),
    ("geometry.tower_thickness_mm", "塔壁厚度", "geometry", "mm", "number", 1.0, None, []),
    ("geometry.flange.outer_diameter_mm", "法兰外径", "geometry", "mm", "number", 500.0, None, []),
    ("geometry.flange.inner_diameter_mm", "法兰内径", "geometry", "mm", "number", 100.0, None, []),
    ("geometry.flange.thickness_mm", "法兰厚度", "geometry", "mm", "number", 1.0, None, []),
    ("geometry.flange.contact_inner_radius_mm", "接触面内半径", "geometry", "mm", "number", 1.0, None, []),
    ("geometry.flange.contact_outer_radius_mm", "接触面外半径", "geometry", "mm", "number", 1.0, None, []),
    ("geometry.bolt.count", "螺栓数量", "geometry", None, "integer", 4.0, 512.0, []),
    ("geometry.bolt.circle_diameter_mm", "螺栓圆直径", "geometry", "mm", "number", 1.0, None, []),
    ("geometry.bolt.diameter_mm", "螺栓直径", "geometry", "mm", "number", 1.0, None, []),
    ("geometry.bolt.effective_area_mm2", "螺栓有效面积", "geometry", "mm²", "number", 1.0, None, []),
    ("material.elastic_modulus_mpa", "钢材弹性模量", "material", "MPa", "number", 1.0, None, []),
    ("material.poisson_ratio", "钢材泊松比", "material", None, "number", 0.0, 0.499, []),
    ("material.density", "钢材密度", "material", "tonne/mm³", "number", 1e-12, 1.0, []),
    ("material.bolt_elastic_modulus_mpa", "螺栓弹性模量", "material", "MPa", "number", 1.0, None, []),
    ("material.bolt_poisson_ratio", "螺栓泊松比", "material", None, "number", 0.0, 0.499, []),
    ("interaction.contact.normal_behavior", "法向接触", "interaction", None, "string", None, None, ["hard"]),
    ("interaction.contact.allow_separation", "允许接触分离", "interaction", None, "boolean", None, None, []),
    ("interaction.contact.friction_coefficient", "摩擦系数", "interaction", None, "number", 0.0, 1.0, []),
    ("interaction.pretension.formulation", "预紧表示", "interaction", None, "string", None, None, ["native_pretension", "equivalent_end_force"]),
    ("interaction.pretension.force_per_bolt_n", "单栓预紧力", "interaction", "N", "number", 0.0, None, []),
    ("interaction.pretension.initial_increment", "预紧初始增量", "interaction", None, "number", 1e-6, 1.0, []),
    ("mesh.element_type", "壳单元类型", "mesh", None, "string", None, None, ["S4R"]),
    ("mesh.bolt_element_type", "螺栓单元类型", "mesh", None, "string", None, None, ["B31"]),
    ("mesh.global_size_mm", "塔节网格尺寸", "mesh", "mm", "number", 1.0, None, []),
    ("mesh.flange_size_mm", "法兰网格尺寸", "mesh", "mm", "number", 1.0, None, []),
    ("mesh.bolt_pad_size_mm", "螺栓垫片区尺寸", "mesh", "mm", "number", 1.0, None, []),
    ("loads.section_force_n", "轴力 N", "loads", "N", "number", None, None, []),
    ("loads.shear_x_n", "剪力 Vx", "loads", "N", "number", None, None, []),
    ("loads.shear_y_n", "剪力 Vy", "loads", "N", "number", None, None, []),
    ("loads.torsion_nmm", "扭矩 T", "loads", "N·mm", "number", None, None, []),
    ("loads.moment_x_nmm", "弯矩 Mx", "loads", "N·mm", "number", None, None, []),
    ("loads.moment_y_nmm", "弯矩 My", "loads", "N·mm", "number", None, None, []),
    ("loads.service_control_mode", "服役步控制方式", "loads", None, "string", None, None, ["section_loads", "section_displacement"]),
    ("boundary.service.u1_mm", "服役步位移 U1", "boundary", "mm", "number", None, None, []),
    ("boundary.service.u2_mm", "服役步位移 U2", "boundary", "mm", "number", None, None, []),
    ("boundary.service.u3_mm", "服役步位移 U3", "boundary", "mm", "number", None, None, []),
    ("boundary.service.ur1_rad", "服役步转角 UR1", "boundary", "rad", "number", None, None, []),
    ("boundary.service.ur2_rad", "服役步转角 UR2", "boundary", "rad", "number", None, None, []),
    ("boundary.service.ur3_rad", "服役步转角 UR3", "boundary", "rad", "number", None, None, []),
    ("analysis.preload_step_name", "预紧步", "analysis", None, "string", None, None, ["STEP_1_BOLT_PRELOAD"]),
    ("analysis.hold_step_name", "锁定步", "analysis", None, "string", None, None, ["STEP_2_PRELOAD_HOLD"]),
    ("analysis.service_step_name", "服役荷载步", "analysis", None, "string", None, None, ["STEP_3_SERVICE_LOAD"]),
    ("analysis.nlgeom", "几何非线性", "analysis", None, "boolean", None, None, [True]),
)

_L2_DEFINITION_SPECS = _L2_DEFINITION_SPECS + (
    ("geometry.bolt.hole_diameter_mm", "螺栓孔直径", "geometry", "mm", "number", 1.0, None, []),
    ("mesh.circumferential_divisions", "塔壁环向网格份数", "mesh", None, "integer", 16.0, 8192.0, []),
    ("mesh.flange_hole_segments", "单个螺栓孔边数", "mesh", None, "integer", 8.0, 128.0, []),
    ("mesh.flange_radial_layers", "螺栓孔径向网格层数", "mesh", None, "integer", 2.0, 32.0, []),
)

_L2_DEFINITIONS = tuple(
    ParameterDefinition(
        key=key, label=label, group=group, unit=unit, value_type=value_type,
        minimum=minimum, maximum=maximum, options=options,
    )
    for key, label, group, unit, value_type, minimum, maximum, options in _L2_DEFINITION_SPECS
)

_SECTION_PATTERN = re.compile(
    r"^geometry\.tower_sections\.(\d+)\."
    r"(z_bottom_mm|z_top_mm|diameter_bottom_mm|diameter_top_mm|thickness_mm)$"
)


def parameter_definitions(ir: dict[str, Any] | None = None) -> dict[str, ParameterDefinition]:
    capability_id = str(((ir or {}).get("modeling_capability") or {}).get("capability_id") or CAPABILITY_ID)
    if capability_id == L2_CAPABILITY_ID:
        return {item.key: item for item in _L2_DEFINITIONS}
    definitions = {item.key: item for item in _BASE_DEFINITIONS}
    sections = ((ir or {}).get("semantic_ir") or {}).get("tower_sections") or []
    labels = {
        "z_bottom_mm": "底标高",
        "z_top_mm": "顶标高",
        "diameter_bottom_mm": "底直径",
        "diameter_top_mm": "顶直径",
        "thickness_mm": "厚度",
    }
    for index, _section in enumerate(sections):
        for field_name, label in labels.items():
            key = f"geometry.tower_sections.{index}.{field_name}"
            definitions[key] = ParameterDefinition(
                key=key,
                label=f"第{index + 1}段{label}",
                group="geometry",
                unit="mm",
                minimum=0 if field_name.startswith("z_") else 0.001,
                legacy_paths=[f"/semantic_ir/tower_sections/{index}/{field_name}"],
            )
    return definitions


def parameter_set_from_ir(
    project_id: str,
    ir: dict[str, Any],
    *,
    revision: int = 1,
) -> ParameterSet:
    capability_id = str((ir.get("modeling_capability") or {}).get("capability_id") or CAPABILITY_ID)
    if capability_id == L2_CAPABILITY_ID:
        return l2_parameter_set(project_id, ir.get("l2_parameters"), revision=revision)
    records: dict[str, ParameterRecord] = {}
    evidence_map = ir.get("parameter_evidence_map") or {}
    for key, definition in parameter_definitions(ir).items():
        value = _special_value(key, ir)
        if value is None:
            for path in definition.legacy_paths:
                value = _get_pointer(ir, path)
                if value is not None:
                    break
        used_capability_default = value is None and key in CANONICAL_DEFAULTS
        if used_capability_default:
            value = deepcopy(CANONICAL_DEFAULTS[key])
        mapped_value = evidence_map.get(key) if isinstance(evidence_map, dict) else None
        mapped = mapped_value if isinstance(mapped_value, dict) else None
        if mapped is None or not ({"confirmation_status", "requires_confirmation"} & set(mapped)):
            confirmed = value is not None
            requires_confirmation = value is None
        else:
            requires_confirmation = bool(mapped.get("requires_confirmation")) or mapped.get("confirmation_status") == "pending"
            confirmed = value is not None and mapped.get("confirmation_status") == "confirmed" and not requires_confirmation
        status: Literal["candidate", "confirmed"] = "confirmed" if confirmed else "candidate"
        records[key] = ParameterRecord(
            key=key,
            label=definition.label,
            group=definition.group,
            value=value,
            unit=definition.unit,
            status=status,
            sources=_sources_for(key, definition, ir),
            domain=definition.domain,
            origin=(
                "capability_default"
                if used_capability_default
                else "engineering_evidence"
                if definition.domain == "engineering"
                else "deterministic_derived"
            ),
            confirmation_status="confirmed" if confirmed else "pending",
            requires_confirmation=requires_confirmation,
        )
    top_mass_record = records.get("loads.top_mass_tonne")
    top_mass = float(top_mass_record.value or 0) if top_mass_record else 0.0
    offset = records["loads.center_of_mass_offset_mm"]
    if top_mass > 0 and offset.value is None:
        offset.value = 0.0
        offset.status = "candidate"
        offset.origin = "capability_default"
        offset.confirmation_status = "pending"
        offset.requires_confirmation = True
        offset.note = "顶部质量已启用；0 mm 是建议值，必须由工程师确认。"
        offset.sources = [SourceReference(
            filename="WT-SHELL-DOOR-STATIC-MODAL-V1",
            locator="loads.center_of_mass_offset_mm",
            extraction_method="capability_default",
            raw_value=0.0,
            raw_unit="mm",
            source_type="capability",
            source_category="capability_default",
            locator_status="exact",
            human_confirmation="unconfirmed",
            formal_use_allowed=False,
        )]
    elif top_mass <= 0 and offset.value is None:
        offset.status = "defaulted"
        offset.origin = "capability_default"
        offset.confirmation_status = "not_required"
        offset.requires_confirmation = False
        offset.note = "顶部质量未启用，该参数当前不参与门禁。"
    tasks: list[Literal["static", "modal"]] = []
    if bool(records["analysis.static_enabled"].value):
        tasks.append("static")
    if bool(records["analysis.modal_enabled"].value):
        tasks.append("modal")
    return ParameterSet(
        project_id=project_id,
        revision=revision,
        parameters=records,
        analysis_tasks=tasks or ["static", "modal"],
    )


def l2_parameter_set(
    project_id: str,
    values: dict[str, Any] | None = None,
    *,
    revision: int = 1,
    confirmed: bool = False,
) -> ParameterSet:
    supplied = values or {}
    records: dict[str, ParameterRecord] = {}
    for definition in _L2_DEFINITIONS:
        value = deepcopy(supplied.get(definition.key, L2_CANONICAL_DEFAULTS[definition.key]))
        records[definition.key] = ParameterRecord(
            key=definition.key,
            label=definition.label,
            group=definition.group,
            value=value,
            unit=definition.unit,
            status="confirmed" if confirmed else "candidate",
            domain=definition.domain,
            origin="engineer_input" if definition.key in supplied else "capability_default",
            confirmation_status="confirmed" if confirmed else "pending",
            requires_confirmation=not confirmed,
            sources=[SourceReference(
                filename=L2_CAPABILITY_ID,
                locator=definition.key,
                extraction_method="capability_default",
                source_type="capability",
            )],
        )
    return ParameterSet(
        project_id=project_id,
        structure_family="wind_tower_local_bolted_flange",
        capability_id=L2_CAPABILITY_ID,
        capability_revision=1,
        capability_hash_sha256=L2_CAPABILITY_HASH_SHA256,
        revision=revision,
        parameters=records,
        analysis_tasks=["static"],
        output_requests=["U", "RF", "S", "E", "NFORC", "CPRESS", "COPEN", "CFORCE"],
        confirmed_at=datetime.now(timezone.utc).isoformat() if confirmed else None,
        confirmed_by="engineer" if confirmed else None,
    )


def validate_parameter_set(parameter_set: ParameterSet) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    definitions = parameter_definitions_from_set(parameter_set)
    for key in sorted(set(parameter_set.parameters) - set(definitions)):
        issues.append(_issue(key, "unknown_parameter", "参数未在当前能力配置中注册。"))
    for key, definition in definitions.items():
        record = parameter_set.parameters.get(key)
        if not is_parameter_active(key, parameter_set):
            continue
        if record is None or record.value is None:
            if definition.required:
                issues.append(_issue(key, "missing", "缺少必需参数。"))
            continue
        try:
            value = _normalize_value(record.value, definition)
        except ValueError as exc:
            issues.append(_issue(key, "invalid", str(exc)))
            continue
        if definition.minimum is not None and isinstance(value, (int, float)) and value < definition.minimum:
            issues.append(_issue(key, "range", f"参数不得小于 {definition.minimum:g}。"))
        if definition.maximum is not None and isinstance(value, (int, float)) and value > definition.maximum:
            issues.append(_issue(key, "range", f"参数不得大于 {definition.maximum:g}。"))
        if definition.options and value not in definition.options:
            issues.append(_issue(key, "unsupported", f"仅支持：{', '.join(map(str, definition.options))}。"))
        if record.status == "candidate" or record.requires_confirmation or record.confirmation_status == "pending":
            issues.append(_issue(key, "unconfirmed", "参数尚未由工程师确认。"))
            continue
        if record.status == "candidate":
            issues.append(_issue(key, "unconfirmed", "参数候选尚未确认。"))
        if record.requires_confirmation or record.confirmation_status == "pending":
            issues.append(_issue(key, "unconfirmed", "参数尚未由工程师确认。"))

    if parameter_set.capability_id == L2_CAPABILITY_ID:
        issues.extend(_validate_l2_relationships(parameter_set))
        return {
            "schema_version": "parameter_validation_v1",
            "layer": "parameter",
            "status": "blocked" if issues else "pass",
            "issues": issues,
        }

    sections = _sections(parameter_set)
    if not sections:
        issues.append(_issue("geometry.tower_sections", "missing", "至少需要一个塔筒分段。"))
    for index, section in enumerate(sections):
        if section["z_top_mm"] <= section["z_bottom_mm"]:
            issues.append(_issue(f"geometry.tower_sections.{index}", "geometry", "分段顶标高必须大于底标高。"))
        if index and not math.isclose(section["z_bottom_mm"], sections[index - 1]["z_top_mm"], abs_tol=1e-6):
            issues.append(_issue(f"geometry.tower_sections.{index}.z_bottom_mm", "geometry", "相邻分段标高必须连续。"))

    door_enabled = bool(_value(parameter_set, "geometry.door.enabled", False))
    if door_enabled and sections:
        for key in (
            "geometry.door.width_mm",
            "geometry.door.height_mm",
            "geometry.door.bottom_z_mm",
        ):
            if _value(parameter_set, key) is None:
                issues.append(_issue(key, "missing", "启用门洞时该参数为必需项。"))
        door_top = float(_value(parameter_set, "geometry.door.bottom_z_mm", 0)) + float(
            _value(parameter_set, "geometry.door.height_mm", 0)
        )
        if door_top > sections[-1]["z_top_mm"]:
            issues.append(_issue("geometry.door", "geometry", "门洞顶部不得超过塔筒总高。"))
        if float(_value(parameter_set, "mesh.door_size_mm", 0)) > float(
            _value(parameter_set, "mesh.global_size_mm", 0)
        ):
            issues.append(_issue("mesh.door_size_mm", "mesh", "门洞局部网格尺寸不得大于全局网格尺寸。"))

    if not parameter_set.analysis_tasks:
        issues.append(_issue("analysis_tasks", "analysis", "至少启用静力或模态分析之一。"))
    return {
        "schema_version": "parameter_validation_v1",
        "layer": "parameter",
        "status": "blocked" if issues else "pass",
        "issues": issues,
    }


def apply_changes(
    parameter_set: ParameterSet,
    changes: list[ParameterChange],
    *,
    increment_revision: bool,
    operator: str | None = None,
) -> tuple[ParameterSet, list[dict[str, Any]], dict[str, Any]]:
    next_set = parameter_set.model_copy(deep=True)
    definitions = parameter_definitions_from_set(next_set)
    diff: list[dict[str, Any]] = []
    seen: set[str] = set()
    for change in changes:
        if change.key in seen:
            raise ValueError(f"同一请求中参数重复：{change.key}")
        seen.add(change.key)
        definition = definitions.get(change.key)
        if definition is None:
            raise ValueError(f"不允许修改的参数：{change.key}")
        value = _normalize_change_value(change, definition)
        record = next_set.parameters[change.key]
        old_value = record.value
        record.value = value
        record.status = "confirmed"
        record.note = "parameter_change"
        record.origin = "engineer_input"
        record.confirmation_status = "confirmed"
        record.requires_confirmation = False
        diff.append(
            {
                "key": change.key,
                "label": definition.label,
                "unit": definition.unit,
                "old_value": old_value,
                "new_value": value,
                "domain": record.domain,
                "origin": record.origin,
                "affected_artifacts": [
                    "mesh", "inp", "datacheck", "solver_jobs", "results"
                ],
            }
        )
    if next_set.capability_id == L2_CAPABILITY_ID:
        next_set.analysis_tasks = ["static"]
    else:
        task_parameters: tuple[
            tuple[Literal["static"], str],
            tuple[Literal["modal"], str],
        ] = (
            ("static", "analysis.static_enabled"),
            ("modal", "analysis.modal_enabled"),
        )
        next_set.analysis_tasks = [
            task
            for task, key in task_parameters
            if bool(_value(next_set, key, False))
        ]
    validation = validate_parameter_set(next_set)
    if increment_revision and validation["status"] == "pass":
        next_set.revision += 1
        next_set.confirmed_at = datetime.now(timezone.utc).isoformat()
        next_set.confirmed_by = operator or "engineer"
    return next_set, diff, validation


def _normalize_change_value(
    change: ParameterChange,
    definition: ParameterDefinition,
) -> Any:
    value = change.value
    if not change.unit:
        return _normalize_value(value, definition)
    if not definition.unit:
        raise ValueError(f"参数 {change.key} 不接受单位。")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"参数 {change.key} 的带单位值必须为数值。")
    source_unit = _canonical_unit(change.unit)
    target_unit = _canonical_unit(definition.unit)
    conversions = {
        ("m", "mm"): 1_000.0,
        ("cm", "mm"): 10.0,
        ("mm", "mm"): 1.0,
        ("kn", "n"): 1_000.0,
        ("n", "n"): 1.0,
        ("pa", "mpa"): 1e-6,
        ("kpa", "mpa"): 1e-3,
        ("mpa", "mpa"): 1.0,
        ("gpa", "mpa"): 1_000.0,
        ("kg", "tonne"): 1e-3,
        ("t", "tonne"): 1.0,
        ("tonne", "tonne"): 1.0,
        ("deg", "deg"): 1.0,
    }
    factor = conversions.get((source_unit, target_unit))
    if factor is None:
        raise ValueError(
            f"参数 {change.key} 的单位 {change.unit} 与正式单位 {definition.unit} 不兼容。"
        )
    return _normalize_value(float(value) * factor, definition)


def _canonical_unit(unit: str) -> str:
    normalized = unit.strip().lower().replace(" ", "")
    aliases = {
        "毫米": "mm",
        "厘米": "cm",
        "米": "m",
        "千牛": "kn",
        "牛": "n",
        "帕": "pa",
        "千帕": "kpa",
        "兆帕": "mpa",
        "吉帕": "gpa",
        "吨": "tonne",
    }
    return aliases.get(normalized, normalized)


def instruction_changes(instruction: str, parameter_set: ParameterSet) -> list[ParameterChange]:
    if parameter_set.capability_id == L2_CAPABILITY_ID:
        return _l2_instruction_changes(instruction)
    patterns = (
        ("geometry.door.height_mm", r"(?:门洞|door).{0,12}(?:高度|height).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("geometry.door.width_mm", r"(?:门洞|door).{0,12}(?:宽度|width).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("geometry.door.bottom_z_mm", r"(?:门洞|door).{0,12}(?:底标高|底部标高|bottom).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("mesh.global_size_mm", r"(?:全局网格|global\s+mesh).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("mesh.door_size_mm", r"(?:门洞网格|门洞局部网格|door\s+(?:local\s+)?mesh).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("material.elastic_modulus_mpa", r"(?:弹性模量|elastic\s+modulus).{0,12}?(-?\d+(?:\.\d+)?)\s*(MPa|GPa|兆帕|吉帕)?"),
        ("material.poisson_ratio", r"(?:泊松比|poisson(?:'s)?\s+ratio).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("loads.top_mass_tonne", r"(?:顶部质量|top\s+mass).{0,12}?(-?\d+(?:\.\d+)?)\s*(tonne|t|吨|kg)?"),
        ("loads.center_of_mass_offset_mm", r"(?:质心偏心距|偏心距|center\s+of\s+mass\s+offset|mass\s+offset).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("loads.top_horizontal_force_n", r"(?:顶部水平力|top\s+(?:horizontal\s+)?force).{0,12}?(-?\d+(?:\.\d+)?)\s*(N|kN|牛|千牛)?"),
        ("loads.wind_pressure_mpa", r"(?:风压|wind\s+pressure).{0,12}?(-?\d+(?:\.\d+)?)\s*(MPa|kPa|Pa|兆帕|千帕|帕)?"),
        ("analysis.modal_modes", r"(?:模态阶数|modal\s+modes?).{0,12}?(\d+)"),
    )
    changes: list[ParameterChange] = []
    for key, expression in patterns:
        match = re.search(expression, instruction, flags=re.IGNORECASE)
        if match:
            unit = match.group(2) if (match.lastindex or 0) >= 2 else None
            changes.append(ParameterChange(key=key, value=float(match.group(1)), unit=unit))

    relative = re.search(
        r"(门洞宽度|门洞高度|全局网格|门洞(?:局部)?网格|door\s+width|door\s+height|global\s+mesh|door\s+mesh)"
        r".{0,16}?(增加|增大|减小|降低|increase|decrease).{0,8}?(\d+(?:\.\d+)?)\s*%",
        instruction,
        flags=re.IGNORECASE,
    )
    if relative:
        subject, direction, percent = relative.groups()
        subject_lower = subject.lower()
        if "宽" in subject or "width" in subject_lower:
            key = "geometry.door.width_mm"
        elif "高" in subject or "height" in subject_lower:
            key = "geometry.door.height_mm"
        elif "全局" in subject or "global" in subject_lower:
            key = "mesh.global_size_mm"
        else:
            key = "mesh.door_size_mm"
        factor = 1 + float(percent) / 100
        if direction.lower() in {"减小", "降低", "decrease"}:
            factor = 1 - float(percent) / 100
        changes = [item for item in changes if item.key != key]
        changes.append(ParameterChange(key=key, value=float(_value(parameter_set, key, 0)) * factor))

    if re.search(r"(关闭门洞|disable\s+door|remove\s+door)", instruction, flags=re.IGNORECASE):
        changes.append(ParameterChange(key="geometry.door.enabled", value=False))
    if re.search(r"(启用门洞|enable\s+door)", instruction, flags=re.IGNORECASE):
        changes.append(ParameterChange(key="geometry.door.enabled", value=True))
    if re.search(r"(增加|启用|add|enable).{0,8}(模态|modal)", instruction, flags=re.IGNORECASE):
        changes.append(ParameterChange(key="analysis.modal_enabled", value=True))
    return changes


def _l2_instruction_changes(instruction: str) -> list[ParameterChange]:
    patterns = (
        ("geometry.lower_segment_height_mm", r"(?:下塔节|lower\s+segment).{0,12}?(?:高度|height).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("geometry.upper_segment_height_mm", r"(?:上塔节|upper\s+segment).{0,12}?(?:高度|height).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("geometry.tower_diameter_mm", r"(?:塔节|塔筒|tower).{0,12}?(?:直径|diameter).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("geometry.tower_thickness_mm", r"(?:塔壁|tower).{0,12}?(?:厚度|壁厚|thickness).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米)?"),
        ("geometry.flange.outer_diameter_mm", r"(?:法兰|flange).{0,12}?(?:外径|outer\s+diameter).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("geometry.flange.inner_diameter_mm", r"(?:法兰|flange).{0,12}?(?:内径|inner\s+diameter).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("geometry.flange.thickness_mm", r"(?:法兰|flange).{0,12}?(?:厚度|thickness).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米)?"),
        ("geometry.bolt.count", r"(?:螺栓数量|bolt\s+count).{0,12}?(\d+)"),
        ("geometry.bolt.circle_diameter_mm", r"(?:螺栓圆|bolt\s+circle).{0,12}?(?:直径|diameter)?.{0,8}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|m|米)?"),
        ("geometry.bolt.diameter_mm", r"(?:螺栓|bolt).{0,12}?(?:杆径|直径|diameter).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米)?"),
        ("interaction.contact.friction_coefficient", r"(?:摩擦系数|friction(?:\s+coefficient)?).{0,12}?(-?\d+(?:\.\d+)?)"),
        ("interaction.pretension.force_per_bolt_n", r"(?:单栓预紧力|预紧力|pretension(?:\s+force)?).{0,12}?(-?\d+(?:\.\d+)?)\s*(N|kN|牛|千牛)?"),
        ("mesh.global_size_mm", r"(?:塔壁网格|全局网格|global\s+mesh).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米)?"),
        ("mesh.flange_size_mm", r"(?:法兰网格|flange\s+mesh).{0,12}?(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米)?"),
    )
    changes: list[ParameterChange] = []
    for key, expression in patterns:
        match = re.search(expression, instruction, flags=re.IGNORECASE)
        if match:
            unit = match.group(2) if (match.lastindex or 0) >= 2 else None
            changes.append(ParameterChange(key=key, value=float(match.group(1)), unit=unit))
    native = bool(re.search(r"(?:原生预紧|native\s+pretension)", instruction, re.IGNORECASE))
    equivalent = bool(re.search(r"(?:等效预紧|端部集中力|equivalent\s+end\s+force)", instruction, re.IGNORECASE))
    if native != equivalent:
        changes.append(ParameterChange(
            key="interaction.pretension.formulation",
            value="native_pretension" if native else "equivalent_end_force",
        ))
    return changes


def apply_parameter_set_to_ir(parameter_set: ParameterSet, ir: dict[str, Any]) -> dict[str, Any]:
    next_ir = deepcopy(ir)
    if parameter_set.capability_id == L2_CAPABILITY_ID:
        next_ir["schema_version"] = PARAMETER_SCHEMA_VERSION
        next_ir["modeling_capability"] = get_capability(parameter_set.capability_id)
        next_ir["l2_parameters"] = {
            key: record.value for key, record in parameter_set.parameters.items()
        }
        next_ir["analysis_cases"] = {
            "static_strength": {
                "enabled": True,
                "steps": [
                    _value(parameter_set, "analysis.preload_step_name"),
                    _value(parameter_set, "analysis.hold_step_name"),
                    _value(parameter_set, "analysis.service_step_name"),
                ],
                "nlgeom": True,
            },
            "modal": {"enabled": False},
        }
        next_ir["parameter_set"] = parameter_set.model_dump(mode="json")
        return next_ir
    definitions = parameter_definitions_from_set(parameter_set)
    for key, record in parameter_set.parameters.items():
        definition = definitions.get(key)
        if definition is None:
            continue
        for path in definition.legacy_paths:
            _set_pointer(next_ir, path, record.value)
    next_ir.setdefault("analysis_cases", {}).setdefault("static_strength", {})["enabled"] = (
        "static" in parameter_set.analysis_tasks
    )
    next_ir.setdefault("analysis_cases", {}).setdefault("modal", {})["enabled"] = (
        "modal" in parameter_set.analysis_tasks
    )
    next_ir["parameter_set"] = parameter_set.model_dump(mode="json")
    next_ir["schema_version"] = PARAMETER_SCHEMA_VERSION
    return next_ir


def parameter_definitions_from_set(parameter_set: ParameterSet) -> dict[str, ParameterDefinition]:
    if parameter_set.capability_id == L2_CAPABILITY_ID:
        return {item.key: item for item in _L2_DEFINITIONS}
    definitions = {item.key: item for item in _BASE_DEFINITIONS}
    for key in parameter_set.parameters:
        match = _SECTION_PATTERN.match(key)
        if not match:
            continue
        field_name = match.group(2)
        definitions[key] = ParameterDefinition(
            key=key,
            label=parameter_set.parameters[key].label,
            group="geometry",
            unit="mm",
            minimum=0 if field_name.startswith("z_") else 0.001,
            legacy_paths=[f"/semantic_ir/tower_sections/{match.group(1)}/{field_name}"],
        )
    return definitions


def _validate_l2_relationships(parameter_set: ParameterSet) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    inner = float(_value(parameter_set, "geometry.flange.inner_diameter_mm", 0))
    outer = float(_value(parameter_set, "geometry.flange.outer_diameter_mm", 0))
    tower = float(_value(parameter_set, "geometry.tower_diameter_mm", 0))
    bolt_circle = float(_value(parameter_set, "geometry.bolt.circle_diameter_mm", 0))
    contact_inner = float(_value(parameter_set, "geometry.flange.contact_inner_radius_mm", 0))
    contact_outer = float(_value(parameter_set, "geometry.flange.contact_outer_radius_mm", 0))
    if not inner < outer <= tower:
        issues.append(_issue("geometry.flange", "geometry", "法兰内径 < 塔节直径 < 法兰外径必须成立。"))
    if not inner < bolt_circle < outer:
        issues.append(_issue("geometry.bolt.circle_diameter_mm", "geometry", "螺栓圆直径必须位于法兰内外径之间。"))
    if not inner / 2 <= contact_inner < contact_outer <= outer / 2:
        issues.append(_issue("geometry.flange.contact_outer_radius_mm", "geometry", "接触半径范围必须位于法兰环面内。"))
    if int(_value(parameter_set, "geometry.bolt.count", 0)) % 4:
        issues.append(_issue("geometry.bolt.count", "geometry", "完整环螺栓数量必须为 4 的倍数。"))
    hole = float(_value(parameter_set, "geometry.bolt.hole_diameter_mm", 0))
    bolt = float(_value(parameter_set, "geometry.bolt.diameter_mm", 0))
    if hole <= bolt:
        issues.append(_issue("geometry.bolt.hole_diameter_mm", "geometry", "螺栓孔直径必须大于螺栓杆直径。"))
    divisions = int(_value(parameter_set, "mesh.circumferential_divisions", 0))
    count = int(_value(parameter_set, "geometry.bolt.count", 0))
    if count and (divisions < count * 4 or divisions % count):
        issues.append(_issue("mesh.circumferential_divisions", "mesh", "环向网格份数必须为螺栓数的整数倍，且每个螺栓节距至少 4 份。"))
    hole_segments = int(_value(parameter_set, "mesh.flange_hole_segments", 0))
    if count and divisions % count == 0 and hole_segments != 4 * (divisions // count):
        issues.append(_issue("mesh.flange_hole_segments", "mesh", "结构化 O-grid 要求螺栓孔边数等于每个螺栓节距环向份数的 4 倍。"))
    return issues


def _special_value(key: str, ir: dict[str, Any]) -> Any:
    if key == "analysis.static_enabled":
        return bool((ir.get("analysis_cases") or {}).get("static_strength", {}).get("enabled", True))
    if key == "analysis.modal_enabled":
        return bool((ir.get("analysis_cases") or {}).get("modal", {}).get("enabled", True))
    if key == "mesh.element_type":
        return _get_pointer(ir, "/abaqus_assembly_ir/elements/0/type") or CANONICAL_DEFAULTS[key]
    if key == "mesh.strategy":
        return _get_pointer(ir, "/mesh_and_set_ir/strategy") or CANONICAL_DEFAULTS[key]
    if key == "boundary.name":
        return _get_pointer(ir, "/abaqus_assembly_ir/boundary_conditions/0/name") or CANONICAL_DEFAULTS[key]
    if key == "loads.top_force_direction":
        raw = (
            _get_pointer(ir, "/semantic_ir/loads_inventory/top_force_direction")
            or "+X"
        )
        normalized = str(raw).strip().upper()
        return {
            "1": "+X", "DOF1": "+X", "X": "+X", "+X": "+X",
            "-1": "-X", "-X": "-X",
            "2": "+Y", "DOF2": "+Y", "Y": "+Y", "+Y": "+Y",
            "-2": "-Y", "-Y": "-Y",
            "3": "+Z", "DOF3": "+Z", "Z": "+Z", "+Z": "+Z",
            "-3": "-Z", "-Z": "-Z",
        }.get(normalized, normalized)
    if key == "material.density":
        return (
            _get_pointer(ir, "/semantic_ir/materials/0/density")
            or _get_pointer(ir, "/semantic_ir/materials/0/density_tonne_mm3")
            or 7.85e-9
        )
    return None


def _sources_for(
    key: str,
    definition: ParameterDefinition,
    ir: dict[str, Any],
) -> list[SourceReference]:
    evidence_map = ir.get("parameter_evidence_map") or {}
    mapped_value = evidence_map.get(key) if isinstance(evidence_map, dict) else None
    mapped = mapped_value if isinstance(mapped_value, dict) else {}
    evidence = mapped.get("evidence") or []
    sources: list[SourceReference] = []
    for item in evidence:
        location = item.get("location") or {}
        locator = (
            location.get("cell_or_range")
            or location.get("json_path")
            or location.get("line")
        )
        sources.append(
            SourceReference(
                filename=str(item.get("filename") or "project_input"),
                locator=str(locator) if locator is not None else None,
                extraction_method=str(item.get("extraction_method") or mapped.get("extraction_method") or "deterministic"),
                evidence_ids=[str(value) for value in mapped.get("evidence_ids") or []],
                raw_value=mapped.get("raw_value"),
                raw_unit=mapped.get("raw_unit"),
                conversion=mapped.get("conversion"),
                mapping_confidence=mapped.get("mapping_confidence", mapped.get("confidence")),
                confidence=mapped.get("mapping_confidence", mapped.get("confidence")),
                evidence=[item],
                source_category=str(mapped.get("source_kind") or "engineering_file"),
                locator_status="exact" if location.get("cell_or_range") else "region",
                consistency_status=str(mapped.get("consistency_status") or "not_checked"),
                validity_status=str(mapped.get("validity_status") or "not_checked"),
                human_confirmation=str(mapped.get("confirmation_status") or "unconfirmed"),
                downstream_validation=str(mapped.get("downstream_validation") or "not_run"),
                formal_use_allowed=bool(mapped.get("formal_use_allowed", mapped.get("confirmation_status") == "confirmed")),
            )
        )
    if sources:
        return sources
    source_name = str((ir.get("project_meta_ir") or {}).get("source_file") or "project_input")
    locator = definition.legacy_paths[0] if definition.legacy_paths else key
    return [SourceReference(filename=source_name, locator=locator, locator_status="legacy_pointer")]


def _sections(parameter_set: ParameterSet) -> list[dict[str, float]]:
    rows: dict[int, dict[str, float]] = {}
    for key, record in parameter_set.parameters.items():
        match = _SECTION_PATTERN.match(key)
        if match and record.value is not None:
            rows.setdefault(int(match.group(1)), {})[match.group(2)] = float(record.value)
    required = {"z_bottom_mm", "z_top_mm", "diameter_bottom_mm", "diameter_top_mm", "thickness_mm"}
    return [rows[index] for index in sorted(rows) if required <= rows[index].keys()]


def _value(parameter_set: ParameterSet, key: str, default: Any = None) -> Any:
    record = parameter_set.parameters.get(key)
    return default if record is None or record.value is None else record.value


def _normalize_value(value: Any, definition: ParameterDefinition) -> Any:
    try:
        if definition.value_type == "boolean":
            if isinstance(value, bool):
                return value
            normalized = str(value).strip().lower()
            if normalized in {"true", "1", "yes", "on", "是", "启用"}:
                return True
            if normalized in {"false", "0", "no", "off", "否", "关闭"}:
                return False
            raise ValueError("无法识别布尔值。")
        if definition.value_type == "integer":
            number = float(value)
            if not number.is_integer():
                raise ValueError("参数必须为整数。")
            return int(number)
        if definition.value_type == "number":
            return float(value)
        return str(value).strip()
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) in {"无法识别布尔值。", "参数必须为整数。"}:
            raise
        raise ValueError("参数值类型不正确。") from exc


def _get_pointer(data: Any, pointer: str) -> Any:
    current = data
    for token in pointer.strip("/").split("/"):
        if not token:
            continue
        try:
            current = current[int(token)] if isinstance(current, list) else current[token]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return current


def _set_pointer(data: Any, pointer: str, value: Any) -> None:
    tokens = [token for token in pointer.strip("/").split("/") if token]
    current = data
    for token in tokens[:-1]:
        current = current[int(token)] if isinstance(current, list) else current.setdefault(token, {})
    if isinstance(current, list):
        current[int(tokens[-1])] = value
    else:
        current[tokens[-1]] = value


def is_parameter_active(key: str, parameter_set: ParameterSet) -> bool:
    """Return whether a conditional parameter participates in the current gates."""
    if key == "loads.center_of_mass_offset_mm":
        return float(_value(parameter_set, "loads.top_mass_tonne", 0) or 0) > 0
    if parameter_set.capability_id == L2_CAPABILITY_ID:
        mode = str(_value(parameter_set, "loads.service_control_mode", "section_loads"))
        if key.startswith("boundary.service."):
            return mode == "section_displacement"
        if key in {
            "loads.section_force_n", "loads.shear_x_n", "loads.shear_y_n",
            "loads.torsion_nmm", "loads.moment_x_nmm", "loads.moment_y_nmm",
        }:
            return mode == "section_loads"
    return True


def _issue(path: str, code: str, message: str) -> dict[str, str]:
    actions = {
        "unconfirmed": "confirm_parameter",
        "missing": "provide_parameter",
        "invalid": "edit_parameter",
        "range": "edit_parameter",
        "unsupported": "edit_parameter",
    }
    return {
        "layer": "parameter",
        "path": path,
        "parameter_key": path,
        "code": code,
        "message": message,
        "resolution_action": actions.get(code, "review_parameter"),
    }
