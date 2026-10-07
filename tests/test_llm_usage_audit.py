from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from windtower_backend.llm_usage import LlmUsageEvent, record_llm_event, usage_from_response, write_case_llm_audit, write_llm_usage_summary


class LlmUsageAuditTest(unittest.TestCase):
    def test_default_case_audit_records_zero_real_llm_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "model_01"
            usage = write_case_llm_audit(
                case_dir,
                "model_01",
                {"confidence_report": {"llm_assist_required": False, "llm_trigger_reasons": []}},
            )

            self.assertEqual(usage["llm_call_count"], 0)
            self.assertEqual(usage["llm_total_tokens"], 0)
            self.assertTrue((case_dir / "llm_audit" / "llm_usage.json").exists())
            events = (case_dir / "llm_audit" / "llm_events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertTrue(events)
            self.assertTrue(all(json.loads(line)["reason"] == "not_required" for line in events if line.strip()))

    def test_missing_parameter_trigger_is_audited_without_calling_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "model_02"
            usage = write_case_llm_audit(
                case_dir,
                "model_02",
                {"confidence_report": {"llm_assist_required": True, "llm_trigger_reasons": ["fem_key_value_parameters_missing"]}},
            )

            self.assertEqual(usage["llm_call_count"], 0)
            self.assertIn("excel_parse_assist", usage["triggered_scenarios"])
            events = [json.loads(line) for line in (case_dir / "llm_audit" / "llm_events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            parse_event = next(event for event in events if event["scenario"] == "excel_parse_assist")
            self.assertTrue(parse_event["triggered"])
            self.assertEqual(parse_event["reason"], "rule_only_policy")

    def test_usage_summary_accumulates_simulated_token_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "llm_audit"
            token_usage = usage_from_response({"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})
            record_llm_event(
                audit_dir,
                LlmUsageEvent(
                    scenario="parameter_candidate_mapping",
                    status="called",
                    reason="unit_test",
                    provider="qwen",
                    model="qwen-max",
                    **token_usage,
                ),
            )
            payload = write_llm_usage_summary(Path(tmp), [{"case_id": "model_01", "load_case": "LC0", **token_usage, "llm_call_count": 1}])

            self.assertEqual(payload["totals"]["llm_call_count"], 1)
            self.assertEqual(payload["totals"]["llm_total_tokens"], 18)
            self.assertTrue((Path(tmp) / "llm_usage_summary.csv").exists())


if __name__ == "__main__":
    unittest.main()
