from __future__ import annotations

from windtower_backend.benchmark import e5_e7_experiment as experiment


def _evidence():
    return {
        "sheets": [
            {
                "cells": [
                    {"cell": "S01!R1C1", "value": "厚度"},
                    {"cell": "S01!R1C2", "value": 20.0},
                    {"cell": "S02!R1C1", "value": "推力"},
                    {"cell": "S02!R1C2", "value": 330000.0},
                    {"cell": "S02!R1C3", "value": "N"},
                    {"cell": "S03!R1C1", "value": "外椭圆长轴Y1"},
                    {"cell": "S03!R1C2", "value": 2600.0},
                    {"cell": "S03!R2C1", "value": "内椭圆长轴Y2"},
                    {"cell": "S03!R2C2", "value": 2300.0},
                    {"cell": "S04!R1C1", "value": "mm"},
                ]
            }
        ]
    }


def test_all_e5_variants_are_non_mutating_and_auditable():
    original = _evidence()
    for condition in experiment.CONDITIONS:
        derived, mutations = experiment._mutate(original, condition)
        assert derived is not original
        assert original == _evidence()
        if condition == "C01_clean":
            assert mutations == []
        else:
            assert mutations, condition


def test_conflict_and_door_variants_create_the_intended_evidence_fault():
    conflict, _ = experiment._mutate(_evidence(), "C11_source_conflict")
    assert any(str(cell.get("cell", "")).endswith("_CONFLICT") for cell in experiment._cells(conflict))
    door, _ = experiment._mutate(_evidence(), "C12_door_conflict")
    values = {cell["cell"]: cell["value"] for cell in experiment._cells(door)}
    assert values["S03!R2C2"] > values["S03!R1C2"]


def test_equivalent_presentation_variants_are_auditable_and_non_mutating():
    original = _evidence()
    for condition in ("C13_header_alias", "C14_layout_reorder", "C15_irrelevant_noise"):
        derived, mutations = experiment._mutate(original, condition)
        assert mutations, condition
        assert derived is not original
        assert original == _evidence()
