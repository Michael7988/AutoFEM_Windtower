from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from windtower_backend.benchmark.experiment_budget import (
    AmbiguousReservation,
    BudgetExceeded,
    budget_summary,
    create_budget_ledger,
    finalize_call,
    load_budget_ledger,
    mark_dispatched,
    reserve_call,
    reservations_as_events,
)


def _metadata() -> dict:
    return {
        "phase": "E1",
        "experiment": "standard",
        "method": "P",
        "case": "T01",
        "role": "engineering",
        "repeat": 1,
        "retry_reason": "",
    }


def test_budget_reservation_finalization_and_resume_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    create_budget_ledger(path, max_calls=2, max_tokens=100, warning_tokens=50, max_retries=1)
    reserved = reserve_call(path, "logical:A01", _metadata(), 10, 20)
    assert reserved["new"] is True
    mark_dispatched(path, "logical:A01")
    event = finalize_call(
        path,
        "logical:A01",
        {
            "status": "called",
            "model": "qwen3.8-max",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "latency_ms": 12,
        },
    )
    assert event["usage_reported"] is True
    assert reserve_call(path, "logical:A01", _metadata(), 10, 20)["action"] == "reuse_final"
    assert budget_summary(load_budget_ledger(path))["actual_http_request_count"] == 1
    assert reservations_as_events(path)[0]["total_tokens"] == 15


def test_budget_blocks_ambiguous_duplicate_and_projected_token_overrun(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    create_budget_ledger(path, max_calls=3, max_tokens=50, warning_tokens=40, max_retries=1)
    reserve_call(path, "pending:A01", _metadata(), 10, 10)
    with pytest.raises(AmbiguousReservation):
        reserve_call(path, "pending:A01", _metadata(), 10, 10)
    with pytest.raises(BudgetExceeded, match="token"):
        reserve_call(path, "large:A01", _metadata(), 25, 25)


def test_budget_enforces_global_retry_reserve(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    create_budget_ledger(path, max_calls=4, max_tokens=500, warning_tokens=400, max_retries=1)
    reserve_call(path, "one:A02", _metadata(), 5, 5, is_retry=True)
    with pytest.raises(BudgetExceeded, match="retry"):
        reserve_call(path, "two:A02", _metadata(), 5, 5, is_retry=True)


def test_budget_atomic_reservations_respect_call_limit(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    create_budget_ledger(path, max_calls=4, max_tokens=1000, warning_tokens=900, max_retries=2)

    def reserve(index: int) -> str:
        try:
            reserve_call(path, f"call-{index}:A01", _metadata(), 1, 1)
            return "reserved"
        except BudgetExceeded:
            return "blocked"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(reserve, range(8)))
    assert outcomes.count("reserved") == 4
    assert outcomes.count("blocked") == 4
    assert budget_summary(load_budget_ledger(path))["reserved_call_count"] == 4
