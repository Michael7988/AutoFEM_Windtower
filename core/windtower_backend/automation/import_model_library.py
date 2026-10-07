from __future__ import annotations

import argparse
import json
from pathlib import Path

from windtower_backend.model_library import ModelLibraryService, VALIDATION_LEVELS


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import an engineer-reviewed ParameterSet and INP into the private model library."
    )
    parser.add_argument("parameter_set", type=Path)
    parser.add_argument("reference_inp", type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--operator", default="engineer")
    parser.add_argument(
        "--validation-level",
        choices=sorted(VALIDATION_LEVELS),
        default="engineer_reviewed",
    )
    args = parser.parse_args()
    result = ModelLibraryService().import_bundle(
        args.parameter_set,
        args.reference_inp,
        label=args.label,
        operator=args.operator,
        validation_level=args.validation_level,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
