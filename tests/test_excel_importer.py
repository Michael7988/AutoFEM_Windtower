from __future__ import annotations

import unittest
from windtower_backend.importers.windtower_adapter import parse_windtower_workbook
from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.importers.door_derivation import derive_door_candidates


class ExcelImporterTest(unittest.TestCase):
    def test_fem_parameter_workbook_is_golden_sample(self) -> None:
        workbook = EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls"

        result = parse_windtower_workbook(workbook)

        self.assertEqual(len(result["tower"]), 42)
        self.assertEqual(len(result["flanges"]), 1)
        self.assertEqual(set(result["door"]), {"D", "H", "P", "W", "X1", "X2", "Y1", "Y2"})
        self.assertEqual(len(result["fem_params"]), 24)
        self.assertEqual(result["fem_params"]["material.name"], "SYNTHETIC_STEEL")
        self.assertEqual(result["fem_params"]["element.element_type"], "S4R")
        self.assertEqual(result["fem_params"]["mesh.strategy"], "V3_CONFORMING_DOOR_MESH")
        self.assertEqual(result["fem_params"]["steps.nlgeom"], "YES")
        self.assertEqual(result["fem_params"]["boundary.dofs"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(result["fem_model_facts"]["material"]["name"], "SYNTHETIC_STEEL")
        self.assertEqual(result["derived_facts"]["door_derivation"]["input_status"], "complete_excel")
        self.assertEqual(result["missing_items"], [])
        self.assertIn("confidence_report", result)
        self.assertFalse(result["confidence_report"]["llm_assist_required"])
        self.assertEqual(result["llm_suggestions"]["authority"], "advisory_only")
        self.assertFalse(result["llm_suggestions"]["enabled"])

    def test_missing_fem_params_triggers_llm_assist_policy(self) -> None:
        from windtower_backend.importers.parse_result import build_confidence_report, build_llm_suggestions

        sheet_map = {"tower": "tower", "flange": "flange", "door": "door", "fem_params": None}
        tower = [
            {"z_mm": 0, "diameter_mm": 4000, "thickness_mm": 30, "source": "tower!R1"},
            {"z_mm": 1000, "diameter_mm": 3900, "thickness_mm": 30, "source": "tower!R2"},
        ]
        flanges = [{"total_thickness_mm": 120, "source": "flange!R1"}]
        door = {"X2": 900, "Y2": 2300, "H": 1200}
        missing = [{"item": "fem_model_parameters", "reason": "Missing FEM parameters."}]

        report = build_confidence_report(sheet_map, tower, flanges, door, {}, missing, [])
        suggestions = build_llm_suggestions(sheet_map, {}, missing, report)

        self.assertTrue(report["llm_assist_required"])
        self.assertIn("fem_key_value_parameters_missing", report["llm_trigger_reasons"])
        self.assertTrue(suggestions["enabled"])
        self.assertEqual(suggestions["patch_candidates"], [])
        self.assertEqual(suggestions["authority"], "advisory_only")

    def test_missing_door_generates_comparison_candidates(self) -> None:
        tower = [
            {"z_mm": 0, "diameter_mm": 4200, "thickness_mm": 30},
            {"z_mm": 1000, "diameter_mm": 4100, "thickness_mm": 30},
        ]

        result = derive_door_candidates(tower, {})

        self.assertEqual(result["input_status"], "door_derivation_required")
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual({item["shape"] for item in result["candidates"]}, {"ellipse", "rounded_rectangle"})
        self.assertIsNone(result["selected_shape"])

if __name__ == "__main__":
    unittest.main()
