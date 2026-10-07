from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from ..capability_profile import FORMAL_INPUT_EXTENSIONS, input_format_registry

import requests

from ..config import Settings, resolve_model_connection


class EngineeringLlmProvider:
    def capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    def map_parameters(
        self,
        evidence: list[dict[str, Any]],
        modeling_goal: str | None,
        mapping_prompt: str | None = None,
        target_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def extract_image_text(self, content: bytes, media_type: str, prompt: str) -> dict[str, Any]:
        raise NotImplementedError

    def understand_drawing(self, content: bytes, media_type: str, prompt: str) -> dict[str, Any]:
        return self.extract_image_text(content, media_type, prompt)

    def usage(self) -> dict[str, Any]:
        raise NotImplementedError

    def connection_test(self) -> dict[str, Any]:
        raise NotImplementedError


class QwenEngineeringProvider(EngineeringLlmProvider):
    def __init__(
        self,
        settings: Settings,
        *,
        model: str | None = None,
        role: str = "engineering",
    ):
        self.settings = settings
        self.main_model = model or settings.qwen_engineering_model
        self.role = role
        self.provider_profile, self.api_key, self.base_url = resolve_model_connection(self.main_model, settings)
        prefix = f"QWEN_{role.upper()}"
        self.temperature = float(os.getenv(f"{prefix}_TEMPERATURE", "0.1"))
        self.max_tokens = int(os.getenv(f"{prefix}_MAX_TOKENS", str(settings.qwen_max_tokens)))
        self.mapping_max_tokens = int(os.getenv(f"{prefix}_MAPPING_MAX_TOKENS", "3000"))
        self.timeout = int(os.getenv(f"{prefix}_TIMEOUT", str(settings.qwen_timeout)))
        self.ocr_model = os.getenv("QWEN_OCR_MODEL", "qwen3.5-ocr")
        self.vision_model = os.getenv("QWEN_VISION_MODEL", "qwen3.7-max-2026-06-08")
        self._usage = {
            "attempts": 0, "calls": 0, "failures": 0, "timeouts": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "latency_ms": 0,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": "engineering_llm_capabilities_v1",
            "provider": "qwen",
            "provider_profile": self.provider_profile,
            "upstream_provider": (
                "deepseek" if self.provider_profile == "deepseek_official"
                else "dashscope" if self.provider_profile == "official"
                else "n1n" if self.provider_profile == "n1n"
                else "legacy"
            ),
            "role": self.role,
            "configured": bool(self.api_key),
            "connection_status": "configured" if self.api_key else "unconfigured",
            "api_protocol": "openai_responses",
            "region": "cn-beijing" if self.provider_profile == "official" else "provider_default",
            "main_model": self.main_model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "mapping_max_tokens": self.mapping_max_tokens,
            "timeout_seconds": self.timeout,
            "ocr_model": self.ocr_model,
            "vision_model": self.vision_model,
            "supports": {"text": True, "structured_output": True, "image": True, "ocr": True, "native_files": False},
            "parse_modes": [
                {"key": "llm_full", "label": "纯 AI 参数形成", "available": bool(self.api_key)},
                {"key": "llm", "label": "本地优先 + AI 补全", "available": bool(self.api_key)},
                {"key": "deterministic", "label": "固定脚本解析", "available": True},
            ],
            "accepted_extensions": list(FORMAL_INPUT_EXTENSIONS),
            "input_format_registry": input_format_registry(),
            "limits": {"max_files": 20, "image_mb": 20, "file_mb": 50, "batch_mb": 200},
            "policy": "llm_full maps the complete active contract from evidence; llm supplements unresolved local mappings; deterministic never calls an LLM.",
        }

    def map_parameters(
        self,
        evidence: list[dict[str, Any]],
        modeling_goal: str | None,
        mapping_prompt: str | None = None,
        target_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key or not evidence:
            return {"status": "not_called", "provider": "none", "model": "none", "fields": [], "usage": {}}
        compact = [
            {
                "evidence_id": item["evidence_id"],
                "filename": item["filename"],
                "field": item.get("field"),
                "unit": item.get("unit") or item.get("raw_unit"),
                "status": item.get("status", "observed"),
                "location": {
                    key: value for key, value in (item.get("location") or {}).items()
                    if value not in (None, "", [], {})
                },
                "text": item["text"][:4000],
            }
            for item in evidence[:30]
        ]
        system = mapping_prompt or _candidate_mapping_prompt()
        payload = {
            "model": self.main_model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps({
                    "modeling_goal": modeling_goal,
                    "target_parameter_keys": target_keys or [],
                    "instruction": (
                        "Return compact JSON only. Emit one object per unresolved target key with only "
                        "path,value,unit,evidence_ids,status and confidence. Do not explain, repeat evidence "
                        "text, output null fields, or repeat resolved parameters."
                    ),
                    "evidence": compact,
                }, ensure_ascii=False)}]},
            ],
            "temperature": self.temperature,
            "max_output_tokens": min(self.max_tokens, self.mapping_max_tokens),
        }
        return self._call_responses(payload)

    def extract_image_text(self, content: bytes, media_type: str, prompt: str) -> dict[str, Any]:
        if not self.api_key:
            return {"status": "not_called", "provider": "none", "model": "none", "text": "", "confidence": 0}
        encoded = base64.b64encode(content).decode("ascii")
        payload = {
            "model": self.ocr_model,
            "input": [{"role": "user", "content": [
                {"type": "input_image", "image_url": f"data:{media_type};base64,{encoded}"},
                {"type": "input_text", "text": prompt},
            ]}],
        }
        result = self._call_responses(payload, parse_json=False)
        return {**result, "confidence": 0.75 if result.get("text") else 0.0}

    def usage(self) -> dict[str, Any]:
        return dict(self._usage)

    def connection_test(self) -> dict[str, Any]:
        if not self.api_key:
            return {"status": "unconfigured", "provider": "qwen", "model": self.main_model, "message": "QWEN_API_KEY 未配置"}
        started = time.time()
        result = self._call_responses(
            {"model": self.main_model, "input": "仅回复 OK", "max_output_tokens": 256},
            parse_json=False,
        )
        ready = result.get("status") == "called" and bool(str(result.get("text") or "").strip())
        return {
            "status": "ready" if ready else "unreachable",
            "provider": "qwen",
            "model": self.main_model,
            "latency_ms": int((time.time() - started) * 1000),
            "error": result.get("error") or (None if ready else "empty_response"),
        }

    def _call_responses(self, payload: dict[str, Any], *, parse_json: bool = True) -> dict[str, Any]:
        started = time.time()
        last_error: Exception | None = None
        request_profile, request_api_key, request_base_url = resolve_model_connection(
            str(payload.get("model") or self.main_model), self.settings
        )
        if not request_api_key:
            return {
                "status": "failed",
                "provider": "qwen",
                "provider_profile": request_profile,
                "model": payload.get("model", self.main_model),
                "fields": [],
                "text": "",
                "usage": {},
                "error": f"API key is not configured for profile: {request_profile}",
            }
        for attempt in range(3):
            self._usage["attempts"] += 1
            try:
                response = requests.post(
                    f"{request_base_url.rstrip('/')}/responses",
                    headers={"Authorization": f"Bearer {request_api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=(10, max(self.timeout, 120)),
                )
                if getattr(response, "status_code", 200) in {429, 500, 502, 503, 504} and attempt < 2:
                    self._usage["failures"] += 1
                    time.sleep(1 + attempt)
                    continue
                if not getattr(response, "ok", True):
                    raise RuntimeError(_safe_response_error(response))
                response.raise_for_status()
                raw = response.json()
                usage = raw.get("usage") or {}
                self._record_usage(usage, started)
                response_model = str(raw.get("model") or payload["model"])
                if response_model != str(payload["model"]):
                    raise RuntimeError(
                        f"LLM response model mismatch: requested={payload['model']} returned={response_model}"
                    )
                text = _responses_text(raw)
                if not parse_json:
                    return {
                        "status": "called",
                        "provider": "qwen",
                        "provider_profile": request_profile,
                        "model": response_model,
                        "text": text,
                        "usage": usage,
                    }
                data = _json_text(text)
                return {
                    "status": "called",
                    "provider": "qwen",
                    "provider_profile": request_profile,
                    "model": response_model,
                    "schema_version": data.get("schema_version"),
                    "fields": data.get("fields", []),
                    "usage": usage,
                }
            except requests.Timeout as exc:
                last_error = exc
                self._usage["timeouts"] += 1
                self._usage["failures"] += 1
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
            except Exception as exc:
                last_error = exc
                self._usage["failures"] += 1
                break
        self._usage["latency_ms"] += int((time.time() - started) * 1000)
        return {
            "status": "failed",
            "provider": "qwen",
            "provider_profile": request_profile,
            "model": payload.get("model", self.main_model),
            "fields": [],
            "text": "",
            "usage": {},
            "error": str(last_error or "LLM request failed"),
        }

    def _record_usage(self, usage: dict[str, Any], started: float) -> None:
        self._usage["calls"] += 1
        self._usage["prompt_tokens"] += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        self._usage["completion_tokens"] += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        self._usage["total_tokens"] += int(usage.get("total_tokens") or 0)
        self._usage["latency_ms"] += int((time.time() - started) * 1000)


class OpenAIEngineeringProvider(EngineeringLlmProvider):
    """OpenAI-compatible implementation of the same evidence contract.

    The evidence layer does not depend on this provider. Switching providers only
    changes parameter mapping/OCR calls and leaves file locations and hashes intact.
    """

    def __init__(self, settings: Settings):
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.main_model = os.getenv("OPENAI_MODEL", "gpt-4.1")
        self.vision_model = os.getenv("OPENAI_VISION_MODEL", self.main_model)
        self.timeout = int(os.getenv("OPENAI_TIMEOUT", str(max(settings.qwen_timeout, 90))))
        self._usage = {
            "attempts": 0, "calls": 0, "failures": 0, "timeouts": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "latency_ms": 0,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": "engineering_llm_capabilities_v1",
            "provider": "openai",
            "configured": bool(self.api_key),
            "main_model": self.main_model,
            "ocr_model": self.vision_model,
            "vision_model": self.vision_model,
            "supports": {"text": True, "structured_output": True, "image": True, "ocr": True, "native_files": False},
            "accepted_extensions": list(FORMAL_INPUT_EXTENSIONS),
            "input_format_registry": input_format_registry(),
            "limits": {"max_files": 20, "image_mb": 20, "file_mb": 50, "batch_mb": 200},
            "policy": "Supports evidence-only full mapping and deterministic-first supplementation; native files are converted before mapping.",
        }

    def map_parameters(
        self,
        evidence: list[dict[str, Any]],
        modeling_goal: str | None,
        mapping_prompt: str | None = None,
        target_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key or not evidence:
            return {"status": "not_called", "provider": "none", "model": "none", "candidates": [], "usage": {}}
        compact = [
            {"evidence_id": item["evidence_id"], "filename": item["filename"], "location": item["location"], "text": item["text"][:2500]}
            for item in evidence[:30]
        ]
        payload = {
            "model": self.main_model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": mapping_prompt or _parameter_mapping_prompt()},
                {"role": "user", "content": json.dumps({
                    "modeling_goal": modeling_goal,
                    "target_parameter_keys": target_keys or [],
                    "instruction": "Only return unresolved target_parameter_keys. Do not repeat resolved fields.",
                    "evidence": compact,
                }, ensure_ascii=False)},
            ],
        }
        result = self._chat(payload)
        if result["status"] != "called":
            return {**result, "candidates": []}
        try:
            data = _json_text(result.pop("text"))
        except Exception as exc:
            return {**result, "status": "failed", "fields": [], "candidates": [], "error": f"Invalid structured response: {exc}"}
        return {
            **result,
            "schema_version": data.get("schema_version"),
            "fields": data.get("fields", []),
            "candidates": data.get("candidates", []),
        }

    def extract_image_text(self, content: bytes, media_type: str, prompt: str) -> dict[str, Any]:
        if not self.api_key:
            return {"status": "not_called", "provider": "none", "model": "none", "text": "", "confidence": 0}
        encoded = base64.b64encode(content).decode("ascii")
        payload = {
            "model": self.vision_model,
            "temperature": 0,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}", "detail": "high"}},
                ],
            }],
        }
        result = self._chat(payload)
        return {**result, "confidence": 0.75 if result["status"] == "called" and result.get("text") else 0}

    def usage(self) -> dict[str, Any]:
        return dict(self._usage)

    def connection_test(self) -> dict[str, Any]:
        if not self.api_key:
            return {"status": "unconfigured", "provider": "openai", "model": self.main_model}
        started = time.time()
        result = self._chat({"model": self.main_model, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 8})
        return {"status": "ready" if result.get("status") == "called" else "unreachable", "provider": "openai", "model": self.main_model, "latency_ms": int((time.time() - started) * 1000), "error": result.get("error")}

    def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            raw = response.json()
            usage = raw.get("usage") or {}
            self._record_usage(usage, started)
            choices = raw.get("choices") or []
            text = str(((choices[0].get("message") or {}).get("content") if choices else "") or "")
            return {"status": "called", "provider": "openai", "model": payload["model"], "text": text, "usage": usage}
        except Exception as exc:
            return {"status": "failed", "provider": "openai", "model": payload["model"], "text": "", "usage": {}, "error": str(exc)}

    def _record_usage(self, usage: dict[str, Any], started: float) -> None:
        self._usage["calls"] += 1
        self._usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self._usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        self._usage["total_tokens"] += int(usage.get("total_tokens") or 0)
        self._usage["latency_ms"] += int((time.time() - started) * 1000)


def create_engineering_provider(settings: Settings) -> EngineeringLlmProvider:
    provider = os.getenv("ENGINEERING_LLM_PROVIDER", "qwen").strip().lower()
    if provider in {"openai", "gpt"}:
        return OpenAIEngineeringProvider(settings)
    return QwenEngineeringProvider(
        settings,
        model=settings.qwen_engineering_model,
        role="engineering",
    )


def create_fem_provider(settings: Settings) -> EngineeringLlmProvider:
    provider = os.getenv("FEM_LLM_PROVIDER", "qwen").strip().lower()
    if provider in {"openai", "gpt"}:
        return OpenAIEngineeringProvider(settings)
    return QwenEngineeringProvider(
        settings,
        model=settings.qwen_fem_model,
        role="fem",
    )


def _parameter_mapping_prompt() -> str:
    return """Map engineering evidence to editable wind-tower parameter candidates. Return JSON only:
{"candidates":[{"key":string,"label":string,"value":any,"unit":string|null,"confidence":number,"evidence_ids":[string],"priority":"P0|P1","requires_confirmation":boolean}]}
Allowed keys are geometry.door.enabled, geometry.door.width_mm, geometry.door.height_mm, geometry.door.bottom_z_mm, geometry.door.angle_deg, geometry.door.reinforcement_enabled, geometry.door.outer_width_mm, geometry.door.outer_height_mm, geometry.door.frame_thickness_mm, geometry.door.reinforcement_material, material.elastic_modulus_mpa, material.poisson_ratio, material.density, material.yield_strength_mpa, mesh.global_size_mm, mesh.door_size_mm, loads.top_mass_tonne, loads.center_of_mass_offset_mm, loads.top_horizontal_force_n, loads.top_force_direction, loads.wind_direction_vector, loads.wind_pressure_mpa, loads.wind_pressure_profile, loads.top_force_parallel_n, loads.top_force_transverse_n, loads.top_force_vertical_n, loads.top_moment_roll_nmm, loads.top_moment_pitch_nmm, loads.top_moment_yaw_nmm, loads.hub_vertical_offset_mm, analysis.static_enabled, analysis.modal_enabled, analysis.modal_modes. Never invent a value or source."""


def _candidate_mapping_prompt() -> str:
    return """Return a compact evidence-grounded wind-tower candidate JSON and no prose:
{"schema_version":"candidate_mapping_ir_v1","fields":[{"path":string,"value":any,"unit":string|null,"status":"observed|inferred","confidence":number,"evidence_ids":[string]}]}
Allowed paths:
domain.wind_tower.tower_segments (list of objects with z_bottom_mm,z_top_mm,diameter_bottom_mm,diameter_top_mm,thickness_mm);
domain.wind_tower.door_opening (enabled,shape,width_mm,height_mm,z_bottom_mm,corner_radius_mm,azimuth_deg,reinforcement_enabled,outer_width_mm,outer_height_mm,frame_thickness_mm,reinforcement_material);
generic.materials[0] (name,elastic_modulus_mpa,poisson_ratio,density_tonne_mm3,yield_strength_mpa);
generic.fem_settings (mesh.global_size_mm,mesh.door_size_mm,loads.top_mass_tonne,loads.center_of_mass_offset_mm,loads.top_total_force_n,loads.top_force_direction,loads.wind_direction_vector,loads.wind_pressure_mpa,loads.wind_pressure_profile,loads.top_force_parallel_n,loads.top_force_transverse_n,loads.top_force_vertical_n,loads.top_moment_roll_nmm,loads.top_moment_pitch_nmm,loads.top_moment_yaw_nmm,loads.hub_vertical_offset_mm,analysis.static_enabled,analysis.modal_enabled,modal.modes,boundary.name).
Use only supplied evidence IDs. Never invent missing values, never output IR, mesh, INP, solver data, explanations, copied evidence text or null fields. Omit unavailable and already resolved values; do not apply engineering defaults."""


def _content_text(payload: dict[str, Any]) -> str:
    output = payload.get("output") or {}
    choices = output.get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            return "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
        return str(content)
    return str(output.get("text") or "")


def _responses_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    parts = []
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts)


def _safe_response_error(response: Any) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    code = str(payload.get("code") or "HTTP_ERROR")
    message = str(payload.get("message") or getattr(response, "reason", "request failed"))
    request_id = str(payload.get("request_id") or "")
    suffix = f" request_id={request_id}" if request_id else ""
    return f"HTTP {status} {code}: {message}{suffix}"


def _json_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(text)
