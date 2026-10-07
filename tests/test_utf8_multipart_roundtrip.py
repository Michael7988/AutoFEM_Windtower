from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from windtower_backend.api.input_batches import create_input_batches_router
from windtower_backend.config import load_settings
from windtower_backend.core.pipeline import WindTowerPipeline
from windtower_backend.input_batches.service import EngineeringInputBatchService
from windtower_backend.persistence import MetadataStore


def _service(root):
    service = EngineeringInputBatchService(WindTowerPipeline(load_settings()))
    service.root = root
    service.cache_root = root / "_cache"
    service.root.mkdir(parents=True, exist_ok=True)
    service.cache_root.mkdir(parents=True, exist_ok=True)
    service._batches.clear()
    service.metadata = MetadataStore(root / "metadata.sqlite3")
    service._load_existing()
    return service


def _client(service):
    app = FastAPI()
    app.include_router(create_input_batches_router(service))
    return TestClient(app)


def test_chinese_multipart_json_persistence_and_get_roundtrip(tmp_path):
    root = tmp_path / "输入批次"
    goal = "建立带门洞风塔 S4R 壳模型，并执行线弹性静力与模态分析。"
    filename = "中国海装塔筒参数.xlsx.txt"
    client = _client(_service(root))
    response = client.post(
        "/api/input-batches",
        data={
            "modeling_goal": goal,
            "parse_mode": "deterministic",
            "llm_execution_mode": "single",
        },
        files={"files": (filename, "门洞宽度 1500 mm".encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    for _ in range(100):
        payload = client.get(f"/api/input-batches/{batch_id}").json()
        if payload["status"] not in {"queued", "processing"}:
            break
        time.sleep(0.02)
    assert payload["modeling_goal"] == goal
    assert payload["files"][0]["filename"] == filename

    restarted = _client(_service(root))
    restored = restarted.get(f"/api/input-batches/{batch_id}")
    assert restored.status_code == 200
    assert restored.json()["modeling_goal"] == goal
    assert restored.json()["files"][0]["filename"] == filename
