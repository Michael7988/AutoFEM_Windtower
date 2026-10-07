from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import BatchFile, EvidenceFragment, EvidenceLocation


DOCUMENT_EVIDENCE_SCHEMA = "document_evidence_v1"
DOCUMENT_MANIFEST_SCHEMA = "document_artifact_manifest_v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_evidence_id(
    source_sha256: str,
    location: EvidenceLocation | dict[str, Any],
    field: str | None,
    content_hash: str,
) -> str:
    locator_model = location if isinstance(location, EvidenceLocation) else EvidenceLocation.model_validate(location)
    locator = locator_model.model_dump(mode="json")
    payload = {
        "source_sha256": source_sha256,
        "location": locator,
        "field": field or "",
        "content_hash": content_hash,
    }
    return "ev-" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:24]


def stabilize_fragment(fragment: EvidenceFragment, source_sha256: str) -> EvidenceFragment:
    item = fragment.model_copy(deep=True)
    item.source_sha256 = source_sha256
    methods = list(dict.fromkeys([*(item.extraction_methods or []), item.extraction_method]))
    item.extraction_methods = methods
    item.evidence_id = stable_evidence_id(source_sha256, item.location, item.field, item.content_hash)
    return item


def reconcile_evidence(
    native: Iterable[EvidenceFragment],
    docling_markdown: str,
    source_sha256: str,
) -> list[EvidenceFragment]:
    """Stabilize native fragments and conservatively verify visible values.

    A value is verified only when the Docling view contains its normalized
    textual form. Absence is not treated as a conflict because layout parsers
    can legitimately omit formulas or hidden cells.
    """

    compact_md = _compact_text(docling_markdown)
    rows: list[EvidenceFragment] = []
    seen: set[str] = set()
    for original in native:
        item = stabilize_fragment(original, source_sha256)
        value = item.value if item.value is not None else item.raw_value
        if value is None and item.field:
            value = _decode_scalar(item.text)
        if value is not None and compact_md:
            alternatives = _value_tokens(value)
            if any(token and token in compact_md for token in alternatives):
                item.status = "verified"
                if "docling" not in item.extraction_methods:
                    item.extraction_methods.append("docling")
        if item.evidence_id not in seen:
            rows.append(item)
            seen.add(item.evidence_id)
    return rows


def cross_validate_fragments(rows: Iterable[EvidenceFragment]) -> list[EvidenceFragment]:
    """Cross-check records that identify the same field at the same location.

    Layout-only Docling blocks normally have no ``field`` and remain observed.
    When an adapter supplies field-level Docling records, agreement with a
    native parser promotes both records to verified; disagreement preserves
    both records and marks them conflict.
    """

    result = [item.model_copy(deep=True) for item in rows]
    groups: dict[tuple[str, str], list[EvidenceFragment]] = {}
    for item in result:
        if not item.field:
            continue
        key = (item.field, canonical_json(item.location.model_dump(mode="json")))
        groups.setdefault(key, []).append(item)
    for group in groups.values():
        methods = {method for item in group for method in (item.extraction_methods or [item.extraction_method])}
        if "docling" not in methods or len(methods) < 2:
            continue
        values = {_normalized_evidence_value(item) for item in group}
        if len(values) > 1:
            for item in group:
                item.status = "conflict"
        else:
            for item in group:
                if item.status not in {"rejected", "needs_review"}:
                    item.status = "verified"
    return result


def fragments_from_docling(
    file: BatchFile,
    document: dict[str, Any],
    markdown: str,
) -> list[EvidenceFragment]:
    """Convert Docling's common text/table representation into evidence blocks.

    The adapter is deliberately tolerant of minor Docling schema changes; the
    complete lossless document remains preserved in docling.json.
    """

    rows: list[EvidenceFragment] = []
    for index, item in enumerate(document.get("texts") or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("orig") or "").strip()
        if not text:
            continue
        label = str(item.get("label") or "text")
        kind = "heading" if label in {"title", "section_header", "heading"} else (
            "list_item" if label == "list_item" else ("formula" if label == "formula" else "text")
        )
        location = _docling_location(item, f"text-{index:05d}")
        rows.append(_docling_fragment(file, kind, text, location))
    for index, table in enumerate(document.get("tables") or []):
        if not isinstance(table, dict):
            continue
        cells = ((table.get("data") or {}).get("table_cells") or table.get("table_cells") or [])
        cell_texts = [str(cell.get("text") or "").strip() for cell in cells if isinstance(cell, dict)]
        text = " | ".join(value for value in cell_texts if value)
        if not text:
            text = str(table.get("text") or "").strip()
        if text:
            rows.append(_docling_fragment(file, "table", text, _docling_location(table, f"table-{index:05d}")))
    if rows or not markdown.strip():
        return _dedupe_fragments(rows)
    line = 1
    for index, chunk in enumerate(_markdown_chunks(markdown, 4000)):
        location = EvidenceLocation(line=line, block_id=f"markdown-{index:05d}")
        rows.append(_docling_fragment(file, "text", chunk, location))
        line += chunk.count("\n") + 1
    return _dedupe_fragments(rows)


def evidence_payload(file: BatchFile, rows: Iterable[EvidenceFragment]) -> dict[str, Any]:
    records = []
    for item in rows:
        record = item.model_dump(mode="json")
        record["source"] = {
            "file_id": file.file_id,
            "filename": file.filename,
            "source_sha256": file.sha256,
        }
        records.append(record)
    return {
        "schema_version": DOCUMENT_EVIDENCE_SCHEMA,
        "source": {
            "file_id": file.file_id,
            "filename": file.filename,
            "source_sha256": file.sha256,
            "media_type": file.media_type,
            "size": file.size,
        },
        "evidence": records,
    }


def render_document_markdown(
    file: BatchFile,
    evidence: Iterable[EvidenceFragment],
    docling_markdown: str = "",
    parser_status: str = "degraded_native_only",
) -> str:
    rows = list(evidence)
    headings = [item for item in rows if item.kind == "heading"]
    parameters = [item for item in rows if item.field]
    unresolved = [item for item in rows if item.status in {"needs_review", "conflict", "rejected"}]
    lines = [
        "---",
        f'schema_version: "{DOCUMENT_EVIDENCE_SCHEMA}"',
        f'file_id: "{_yaml(file.file_id)}"',
        f'filename: "{_yaml(file.filename)}"',
        f'source_sha256: "{file.sha256}"',
        f'parser_status: "{_yaml(parser_status)}"',
        f"evidence_count: {len(rows)}",
        "---",
        "",
        f"# {file.filename}",
        "",
        "## 目录",
        "",
        "- [文档说明](#文档说明)",
        "- [原始文档视图](#原始文档视图)",
        "- [参数与证据](#参数与证据)",
        "- [冲突与待确认项](#冲突与待确认项)",
        "- [来源位置附录](#来源位置附录)",
    ]
    for item in headings[:50]:
        title = item.text.strip().replace("\n", " ")
        if title:
            lines.append(f"  - {title}")
    lines.extend([
        "",
        "## 文档说明",
        "",
        f"- 文件：`{file.filename}`",
        f"- SHA-256：`{file.sha256}`",
        f"- 解析状态：`{parser_status}`",
        f"- Evidence 条目：{len(rows)}",
        "",
        "## 原始文档视图",
        "",
        docling_markdown.strip() or "_Docling视图不可用；以下证据来自格式专用解析器。_",
        "",
        "## 参数与证据",
        "",
        "| 字段 | 数值 | 单位 | 状态 | Evidence ID | 来源位置 |",
        "|---|---:|---|---|---|---|",
    ])
    for item in parameters:
        value = item.value if item.value is not None else item.raw_value
        unit = item.unit if item.unit is not None else item.raw_unit
        lines.append(
            "| {field} | {value} | {unit} | {status} | `{evidence}` | {locator} |".format(
                field=_cell(item.field or ""), value=_cell(value), unit=_cell(unit or ""),
                status=item.status, evidence=item.evidence_id, locator=_cell(format_location(item.location)),
            )
        )
    if not parameters:
        lines.append("| — | — | — | — | — | — |")
    lines.extend([
        "",
        "## 冲突与待确认项",
        "",
    ])
    if unresolved:
        for item in unresolved:
            lines.append(f"- `{item.evidence_id}` `{item.status}` {item.field or item.text[:120]}")
    else:
        lines.append("无。")
    lines.extend([
        "",
        "## 来源位置附录",
        "",
        "| Evidence ID | 类型 | 位置 | 提取方式 | 内容哈希 |",
        "|---|---|---|---|---|",
    ])
    for item in rows:
        lines.append(
            f"| `{item.evidence_id}` | {item.kind} | {_cell(format_location(item.location))} | "
            f"{_cell(', '.join(item.extraction_methods or [item.extraction_method]))} | `{item.content_hash}` |"
        )
    return "\n".join(lines).rstrip() + "\n"


def format_location(location: EvidenceLocation) -> str:
    values = []
    if location.title_path:
        values.append(" / ".join(location.title_path))
    for name, value in (
        ("sheet", location.sheet), ("cell", location.cell_or_range),
        ("page", location.page), ("slide", location.slide), ("line", location.line),
        ("json", location.json_path), ("block", location.block_id),
    ):
        if value not in (None, ""):
            values.append(f"{name}={value}")
    if location.bbox:
        values.append("bbox=" + ",".join(f"{value:g}" for value in location.bbox))
    return "; ".join(values) or "unknown"


def write_document_artifacts(
    document_root: Path,
    file: BatchFile,
    evidence: list[EvidenceFragment],
    docling_json: dict[str, Any],
    docling_markdown: str,
    parser_status: str,
    parser_meta: dict[str, Any],
    source_path: Path,
) -> dict[str, Any]:
    if sha256_file(source_path) != file.sha256:
        raise ValueError("Source SHA-256 changed before document artifacts were committed")
    document_root.mkdir(parents=True, exist_ok=True)
    assets = document_root / "assets"
    assets.mkdir(exist_ok=True)
    source_target = document_root / ("source" + Path(file.filename).suffix.lower())
    _atomic_bytes(source_target, source_path.read_bytes())
    docling_path = document_root / "docling.json"
    evidence_path = document_root / "evidence.json"
    markdown_path = document_root / "document.md"
    _atomic_text(docling_path, json.dumps(docling_json, ensure_ascii=False, indent=2, default=str) + "\n")
    _atomic_text(evidence_path, json.dumps(evidence_payload(file, evidence), ensure_ascii=False, indent=2, default=str) + "\n")
    markdown = render_document_markdown(file, evidence, docling_markdown, parser_status)
    _atomic_text(markdown_path, markdown)
    manifest = {
        "schema_version": DOCUMENT_MANIFEST_SCHEMA,
        "file_id": file.file_id,
        "filename": file.filename,
        "media_type": file.media_type,
        "size": file.size,
        "source_sha256": file.sha256,
        "parser_status": parser_status,
        "parser": parser_meta,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "warnings": list(parser_meta.get("warnings") or []),
        "artifacts": {
            "source": {"path": source_target.name, "sha256": sha256_file(source_target)},
            "docling_json": {"path": docling_path.name, "sha256": sha256_file(docling_path)},
            "markdown": {"path": markdown_path.name, "sha256": sha256_file(markdown_path)},
            "evidence": {"path": evidence_path.name, "sha256": sha256_file(evidence_path)},
        },
    }
    manifest_path = document_root / "manifest.json"
    _atomic_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n")
    manifest["manifest_sha256"] = sha256_file(manifest_path)
    return manifest


def _atomic_text(path: Path, value: str) -> None:
    _atomic_bytes(path, value.encode("utf-8"))


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _value_tokens(value: Any) -> list[str]:
    if isinstance(value, bool):
        return [str(value).lower(), "是" if value else "否"]
    if isinstance(value, (int, float)):
        number = float(value)
        tokens = [str(value), f"{number:g}"]
        if number.is_integer():
            tokens.append(str(int(number)))
        return list(dict.fromkeys(_compact_text(item) for item in tokens))
    return [_compact_text(str(value))]


def _decode_scalar(text: str) -> Any:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if not isinstance(value, (dict, list)) else None


def _normalized_evidence_value(item: EvidenceFragment) -> str:
    value = item.value if item.value is not None else item.raw_value
    if value is None:
        value = _decode_scalar(item.text)
    return canonical_json({"value": value, "unit": (item.unit or item.raw_unit or "").strip().lower()})


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _yaml(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _docling_location(item: dict[str, Any], block_id: str) -> EvidenceLocation:
    provenance = item.get("prov") or item.get("provenance") or []
    first = provenance[0] if isinstance(provenance, list) and provenance and isinstance(provenance[0], dict) else {}
    bbox_value = first.get("bbox") or item.get("bbox")
    if isinstance(bbox_value, dict):
        bbox = [bbox_value.get(key) for key in ("l", "t", "r", "b")]
        bbox = [float(value) for value in bbox if value is not None]
    elif isinstance(bbox_value, (list, tuple)):
        bbox = [float(value) for value in bbox_value]
    else:
        bbox = None
    page = first.get("page_no") or first.get("page") or item.get("page_no")
    return EvidenceLocation(
        page=int(page) if page not in (None, "") else None,
        bbox=bbox if bbox and len(bbox) == 4 else None,
        block_id=str(item.get("self_ref") or item.get("id") or block_id),
    )


def _docling_fragment(file: BatchFile, kind: str, text: str, location: EvidenceLocation) -> EvidenceFragment:
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    exploratory = file.support_tier == "exploratory"
    return EvidenceFragment(
        evidence_id=stable_evidence_id(file.sha256, location, None, content_hash),
        file_id=file.file_id,
        filename=file.filename,
        kind=kind,  # type: ignore[arg-type]
        text=text[:12000],
        location=location,
        extraction_method="docling",
        extraction_methods=["docling"],
        confidence=0.75 if exploratory else 1.0,
        requires_ocr=exploratory,
        from_ocr=exploratory,
        content_hash=content_hash,
        status="needs_review" if exploratory else "observed",
        source_sha256=file.sha256,
    )


def _markdown_chunks(markdown: str, size: int) -> list[str]:
    paragraphs = [value.strip() for value in re.split(r"\n\s*\n", markdown) if value.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > size:
            chunks.append(current)
            current = ""
        current = paragraph if not current else current + "\n\n" + paragraph
    if current:
        chunks.append(current)
    return chunks


def _dedupe_fragments(rows: list[EvidenceFragment]) -> list[EvidenceFragment]:
    result = []
    seen = set()
    for item in rows:
        marker = (item.content_hash, format_location(item.location))
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result
