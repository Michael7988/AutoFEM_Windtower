from __future__ import annotations

import json

from windtower_backend.modeling.repository import ArtifactRepository
from windtower_backend.persistence import MetadataStore


def test_task_lease_prevents_duplicate_worker_claim(tmp_path) -> None:
    store_a = MetadataStore(tmp_path / "metadata.sqlite3")
    store_b = MetadataStore(tmp_path / "metadata.sqlite3")
    payload = {
        "submission_id": "sub-lease",
        "project_id": "wt-lease",
        "status": "queued",
        "stage": "queued",
        "tasks": {
            "static_strength": {
                "status": "queued",
                "stage": "queued",
                "attempt": 1,
            }
        },
    }
    store_a.record_submission(payload)
    assert store_a.claim_task("sub-lease", "static_strength", "worker-a", lease_seconds=60)
    assert not store_b.claim_task("sub-lease", "static_strength", "worker-b", lease_seconds=60)
    assert store_a.heartbeat_task("sub-lease", "static_strength", "worker-a", lease_seconds=60)
    store_a.release_task("sub-lease", "static_strength", "worker-a", "completed")


def test_artifact_index_is_reused_without_recursive_scan(tmp_path, monkeypatch) -> None:
    root = tmp_path / "wt-indexed"
    parameters = root / "parameters"
    parameters.mkdir(parents=True)
    (root / "ir.json").write_text("{}", encoding="utf-8")
    artifact = parameters / "parameter_set.json"
    artifact.write_text(json.dumps({"ok": True}), encoding="utf-8")
    repository = ArtifactRepository(MetadataStore(tmp_path / "metadata.sqlite3"))
    repository.replace(
        "wt-indexed",
        root,
        {"ir.json": root / "ir.json", "parameters/parameter_set.json": artifact},
    )

    def fail_rglob(*_args, **_kwargs):
        raise AssertionError("indexed project must not recurse through its directory")

    monkeypatch.setattr(type(root), "rglob", fail_rglob)
    loaded = repository.load("wt-indexed", root)
    assert loaded["parameters/parameter_set.json"] == artifact.resolve()


def test_unindexed_project_is_indexed_idempotently(tmp_path) -> None:
    root = tmp_path / "wt-unindexed"
    root.mkdir()
    (root / "ir.json").write_text("{}", encoding="utf-8")
    (root / "result.txt").write_text("ok", encoding="utf-8")
    repository = ArtifactRepository(MetadataStore(tmp_path / "metadata.sqlite3"))
    first = repository.load("wt-unindexed", root)
    second = repository.load("wt-unindexed", root)
    assert first == second
    assert (root / "artifact_index.json").is_file()
