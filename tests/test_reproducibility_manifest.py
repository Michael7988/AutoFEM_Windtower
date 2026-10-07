from __future__ import annotations

from dataclasses import replace

from windtower_backend.config import load_settings
from windtower_backend.reproducibility import build_environment_manifest


def test_environment_v2_is_additive_and_secret_free() -> None:
    settings = replace(load_settings(), qwen_api_key="must-not-appear")
    payload = build_environment_manifest(settings)
    assert payload["schema_version"] == "experiment_environment_v2"
    assert payload["python"]
    assert payload["platform"]
    assert payload["secrets_recorded"] is False
    assert payload["implementation_sha256"]
    assert payload["dependencies_sha256"]
    assert payload["configuration_hashes"]
    assert "must-not-appear" not in str(payload)
