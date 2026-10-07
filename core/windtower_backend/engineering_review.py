from __future__ import annotations

from typing import Any


class EngineeringReviewRequired(RuntimeError):
    def __init__(self, pending_keys: list[str]):
        self.pending_keys = pending_keys
        super().__init__(
            "以下参数仍需工程师确认后才能生成计算文件或开始计算："
            + "、".join(pending_keys)
        )


def assert_compute_ready(ir: dict[str, Any]) -> None:
    review = ir.get("engineering_review")
    if not isinstance(review, dict):
        return
    pending = [
        str(item.get("key"))
        for item in review.get("pending_parameters") or []
        if item.get("key")
    ]
    if pending:
        raise EngineeringReviewRequired(sorted(set(pending)))
    parameter_set = ir.get("parameter_set")
    if isinstance(parameter_set, dict):
        from .parameter_ir import ParameterSet, validate_parameter_set

        validation = validate_parameter_set(ParameterSet.model_validate(parameter_set))
        if validation["status"] != "pass":
            raise EngineeringReviewRequired([
                str(item.get("key") or item.get("message"))
                for item in validation["issues"]
            ])


def confirm_changed_parameters(ir: dict[str, Any], keys: list[str]) -> None:
    review = ir.get("engineering_review")
    if not isinstance(review, dict) or not keys:
        return
    changed = set(keys)
    review["pending_parameters"] = [
        item
        for item in review.get("pending_parameters") or []
        if str(item.get("key")) not in changed
    ]
    review["status"] = "confirmed" if not review["pending_parameters"] else "pending"
    editable = ir.get("editable_model_ir") or {}
    editable.setdefault("provenance", {})["engineering_review_status"] = review["status"]
