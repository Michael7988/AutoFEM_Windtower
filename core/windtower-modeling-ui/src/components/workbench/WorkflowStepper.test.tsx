import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ModelActions } from "../../App";
import type { WorkflowState } from "../../types";
import { WorkflowStepper } from "./WorkflowStepper";

afterEach(cleanup);

describe("五步工作流", () => {
  it("显示统一步骤状态和参数阻断", () => {
    const state = workflow({ parameter: false, mesh: false, model_file: false, datacheck: false, results: false }, "parameters");
    render(<WorkflowStepper loading={false} state={state} />);
    expect(screen.getByText("确认参数")).toBeTruthy();
    expect(screen.getByText("已阻断")).toBeTruthy();
    expect(screen.getAllByText("待执行").length).toBeGreaterThan(0);
  });

  it("按参数、网格、模型和 Datacheck 依赖禁用操作", () => {
    const state = workflow({ parameter: true, mesh: false, model_file: false, datacheck: false, results: false }, "mesh_model");
    render(<ModelActions {...actions()} workflow={state} />);
    expect(button("生成并检查网格").disabled).toBe(false);
    expect(button("生成模型文件").disabled).toBe(true);
    expect(button("执行 Datacheck").disabled).toBe(true);
    expect(button("提交计算").disabled).toBe(true);
  });

  it("Datacheck 通过后才允许提交求解", () => {
    const state = workflow({ parameter: true, mesh: true, model_file: true, datacheck: true, results: false }, "solve_results");
    render(<ModelActions {...actions()} workflow={state} />);
    expect(button("生成模型文件").disabled).toBe(false);
    expect(button("执行 Datacheck").disabled).toBe(false);
    expect(button("提交计算").disabled).toBe(false);
  });
});

function button(name: string) {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

function actions() {
  return {
    disabled: false,
    busy: "",
    mesh: null,
    build: null,
    submission: null,
    jobs: { static_strength: true, modal: true },
    onJobsChange: vi.fn(),
    onValidate: vi.fn(),
    onBuild: vi.fn(),
    onDatacheck: vi.fn(),
    onSubmit: vi.fn(),
    onPublish: vi.fn(),
    onRepair: vi.fn(),
    repairPreview: null,
  };
}

function workflow(gates: WorkflowState["gates"], currentStep: WorkflowState["current_step"]): WorkflowState {
  const ids: WorkflowState["current_step"][] = ["input", "parameters", "mesh_model", "model_check", "solve_results"];
  const labels = ["输入资料", "确认参数", "网格与模型", "模型检查", "求解结果"];
  return {
    schema_version: "project_workflow_state_v1",
    project_id: "wt-test",
    parameter_revision: 1,
    current_step: currentStep,
    steps: ids.map((id, index) => ({
      id,
      label: labels[index],
      status: id === currentStep ? (id === "parameters" ? "blocked" : "ready") : index < ids.indexOf(currentStep) ? "completed" : "not_started",
    })),
    blockers: currentStep === "parameters" ? [{ layer: "parameter", code: "unconfirmed", parameter_key: "loads.center_of_mass_offset_mm", message: "顶部质心偏心距未确认", resolution_action: "confirm_parameter" }] : [],
    advisories: [],
    environment: {},
    gates,
  };
}
