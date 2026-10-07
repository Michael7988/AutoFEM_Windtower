from __future__ import annotations

import json
import os
from typing import Any

from ..knowledge import LocalKnowledgeBase
from .providers import EngineeringLlmProvider


class RestrictedAdvisor:
    def __init__(
        self,
        role: str,
        provider: EngineeringLlmProvider,
        knowledge: LocalKnowledgeBase,
    ) -> None:
        self.role = role
        self.provider = provider
        self.knowledge = knowledge
        self.prompt_version = os.getenv(
            f"QWEN_{role.upper()}_PROMPT_VERSION",
            f"{role}_advisor_v1",
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            **self.provider.capabilities(),
            "advisor_role": self.role,
            "prompt_version": self.prompt_version,
            "knowledge_base": self.knowledge.status(),
        }

    def map_parameters(
        self,
        evidence: list[dict[str, Any]],
        modeling_goal: str | None,
        mapping_prompt: str,
        target_keys: list[str],
    ) -> dict[str, Any]:
        references = self.knowledge.query(
            " ".join([modeling_goal or "", *target_keys]),
            limit=6,
        )
        prompt = f"{mapping_prompt}\n\n{_advisor_policy(self.role)}"
        if references:
            prompt += (
                "\nRetrieved local references are advisory context only. Re-rank them for "
                "relevance and cite reference_id in knowledge_reference_ids when used:\n"
                + json.dumps(references, ensure_ascii=False)
            )
        result = self.provider.map_parameters(
            evidence,
            modeling_goal,
            mapping_prompt=prompt,
            target_keys=target_keys,
        )
        result["advisor_role"] = self.role
        result["prompt_version"] = self.prompt_version
        result["knowledge_references"] = [
            {key: item[key] for key in ("reference_id", "title", "section", "source_name", "sha256")}
            for item in references
        ]
        result["knowledge_base"] = self.knowledge.status()
        return result

    def usage(self) -> dict[str, Any]:
        return self.provider.usage()

    def connection_test(self) -> dict[str, Any]:
        result = self.provider.connection_test()
        result["advisor_role"] = self.role
        return result


class EngineeringAdvisor(RestrictedAdvisor):
    def __init__(self, provider: EngineeringLlmProvider, knowledge: LocalKnowledgeBase) -> None:
        super().__init__("engineering", provider, knowledge)

    def extract_image_text(self, content: bytes, media_type: str, prompt: str) -> dict[str, Any]:
        return self.provider.extract_image_text(content, media_type, prompt)


class FemAdvisor(RestrictedAdvisor):
    def __init__(self, provider: EngineeringLlmProvider, knowledge: LocalKnowledgeBase) -> None:
        super().__init__("fem", provider, knowledge)


def _advisor_policy(role: str) -> str:
    if role == "engineering":
        return (
            "You are the engineering-information advisor. Standards may explain fields, units "
            "and validation ranges, but must never supply project-specific geometry or loads. "
            "Do not output FEM settings, ParameterSet, mesh, Abaqus keywords or INP."
        )
    return (
        "You are the FEM-specification advisor. Manuals may explain element, mesh, boundary, "
        "load implementation, analysis steps and outputs, but must not override deterministic "
        "reference-model evidence. Do not output engineering geometry, topology arrays, Abaqus "
        "keywords, executable code or INP."
    )
