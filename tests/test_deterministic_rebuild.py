from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from windtower_backend.l0 import build_l0
from windtower_backend.modeling import ProjectState


@pytest.mark.numerical
def test_same_confirmed_model_rebuilds_identical_inp_ten_times(tmp_path: Path) -> None:
    ir = json.loads(
        Path("tests/fixtures/synthetic_l0_ir.json").read_text(encoding="utf-8")
    )
    ir["mesh_and_set_ir"]["global_size_mm"] = 1500.0
    ir["mesh_and_set_ir"]["door_size_mm"] = 300.0
    observed: list[dict[str, str]] = []

    for index in range(10):
        root = tmp_path / f"run-{index:02d}"
        state = ProjectState(
            project_id="wt-repeat",
            root=root,
            excel_path=root / "source.xlsx",
            ir=deepcopy(ir),
            artifacts={},
        )
        report = build_l0(state)
        assert report["status"] == "ready"
        observed.append({
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.inp"))
        })

    assert observed[0]
    assert all(item == observed[0] for item in observed[1:])

