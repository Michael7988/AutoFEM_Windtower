from __future__ import annotations

import unittest

from windtower_backend.qwen_client.implementation import _normalize_patch_item


class QwenClientTest(unittest.TestCase):
    def test_normalizes_llm_dot_paths_to_json_pointer(self) -> None:
        item = {"op": "replace", "path": "semantic_ir.materials.0.name", "value": "Q345", "label": "material"}

        self.assertEqual(_normalize_patch_item(item)["path"], "/semantic_ir/materials/0/name")

    def test_preserves_json_pointer_paths(self) -> None:
        item = {"op": "replace", "path": "/semantic_ir/materials/0/name", "value": "Q345", "label": "material"}

        self.assertEqual(_normalize_patch_item(item)["path"], "/semantic_ir/materials/0/name")


if __name__ == "__main__":
    unittest.main()
