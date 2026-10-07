from __future__ import annotations

import json

from windtower_backend.benchmark import hybrid_paper_experiment as hybrid


class _Response:
    def __init__(self, status: int, lines: list = None, text: str = "", content_type: str = "text/event-stream") -> None:
        self.status_code = status
        self._lines = lines or []
        self.text = text
        self.headers = {"content-type": content_type, "x-request-id": "request-123"}

    def iter_lines(self, decode_unicode: bool = True) -> list:
        assert decode_unicode is True
        return self._lines

    def json(self) -> dict:
        return json.loads(self.text)


def _payload(stream: bool = True) -> dict:
    return {
        "model": "qwen3.8-max",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "test"}]}],
        "max_output_tokens": 32,
        "stream": stream,
    }


def test_single_http_request_parses_responses_sse_and_usage(monkeypatch: object) -> None:
    final = {
        "id": "response-1",
        "model": "qwen3.8-max",
        "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
        "output": [{"content": [{"type": "output_text", "text": '{"answer":"OK"}'}]}],
    }
    lines = [
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": '{"answer":"OK"}'}),
        "data: " + json.dumps({"type": "response.completed", "response": final}),
        "data: [DONE]",
    ]
    monkeypatch.setattr(hybrid.requests, "post", lambda *args, **kwargs: _Response(200, lines=lines))
    settings = type("Settings", (), {"qwen_base_url": "https://example.test", "qwen_api_key": "secret"})()
    result = hybrid._single_http_request(settings, _payload(), expect_json=True)
    assert result["status"] == "called"
    assert result["data"] == {"answer": "OK"}
    assert result["usage"]["total_tokens"] == 9
    assert result["request_id"] == "request-123"
    assert result["http_request_count"] == 1


def test_single_http_request_parses_typeless_relay_sse(monkeypatch: object) -> None:
    final = {
        "id": "response-relay",
        "object": "response",
        "status": "completed",
        "model": "qwen3.8-max",
        "usage": {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
        "output": [{"content": [{"type": "output_text", "text": '{"answer":"OK"}'}]}],
    }
    lines = [
        "event:",
        "data: " + json.dumps({"delta": '{"answer":"OK"}'}),
        "event:",
        "data: " + json.dumps(final),
        "data: [DONE]",
    ]
    monkeypatch.setattr(hybrid.requests, "post", lambda *args, **kwargs: _Response(200, lines=lines))
    settings = type("Settings", (), {"qwen_base_url": "https://relay.test", "qwen_api_key": "secret"})()
    result = hybrid._single_http_request(settings, _payload(), expect_json=True)
    assert result["status"] == "called"
    assert result["data"] == {"answer": "OK"}
    assert result["usage"]["total_tokens"] == 14
    assert result["model"] == "qwen3.8-max"


def test_single_http_request_accepts_event_field_sse(monkeypatch: object) -> None:
    final = {
        "id": "response-deepseek",
        "model": "deepseek-v4-pro",
        "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
        "output": [{"content": [{"type": "output_text", "text": "OK"}]}],
    }
    lines = [
        "data: " + json.dumps({"event": "response.output_text.delta", "delta": "OK"}),
        "data: " + json.dumps({"event": "response.completed", "response": final}),
    ]
    monkeypatch.setattr(hybrid.requests, "post", lambda *args, **kwargs: _Response(200, lines=lines))
    settings = type("Settings", (), {"qwen_base_url": "https://api.deepseek.com", "qwen_api_key": "secret"})()
    result = hybrid._single_http_request(settings, {**_payload(), "model": "deepseek-v4-pro"}, expect_json=False)
    assert result["status"] == "called"
    assert result["raw_text"] == "OK"
    assert result["usage"]["total_tokens"] == 10


def test_single_http_request_marks_429_and_5xx_retryable(monkeypatch: object) -> None:
    settings = type("Settings", (), {"qwen_base_url": "https://example.test", "qwen_api_key": "secret"})()
    for status in (429, 500, 503):
        monkeypatch.setattr(
            hybrid.requests,
            "post",
            lambda *args, _status=status, **kwargs: _Response(_status, text="provider unavailable"),
        )
        result = hybrid._single_http_request(settings, _payload(), expect_json=True)
        assert result["status"] == "failed"
        assert result["retryable"] is True
        assert result["http_request_count"] == 1


def test_usage_gate_rejects_missing_and_inconsistent_provider_usage() -> None:
    assert hybrid._usage_gate({"usage_reported": False, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}) == [
        "provider_usage_missing_or_inconsistent"
    ]
    assert "usage_sum_mismatch" in hybrid._usage_gate(
        {"usage_reported": True, "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 7}
    )
