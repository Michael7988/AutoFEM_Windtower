from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Callable

from .config import ABAQUSWORK
from .meshing.models import normalize_tower_points
from .meshing.sets import element_center as native_element_center
from .meshing.sets import segments_from_centers, windward_surface_groups as native_windward_surface_groups


class ExternalAbaqusToolUnavailable(RuntimeError):
    """Raised when an optional legacy Abaqus helper script is unavailable."""


_ASSEMBLY_MODULE: Any | None = None
_MESH_MODULE: Any | None = None
_ASSEMBLY_ERROR: Exception | None = None
_MESH_ERROR: Exception | None = None


def adapter_status() -> dict[str, Any]:
    assembly = _load_assembly_module(required=False)
    mesh = _load_mesh_module(required=False)
    return {
        "abaquswork": str(ABAQUSWORK),
        "abaquswork_exists": ABAQUSWORK.exists(),
        "assemble_abaqus_analysis": "available" if assembly else "missing",
        "build_windtower_mesh": "available" if mesh else "missing",
        "errors": [str(error) for error in (_ASSEMBLY_ERROR, _MESH_ERROR) if error],
    }


def build_conforming_door_mesh(*args: Any, **kwargs: Any) -> Any:
    return _assembly_func("build_conforming_door_mesh")(*args, **kwargs)


def element_center(nodes: dict[int, tuple[float, float, float]], conn: list[int]) -> tuple[float, float, float]:
    module = _load_assembly_module(required=False)
    return module.element_center(nodes, conn) if module else native_element_center(nodes, conn)


def find_flange_ranges(tower: list[dict[str, Any]], flanges: list[dict[str, Any]]) -> list[Any]:
    module = _load_assembly_module(required=False)
    if module:
        return module.find_flange_ranges(tower, flanges)
    if not tower or not flanges:
        return []
    points = normalize_tower_points(tower)
    ranges: list[Any] = []
    start_index = 0
    for flange in flanges:
        diameter = float(flange.get("diameter_mm") or flange.get("mean_diameter_mm") or 0.0)
        thickness = float(flange.get("total_thickness_mm") or 0.0)
        candidates = list(enumerate(points[start_index:], start=start_index)) or list(enumerate(points))
        index, point = min(candidates, key=lambda item: (abs(item[1].diameter_mm - diameter), item[0]))
        half = max(thickness, 1.0) * 0.5
        ranges.append((flange, max(points[0].z_mm, point.z_mm - half), min(points[-1].z_mm, point.z_mm + half)))
        start_index = min(index + 1, len(points) - 1)
    return ranges


def segments_from_element_centers(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, list[int]],
    tower: list[dict[str, Any]],
) -> dict[int, int]:
    module = _load_assembly_module(required=False)
    if module:
        return module.segments_from_element_centers(nodes, elements, tower)
    return segments_from_centers(nodes, elements, normalize_tower_points(tower))


def static_check(*args: Any, **kwargs: Any) -> Any:
    return _assembly_func("static_check")(*args, **kwargs)


def windward_surface_groups(nodes: dict[int, tuple[float, float, float]], elements: dict[int, list[int]]) -> tuple[list[int], list[int]]:
    module = _load_assembly_module(required=False)
    return module.windward_surface_groups(nodes, elements) if module else native_windward_surface_groups(nodes, elements)


def write_generated_analysis_inp(*args: Any, **kwargs: Any) -> Any:
    return _assembly_func("write_generated_analysis_inp")(*args, **kwargs)


def build_stepped_door_mesh(*args: Any, **kwargs: Any) -> Any:
    return _mesh_func("build_mesh")(*args, **kwargs)


def _assembly_func(name: str) -> Callable[..., Any]:
    module = _load_assembly_module(required=True)
    return getattr(module, name)


def _mesh_func(name: str) -> Callable[..., Any]:
    module = _load_mesh_module(required=True)
    return getattr(module, name)


def _load_assembly_module(required: bool) -> Any | None:
    global _ASSEMBLY_MODULE, _ASSEMBLY_ERROR
    if _ASSEMBLY_MODULE is not None:
        return _ASSEMBLY_MODULE
    try:
        _ensure_abaquswork_on_path()
        _ASSEMBLY_MODULE = importlib.import_module("assemble_abaqus_analysis")
        return _ASSEMBLY_MODULE
    except Exception as exc:
        _ASSEMBLY_ERROR = exc
        if required:
            raise ExternalAbaqusToolUnavailable(_missing_message("assemble_abaqus_analysis.py", exc)) from exc
        return None


def _load_mesh_module(required: bool) -> Any | None:
    global _MESH_MODULE, _MESH_ERROR
    if _MESH_MODULE is not None:
        return _MESH_MODULE
    try:
        _ensure_abaquswork_on_path()
        _MESH_MODULE = importlib.import_module("build_windtower_mesh")
        return _MESH_MODULE
    except Exception as exc:
        _MESH_ERROR = exc
        if required:
            raise ExternalAbaqusToolUnavailable(_missing_message("build_windtower_mesh.py", exc)) from exc
        return None


def _ensure_abaquswork_on_path() -> None:
    path = str(Path(ABAQUSWORK))
    if path not in sys.path:
        sys.path.insert(0, path)


def _missing_message(script_name: str, error: Exception) -> str:
    return f"Optional Abaqus helper {script_name} is unavailable under {ABAQUSWORK}: {error}"
