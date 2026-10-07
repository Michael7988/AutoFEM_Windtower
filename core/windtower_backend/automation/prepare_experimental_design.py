from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..benchmark.dataset import generate_llm_benchmark
from ..benchmark.experiment_dataset import generate_experiment_datasets


DEFAULT_DESIGN_ROOT = (
    Path(__file__).resolve().parents[3] / "Experimental Design" / "datasets" / "v1"
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Generate the frozen v1 wind-tower CAE research datasets."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_DESIGN_ROOT)
    parser.add_argument(
        "--cases",
        nargs="+",
        default=[f"S{index:02d}" for index in range(1, 11)],
    )
    parser.add_argument(
        "--private-reference-root",
        type=Path,
        default=None,
        help="Optional directory containing the five internal G1 engineering workbooks.",
    )
    args = parser.parse_args()
    if args.private_reference_root is not None:
        os.environ["WINDTOWER_PRIVATE_REFERENCE_ROOT"] = str(
            args.private_reference_root.resolve()
        )
    root = args.root.resolve()
    benchmark = generate_llm_benchmark(root)
    experiment = generate_experiment_datasets(
        root,
        case_ids=tuple(value.upper() for value in args.cases),
        include_internal_engineering=bool(
            os.getenv("WINDTOWER_PRIVATE_REFERENCE_ROOT", "").strip()
        ),
    )
    summary = {
        "schema_version": "experimental_design_preparation_summary_v1",
        "root": str(root),
        "benchmark_manifest": str(root / "benchmark_manifest.json"),
        "benchmark_case_count": benchmark["case_count"],
        "experiment_manifest": experiment["manifest"],
        "experiment_manifest_sha256": experiment["manifest_sha256"],
        "groups": {
            key: (
                len(value.get("cases") or [])
                if key != "G6"
                else len(((value.get("datacheck") or {}).get("cases") or []))
            )
            for key, value in (experiment.get("groups") or {}).items()
        },
        "declaration": experiment["declaration"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
