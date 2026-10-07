from __future__ import annotations

from windtower_backend.app import create_app


def _call_get(path: str) -> dict:
    route = next(route for route in create_app().routes if getattr(route, "path", None) == path and "GET" in route.methods)
    return route.endpoint()


def test_desktop_health_exposes_versions_without_secrets() -> None:
    payload = _call_get("/api/desktop/health")
    assert payload["ok"] is True
    assert payload["protocol_version"] == "windtower_desktop_v1"
    assert payload["schema_version"] == "windtower_parameter_set_v2"
    assert "desktop_token" not in str(payload).lower()
    assert "qwen_api_key" not in str(payload).lower()
    assert payload["solver"]["active_count"] >= 0


def test_desktop_version_contract_is_explicit() -> None:
    assert _call_get("/api/desktop/version") == {
        "app": "1.0.0",
        "desktop_protocol": "windtower_desktop_v1",
        "parameter_ir": "windtower_parameter_set_v2",
        "generator": "wind_tower_s4r_adapter_v1",
        "validation": "three_layer_validation_v1",
    }
