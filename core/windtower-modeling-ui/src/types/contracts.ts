export type ParseMode = "llm" | "llm_full" | "deterministic";
export type LlmExecutionMode = "single" | "chunked_parallel";
export type JobType = "static_strength" | "modal";
export type ViewMode = "3d" | "xz" | "yz" | "xy";
export type HighlightMode = "mesh" | "door" | "flange" | "contact" | "bolts";
export type ResultScalar = "S_Mises" | "U_mag";

export interface BatchFile {
  file_id: string;
  filename: string;
  size: number;
  status: string;
  progress: number;
  processing_method?: string;
  evidence_count?: number;
  candidate_count?: number;
  error?: string | null;
}

export interface ParameterCandidate {
  candidate_id: string;
  key: string;
  label: string;
  normalized_value: unknown;
  normalized_unit?: string | null;
  raw_value?: unknown;
  raw_unit?: string | null;
  conversion?: string | null;
  evidence_ids?: string[];
  source_file_id?: string;
  source_kind?: string;
  extraction_method?: string;
  confirmation_status?: string;
  requires_confirmation?: boolean;
  confidence?: number;
}

export interface EvidenceLocation {
  page?: number | null;
  sheet?: string | null;
  cell_or_range?: string | null;
  line?: number | null;
  slide?: number | null;
  json_path?: string | null;
}

export interface EvidenceFragment {
  evidence_id: string;
  file_id: string;
  filename: string;
  kind: string;
  text: string;
  location?: EvidenceLocation;
  extraction_method: string;
  confidence?: number;
}

export interface KnowledgeBaseStatus {
  domain: "engineering" | "fem";
  status: string;
  document_count?: number;
  chunk_count?: number;
}

export interface AdvisorChannel {
  status?: string;
  provider?: string;
  model?: string;
  knowledge_base?: KnowledgeBaseStatus;
  file_count?: number;
  candidate_count?: number;
  source?: "model_library" | "reference_inp" | "capability_default" | string;
  compatibility?: string;
  warnings?: string[];
  usage?: Record<string, unknown>;
}

export interface ModelLibraryEntry {
  schema_version?: string;
  model_id: string;
  version?: number;
  status: string;
  label: string;
  structure_family?: string;
  capability_id?: string;
  parameter_revision?: number;
  validation_level: "engineer_reviewed" | "datacheck_passed" | "solver_verified";
  published_by?: string;
  signature?: {
    height_mm?: number | null;
    base_diameter_mm?: number | null;
    top_diameter_mm?: number | null;
    door_enabled?: boolean | null;
    analysis_tasks?: string[];
  };
  similarity_score?: number;
  recommendation_reasons?: string[];
}

export interface ModelLibraryResponse {
  schema_version: string;
  items: ModelLibraryEntry[];
}

export interface KnowledgeBasesResponse {
  schema_version?: string;
  engineering: KnowledgeBaseStatus;
  fem: KnowledgeBaseStatus;
}

export interface ParameterConflict {
  conflict_id: string;
  key: string;
  label: string;
  candidate_ids: string[];
  blocking: boolean;
  resolved_candidate_id?: string | null;
  resolved_value?: unknown;
}

export interface InputBatch {
  batch_id: string;
  capability_id?: string;
  modeling_goal?: string | null;
  parse_mode: ParseMode;
  llm_execution_mode?: LlmExecutionMode;
  provider?: string;
  model?: string;
  status: string;
  progress: number;
  files: BatchFile[];
  candidates: ParameterCandidate[];
  evidence?: EvidenceFragment[];
  conflicts: ParameterConflict[];
  warnings: string[];
  diagnostics?: Array<{
    code: string;
    severity?: string;
    retryable?: boolean;
    message?: string;
  }>;
  llm_partial_failure?: Record<string, unknown> | null;
  requires_confirmation?: string[];
  project_id?: string | null;
  review_revision?: number;
  review_parameters?: Array<{
    key: string;
    value: unknown;
    unit?: string | null;
  }>;
  engineering_review?: ReviewState;
  engineering_channel?: AdvisorChannel;
  fem_channel?: AdvisorChannel;
  selected_library_model?: ModelLibraryEntry | null;
  model_library_recommendations?: ModelLibraryEntry[];
  merge_readiness?: {
    engineering_confirmed?: boolean;
    fem_confirmed?: boolean;
    capability_accepted?: boolean;
    ready?: boolean;
    blockers?: string[];
  };
  engineering_llm_usage?: Record<string, unknown>;
  fem_llm_usage?: Record<string, unknown>;
  reference_model?: ReferenceModelState | null;
  reference_transfer?: ReferenceTransfer;
  fem_specification?: FemSpecification;
  fem_review_revision?: number;
  fem_review?: ReviewState;
  capability_match?: {
    capability_id?: string;
    status?: string;
    mode?: string;
    reasons?: string[];
    warnings?: string[];
  };
  readiness?: {
    preview_allowed?: boolean;
    compute_allowed?: boolean;
    technical_blockers?: string[];
    pending_confirmation_keys?: string[];
  };
}

export interface TransferItem {
  module: string;
  decision: "reusable" | "conditional" | "deferred" | "blocked" | string;
  label: string;
  reason: string;
  source_lines?: number[];
  keywords?: string[];
  status?: string;
}

export interface ReferenceTransfer {
  schema_version?: string;
  reusable?: TransferItem[];
  conditional?: TransferItem[];
  deferred_features?: TransferItem[];
  blocked?: TransferItem[];
}

export interface ReviewState {
  status?: string;
  confirmed_at?: string | null;
  operator?: string | null;
}

export interface ReferenceModelSummary {
  reference_model_id?: string;
  filename: string;
  status: string;
  compatibility: "compatible" | "partial" | "incompatible";
  compatibility_reasons?: string[];
  warnings?: string[];
  node_count?: number;
  element_count?: number;
  element_types?: Record<string, number>;
  materials?: Array<{ name?: string }>;
  steps?: Array<{ name?: string; procedures?: string[]; modes?: number }>;
  mesh_statistics?: {
    global_edge_median_mm?: number | null;
    door_edge_median_mm?: number | null;
    door_element_count?: number;
    bounding_box?: Record<string, number>;
  };
  constraints?: Array<{ type?: string; name?: string }>;
  mass_properties?: Record<string, unknown>;
  reference_transfer?: ReferenceTransfer;
  deferred_features?: TransferItem[];
}

export interface ReferenceModelState {
  summary: ReferenceModelSummary;
}

export interface FemSpecificationValue {
  value: unknown;
  origin: string;
  confirmation_status: string;
  requires_confirmation: boolean;
  alternatives?: Array<Partial<FemSpecificationValue> & { model_id?: string }>;
  model_id?: string;
  sources?: Array<{
    branch: "source_data" | "engineer_input" | "reference_model" | "llm_recommendation" | "capability_profile" | string;
    origin?: string;
    source_file_id?: string;
    evidence_ids?: string[];
    locator?: string | null;
    model?: string | null;
    operator?: string;
    value?: unknown;
  }>;
}

export interface FemSpecification {
  schema_version?: string;
  capability_id: string;
  editable_parameter_keys?: string[];
  reference_model_id?: string | null;
  compatibility: string;
  status: string;
  source_mode?: string;
  requires_engineer_confirmation?: boolean;
  parameters: Record<string, FemSpecificationValue>;
  output_requests?: string[];
}

export interface ConfirmBatchResponse {
  project_id: string;
  input_batch: InputBatch;
}

export interface ParameterRecord {
  key: string;
  label: string;
  group: "geometry" | "material" | "mesh" | "loads" | "analysis" | "boundary" | "interaction";
  value: unknown;
  unit?: string | null;
  status: string;
  note?: string | null;
  domain: "engineering" | "fem";
  origin: string;
  confirmation_status: string;
  requires_confirmation: boolean;
  sources?: Array<{
    filename: string;
    locator?: string | null;
    extraction_method?: string;
    evidence_ids?: string[];
    source_type?: string;
    source_category?: string | null;
    raw_value?: unknown;
    raw_unit?: string | null;
    conversion?: string | null;
    mapping_confidence?: number | null;
    confidence?: number | null;
    locator_status?: string;
    consistency_status?: string;
    validity_status?: string;
    human_confirmation?: string;
    downstream_validation?: string;
    formal_use_allowed?: boolean;
  }>;
}

export interface ModelingCapability {
  capability_id: string;
  capability_revision: number;
  label: string;
  structure_family: string;
  complexity_level?: string;
  element_types: string[];
  supported_analyses: string[];
  fixed_rules: Record<string, unknown>;
  editable_fem_parameters: string[];
  scope_note?: string;
  unsupported_features?: string[];
}

export interface CapabilityRegistryResponse {
  schema_version: string;
  items: ModelingCapability[];
}

export interface ParameterTraceSource {
  filename?: string;
  locator?: string | null;
  excerpt?: string | null;
  extraction_method?: string;
  mapping_confidence?: number | null;
}

export interface ParameterTrace {
  project_id: string;
  revision: number;
  key: string;
  label: string;
  status: string;
  confirmation_status: string;
  confirmed_at?: string | null;
  confirmed_by?: string | null;
  raw_value?: unknown;
  raw_unit?: string | null;
  normalized_value: unknown;
  normalized_unit?: string | null;
  conversion?: string | null;
  acquisition_method?: string | null;
  mapping_confidence?: number | null;
  confidence?: number | null;
  assurance?: {
    source_category?: string | null;
    locator_status?: string;
    consistency_status?: string;
    validity_status?: string;
    human_confirmation?: string;
    downstream_validation?: string;
    formal_use_allowed?: boolean;
  };
  sources: ParameterTraceSource[];
  consumed_by: string[];
}

export interface ValidationCheck {
  id: string;
  label: string;
  status: "pass" | "warning" | "fail" | "not_run";
  value?: unknown;
}

export interface ValidationSummary {
  project_id: string;
  parameter_revision: number;
  status: "pass" | "warning" | "fail" | "not_run";
  groups: Array<{
    id: string;
    label: string;
    status: ValidationCheck["status"];
    counts: { passed: number; warnings: number; failed: number; not_run: number };
    checks: ValidationCheck[];
  }>;
  totals: { passed: number; warnings: number; failed: number; not_run: number };
}

export interface RevisionList {
  project_id: string;
  items: Array<{
    revision: number;
    created_at?: string | null;
    operator: string;
    summary: string;
    change_count: number;
  }>;
}

export interface RevisionDiff {
  project_id: string;
  from_revision: number;
  to_revision: number;
  diff: Array<{
    key: string;
    label: string;
    unit?: string | null;
    old_value: unknown;
    new_value: unknown;
    group: string;
  }>;
  rebuild_required: string[];
  unchanged: string[];
}

export interface ParameterSet {
  schema_version: string;
  structure_family: string;
  capability_id: string;
  reference_model_id?: string | null;
  reference_model_compatibility?: string;
  project_id: string;
  revision: number;
  parameters: Record<string, ParameterRecord>;
  analysis_tasks?: Array<"static" | "modal">;
  output_requests?: string[];
}

export interface ValidationIssue {
  key?: string;
  category?: string;
  message?: string;
  severity?: string;
}

export interface ParameterSetResponse {
  parameter_set: ParameterSet;
  validation: { status: string; issues: ValidationIssue[] };
}

export interface ParameterChange {
  key: string;
  value: unknown;
  unit?: string | null;
}

export interface ParameterChangePreview {
  preview_id: string;
  base_revision: number;
  diff: Array<{
    key: string;
    label: string;
    unit?: string | null;
    old_value: unknown;
    new_value: unknown;
    domain?: "engineering" | "fem";
    origin?: string;
    affected_artifacts?: string[];
  }>;
  validation: { status: string; issues: ValidationIssue[] };
  invalidated_artifacts?: string[];
}

export interface MeshStats {
  nodes?: number;
  s4r_elements?: number;
  door_patch_elements?: number;
  door_neighbor_elements?: number;
  [key: string]: unknown;
}

export interface MeshReport {
  status: string;
  mesh_stats?: MeshStats;
  warnings?: Array<string | { message?: string }>;
  quality_report?: Record<string, unknown>;
}

export interface PyVistaPreview {
  status: string;
  source?: string;
  artifact_url?: string;
  mesh_data_url?: string;
  mesh_stats?: MeshStats;
  feature_sets?: Record<string, number>;
  warnings?: string[];
}

export interface MeshData {
  points: number[][];
  quads: number[][];
  lines?: number[][];
  deformed_points?: number[][];
  point_data?: Record<string, unknown>;
  cell_data?: Record<string, unknown>;
  line_data?: Record<string, unknown>;
  bounds?: { min: number[]; max: number[] };
  stats?: Record<string, number>;
}

export interface OdbResultMesh extends MeshData {
  schema_version: string;
  status: string;
  source?: string;
  warnings?: string[];
  summary?: {
    max_mises?: number;
    max_u_mag?: number;
    node_count?: number;
    element_count?: number;
    door_patch_elements?: number;
  };
}

export interface BuildReport {
  status: string;
  build_id?: string;
  parameter_revision?: number;
  jobs?: Record<string, {
    status?: string;
    artifact_name?: string;
    checks?: Record<string, boolean>;
  }>;
}

export type WorkflowStepStatus = "not_started" | "ready" | "running" | "blocked" | "completed";

export interface WorkflowBlocker {
  layer: "input" | "parameter" | "model" | "environment" | string;
  code: string;
  parameter_key?: string | null;
  message: string;
  resolution_action: string;
}

export interface WorkflowState {
  schema_version: "project_workflow_state_v1";
  project_id: string;
  parameter_revision: number;
  current_step: "input" | "parameters" | "mesh_model" | "model_check" | "solve_results";
  steps: Array<{
    id: WorkflowState["current_step"];
    label: string;
    status: WorkflowStepStatus;
  }>;
  blockers: WorkflowBlocker[];
  advisories?: WorkflowBlocker[];
  next_action?: { id: string; label: string } | null;
  environment: Record<string, { status: string; required?: boolean; message?: string | null }>;
  gates: {
    parameter: boolean;
    mesh: boolean;
    model_file: boolean;
    datacheck: boolean;
    results: boolean;
  };
}

export interface RepairPreview {
  schema_version: "semantic_command_v1";
  stage: "model_repair";
  status: "proposed" | "manual_required";
  canonical_parameter_patch: Array<{
    op: string;
    parameter_key: string;
    value: unknown;
    unit?: string | null;
    requires_engineer_confirmation: boolean;
  }>;
  manual_diagnostics: WorkflowBlocker[];
  explanation: string;
}

export interface SubmissionTask {
  job_type?: string;
  status?: string;
  stage?: string;
  elapsed_seconds?: number;
  log_excerpt?: string[];
}

export interface Submission {
  submission_id: string;
  status: string;
  stage?: string;
  tasks?: Record<string, SubmissionTask>;
  results?: Record<string, unknown> | null;
  postprocess?: Record<string, unknown> | null;
}

export interface ArtifactItem {
  name: string;
  filename: string;
  relative_path: string;
  category: "parameters" | "mesh" | "static_inp" | "modal_inp" | "solver" | "results" | "other";
  size_bytes: number;
  content_type: string;
  previewable: boolean;
}

export interface ArtifactList {
  schema_version: string;
  project_id: string;
  items: ArtifactItem[];
}
