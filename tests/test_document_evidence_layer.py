from __future__ import annotations

import io
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.windtower_backend.build_context import BuildContext
from core.windtower_backend.api.input_batches import create_input_batches_router
from core.windtower_backend.input_batches.docling_client import (
    DoclingArtifactError,
    read_conversion_archive,
)
from core.windtower_backend.input_batches.document_evidence import (
    cross_validate_fragments,
    fragments_from_docling,
    render_document_markdown,
    stabilize_fragment,
    write_document_artifacts,
)
from core.windtower_backend.input_batches.evidence_retrieval import select_relevant_evidence
from core.windtower_backend.input_batches.models import BatchFile, EvidenceFragment, EvidenceLocation
from services.docling_worker.app import _prepare_docling_source


def _file() -> BatchFile:
    return BatchFile(
        file_id="file-1",
        filename="tower.xlsx",
        suffix=".xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size=4,
        sha256=hashlib.sha256(b"test").hexdigest(),
        processing_method="直接读取",
        cost_level="no_ocr",
    )


def _fragment(value: float = 2300) -> EvidenceFragment:
    return EvidenceFragment(
        evidence_id="temporary",
        file_id="file-1",
        filename="tower.xlsx",
        kind="table_cell",
        text=f"门洞高度 {value} mm",
        location=EvidenceLocation(sheet="Door", cell_or_range="E4", title_path=["门框参数"]),
        extraction_method="native_excel",
        confidence=1.0,
        content_hash="b" * 64,
        field="geometry.door.height_mm",
        label="门洞高度",
        raw_value=value,
        raw_unit="mm",
        value=value,
        unit="mm",
    )


def test_evidence_id_is_stable_and_location_sensitive() -> None:
    first = stabilize_fragment(_fragment(), "a" * 64)
    second = stabilize_fragment(_fragment(), "a" * 64)
    changed = _fragment()
    changed.location.cell_or_range = "E5"
    third = stabilize_fragment(changed, "a" * 64)
    assert first.evidence_id == second.evidence_id
    assert first.evidence_id.startswith("ev-")
    assert len(first.evidence_id) == 27
    assert third.evidence_id != first.evidence_id


def test_docling_blocks_keep_page_bbox_and_stable_id() -> None:
    document = {
        "texts": [{
            "self_ref": "#/texts/0",
            "label": "section_header",
            "text": "塔筒几何参数",
            "prov": [{"page_no": 2, "bbox": {"l": 10, "t": 20, "r": 110, "b": 40}}],
        }],
        "tables": [{
            "self_ref": "#/tables/0",
            "data": {"table_cells": [{"text": "门洞高度"}, {"text": "2300 mm"}]},
            "prov": [{"page_no": 3, "bbox": [1, 2, 3, 4]}],
        }],
    }
    first = fragments_from_docling(_file(), document, "")
    second = fragments_from_docling(_file(), document, "")
    assert [item.evidence_id for item in first] == [item.evidence_id for item in second]
    assert first[0].kind == "heading"
    assert first[0].location.page == 2
    assert first[0].location.bbox == [10.0, 20.0, 110.0, 40.0]
    assert first[1].kind == "table"
    assert "2300 mm" in first[1].text


def test_cross_validation_preserves_disagreement_as_conflict() -> None:
    native = stabilize_fragment(_fragment(2300), "a" * 64)
    docling = _fragment(2301)
    docling.extraction_method = "docling"
    docling.extraction_methods = ["docling"]
    docling.content_hash = "c" * 64
    docling = stabilize_fragment(docling, "a" * 64)
    checked = cross_validate_fragments([native, docling])
    assert [item.status for item in checked] == ["conflict", "conflict"]
    assert {item.value for item in checked} == {2300, 2301}


def test_docling_worker_requires_libreoffice_for_legacy_xls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.xls"
    source.write_bytes(b"legacy")
    monkeypatch.setattr("services.docling_worker.app.shutil.which", lambda _name: None)
    with pytest.raises(RuntimeError, match="LibreOffice"):
        _prepare_docling_source(source, tmp_path)


def test_docling_worker_keeps_supported_source_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"modern")
    prepared, metadata = _prepare_docling_source(source, tmp_path)
    assert prepared == source
    assert metadata == {"used": False}


def test_markdown_and_evidence_json_cross_reference(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"test")
    file = _file()
    fragment = stabilize_fragment(_fragment(), file.sha256)
    manifest = write_document_artifacts(
        tmp_path / "document",
        file,
        [fragment],
        {"name": "docling"},
        "# Docling view\n\n门洞高度 2300 mm",
        "converted",
        {"docling_version": "test", "warnings": []},
        source,
    )
    markdown = (tmp_path / "document" / "document.md").read_text(encoding="utf-8")
    evidence = json.loads((tmp_path / "document" / "evidence.json").read_text(encoding="utf-8"))
    assert fragment.evidence_id in markdown
    assert evidence["schema_version"] == "document_evidence_v1"
    assert evidence["evidence"][0]["evidence_id"] == fragment.evidence_id
    assert evidence["evidence"][0]["source"]["source_sha256"] == file.sha256
    assert manifest["artifacts"]["markdown"]["sha256"]


def test_markdown_is_deterministic() -> None:
    file = _file()
    fragment = stabilize_fragment(_fragment(), file.sha256)
    assert render_document_markdown(file, [fragment], "text", "converted") == render_document_markdown(
        file, [fragment], "text", "converted"
    )


def _archive(name: str = "manifest.json", manifest_sha: str = "a" * 64) -> bytes:
    stream = io.BytesIO()
    manifest = {
        "schema_version": "docling_conversion_manifest_v1",
        "source_sha256": manifest_sha,
    }
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, json.dumps(manifest))
        archive.writestr("docling.json", "{}")
        archive.writestr("document.md", "# document")
    return stream.getvalue()


def test_docling_archive_contract() -> None:
    result = read_conversion_archive(_archive(), "a" * 64)
    assert result.markdown == "# document"
    assert result.document == {}


def test_docling_archive_rejects_path_traversal() -> None:
    with pytest.raises(DoclingArtifactError, match="Unsafe"):
        read_conversion_archive(_archive("../manifest.json"), "a" * 64)


def test_retrieval_prefers_exact_field_and_obeys_budget(tmp_path: Path) -> None:
    exact = stabilize_fragment(_fragment(), "a" * 64)
    other = _fragment(999)
    other.field = "mesh.global_size_mm"
    other.label = "全局网格尺寸"
    other = stabilize_fragment(other, "a" * 64)
    selected = select_relevant_evidence(
        [other, exact],
        ["geometry.door.height_mm"],
        lambda key: "门洞高度" if key.endswith("height_mm") else key,
        tmp_path / "fts.sqlite3",
        top_k_per_field=1,
        max_fragments=1,
        token_budget=12000,
    )
    assert [item.evidence_id for item in selected] == [exact.evidence_id]


def test_consumption_carries_evidence_ids() -> None:
    context = BuildContext(
        parameter_set={
            "revision": 1,
            "parameters": {
                "mesh.global_size_mm": {
                    "value": 650,
                    "sources": [{"evidence_ids": ["ev-123"]}],
                }
            },
        },
        known_keys={"mesh.global_size_mm"},
        required_keys={"mesh.global_size_mm"},
    )
    context.consume("mesh.global_size_mm", "MeshCompiler", "mesh")
    report = context.finalize()
    assert report["parameters"]["mesh.global_size_mm"]["evidence_ids"] == ["ev-123"]


def test_document_evidence_query_api_contract() -> None:
    class Service:
        def list_documents(self, batch_id: str):
            return {"schema_version": "document_collection_v1", "batch_id": batch_id, "documents": []}

        def get_document_markdown(self, batch_id: str, file_id: str):
            return f"# {batch_id}/{file_id}"

        def query_evidence(self, batch_id: str, **filters):
            return {"schema_version": "document_evidence_query_v1", "batch_id": batch_id, "filters": filters, "evidence": []}

    app = FastAPI()
    app.include_router(create_input_batches_router(Service()))  # type: ignore[arg-type]
    client = TestClient(app)
    assert client.get("/api/input-batches/b1/documents").status_code == 200
    markdown = client.get("/api/input-batches/b1/documents/f1/markdown")
    assert markdown.status_code == 200
    assert markdown.text == "# b1/f1"
    query = client.get("/api/input-batches/b1/evidence", params={"field": "mesh.global_size_mm"})
    assert query.status_code == 200
    assert query.json()["filters"]["field"] == "mesh.global_size_mm"
