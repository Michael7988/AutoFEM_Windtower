from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..capability_profile import CANONICAL_DEFAULTS, MESH_STRATEGY

STRATEGY = MESH_STRATEGY


@dataclass(frozen=True)
class TowerMeshInput:
    z_mm: float
    diameter_mm: float
    thickness_mm: float
    source: str = ""


@dataclass(frozen=True)
class DoorMeshInput:
    shape: str = "ellipse"
    width_mm: float = 0.0
    height_mm: float = 0.0
    bottom_z_mm: float = 0.0
    theta_center_deg: float = 0.0
    outer_width_mm: float = 0.0
    outer_height_mm: float = 0.0
    reinforcement_enabled: bool = False
    reinforcement_thickness_mm: float = 0.0
    reinforcement_material: str = "Q355ND"

    @property
    def center_z_mm(self) -> float:
        return self.bottom_z_mm + 0.5 * self.height_mm


@dataclass(frozen=True)
class MeshControls:
    global_size_mm: float = float(CANONICAL_DEFAULTS["mesh.global_size_mm"])
    door_size_mm: float = float(CANONICAL_DEFAULTS["mesh.door_size_mm"])
    flange_size_mm: float = 500.0
    hole_layers: int = int(CANONICAL_DEFAULTS["mesh.hole_layers"])
    patch_margin_u_mm: float = float(CANONICAL_DEFAULTS["mesh.patch_margin_u_mm"])
    patch_margin_z_mm: float = float(CANONICAL_DEFAULTS["mesh.patch_margin_z_mm"])
    write_msh: bool = False


@dataclass
class MeshQualityReport:
    status: str
    blocked: bool
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeshResult:
    strategy: str
    nodes: dict[int, tuple[float, float, float]]
    elements: dict[int, list[int]]
    element_segments: dict[int, int]
    sets: dict[str, list[int] | dict[str, list[int]]]
    surfaces: dict[str, dict[str, list[int]]]
    quality_report: MeshQualityReport
    artifacts: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize_tower_points(tower_sections: list[dict[str, Any]]) -> list[TowerMeshInput]:
    if not tower_sections:
        return []
    if "z_mm" in tower_sections[0]:
        points = [
            TowerMeshInput(
                z_mm=float(item["z_mm"]),
                diameter_mm=float(item["diameter_mm"]),
                thickness_mm=float(item.get("thickness_mm") or 0.0),
                source=str(item.get("source", "")),
            )
            for item in tower_sections
        ]
    else:
        points = []
        for item in tower_sections:
            points.append(
                TowerMeshInput(
                    z_mm=float(item["z_bottom_mm"]),
                    diameter_mm=float(item["diameter_bottom_mm"]),
                    thickness_mm=float(item.get("thickness_mm") or 0.0),
                    source=str(item.get("source", "")),
                )
            )
        last = tower_sections[-1]
        points.append(
            TowerMeshInput(
                z_mm=float(last["z_top_mm"]),
                diameter_mm=float(last["diameter_top_mm"]),
                thickness_mm=float(last.get("thickness_mm") or 0.0),
                source=str(last.get("source", "")),
            )
        )
    return sorted(points, key=lambda item: item.z_mm)


def normalize_door(door: dict[str, Any]) -> DoorMeshInput | None:
    width = door.get("X2", door.get("width_mm"))
    height = door.get("Y2", door.get("height_mm"))
    bottom = door.get("H", door.get("bottom_z_mm"))
    if width is None or height is None or bottom is None:
        return None
    return DoorMeshInput(
        shape=str(door.get("shape", "ellipse")),
        width_mm=float(width),
        height_mm=float(height),
        bottom_z_mm=float(bottom),
        theta_center_deg=float(door.get("theta_center_deg", 0.0) or 0.0),
        outer_width_mm=float(door.get("X1", door.get("outer_width_mm", width)) or width),
        outer_height_mm=float(door.get("Y1", door.get("outer_height_mm", height)) or height),
        reinforcement_enabled=bool(door.get("reinforcement_enabled", door.get("local_reinforcement", False))),
        reinforcement_thickness_mm=float(door.get("W", door.get("frame_thickness_mm", 0.0)) or 0.0),
        reinforcement_material=str(door.get("reinforcement_material", "Q355ND") or "Q355ND"),
    )


def normalize_controls(controls: dict[str, Any] | MeshControls | None) -> MeshControls:
    if controls is None:
        return MeshControls()
    if isinstance(controls, MeshControls):
        return controls
    return MeshControls(
        global_size_mm=float(controls.get("global_size_mm", CANONICAL_DEFAULTS["mesh.global_size_mm"])),
        door_size_mm=float(controls.get("door_size_mm", CANONICAL_DEFAULTS["mesh.door_size_mm"])),
        flange_size_mm=float(controls.get("flange_size_mm", 500.0)),
        hole_layers=int(controls.get("hole_layers", CANONICAL_DEFAULTS["mesh.hole_layers"])),
        patch_margin_u_mm=float(controls.get("patch_margin_u_mm", CANONICAL_DEFAULTS["mesh.patch_margin_u_mm"])),
        patch_margin_z_mm=float(controls.get("patch_margin_z_mm", CANONICAL_DEFAULTS["mesh.patch_margin_z_mm"])),
        write_msh=bool(controls.get("write_msh", False)),
    )
