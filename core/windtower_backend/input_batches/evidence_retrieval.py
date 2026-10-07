from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Callable, Iterable

from .models import EvidenceFragment


def select_relevant_evidence(
    evidence: Iterable[EvidenceFragment],
    target_keys: list[str],
    label_for_key: Callable[[str], str],
    index_path: Path,
    *,
    top_k_per_field: int = 5,
    max_fragments: int = 30,
    token_budget: int = 12000,
) -> list[EvidenceFragment]:
    rows = [item for item in evidence if item.text and not item.text.startswith("[") and item.status != "rejected"]
    if not rows:
        return []
    fts_scores = _fts_scores(rows, target_keys, label_for_key, index_path)
    selected: list[EvidenceFragment] = []
    selected_ids: set[str] = set()
    for key in target_keys:
        terms = _terms(key, label_for_key(key))
        ranked = sorted(
            rows,
            key=lambda item: (
                _rule_score(item, key, terms),
                fts_scores.get((key, item.evidence_id), 0.0),
                item.confidence,
                item.evidence_id,
            ),
            reverse=True,
        )
        for item in ranked[:top_k_per_field]:
            if item.evidence_id not in selected_ids:
                selected.append(item)
                selected_ids.add(item.evidence_id)
    if len(selected) < max_fragments:
        fallback = sorted(
            rows,
            key=lambda item: (
                max((_rule_score(item, key, _terms(key, label_for_key(key))) for key in target_keys), default=0),
                item.confidence,
                item.evidence_id,
            ),
            reverse=True,
        )
        for item in fallback:
            if item.evidence_id not in selected_ids:
                selected.append(item)
                selected_ids.add(item.evidence_id)
            if len(selected) >= max_fragments:
                break
    result: list[EvidenceFragment] = []
    used_tokens = 0
    for item in selected:
        estimated = max(1, len(json.dumps(item.model_dump(mode="json"), ensure_ascii=False, default=str)) // 4)
        if result and used_tokens + estimated > token_budget:
            continue
        result.append(item)
        used_tokens += estimated
        if len(result) >= max_fragments:
            break
    return result


def build_catalog(evidence: Iterable[EvidenceFragment], token_budget: int = 1500) -> str:
    groups: dict[tuple[str, str], dict[str, object]] = {}
    for item in evidence:
        title = " / ".join(item.location.title_path)
        key = (item.filename, title)
        group = groups.setdefault(key, {"filename": item.filename, "title": title, "count": 0, "kinds": set(), "fields": set()})
        group["count"] = int(group["count"]) + 1
        group["kinds"].add(item.kind)  # type: ignore[union-attr]
        if item.field:
            group["fields"].add(item.field)  # type: ignore[union-attr]
    lines = ["# Evidence catalog"]
    for group in groups.values():
        line = "- {filename} | {title} | {count} blocks | {kinds} | fields: {fields}".format(
            filename=group["filename"], title=group["title"] or "root", count=group["count"],
            kinds=",".join(sorted(group["kinds"])), fields=",".join(sorted(group["fields"])),
        )
        if (len("\n".join(lines)) + len(line)) // 4 > token_budget:
            break
        lines.append(line)
    return "\n".join(lines)


def _rule_score(item: EvidenceFragment, key: str, terms: set[str]) -> int:
    haystack = " ".join((
        item.field or "", item.label or "", item.text,
        item.location.json_path or "", item.location.cell_or_range or "",
        " ".join(item.location.title_path),
    )).lower()
    score = 1000 if item.field == key else 0
    score += 100 if item.file_id == "modeling-goal" else 0
    score += 15 * sum(1 for term in terms if term and term in haystack)
    score += 3 if item.kind in {"structured", "table", "table_cell"} else 0
    score += 2 if item.status == "verified" else 0
    return score


def _terms(key: str, label: str) -> set[str]:
    values = {key.lower(), key.rsplit(".", 1)[-1].lower(), label.lower()}
    values.update(token.lower() for token in re.split(r"[._\-\s]+", key) if len(token) > 1)
    return {value for value in values if value}


def _fts_scores(
    rows: list[EvidenceFragment],
    target_keys: list[str],
    label_for_key: Callable[[str], str],
    index_path: Path,
) -> dict[tuple[str, str], float]:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    scores: dict[tuple[str, str], float] = {}
    try:
        connection = sqlite3.connect(str(index_path))
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(evidence_id UNINDEXED, content)")
        connection.execute("DELETE FROM evidence_fts")
        connection.executemany(
            "INSERT INTO evidence_fts(evidence_id, content) VALUES (?, ?)",
            [
                (
                    item.evidence_id,
                    " ".join((item.field or "", item.label or "", item.text, " ".join(item.location.title_path))),
                )
                for item in rows
            ],
        )
        connection.commit()
        for key in target_keys:
            tokens = _query_tokens(key + " " + label_for_key(key))
            if not tokens:
                continue
            query = " OR ".join(f'"{token}"' for token in tokens[:8])
            for evidence_id, rank in connection.execute(
                "SELECT evidence_id, bm25(evidence_fts) FROM evidence_fts WHERE evidence_fts MATCH ? LIMIT 30",
                (query,),
            ):
                scores[(key, str(evidence_id))] = max(0.0, -float(rank))
    except sqlite3.Error:
        return {}
    finally:
        try:
            connection.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
    return scores


def _query_tokens(value: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", value.lower())))
