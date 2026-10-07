from __future__ import annotations

from windtower_backend.l0.logs import stage_has_fatal_error


def test_sim_database_failure_is_fatal_even_when_wrapper_text_is_mixed() -> None:
    text = """
    Abaqus JOB example_dc COMPLETED
    Abaqus/Standard Analysis exited with an error
    ***ERROR: SIM database file cannot be opened - No such file
    THE PROGRAM HAS DISCOVERED 1 FATAL ERRORS
    """

    assert stage_has_fatal_error(text)


def test_completed_job_without_fatal_marker_is_not_fatal() -> None:
    assert not stage_has_fatal_error("Abaqus JOB example_dc COMPLETED WITH 1 WARNING")
