from __future__ import annotations

import unittest

from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.facts import parse_engineering_facts
from windtower_backend.ir import build_ir_from_facts


class IrBuilderTest(unittest.TestCase):
    def test_golden_excel_builds_fact_model_and_abaqus_ir(self) -> None:
        workbook = sorted(EXAMPLE_ROOT.glob("*FEM*.xls"))[0]
        package = parse_engineering_facts(workbook)

        result = build_ir_from_facts(package, project_id="wt-ir-test")
        bundle = result.ir_bundle

        self.assertIn(result.status, {"pass", "warning"})
        self.assertEqual(bundle.fact_ir["ir_type"], "fact_ir")
        self.assertEqual(bundle.model_ir["ir_type"], "model_ir")
        self.assertEqual(bundle.abaqus_ir["ir_type"], "abaqus_ir")
        self.assertTrue(bundle.model_ir["geometry"]["tower_sections"])
        self.assertTrue(bundle.model_ir["geometry"]["fact_refs"])
        self.assertEqual(bundle.model_ir["materials"][0]["name"], "SYNTHETIC_STEEL")
        self.assertEqual(bundle.abaqus_ir["elements"][0]["type"], "S4R")
        self.assertEqual(bundle.abaqus_ir["sections"][0]["type"], "Shell Section")
        self.assertTrue(bundle.abaqus_ir["boundary_conditions"])
        self.assertIn("top_total_force_n", bundle.model_ir["loads"])
        self.assertIn("wind_pressure_mpa", bundle.model_ir["loads"])
        self.assertTrue(bundle.abaqus_ir["source_trace"])

    def test_unresolved_fem_facts_block_ir_build(self) -> None:
        workbook = sorted(EXAMPLE_ROOT.glob("*FEM*.xls"))[0]
        package = parse_engineering_facts(workbook)
        next(fact for fact in package.facts if fact.fact_id == "material.name").status = "unresolved_fact"

        result = build_ir_from_facts(package, project_id="wt-ir-missing")

        self.assertEqual(result.status, "blocked")
        self.assertTrue(any(item["fact_id"] == "material.name" for item in result.ir_bundle.unresolved))


if __name__ == "__main__":
    unittest.main()
