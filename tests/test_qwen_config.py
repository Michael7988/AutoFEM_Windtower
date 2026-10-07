from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from windtower_backend.config import load_llm_api_config


class QwenConfigTest(unittest.TestCase):
    def test_loads_private_qwen_yaml_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_api_config.yaml"
            path.write_text(
                """
qwen:
  api_key: "sk-test"
  model: "qwen-max"
  base_url: "https://dashscope.aliyuncs.com/api/v1"
  temperature: 0.2
  max_tokens: 128
  timeout: 30
""",
                encoding="utf-8",
            )
            data = load_llm_api_config(path)

        self.assertEqual(data["qwen"]["api_key"], "sk-test")
        self.assertEqual(data["qwen"]["model"], "qwen-max")
        self.assertEqual(data["qwen"]["temperature"], 0.2)
        self.assertEqual(data["qwen"]["max_tokens"], 128)


if __name__ == "__main__":
    unittest.main()
