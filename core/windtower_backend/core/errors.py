from __future__ import annotations


def excel_upload_error(category: str, message: str, filename: str, hint: str | None = None) -> dict[str, object]:
    return {
        "category": category,
        "message": message,
        "filename": filename,
        "supported_formats": [".xls", ".xlsx", ".xlsm"],
        "hint": hint or "Please upload a wind tower mechanical interface workbook with tower, door, and FEM parameter sheets.",
    }


def classify_excel_exception(filename: str, exc: Exception) -> dict[str, object]:
    text = str(exc)
    lowered = text.lower()
    if "unsupported workbook format" in lowered:
        return excel_upload_error("unsupported_format", text, filename)
    if "no tower profile sheet" in lowered or ("tower" in lowered and "required" in lowered):
        return excel_upload_error("missing_tower_sheet", text, filename, "Check that the workbook includes a tower section/profile sheet.")
    if "door" in lowered:
        return excel_upload_error("missing_or_invalid_door_sheet", text, filename, "Check that the workbook includes door opening dimensions X2/Y2/H.")
    if "fem" in lowered or "material" in lowered:
        return excel_upload_error("missing_or_invalid_fem_params", text, filename, "Check the FEM parameter sheet for material, element, mesh, load, and boundary fields.")
    return excel_upload_error("excel_parse_failed", text, filename)
