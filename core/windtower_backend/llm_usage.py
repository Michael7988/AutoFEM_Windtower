from __future__ import annotations

import csv
import hashlib
import html
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LLM_SCENARIOS = (
    "excel_parse_assist",
    "parameter_candidate_mapping",
    "parameter_candidate_review",
    "abaqus_failure_explanation",
    "result_sanity_explanation",
)


@dataclass(frozen=True)
class LlmUsageEvent:
    scenario: str
    status: str
    reason: str
    provider: str = "none"
    model: str = "none"
    prompt_hash: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    fallback_used: bool = False
    artifact_ref: str = ""
    triggered: bool = False
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {
            "timestamp": self.timestamp or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scenario": self.scenario,
            "status": self.status,
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "prompt_tokens": int(self.prompt_tokens or 0),
            "completion_tokens": int(self.completion_tokens or 0),
            "total_tokens": int(self.total_tokens or 0),
            "latency_ms": int(self.latency_ms or 0),
            "fallback_used": bool(self.fallback_used),
            "artifact_ref": self.artifact_ref,
            "triggered": bool(self.triggered),
        }
        return data


def prompt_hash(prompt: str | None) -> str:
    if not prompt:
        return ""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def usage_from_response(raw: dict[str, Any] | None) -> dict[str, int]:
    usage = (raw or {}).get("usage") or (raw or {}).get("output", {}).get("usage") or {}
    prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def record_llm_event(audit_dir: Path, event: LlmUsageEvent | dict[str, Any]) -> dict[str, Any]:
    audit_dir.mkdir(parents=True, exist_ok=True)
    data = event.to_dict() if isinstance(event, LlmUsageEvent) else _normalize_event(event)
    with (audit_dir / "llm_events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")
    return data


def write_case_llm_audit(
    case_dir: Path,
    case_id: str,
    parsed: dict[str, Any] | None,
    enable_llm: bool = False,
    llm_audit_only: bool = True,
) -> dict[str, Any]:
    audit_dir = case_dir / "llm_audit"
    if audit_dir.exists():
        events_path = audit_dir / "llm_events.jsonl"
        if events_path.exists():
            events_path.unlink()
    confidence = (parsed or {}).get("confidence_report", {}) if isinstance(parsed, dict) else {}
    llm_required = bool(confidence.get("llm_assist_required"))
    trigger_reasons = confidence.get("llm_trigger_reasons") or []
    for scenario in LLM_SCENARIOS:
        triggered = scenario == "excel_parse_assist" and llm_required
        if triggered and enable_llm and not llm_audit_only:
            status = "not_called"
            reason = "batch_llm_advisory_call_not_enabled_in_deterministic_path"
        elif triggered:
            status = "not_called"
            reason = "rule_only_policy" if llm_audit_only else "disabled"
        else:
            status = "not_called"
            reason = "not_required"
        record_llm_event(
            audit_dir,
            LlmUsageEvent(
                scenario=scenario,
                status=status,
                reason=reason,
                artifact_ref="facts/engineering_facts.json" if scenario == "excel_parse_assist" else "",
                triggered=triggered,
            ),
        )
    events = read_llm_events(audit_dir)
    usage = summarize_llm_events(events)
    usage.update(
        {
            "case_id": case_id,
            "llm_assist_required": llm_required,
            "llm_trigger_reasons": trigger_reasons,
            "policy": "audit-only/rule-only",
            "events_file": str(audit_dir / "llm_events.jsonl"),
        }
    )
    (audit_dir / "llm_usage.json").write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_case_policy_report(audit_dir, usage, events)
    return usage


def read_llm_events(audit_dir: Path) -> list[dict[str, Any]]:
    path = audit_dir / "llm_events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def summarize_llm_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    called = [event for event in events if event.get("status") == "called"]
    return {
        "llm_call_count": len(called),
        "llm_prompt_tokens": sum(int(event.get("prompt_tokens") or 0) for event in events),
        "llm_completion_tokens": sum(int(event.get("completion_tokens") or 0) for event in events),
        "llm_total_tokens": sum(int(event.get("total_tokens") or 0) for event in events),
        "scenario_count": len(events),
        "triggered_scenarios": [event.get("scenario") for event in events if event.get("triggered")],
    }


def write_llm_usage_summary(batch_root: Path, case_usages: list[dict[str, Any]]) -> dict[str, Any]:
    batch_root.mkdir(parents=True, exist_ok=True)
    totals = {
        "llm_call_count": sum(int(item.get("llm_call_count") or 0) for item in case_usages),
        "llm_prompt_tokens": sum(_usage_int(item, "llm_prompt_tokens", "prompt_tokens") for item in case_usages),
        "llm_completion_tokens": sum(_usage_int(item, "llm_completion_tokens", "completion_tokens") for item in case_usages),
        "llm_total_tokens": sum(_usage_int(item, "llm_total_tokens", "total_tokens") for item in case_usages),
    }
    payload = {"status": "pass", "policy": "audit-only/rule-only", "totals": totals, "cases": case_usages}
    (batch_root / "llm_usage_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "case_id",
        "load_case",
        "llm_call_count",
        "llm_prompt_tokens",
        "llm_completion_tokens",
        "llm_total_tokens",
        "llm_assist_required",
        "policy",
    ]
    with (batch_root / "llm_usage_summary.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in case_usages:
            writer.writerow({field: item.get(field, "") for field in fields})
    _write_batch_audit_report(batch_root, payload)
    return payload


def _usage_int(item: dict[str, Any], preferred: str, fallback: str) -> int:
    return int(item.get(preferred) or item.get(fallback) or 0)


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    data = LlmUsageEvent(
        scenario=str(event.get("scenario") or "unknown"),
        status=str(event.get("status") or "not_called"),
        reason=str(event.get("reason") or ""),
        provider=str(event.get("provider") or "none"),
        model=str(event.get("model") or "none"),
        prompt_hash=str(event.get("prompt_hash") or ""),
        prompt_tokens=int(event.get("prompt_tokens") or 0),
        completion_tokens=int(event.get("completion_tokens") or 0),
        total_tokens=int(event.get("total_tokens") or 0),
        latency_ms=int(event.get("latency_ms") or 0),
        fallback_used=bool(event.get("fallback_used")),
        artifact_ref=str(event.get("artifact_ref") or ""),
        triggered=bool(event.get("triggered")),
        timestamp=str(event.get("timestamp") or ""),
    ).to_dict()
    return data


def _write_case_policy_report(audit_dir: Path, usage: dict[str, Any], events: list[dict[str, Any]]) -> None:
    lines = [
        "# LLM Usage Audit",
        "",
        f"- Case: {usage.get('case_id')}",
        f"- Policy: {usage.get('policy')}",
        f"- LLM assist required: {usage.get('llm_assist_required')}",
        f"- LLM calls: {usage.get('llm_call_count')}",
        f"- Prompt tokens: {usage.get('llm_prompt_tokens')}",
        f"- Completion tokens: {usage.get('llm_completion_tokens')}",
        f"- Total tokens: {usage.get('llm_total_tokens')}",
        "",
        "## Scenarios",
        "",
    ]
    for event in events:
        lines.append(f"- {event.get('scenario')}: {event.get('status')} ({event.get('reason')})")
    (audit_dir / "llm_policy_report.md").write_text("\n".join(lines), encoding="utf-8")


def _write_batch_audit_report(batch_root: Path, payload: dict[str, Any]) -> None:
    totals = payload.get("totals", {})
    lines = [
        "# Batch LLM Usage Audit",
        "",
        f"- Policy: {payload.get('policy')}",
        f"- LLM calls: {totals.get('llm_call_count')}",
        f"- Prompt tokens: {totals.get('llm_prompt_tokens')}",
        f"- Completion tokens: {totals.get('llm_completion_tokens')}",
        f"- Total tokens: {totals.get('llm_total_tokens')}",
        "",
        "## Cases",
        "",
    ]
    for item in payload.get("cases", []):
        lines.append(
            f"- {item.get('case_id')} {item.get('load_case', '')}: "
            f"calls={item.get('llm_call_count')}, total_tokens={item.get('llm_total_tokens')}, "
            f"assist_required={item.get('llm_assist_required')}"
        )
    md = "\n".join(lines)
    (batch_root / "llm_audit_report.md").write_text(md, encoding="utf-8")
    html_text = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>Batch LLM Usage Audit</title>"
        "<style>body{font-family:Arial,'Microsoft YaHei',sans-serif;max-width:1100px;margin:28px auto;color:#172033}"
        "pre{white-space:pre-wrap;background:#f7f9fc;border:1px solid #d8dee8;padding:18px;border-radius:6px}</style>"
        "</head><body><pre>"
        + html.escape(md)
        + "</pre></body></html>"
    )
    (batch_root / "llm_audit_report.html").write_text(html_text, encoding="utf-8")
