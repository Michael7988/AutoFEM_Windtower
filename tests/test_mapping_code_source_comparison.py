from __future__ import annotations

from core.windtower_backend.benchmark.mapping_code_source_comparison import _status_score


def test_status_score_is_conservative() -> None:
    assert _status_score("pass") == 1.0
    assert _status_score("partial") == 0.5
    assert _status_score("fail") == 0.0
    assert _status_score("not_reached") == 0.0

