from __future__ import annotations

import csv
from pathlib import Path
import unittest

from windtower_backend.automation import SingleL0WorkflowOptions, run_single_l0_workflow
from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.l0 import postprocess_l0
from windtower_backend.modeling import ProjectState, build_ir
from windtower_backend.importers.windtower_adapter import parse_windtower_workbook


class SingleL0AutomationTest(unittest.TestCase):
    def test_single_excel_workflow_requires_explicit_offset_confirmation(self) -> None:
        workbook = sorted(EXAMPLE_ROOT.glob("*FEM*.xls"))[0]

        result = run_single_l0_workflow(SingleL0WorkflowOptions(excel_path=workbook, run_abaqus=False, extract_odb=True))

        self.assertEqual(result["status"], "blocked")
        issues = result["stages"]["parameter_gate"]["validation_report"]["errors"]
        self.assertTrue(any(item.get("parameter_key") == "loads.center_of_mass_offset_mm" for item in issues))
        self.assertFalse(any(name.endswith(".inp") for name in result["artifacts"]))

    def test_postprocess_without_odb_keeps_pending_values(self) -> None:
        workbook = sorted(EXAMPLE_ROOT.glob("*FEM*.xls"))[0]
        parsed = parse_windtower_workbook(workbook)
        ir = build_ir("wt-postprocess-test", workbook.name, parsed["tower"], parsed["flanges"], parsed["door"], parsed["fem_params"], import_meta=parsed)
        with __import__("tempfile").TemporaryDirectory() as tmp:
            state = ProjectState(project_id="wt-postprocess-test", root=Path(tmp), excel_path=workbook, ir=ir, artifacts={})
            summary = postprocess_l0(state)
            rows = list(csv.DictReader((Path(tmp) / "results" / "door_stress_summary.csv").open(newline="", encoding="utf-8")))

        self.assertEqual(summary["status"], "warning")
        self.assertEqual(summary["odb_extraction"]["status"], "skipped")
        self.assertTrue(rows)
        self.assertTrue(str(rows[0]["max_mises"]).startswith("pending"))


if __name__ == "__main__":
    unittest.main()
