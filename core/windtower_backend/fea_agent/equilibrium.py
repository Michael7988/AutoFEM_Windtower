from __future__ import annotations

import math
from typing import Any


EQUILIBRIUM_THRESHOLD = 0.01


def _vector(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        return [float(value[0]), float(value[1]), float(value[2])]
    except (TypeError, ValueError):
        return None


def _norm(value: list[float]) -> float:
    return math.sqrt(sum(component * component for component in value))


def evaluate_equilibrium(
    frozen_resultants: dict[str, Any] | None,
    reaction_resultants: dict[str, Any] | None,
    *,
    threshold: float = EQUILIBRIUM_THRESHOLD,
    epsilon: float = 1.0e-12,
    moment_reference_length_mm: float = 1.0,
) -> dict[str, Any]:
    """Evaluate force/moment closure without any Abaqus dependency.

    The moment denominator includes a small force-times-length floor.  This
    avoids a false relative-error failure when a nearly axisymmetric gravity
    field has a theoretical resultant moment close to zero.
    """
    frozen = frozen_resultants or {}
    reactions = (reaction_resultants or {}).get("steps") or {}
    steps: dict[str, Any] = {}
    blockers: list[dict[str, str]] = []
    for step_name, external in frozen.items():
        reaction = reactions.get(step_name) or {}
        force_ext = _vector(external.get("force_n")) if isinstance(external, dict) else None
        moment_ext = _vector(external.get("moment_nmm")) if isinstance(external, dict) else None
        force_reaction = _vector(reaction.get("force_n"))
        moment_reaction = _vector(reaction.get("moment_nmm"))
        missing = []
        if force_ext is None or moment_ext is None:
            missing.append("frozen_external_resultants")
        if force_reaction is None or not reaction.get("rf_available"):
            missing.append("RF")
        if moment_reaction is None or not reaction.get("rm_available"):
            missing.append("RM")
        if missing:
            item = {
                "status": "blocked",
                "missing": missing,
                "eta_F": None,
                "eta_M": None,
            }
            blockers.append({
                "step": step_name,
                "code": "EQUILIBRIUM_INPUT_MISSING",
                "message": "Missing equilibrium input: " + ", ".join(missing),
            })
            steps[step_name] = item
            continue
        force_residual = [force_ext[i] + force_reaction[i] for i in range(3)]
        moment_residual = [moment_ext[i] + moment_reaction[i] for i in range(3)]
        eta_f = _norm(force_residual) / max(_norm(force_ext), epsilon)
        moment_scale = max(
            _norm(moment_ext),
            _norm(force_ext) * max(float(moment_reference_length_mm), 0.0),
            epsilon,
        )
        eta_m = _norm(moment_residual) / moment_scale
        passed = eta_f < threshold and eta_m < threshold
        steps[step_name] = {
            "status": "pass" if passed else "blocked",
            "external_force_n": force_ext,
            "reaction_force_n": force_reaction,
            "force_residual_n": force_residual,
            "external_moment_nmm": moment_ext,
            "reaction_moment_nmm": moment_reaction,
            "moment_residual_nmm": moment_residual,
            "eta_F": eta_f,
            "eta_M": eta_m,
            "moment_normalization_nmm": moment_scale,
            "moment_reference_length_mm": moment_reference_length_mm,
            "threshold": threshold,
            "frame": reaction.get("frame"),
            "set": reaction.get("set"),
        }
        if not passed:
            blockers.append({
                "step": step_name,
                "code": "EQUILIBRIUM_RESIDUAL_EXCEEDED",
                "message": f"eta_F={eta_f:.6g}, eta_M={eta_m:.6g}, threshold={threshold:.6g}",
            })
    if not frozen:
        blockers.append({
            "step": "",
            "code": "FROZEN_LOAD_RESULTANTS_MISSING",
            "message": "No frozen external load resultants were supplied.",
        })
    return {
        "schema_version": "equilibrium_check_v2",
        "status": "blocked" if blockers else "pass",
        "threshold": threshold,
        "steps": steps,
        "blockers": blockers,
    }
