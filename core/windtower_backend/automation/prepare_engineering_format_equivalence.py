from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..benchmark.engineering_format_equivalence import (
    DEFAULT_PRIVATE_DIRECTORY,
    generate_engineering_format_dataset,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the private G3-E engineering format-equivalence dataset."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "example",
    )
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--expected-cases", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-reference-compile", action="store_true")
    args = parser.parse_args()
    output_root = args.output_root or args.source_root / DEFAULT_PRIVATE_DIRECTORY
    manifest = generate_engineering_format_dataset(
        args.source_root,
        output_root,
        expected_cases=args.expected_cases,
        force=args.force,
        compile_reference_models=not args.skip_reference_compile,
    )
    print(
        json.dumps(
            {
                "output_root": str(output_root.resolve()),
                "case_count": manifest["case_count"],
                "condition_count": manifest["condition_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
