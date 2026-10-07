from __future__ import annotations

import pytest

from windtower_backend.importers.fem_facts import parse_wind_pressure_profile
from windtower_backend.l0.implementation import normalize_wind_direction, rotate_local_top_loads
from windtower_backend.meshing import build_windtower_quad_mesh
from windtower_backend.meshing.sets import windward_surface_groups


def test_excel_wind_pressure_profile_is_compiled_to_five_bands() -> None:
    profile = parse_wind_pressure_profile(
        "0-20m:0.0008;20-40m:0.0010;40-60m:0.00115;"
        "60-80m:0.00130;80-110m:0.00145"
    )

    assert len(profile) == 5
    assert profile[0]["z_min_mm"] == 0.0
    assert profile[-1]["z_max_mm"] == 110_000.0
    assert profile[-1]["pressure_mpa"] == pytest.approx(0.00145)


def test_signed_abaqus_wind_pressure_profile_is_preserved() -> None:
    profile = parse_wind_pressure_profile(
        "0-20m:-0.00089;20-40m:-0.00114;40-60m:-0.00127;"
        "60-80m:-0.00137;80-110m:-0.00145"
    )

    assert len(profile) == 5
    assert [item["pressure_mpa"] for item in profile] == pytest.approx(
        [-0.00089, -0.00114, -0.00127, -0.00137, -0.00145]
    )


def test_wind_vector_rotates_force_and_cog_moment_but_not_vertical_force() -> None:
    vector = normalize_wind_direction([0.0, 2.0, 0.0])
    result = rotate_local_top_loads(
        {
            "top_force_parallel_n": 1_000_000.0,
            "top_force_transverse_n": 100_000.0,
            "top_force_vertical_n": -3_139_200.0,
            "top_moment_roll_nmm": 6.0e9,
            "top_moment_pitch_nmm": 2.0e9,
            "top_moment_yaw_nmm": 5.0e9,
        },
        vector,
        mass_tonne=320.0,
        eccentricity_mm=1500.0,
    )

    assert result["Fx"] == pytest.approx(-100_000.0)
    assert result["Fy"] == pytest.approx(-1_000_000.0)
    assert result["Fz"] == pytest.approx(-3_139_200.0)
    assert result["Mx"] == pytest.approx(2.0e9 + 3_139_200.0 * 1500.0)
    assert result["My"] == pytest.approx(6.0e9)
    assert result["Mz"] == pytest.approx(5.0e9)


def test_windward_selection_uses_supplied_vector() -> None:
    nodes = {
        1: (1.0, 0.0, 0.0), 2: (0.0, 1.0, 0.0),
        3: (0.0, 1.0, 1.0), 4: (1.0, 0.0, 1.0),
        5: (-1.0, 0.0, 0.0), 6: (0.0, -1.0, 0.0),
        7: (0.0, -1.0, 1.0), 8: (-1.0, 0.0, 1.0),
    }
    elements = {1: [1, 2, 3, 4], 2: [5, 6, 7, 8]}

    x_selected = set().union(*map(set, windward_surface_groups(nodes, elements, (1.0, 0.0, 0.0))))
    minus_x_selected = set().union(*map(set, windward_surface_groups(nodes, elements, (-1.0, 0.0, 0.0))))

    assert x_selected == {1}
    assert minus_x_selected == {2}


def test_reinforcement_ring_has_conforming_nonempty_element_set() -> None:
    tower = [
        {"z_mm": 0.0, "diameter_mm": 4244.0, "thickness_mm": 42.0},
        {"z_mm": 5000.0, "diameter_mm": 4244.0, "thickness_mm": 42.0},
        {"z_mm": 10_000.0, "diameter_mm": 4000.0, "thickness_mm": 35.0},
    ]
    door = {
        "X2": 900.0, "Y2": 2300.0, "H": 1200.0,
        "X1": 1200.0, "Y1": 2600.0, "W": 120.0,
        "reinforcement_enabled": True,
    }
    result = build_windtower_quad_mesh(
        tower,
        door,
        controls={
            "global_size_mm": 650.0, "door_size_mm": 120.0,
            "hole_layers": 32, "patch_margin_u_mm": 1450.0,
            "patch_margin_z_mm": 900.0,
        },
    )

    reinforcement = result.sets["DOOR_REINFORCEMENT_ELEMENTS"]
    adjacent = result.sets["DOOR_REINFORCEMENT_ADJACENT_ELEMENTS"]
    check = result.sets["DOOR_REINFORCEMENT_CHECK_ELEMENTS"]
    assert reinforcement
    assert adjacent
    assert set(check) == set(reinforcement) | set(adjacent)
    assert set(reinforcement).isdisjoint(adjacent)
    assert set(reinforcement).issubset(result.elements)
    assert result.quality_report.blocked is False
    assert result.sets["DOOR_OPENING_NODES"]
