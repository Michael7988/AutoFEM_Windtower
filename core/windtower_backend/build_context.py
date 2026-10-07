from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Iterable

from .capability_profile import (
    CAPABILITY_HASH_SHA256,
    CAPABILITY_ID,
    CAPABILITY_REVISION,
)


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_ids(record: dict[str, Any] | None) -> list[str]:
    result: list[str] = []
    for source in (record or {}).get("sources") or []:
        if not isinstance(source, dict):
            continue
        for value in source.get("evidence_ids") or []:
            evidence_id = str(value)
            if evidence_id and evidence_id not in result:
                result.append(evidence_id)
    return result


@dataclass
class BuildContext:
    """Runtime evidence for the formal parameter-to-artifact consumption closure."""

    parameter_set: dict[str, Any]
    known_keys: set[str]
    required_keys: set[str]
    records: dict[str, dict[str, Any]] = field(init=False)
    entries: dict[str, dict[str, Any]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        raw_records = self.parameter_set.get("parameters") or {}
        self.records = {
            str(key): dict(record)
            for key, record in raw_records.items()
            if isinstance(record, dict)
        }
        unknown = sorted(set(self.records) - self.known_keys)
        if unknown:
            raise ValueError("Unregistered parameters: " + ", ".join(unknown))

    @property
    def door_enabled(self) -> bool:
        return bool((self.records.get("geometry.door.enabled") or {}).get("value", False))

    @property
    def modal_enabled(self) -> bool:
        return bool((self.records.get("analysis.modal_enabled") or {}).get("value", False))

    def activity(self, key: str) -> str:
        if key in {
            "geometry.door.width_mm", "geometry.door.height_mm",
            "geometry.door.bottom_z_mm", "geometry.door.theta_center_deg",
            "mesh.door_size_mm", "mesh.patch_margin_u_mm",
            "mesh.patch_margin_z_mm", "mesh.hole_layers",
        } and not self.door_enabled:
            return "not_applicable"
        if key == "analysis.modal_modes" and not self.modal_enabled:
            return "not_applicable"
        return "active"

    def consume(
        self,
        key: str,
        consumer: str,
        stage: str,
        *,
        derived_artifacts: Iterable[str] = (),
    ) -> Any:
        if key not in self.known_keys:
            raise KeyError(f"Unregistered parameter: {key}")
        activity = self.activity(key)
        record = self.records.get(key)
        value = None if record is None else record.get("value")
        entry = self.entries.setdefault(
            key,
            {
                "activity": activity,
                "consumption_status": "not_applicable" if activity == "not_applicable" else "not_run",
                "consumers": [],
                "stages": [],
                "derived_artifacts": [],
                "value_sha256": _sha256(value),
                "artifact_sha256": {},
                "evidence_ids": _evidence_ids(record),
            },
        )
        if activity == "not_applicable":
            return value
        if consumer not in entry["consumers"]:
            entry["consumers"].append(consumer)
        if stage not in entry["stages"]:
            entry["stages"].append(stage)
        for artifact in derived_artifacts:
            if artifact not in entry["derived_artifacts"]:
                entry["derived_artifacts"].append(artifact)
        entry["consumption_status"] = "consumed" if value is not None else "not_run"
        return value

    def finalize(self, artifact_hashes: dict[str, str] | None = None) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        for key in sorted(self.known_keys):
            if key not in self.entries:
                activity = self.activity(key)
                value = (self.records.get(key) or {}).get("value")
                self.entries[key] = {
                    "activity": activity,
                    "consumption_status": "not_applicable" if activity == "not_applicable" else "not_run",
                    "consumers": [],
                    "stages": [],
                    "derived_artifacts": [],
                    "value_sha256": _sha256(value),
                    "artifact_sha256": {},
                    "evidence_ids": _evidence_ids(self.records.get(key)),
                }
            entry = self.entries[key]
            if (
                entry["activity"] == "active"
                and key in self.required_keys
                and entry["consumption_status"] != "consumed"
            ):
                errors.append(
                    {
                        "field": key,
                        "code": "ACTIVE_PARAMETER_NOT_CONSUMED",
                        "message": f"Required active parameter '{key}' was not consumed by L0.",
                    }
                )
            for artifact in entry["derived_artifacts"]:
                digest = (artifact_hashes or {}).get(artifact)
                if digest:
                    entry["artifact_sha256"][artifact] = digest
        active_values = {
            key: (self.records.get(key) or {}).get("value")
            for key, entry in self.entries.items()
            if entry["activity"] == "active" and (self.records.get(key) or {}).get("value") is not None
        }
        return {
            "schema_version": "parameter_consumption_v1",
            "capability_id": CAPABILITY_ID,
            "capability_revision": CAPABILITY_REVISION,
            "capability_hash_sha256": CAPABILITY_HASH_SHA256,
            "parameter_revision": self.parameter_set.get("revision", 1),
            "semantic_hash": _sha256(active_values),
            "status": "blocked" if errors else "pass",
            "errors": errors,
            "parameters": {key: self.entries[key] for key in sorted(self.entries)},
        }


def legacy_consumption_report() -> dict[str, Any]:
    return {
        "schema_version": "parameter_consumption_v1",
        "capability_id": CAPABILITY_ID,
        "capability_revision": CAPABILITY_REVISION,
        "capability_hash_sha256": CAPABILITY_HASH_SHA256,
        "parameter_revision": None,
        "semantic_hash": None,
        "status": "legacy_compatibility",
        "errors": [],
        "warnings": [
            {
                "code": "legacy_default_applied",
                "message": "Direct L0 input has no formal ParameterSet; capability defaults were applied for backward compatibility.",
            }
        ],
        "parameters": {},
    }
