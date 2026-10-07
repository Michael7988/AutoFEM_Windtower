from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1


class MetadataStore:
    """Small SQLite control plane; large engineering artifacts remain files."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def upsert_project(self, project_id: str, root: Path, ir_path: Path) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO projects(project_id, root_path, ir_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    root_path=excluded.root_path,
                    ir_path=excluded.ir_path,
                    updated_at=excluded.updated_at
                """,
                (project_id, str(root), str(ir_path), now, now),
            )

    def replace_artifacts(
        self,
        project_id: str,
        artifacts: Iterable[dict[str, Any]],
    ) -> None:
        rows = list(artifacts)
        with self._connect() as db:
            db.execute("DELETE FROM artifacts WHERE project_id=?", (project_id,))
            db.executemany(
                """
                INSERT INTO artifacts(
                    project_id, name, path, category, size_bytes, sha256, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        item["name"],
                        item["path"],
                        item["category"],
                        int(item["size_bytes"]),
                        item.get("sha256"),
                        time.time(),
                    )
                    for item in rows
                ],
            )

    def list_artifacts(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT name, path, category, size_bytes, sha256
                FROM artifacts WHERE project_id=? ORDER BY name
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_input_batch(self, batch_id: str, status: str, payload: str) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO input_batches(batch_id, status, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    status=excluded.status,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (batch_id, status, payload, now, now),
            )

    def record_submission(self, payload: dict[str, Any]) -> None:
        now = time.time()
        submission_id = str(payload["submission_id"])
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO submissions(
                    submission_id, project_id, status, stage, idempotency_key,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(submission_id) DO UPDATE SET
                    status=excluded.status,
                    stage=excluded.stage,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    submission_id,
                    str(payload.get("project_id") or ""),
                    str(payload.get("status") or ""),
                    str(payload.get("stage") or ""),
                    str(payload.get("idempotency_key") or ""),
                    json.dumps(payload, ensure_ascii=False),
                    float(payload.get("created_at") or now),
                    now,
                ),
            )
            for job_type, task in (payload.get("tasks") or {}).items():
                db.execute(
                    """
                    INSERT INTO solver_tasks(
                        submission_id, job_type, status, stage, attempt,
                        payload_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(submission_id, job_type) DO UPDATE SET
                        status=excluded.status,
                        stage=excluded.stage,
                        attempt=excluded.attempt,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        submission_id,
                        job_type,
                        str(task.get("status") or ""),
                        str(task.get("stage") or ""),
                        int(task.get("attempt") or 1),
                        json.dumps(task, ensure_ascii=False),
                        now,
                    ),
                )

    def get_submission(
        self,
        submission_id: str,
        project_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT payload_json FROM submissions
                WHERE submission_id=? AND project_id=?
                """,
                (submission_id, project_id),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def claim_task(
        self,
        submission_id: str,
        job_type: str,
        owner: str,
        *,
        lease_seconds: int,
    ) -> bool:
        now = time.time()
        expires = now + lease_seconds
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT lease_owner, lease_expires_at
                FROM solver_tasks WHERE submission_id=? AND job_type=?
                """,
                (submission_id, job_type),
            ).fetchone()
            if row is None:
                return False
            current_owner = str(row["lease_owner"] or "")
            current_expiry = float(row["lease_expires_at"] or 0)
            if current_owner and current_owner != owner and current_expiry > now:
                return False
            db.execute(
                """
                UPDATE solver_tasks
                SET lease_owner=?, lease_expires_at=?, heartbeat_at=?, updated_at=?
                WHERE submission_id=? AND job_type=?
                """,
                (owner, expires, now, now, submission_id, job_type),
            )
            db.execute(
                """
                INSERT INTO state_transitions(
                    submission_id, job_type, from_state, to_state, recorded_at, detail
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (submission_id, job_type, "queued", "claimed", now, owner),
            )
        return True

    def heartbeat_task(
        self,
        submission_id: str,
        job_type: str,
        owner: str,
        *,
        lease_seconds: int,
    ) -> bool:
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE solver_tasks
                SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                WHERE submission_id=? AND job_type=? AND lease_owner=?
                """,
                (now, now + lease_seconds, now, submission_id, job_type, owner),
            )
        return cursor.rowcount == 1

    def release_task(
        self,
        submission_id: str,
        job_type: str,
        owner: str,
        final_state: str,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                UPDATE solver_tasks
                SET lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=?,
                    status=?, updated_at=?
                WHERE submission_id=? AND job_type=? AND lease_owner=?
                """,
                (now, final_state, now, submission_id, job_type, owner),
            )

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info(
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects(
                    project_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    ir_path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts(
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    category TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(project_id, name)
                );
                CREATE TABLE IF NOT EXISTS input_batches(
                    batch_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions(
                    submission_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    idempotency_key TEXT,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS solver_tasks(
                    submission_id TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    heartbeat_at REAL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(submission_id, job_type)
                );
                CREATE TABLE IF NOT EXISTS state_transitions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id TEXT NOT NULL,
                    job_type TEXT,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS work_leases(
                    resource TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL
                );
                """
            )
            # Older development databases briefly used a global unique index.
            # Project ids can be reused by isolated test/data roots, so durable
            # idempotency is scoped by the submission snapshots and task lease.
            db.execute("DROP INDEX IF EXISTS submission_idempotency")
            row = db.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            if row is None:
                db.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db
