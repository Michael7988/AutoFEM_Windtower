from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from windtower_backend.config import load_settings
from windtower_backend.core.pipeline import WindTowerPipeline
from windtower_backend.input_batches.models import (
    BatchFile,
    ConfirmFemSpecificationRequest,
    InputBatch,
    ParameterCompletion,
)
from windtower_backend.input_batches.service import EngineeringInputBatchService
from windtower_backend.reference_models import capability_fem_specification


def _service(tmp_path: Path) -> EngineeringInputBatchService:
    service = EngineeringInputBatchService(WindTowerPipeline(load_settings()))
    service.root = tmp_path / "batches"
    service.cache_root = service.root / "_cache"
    service.cache_root.mkdir(parents=True, exist_ok=True)
    service._batches.clear()
    return service


def _batch(batch_id: str, *, status: str = "ready") -> InputBatch:
    now = datetime.now(timezone.utc).isoformat()
    return InputBatch(
        batch_id=batch_id,
        parse_mode="deterministic",
        status=status,
        created_at=now,
        updated_at=now,
    )


def test_restart_persists_retryable_interruption_without_duplicate_warning(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    batch = _batch("batch-interrupted", status="processing")
    batch.files = [BatchFile(
        file_id="file-01",
        filename="tower.xlsx",
        suffix=".xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size=10,
        sha256="a" * 64,
        status="mapping",
        processing_method="spreadsheet",
        cost_level="no_ocr",
    )]
    path = service.root / batch.batch_id / "batch.json"
    path.parent.mkdir(parents=True)
    path.write_text(batch.model_dump_json(indent=2), encoding="utf-8")

    service._load_existing()
    recovered = service.get_batch(batch.batch_id)
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert recovered["status"] == "failed"
    assert recovered["files"][0]["status"] == "failed"
    assert recovered["files"][0]["error"]
    assert persisted["status"] == "failed"
    assert [item["code"] for item in recovered["diagnostics"]] == [
        "PROCESS_INTERRUPTED"
    ]

    service._batches.clear()
    service._load_existing()
    loaded_again = service.get_batch(batch.batch_id)
    assert len(loaded_again["warnings"]) == 1
    assert len(loaded_again["diagnostics"]) == 1


def test_goal_only_interruption_can_be_retried(tmp_path: Path) -> None:
    service = _service(tmp_path)
    batch = _batch("batch-goal-only", status="failed")
    batch.diagnostics = [{
        "code": "PROCESS_INTERRUPTED",
        "severity": "warning",
        "retryable": True,
        "message": "interrupted",
    }]
    service._batches[batch.batch_id] = batch
    submitted: list[tuple[Any, ...]] = []

    class _Executor:
        def submit(self, *args: Any) -> None:
            submitted.append(args)

    service._executor = _Executor()  # type: ignore[assignment]

    response = service.retry(batch.batch_id)

    assert response["status"] == "queued"
    assert submitted and submitted[0][1:] == (batch.batch_id, None)


def test_fem_confirmation_replay_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    batch = _batch("batch-fem-replay")
    batch.fem_specification = capability_fem_specification()
    for item in batch.fem_specification["parameters"].values():
        item["confirmation_status"] = "confirmed"
        item["requires_confirmation"] = False
    batch.fem_specification["status"] = "confirmed"
    batch.fem_review = {
        "status": "confirmed",
        "confirmed_at": batch.updated_at,
        "operator": "engineer",
    }
    batch.fem_review_revision = 3
    service._batches[batch.batch_id] = batch
    request = ConfirmFemSpecificationRequest(
        review_revision=3,
        parameters=[ParameterCompletion(key="mesh.global_size_mm", value=650.0)],
    )
    original_updated_at = batch.updated_at

    first = service.confirm_fem_specification(batch.batch_id, request)
    second = service.confirm_fem_specification(batch.batch_id, request)

    assert first == second
    assert second["updated_at"] == original_updated_at


def test_incompatible_reference_cannot_be_confirmed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    batch = _batch("batch-incompatible")
    batch.fem_specification = capability_fem_specification()
    batch.fem_specification["compatibility"] = "incompatible"
    batch.fem_review_revision = 2
    service._batches[batch.batch_id] = batch

    with pytest.raises(ValueError, match="不符合当前建模能力"):
        service.confirm_fem_specification(
            batch.batch_id,
            ConfirmFemSpecificationRequest(review_revision=2),
        )


def test_fem_confirmation_tolerates_unchanged_fixed_parameters(tmp_path: Path) -> None:
    service = _service(tmp_path)
    batch = _batch("batch-fixed-fem-echo")
    batch.fem_specification = capability_fem_specification()
    batch.fem_review_revision = 2
    service._batches[batch.batch_id] = batch

    reviewed = service.confirm_fem_specification(
        batch.batch_id,
        ConfirmFemSpecificationRequest(
            review_revision=2,
            parameters=[
                ParameterCompletion(
                    key="mesh.strategy",
                    value="V3_CONFORMING_DOOR_MESH",
                )
            ],
        ),
    )

    assert reviewed["fem_review"]["status"] == "confirmed"
    assert (
        reviewed["fem_specification"]["parameters"]["mesh.strategy"]["origin"]
        == "capability_default"
    )


def test_fem_confirmation_rejects_changed_fixed_parameters(tmp_path: Path) -> None:
    service = _service(tmp_path)
    batch = _batch("batch-fixed-fem-change")
    batch.fem_specification = capability_fem_specification()
    batch.fem_review_revision = 2
    service._batches[batch.batch_id] = batch

    with pytest.raises(ValueError, match="不允许修改的 FEM 参数：mesh.strategy"):
        service.confirm_fem_specification(
            batch.batch_id,
            ConfirmFemSpecificationRequest(
                review_revision=2,
                parameters=[
                    ParameterCompletion(key="mesh.strategy", value="another_strategy")
                ],
            ),
        )


def test_reference_replacement_is_atomic_and_delete_resets_fem_state(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    batch = _batch("batch-reference")
    service._batches[batch.batch_id] = batch
    reference_root = service.root / batch.batch_id / "reference"
    reference_root.mkdir(parents=True)
    old_reference = reference_root / "old.inp"
    old_reference.write_text("*Heading\n", encoding="utf-8")
    supported = b"""*Heading
*Part, name=TOWER
*Node
1, 0., 0., 0.
2, 1., 0., 0.
3, 1., 1., 0.
4, 0., 1., 0.
*Element, type=S4R
1, 1, 2, 3, 4
*Material, name=STEEL
*Elastic
206000., 0.3
*Shell Section, material=STEEL
30.
*Step, name=STATIC
*Static
*End Step
"""

    replaced = service.put_reference_model(
        batch.batch_id,
        "replacement.inp",
        supported,
    )

    references = list(reference_root.glob("*.inp"))
    assert len(references) == 1
    assert references[0].name == replaced["reference_model"]["stored_name"]
    assert not old_reference.exists()

    with pytest.raises(ValueError, match="不能为空"):
        service.put_reference_model(batch.batch_id, "invalid.inp", b"")
    assert references[0].read_bytes() == supported

    reset = service.delete_reference_model(batch.batch_id)
    assert list(reference_root.glob("*.inp")) == []
    assert reset["reference_model"] is None
    assert reset["fem_review"]["status"] == "pending"
    assert reset["capability_match"]["mode"] == "capability_default"


def test_confirmed_batch_replay_returns_existing_project(tmp_path: Path) -> None:
    service = _service(tmp_path)
    batch = _batch("batch-confirmed", status="confirmed")
    batch.project_id = "wt-existing"
    service._batches[batch.batch_id] = batch
    service.pipeline.project = lambda project_id: {  # type: ignore[method-assign]
        "project_id": project_id,
        "status": "ready",
    }

    response = service.confirm(batch.batch_id)

    assert response["project_id"] == "wt-existing"
    assert response["input_batch"]["batch_id"] == batch.batch_id
