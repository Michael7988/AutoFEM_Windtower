from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from windtower_backend.fea_agent import (
    build_abaqus_diagnostic_report,
    build_model_validation_report,
    build_result_sanity_report,
)
from windtower_backend.config import EXAMPLE_ROOT
from windtower_backend.importers.windtower_adapter import parse_windtower_workbook
from windtower_backend.modeling import build_ir


class FeaAgentTest(unittest.TestCase):
    def _sample_ir(self) -> dict:
        workbook = EXAMPLE_ROOT / "synthetic_mechanical_interface_FEM.xls"
        parsed = parse_windtower_workbook(workbook)
        return build_ir(
            "wt-test",
            workbook.name,
            parsed["tower"],
            parsed["flanges"],
            parsed["door"],
            parsed["fem_params"],
            import_meta=parsed,
        )

    def test_model_validation_report_is_typed_and_advisory(self) -> None:
        report = build_model_validation_report(self._sample_ir())

        self.assertEqual(report["report_type"], "ModelValidationReport")
        self.assertIn(report["status"], {"pass", "warning"})
        self.assertFalse(report["blocked"])
        self.assertEqual(report["llm_policy"]["forbidden_outputs"][0], "direct Abaqus INP mutation")
        self.assertIn("geometry", report["coverage"])

    def test_abaqus_diagnostics_classifies_error_categories(self) -> None:
        log_text = """
        Abaqus Error: The set BASE_FIXED has not been defined
        WARNING: output request U is empty
        """

        report = build_abaqus_diagnostic_report(log_text, returncode=1, job_type="static_strength")

        categories = {item["category"] for item in report["findings"]}
        self.assertEqual(report["status"], "blocked")
        self.assertIn("empty_set", categories)
        self.assertIn("abaqus_warning", categories)
        self.assertTrue(any(item["layer"] == "boundary_or_set" for item in report["routing"]))
        self.assertTrue(all(item["automatic_apply"] is False for item in report["routing"]))

    def test_abaqus_diagnostics_routes_missing_section_without_auto_repair(self) -> None:
        report = build_abaqus_diagnostic_report(
            "***ERROR: 48 elements have missing property definitions. The elements have no section property.",
            returncode=1,
            job_type="static_strength",
        )
        categories = {item["category"] for item in report["findings"]}
        self.assertIn("section_missing", categories)
        self.assertTrue(any(item["layer"] == "material_or_section" for item in report["routing"]))
        self.assertIn("no automatic", report["repair_policy"])

    def test_abaqus_diagnostics_classifies_contact_attempt_and_eigen_warnings(self) -> None:
        log_text = """
        ***WARNING: DISPLACEMENT INCREMENT FOR CONTACT IS TOO BIG.
        ***ERROR: TOO MANY ATTEMPTS MADE FOR THIS INCREMENT
        168 ANALYSIS WARNINGS ARE NEGATIVE EIGENVALUE MESSAGES
        """

        report = build_abaqus_diagnostic_report(log_text, returncode=1, job_type="static_strength")

        categories = {item["category"] for item in report["findings"]}
        self.assertEqual(report["status"], "blocked")
        self.assertIn("contact_increment_too_large", categories)
        self.assertIn("too_many_attempts", categories)
        self.assertIn("negative_eigen_warning", categories)

    def test_result_sanity_report_handles_pending_placeholders(self) -> None:
        ir = self._sample_ir()
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            results.mkdir()
            (results / "results_summary.json").write_text('{"status": "warning"}', encoding="utf-8")
            (results / "door_stress_summary.csv").write_text("max_mises,p95_mises\npending_odb_extraction,pending_odb_extraction\n", encoding="utf-8")
            report = build_result_sanity_report(Path(tmp), ir)

        self.assertEqual(report["report_type"], "ResultSanityReport")
        self.assertEqual(report["status"], "warning")
        self.assertTrue(any(item["status"] == "pending" for item in report["checks"]))


if __name__ == "__main__":
    unittest.main()
