from __future__ import annotations

from datetime import datetime, timezone

import pytest

from windtower_backend.config import load_settings
from windtower_backend.core.pipeline import WindTowerPipeline
from windtower_backend.input_batches.models import (
    ConfirmBatchReviewRequest,
    InputBatch,
    ParameterCandidate,
    ParameterCompletion,
)
from windtower_backend.input_batches.service import EngineeringInputBatchService
from windtower_backend.parameter_normalization import normalize_parameter_value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1, "+X"), (1.0, "+X"), ("DOF1", "+X"), ("正X", "+X"),
        (-1, "-X"), ("X负向", "-X"),
        (2, "+Y"), ("U2", "+Y"), ("Y正向", "+Y"),
        (-2, "-Y"), ("负Y", "-Y"),
    ],
)
def test_top_force_direction_aliases_are_canonical(raw, expected) -> None:
    value, conversion = normalize_parameter_value(
        "loads.top_force_direction", raw
    )
    assert value == expected
    if str(raw) != expected:
        assert conversion


def test_unknown_top_force_direction_requires_explicit_selection() -> None:
    with pytest.raises(ValueError, match="请选择"):
        normalize_parameter_value("loads.top_force_direction", "东北方向")


def test_existing_batch_direction_is_upgraded_without_losing_raw_evidence() -> None:
    service = EngineeringInputBatchService(WindTowerPipeline(load_settings()))
    now = datetime.now(timezone.utc).isoformat()
    candidate = ParameterCandidate(
        candidate_id="cand-direction",
        key="loads.top_force_direction",
        label="顶部力方向",
        raw_value=1,
        normalized_value=1,
        source_file_id="file-1",
        extraction_method="deterministic_excel",
    )
    batch = InputBatch(
        batch_id="batch-direction-upgrade",
        files=[],
        candidates=[candidate],
        resolved_parameters={"loads.top_force_direction": 1},
        created_at=now,
        updated_at=now,
    )
    service._upgrade_parameter_values(batch)

    assert batch.resolved_parameters["loads.top_force_direction"] == "+X"
    assert batch.candidates[0].raw_value == 1
    assert batch.candidates[0].normalized_value == "+X"
    assert batch.candidates[0].conversion == "1 → +X"


def test_final_review_rejects_stale_hash_before_applying_values() -> None:
    service = EngineeringInputBatchService(WindTowerPipeline(load_settings()))
    now = datetime.now(timezone.utc).isoformat()
    batch = InputBatch(
        batch_id="batch-stale-review",
        review_revision=3,
        review_parameters=[{
            "key": "loads.top_force_direction",
            "label": "顶部力方向",
            "group": "loads",
            "value": "+X",
            "unit": None,
            "control": "select",
            "options": ["+X", "-X", "+Y", "-Y"],
            "source_kind": "file_observed",
            "confidence": 1,
            "attention": False,
        }],
        created_at=now,
        updated_at=now,
    )
    request = ConfirmBatchReviewRequest(
        review_revision=2,
        parameters=[
            ParameterCompletion(key="loads.top_force_direction", value="+X")
        ],
    )
    with pytest.raises(ValueError, match="已经更新"):
        service._apply_final_review(batch, request)
