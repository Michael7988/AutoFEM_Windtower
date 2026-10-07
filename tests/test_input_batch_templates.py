from __future__ import annotations

from datetime import datetime, timezone

import pytest

from windtower_backend.config import load_settings
from windtower_backend.core.pipeline import WindTowerPipeline
from windtower_backend.engineering_review import (
    EngineeringReviewRequired,
    assert_compute_ready,
    confirm_changed_parameters,
)
from windtower_backend.input_batches.model_templates import ModelTemplateRegistry
from windtower_backend.input_batches.models import InputBatch, ParameterCandidate
from windtower_backend.input_batches.service import EngineeringInputBatchService


def _service() -> EngineeringInputBatchService:
    return EngineeringInputBatchService(WindTowerPipeline(load_settings()))


def _batch(**updates):
    now = datetime.now(timezone.utc).isoformat()
    values = {
        "batch_id": "batch-test-template",
        "modeling_goal": "建立风塔并进行静力和模态分析",
        "parse_mode": "deterministic",
        "provider": "deterministic",
        "model": "none",
        "selected_template_id": "wind_tower_quick_v1",
        "selected_template_version": "1.0.0",
        "created_at": now,
        "updated_at": now,
    }
    values.update(updates)
    return InputBatch(**values)


def _valid_values():
    return {
        "geometry.tower_sections": [{
            "z_bottom_mm": 0.0,
            "z_top_mm": 10000.0,
            "diameter_bottom_mm": 5000.0,
            "diameter_top_mm": 4000.0,
            "thickness_mm": 20.0,
        }],
        "geometry.door.enabled": False,
        "material.name": "Q345",
        "material.elastic_modulus_mpa": 206000.0,
        "material.poisson_ratio": 0.3,
        "material.density": 7.85e-9,
        "material.yield_strength_mpa": 345.0,
        "mesh.global_size_mm": 650.0,
        "loads.top_mass_tonne": 100.0,
        "loads.top_horizontal_force_n": 800000.0,
        "loads.top_force_direction": "+X",
        "loads.wind_pressure_mpa": 0.0012,
        "analysis.static_enabled": True,
        "analysis.modal_enabled": True,
        "analysis.modal_modes": 12,
        "boundary.name": "BASE_FIXED",
    }


def test_template_registry_has_versioned_quick_and_detailed_prompts():
    registry = ModelTemplateRegistry()
    templates = {item["template_id"]: item for item in registry.list_public()["templates"]}
    assert set(templates) == {"wind_tower_quick_v1", "wind_tower_detailed_v1"}
    assert templates["wind_tower_quick_v1"]["prompt_hash"] != templates["wind_tower_detailed_v1"]["prompt_hash"]
    assert registry.recommend("建立更精细的局部风塔模型", [])["recommended_template_id"] == "wind_tower_detailed_v1"


def test_modeling_goal_becomes_traceable_static_and_modal_candidates():
    service = _service()
    batch = _batch()
    service._ensure_goal_evidence_and_candidates(batch)
    service._merge_candidates(batch)
    assert batch.resolved_parameters["analysis.static_enabled"] is True
    assert batch.resolved_parameters["analysis.modal_enabled"] is True
    assert {item.source_kind for item in batch.candidates} == {"goal_explicit"}
    assert all(item.evidence_ids for item in batch.candidates)


def test_llm_suggestion_allows_preview_but_requires_review_before_compute():
    service = _service()
    candidate = ParameterCandidate(
        candidate_id="cand-suggested",
        key="mesh.global_size_mm",
        label="整体网格尺寸",
        raw_value=650.0,
        normalized_value=650.0,
        normalized_unit="mm",
        source_file_id="llm-suggestion",
        extraction_method="llm_mapping",
        provider="qwen",
        model="qwen3.7-max",
        confidence=0.7,
        requires_confirmation=True,
        source_kind="llm_suggested",
        confirmation_status="pending",
        assumption_basis="用于整体模型预览的保守建议",
    )
    batch = _batch(resolved_parameters=_valid_values(), candidates=[candidate])
    service._build_readiness(batch)
    assert batch.completeness_report == {}
    assert batch.readiness["preview_allowed"] is True
    assert batch.readiness["compute_allowed"] is False
    assert "mesh.global_size_mm" in batch.readiness["pending_confirmation_keys"]
    assert batch.readiness["fem_status"] == "pending"

    ir = {
        "engineering_review": {
            "status": "pending",
            "pending_parameters": [{"key": "mesh.global_size_mm"}],
        },
        "editable_model_ir": {"provenance": {}},
    }
    with pytest.raises(EngineeringReviewRequired):
        assert_compute_ready(ir)
    confirm_changed_parameters(ir, ["mesh.global_size_mm"])
    assert_compute_ready(ir)
    assert ir["engineering_review"]["status"] == "confirmed"
