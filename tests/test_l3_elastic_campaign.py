"""Regression checks for the distinct L3 capability and the repaired gate."""
import json
import hashlib
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "experiments/F02_L3_V6_ELASTIC/qwen_l3_experiment_v2"))

from windtower_backend.l3.parameter_ir import reference_ir, validate
from repaired_mapper import compile_model

INP = ROOT / "experiments/F02_L3_V6_ELASTIC/F02_L3_V5_R2_ELASTIC_PROBE.inp"


class TestL3ElasticCampaign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INP.read_text(encoding="latin1")
        cls.ir = reference_ir(INP)
        cls.full_ir = json.loads((ROOT / "experiments/F02_L3_V6_ELASTIC/qwen_l3_experiment_v2/full_parameter_ir.json").read_text(encoding="utf-8"))

    def test_reference_ir_is_complete_and_evidence_backed(self):
        check = validate(self.ir, self.ir)
        self.assertTrue(check["pass"])
        self.assertEqual(self.ir["fields"]["bolt.pretension_n"]["value"], 1688400.0)
        self.assertEqual(self.ir["fields"]["transfer.amplitude_count"]["value"], 630)

    def test_repaired_mapping_rejects_corrupt_template(self):
        compile_model(self.full_ir, self.text)
        corrupted = self.text.replace("*Amplitude, name=L2H_23418_1, time=STEP TIME", "** removed amplitude", 1)
        with self.assertRaises(ValueError):
            compile_model(self.full_ir, corrupted)

    def test_transfer_history_is_driven_by_ir(self):
        changed = copy.deepcopy(self.full_ir)
        changed["transfer_payload"]["amplitudes"]["L2H_23418_1"][-1][1] += 0.125
        bare = {k:v for k,v in changed["transfer_payload"].items() if k != "sha256"}
        changed["transfer_payload"]["sha256"] = hashlib.sha256(json.dumps(bare,sort_keys=True,separators=(",", ":")).encode("utf-8")).hexdigest()
        generated = compile_model(changed, self.text)
        self.assertIn("L2H_23418_1", generated)
        self.assertNotEqual(generated, compile_model(self.full_ir, self.text))
        self.assertIn("-5.75368848011", generated)


if __name__ == "__main__":
    unittest.main()
