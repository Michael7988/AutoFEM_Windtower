from __future__ import annotations

from dataclasses import replace

from windtower_backend.config import load_settings
from windtower_backend.input_batches.providers import QwenEngineeringProvider


class _ErrorResponse:
    status_code = 400
    ok = False
    reason = "Bad Request"

    @staticmethod
    def json():
        return {
            "request_id": "request-test",
            "code": "InvalidParameter",
            "message": "max_output_tokens must be at least 16",
        }

    @staticmethod
    def raise_for_status() -> None:
        raise AssertionError("structured error should be handled first")


def test_qwen_connection_reserves_reasoning_and_answer_tokens(monkeypatch) -> None:
    settings = replace(load_settings(), qwen_api_key="test-key")
    provider = QwenEngineeringProvider(settings)
    observed = {}

    class _SuccessResponse:
        status_code = 200
        ok = True

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json():
            return {"output_text": "OK", "usage": {}}

    def post(_url, **kwargs):
        observed.update(kwargs["json"])
        return _SuccessResponse()

    monkeypatch.setattr("windtower_backend.input_batches.providers.requests.post", post)

    result = provider.connection_test()

    assert result["status"] == "ready"
    assert observed["max_output_tokens"] == 256


def test_qwen_error_preserves_safe_server_diagnostics(monkeypatch) -> None:
    settings = replace(load_settings(), qwen_api_key="test-key")
    provider = QwenEngineeringProvider(settings)
    monkeypatch.setattr(
        "windtower_backend.input_batches.providers.requests.post",
        lambda *_args, **_kwargs: _ErrorResponse(),
    )

    result = provider.connection_test()

    assert result["status"] == "unreachable"
    assert "InvalidParameter" in result["error"]
    assert "request-test" in result["error"]
    assert "test-key" not in result["error"]


def test_qwen_rejects_response_from_a_different_model(monkeypatch) -> None:
    settings = replace(load_settings(), qwen_api_key="test-key")
    provider = QwenEngineeringProvider(settings, model="qwen3.8-max")

    class _WrongModelResponse:
        status_code = 200
        ok = True

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json():
            return {
                "model": "qwen-max",
                "output_text": '{"schema_version":"test","fields":[]}',
                "usage": {"total_tokens": 12},
            }

    monkeypatch.setattr(
        "windtower_backend.input_batches.providers.requests.post",
        lambda *_args, **_kwargs: _WrongModelResponse(),
    )

    result = provider.map_parameters(
        [{"evidence_id": "E1", "filename": "CASE_A.xls", "location": {}, "text": "R1C1=1"}],
        "test",
    )

    assert result["status"] == "failed"
    assert "model mismatch" in result["error"]
