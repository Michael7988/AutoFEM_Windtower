from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.facts import parse_engineering_facts, write_fact_artifacts


class EngineeringFactsTest(unittest.TestCase):
    def test_golden_workbook_builds_traceable_engineering_fact_package(self) -> None:
        workbook = EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls"

        package = parse_engineering_facts(workbook)
        facts = {fact.fact_id: fact for fact in package.facts}

        self.assertIn(package.status, {"pass", "warning"})
        self.assertEqual(package.summary["tower_point_count"], 42)
        self.assertEqual(package.summary["flange_count"], 1)
        self.assertTrue(package.summary["door_complete"])
        self.assertEqual(package.summary["fem_param_count"], 24)
        self.assertFalse(package.summary["llm_assist_required"])
        self.assertEqual(facts["tower_profile.001.thickness_mm"].source.parser, "tower_parser")
        self.assertEqual(facts["material.name"].value, "SYNTHETIC_STEEL")
        self.assertEqual(facts["element.element_type"].value, "S4R")
        self.assertEqual(facts["mesh.strategy"].value, "V3_CONFORMING_DOOR_MESH")
        self.assertIn("loads.top_total_force_n", facts)
        self.assertIn("loads.wind_pressure_mpa", facts)
        self.assertIn("boundary.name", facts)
        self.assertTrue(facts["door.X2"].source.workbook.endswith(".xls"))

    def test_fact_artifacts_are_written_for_paper_audit(self) -> None:
        workbook = EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls"
        package = parse_engineering_facts(workbook)

        with tempfile.TemporaryDirectory() as tmp:
            artifacts = write_fact_artifacts(package, Path(tmp))

            expected = {
                "engineering_facts.json",
                "parse_validation_report.json",
                "traceability_matrix.csv",
                "input_manifest.json",
                "parse_report.md",
                "parse_report.html",
            }
            self.assertEqual(set(artifacts), expected)
            for path in artifacts.values():
                self.assertTrue(path.exists())
                self.assertGreater(path.stat().st_size, 0)

    def test_missing_fem_parameters_become_unresolved_facts(self) -> None:
        from windtower_backend.importers.parse_result import build_confidence_report, build_llm_suggestions
        from windtower_backend.facts.builder import build_engineering_fact_package

        imported = {
            "source_file": "missing_fem.xls",
            "source_files": ["missing_fem.xls"],
            "detected_format": "unit_test",
            "sheet_map": {"tower": "tower", "flange": "flange", "door": "door", "fem_params": None},
            "tower": [
                {"z_mm": 0, "diameter_mm": 4000, "thickness_mm": 30, "source": "tower!R1"},
                {"z_mm": 1000, "diameter_mm": 3900, "thickness_mm": 30, "source": "tower!R2"},
            ],
            "flanges": [{"diameter_mm": 4000, "total_thickness_mm": 120, "source": "flange!R1"}],
            "door": {"X2": 900, "Y2": 2300, "H": 1200},
            "fem_params": {},
            "fem_param_records": [],
            "loads_inventory": {},
            "missing_items": [{"item": "fem_model_parameters", "reason": "Missing FEM parameters."}],
            "warnings": [],
            "parsed_facts": {"door_complete": True},
            "derived_facts": {"door_derivation": {"input_status": "complete_excel", "candidates": []}},
        }
        report = build_confidence_report(imported["sheet_map"], imported["tower"], imported["flanges"], imported["door"], {}, imported["missing_items"], [])
        imported["confidence_report"] = report
        imported["llm_suggestions"] = build_llm_suggestions(imported["sheet_map"], {}, imported["missing_items"], report)

        package = build_engineering_fact_package(imported, "missing_fem.xls")
        unresolved = [fact for fact in package.facts if fact.status == "unresolved_fact"]

        self.assertEqual(package.status, "blocked")
        self.assertTrue(package.summary["llm_assist_required"])
        self.assertTrue(any(fact.fact_id == "material.name" for fact in unresolved))


if __name__ == "__main__":
    unittest.main()
