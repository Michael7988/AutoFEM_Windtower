from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .door_derivation import derive_door_candidates
from .fem_facts import find_fem_sheet, parse_fem_params as parse_fem_params_typed
from .parse_result import ExcelParseResult, build_confidence_report, build_llm_suggestions, build_parsed_facts, build_raw_tables
from .standard_input import detect_workbook_format, normalize_import_to_standard_input
from .validation import validate_import_facts
from .workbook_reader import SheetData, WorkbookData, read_workbook_data


DOOR_SYMBOLS = {
    "外椭圆长轴Y1": "Y1",
    "外椭圆短轴X1": "X1",
    "内椭圆长轴Y2": "Y2",
    "内椭圆短轴X2": "X2",
    "中间距D": "D",
    "厚度W": "W",
    "与底法兰端面距离H": "H",
    "与塔筒中心线距离P": "P",
}


def parse_windtower_workbook(path: str | Path, load_paths: list[str | Path] | None = None) -> ExcelParseResult:
    workbook = read_workbook_data(path)
    format_report = detect_workbook_format(workbook)
    if format_report["input_format"] in {"semantic_standard_workbook", "windtower_mechanical_interface", "unknown_llm_assisted"}:
        normalized = normalize_import_to_standard_input(path)
        if load_paths:
            normalized["loads_inventory"] = {
                **(normalized.get("loads_inventory") or {}),
                "attached_load_workbooks": parse_load_workbooks(load_paths),
            }
        return normalized

    tower_sheet = find_sheet(workbook, ["塔节", "塔筒"])
    flange_sheet = find_sheet(workbook, ["法兰"])
    door_sheet = find_sheet(workbook, ["门洞", "门框"])

    tower = parse_tower_sheet(tower_sheet) if tower_sheet else []
    flanges = parse_flange_sheet(flange_sheet) if flange_sheet else []
    door = parse_door_sheet(door_sheet) if door_sheet else {}
    fem_params, fem_param_records, fem_model_facts = parse_fem_params(workbook)
    loads_inventory = parse_load_workbooks(load_paths or [])
    fem_sheet = find_fem_sheet(workbook)
    door_derivation = derive_door_candidates(tower, door)

    sheet_map = {
        "tower": tower_sheet.name if tower_sheet else None,
        "flange": flange_sheet.name if flange_sheet else None,
        "door": door_sheet.name if door_sheet else None,
        "fem_params": fem_sheet.name if fem_sheet else None,
    }
    missing_items, warnings = validate_import_facts(tower, flanges, door, fem_params, loads_inventory)
    if not tower_sheet:
        missing_items.append({"item": "tower_sheet", "reason": "No tower profile sheet was detected."})
    if not flange_sheet:
        warnings.append("No flange sheet was detected.")
    if not door_sheet:
        missing_items.append({"item": "door_sheet", "reason": "No door geometry sheet was detected."})
    parsed_facts = build_parsed_facts(tower, flanges, door, fem_params, loads_inventory)
    confidence_report = build_confidence_report(sheet_map, tower, flanges, door, fem_params, missing_items, warnings)
    llm_suggestions = build_llm_suggestions(sheet_map, fem_params, missing_items, confidence_report)

    return {
        "source_file": Path(path).name,
        "source_files": [Path(path).name, *[Path(item).name for item in load_paths or []]],
        "detected_format": "windtower_mechanical_interface",
        "sheet_map": sheet_map,
        "raw_tables": build_raw_tables(workbook),
        "parsed_facts": parsed_facts,
        "tower": tower,
        "flanges": flanges,
        "door": door,
        "fem_params": fem_params,
        "fem_param_records": fem_param_records,
        "fem_model_facts": fem_model_facts,
        "derived_facts": {"door_derivation": door_derivation},
        "standard_references": standard_references_for_import(door_derivation),
        "loads_inventory": loads_inventory,
        "missing_items": missing_items,
        "warnings": warnings,
        "llm_suggestions": llm_suggestions,
        "confidence_report": confidence_report,
    }


def parse_load_workbooks(paths: list[str | Path]) -> dict[str, Any]:
    summaries = []
    fatigue = []
    occurrences = []
    warnings = []
    for path_like in paths:
        path = Path(path_like)
        try:
            workbook = read_workbook_data(path)
        except Exception as exc:
            warnings.append(f"{path.name}: {exc}")
            continue
        if "次数" in path.name:
            occurrences.extend(parse_occurrence_workbook(workbook, path.name))
        else:
            summaries.extend(parse_ultimate_load_workbook(workbook, path.name))
            fatigue.extend(parse_fatigue_workbook(workbook, path.name))
        if not summaries and not fatigue and "次数" not in path.name and "极限" in path.name:
            warnings.append(f"{path.name}: no load rows were recognized.")
        if "次数" in path.name and not occurrences:
            warnings.append(f"{path.name}: no occurrence rows were recognized.")

    return {
        "ultimate_cases": summaries,
        "fatigue_equivalent": fatigue,
        "occurrences": occurrences[:20],
        "occurrence_count": len(occurrences),
        "source": "load_workbooks" if paths else "missing",
        "warnings": warnings,
    }


def parse_tower_sheet(sheet: SheetData) -> list[dict[str, Any]]:
    header_row = find_header_row(sheet, [["序号"], ["高度"], ["中径", "直径"], ["壁厚"]])
    if header_row is None:
        return []
    headers = [norm(sheet.cell(header_row, col)) for col in range(sheet.ncols)]
    index_col = find_col(headers, ["序号", "index"])
    z_col = find_col(headers, ["高度", "z"])
    diameter_col = find_col(headers, ["中径", "直径", "dt", "diameter"])
    length_col = find_col(headers, ["对应截面长度", "截面长度", "长度", "length"])
    thickness_col = find_col(headers, ["壁厚", "厚度t", "thickness"])
    if z_col is None or diameter_col is None or thickness_col is None:
        return []

    rows: list[dict[str, Any]] = []
    for row in range(header_row + 1, sheet.nrows):
        index_value = sheet.cell(row, index_col) if index_col is not None else len(rows)
        z_value = number(sheet.cell(row, z_col))
        diameter = number(sheet.cell(row, diameter_col))
        thickness = number(sheet.cell(row, thickness_col))
        if z_value is None and diameter is None and thickness is None:
            continue
        if z_value is None or diameter is None or thickness is None:
            continue
        rows.append(
            {
                "index": int(number(index_value) if number(index_value) is not None else len(rows)),
                "z_mm": z_value * 1000.0 if z_value < 1000 else z_value,
                "diameter_mm": diameter,
                "length_mm": number(sheet.cell(row, length_col)) if length_col is not None else None,
                "thickness_mm": thickness,
                "source": f"{sheet.name}!R{row + 1}",
            }
        )
    return rows


def parse_flange_sheet(sheet: SheetData) -> list[dict[str, Any]]:
    header_row = find_header_row(sheet, [["序号"], ["中径", "直径"], ["螺孔数量", "螺栓数量"]])
    if header_row is None:
        return []
    headers = [norm(sheet.cell(header_row, col)) for col in range(sheet.ncols)]
    index_col = find_col(headers, ["序号", "index"])
    diameter_col = find_col(headers, ["中径da", "中径", "直径", "diameter"])
    bolt_circle_col = find_col(headers, ["分布圆直径", "螺栓圆", "boltcircle"])
    inner_col = find_col(headers, ["内径df", "内径"])
    clamp_col = find_col(headers, ["夹持厚度", "夹持区高度", "厚度h"])
    neck_col = find_col(headers, ["颈部厚度", "颈部壁厚", "主体厚度"])
    total_col = find_col(headers, ["总厚度", "法兰总高度", "厚度h", "total"])
    bolt_count_col = find_col(headers, ["螺孔数量", "螺栓数量", "数量n"])
    bolt_diameter_col = find_col(headers, ["螺孔直径", "螺栓直径", "直径d"])

    rows: list[dict[str, Any]] = []
    for row in range(header_row + 1, sheet.nrows):
        index = number(sheet.cell(row, index_col)) if index_col is not None else len(rows) + 1
        diameter = number(sheet.cell(row, diameter_col)) if diameter_col is not None else None
        if index is None or diameter is None:
            continue
        total = number(sheet.cell(row, total_col)) if total_col is not None else None
        rows.append(
            {
                "index": int(index),
                "diameter_mm": diameter,
                "bolt_circle_mm": number(sheet.cell(row, bolt_circle_col)) if bolt_circle_col is not None else None,
                "inner_diameter_mm": number(sheet.cell(row, inner_col)) if inner_col is not None else None,
                "clamp_thickness_mm": number(sheet.cell(row, clamp_col)) if clamp_col is not None else None,
                "neck_thickness_mm": number(sheet.cell(row, neck_col)) if neck_col is not None else None,
                "total_thickness_mm": total,
                "bolt_count": int(number(sheet.cell(row, bolt_count_col)) or 0) if bolt_count_col is not None else 0,
                "bolt_diameter_mm": number(sheet.cell(row, bolt_diameter_col)) if bolt_diameter_col is not None else None,
                "source": f"{sheet.name}!R{row + 1}",
            }
        )
    return rows


def parse_door_sheet(sheet: SheetData) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for row in range(sheet.nrows):
        row_values = [sheet.cell(row, col) for col in range(sheet.ncols)]
        text_values = [str(value).strip() for value in row_values if str(value).strip()]
        if not text_values:
            continue
        symbol = find_door_symbol(text_values)
        if not symbol:
            continue
        numeric_values = [strict_number(value) for value in row_values]
        numeric_values = [value for value in numeric_values if value is not None]
        if numeric_values:
            values[symbol] = numeric_values[-1]
    return values


def parse_fem_params(workbook: WorkbookData) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    return parse_fem_params_typed(workbook)


def parse_ultimate_load_workbook(workbook: WorkbookData, source_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sheet in workbook.sheets.values():
        if not any("loadcase" in norm(sheet.cell(row, col)) or "load case" in str(sheet.cell(row, col)).lower() for row in range(min(sheet.nrows, 5)) for col in range(sheet.ncols)):
            continue
        header_row = 1 if sheet.nrows > 1 else 0
        headers = [str(sheet.cell(0, col)).strip() or str(sheet.cell(header_row, col)).strip() for col in range(sheet.ncols)]
        load_case_col = find_col([norm(item) for item in headers], ["loadcase", "工况"])
        if load_case_col is None:
            load_case_col = 2
        component_cols = {
            "Mx_kNm": find_col([norm(item) for item in headers], ["mx"]),
            "My_kNm": find_col([norm(item) for item in headers], ["my"]),
            "Mxy_kNm": find_col([norm(item) for item in headers], ["mxy"]),
            "Mz_kNm": find_col([norm(item) for item in headers], ["mz"]),
            "Fx_kN": find_col([norm(item) for item in headers], ["fx"]),
            "Fy_kN": find_col([norm(item) for item in headers], ["fy"]),
            "Fz_kN": find_col([norm(item) for item in headers], ["fz"]),
        }
        for row in range(2, sheet.nrows):
            load_case = str(sheet.cell(row, load_case_col)).strip()
            if not load_case:
                continue
            item: dict[str, Any] = {
                "source_file": source_name,
                "sheet": sheet.name,
                "component": str(sheet.cell(row, 0)).strip(),
                "extreme": str(sheet.cell(row, 1)).strip(),
                "load_case": load_case,
            }
            for key, col in component_cols.items():
                if col is not None:
                    item[key] = number(sheet.cell(row, col))
            rows.append(item)
    return rows


def parse_fatigue_workbook(workbook: WorkbookData, source_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sheet in workbook.sheets.values():
        if "疲劳" not in sheet.name:
            continue
        for row in range(sheet.nrows):
            for col in range(sheet.ncols):
                label = str(sheet.cell(row, col)).strip()
                if "tower" in label.lower() or "section" in label.lower() or "疲劳" in label:
                    value = number(sheet.cell(row + 1, col)) if row + 1 < sheet.nrows else None
                    if value is not None:
                        rows.append({"source_file": source_name, "sheet": sheet.name, "label": label, "value": value})
    return rows


def parse_occurrence_workbook(workbook: WorkbookData, source_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sheet in workbook.sheets.values():
        for row in range(1, sheet.nrows):
            case = str(sheet.cell(row, 0)).strip()
            count = number(sheet.cell(row, 1))
            if case and count is not None:
                rows.append({"source_file": source_name, "sheet": sheet.name, "load_case": case, "count": count})
    return rows


def find_sheet(workbook: WorkbookData, keywords: list[str]) -> SheetData | None:
    for name, sheet in workbook.sheets.items():
        normalized = norm(name)
        if all(norm(keyword) in normalized for keyword in keywords[:1]) or any(norm(keyword) in normalized for keyword in keywords):
            return sheet
    return None


def find_header_row(sheet: SheetData, groups: list[list[str]]) -> int | None:
    for row in range(sheet.nrows):
        text = " ".join(norm(sheet.cell(row, col)) for col in range(sheet.ncols))
        if all(any(norm(keyword) in text for keyword in group) for group in groups):
            return row
    return None


def find_col(headers: list[str], aliases: list[str]) -> int | None:
    normalized_aliases = [norm(alias) for alias in aliases]
    for col, header in enumerate(headers):
        if any(alias and alias in header for alias in normalized_aliases):
            return col
    return None


def find_door_symbol(values: list[str]) -> str | None:
    joined = " ".join(values)
    for label, symbol in DOOR_SYMBOLS.items():
        if label in joined or symbol in values:
            return symbol
    for value in values:
        cleaned = value.strip()
        if cleaned in {"Y1", "X1", "Y2", "X2", "D", "W", "H", "P"}:
            return cleaned
    return None


def parse_fem_value(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    text = str(value).strip()
    if text == "":
        return None
    if "," in text and not re.search(r"[A-Za-z_]+,", text):
        return [parse_fem_value(item) for item in text.split(",")]
    parsed = number(text)
    return parsed if parsed is not None else text


def standard_references_for_import(door_derivation: dict[str, Any]) -> list[dict[str, Any]]:
    references = [
        {
            "id": "manual_rule:fem_parser_typed_values",
            "type": "manual_rule",
            "title": "Typed FEM key-value parsing",
            "note": "Engineering identifiers such as Q345, S4R, L2, NO and Abaqus set names are preserved as strings.",
        },
        {
            "id": "manual_rule:llm_advisory_only",
            "type": "manual_rule",
            "title": "LLM advisory parsing policy",
            "note": "LLM suggestions are patch candidates only and must not overwrite deterministic parsed facts directly.",
        },
    ]
    for reference_id in door_derivation.get("standard_references", []):
        references.append(
            {
                "id": reference_id,
                "type": "manual_rule",
                "title": reference_id,
                "note": "Door derivation and candidate comparison policy.",
            }
        )
    return references


def number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def strict_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def norm(value: Any) -> str:
    return re.sub(r"[\s（）()_\-./]+", "", str(value).strip().lower())
