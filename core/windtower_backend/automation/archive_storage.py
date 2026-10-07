"""Create verified, recoverable project archives; dry-run is the default."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import BACKUP_ROOT, STORAGE_ROOT
from ..modeling.repository import ArtifactRepository


PROJECT_PATTERN = re.compile(r"^wt-[A-Za-z0-9_-]+$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--archive", action="store_true", help="Create and verify the ZIP archive.")
    parser.add_argument("--delete-intermediate", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not PROJECT_PATTERN.fullmatch(args.project_id):
        raise SystemExit("Invalid project id.")
    project_root = (STORAGE_ROOT / args.project_id).resolve()
    storage_root = STORAGE_ROOT.resolve()
    if storage_root not in project_root.parents or not project_root.is_dir():
        raise SystemExit("Project does not exist under the configured storage root.")

    repository = ArtifactRepository()
    artifacts = repository.load(args.project_id, project_root)
    rows = [
        {
            "name": name,
            "path": str(path),
            "category": _category_from_index(project_root, name),
            "size_bytes": path.stat().st_size,
        }
        for name, path in sorted(artifacts.items())
        if path.is_file()
    ]
    plan = {
        "schema_version": "storage_archive_plan_v1",
        "project_id": args.project_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": not args.archive,
        "files": rows,
        "total_size_bytes": sum(item["size_bytes"] for item in rows),
    }
    if not args.archive:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    archive_path = BACKUP_ROOT / f"{args.project_id}.zip"
    temporary = archive_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr("archive_manifest.json", json.dumps(plan, ensure_ascii=False, indent=2))
        for item in rows:
            path = Path(item["path"])
            archive.write(path, path.relative_to(project_root).as_posix())
    temporary.replace(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise RuntimeError(f"Archive verification failed at {bad_member}.")
    result = {
        **plan,
        "dry_run": False,
        "archive_path": str(archive_path),
        "archive_sha256": _sha256(archive_path),
        "verified": True,
    }

    if args.delete_intermediate:
        if not args.confirm:
            raise SystemExit("Deletion requires both --delete-intermediate and --confirm.")
        removable = [
            Path(item["path"])
            for item in rows
            if item["category"] in {"intermediate", "cache"}
        ]
        for path in removable:
            resolved = path.resolve()
            if project_root not in resolved.parents or not resolved.is_file():
                raise RuntimeError(f"Refusing unsafe deletion target: {resolved}")
        for path in removable:
            path.unlink()
        result["deleted_files"] = len(removable)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _category_from_index(root: Path, name: str) -> str:
    index_path = root / "artifact_index.json"
    if not index_path.is_file():
        return "intermediate"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    return next(
        (
            item["category"]
            for item in payload.get("artifacts") or []
            if item.get("name") == name
        ),
        "intermediate",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
