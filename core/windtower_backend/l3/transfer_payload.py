"""Lossless numeric transfer payload for the L3 parameter IR.

Large node histories are read deterministically from the frozen L2-derived INP;
Qwen extracts the bounded engineering scalars rather than transcribing 6,930
time/value pairs by hand.
"""
from __future__ import annotations

import hashlib
import json
import re


AMP = re.compile(r"(?im)^\*Amplitude, name=(L2H_\d+_[123]), time=STEP TIME\s*\n(.*?)(?=^\*|\Z)", re.S)
HOLD = re.compile(r"(?m)^PART-1-1\.(\d+), ([123]), [123], ([+-]?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?)$")
SERVICE = re.compile(r"(?m)^\*Boundary, op=NEW, amplitude=(L2H_\d+_[123])\s*\nPART-1-1\.(\d+), ([123]), [123], 1\.0$")


def from_inp(text: str) -> dict:
    amps = {}
    for name, body in AMP.findall(text):
        nums = [float(x) for x in body.replace("\n", ",").split(",") if x.strip()]
        if len(nums) != 22:
            raise ValueError("Amplitude %s has %d numbers" % (name, len(nums)))
        amps[name] = [[nums[i], nums[i+1]] for i in range(0, len(nums), 2)]
    parts = text.split("*Step, name=")
    steps = {p.split(",", 1)[0]: p for p in parts[1:]}
    hold = {}
    for step in ("STEP_1_BOLT_PRETENSION", "STEP_2_LOCK_PRETENSION"):
        hold[step] = {"%s_%s" % (node, dof): float(value)
                      for node,dof,value in HOLD.findall(steps[step])
                      if 23418 <= int(node) <= 23627}
    service = [[name, int(node), int(dof)] for name,node,dof in SERVICE.findall(steps["STEP_3_SERVICE_LOAD"])]
    if len(amps)!=630 or any(len(v)!=630 for v in hold.values()) or len(service)!=630:
        raise ValueError("Unexpected L3 transfer inventory")
    values = {"source": "L2 service and hold fields stored in frozen INP", "amplitudes": amps,
              "hold_boundaries": hold, "service_bindings": service}
    canonical=json.dumps(values,sort_keys=True,separators=(",", ":"))
    values["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return values
