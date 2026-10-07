from __future__ import annotations

from pathlib import Path

from windtower_backend.input_batches.advisors import EngineeringAdvisor, FemAdvisor
from windtower_backend.knowledge import KnowledgeBaseManager, LocalKnowledgeBase


class _RecordingProvider:
    def __init__(self, role: str) -> None:
        self.role = role
        self.prompt = ""

    def capabilities(self):
        return {"provider": "fake", "configured": True, "main_model": f"{self.role}-model"}

    def map_parameters(self, _evidence, _goal, mapping_prompt=None, target_keys=None):
        self.prompt = str(mapping_prompt or "")
        return {"status": "called", "provider": "fake", "model": f"{self.role}-model", "fields": []}

    def usage(self):
        return {"calls": 1}

    def connection_test(self):
        return {"status": "ready"}

    def extract_image_text(self, _content, _media_type, _prompt):
        return {"status": "not_called", "text": ""}


def test_dual_knowledge_bases_are_isolated_and_cited(tmp_path: Path) -> None:
    engineering_source = tmp_path / "engineering"
    fem_source = tmp_path / "fem"
    engineering_source.mkdir()
    fem_source.mkdir()
    (engineering_source / "tower-standard.md").write_text(
        "# 材料\n钢塔材料属性必须保留规范来源和单位。",
        encoding="utf-8",
    )
    (fem_source / "abaqus-manual.md").write_text(
        "# S4R\nS4R is a four-node reduced-integration shell element.",
        encoding="utf-8",
    )
    manager = KnowledgeBaseManager(tmp_path / "indexes")
    manager.engineering.rebuild([engineering_source])
    manager.fem.rebuild([fem_source])
    engineering_provider = _RecordingProvider("engineering")
    fem_provider = _RecordingProvider("fem")
    engineering = EngineeringAdvisor(engineering_provider, manager.engineering)
    fem = FemAdvisor(fem_provider, manager.fem)

    engineering_result = engineering.map_parameters(
        [], "钢塔材料", "engineering prompt", ["material.density"]
    )
    fem_result = fem.map_parameters(
        [], "S4R mesh", "fem prompt", ["mesh.element_type"]
    )

    assert engineering_result["knowledge_references"][0]["source_name"] == "tower-standard.md"
    assert fem_result["knowledge_references"][0]["source_name"] == "abaqus-manual.md"
    assert "four-node reduced-integration" not in engineering_provider.prompt
    assert "project-specific geometry or loads" in engineering_provider.prompt
    assert "must not override deterministic" in fem_provider.prompt


def test_missing_knowledge_index_degrades_without_failure(tmp_path: Path) -> None:
    knowledge = LocalKnowledgeBase("engineering", tmp_path)
    provider = _RecordingProvider("engineering")
    advisor = EngineeringAdvisor(provider, knowledge)

    result = advisor.map_parameters([], "tower", "prompt", ["material.name"])

    assert result["status"] == "called"
    assert result["knowledge_references"] == []
    assert result["knowledge_base"]["status"] == "not_indexed"
