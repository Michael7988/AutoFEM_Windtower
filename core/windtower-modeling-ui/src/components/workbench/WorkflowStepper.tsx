import { Check, LoaderCircle } from "lucide-react";

import type { WorkflowState, WorkflowStepStatus } from "../../types";

const FALLBACK_STEPS = ["输入资料", "确认参数", "网格与模型", "模型检查", "求解结果"];

export function WorkflowStepper({ state, loading }: { state: WorkflowState | null; loading: boolean }) {
  const steps = state?.steps ?? FALLBACK_STEPS.map((label, index) => ({
    id: ["input", "parameters", "mesh_model", "model_check", "solve_results"][index] as WorkflowState["current_step"],
    label,
    status: (index === 0 ? "ready" : "not_started") as WorkflowStepStatus,
  }));
  return (
    <nav aria-label="建模工作流" className="workflow-stepper">
      {steps.map((step, index) => (
        <div aria-current={state?.current_step === step.id ? "step" : undefined} className={`workflow-step ${step.status}`} key={step.id}>
          <span>{step.status === "completed" ? <Check size={14} /> : loading && state?.current_step === step.id ? <LoaderCircle className="spin" size={14} /> : index + 1}</span>
          <div><strong>{step.label}</strong><small>{stepStatusLabel(step.status)}</small></div>
        </div>
      ))}
    </nav>
  );
}

function stepStatusLabel(status: WorkflowStepStatus) {
  return {
    not_started: "待执行",
    ready: "可执行",
    running: "执行中",
    blocked: "已阻断",
    completed: "已完成",
  }[status];
}
