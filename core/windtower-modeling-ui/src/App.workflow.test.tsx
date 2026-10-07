import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { InputBatch } from "./types";

const api = vi.hoisted(() => ({
  createInputBatch: vi.fn(),
  deleteReferenceModel: vi.fn(),
  getKnowledgeBasesStatus: vi.fn(),
  getModelLibrary: vi.fn(),
  getWorkflowState: vi.fn(),
  putReferenceModel: vi.fn(),
}));

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  createInputBatch: api.createInputBatch,
  deleteReferenceModel: api.deleteReferenceModel,
  getKnowledgeBasesStatus: api.getKnowledgeBasesStatus,
  getModelLibrary: api.getModelLibrary,
  getWorkflowState: api.getWorkflowState,
  putReferenceModel: api.putReferenceModel,
}));

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  api.getModelLibrary.mockResolvedValue({ schema_version: "v1", items: [] });
  api.getKnowledgeBasesStatus.mockResolvedValue({
    engineering: { domain: "engineering", status: "ready" },
    fem: { domain: "fem", status: "ready" },
  });
  api.getWorkflowState.mockResolvedValue({
    schema_version: "project_workflow_state_v1",
    project_id: "wt-existing",
    parameter_revision: 1,
    current_step: "parameters",
    steps: [
      { id: "input", label: "输入资料", status: "completed" },
      { id: "parameters", label: "确认参数", status: "ready" },
      { id: "mesh_model", label: "网格与模型", status: "not_started" },
      { id: "model_check", label: "模型检查", status: "not_started" },
      { id: "solve_results", label: "求解结果", status: "not_started" },
    ],
    blockers: [],
    next_action: { id: "confirm_parameters", label: "确认参数" },
    environment: {},
    gates: { parameter: false, mesh: false, model_file: false, datacheck: false, results: false },
  });
});

describe("参考模型工作流", () => {
  it("can leave the current project and start a fresh validation", async () => {
    api.createInputBatch.mockResolvedValue(batch({ project_id: "wt-existing" }));
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "解析工程资料与参考模型" }));
    const newValidation = await screen.findByRole("button", { name: "新建验证" });
    fireEvent.click(newValidation);

    expect(screen.getByLabelText("建模目标")).toBeTruthy();
    expect(screen.getByRole("button", { name: "解析工程资料与参考模型" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "恢复最近项目" })).toBeTruthy();
  });

  it("can replace an existing batch reference without creating another batch", async () => {
    const initial = batch();
    const replaced = batch({
      reference_model: {
        summary: {
          filename: "replacement.inp",
          status: "parsed",
          compatibility: "compatible",
          element_count: 42,
        },
      },
    });
    api.createInputBatch.mockResolvedValue(initial);
    api.putReferenceModel.mockResolvedValue(replaced);
    const view = render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "解析工程资料与参考模型" }));
    await screen.findByText("未提供参考模型，将使用当前风塔能力配置候选。");
    const input = view.container.querySelector<HTMLInputElement>('input[accept=".inp"]');
    expect(input).toBeTruthy();
    const reference = new File(["*Heading"], "replacement.inp", { type: "text/plain" });
    fireEvent.change(input!, { target: { files: [reference] } });
    fireEvent.click(screen.getByRole("button", { name: "替换为所选参考 INP" }));

    await waitFor(() => expect(api.putReferenceModel).toHaveBeenCalledWith("batch-ui", reference));
    expect(api.createInputBatch).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("replacement.inp")).toBeTruthy();
  });

  it("removes a reference and returns to capability candidates", async () => {
    const withReference = batch({
      reference_model: {
        summary: {
          filename: "old.inp",
          status: "parsed",
          compatibility: "compatible",
          element_count: 10,
        },
      },
    });
    api.createInputBatch.mockResolvedValue(withReference);
    api.deleteReferenceModel.mockResolvedValue(batch());
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "解析工程资料与参考模型" }));
    fireEvent.click(await screen.findByRole("button", { name: "移除参考模型" }));

    await waitFor(() => expect(api.deleteReferenceModel).toHaveBeenCalledWith("batch-ui"));
    expect(await screen.findByText("未提供参考模型，将使用当前风塔能力配置候选。")).toBeTruthy();
  });

  it("blocks project creation for an incompatible reference", async () => {
    api.createInputBatch.mockResolvedValue(batch({
      capability_match: { status: "incompatible" },
      review_revision: 1,
      review_parameters: [{ key: "geometry.door.width_mm", value: 1200, unit: "mm" }],
      readiness: { preview_allowed: true, compute_allowed: false },
    }));
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "解析工程资料与参考模型" }));
    const confirm = await screen.findByRole("button", { name: "确认全部参数并建立项目" });

    expect((confirm as HTMLButtonElement).disabled).toBe(true);
  });
});

function batch(overrides: Partial<InputBatch> = {}): InputBatch {
  return {
    batch_id: "batch-ui",
    parse_mode: "deterministic",
    status: "ready",
    progress: 100,
    files: [],
    candidates: [],
    conflicts: [],
    warnings: [],
    fem_review_revision: 1,
    fem_review: { status: "pending" },
    fem_specification: {
      capability_id: "WT-SHELL-DOOR-STATIC-MODAL-V1",
      compatibility: "compatible",
      status: "pending",
      parameters: {},
    },
    capability_match: { status: "compatible" },
    readiness: { preview_allowed: true, compute_allowed: false },
    ...overrides,
  };
}
