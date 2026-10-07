from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..benchmark.e1_real_engineering import (
    generate_e1_dataset,
    render_e1_outputs,
    run_e1_experiment,
)
from ..config import ROOT


DEFAULT_OUTPUT = ROOT / "experiments" / "trusted_fem_e1"
DEFAULT_SOURCE = ROOT / "core" / "example"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Generate and run the frozen real-engineering E1 pre-experiment.",
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--force-generate", action="store_true")
    parser.add_argument("--mode", choices=("R0", "R1", "all"), default="all")
    parser.add_argument("--r0-repetitions", type=int, default=1)
    parser.add_argument("--r1-repetitions", type=int, default=5)
    parser.add_argument(
        "--run-llm",
        action="store_true",
        help="Explicitly permit de-identified evidence calls to the configured Qwen advisors.",
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=360.0)
    parser.add_argument("--case-ids", nargs="*", default=None)
    parser.add_argument("--runner-partition", default="single")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    manifest_path = args.output_root / "dataset_manifest.json"
    if args.generate or args.force_generate or not manifest_path.is_file():
        manifest = generate_e1_dataset(
            args.source_root,
            args.output_root,
            force=args.force_generate,
        )
        print(json.dumps({
            "dataset": str(manifest_path),
            "condition_count": manifest["condition_count"],
            "truth_status": manifest["truth_status"],
        }, ensure_ascii=False, indent=2))
    if args.render_only:
        summary = render_e1_outputs(args.output_root)
    else:
        summary = run_e1_experiment(
            args.output_root,
            mode=args.mode,
            r0_repetitions=args.r0_repetitions,
            r1_repetitions=args.r1_repetitions,
            run_llm=args.run_llm,
            resume=not args.no_resume,
            timeout_seconds=args.timeout_seconds,
            case_ids=args.case_ids,
            runner_partition=args.runner_partition,
        )
    print(json.dumps({
        "status": summary.get("status"),
        "run_count": summary.get("run_count"),
        "expected_run_count": summary.get("expected_run_count"),
        "truth_status": summary.get("truth_status"),
        "results": str(args.output_root / "results"),
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("status") == "pre_experiment_complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
