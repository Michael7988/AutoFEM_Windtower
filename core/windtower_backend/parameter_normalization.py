from __future__ import annotations

from typing import Any


class ParameterNormalizationError(ValueError):
    """Raised when a registered parameter cannot be safely normalized."""


_DIRECTION_ALIASES = {
    "1": "+X",
    "1.0": "+X",
    "+1": "+X",
    "+1.0": "+X",
    "DOF1": "+X",
    "U1": "+X",
    "X": "+X",
    "+X": "+X",
    "GLOBALX": "+X",
    "GLOBAL_X": "+X",
    "正X": "+X",
    "X正向": "+X",
    "-1": "-X",
    "-1.0": "-X",
    "-X": "-X",
    "负X": "-X",
    "X负向": "-X",
    "2": "+Y",
    "2.0": "+Y",
    "+2": "+Y",
    "+2.0": "+Y",
    "DOF2": "+Y",
    "U2": "+Y",
    "Y": "+Y",
    "+Y": "+Y",
    "GLOBALY": "+Y",
    "GLOBAL_Y": "+Y",
    "正Y": "+Y",
    "Y正向": "+Y",
    "-2": "-Y",
    "-2.0": "-Y",
    "-Y": "-Y",
    "负Y": "-Y",
    "Y负向": "-Y",
    "3": "+Z",
    "3.0": "+Z",
    "+3": "+Z",
    "+3.0": "+Z",
    "DOF3": "+Z",
    "U3": "+Z",
    "Z": "+Z",
    "+Z": "+Z",
    "GLOBALZ": "+Z",
    "GLOBAL_Z": "+Z",
    "正Z": "+Z",
    "Z正向": "+Z",
    "-3": "-Z",
    "-3.0": "-Z",
    "-Z": "-Z",
    "负Z": "-Z",
    "Z负向": "-Z",
}


def normalize_parameter_value(key: str, value: Any) -> tuple[Any, str | None]:
    """Return the canonical value and an auditable conversion description."""
    if key != "loads.top_force_direction":
        return value, None
    if value is None or (isinstance(value, str) and not value.strip()):
        return value, None
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            vector = tuple(float(item) for item in value)
        except (TypeError, ValueError):
            vector = ()
        vector_aliases = {
            (1.0, 0.0, 0.0): "+X", (-1.0, 0.0, 0.0): "-X",
            (0.0, 1.0, 0.0): "+Y", (0.0, -1.0, 0.0): "-Y",
            (0.0, 0.0, 1.0): "+Z", (0.0, 0.0, -1.0): "-Z",
        }
        canonical = vector_aliases.get(vector)
        if canonical is None:
            raise ParameterNormalizationError(
                "顶部力方向向量必须为六个全局轴向单位向量之一：±X、±Y、±Z。"
            )
        return canonical, f"{list(value)} → {canonical}"
    raw = str(value).strip()
    compact = raw.upper().replace(" ", "").replace("方向", "")
    canonical = _DIRECTION_ALIASES.get(compact)
    if canonical is None:
        raise ParameterNormalizationError(
            "顶部力方向无法识别，请选择 +X、-X、+Y、-Y、+Z 或 -Z。"
        )
    conversion = None if raw == canonical else f"{raw} → {canonical}"
    return canonical, conversion
