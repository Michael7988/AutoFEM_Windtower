from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..benchmark.runner import run_llm_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evidence-gated LLM heterogeneous-input CAE benchmark.")
    parser.add_argument("--case", default="S01")
    parser.add_argument("--llm-mode", default="forced_parse", choices=("off", "adaptive", "forced_parse", "forced_review"))
    parser.add_argument("--analysis", nargs="+", default=("static_strength", "modal"), choices=("static_strength", "modal"))
    parser.add_argument("--run-abaqus", action="store_true")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--attempt-id", default=None)
    args = parser.parse_args()
    result = run_llm_benchmark(
        case_id=args.case,
        llm_mode=args.llm_mode,
        analyses=tuple(args.analysis),
        run_abaqus=args.run_abaqus,
        root=args.root,
        attempt_id=args.attempt_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
