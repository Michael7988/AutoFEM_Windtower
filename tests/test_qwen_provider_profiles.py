from __future__ import annotations

import pytest

from windtower_backend.config import _profile_for_model, _resolve_qwen_connection


def test_auto_profile_routes_model_families() -> None:
    assert _profile_for_model("qwen3.8-max") == "official"
    assert _profile_for_model("deepseek-v4-pro") == "deepseek_official"
    assert _profile_for_model("gpt-5.6-sol") == "n1n"
    assert _profile_for_model("claude-fable-5-1") == "n1n"


def test_named_qwen_profiles_keep_credentials_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "legacy-relay-key")
    monkeypatch.setenv("QWEN_OFFICIAL_API_KEY", "official-key")
    monkeypatch.setenv("QWEN_PROVIDER_PROFILE", "official")
    profile, key, base_url = _resolve_qwen_connection(model="qwen3.8-max", desktop_config={}, qwen_config={})
    assert (profile, key) == ("official", "official-key")
    assert base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    monkeypatch.setenv("QWEN_PROVIDER_PROFILE", "n1n")
    profile, key, base_url = _resolve_qwen_connection(model="qwen3.8-max", desktop_config={}, qwen_config={})
    assert (profile, key) == ("n1n", "legacy-relay-key")
    assert base_url == "https://api.n1n.ai/v1"


def test_unknown_qwen_profile_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_PROVIDER_PROFILE", "unknown")
    with pytest.raises(ValueError, match="auto, legacy, official"):
        _resolve_qwen_connection(model="qwen3.8-max", desktop_config={}, qwen_config={})
