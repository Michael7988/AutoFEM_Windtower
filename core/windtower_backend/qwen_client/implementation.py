"""Bounded Qwen calls for heterogeneous-input parameter mapping."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from ..config import Settings
from ..llm_usage import prompt_hash, usage_from_response


SYSTEM_PROMPT = """Map the supplied engineering evidence to parameter candidates.
Return JSON only. Never emit a formal parameter set, mesh, node, element,
Abaqus keyword, INP text, solver command, or repair action.
The response may contain only candidate fields with source locations,
normalized values and confidence."""


def qwen_normalize_standard_input(
    settings: Settings,
    profile: dict[str, Any],
    validation: dict[str, Any] | None = None,
    hints: list[str] | None = None,
) -> dict[str, Any]:
    prompt_payload = {
        "task": "candidate_parameter_mapping",
        "profile": _compact_workbook_profile(profile),
        "validation": _compact_validation(validation or {}),
        "hints": hints or [],
    }
    return _call_candidate_mapping(
        settings,
        prompt_payload,
        reason="standard_input_mapping",
    )


def qwen_review_standard_input(
    settings: Settings,
    profile: dict[str, Any],
    validation: dict[str, Any] | None = None,
    standard_candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prompt_payload = {
        "task": "candidate_parameter_review",
        "profile": _compact_workbook_profile(profile),
        "validation": _compact_validation(validation or {}),
        "candidate": standard_candidate or {},
    }
    return _call_candidate_mapping(
        settings,
        prompt_payload,
        reason="standard_input_review",
    )


def _call_candidate_mapping(
    settings: Settings,
    prompt_payload: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    prompt_text = json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True)
    if not settings.qwen_api_key:
        return {
            "status": "not_called",
            "reason": "missing_api_key",
            "provider": "none",
            "model": "none",
            "prompt_hash": prompt_hash(prompt_text),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "candidate": None,
        }
    # OpenAI-compatible chat completion call (works with DashScope compatible-mode
    # and third-party relays such as n1n.ai). The legacy DashScope native
    # /services/aigc/text-generation/generation endpoint is not used here so the
    # same code path serves any OpenAI-compatible base_url.
    payload = {
        "model": settings.qwen_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": min(settings.qwen_temperature, 0.2),
        "max_tokens": min(settings.qwen_max_tokens, 4000),
    }
    started = time.time()
    try:
        response = requests.post(
            f"{settings.qwen_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.qwen_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=settings.qwen_timeout,
        )
        response.raise_for_status()
        raw = response.json()
        candidate = _json_from_text(_extract_content(raw))
        return {
            "status": "called",
            "reason": reason,
            "provider": "qwen",
            "model": settings.qwen_model,
            "prompt_hash": prompt_hash(prompt_text),
            **usage_from_response(raw),
            "latency_ms": int((time.time() - started) * 1000),
            "candidate": candidate,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": reason,
            "provider": "qwen",
            "model": settings.qwen_model,
            "prompt_hash": prompt_hash(prompt_text),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
            "candidate": None,
        }


def _compact_workbook_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_file": profile.get("source_file"),
        "input_format": profile.get("input_format"),
        "format_confidence": profile.get("format_confidence"),
        "format_reasons": profile.get("format_reasons") or [],
        "sheets": [
            {
                "name": sheet.get("name"),
                "rows": sheet.get("rows"),
                "columns": sheet.get("columns"),
                "preview": (sheet.get("preview") or [])[:40],
            }
            for sheet in profile.get("sheets") or []
        ],
    }


def _compact_validation(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": validation.get("status"),
        "blocked": validation.get("blocked"),
        "missing_items": validation.get("missing_items") or [],
        "issues": validation.get("issues") or [],
    }


def _extract_content(raw: dict[str, Any]) -> str:
    # OpenAI chat/completions format: top-level choices[].message.content
    choices = raw.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            return "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
        return str(content or "")
    # DashScope Responses / native format: output.choices[].message.content
    output = raw.get("output") or {}
    choices = output.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            return "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
        return str(content or "")
    return str(output.get("text") or raw.get("text") or "")


def _json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be one JSON object.")
    return payload


def _normalize_patch_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a legacy test fixture without exposing patch APIs."""

    normalized = dict(item)
    path = str(normalized.get("path") or "")
    if path and not path.startswith("/"):
        path = "/" + path.replace(".", "/")
    normalized["path"] = path
    return normalized
