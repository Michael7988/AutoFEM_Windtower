from __future__ import annotations

from core.windtower_backend.benchmark.validation_feedback_experiment import (
    _equilibrium_boundary_tests,
    _fault_degenerate_element,
    _fault_empty_boundary_set,
    _fault_missing_section,
    _fault_missing_surface,
    _fault_syntax,
)


FIXTURE = """*Heading
*Node
1, 1., 0., 0.
2, 0., 1., 0.
*Element, type=S4R
1, 1, 2, 2, 1
*Shell Section, elset=SEG_001, material=STEEL
10., 5
*Step, name=S
*Static
*Boundary
BASE_FIXED, 1, 6
*Dsload
WINDWARD_SURFACE, P, 0.1
*End Step
"""


def test_fault_mutations_are_targeted() -> None:
    assert "*Statik" in _fault_syntax(FIXTURE)
    assert "UNDEFINED_BASE_SET" in _fault_empty_boundary_set(FIXTURE)
    assert "UNDEFINED_WIND_SURFACE" in _fault_missing_surface(FIXTURE)
    assert "1, 1, 1, 1, 1" in _fault_degenerate_element(FIXTURE)
    assert "*Shell Section" not in _fault_missing_section(FIXTURE)


def test_equilibrium_boundary_matrix_is_exact() -> None:
    rows = _equilibrium_boundary_tests()
    assert len(rows) == 4
    assert all(row["pass"] for row in rows)
