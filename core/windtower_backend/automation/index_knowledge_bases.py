from __future__ import annotations

import argparse
from pathlib import Path

from windtower_backend.knowledge import LocalKnowledgeBase


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a private local knowledge index.")
    parser.add_argument("domain", choices=["engineering", "fem"])
    parser.add_argument("sources", nargs="+", type=Path)
    args = parser.parse_args()
    result = LocalKnowledgeBase(args.domain).rebuild(args.sources)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
