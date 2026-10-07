from __future__ import annotations

import json
from pathlib import Path

from windtower_backend.config import EXAMPLE_ROOT

from windtower_backend.importers.standard_input import normalize_import_to_standard_input, profile_workbook, remember_input_mapping, validate_standard_input


ROOT = Path(__file__).resolve().parents[1]
TEN_MW = EXAMPLE_ROOT / "synthetic_standard_tower.xlsx"


def test_confirmed_mapping_is_validated_and_recorded_in_alias_registry(tmp_path: Path) -> None:
    normalized = normalize_import_to_standard_input(TEN_MW)
    package = normalized["standard_input_package"]
    profile = profile_workbook(TEN_MW)
    registry_path = tmp_path / "format_alias_registry.json"

    validation = validate_standard_input({"standard_input_package": package, "fem_params": normalized["fem_params"]})
    recorded = remember_input_mapping(profile, package, registry_path=registry_path)

    assert validation["blocked"] is False
    assert recorded["status"] == "pass"
    assert recorded["entry"]["input_format"] == "semantic_standard_workbook"
    assert recorded["entry"]["profile_hash"] == profile["profile_hash"]

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["schema_version"] == "format_alias_registry_v1"
    assert registry["formats"][0]["field_count"] >= 10


def test_blocked_confirmed_package_is_not_promotable() -> None:
    validation = validate_standard_input({"standard_input_package": {"tower_segments": [], "door_opening": {"enabled": {"value": True}}}})

    assert validation["blocked"] is True
    assert validation["requires_user_confirmation"] is True
    assert validation["status"] == "blocked"
