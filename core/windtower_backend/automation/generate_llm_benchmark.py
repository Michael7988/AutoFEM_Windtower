from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..benchmark import DEFAULT_BENCHMARK_ROOT, generate_llm_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate ten auditable synthetic heterogeneous wind-tower Excel benchmark cases.")
    parser.add_argument("--out", default=str(DEFAULT_BENCHMARK_ROOT))
    args = parser.parse_args()
    result = generate_llm_benchmark(Path(args.out))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
