from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import KNOWLEDGE_ROOT


KNOWLEDGE_DOMAINS = {"engineering", "fem"}


class LocalKnowledgeBase:
    def __init__(self, domain: str, root: Path | None = None) -> None:
        if domain not in KNOWLEDGE_DOMAINS:
            raise ValueError(f"Unsupported knowledge domain: {domain}")
        self.domain = domain
        self.root = Path(root or KNOWLEDGE_ROOT)
        self.path = self.root / f"{domain}.sqlite3"

    def status(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "domain": self.domain,
                "status": "not_indexed",
                "document_count": 0,
                "chunk_count": 0,
            }
        try:
            with self._connect(read_only=True) as db:
                documents = int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
                chunks = int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        except sqlite3.Error as exc:
            return {
                "domain": self.domain,
                "status": "unavailable",
                "document_count": 0,
                "chunk_count": 0,
                "error": str(exc),
            }
        return {
            "domain": self.domain,
            "status": "ready" if chunks else "empty",
            "document_count": documents,
            "chunk_count": chunks,
        }

    def rebuild(self, sources: Iterable[Path]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        with self._connect() as db:
            self._initialize(db)
            for source in sorted({Path(item).resolve() for item in sources}):
                for path in _markdown_files(source):
                    body = path.read_text(encoding="utf-8", errors="replace")
                    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
                    document_id = f"doc-{digest[:16]}"
                    db.execute(
                        "INSERT INTO documents(document_id, title, source_name, sha256) VALUES (?, ?, ?, ?)",
                        (document_id, path.stem, path.name, digest),
                    )
                    for index, chunk in enumerate(_markdown_chunks(body), start=1):
                        chunk_id = f"{document_id}-c{index:04d}"
                        heading = chunk["heading"]
                        text = chunk["text"]
                        db.execute(
                            "INSERT INTO chunks(chunk_id, document_id, heading, body) VALUES (?, ?, ?, ?)",
                            (chunk_id, document_id, heading, text),
                        )
                        db.execute(
                            "INSERT INTO chunk_search(chunk_id, search_text) VALUES (?, ?)",
                            (chunk_id, _search_text(f"{heading} {text}")),
                        )
        return self.status()

    def query(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        if not query.strip() or self.status()["status"] != "ready":
            return []
        tokens = _search_tokens(query)
        if not tokens:
            return []
        expression = " OR ".join(f'"{token}"' for token in tokens[:24])
        try:
            with self._connect(read_only=True) as db:
                rows = db.execute(
                    """
                    SELECT c.chunk_id, c.heading, c.body, d.title, d.source_name,
                           d.sha256, bm25(chunk_search) AS score
                    FROM chunk_search
                    JOIN chunks c ON c.chunk_id=chunk_search.chunk_id
                    JOIN documents d ON d.document_id=c.document_id
                    WHERE chunk_search MATCH ?
                    ORDER BY score, c.chunk_id
                    LIMIT ?
                    """,
                    (expression, max(1, min(limit, 20))),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "reference_id": str(row["chunk_id"]),
                "domain": self.domain,
                "title": str(row["title"]),
                "section": str(row["heading"] or "正文"),
                "source_name": str(row["source_name"]),
                "sha256": str(row["sha256"]),
                "text": str(row["body"])[:1800],
                "score": float(row["score"]),
            }
            for row in rows
        ]

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        else:
            db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _initialize(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE documents(
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_name TEXT NOT NULL,
                sha256 TEXT NOT NULL
            );
            CREATE TABLE chunks(
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                heading TEXT,
                body TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(document_id)
            );
            CREATE VIRTUAL TABLE chunk_search USING fts5(
                chunk_id UNINDEXED,
                search_text,
                tokenize='unicode61'
            );
            """
        )


class KnowledgeBaseManager:
    def __init__(self, root: Path | None = None) -> None:
        self.engineering = LocalKnowledgeBase("engineering", root)
        self.fem = LocalKnowledgeBase("fem", root)

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": "dual_knowledge_base_status_v1",
            "engineering": self.engineering.status(),
            "fem": self.fem.status(),
        }


def _markdown_files(source: Path) -> list[Path]:
    if source.is_file() and source.suffix.lower() in {".md", ".txt"}:
        return [source]
    if source.is_dir():
        return sorted(
            path
            for path in source.rglob("*")
            if path.is_file() and path.suffix.lower() in {".md", ".txt"}
        )
    return []


def _markdown_chunks(text: str, max_chars: int = 1800) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    heading = "正文"
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        while body:
            chunks.append({"heading": heading, "text": body[:max_chars]})
            body = body[max_chars:].lstrip()
        buffer = []

    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            flush()
            heading = line.lstrip("# ").strip() or "正文"
            continue
        if not line.strip() and sum(len(item) for item in buffer) >= max_chars // 2:
            flush()
            continue
        buffer.append(line)
    flush()
    return [item for item in chunks if item["text"]]


def _search_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for item in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}|\d+(?:\.\d+)?|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", item) and len(item) > 2:
            tokens.extend(item[index:index + 2] for index in range(len(item) - 1))
        else:
            tokens.append(item)
    return list(dict.fromkeys(token for token in tokens if token.strip()))


def _search_text(text: str) -> str:
    return " ".join(_search_tokens(text))
