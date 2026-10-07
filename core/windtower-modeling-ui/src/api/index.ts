import type {
  ArtifactList,
  BuildReport,
  ConfirmBatchResponse,
  InputBatch,
  JobType,
  MeshReport,
  ModelLibraryEntry,
  ModelLibraryResponse,
  KnowledgeBasesResponse,
  OdbResultMesh,
  ParameterChangePreview,
  ParameterSetResponse,
  ParameterTrace,
  ParseMode,
  LlmExecutionMode,
  PyVistaPreview,
  Submission,
  ValidationSummary,
  RevisionList,
  RevisionDiff,
  WorkflowState,
  RepairPreview,
  CapabilityRegistryResponse,
} from "../types";
import { API_BASE, ApiError } from "./client";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(
      String((payload as { detail?: string }).detail ?? response.statusText),
      response.status,
      payload,
    );
  }
  return payload as T;
}

export async function createInputBatch(
  files: File[],
  goal: string,
  parseMode: ParseMode,
  llmExecutionMode: LlmExecutionMode,
  referenceModel?: File | null,
  modelLibraryId?: string,
  capabilityId = "WT-SHELL-DOOR-STATIC-MODAL-V1",
): Promise<InputBatch> {
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  if (referenceModel) body.append("reference_model", referenceModel);
  if (modelLibraryId) body.append("model_library_id", modelLibraryId);
  body.append("modeling_goal", goal);
  body.append("parse_mode", parseMode);
  body.append("llm_execution_mode", llmExecutionMode);
  body.append("capability_id", capabilityId);
  return request("/api/input-batches", { method: "POST", body });
}

export const getCapabilities = () =>
  request<CapabilityRegistryResponse>("/api/capabilities");

export const getInputBatch = (batchId: string) =>
  request<InputBatch>(`/api/input-batches/${batchId}`);

export const resolveInputConflict = (
  batchId: string,
  conflictId: string,
  candidateId: string,
) => request<InputBatch>(
  `/api/input-batches/${batchId}/conflicts/resolve`,
  json({ resolutions: [{ conflict_id: conflictId, candidate_id: candidateId }] }),
);

export async function appendInputFiles(batchId: string, files: File[]) {
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  return request<InputBatch>(`/api/input-batches/${batchId}/files`, {
    method: "POST",
    body,
  });
}

export const reparseInputBatch = (batchId: string, parseMode: ParseMode) =>
  request<InputBatch>(
    `/api/input-batches/${batchId}/reparse`,
    json({ parse_mode: parseMode }),
  );

export const confirmBatch = (batchId: string, body: object) =>
  request<ConfirmBatchResponse>(`/api/input-batches/${batchId}/confirm`, {
    ...json(body),
  });

export const confirmEngineeringReview = (batchId: string, body: object) =>
  request<InputBatch>(
    `/api/input-batches/${batchId}/engineering-review/confirm`,
    json(body),
  );

export const confirmAndCreateInputBatch = (batchId: string, body: object) =>
  request<ConfirmBatchResponse>(
    `/api/input-batches/${batchId}/confirm-and-create`,
    json(body),
  );

export const promoteInputBatch = (batchId: string) =>
  request<ConfirmBatchResponse>(`/api/input-batches/${batchId}/confirm`, {
    method: "POST",
  });

export async function putReferenceModel(batchId: string, file: File) {
  const body = new FormData();
  body.append("file", file);
  return request<InputBatch>(`/api/input-batches/${batchId}/reference-model`, {
    method: "PUT",
    body,
  });
}

export const deleteReferenceModel = (batchId: string) =>
  request<InputBatch>(`/api/input-batches/${batchId}/reference-model`, {
    method: "DELETE",
  });

export const selectLibraryModel = (batchId: string, modelId: string) =>
  request<InputBatch>(
    `/api/input-batches/${batchId}/reference-model/select`,
    json({ model_id: modelId }),
  );

export const getModelLibrary = () =>
  request<ModelLibraryResponse>("/api/model-library");

export const publishProjectModel = (projectId: string, label: string) =>
  request<ModelLibraryEntry>(
    `/api/projects/${projectId}/model-library/publish`,
    json({ label, operator: "engineer" }),
  );

export const getKnowledgeBasesStatus = () =>
  request<KnowledgeBasesResponse>("/api/knowledge-bases/status");

export const confirmFemSpecification = (batchId: string, body: object) =>
  request<InputBatch>(
    `/api/input-batches/${batchId}/fem-specification/confirm`,
    json(body),
  );

export const getParameters = (projectId: string) =>
  request<ParameterSetResponse>(`/api/projects/${projectId}/parameters`);

export const getLatestProject = () =>
  request<{ project_id: string }>("/api/projects/latest");

export const getParameterTrace = (projectId: string, key: string) =>
  request<ParameterTrace>(`/api/projects/${projectId}/parameters/${encodeURIComponent(key)}/trace`);

export const getValidationSummary = (projectId: string) =>
  request<ValidationSummary>(`/api/projects/${projectId}/validation-summary`);

export const getWorkflowState = (projectId: string) =>
  request<WorkflowState>(`/api/projects/${projectId}/workflow-state`);

export const previewModelRepair = (projectId: string) =>
  request<RepairPreview>(`/api/projects/${projectId}/repairs/preview`, { method: "POST" });

export const getRevisions = (projectId: string) =>
  request<RevisionList>(`/api/projects/${projectId}/revisions`);

export const getRevisionDiff = (projectId: string, fromRevision: number, toRevision: number) =>
  request<RevisionDiff>(`/api/projects/${projectId}/revisions/diff?from_revision=${fromRevision}&to_revision=${toRevision}`);

export const previewParameterChanges = (projectId: string, body: object) =>
  request<ParameterChangePreview>(
    `/api/projects/${projectId}/parameter-changes/preview`,
    json(body),
  );

export const confirmParameterChanges = (projectId: string, body: object) =>
  request<ParameterSetResponse>(
    `/api/projects/${projectId}/parameter-changes/confirm`,
    json(body),
  );

export const validateProject = (projectId: string) =>
  request<Record<string, unknown>>(`/api/projects/${projectId}/validate`, {
    method: "POST",
  });

export const meshProject = (projectId: string) =>
  request<MeshReport>(`/api/projects/${projectId}/mesh`, { method: "POST" });

export const getMeshPreview = (projectId: string) =>
  request<PyVistaPreview>(`/api/projects/${projectId}/mesh-preview`, {
    method: "POST",
  });

export const buildProject = (projectId: string) =>
  request<BuildReport>(`/api/projects/${projectId}/build`, { method: "POST" });

export const datacheckProject = (projectId: string, jobType: JobType | "all") =>
  request<Record<string, unknown>>(
    `/api/projects/${projectId}/datacheck`,
    json({ job_type: jobType }),
  );

export const submitProject = (projectId: string, jobTypes: JobType[]) =>
  request<Submission>(
    `/api/projects/${projectId}/submissions`,
    json({
      job_types: jobTypes,
      run_postprocess: true,
      idempotency_key: crypto.randomUUID(),
    }),
  );

export const getSubmission = (projectId: string, submissionId: string) =>
  request<Submission>(
    `/api/projects/${projectId}/submissions/${submissionId}`,
  );

export const getResults = (projectId: string) =>
  request<Record<string, unknown>>(`/api/projects/${projectId}/results`);

export const getResultMesh = (projectId: string) =>
  request<OdbResultMesh>(`/api/projects/${projectId}/result-mesh`);

export const getArtifacts = (projectId: string) =>
  request<ArtifactList>(`/api/projects/${projectId}/artifacts`);

export function artifactUrl(
  projectId: string,
  name: string,
  download = false,
) {
  const encoded = name.split("/").map(encodeURIComponent).join("/");
  return `${API_BASE}/api/projects/${projectId}/artifacts/${encoded}${
    download ? "?download=true" : ""
  }`;
}

export function apiAssetUrl(path?: string) {
  if (!path) return "";
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

function json(body: object): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
