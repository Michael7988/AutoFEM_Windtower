import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ParameterReviewTable, ParameterTracePanel, ValidationPanel } from "./TrustPanels";

afterEach(cleanup);

describe("可信建模面板", () => {
  it("区分映射置信度、人工确认和正式使用权限", () => {
    render(<ParameterTracePanel loading={false} onBack={() => undefined} trace={{
      project_id: "wt-1", revision: 2, key: "mesh.global_size_mm", label: "全局网格", status: "confirmed", confirmation_status: "confirmed",
      raw_value: 500, raw_unit: "mm", normalized_value: 500, normalized_unit: "mm", mapping_confidence: .96,
      assurance: { source_category: "fem_file", locator_status: "exact", human_confirmation: "confirmed", formal_use_allowed: true },
      sources: [{ filename: "FEM.xls", locator: "Sheet FEM · D12" }], consumed_by: ["S4RMesh"],
    }} />);
    expect(screen.getByText("Mapping confidence")).toBeTruthy();
    expect(screen.getByText("0.96")).toBeTruthy();
    expect(screen.getByText("allowed")).toBeTruthy();
  });

  it("参数审查会优先显示待确认项", () => {
    const onSelect = vi.fn();
    render(<ParameterReviewTable items={[
      { key: "a", label: "已确认", value: 1, source: "工程表", status: "confirmed" },
      { key: "b", label: "待确认默认", value: 2, source: "能力默认", status: "review" },
    ]} onSelect={onSelect} />);
    const rows = screen.getAllByRole("row");
    expect(rows[1].textContent).toContain("待确认默认");
    fireEvent.click(rows[1]);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("not_run、warning和fail保持独立", () => {
    render(<ValidationPanel summary={{
      project_id: "wt-1", parameter_revision: 1, status: "warning",
      totals: { passed: 1, warnings: 1, failed: 0, not_run: 1 },
      groups: [{ id: "model", label: "Model and mesh", status: "warning", counts: { passed: 1, warnings: 1, failed: 0, not_run: 1 }, checks: [
        { id: "aspect", label: "Aspect ratio", status: "warning", value: 8.2 },
        { id: "odb", label: "ODB", status: "not_run" },
      ] }],
    }} />);
    expect(screen.getByText(/1 warning/)).toBeTruthy();
    expect(screen.getByText(/1 not run/)).toBeTruthy();
    expect(screen.getByText("not run")).toBeTruthy();
  });
});
