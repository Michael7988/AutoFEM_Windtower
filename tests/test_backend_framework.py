from __future__ import annotations

import unittest

from windtower_backend.abaqus_adapter import adapter_status
from windtower_backend.config import (
    ABAQUSWORK,
    BACKEND_ROOT,
    DATA_ROOT,
    ROOT,
    STORAGE_ROOT,
    WORK_ROOT,
)


class BackendFrameworkTest(unittest.TestCase):
    def test_project_paths_resolve_backend_and_abaquswork(self) -> None:
        self.assertEqual(BACKEND_ROOT.name, "windtower_backend")
        self.assertEqual(ROOT.name, "AutoFEM_Windtower")
        expected_storage = DATA_ROOT / "projects" if DATA_ROOT else BACKEND_ROOT / "storage"
        expected_work = WORK_ROOT if WORK_ROOT else ROOT / "ABAQUSwork"
        self.assertEqual(STORAGE_ROOT, expected_storage)
        self.assertEqual(ABAQUSWORK, expected_work)

    def test_abaqus_adapter_reports_external_script_status(self) -> None:
        status = adapter_status()

        if "missing" in status.values():
            self.skipTest("Optional external Abaqus helper scripts are not installed in this workspace")

        self.assertEqual(status["assemble_abaqus_analysis"], "available")
        self.assertEqual(status["build_windtower_mesh"], "available")

if __name__ == "__main__":
    unittest.main()
