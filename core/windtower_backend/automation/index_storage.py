"""Idempotently build SQLite/artifact indexes for historical projects."""

from __future__ import annotations

import argparse
import json

from ..config import STORAGE_ROOT
from ..modeling.repository import ArtifactRepository


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    repository = ArtifactRepository()
    projects = [
        path
        for path in sorted(STORAGE_ROOT.glob("wt-*"))
        if path.is_dir() and (path / "ir.json").is_file()
    ]
    if args.limit is not None:
        projects = projects[: max(0, args.limit)]
    results = []
    for root in projects:
        artifacts = repository.load(root.name, root)
        results.append({"project_id": root.name, "artifact_count": len(artifacts)})
    print(json.dumps({
        "schema_version": "storage_index_run_v1",
        "project_count": len(results),
        "projects": results,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
