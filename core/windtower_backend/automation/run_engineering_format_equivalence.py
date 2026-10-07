from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..benchmark.engineering_format_equivalence import (
    DEFAULT_PRIVATE_DIRECTORY,
    run_engineering_format_experiment,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic or explicitly enabled LLM G3-E parsing."
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=(
            Path(__file__).resolve().parents[2]
            / "example"
            / DEFAULT_PRIVATE_DIRECTORY
        ),
    )
    parser.add_argument("--run-llm", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args()
    result = run_engineering_format_experiment(
        args.dataset_root,
        run_llm=args.run_llm,
        repetitions=args.repetitions,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "experiment_id": result["experiment_id"],
                "mode": result["mode"],
                "run_count": result["run_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
