from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


BatchStatus = Literal[
    "queued", "processing", "awaiting_template_confirmation", "awaiting_intent_confirmation", "needs_input",
    "needs_confirmation", "ready", "completed_with_llm_warning",
    "blocked", "failed", "confirmed",
]
FileStatus = Literal["queued", "extracting", "mapping", "completed", "needs_confirmation", "failed"]
ParseMode = Literal["llm", "llm_full", "deterministic"]
LlmExecutionMode = Literal["single", "chunked_parallel"]


class EvidenceLocation(BaseModel):
    page: int | None = None
    sheet: str | None = None
    cell_or_range: str | None = None
    line: int | None = None
    slide: int | None = None
    json_path: str | None = None
    bbox: list[float] | None = None
    title_path: list[str] = Field(default_factory=list)
    block_id: str | None = None


class EvidenceFragment(BaseModel):
    evidence_id: str
    file_id: str
    filename: str
    kind: Literal[
        "text", "table", "table_cell", "image", "drawing", "structured",
        "heading", "list_item", "formula",
    ]
    text: str
    location: EvidenceLocation = Field(default_factory=EvidenceLocation)
    extraction_method: str
    confidence: float = Field(default=1.0, ge=0, le=1)
    requires_ocr: bool = False
    from_ocr: bool = False
    content_hash: str
    field: str | None = None
    label: str | None = None
    raw_value: Any = None
    raw_unit: str | None = None
    value: Any = None
    unit: str | None = None
    status: Literal["observed", "verified", "needs_review", "conflict", "rejected"] = "observed"
    extraction_methods: list[str] = Field(default_factory=list)
    source_sha256: str | None = None


class ParameterCandidate(BaseModel):
    candidate_id: str
    key: str
    label: str
    raw_value: Any
    raw_unit: str | None = None
    normalized_value: Any
    normalized_unit: str | None = None
    conversion: str | None = None
    source_file_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    extraction_method: str
    provider: str = "deterministic"
    model: str = "none"
    confidence: float = Field(default=1.0, ge=0, le=1)
    priority: Literal["P0", "P1"] = "P0"
    requires_confirmation: bool = False
    confirmation_reason: str | None = None
    source_kind: Literal[
        "file_observed", "goal_explicit", "llm_mapped", "llm_suggested",
        "system_setting", "engineer_input", "reference_model_value",
        "model_library_reference", "capability_default", "fem_supplement",
    ] = "file_observed"
    confirmation_status: Literal["not_required", "pending", "confirmed"] = "not_required"
    assumption_basis: str | None = None
    template_id: str | None = None
    prompt_hash: str | None = None


class ParameterConflict(BaseModel):
    conflict_id: str
    key: str
    label: str
    priority: Literal["P0", "P1"]
    candidate_ids: list[str]
    blocking: bool
    resolved_candidate_id: str | None = None
    resolved_value: Any = None
    resolved_unit: str | None = None
    confirmation_status: Literal["pending", "confirmed"] = "pending"


class BatchFile(BaseModel):
    file_id: str
    filename: str
    suffix: str
    media_type: str
    size: int
    sha256: str
    status: FileStatus = "queued"
    processing_method: str
    cost_level: Literal["no_ocr", "partial_ocr", "full_ocr", "complex_drawing"]
    progress: int = 0
    evidence_count: int = 0
    candidate_count: int = 0
    ocr_pages: int = 0
    cache_hits: int = 0
    error: str | None = None
    document_status: Literal[
        "pending", "converted", "degraded_native_only", "needs_review", "failed"
    ] = "pending"
    document_manifest: str | None = None
    document_manifest_sha256: str | None = None
    support_tier: Literal["formal", "exploratory"] = "formal"


class InputBatch(BaseModel):
    schema_version: str = "engineering_input_batch_v3"
    batch_id: str
    capability_id: str = "WT-SHELL-DOOR-STATIC-MODAL-V1"
    modeling_goal: str | None = None
    parse_mode: ParseMode = "llm"
    llm_execution_mode: LlmExecutionMode = "single"
    provider: str = "none"
    model: str = "none"
    status: BatchStatus = "queued"
    progress: int = 0
    files: list[BatchFile] = Field(default_factory=list)
    evidence: list[EvidenceFragment] = Field(default_factory=list)
    candidates: list[ParameterCandidate] = Field(default_factory=list)
    conflicts: list[ParameterConflict] = Field(default_factory=list)
    resolved_parameters: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    candidate_mapping_ir: dict[str, Any] | None = None
    standard_input_candidate: dict[str, Any] | None = None
    completeness_report: dict[str, Any] = Field(default_factory=dict)
    template_recommendation: dict[str, Any] = Field(default_factory=dict)
    selected_template_id: str | None = None
    selected_template_version: str | None = None
    prompt_hash: str | None = None
    parameter_advisories: list[dict[str, Any]] = Field(default_factory=list)
    intent_contract: dict[str, Any] = Field(default_factory=dict)
    model_composition: dict[str, Any] = Field(default_factory=dict, exclude=True)
    active_parameter_readiness: dict[str, Any] = Field(default_factory=dict)
    unsupported_candidates: list[dict[str, Any]] = Field(default_factory=list)
    llm_partial_failure: dict[str, Any] | None = None
    llm_retry_groups: list[str] = Field(default_factory=list)
    review_revision: int = 0
    review_parameters: list[dict[str, Any]] = Field(default_factory=list)
    final_review: dict[str, Any] = Field(default_factory=dict)
    engineering_review: dict[str, Any] = Field(default_factory=lambda: {
        "status": "pending",
        "confirmed_at": None,
        "operator": None,
    })
    reference_model: dict[str, Any] | None = None
    reference_transfer: dict[str, Any] = Field(default_factory=dict)
    fem_specification: dict[str, Any] = Field(default_factory=dict)
    fem_review_revision: int = 1
    fem_review: dict[str, Any] = Field(default_factory=lambda: {
        "status": "pending",
        "confirmed_at": None,
        "operator": None,
    })
    capability_match: dict[str, Any] = Field(default_factory=dict)
    engineering_channel: dict[str, Any] = Field(default_factory=dict)
    fem_channel: dict[str, Any] = Field(default_factory=dict)
    selected_library_model: dict[str, Any] | None = None
    model_library_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    merge_readiness: dict[str, Any] = Field(default_factory=dict)
    engineering_llm_usage: dict[str, Any] = Field(default_factory=dict)
    fem_llm_usage: dict[str, Any] = Field(default_factory=dict)
    readiness: dict[str, Any] = Field(default_factory=lambda: {
        "preview_allowed": False,
        "compute_allowed": False,
        "technical_blockers": [],
        "pending_confirmation_keys": [],
    })
    created_at: str
    updated_at: str
    project_id: str | None = None


class ConflictResolution(BaseModel):
    conflict_id: str
    candidate_id: str | None = None
    value: Any = None
    unit: str | None = None


class ResolveConflictsRequest(BaseModel):
    resolutions: list[ConflictResolution]


class ParameterCompletion(BaseModel):
    key: str
    value: Any
    unit: str | None = None


class CompleteParametersRequest(BaseModel):
    changes: list[ParameterCompletion]


class ReparseRequest(BaseModel):
    parse_mode: ParseMode
    llm_execution_mode: LlmExecutionMode | None = None


class ConfirmTemplateRequest(BaseModel):
    template_id: str
    version: str


class ConfirmIntentRequest(BaseModel):
    model_family: str = "wind_tower"
    fidelity: Literal["quick", "detailed"] = "quick"
    analysis_tasks: list[str] = Field(default_factory=list)
    requested_features: list[str] = Field(default_factory=list)
    output_targets: list[str] = Field(default_factory=list)


class ConfirmBatchReviewRequest(BaseModel):
    review_revision: int
    parameters: list[ParameterCompletion]
    operator: str = "engineer"


class ConfirmFemSpecificationRequest(BaseModel):
    review_revision: int
    parameters: list[ParameterCompletion] = Field(default_factory=list)
    operator: str = "engineer"


class ConfirmAndCreateRequest(BaseModel):
    engineering_review_revision: int
    fem_review_revision: int
    engineering_parameters: list[ParameterCompletion]
    fem_parameters: list[ParameterCompletion] = Field(default_factory=list)
    operator: str = "engineer"
