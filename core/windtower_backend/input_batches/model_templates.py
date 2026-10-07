from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates"
SAFE_MAPPING_PROMPT = """You map engineering evidence into parameter candidates only.
Never create formal IR, mesh, solver data or Abaqus INP.
Return JSON only with schema_version=candidate_mapping_ir_v2 and fields.
Each field must contain path, normalized_value, normalized_unit, source_kind,
confidence, evidence_ids, mapping_reason and assumption_basis.
source_kind must be file_observed, goal_explicit or llm_suggested.
Use supplied evidence IDs for observed values. An llm_suggested field must have
an empty evidence_ids list and a clear assumption_basis. Never fabricate evidence.
loads.top_force_direction must be exactly one of +X, -X, +Y, -Y, +Z or -Z."""


class ModelTemplateRegistry:
    def __init__(self, root: Path = TEMPLATE_ROOT):
        self.root = root
        self._templates = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        templates: dict[str, dict[str, Any]] = {}
        for path in sorted(self.root.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"Invalid model template: {path.name}")
            self._validate_template(payload, path.name)
            template_id = str(payload.get("template_id") or "")
            if not template_id or template_id in templates:
                raise ValueError(f"Invalid or duplicate template id: {template_id}")
            payload["source_file"] = path.name
            payload["prompt_hash"] = self.prompt_hash(payload)
            templates[template_id] = payload
        return templates

    def list_public(self) -> dict[str, Any]:
        canonical = self._templates.get("wind_tower_shell_door_v1") or {}
        return {
            "schema_version": "engineering_model_template_registry_v1",
            "supported_analysis_tasks": ["static", "modal"],
            "supported_features": ["tower_shell", "door_opening", "local_refinement", "top_mass", "wind_load"],
            "structure_adapter": {
                "id": "wind_tower_s4r",
                "structure_family": "wind_tower_shell_with_door",
                "element_type": "S4R",
                "analysis_tasks": ["static", "modal"],
            },
            "templates": [
                {
                    **{
                        key: template.get(key)
                        for key in (
                            "template_id", "version", "model_family", "display_name",
                            "description", "fidelity", "adapter", "prompt_hash",
                        )
                    },
                    "parameter_count": len(template.get("parameters") or []),
                    "analysis_tasks": ["static", "modal"],
                    "features": (
                        ["tower_shell", "door_opening", "top_mass", "wind_load"]
                        + (["local_refinement"] if template.get("fidelity") == "detailed" else [])
                    ),
                }
                for template in self._templates.values()
                if template.get("selectable", True)
            ],
            "canonical_engineering_template": {
                key: canonical.get(key)
                for key in (
                    "template_id", "version", "model_family", "display_name",
                    "description", "adapter", "supported_analysis_tasks",
                    "unsupported_features", "prompt_hash",
                )
            },
        }

    def get(self, template_id: str, version: str | None = None) -> dict[str, Any]:
        template = self._templates.get(template_id)
        if template is None:
            raise ValueError(f"未知模型模板：{template_id}")
        if version and str(template.get("version")) != version:
            raise ValueError(f"模型模板版本不匹配：{template_id}@{version}")
        return template

    def recommend(self, modeling_goal: str | None, filenames: list[str]) -> dict[str, Any]:
        text = f"{modeling_goal or ''} {' '.join(filenames)}".lower()
        detailed_tokens = ("精细", "细化", "局部", "详细", "高精度", "detailed", "refine")
        recommended = "wind_tower_detailed_v1" if any(token in text for token in detailed_tokens) else "wind_tower_quick_v1"
        alternatives = [
            key for key, template in self._templates.items()
            if key != recommended and template.get("selectable", True)
        ]
        return {
            "recommended_template_id": recommended,
            "alternative_template_ids": alternatives,
            "reason": "目标中包含精细化或局部建模要求。" if recommended.endswith("detailed_v1") else "当前目标适合先建立快速整体模型。",
            "source": "bounded_goal_classifier",
            "requires_confirmation": True,
        }

    def mapping_prompt(self, template: dict[str, Any]) -> str:
        contract = [
            {
                "key": item.get("key"),
                "label": item.get("label"),
                "type": item.get("type"),
                "unit": item.get("unit"),
                "suggestible": bool(item.get("suggestible")),
            }
            for item in template.get("parameters") or []
        ]
        return "\n".join((
            SAFE_MAPPING_PROMPT,
            str(template.get("prompt_instructions") or ""),
            "Template parameter contract:",
            json.dumps(contract, ensure_ascii=False, separators=(",", ":")),
        ))

    def ui_schema(self, template_id: str, available_parameter_keys: list[str] | None = None) -> dict[str, Any]:
        template = self.get(template_id)
        sections = []
        parameters = template.get("parameters") or []
        available = available_parameter_keys or []
        for group in dict.fromkeys(str(item.get("group") or "other") for item in parameters):
            group_items = [
                item for item in parameters
                if item.get("group") == group and item.get("type") != "collection"
            ]
            keys = [item["key"] for item in group_items if item["key"] in available or not available]
            if not keys:
                continue
            sections.append({
                "id": group,
                "title": {
                    "geometry": "几何与门洞",
                    "material": "材料",
                    "mesh": "网格设置",
                    "loads": "载荷",
                    "analysis": "分析设置",
                }.get(group, group),
                "layout": "form",
                "parameter_keys": keys,
                "primary_parameter_keys": [item["key"] for item in group_items if item.get("display_tier") == "primary" and item["key"] in keys],
                "collapsed": all(item.get("display_tier") != "primary" for item in group_items),
            })
        section_keys = [key for key in available if key.startswith("geometry.tower_sections.")]
        if section_keys:
            sections.insert(1 if sections else 0, {
                "id": "tower_sections",
                "title": "塔筒分段",
                "description": "按高度检查每一段的直径和厚度。",
                "layout": "collection_table",
                "parameter_keys": section_keys,
                "collection": {
                    "key_pattern": r"geometry\.tower_sections\.(\d+)\.(.+)",
                    "index_label": "段",
                    "columns": [
                        {"field": "z_bottom_mm", "label": "底部标高", "unit": "mm"},
                        {"field": "z_top_mm", "label": "顶部标高", "unit": "mm"},
                        {"field": "diameter_bottom_mm", "label": "底部直径", "unit": "mm"},
                        {"field": "diameter_top_mm", "label": "顶部直径", "unit": "mm"},
                        {"field": "thickness_mm", "label": "厚度", "unit": "mm"},
                    ],
                },
            })
        return {
            "schema_version": "parameter_ui_schema_v2",
            "template_id": template_id,
            "template_version": template["version"],
            "sections": sections,
        }

    @staticmethod
    def prompt_hash(template: dict[str, Any]) -> str:
        body = f"{SAFE_MAPPING_PROMPT}\n{template.get('prompt_instructions')}\n{json.dumps(template.get('parameters') or [], ensure_ascii=False, sort_keys=True)}"
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_template(payload: dict[str, Any], filename: str) -> None:
        required = ("schema_version", "template_id", "version", "model_family", "adapter")
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise ValueError(f"Model template {filename} is missing: {missing}")
        parameters = payload.get("parameters") or []
        if not isinstance(parameters, list):
            raise ValueError(f"Model template {filename} parameters must be a list")
        keys = [str(item.get("key") or "") for item in parameters if isinstance(item, dict)]
        if any(not key for key in keys) or len(keys) != len(parameters):
            raise ValueError(f"Model template {filename} contains an invalid parameter")
        if len(keys) != len(set(keys)):
            raise ValueError(f"Model template {filename} contains duplicate parameter keys")
        allowed_requirements = {
            "required", "conditional_required", "derivable", "defaultable",
            "optional", "alternative", "degradable",
        }
        for item in parameters:
            requirement = item.get("requirement")
            if requirement is not None and requirement not in allowed_requirements:
                raise ValueError(
                    f"Model template {filename} has invalid requirement {requirement}"
                )
