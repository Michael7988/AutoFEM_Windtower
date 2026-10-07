from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from windtower_backend.importers.windtower_adapter import parse_windtower_workbook
from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.meshing import build_windtower_quad_mesh
from windtower_backend.meshing.abaqus_writer import write_mesh_inp
from windtower_backend.meshing.models import normalize_tower_points


class WindTowerMeshingTest(unittest.TestCase):
    def test_quad_gmsh_mesh_matches_reference_scale(self) -> None:
        workbook = EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls"
        parsed = parse_windtower_workbook(workbook)

        result = build_windtower_quad_mesh(
            tower_sections=parsed["tower"],
            door=parsed["door"],
            flanges=parsed["flanges"],
            controls={
                "global_size_mm": 650.0,
                "door_size_mm": 120.0,
                "hole_layers": 32,
                "patch_margin_u_mm": 1450.0,
                "patch_margin_z_mm": 900.0,
            },
        )

        self.assertEqual(result.strategy, "V3_CONFORMING_DOOR_MESH")
        self.assertEqual(len(result.nodes), 14154)
        self.assertEqual(len(result.elements), 14028)
        self.assertTrue(result.quality_report.metrics["all_quadrilateral"])
        self.assertIn(result.quality_report.status, {"pass", "warning"})
        self.assertGreater(len(result.sets["DOOR_OPENING_NODES"]), 100)
        metrics = result.quality_report.metrics
        self.assertEqual(metrics["door_boundary_loop_count"], 1)
        self.assertTrue(metrics["door_boundary_is_closed"])
        self.assertEqual(metrics["door_boundary_branch_nodes"], 0)
        self.assertEqual(metrics["elements_inside_door_opening"], 0)
        self.assertEqual(metrics["elements_crossing_door_boundary"], 0)
        self.assertLess(metrics["door_neighbor_edge_p50_mm"], 0.35 * 650.0)
        self.assertLess(metrics["door_neighbor_edge_p90_mm"], 0.55 * 650.0)
        self.assertGreater(len(result.sets["BASE_FIXED"]), 0)
        self.assertGreater(len(result.sets["TOP_LOAD_NODES"]), 0)
        self.assertGreater(len(result.surfaces["WINDWARD_SURFACE"]["SPOS"]) + len(result.surfaces["WINDWARD_SURFACE"]["SNEG"]), 0)

    def test_mesh_artifact_writers_are_mesh_only(self) -> None:
        workbook = EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls"
        parsed = parse_windtower_workbook(workbook)
        result = build_windtower_quad_mesh(parsed["tower"], parsed["door"], parsed["flanges"], parsed["fem_params"])

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "mesh_only.inp"
            write_mesh_inp(out, result, normalize_tower_points(parsed["tower"]))
            text = out.read_text(encoding="utf-8")

        self.assertIn("*Element, type=S4R", text)
        self.assertIn("*Shell Section", text)
        self.assertNotIn("*Step", text)
        self.assertNotIn("*Cload", text)
        self.assertNotIn("*Dsload", text)


if __name__ == "__main__":
    unittest.main()
