from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from ..config import METADATA_DB
from ..persistence import MetadataStore


ARTIFACT_INDEX_NAME = "artifact_index.json"


class ArtifactRepository:
    def __init__(self, store: MetadataStore | None = None) -> None:
        self.store = store or MetadataStore(METADATA_DB)

    def load(self, project_id: str, root: Path) -> dict[str, Path]:
        rows = self.store.list_artifacts(project_id)
        if rows:
            return {
                item["name"]: Path(item["path"])
                for item in rows
                if Path(item["path"]).is_file()
            }
        manifest = root / ARTIFACT_INDEX_NAME
        if manifest.is_file():
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            artifacts = {
                item["name"]: root / item["relative_path"]
                for item in payload.get("artifacts") or []
                if (root / item["relative_path"]).is_file()
            }
            self.replace(project_id, root, artifacts)
            return artifacts
        artifacts = {
            path.relative_to(root).as_posix(): path
            for path in root.rglob("*")
            if path.is_file() and path.name != ARTIFACT_INDEX_NAME
        }
        self.replace(project_id, root, artifacts)
        return artifacts

    def replace(self, project_id: str, root: Path, artifacts: dict[str, Path]) -> None:
        rows = []
        for name, path in sorted(artifacts.items()):
            if not path.is_file():
                continue
            rows.append({
                "name": name.replace("\\", "/"),
                "path": str(path.resolve()),
                "relative_path": path.resolve().relative_to(root.resolve()).as_posix(),
                "category": artifact_category(name),
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            })
        self.store.upsert_project(project_id, root.resolve(), (root / "ir.json").resolve())
        self.store.replace_artifacts(project_id, rows)
        payload = {
            "schema_version": "artifact_index_v1",
            "project_id": project_id,
            "artifacts": [
                {
                    key: item[key]
                    for key in ("name", "relative_path", "category", "size_bytes", "sha256")
                }
                for item in rows
            ],
        }
        _atomic_json(root / ARTIFACT_INDEX_NAME, payload)


def artifact_category(name: str) -> str:
    normalized = name.replace("\\", "/").lower()
    if normalized.startswith(("source/", "input/")):
        return "source"
    if normalized.startswith(("parameters/", "facts/", "ir/")) or normalized in {
        "ir.json",
        "parameter_set.json",
    }:
        return "model"
    if normalized.startswith(("results/", "reports/")):
        return "final"
    if normalized.startswith(("cache/", ".cache/")):
        return "cache"
    return "intermediate"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
