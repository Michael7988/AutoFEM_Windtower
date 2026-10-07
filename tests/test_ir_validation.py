from __future__ import annotations

import copy
import unittest

from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.facts import parse_engineering_facts
from windtower_backend.ir import build_ir_from_facts
from windtower_backend.validation import validate_ir_layers


class IrValidationTest(unittest.TestCase):
    def _bundle(self):
        workbook = sorted(EXAMPLE_ROOT.glob("*FEM*.xls"))[0]
        return build_ir_from_facts(parse_engineering_facts(workbook), project_id="wt-val-test").ir_bundle

    def test_golden_ir_validation_is_not_blocked(self) -> None:
        validation = validate_ir_layers(self._bundle())

        self.assertIn(validation.status, {"pass", "warning"})
        self.assertNotEqual(validation.status, "blocked")
        self.assertEqual(validation.schema_validation_report.status, "pass")
        self.assertEqual(validation.geometry_validation_report.status, "pass")
        self.assertEqual(validation.abaqus_semantic_validation_report.status, "pass")

    def test_missing_elastic_modulus_blocks_physics_validation(self) -> None:
        bundle = copy.deepcopy(self._bundle())
        bundle.model_ir["materials"][0]["elastic_modulus_mpa"] = None

        validation = validate_ir_layers(bundle)

        self.assertEqual(validation.status, "blocked")
        self.assertTrue(any(issue.field == "elastic_modulus_positive" for issue in validation.physics_validation_report.issues))

    def test_invalid_door_dimensions_block_geometry_validation(self) -> None:
        bundle = copy.deepcopy(self._bundle())
        bundle.model_ir["geometry"]["door_openings"][0]["height_mm"] = 999999999

        validation = validate_ir_layers(bundle)

        self.assertEqual(validation.geometry_validation_report.status, "blocked")

    def test_missing_boundary_and_bad_load_step_block_abaqus_validation(self) -> None:
        bundle = copy.deepcopy(self._bundle())
        bundle.abaqus_ir["boundary_conditions"] = []
        bundle.abaqus_ir["loads"] = [{"name": "BAD_LOAD", "step": "MISSING_STEP"}]

        validation = validate_ir_layers(bundle)

        self.assertEqual(validation.abaqus_semantic_validation_report.status, "blocked")
        fields = {issue.field for issue in validation.abaqus_semantic_validation_report.issues}
        self.assertIn("boundary_declared", fields)
        self.assertIn("load_step_refs_valid", fields)


if __name__ == "__main__":
    unittest.main()
