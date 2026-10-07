from __future__ import annotations

import argparse
import json
from pathlib import Path

from .single_l0 import SingleL0WorkflowOptions, run_single_l0_workflow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Excel -> L0 FEM automation workflow.")
    parser.add_argument("--excel", required=True, help="Path to the wind-tower FEM parameter workbook.")
    parser.add_argument("--job-type", default="all", choices=["all", "static_strength", "modal", "buckling"])
    parser.add_argument("--run-abaqus", action="store_true", help="Run Abaqus datacheck and jobs after INP build.")
    parser.add_argument("--extract-odb", action="store_true", help="Run postprocess and ODB extraction if ODB files exist.")
    args = parser.parse_args()
    result = run_single_l0_workflow(
        SingleL0WorkflowOptions(
            excel_path=Path(args.excel),
            job_type=args.job_type,
            run_abaqus=args.run_abaqus,
            extract_odb=args.extract_odb,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
