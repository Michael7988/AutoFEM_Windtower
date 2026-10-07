from __future__ import annotations

import argparse
import json
import time
from typing import Any

import requests

from .config import Settings, load_settings
from .input_batches.providers import _responses_text


DEFAULT_QWEN_TEXT_MODELS = [
    "qwen-max",
    "qwen-plus",
    "qwen-turbo",
    "qwen-long",
    "qwen-flash",
    "qwen3-max",
    "qwen3.8-max",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-plus",
    "qwen3.5-plus",
]


def settings_summary(settings: Settings) -> dict[str, Any]:
    return {
        "qwen_api_key_set": bool(settings.qwen_api_key),
        "qwen_api_key_preview": _mask_key(settings.qwen_api_key),
        "qwen_model": settings.qwen_model,
        "qwen_base_url": settings.qwen_base_url,
        "qwen_temperature": settings.qwen_temperature,
        "qwen_max_tokens": settings.qwen_max_tokens,
        "qwen_timeout": settings.qwen_timeout,
    }


def test_qwen_model(settings: Settings, model: str | None = None, prompt: str = "只回答 OK") -> dict[str, Any]:
    model = model or settings.qwen_model
    if not settings.qwen_api_key:
        return {"model": model, "status": "missing_api_key", "ok": False}
    payload = {
        "model": model,
        "input": prompt,
        "temperature": min(settings.qwen_temperature, 0.2),
        "max_output_tokens": 256,
    }
    url = f"{settings.qwen_base_url.rstrip('/')}/responses"
    started = time.time()
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.qwen_api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=settings.qwen_timeout,
        )
    except Exception as exc:
        return {"model": model, "status": "request_failed", "ok": False, "error": str(exc), "latency_s": round(time.time() - started, 3)}
    latency = round(time.time() - started, 3)
    try:
        raw = response.json()
    except ValueError:
        raw = {"raw_text": response.text[:500]}
    if response.status_code >= 400:
        return {
            "model": model,
            "status": "http_error",
            "ok": False,
            "status_code": response.status_code,
            "error_code": raw.get("code") or raw.get("Code"),
            "error_message": raw.get("message") or raw.get("Message") or response.text[:300],
            "latency_s": latency,
        }
    content = _extract_content(raw)
    return {
        "model": model,
        "status": "pass" if content else "empty_response",
        "ok": bool(content),
        "content_preview": content[:120],
        "usage": raw.get("usage") or raw.get("output", {}).get("usage") or {},
        "request_id": raw.get("request_id") or raw.get("RequestId"),
        "latency_s": latency,
    }


def probe_qwen_models(settings: Settings, models: list[str] | None = None) -> dict[str, Any]:
    models = models or DEFAULT_QWEN_TEXT_MODELS
    results = [test_qwen_model(settings, model=model) for model in models]
    return {
        "settings": settings_summary(settings),
        "available_models": [item["model"] for item in results if item.get("ok")],
        "failed_models": [
            {
                "model": item["model"],
                "status": item.get("status"),
                "status_code": item.get("status_code"),
                "error_code": item.get("error_code"),
                "error_message": item.get("error_message"),
            }
            for item in results
            if not item.get("ok")
        ],
        "results": results,
    }


def _extract_content(raw: dict[str, Any]) -> str:
    responses_text = _responses_text(raw)
    if responses_text:
        return responses_text
    for item in raw.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                return str(content.get("text") or "")
    output = raw.get("output", {})
    if not isinstance(output, dict):
        return str(raw.get("output_text") or raw.get("content") or "")
    choices = output.get("choices") or []
    if choices:
        return str(choices[0].get("message", {}).get("content", "") or "")
    return str(output.get("text", "") or raw.get("content", "") or "")


def _mask_key(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def list_relay_models(settings: Settings) -> dict[str, Any]:
    """List models exposed by the configured OpenAI-compatible relay."""
    if not settings.qwen_api_key:
        return {"status": "missing_api_key", "models": []}
    url = f"{settings.qwen_base_url.rstrip('/')}/models"
    started = time.time()
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {settings.qwen_api_key}"},
            timeout=settings.qwen_timeout,
        )
    except Exception as exc:
        return {"status": "request_failed", "ok": False, "error": str(exc), "latency_s": round(time.time() - started, 3), "models": []}
    latency = round(time.time() - started, 3)
    if response.status_code >= 400:
        raw = {}
        try:
            raw = response.json()
        except ValueError:
            raw = {"raw_text": response.text[:300]}
        return {
            "status": "http_error", "ok": False,
            "status_code": response.status_code,
            "error_code": raw.get("code") or raw.get("Code") or raw.get("error", {}).get("type"),
            "error_message": raw.get("message") or raw.get("error", {}).get("message") or response.text[:300],
            "latency_s": latency, "models": [],
        }
    try:
        data = response.json()
    except ValueError:
        return {"status": "bad_json", "ok": False, "models": [], "raw_text": response.text[:500]}
    models = [
        {
            "id": item.get("id"),
            "owned_by": item.get("owned_by"),
            "endpoint_types": item.get("supported_endpoint_types"),
            "tags": item.get("tags"),
        }
        for item in data.get("data", [])
    ]
    return {"status": "pass", "ok": True, "count": len(models), "latency_s": latency, "models": models}


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Qwen/relay connectivity and model availability.")
    parser.add_argument("--model", default="", help="Test one model. Defaults to configured qwen.model.")
    parser.add_argument("--probe-models", action="store_true", help="Probe a default list of Qwen text models.")
    parser.add_argument("--models", default="", help="Comma-separated model IDs to probe.")
    parser.add_argument("--list-models", action="store_true", help="List models exposed by the configured relay.")
    args = parser.parse_args()
    settings = load_settings()
    if args.list_models:
        result = list_relay_models(settings)
    elif args.probe_models or args.models:
        models = [item.strip() for item in args.models.split(",") if item.strip()] or None
        result = probe_qwen_models(settings, models)
    else:
        result = {"settings": settings_summary(settings), "test": test_qwen_model(settings, args.model or None)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
