from __future__ import annotations

import json

from core.windtower_backend.benchmark.framework_validation_v2 import (
    _evidence_content_hash,
    _evidence_id,
    _validate_evidence_record,
    _variant_evidence,
)


def _record():
    value = {
        "field": "geometry.height_mm",
        "value": 96700.0,
        "unit": "mm",
        "status": "observed",
        "locator": "S01!R3C3",
        "source_sha256": "a" * 64,
    }
    value["content_sha256"] = _evidence_content_hash(value)
    value["evidence_id"] = _evidence_id(value)
    return value


def test_evidence_record_is_deterministic_and_tamper_evident():
    first = _record()
    second = _record()
    assert first["evidence_id"] == second["evidence_id"]
    assert _validate_evidence_record(first) == []
    first["value"] = 96.7
    assert "content_hash_mismatch" in _validate_evidence_record(first)


def test_reordering_preserves_values_but_changes_order():
    source = {"sheets": [{"sheet_alias": "A", "cells": [{"cell": "A!R1C1", "value": 1}, {"cell": "A!R2C1", "value": 2}]}, {"sheet_alias": "B", "cells": [{"cell": "B!R1C1", "value": 3}]}]}
    result = _variant_evidence(source, "reordered")
    assert result["sheets"][0]["sheet_alias"] == "B"
    assert sorted(cell["value"] for sheet in result["sheets"] for cell in sheet["cells"]) == [1, 2, 3]
    assert source["sheets"][0]["sheet_alias"] == "A"


def test_unit_variant_scales_row_value_without_mutating_source():
    source = {"sheets": [{"sheet_alias": "A", "cells": [{"cell": "A!R1C3", "value": 96700.0}, {"cell": "A!R1C5", "value": "mm"}]}]}
    result = _variant_evidence(source, "equivalent_units")
    assert result["sheets"][0]["cells"][0]["value"] == 96.7
    assert result["sheets"][0]["cells"][1]["value"] == "m"
    assert source["sheets"][0]["cells"][0]["value"] == 96700.0


def test_conflict_variant_adds_explicit_source_reference():
    source = {"sheets": [{"sheet_alias": "A", "cells": [{"cell": "A!R1C1", "value": 10.0}]}]}
    result = _variant_evidence(source, "conflicting_duplicate")
    assert len(result["sheets"]) == 2
    assert "A!R1C1" in json.dumps(result["sheets"][1], ensure_ascii=False)

