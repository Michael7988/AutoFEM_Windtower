from __future__ import annotations

from pathlib import Path

from windtower_backend.benchmark.b1_direct_baseline import (
    _scrubbed_environment,
    run_b1_script_isolated,
    validate_b1_script,
)


SAFE_SCRIPT = """from abaqus import mdb
from abaqusConstants import *
model = mdb.Model(name='TowerModel')
job = mdb.Job(name='tower_static', model='TowerModel')
job.writeInput()
"""


def test_b1_ast_gate_accepts_minimal_abaqus_model_script() -> None:
    result = validate_b1_script(SAFE_SCRIPT)
    assert result["status"] == "pass"
    assert result["issues"] == []
    assert result["ast_node_count"] > 0


def test_b1_ast_gate_rejects_process_file_network_and_path_escape() -> None:
    scripts = [
        "import os\nfrom abaqus import mdb\nos.system('whoami')\nmdb.saveAs(pathName='x.cae')",
        "from abaqus import mdb\nopen('secret.txt').read()\nmdb.saveAs(pathName='x.cae')",
        "import requests\nfrom abaqus import mdb\nrequests.get('https://example.com')\nmdb.saveAs(pathName='x.cae')",
        "from abaqus import mdb\nmdb.saveAs(pathName='../outside.cae')",
        "from abaqus import mdb\np='x.cae'\nmdb.saveAs(pathName=p)",
    ]
    for script in scripts:
        result = validate_b1_script(script)
        assert result["status"] == "blocked"
        assert result["issues"]


def test_b1_runner_never_starts_process_when_gate_blocks(tmp_path: Path) -> None:
    result = run_b1_script_isolated(
        "import os\nos.remove('x')",
        tmp_path / "staging",
        tmp_path / "artifacts",
        "definitely-not-an-abaqus-command",
    )
    assert result["status"] == "blocked_ast_gate"
    assert result["http_or_process_attempt_count"] == 0
    assert not (tmp_path / "artifacts").exists()


def test_scrubbed_environment_removes_credentials() -> None:
    scrubbed = _scrubbed_environment(
        {
            "PATH": "ok",
            "QWEN_API_KEY": "secret",
            "ACCESS_TOKEN": "secret",
            "DATABASE_PASSWORD": "secret",
            "NORMAL_VALUE": "keep",
        }
    )
    assert scrubbed == {"PATH": "ok", "NORMAL_VALUE": "keep"}
