import {
  AlertTriangle,
  Box,
  Check,
  ChevronRight,
  CircleDot,
  Download,
  Eye,
  FileArchive,
  FilePlus2,
  FileText,
  FolderOpen,
  Grid3X3,
  History,
  LoaderCircle,
  Menu,
  Play,
  RefreshCw,
  Settings2,
  UploadCloud,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  apiAssetUrl,
  appendInputFiles,
  artifactUrl,
  buildProject,
  confirmAndCreateInputBatch,
  confirmParameterChanges,
  createInputBatch,
  datacheckProject,
  deleteReferenceModel,
  getArtifacts,
  getCapabilities,
  getInputBatch,
  getKnowledgeBasesStatus,
  getLatestProject,
  getMeshPreview,
  getModelLibrary,
  getParameters,
  getParameterTrace,
  getRevisionDiff,
  getRevisions,
  getValidationSummary,
  getResultMesh,
  getResults,
  getSubmission,
  meshProject,
  previewParameterChanges,
  previewModelRepair,
  publishProjectModel,
  putReferenceModel,
  reparseInputBatch,
  resolveInputConflict,
  selectLibraryModel,
  submitProject,
  validateProject,
} from "./api";
import { ApiError } from "./api/client";
import { ViewportWorkspace, type ViewContent } from "./components/workbench/ViewportWorkspace";
import { PreparationConfirmBar } from "./components/workbench/PreparationConfirmBar";
import {
  ParameterReviewTable,
  ParameterTracePanel,
  RevisionPanel,
  ValidationPanel,
  type ReviewTableItem,
} from "./components/workbench/TrustPanels";
import {
  ParameterEditor as ParameterEditorPanel,
  PreparationParameterReview as PreparationParameterReviewPanel,
} from "./components/workbench/ParameterPanels";
import { usePolling } from "./hooks/usePolling";
import { useWorkflowState } from "./hooks/useWorkflowState";
import { LanguageSwitcher, useLocalization } from "./i18n";
import { isFemParameterEditable } from "./components/workbench/parameterPresentation";
import { WorkflowStepper } from "./components/workbench/WorkflowStepper";

export { PreparationConfirmBar };
import type {
  ArtifactItem,
  ArtifactList,
  BuildReport,
  CapabilityRegistryResponse,
  HighlightMode,
  InputBatch,
  JobType,
  MeshReport,
  ModelLibraryEntry,
  ModelLibraryResponse,
  KnowledgeBasesResponse,
  OdbResultMesh,
  ParameterChange,
  ParameterChangePreview,
  ParameterRecord,
  ParameterTrace,
  ParameterSetResponse,
  ParseMode,
  LlmExecutionMode,
  PyVistaPreview,
  RevisionDiff,
  RevisionList,
  RepairPreview,
  ResultScalar,
  Submission,
  ValidationSummary,
  ViewMode,
  WorkflowState,
} from "./types";

type ParameterMode = "natural" | "direct" | "evidence" | "revisions";
type PreparationDomain = "engineering" | "fem";
type OutputTab = "model" | "validation" | "results" | "files";

const DEFAULT_GOAL =
  "建立带门洞风电塔筒 S4R 壳模型，执行线弹性静力与模态分析。";
const TERMINAL_BATCH = new Set([
  "ready",
  "needs_input",
  "needs_confirmation",
  "completed_with_llm_warning",
  "blocked",
  "failed",
  "confirmed",
]);
const TERMINAL_SUBMISSION = new Set(["completed", "failed", "blocked", "partial"]);
const GROUPS: Array<{ key: ParameterRecord["group"]; label: string }> = [
  { key: "geometry", label: "几何与门洞" },
  { key: "material", label: "材料截面" },
  { key: "interaction", label: "接触与预紧" },
  { key: "mesh", label: "网格" },
  { key: "loads", label: "荷载" },
  { key: "boundary", label: "边界" },
  { key: "analysis", label: "分析任务" },
];
const ARTIFACT_CATEGORIES: Array<{ key: ArtifactItem["category"]; label: string }> = [
  { key: "parameters", label: "参数" },
  { key: "mesh", label: "网格" },
  { key: "static_inp", label: "静力 INP" },
  { key: "modal_inp", label: "模态 INP" },
  { key: "solver", label: "Datacheck 与日志" },
  { key: "results", label: "结果" },
  { key: "other", label: "其他" },
];

export function App() {
  const { language } = useLocalization();
  const uploadRef = useRef<HTMLInputElement | null>(null);
  const appendRef = useRef<HTMLInputElement | null>(null);
  const referenceRef = useRef<HTMLInputElement | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [referenceFile, setReferenceFile] = useState<File | null>(null);
  const [selectedLibraryId, setSelectedLibraryId] = useState("");
  const [modelLibrary, setModelLibrary] = useState<ModelLibraryResponse | null>(null);
  const [capabilities, setCapabilities] = useState<CapabilityRegistryResponse | null>(null);
  const [capabilityId, setCapabilityId] = useState("WT-SHELL-DOOR-STATIC-MODAL-V1");
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBasesResponse | null>(null);
  const [goal, setGoal] = useState(DEFAULT_GOAL);
  const [parseMode, setParseMode] = useState<ParseMode>("llm_full");
  const [llmExecutionMode, setLlmExecutionMode] = useState<LlmExecutionMode>("single");
  const [batch, setBatch] = useState<InputBatch | null>(null);
  const [parameters, setParameters] = useState<ParameterSetResponse | null>(null);
  const [draftValues, setDraftValues] = useState<Record<string, unknown>>({});
  const [engineeringDraftValues, setEngineeringDraftValues] = useState<Record<string, unknown>>({});
  const [femDraftValues, setFemDraftValues] = useState<Record<string, unknown>>({});
  const [parameterMode, setParameterMode] = useState<ParameterMode>("natural");
  const [parameterTrace, setParameterTrace] = useState<ParameterTrace | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [revisions, setRevisions] = useState<RevisionList | null>(null);
  const [revisionDiff, setRevisionDiff] = useState<RevisionDiff | null>(null);
  const [preparationDomain, setPreparationDomain] = useState<PreparationDomain>("engineering");
  const [instruction, setInstruction] = useState("");
  const [preview, setPreview] = useState<ParameterChangePreview | null>(null);
  const [validation, setValidation] = useState<Record<string, unknown> | null>(null);
  const [validationSummary, setValidationSummary] = useState<ValidationSummary | null>(null);
  const [mesh, setMesh] = useState<MeshReport | null>(null);
  const [meshPreview, setMeshPreview] = useState<PyVistaPreview | null>(null);
  const [build, setBuild] = useState<BuildReport | null>(null);
  const [datacheck, setDatacheck] = useState<Record<string, unknown> | null>(null);
  const [submission, setSubmission] = useState<Submission | null>(null);
  const [results, setResults] = useState<Record<string, unknown> | null>(null);
  const [resultMesh, setResultMesh] = useState<OdbResultMesh | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactList | null>(null);
  const [artifactPreview, setArtifactPreview] = useState<{
    item: ArtifactItem;
    content: string;
  } | null>(null);
  const [viewContent, setViewContent] = useState<ViewContent>("mesh");
  const [viewMode, setViewMode] = useState<ViewMode>("3d");
  const [highlight, setHighlight] = useState<HighlightMode>("mesh");
  const [scalar, setScalar] = useState<ResultScalar>("S_Mises");
  const [deformationScale, setDeformationScale] = useState(1);
  const [outputTab, setOutputTab] = useState<OutputTab>("model");
  const [jobs, setJobs] = useState<Record<JobType, boolean>>({
    static_strength: true,
    modal: true,
  });
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [repairPreview, setRepairPreview] = useState<RepairPreview | null>(null);

  useEffect(() => {
    const chineseGoal = "建立带门洞风电塔筒 S4R 壳模型，执行线弹性静力与模态分析。";
    const englishGoal = "Create an S4R shell model of a wind-turbine tower with a door opening, and perform linear static and modal analyses.";
    setGoal((current) => {
      if (language === "en-US" && current === chineseGoal) return englishGoal;
      if (language === "zh-CN" && current === englishGoal) return chineseGoal;
      return current;
    });
  }, [language]);

  const projectId = batch?.project_id ?? parameters?.parameter_set.project_id ?? null;
  const { workflowState, workflowLoading, refreshWorkflow } = useWorkflowState(projectId);
  const referenceSummary = batch?.reference_model?.summary;
  const femConfirmed = batch?.fem_review?.status === "confirmed";
  const engineeringReviewRows = batch?.review_parameters ?? [];
  const femReviewRows = Object.entries(batch?.fem_specification?.parameters ?? {});
  const referenceCandidateCount = batch?.candidates.filter(
    (candidate) => candidate.source_kind === "reference_model_value",
  ).length ?? 0;
  const missingPreparationKeys = [
    ...engineeringReviewRows
      .filter((row) => isMissingValue(engineeringDraftValues[row.key] ?? row.value))
      .map((row) => row.key),
    ...femReviewRows
      .filter(([key, item]) => isMissingValue(femDraftValues[key] ?? item.value))
      .map(([key]) => key),
  ];
  const parameterRows = useMemo(
    () => Object.values(parameters?.parameter_set.parameters ?? {}),
    [parameters],
  );
  const towerRows = useMemo(
    () => parameterRows.filter((row) => row.key.startsWith("geometry.tower_sections.")),
    [parameterRows],
  );
  const groupedTowerRows = useMemo(() => groupTowerParameters(towerRows), [towerRows]);
  const changedParameters = useMemo(
    () => directChanges(parameterRows, draftValues),
    [draftValues, parameterRows],
  );
  const reviewItems = useMemo<ReviewTableItem[]>(() => parameterRows.map((row) => {
    const firstSource = row.sources?.[0];
    const confirmed = row.confirmation_status === "confirmed";
    return {
      key: row.key,
      label: safeParameterLabel(row.key, row.label),
      value: row.value,
      unit: row.unit,
      source: firstSource?.filename ?? row.origin ?? "unlocated",
      status: confirmed ? "confirmed" : row.requires_confirmation ? "review" : "candidate",
      additionalSources: Math.max(0, (row.sources?.length ?? 0) - 1),
    };
  }), [parameterRows]);
  const selectedJobs = (Object.entries(jobs) as Array<[JobType, boolean]>)
    .filter(([, selected]) => selected)
    .map(([job]) => job);
  const parameterGate = parameters?.validation.status ?? "pending";
  const modelGate = statusFrom(validation, "model") ?? mesh?.status ?? "pending";
  const solverStatus = submission?.status ?? valueStatus(datacheck) ?? "pending";
  const pendingConfirmationKeys = parameterRows
    .filter((row) => row.value !== null && row.value !== undefined && (row.requires_confirmation || row.confirmation_status === "pending"))
    .map((row) => row.key);

  const reportError = useCallback((reason: unknown) => {
    setError(reason instanceof Error ? reason.message : String(reason));
  }, []);

  const openParameterTrace = useCallback(async (item: ReviewTableItem) => {
    if (!projectId) return;
    setTraceLoading(true);
    setParameterTrace(null);
    try {
      setParameterTrace(await getParameterTrace(projectId, item.key));
    } catch (reason) {
      reportError(reason);
    } finally {
      setTraceLoading(false);
    }
  }, [projectId, reportError]);

  const openRevisionHistory = useCallback(async () => {
    if (!projectId) return;
    try {
      const history = await getRevisions(projectId);
      setRevisions(history);
      const ordered = [...history.items].sort((a, b) => a.revision - b.revision);
      if (ordered.length >= 2) {
        setRevisionDiff(await getRevisionDiff(
          projectId,
          ordered[ordered.length - 2].revision,
          ordered[ordered.length - 1].revision,
        ));
      } else {
        setRevisionDiff(null);
      }
    } catch (reason) {
      reportError(reason);
    }
  }, [projectId, reportError]);

  useEffect(() => {
    void Promise.allSettled([getModelLibrary(), getKnowledgeBasesStatus(), getCapabilities()]).then((responses) => {
      const [libraryResponse, knowledgeResponse, capabilitiesResponse] = responses;
      if (libraryResponse.status === "fulfilled") setModelLibrary(libraryResponse.value);
      if (knowledgeResponse.status === "fulfilled") setKnowledgeBases(knowledgeResponse.value);
      if (capabilitiesResponse.status === "fulfilled") setCapabilities(capabilitiesResponse.value);
    });
  }, []);

  usePolling({
    enabled: Boolean(batch && !TERMINAL_BATCH.has(batch.status)),
    intervalMs: 900,
    pollKey: batch?.batch_id,
    poll: async () => getInputBatch(batch!.batch_id),
    onValue: setBatch,
    onError: reportError,
  });

  useEffect(() => {
    setEngineeringDraftValues(Object.fromEntries(
      (batch?.review_parameters ?? []).map((item) => [item.key, item.value]),
    ));
  }, [batch?.review_revision, batch?.engineering_review?.status]);

  usePolling({
    enabled: Boolean(
      projectId &&
      submission?.submission_id &&
      !TERMINAL_SUBMISSION.has(submission.status),
    ),
    intervalMs: 1400,
    pollKey: projectId && submission?.submission_id
      ? `${projectId}:${submission.submission_id}`
      : null,
    poll: async () => getSubmission(projectId!, submission!.submission_id),
    onValue: setSubmission,
    onError: reportError,
  });

  useEffect(() => {
    if (!projectId || !submission || !TERMINAL_SUBMISSION.has(submission.status)) return;
    void Promise.allSettled([
      getResults(projectId).then(setResults),
      getResultMesh(projectId).then(setResultMesh),
      getArtifacts(projectId).then(setArtifacts),
    ]);
  }, [projectId, submission?.status]);

  useEffect(() => {
    setFemDraftValues(Object.fromEntries(
      Object.entries(batch?.fem_specification?.parameters ?? {}).map(
        ([key, item]) => [key, item.value],
      ),
    ));
  }, [batch?.fem_review_revision, batch?.fem_specification?.status]);

  function hydrateParameters(response: ParameterSetResponse) {
    setParameters(response);
    window.localStorage.setItem("windtower.lastProjectId", response.parameter_set.project_id);
    setDraftValues(
      Object.fromEntries(
        Object.values(response.parameter_set.parameters).map((row) => [row.key, row.value]),
      ),
    );
  }

  async function run(label: string, action: () => Promise<void>) {
    setBusy(label);
    setError("");
    try {
      await action();
    } catch (reason) {
      reportError(reason);
    } finally {
      setBusy("");
    }
  }

  function resetGeneratedState() {
    setValidation(null);
    setValidationSummary(null);
    setMesh(null);
    setMeshPreview(null);
    setBuild(null);
    setDatacheck(null);
    setSubmission(null);
    setResults(null);
    setResultMesh(null);
    setArtifacts(null);
    setArtifactPreview(null);
    setViewContent("mesh");
    setOutputTab("model");
  }

  function startNewValidation() {
    window.localStorage.removeItem("windtower.lastProjectId");
    setFiles([]);
    setReferenceFile(null);
    setSelectedLibraryId("");
    setGoal(DEFAULT_GOAL);
    setBatch(null);
    setParameters(null);
    setDraftValues({});
    setEngineeringDraftValues({});
    setFemDraftValues({});
    setParameterMode("natural");
    setPreparationDomain("engineering");
    setInstruction("");
    setPreview(null);
    setParameterTrace(null);
    setRevisions(null);
    setRevisionDiff(null);
    setRepairPreview(null);
    setError("");
    setBusy("");
    setLeftOpen(true);
    setRightOpen(false);
    resetGeneratedState();
  }

  async function restoreLatestProject() {
    await run("恢复最近项目", async () => {
      const latest = await getLatestProject();
      const response = await getParameters(latest.project_id);
      hydrateParameters(response);
      const [artifactResponse, previewResponse] = await Promise.allSettled([
        getArtifacts(latest.project_id),
        getMeshPreview(latest.project_id),
      ]);
      if (artifactResponse.status === "fulfilled") setArtifacts(artifactResponse.value);
      if (previewResponse.status === "fulfilled") setMeshPreview(previewResponse.value);
      setLeftOpen(false);
    });
  }

  async function uploadBatch(nextFiles = files) {
    if (!goal.trim()) throw new Error("请先填写建模目标。");
    const created = await createInputBatch(
      nextFiles,
      goal.trim(),
      parseMode,
      llmExecutionMode,
      referenceFile,
      selectedLibraryId,
      capabilityId,
    );
    setBatch(created);
    setFiles(nextFiles);
    setReferenceFile(null);
    setParameters(null);
    setDraftValues({});
    setPreview(null);
    resetGeneratedState();
  }

  async function removeReference() {
    if (!batch) return;
    setBatch(await deleteReferenceModel(batch.batch_id));
    setReferenceFile(null);
    setSelectedLibraryId("");
  }

  async function chooseLibraryModel() {
    if (!batch) throw new Error("请先上传工程资料并建立输入批次。");
    if (!selectedLibraryId) throw new Error("请选择一个已验证模型。");
    setBatch(await selectLibraryModel(batch.batch_id, selectedLibraryId));
    setReferenceFile(null);
  }

  async function replaceReferenceModel() {
    if (!batch) throw new Error("请先建立输入批次。");
    if (!referenceFile) throw new Error("请先选择参考 INP。");
    setBatch(await putReferenceModel(batch.batch_id, referenceFile));
    setSelectedLibraryId("");
    setReferenceFile(null);
  }

  async function appendFiles(nextFiles: File[]) {
    if (!batch || !nextFiles.length) return;
    setBatch(await appendInputFiles(batch.batch_id, nextFiles));
    setFiles((current) => [...current, ...nextFiles]);
  }

  async function confirmAllAndCreate() {
    if (!batch || !hasPreparedEngineeringReview(batch, engineeringReviewRows.length)) {
      throw new Error("参数审查尚未准备完成。");
    }
    if (missingPreparationKeys.length) {
      throw new Error(`还需补充 ${missingPreparationKeys.length} 项参数。`);
    }
    const confirmed = await confirmAndCreateInputBatch(batch.batch_id, {
      engineering_review_revision: batch.review_revision,
      fem_review_revision: batch.fem_review_revision ?? 1,
      engineering_parameters: engineeringReviewRows.map((item) => ({
        key: item.key,
        value: engineeringDraftValues[item.key] ?? item.value,
        unit: item.unit ?? null,
      })),
      fem_parameters: femReviewRows
        .filter(([key]) => isFemParameterEditable(batch, key))
        .map(([key, item]) => ({
          key,
          value: femDraftValues[key] ?? item.value,
        })),
      operator: "engineer",
    });
    setBatch(confirmed.input_batch);
    const parameterResponse = await getParameters(confirmed.project_id);
    hydrateParameters(parameterResponse);
    await refreshMesh(confirmed.project_id);
    setArtifacts(await getArtifacts(confirmed.project_id));
    await refreshWorkflow();
  }

  async function refreshMesh(activeProjectId = projectId) {
    if (!activeProjectId) return;
    const [meshReport, previewReport] = await Promise.all([
      meshProject(activeProjectId),
      getMeshPreview(activeProjectId),
    ]);
    setMesh(meshReport);
    setMeshPreview(previewReport);
    setViewContent("mesh");
    await refreshWorkflow();
  }

  async function previewPendingConfirmations() {
    if (!projectId || !parameters || !pendingConfirmationKeys.length) return;
    setParameterMode("direct");
    setPreview(await previewParameterChanges(projectId, {
      base_revision: parameters.parameter_set.revision,
      confirm_keys: pendingConfirmationKeys,
    }));
  }

  async function previewChanges() {
    if (!projectId || !parameters) return;
    const body = parameterMode === "natural"
      ? {
          base_revision: parameters.parameter_set.revision,
          instruction: instruction.trim(),
        }
      : {
          base_revision: parameters.parameter_set.revision,
          changes: changedParameters,
        };
    setPreview(await previewParameterChanges(projectId, body));
  }

  async function confirmChanges() {
    if (!projectId || !preview) return;
    let response: ParameterSetResponse;
    try {
      response = await confirmParameterChanges(projectId, {
        preview_id: preview.preview_id,
        base_revision: preview.base_revision,
        operator: "engineer",
      });
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        hydrateParameters(await getParameters(projectId));
        setPreview(null);
        throw new Error("参数版本已更新，已刷新到最新 revision，请重新预览差异。");
      }
      throw reason;
    }
    hydrateParameters(response);
    setPreview(null);
    setInstruction("");
    resetGeneratedState();
    await refreshMesh(projectId);
    setArtifacts(await getArtifacts(projectId));
    await refreshWorkflow();
  }

  async function validateAndMesh() {
    if (!projectId) return;
    const gate = await validateProject(projectId);
    setValidation(gate);
    await refreshMesh(projectId);
  }

  async function buildModel() {
    if (!projectId) return;
    setBuild(await buildProject(projectId));
    setArtifacts(await getArtifacts(projectId));
    setValidationSummary(await getValidationSummary(projectId));
    await refreshWorkflow();
  }

  async function publishModel() {
    if (!projectId || !parameters) return;
    const label = `风塔模型 ${projectId} R${parameters.parameter_set.revision}`;
    const published = await publishProjectModel(projectId, label);
    const refreshed = await getModelLibrary();
    setModelLibrary(refreshed);
    setSelectedLibraryId(published.model_id);
  }

  async function runDatacheck() {
    if (!projectId) return;
    setDatacheck(await datacheckProject(projectId, "all"));
    setArtifacts(await getArtifacts(projectId));
    setValidationSummary(await getValidationSummary(projectId));
    setOutputTab("validation");
    await refreshWorkflow();
  }

  async function previewRepair() {
    if (!projectId) return;
    setRepairPreview(await previewModelRepair(projectId));
    setOutputTab("model");
  }

  async function submitSolver() {
    if (!projectId || !selectedJobs.length) return;
    setSubmission(await submitProject(projectId, selectedJobs));
    setOutputTab("results");
  }

  async function refreshResults() {
    if (!projectId) return;
    const [resultReport, artifactReport] = await Promise.all([
      getResults(projectId),
      getArtifacts(projectId),
    ]);
    setResults(resultReport);
    setArtifacts(artifactReport);
    try {
      const resultMeshReport = await getResultMesh(projectId);
      setResultMesh(resultMeshReport);
      setViewContent("result");
    } catch {
      setResultMesh(null);
    }
  }

  async function previewArtifact(item: ArtifactItem) {
    if (!projectId || !item.previewable) return;
    const response = await fetch(artifactUrl(projectId, item.name));
    if (!response.ok) throw new Error("文件预览失败，请稍后重试。");
    const content = (await response.text()).slice(0, 160_000);
    setArtifactPreview({ item, content });
  }

  function setDroppedFiles(nextFiles: File[]) {
    if (!nextFiles.length) return;
    setFiles(nextFiles);
  }

  return (
    <main className="workbench">
      <header className="topbar">
        <div className="brand" data-testid="centered-brand">
          <div className="brand-mark"><Box size={19} /></div>
          <div>
            <strong>AutoFEM WindTower</strong>
            <span>LLM 辅助参数化有限元建模工作台</span>
          </div>
        </div>
        <div className="topbar-actions">
          <LanguageSwitcher />
          {projectId ? (
            <button className="project-switch-button" onClick={startNewValidation} type="button">
              <FilePlus2 size={16} />
              <span>新建验证</span>
            </button>
          ) : (
            <button
              className="project-switch-button"
              disabled={busy === "恢复最近项目"}
              onClick={() => void restoreLatestProject()}
              type="button"
            >
              {busy === "恢复最近项目" ? <LoaderCircle className="spin" size={16} /> : <History size={16} />}
              <span>恢复最近项目</span>
            </button>
          )}
          <div className="drawer-actions">
            <button className="icon-button" onClick={() => setLeftOpen(true)} title="打开输入栏" type="button">
              <UploadCloud size={18} />
            </button>
            <button className="icon-button" onClick={() => setRightOpen(true)} title="打开参数栏" type="button">
              <Menu size={18} />
            </button>
          </div>
        </div>
      </header>

      <WorkflowStepper loading={workflowLoading} state={workflowState} />

      {error ? (
        <div className="global-error">
          <AlertTriangle size={16} />
          <span>{error}</span>
          <button onClick={() => setError("")} title="关闭" type="button"><X size={15} /></button>
        </div>
      ) : null}

      <section className="workspace-grid">
        <aside className={`side-panel left-panel ${leftOpen ? "drawer-open" : ""}`}>
          <PanelHeading
            title="输入与解析"
            icon={<UploadCloud size={17} />}
            onClose={() => setLeftOpen(false)}
          />
          <div className="panel-scroll">
            {projectId ? (
              <section className="project-input-summary">
                <span className="section-kicker">已完成 · 输入资料</span>
                <strong>{batch?.files[0]?.filename ?? "已建立正式参数集"}</strong>
                <p>{goal}</p>
                <small>{batch?.files.length ?? 0} 个文件 · 项目 {projectId}</small>
              </section>
            ) : (<>
            <section className="form-section">
              <label className="field-label" htmlFor="capability-select">建模能力</label>
              <select
                id="capability-select"
                value={capabilityId}
                onChange={(event) => {
                  const next = event.target.value;
                  setCapabilityId(next);
                  setReferenceFile(null);
                  setSelectedLibraryId("");
                  if (next === "WT-FLANGE-BOLT-CONTACT-PRELOAD-V1") {
                    setGoal("建立局部上下塔节单法兰模型，采用 S4R 塔壁与法兰、B31 螺栓、法兰面接触及原生预紧，执行预紧—锁定—外载三个非线性静力步。");
                    setJobs({ static_strength: true, modal: false });
                  } else {
                    setGoal(DEFAULT_GOAL);
                    setJobs({ static_strength: true, modal: true });
                  }
                }}
              >
                {(capabilities?.items ?? []).map((capability) => (
                  <option key={capability.capability_id} value={capability.capability_id}>
                    {capability.complexity_level ? `${capability.complexity_level} · ` : "L1 · "}{capability.label}
                  </option>
                ))}
                {!capabilities?.items.length ? <>
                  <option value="WT-SHELL-DOOR-STATIC-MODAL-V1">L1 · 带门洞整塔壳模型</option>
                  <option value="WT-FLANGE-BOLT-CONTACT-PRELOAD-V1">L2 · 局部法兰螺栓接触预紧</option>
                </> : null}
              </select>
              <small className="field-help">
                {capabilityId === "WT-FLANGE-BOLT-CONTACT-PRELOAD-V1"
                  ? "范围固定为局部双塔节、单法兰、B31 螺栓与三步预紧；不支持实体螺栓或多法兰。"
                  : "带门洞整塔 S4R 壳模型，支持线性静力与模态分析。"}
              </small>
              <label className="field-label" htmlFor="modeling-goal">建模目标</label>
              <textarea
                id="modeling-goal"
                className="goal-input"
                value={goal}
                onChange={(event) => setGoal(event.target.value)}
              />
              <details className="advanced-options">
                <summary>解析设置</summary>
                <div className="segmented full">
                  <button className={parseMode === "llm_full" ? "active" : ""} onClick={() => setParseMode("llm_full")} type="button">纯 AI 参数形成</button>
                  <button className={parseMode === "llm" ? "active" : ""} onClick={() => setParseMode("llm")} type="button">本地优先 + AI 补全</button>
                  <button className={parseMode === "deterministic" ? "active" : ""} onClick={() => setParseMode("deterministic")} type="button">仅本地解析</button>
                </div>
                {parseMode !== "deterministic" ? (
                  <label className="field-label" htmlFor="llm-execution-mode">
                    顾问调用策略
                    <select
                      id="llm-execution-mode"
                      value={llmExecutionMode}
                      onChange={(event) => setLlmExecutionMode(event.target.value as LlmExecutionMode)}
                    >
                      <option value="single">单次整批解析（默认）</option>
                      <option value="chunked_parallel">大数据分块并发解析</option>
                    </select>
                    <small>常规工程资料只调用一次；仅在内容过多时启用分块并发。</small>
                  </label>
                ) : null}
              </details>
            </section>

            <section className="form-section">
              <div className="simple-section-title">工程资料 <span>可选</span></div>
              <div
                className="dropzone"
                onClick={() => uploadRef.current?.click()}
                onDragOver={(event) => event.preventDefault()}
                onDrop={(event) => {
                  event.preventDefault();
                  setDroppedFiles(Array.from(event.dataTransfer.files));
                }}
                role="button"
                tabIndex={0}
              >
                <UploadCloud size={26} />
                <strong>拖拽工程资料到此处</strong>
                <span>Excel、CSV、JSON、YAML、TXT、DOCX、文字层 PDF</span>
                <input
                  ref={uploadRef}
                  accept=".xls,.xlsx,.xlsm,.csv,.tsv,.json,.yaml,.yml,.txt,.md,.docx,.pdf"
                  hidden
                  multiple
                  type="file"
                  onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
                />
              </div>
              {files.length ? (
                <div className="selected-files">
                  {files.slice(0, 5).map((file) => (
                    <div key={`${file.name}-${file.size}`}>
                      <FileText size={14} />
                      <span title={file.name}>{file.name}</span>
                      <small>{formatBytes(file.size)}</small>
                    </div>
                  ))}
                  {files.length > 5 ? <small>另有 {files.length - 5} 个文件</small> : null}
                </div>
              ) : null}
            </section>

            <section className="form-section reference-section">
              <div className="section-title-row">
                <div>
                  <strong>参考模型</strong>
                  <span className="optional-label">可选</span>
                </div>
                <StateBadge value={batch?.capability_match?.status ?? "pending"} />
              </div>
              <p className="section-description">
                可上传参考 INP、选择已验证模型，或直接采用当前能力配置。
              </p>
              <label className="field-label" htmlFor="model-library-select">已验证模型库</label>
              <select
                disabled={Boolean(busy)}
                id="model-library-select"
                value={selectedLibraryId}
                onChange={(event) => {
                  setSelectedLibraryId(event.target.value);
                  if (event.target.value) setReferenceFile(null);
                }}
              >
                <option value="">选择相近模型（可选）</option>
                {(batch?.model_library_recommendations?.length
                  ? batch.model_library_recommendations
                  : modelLibrary?.items ?? []).map((item) => (
                  <option key={item.model_id} value={item.model_id}>
                    {item.label} · {validationLevelLabel(item.validation_level)}
                    {typeof item.similarity_score === "number" ? ` · 匹配 ${Math.round(item.similarity_score * 100)}%` : ""}
                  </option>
                ))}
              </select>
              <div className="source-divider"><span>或临时参考 INP</span></div>
              <input
                ref={referenceRef}
                accept=".inp"
                hidden
                type="file"
                onChange={(event) => {
                  setReferenceFile(event.target.files?.[0] ?? null);
                  if (event.target.files?.[0]) setSelectedLibraryId("");
                }}
              />
              <button className="reference-picker" onClick={() => referenceRef.current?.click()} type="button">
                <FileText size={15} />
                <span>{referenceFile?.name ?? referenceSummary?.filename ?? "选择参考 INP"}</span>
              </button>
              {referenceSummary ? (
                <div className={`reference-summary ${referenceSummary.compatibility}`}>
                  <div><strong>{humanCompatibility(referenceSummary.compatibility)}</strong><span>{referenceSummary.element_count?.toLocaleString() ?? 0} 个单元</span></div>
                  <p>
                    {Object.keys(referenceSummary.element_types ?? {}).join("、") || "未识别单元"}
                    {referenceSummary.steps?.length ? ` · ${referenceSummary.steps.length} 个分析步` : ""}
                  </p>
                  {batch?.selected_library_model ? <p>来源：已验证模型 · {batch.selected_library_model.label}</p> : null}
                  {referenceSummary.compatibility_reasons?.map((reason) => <small key={reason}>{reason}</small>)}
                </div>
              ) : batch ? (
                <div className="notice neutral">未提供参考模型，将使用当前风塔能力配置候选。</div>
              ) : null}
              {batch?.fem_specification ? (
                <div className={`notice ${femConfirmed ? "success" : "neutral"}`}>
                  {femConfirmed ? <Check size={15} /> : <Settings2 size={15} />}
                  {femConfirmed ? "FEM 规范已确认" : "请在右侧审查 FEM 参数候选。"}
                </div>
              ) : null}
              <button
                className="primary-button full combined-parse-button"
                disabled={!goal.trim() || Boolean(busy)}
                onClick={() => void run("upload", () => uploadBatch())}
                type="button"
              >
                {busy === "upload" ? <LoaderCircle className="spin" size={17} /> : <UploadCloud size={17} />}
                解析工程资料与参考模型
              </button>
              <small className="source-note">
                工程资料和参考模型均可选；仅填写明确建模目标也可开始，由工程师确认全部建议值。
              </small>
            </section>
            </>)}

            {batch ? (
              <section className={`form-section batch-section ${projectId ? "compact" : ""}`}>
                <div className="section-title-row">
                  <div>
                    <span className="section-kicker">解析状态</span>
                    <strong>{humanStatus(batch.status)}</strong>
                  </div>
                  <StateBadge value={batch.status} />
                </div>
                <Progress value={batch.progress} />
                <div className="batch-files">
                  {batch.files.map((file) => (
                    <div className="batch-file" key={file.file_id}>
                      <FileText size={14} />
                      <div>
                        <span title={file.filename}>{file.filename}</span>
                        <small>{file.processing_method || "解析"} · {file.candidate_count ?? 0} 个候选</small>
                      </div>
                      <StateDot value={file.status} />
                    </div>
                  ))}
                </div>

                {batch.warnings.length ? (
                  <div className="notice warning">
                    <AlertTriangle size={15} />
                    <div>{batch.warnings.slice(0, 4).map((warning) => <p key={warning}>{warning}</p>)}</div>
                  </div>
                ) : null}

                {batch.conflicts.length ? (
                  <div className="conflicts">
                    <span className="section-kicker">冲突确认</span>
                    {batch.conflicts.map((conflict) => (
                      <div className="conflict-card" key={conflict.conflict_id}>
                        <strong>{safeParameterLabel(conflict.key, conflict.label)}</strong>
                        <div className="candidate-list">
                          {conflict.candidate_ids.map((candidateId) => {
                            const candidate = batch.candidates.find((item) => item.candidate_id === candidateId);
                            if (!candidate) return null;
                            const selected = conflict.resolved_candidate_id === candidateId;
                            return (
                              <button
                                className={selected ? "candidate selected" : "candidate"}
                                key={candidateId}
                                onClick={() => void run("conflict", async () => {
                                  setBatch(await resolveInputConflict(batch.batch_id, conflict.conflict_id, candidateId));
                                })}
                                type="button"
                              >
                                <CircleDot size={13} />
                                {formatValue(candidate.normalized_value)} {candidate.normalized_unit ?? ""}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : null}

                {projectId ? (
                  <div className="notice success"><Check size={15} />正式参数集已建立</div>
                ) : null}
                <details className="advanced-options batch-advanced">
                  <summary>高级操作</summary>
                  <div className="button-row">
                    <input
                      ref={appendRef}
                      hidden
                      multiple
                      type="file"
                      onChange={(event) => {
                        const nextFiles = Array.from(event.target.files ?? []);
                        void run("append", () => appendFiles(nextFiles));
                      }}
                    />
                    <button className="ghost-button" onClick={() => appendRef.current?.click()} type="button">
                      <FilePlus2 size={15} />追加资料
                    </button>
                    <button className="ghost-button" disabled={Boolean(busy)} onClick={() => void run("reparse", async () => setBatch(await reparseInputBatch(batch.batch_id, parseMode)))} type="button">
                      <RefreshCw size={15} />重新解析
                    </button>
                  </div>
                  {referenceSummary ? (
                    <button className="ghost-button full" disabled={Boolean(busy)} onClick={() => void run("remove-reference", removeReference)} type="button">
                      <X size={15} />移除参考模型
                    </button>
                  ) : null}
                  {referenceFile ? (
                    <button className="ghost-button full" disabled={Boolean(busy)} onClick={() => void run("replace-reference", replaceReferenceModel)} type="button">
                      <FileText size={15} />替换为所选参考 INP
                    </button>
                  ) : null}
                  {selectedLibraryId ? (
                    <button className="ghost-button full" disabled={Boolean(busy)} onClick={() => void run("select-library", chooseLibraryModel)} type="button">
                      <FolderOpen size={15} />替换为所选模型规范
                    </button>
                  ) : null}
                </details>
                <details className="technical-details">
                  <summary>技术详情</summary>
                  <dl>
                    <div><dt>批次编号</dt><dd>{batch.batch_id}</dd></div>
                    <div><dt>解析模式</dt><dd>{batch.parse_mode === "llm_full" ? "纯 AI 参数形成" : batch.parse_mode === "llm" ? "本地优先 + AI 补全" : "确定性解析"}</dd></div>
                    <div><dt>调用策略</dt><dd>{batch.llm_execution_mode === "chunked_parallel" ? "大数据分块并发" : "单次整批"}</dd></div>
                    <div><dt>模型服务</dt><dd>{[batch.provider, batch.model].filter(Boolean).join(" / ") || "本地解析"}</dd></div>
                    <div><dt>工程知识库</dt><dd>{knowledgeBases?.engineering?.status ?? "未索引"}</dd></div>
                    <div><dt>FEM 知识库</dt><dd>{knowledgeBases?.fem?.status ?? "未索引"}</dd></div>
                  </dl>
                  {batch.llm_partial_failure ? <pre>{JSON.stringify(batch.llm_partial_failure, null, 2)}</pre> : null}
                </details>
              </section>
            ) : null}
          </div>
          <EngineeringStatusPanel
            items={[
              { label: "项目", value: projectId ?? "待创建", statusTone: projectId ? "pass" : "idle" },
              { label: "Revision", value: parameters ? `R${parameters.parameter_set.revision}` : "—", statusTone: parameters ? "pass" : "idle" },
              { label: "当前步骤", value: workflowState?.steps.find((step) => step.id === workflowState.current_step)?.label ?? "输入资料", statusTone: tone(workflowState?.steps.find((step) => step.id === workflowState.current_step)?.status) },
            ]}
          />
        </aside>

        <ViewportWorkspace
          busy={Boolean(busy)}
          deformationScale={deformationScale}
          highlight={highlight}
          mesh={mesh}
          meshDataUrl={apiAssetUrl(meshPreview?.mesh_data_url)}
          meshPreview={meshPreview}
          onDeformationScaleChange={setDeformationScale}
          onError={reportError}
          onHighlightChange={setHighlight}
          onRefresh={() => void run("mesh", () => refreshMesh())}
          onScalarChange={setScalar}
          onViewContentChange={setViewContent}
          onViewModeChange={setViewMode}
          projectReady={Boolean(projectId)}
          resultMesh={resultMesh}
          scalar={scalar}
          viewContent={viewContent}
          viewMode={viewMode}
        />

        <aside className={`side-panel right-panel ${rightOpen ? "drawer-open" : ""}`}>
          <section className="right-section parameter-section">
            <PanelHeading
              title={parameters ? "参数修改" : "参数审查"}
              icon={<Settings2 size={17} />}
              meta={parameters ? `R${parameters.parameter_set.revision}` : "待建立"}
              onClose={() => setRightOpen(false)}
            />
            {!parameters && batch ? (
              <PreparationConfirmBar
                busy={busy}
                engineeringCount={engineeringReviewRows.length}
                femCount={femReviewRows.length}
                missingCount={missingPreparationKeys.length}
                onConfirm={() => void run("confirm-all", confirmAllAndCreate)}
                ready={
                  TERMINAL_BATCH.has(batch.status) &&
                  Boolean(batch.readiness?.preview_allowed) &&
                  batch.capability_match?.status !== "incompatible"
                }
                referenceCount={referenceCandidateCount}
              />
            ) : null}
            {parameters ? (
              <div className="parameter-mode-tabs">
                <button className={parameterMode === "natural" ? "active" : ""} onClick={() => setParameterMode("natural")} type="button">自然语言修改</button>
                <button className={parameterMode === "direct" ? "active" : ""} onClick={() => setParameterMode("direct")} type="button">直接编辑参数</button>
                <button className={parameterMode === "evidence" ? "active" : ""} onClick={() => { setParameterMode("evidence"); setParameterTrace(null); }} type="button">证据</button>
                <button className={parameterMode === "revisions" ? "active" : ""} onClick={() => { setParameterMode("revisions"); void openRevisionHistory(); }} type="button"><History size={13} />版本</button>
              </div>
            ) : (
              <div className="parameter-mode-tabs">
                <button className={preparationDomain === "engineering" ? "active" : ""} onClick={() => setPreparationDomain("engineering")} type="button">工程参数</button>
                <button className={preparationDomain === "fem" ? "active" : ""} onClick={() => setPreparationDomain("fem")} type="button">FEM 参数</button>
              </div>
            )}
            {parameters && pendingConfirmationKeys.length ? (
              <div className="workflow-blocker-card">
                <AlertTriangle size={16} />
                <div>
                  <strong>{workflowState?.blockers[0]?.message ?? `还有 ${pendingConfirmationKeys.length} 项参数待确认`}</strong>
                  <small>{pendingConfirmationKeys.map((key) => safeParameterLabel(key, parameters.parameter_set.parameters[key]?.label ?? key)).join("、")}</small>
                </div>
                <button disabled={Boolean(busy)} onClick={() => void run("preview-pending", previewPendingConfirmations)} type="button">去确认参数</button>
              </div>
            ) : null}
            <div className="right-scroll parameter-scroll">
              {!parameters ? (
                <PreparationParameterReviewPanel
                  batch={batch}
                  domain={preparationDomain}
                  engineeringDraftValues={engineeringDraftValues}
                  femDraftValues={femDraftValues}
                  setEngineeringDraftValue={(key, value) => setEngineeringDraftValues((current) => ({ ...current, [key]: value }))}
                  setFemDraftValue={(key, value) => setFemDraftValues((current) => ({ ...current, [key]: value }))}
                />
              ) : parameterMode === "evidence" ? (
                parameterTrace || traceLoading ? (
                  <ParameterTracePanel trace={parameterTrace} loading={traceLoading} onBack={() => setParameterTrace(null)} />
                ) : (
                  <ParameterReviewTable items={reviewItems} onSelect={(item) => void openParameterTrace(item)} />
                )
              ) : parameterMode === "revisions" ? (
                <RevisionPanel
                  revisions={revisions}
                  diff={revisionDiff}
                  onBack={() => setParameterMode("evidence")}
                />
              ) : parameterMode === "natural" ? (
                <section className="natural-change">
                  <label className="field-label" htmlFor="parameter-instruction">修改目标</label>
                  <textarea
                    id="parameter-instruction"
                    placeholder="例如：门洞宽度增加 5%，全局网格尺寸调整为 350 mm"
                    value={instruction}
                    onChange={(event) => setInstruction(event.target.value)}
                  />
                  <div className="scope-note">
                    <Check size={14} />
                    LLM 仅映射至允许参数，不生成节点、单元或 INP。
                  </div>
                </section>
              ) : (
                <ParameterEditorPanel
                  draftValues={draftValues}
                  groupedTowerRows={groupedTowerRows}
                  rows={parameterRows}
                  setDraftValue={(key, value) => setDraftValues((current) => ({ ...current, [key]: value }))}
                />
              )}

              {preview ? (
                <section className="preview-card">
                  <div className="section-title-row">
                    <div>
                      <span className="section-kicker">差异预览</span>
                      <strong>{preview.diff.length} 项变化</strong>
                    </div>
                    <StateBadge value={preview.validation.status} />
                  </div>
                  <div className="diff-list">
                    {preview.diff.map((item) => (
                      <div key={item.key}>
                        <span>{safeParameterLabel(item.key, item.label)}</span>
                        <p>
                          <del>{formatValue(item.old_value)}</del>
                          <ChevronRight size={13} />
                          <ins>{formatValue(item.new_value)} {item.unit ?? ""}</ins>
                        </p>
                      </div>
                    ))}
                  </div>
                  {preview.validation.issues.length ? (
                    <div className="validation-issues">
                      {preview.validation.issues.map((issue, index) => (
                        <p key={`${issue.key}-${index}`}>{issue.message || issue.category}</p>
                      ))}
                    </div>
                  ) : null}
                  <button
                    className="primary-button full"
                    disabled={preview.validation.status !== "pass" || Boolean(busy)}
                    onClick={() => void run("confirm-change", confirmChanges)}
                    type="button"
                  >
                    <Check size={16} />工程师确认并创建新版本
                  </button>
                </section>
              ) : null}
            </div>
            {parameters && (parameterMode === "natural" || parameterMode === "direct") ? (
              <div className="parameter-actionbar">
                <span>{parameterMode === "direct" ? `${changedParameters.length} 项待修改` : "先预览，再确认"}</span>
                <button
                  className="primary-button"
                  disabled={
                    Boolean(busy) ||
                    (parameterMode === "natural" ? !instruction.trim() : !changedParameters.length)
                  }
                  onClick={() => void run("preview-change", previewChanges)}
                  type="button"
                >
                  <Eye size={15} />预览差异
                </button>
              </div>
            ) : null}
          </section>

          <section className="right-section output-section">
            <div className="output-tabs">
              <button className={outputTab === "model" ? "active" : ""} onClick={() => setOutputTab("model")} type="button">模型</button>
              <button className={outputTab === "validation" ? "active" : ""} onClick={() => setOutputTab("validation")} type="button">验证</button>
              <button className={outputTab === "results" ? "active" : ""} onClick={() => setOutputTab("results")} type="button">结果</button>
              <button className={outputTab === "files" ? "active" : ""} onClick={() => setOutputTab("files")} type="button">文件</button>
            </div>
            <div className="right-scroll output-scroll">
              {outputTab === "model" ? (
                <ModelActions
                  build={build}
                  busy={busy}
                  disabled={!projectId}
                  jobs={jobs}
                  mesh={mesh}
                  workflow={workflowState}
                  repairPreview={repairPreview}
                  onBuild={() => void run("build", buildModel)}
                  onDatacheck={() => void run("datacheck", runDatacheck)}
                  onJobsChange={(job, checked) => setJobs((current) => ({ ...current, [job]: checked }))}
                  onSubmit={() => void run("solve", submitSolver)}
                  onPublish={() => void run("publish-model", publishModel)}
                  onRepair={() => void run("repair-preview", previewRepair)}
                  onValidate={() => void run("validate", validateAndMesh)}
                  submission={submission}
                />
              ) : outputTab === "validation" ? (
                <ValidationPanel summary={validationSummary} />
              ) : outputTab === "results" ? (
                <ResultsPanel
                  disabled={!projectId}
                  onRefresh={() => void run("results", refreshResults)}
                  onShowMesh={() => setViewContent("result")}
                  resultMesh={resultMesh}
                  results={results}
                  submission={submission}
                />
              ) : (
                <ArtifactsPanel
                  artifactPreview={artifactPreview}
                  artifacts={artifacts}
                  onPreview={(item) => void run("artifact", () => previewArtifact(item))}
                  projectId={projectId}
                />
              )}
            </div>
          </section>
        </aside>
      </section>

      {(leftOpen || rightOpen) ? (
        <button
          aria-label="关闭抽屉"
          className="drawer-backdrop"
          onClick={() => {
            setLeftOpen(false);
            setRightOpen(false);
          }}
          type="button"
        />
      ) : null}
    </main>
  );
}

export function hasPreparedEngineeringReview(batch: InputBatch | null, engineeringRowCount: number) {
  if (!batch?.review_revision) return false;
  return batch.capability_id === "WT-FLANGE-BOLT-CONTACT-PRELOAD-V1" || engineeringRowCount > 0;
}

function PanelHeading({
  title,
  icon,
  meta,
  onClose,
}: {
  title: string;
  icon: React.ReactNode;
  meta?: string;
  onClose: () => void;
}) {
  return (
    <header className="panel-heading">
      <div className="panel-title-icon">{icon}</div>
      <div>
        <h2>{title}</h2>
      </div>
      {meta ? <small>{meta}</small> : null}
      <button className="drawer-close" onClick={onClose} type="button"><X size={16} /></button>
    </header>
  );
}

function EngineeringStatusPanel({
  items,
}: {
  items: Array<{
    label: string;
    value: string;
    statusTone: "pass" | "warning" | "danger" | "idle" | "working";
  }>;
}) {
  return (
    <section className="engineering-status-panel" data-testid="engineering-status-panel">
      <div className="engineering-status-heading">工程状态</div>
      <div className="engineering-status-grid">
        {items.map((item) => (
          <div className="engineering-status-item" key={item.label}>
            <span>{item.label}</span>
            <strong className={item.statusTone}><i />{item.value}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

export function TechnicalDetails({ children }: { children: React.ReactNode }) {
  return (
    <details className="technical-details">
      <summary>技术详情</summary>
      <div className="technical-details-content">{children}</div>
    </details>
  );
}

function ResultDetails({ results }: { results: Record<string, unknown> }) {
  return (
    <TechnicalDetails>
      <pre>{JSON.stringify(results, null, 2)}</pre>
    </TechnicalDetails>
  );
}

function StateBadge({ value }: { value: string }) {
  return <span className={`state-badge ${tone(value)}`}>{humanStatus(value)}</span>;
}

function StateDot({ value }: { value: string }) {
  return <span className={`state-dot ${tone(value)}`} title={humanStatus(value)} />;
}

function Progress({ value }: { value: number }) {
  return (
    <div className="progress-row">
      <div><span style={{ width: `${Math.max(0, Math.min(value, 100))}%` }} /></div>
      <strong>{value}%</strong>
    </div>
  );
}

function EmptyPanel({ title, text }: { title: string; text: string }) {
  return (
    <div className="empty-panel">
      <Box size={28} />
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  );
}

function ParameterEditor({
  rows,
  groupedTowerRows,
  draftValues,
  setDraftValue,
}: {
  rows: ParameterRecord[];
  groupedTowerRows: Array<{ index: number; rows: ParameterRecord[] }>;
  draftValues: Record<string, unknown>;
  setDraftValue: (key: string, value: unknown) => void;
}) {
  const domains: Array<{ key: ParameterRecord["domain"]; label: string; note: string }> = [
    { key: "engineering", label: "工程参数", note: "来自结构资料与工程师确认" },
    { key: "fem", label: "FEM 参数", note: "来自参考规范与能力配置" },
  ];
  return (
    <div className="parameter-editor">
      {domains.map((domain) => (
        <section className="parameter-domain" key={domain.key}>
          <div className="parameter-domain-heading">
            <strong>{domain.label}</strong><span>{domain.note}</span>
          </div>
          {domain.key === "engineering" && groupedTowerRows.length ? (
            <details open>
              <summary>塔段参数 <small>{groupedTowerRows.length} 段 · {originLabel(groupedTowerRows[0].rows[0]?.origin ?? "engineering_evidence")}</small></summary>
              <div className="tower-table-wrap">
                <table className="tower-table">
                  <thead><tr><th>段</th><th>底标高</th><th>顶标高</th><th>底直径</th><th>顶直径</th><th>厚度</th></tr></thead>
                  <tbody>
                    {groupedTowerRows.map((section) => (
                      <tr key={section.index}>
                        <th>S{section.index + 1}</th>
                        {["z_bottom_mm", "z_top_mm", "diameter_bottom_mm", "diameter_top_mm", "thickness_mm"].map((field) => {
                          const row = section.rows.find((item) => item.key.endsWith(`.${field}`));
                          return (
                            <td key={field}>
                              {row ? (
                                <input
                                  aria-label={safeParameterLabel(row.key, row.label)}
                                  value={String(draftValues[row.key] ?? "")}
                                  onChange={(event) => setDraftValue(row.key, coerceDraft(event.target.value, row.value))}
                                />
                              ) : "—"}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          ) : null}
          {GROUPS.map((group) => {
            const groupRows = rows.filter(
              (row) => row.domain === domain.key && row.group === group.key && !row.key.startsWith("geometry.tower_sections."),
            );
            if (!groupRows.length) return null;
            return (
              <details key={`${domain.key}-${group.key}`} open={group.key === "geometry" || group.key === "mesh"}>
                <summary>{group.label}<small>{groupRows.length} 项</small></summary>
                <div className="parameter-form-grid">
                  {groupRows.map((row) => (
                    <label key={row.key}>
                      <span>
                        {safeParameterLabel(row.key, row.label)}
                        {row.unit ? <em>{row.unit}</em> : null}
                        <small className="origin-badge">{originLabel(row.origin)}</small>
                      </span>
                      {typeof row.value === "boolean" ? (
                        <select value={String(Boolean(draftValues[row.key]))} onChange={(event) => setDraftValue(row.key, event.target.value === "true")}>
                          <option value="true">启用</option>
                          <option value="false">禁用</option>
                        </select>
                      ) : (
                        <input
                          value={String(draftValues[row.key] ?? "")}
                          onChange={(event) => setDraftValue(row.key, coerceDraft(event.target.value, row.value))}
                        />
                      )}
                    </label>
                  ))}
                </div>
              </details>
            );
          })}
        </section>
      ))}
    </div>
  );
}

function PreparationParameterReview({
  batch,
  domain,
  engineeringDraftValues,
  femDraftValues,
  setEngineeringDraftValue,
  setFemDraftValue,
}: {
  batch: InputBatch | null;
  domain: PreparationDomain;
  engineeringDraftValues: Record<string, unknown>;
  femDraftValues: Record<string, unknown>;
  setEngineeringDraftValue: (key: string, value: unknown) => void;
  setFemDraftValue: (key: string, value: unknown) => void;
}) {
  if (!batch) {
    return <EmptyPanel title="等待输入资料" text="上传工程资料后，两个顾问分别形成工程参数与 FEM 规范候选。" />;
  }
  if (domain === "engineering") {
    const rows = [...(batch.review_parameters ?? [])].sort((left, right) => (
      Number(isMissingValue(engineeringDraftValues[left.key] ?? left.value)) * -1
      - Number(isMissingValue(engineeringDraftValues[right.key] ?? right.value)) * -1
    ));
    if (!rows.length) {
      return <EmptyPanel title="工程参数尚未就绪" text="等待工程资料提取完成，缺失值将保留为空并由工程师补充。" />;
    }
    return (
      <div className="preparation-review">
        <ReviewIntroduction
          confirmed={batch.engineering_review?.status === "confirmed"}
          text="工程顾问只映射带来源的工程事实；参考项目值和 LLM 建议不会自动写入。"
          title="工程参数候选"
        />
        <div className="preparation-parameter-list">
          {rows.map((row) => {
            const source = candidateForReview(batch, row.key, row.value);
            const current = engineeringDraftValues[row.key] ?? row.value;
            return (
              <label className={isMissingValue(current) ? "missing-parameter" : ""} key={row.key}>
                <span>
                  <strong>{safeParameterLabel(row.key, row.key)}</strong>
                  <em>{candidateSourceLabel(source?.source_kind, source?.extraction_method)}</em>
                </span>
                <div>
                  {typeof row.value === "boolean" ? (
                    <select
                      disabled={batch.engineering_review?.status === "confirmed"}
                      value={String(Boolean(current))}
                      onChange={(event) => setEngineeringDraftValue(row.key, event.target.value === "true")}
                    >
                      <option value="true">启用</option>
                      <option value="false">禁用</option>
                    </select>
                  ) : (
                    <input
                      disabled={batch.engineering_review?.status === "confirmed"}
                      value={String(current ?? "")}
                      onChange={(event) => setEngineeringDraftValue(row.key, coerceDraft(event.target.value, row.value))}
                    />
                  )}
                  <small>{row.unit ?? ""}</small>
                </div>
              </label>
            );
          })}
        </div>
      </div>
    );
  }
  const parameters = batch.fem_specification?.parameters ?? {};
  if (!Object.keys(parameters).length) {
    return <EmptyPanel title="FEM 规范尚未就绪" text="请选择模型库、上传参考 INP，或使用当前能力配置。" />;
  }
  return (
    <div className="preparation-review">
      <ReviewIntroduction
        confirmed={batch.fem_review?.status === "confirmed"}
        text="FEM 顾问解释建模规范；网格拓扑、Abaqus 关键词和 INP 均由确定性生成器负责。"
        title="FEM 参数候选"
      />
      <TransferReview transfer={batch.reference_transfer} />
      <div className="preparation-parameter-list">
        {Object.entries(parameters)
          .sort(([leftKey, left], [rightKey, right]) => (
            Number(isMissingValue(femDraftValues[leftKey] ?? left.value)) * -1
            - Number(isMissingValue(femDraftValues[rightKey] ?? right.value)) * -1
          ))
          .map(([key, item]) => (
          <label className={isMissingValue(femDraftValues[key] ?? item.value) ? "missing-parameter" : ""} key={key}>
            <span>
              <strong>{shortFemLabel(key)}</strong>
              <em>{isFemParameterEditable(batch, key) ? originLabel(item.origin) : `${originLabel(item.origin)} · 固定规则`}</em>
            </span>
            <div>
              {typeof item.value === "boolean" ? (
                <select
                  disabled={!isFemParameterEditable(batch, key) || batch.fem_review?.status === "confirmed"}
                  value={String(Boolean(femDraftValues[key]))}
                  onChange={(event) => setFemDraftValue(key, event.target.value === "true")}
                >
                  <option value="true">启用</option>
                  <option value="false">禁用</option>
                </select>
              ) : (
                <input
                  disabled={!isFemParameterEditable(batch, key) || batch.fem_review?.status === "confirmed"}
                  value={String(femDraftValues[key] ?? "")}
                  onChange={(event) => setFemDraftValue(key, coerceDraft(event.target.value, item.value))}
                />
              )}
            </div>
            {item.alternatives?.length ? <small className="alternative-note">另有 {item.alternatives.length} 个来源候选可追溯</small> : null}
          </label>
        ))}
      </div>
    </div>
  );
}

function TransferReview({ transfer }: { transfer?: InputBatch["reference_transfer"] }) {
  if (!transfer) return null;
  const reusable = transfer.reusable ?? [];
  const conditional = transfer.conditional ?? [];
  const deferred = transfer.deferred_features ?? [];
  if (!reusable.length && !conditional.length && !deferred.length) return null;
  return (
    <div className="transfer-review">
      {reusable.length ? (
        <section>
          <strong>可复用规范</strong>
          {reusable.map((item) => <p key={`${item.module}-${item.label}`}><Check size={13} />{item.label}</p>)}
        </section>
      ) : null}
      {conditional.length ? (
        <section className="conditional">
          <strong>需工程师确认</strong>
          {conditional.map((item) => (
            <p key={`${item.module}-${item.label}`} title={item.reason}><AlertTriangle size={13} />{item.label}</p>
          ))}
        </section>
      ) : null}
      {deferred.length ? (
        <details>
          <summary>当前不处理（{deferred.length}）</summary>
          {deferred.map((item) => <p key={`${item.module}-${item.label}`}>{item.label}：{item.reason}</p>)}
        </details>
      ) : null}
    </div>
  );
}

function ReviewIntroduction({ title, text, confirmed }: { title: string; text: string; confirmed: boolean }) {
  return (
    <div className="review-introduction">
      <div><strong>{title}</strong><StateBadge value={confirmed ? "confirmed" : "pending"} /></div>
      <p>{text}</p>
    </div>
  );
}

export function ModelActions({
  disabled,
  busy,
  mesh,
  build,
  submission,
  jobs,
  onJobsChange,
  onValidate,
  onBuild,
  onDatacheck,
  onSubmit,
  onPublish,
  onRepair,
  workflow,
  repairPreview,
}: {
  disabled: boolean;
  busy: string;
  mesh: MeshReport | null;
  build: BuildReport | null;
  submission: Submission | null;
  jobs: Record<JobType, boolean>;
  onJobsChange: (job: JobType, checked: boolean) => void;
  onValidate: () => void;
  onBuild: () => void;
  onDatacheck: () => void;
  onSubmit: () => void;
  onPublish: () => void;
  onRepair: () => void;
  workflow: WorkflowState | null;
  repairPreview: RepairPreview | null;
}) {
  if (disabled) return <EmptyPanel title="等待项目" text="正式参数集建立后可执行模型门禁和求解。" />;
  const parameterReady = workflow?.gates.parameter ?? false;
  const meshReady = workflow?.gates.mesh ?? mesh?.status === "pass";
  const modelReady = workflow?.gates.model_file ?? build?.status === "ready";
  const datacheckReady = workflow?.gates.datacheck ?? false;
  return (
    <div className="model-actions">
      <div className="gate-list">
        {(workflow?.steps.slice(1, 4) ?? []).map((step) => <GateRow key={step.id} label={step.label} value={step.status} />)}
      </div>
      {workflow?.blockers.length ? (
        <div className="gate-blockers">
          {workflow.blockers.map((blocker) => <p key={`${blocker.layer}-${blocker.code}-${blocker.parameter_key ?? ""}`}><AlertTriangle size={13} />{blocker.message}</p>)}
        </div>
      ) : null}
      <div className="model-button-grid">
        <button className="secondary-button" disabled={!parameterReady || Boolean(busy)} onClick={onValidate} type="button"><Grid3X3 size={15} />生成并检查网格</button>
        <button className="secondary-button" disabled={!meshReady || Boolean(busy)} onClick={onBuild} type="button"><FileArchive size={15} />生成模型文件</button>
        <button className="secondary-button" disabled={!modelReady || Boolean(busy)} onClick={onDatacheck} type="button"><Check size={15} />执行 Datacheck</button>
      </div>
      <button className="ghost-button full" disabled={!(workflow?.blockers.length || workflow?.advisories?.length) || Boolean(busy)} onClick={onRepair} type="button"><Settings2 size={15} />诊断与修复预览</button>
      {repairPreview ? (
        <div className="repair-preview">
          <strong>{repairPreview.status === "proposed" ? "可审查的参数补丁" : "需要人工处理"}</strong>
          <p>{repairPreview.explanation}</p>
          {repairPreview.canonical_parameter_patch.map((patch) => <small key={patch.parameter_key}>{patch.parameter_key} = {formatValue(patch.value)} {patch.unit ?? ""}</small>)}
        </div>
      ) : null}
      <button
        className="ghost-button full"
        disabled={!datacheckReady || Boolean(busy)}
        onClick={onPublish}
        type="button"
      >
        <FolderOpen size={15} />发布为已验证模型版本
      </button>
      <div className="job-selection">
        <span className="section-kicker">显式求解任务</span>
        <label><input checked={jobs.static_strength} onChange={(event) => onJobsChange("static_strength", event.target.checked)} type="checkbox" />线弹性静力</label>
        <label><input checked={jobs.modal} onChange={(event) => onJobsChange("modal", event.target.checked)} type="checkbox" />模态分析</label>
      </div>
      <button
        className="solve-button full"
        disabled={!datacheckReady || (!jobs.static_strength && !jobs.modal) || Boolean(busy)}
        onClick={onSubmit}
        type="button"
      >
        {busy === "solve" ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
        提交计算
      </button>
      {submission ? (
        <div className="task-list">
          <div className="section-title-row"><strong>求解任务</strong><StateBadge value={submission.status} /></div>
          {Object.entries(submission.tasks ?? {}).map(([name, task]) => (
            <div key={name}><span>{jobLabel(name)}</span><StateBadge value={task.status ?? "pending"} /></div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function GateRow({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><StateBadge value={value} /></div>;
}

function ResultsPanel({
  disabled,
  results,
  resultMesh,
  submission,
  onRefresh,
  onShowMesh,
}: {
  disabled: boolean;
  results: Record<string, unknown> | null;
  resultMesh: OdbResultMesh | null;
  submission: Submission | null;
  onRefresh: () => void;
  onShowMesh: () => void;
}) {
  if (disabled) return <EmptyPanel title="暂无结果" text="请先建立项目并显式提交 Abaqus。" />;
  const metrics = [
    ["最大位移", findMetric(results, ["max_displacement", "max_u_mag", "u_max"]), "mm"],
    ["最大 Mises 应力", findMetric(results, ["max_mises", "mises_max"]), "MPa"],
    ["反力平衡", findMetric(results, ["reaction_balance", "force_balance"]), ""],
    ["一阶频率", findMetric(results, ["first_frequency", "frequency_1"]), "Hz"],
  ];
  return (
    <div className="results-panel">
      <div className="section-title-row">
        <div><span className="section-kicker">求解状态</span><strong>{humanStatus(submission?.status ?? "pending")}</strong></div>
        <button className="ghost-button" onClick={onRefresh} type="button"><RefreshCw size={14} />刷新</button>
      </div>
      <div className="result-metrics">
        {metrics.map(([label, value, unit]) => (
          <div key={label}><span>{label}</span><strong>{value ?? "待生成"}</strong><small>{value === null ? "" : unit}</small></div>
        ))}
      </div>
      <div className="result-completeness">
        <GateRow label="ODB 完整性" value={resultMesh?.status ?? "pending"} />
        <GateRow label="静力结果" value={findStatus(results, "static") ?? "pending"} />
        <GateRow label="模态结果" value={findStatus(results, "modal") ?? "pending"} />
      </div>
      <button className="secondary-button full" disabled={!resultMesh} onClick={onShowMesh} type="button"><Eye size={15} />在中间视区查看云图</button>
      {results ? <ResultDetails results={results} /> : null}
    </div>
  );
}

function ArtifactsPanel({
  projectId,
  artifacts,
  artifactPreview,
  onPreview,
}: {
  projectId: string | null;
  artifacts: ArtifactList | null;
  artifactPreview: { item: ArtifactItem; content: string } | null;
  onPreview: (item: ArtifactItem) => void;
}) {
  if (!projectId) return <EmptyPanel title="暂无核心文件" text="参数、网格和模型构建后自动登记制品。" />;
  if (artifactPreview) {
    return (
      <div className="artifact-preview">
        <div className="section-title-row">
          <div><span className="section-kicker">文件预览</span><strong>{artifactPreview.item.filename}</strong></div>
          <a className="download-button" href={artifactUrl(projectId, artifactPreview.item.name, true)}><Download size={14} />下载</a>
        </div>
        <pre>{artifactPreview.content}</pre>
      </div>
    );
  }
  if (!artifacts?.items.length) return <EmptyPanel title="暂无核心文件" text="执行网格、构建或求解后刷新文件清单。" />;
  return (
    <div className="artifact-groups">
      <p className="artifact-list-note">仅显示正式参数、证据追踪、模型文件、检查报告和计算结果；运行所需的内部文件已隐藏。</p>
      {ARTIFACT_CATEGORIES.map((category) => {
        const items = artifacts.items.filter((item) => item.category === category.key);
        if (!items.length) return null;
        return (
          <section key={category.key}>
            <span className="section-kicker">{category.label} · {items.length}</span>
            {items.map((item) => (
              <div className="artifact-row" key={`${item.name}-${item.relative_path}`}>
                <FileText size={15} />
                <div>
                  <strong title={item.filename}>{item.filename}</strong>
                  <small>{formatBytes(item.size_bytes)} · {item.content_type}</small>
                </div>
                {item.previewable ? <button onClick={() => onPreview(item)} title="预览" type="button"><Eye size={14} /></button> : null}
                <a href={artifactUrl(projectId, item.name, true)} title="下载"><Download size={14} /></a>
              </div>
            ))}
          </section>
        );
      })}
    </div>
  );
}

function groupTowerParameters(rows: ParameterRecord[]) {
  const grouped = new Map<number, ParameterRecord[]>();
  rows.forEach((row) => {
    const index = Number(row.key.split(".")[2]);
    grouped.set(index, [...(grouped.get(index) ?? []), row]);
  });
  return Array.from(grouped, ([index, sectionRows]) => ({ index, rows: sectionRows }))
    .sort((first, second) => first.index - second.index);
}

export function directChanges(
  rows: ParameterRecord[],
  draftValues: Record<string, unknown>,
): ParameterChange[] {
  return rows.flatMap((row) => {
    const draft = draftValues[row.key];
    return equivalent(draft, row.value)
      ? []
      : [{ key: row.key, value: draft, unit: row.unit }];
  });
}

function equivalent(first: unknown, second: unknown) {
  if (typeof second === "number") return Number(first) === second;
  return first === second;
}

function coerceDraft(value: string, original: unknown) {
  if (typeof original === "number") {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : value;
  }
  return value;
}

function humanCompatibility(value: string) {
  if (value === "compatible") return "适用于当前能力";
  if (value === "partial") return "部分信息可参考";
  return "不适用于当前能力";
}

function shortFemLabel(key: string) {
  return safeParameterLabel(key, key);
}

function originLabel(origin: string) {
  const labels: Record<string, string> = {
    engineering_evidence: "工程资料",
    reference_model: "参考模型",
    capability_default: "能力配置",
    llm_suggestion: "LLM 建议",
    engineer_input: "工程师",
    deterministic_derived: "规则派生",
    model_library: "已验证模型",
    legacy_confirmed: "已有项目",
  };
  return labels[origin] ?? origin;
}

function candidateForReview(batch: InputBatch, key: string, value: unknown) {
  return batch.candidates.find((candidate) => (
    candidate.key === key && equivalent(candidate.normalized_value, value)
  )) ?? batch.candidates.find((candidate) => candidate.key === key);
}

function candidateSourceLabel(sourceKind?: string, extractionMethod?: string) {
  const labels: Record<string, string> = {
    file_observed: "工程观测",
    engineer_input: "工程师输入",
    reference_model_value: "参考模型",
    model_library_reference: "参考项目值",
    llm_mapped: "LLM 映射",
    llm_suggested: "LLM 建议",
    system_default: "能力默认",
    system_setting: "待工程师补充",
  };
  return labels[sourceKind ?? ""] ?? (extractionMethod === "deterministic_derived" ? "规则派生" : "待确认");
}

function isMissingValue(value: unknown) {
  return value === null || value === undefined || value === "";
}

function validationLevelLabel(level: ModelLibraryEntry["validation_level"]) {
  const labels: Record<ModelLibraryEntry["validation_level"], string> = {
    engineer_reviewed: "工程师复核",
    datacheck_passed: "模型检查通过",
    solver_verified: "求解验证通过",
  };
  return labels[level];
}

function statusFrom(value: Record<string, unknown> | null, key: string) {
  const nested = value?.[key];
  if (nested && typeof nested === "object" && "status" in nested) {
    return String((nested as { status: unknown }).status);
  }
  return null;
}

function valueStatus(value: Record<string, unknown> | null) {
  return value && typeof value.status === "string" ? value.status : null;
}

function findMetric(value: unknown, keys: string[]): string | null {
  if (!value || typeof value !== "object") return null;
  for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
    if (keys.includes(key) && (typeof nested === "number" || typeof nested === "string")) {
      return typeof nested === "number" ? formatNumber(nested) : nested;
    }
  }
  for (const nested of Object.values(value as Record<string, unknown>)) {
    const found = findMetric(nested, keys);
    if (found !== null) return found;
  }
  return null;
}

function findStatus(value: unknown, keyPart: string): string | null {
  if (!value || typeof value !== "object") return null;
  for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
    if (key.toLowerCase().includes(keyPart) && nested && typeof nested === "object") {
      const status = (nested as { status?: unknown }).status;
      if (typeof status === "string") return status;
    }
  }
  return null;
}

function safeParameterLabel(key: string, fallback: string) {
  const labels: Record<string, string> = {
    "geometry.door.enabled": "启用门洞",
    "geometry.door.width_mm": "门洞宽度",
    "geometry.door.height_mm": "门洞高度",
    "geometry.door.bottom_z_mm": "门洞底标高",
    "geometry.door.theta_center_deg": "门洞环向角度",
    "material.name": "材料名称",
    "material.elastic_modulus_mpa": "弹性模量",
    "material.poisson_ratio": "泊松比",
    "material.density": "材料密度",
    "material.yield_strength_mpa": "屈服强度",
    "mesh.element_type": "壳单元类型",
    "mesh.global_size_mm": "全局网格尺寸",
    "mesh.door_size_mm": "门洞局部网格尺寸",
    "mesh.patch_margin_u_mm": "门洞环向细化边界",
    "mesh.patch_margin_z_mm": "门洞高度向细化边界",
    "mesh.hole_layers": "门洞环向层数",
    "loads.top_mass_tonne": "顶部等效质量",
    "loads.top_horizontal_force_n": "顶部水平力",
    "loads.top_force_direction": "顶部力方向",
    "loads.wind_pressure_mpa": "风压",
    "analysis.static_enabled": "静力分析",
    "analysis.modal_enabled": "模态分析",
    "analysis.modal_modes": "模态阶数",
    "boundary.name": "底部边界条件",
  };
  if (labels[key]) return labels[key];
  const tower = key.match(/^geometry\.tower_sections\.(\d+)\.(.+)$/);
  if (tower) {
    const names: Record<string, string> = {
      z_bottom_mm: "底标高",
      z_top_mm: "顶标高",
      diameter_bottom_mm: "底直径",
      diameter_top_mm: "顶直径",
      thickness_mm: "厚度",
    };
    return `第 ${Number(tower[1]) + 1} 段${names[tower[2]] ?? tower[2]}`;
  }
  return fallback || key;
}

function tone(value?: string): "pass" | "warning" | "danger" | "idle" | "working" {
  const normalized = (value ?? "").toLowerCase();
  if (["pass", "ready", "confirmed", "completed", "ok", "compatible"].includes(normalized)) return "pass";
  if (["failed", "blocked", "error"].includes(normalized)) return "danger";
  if (normalized.includes("warning") || normalized.includes("needs_") || normalized === "partial") return "warning";
  if (["queued", "processing", "running", "submitted", "extracting", "mapping"].includes(normalized)) return "working";
  return "idle";
}

function humanStatus(value: string) {
  const labels: Record<string, string> = {
    pending: "待执行",
    pass: "通过",
    ready: "就绪",
    confirmed: "已确认",
    completed: "已完成",
    completed_with_llm_warning: "完成（有警告）",
    processing: "解析中",
    queued: "排队中",
    running: "运行中",
    submitted: "已提交",
    blocked: "已阻断",
    failed: "失败",
    warning: "警告",
    partial: "部分完成",
    compatible: "适用",
    incompatible: "不适用",
    needs_input: "需要补充",
    needs_confirmation: "需要确认",
    extracting: "提取中",
    mapping: "映射中",
  };
  return labels[value] ?? value;
}

function jobLabel(value: string) {
  return value === "static_strength" ? "线弹性静力" : value === "modal" ? "模态分析" : value;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return formatNumber(value);
  if (Array.isArray(value)) return `${value.length} 项`;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 6 }).format(value);
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
