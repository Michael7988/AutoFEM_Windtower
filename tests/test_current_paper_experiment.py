from pathlib import Path

from core.windtower_backend.benchmark.current_paper_experiment import DEFAULT_ORDER, _history_rows


def test_current_paper_case_order_is_four_unique_cases() -> None:
    assert DEFAULT_ORDER == ("T01", "T02", "T03", "T04")
    assert len(set(DEFAULT_ORDER)) == 4


def test_history_inventory_marks_blocked_assets_outside_body(tmp_path: Path) -> None:
    (tmp_path / "trusted_fem_v4_demo").mkdir()
    (tmp_path / "hybrid_demo").mkdir()
    rows = _history_rows(tmp_path)
    dispositions = {row["experiment"]: row["disposition"] for row in rows}
    assert dispositions["trusted_fem_v4_demo"] == "排除"
    assert dispositions["hybrid_demo"] == "排除"
