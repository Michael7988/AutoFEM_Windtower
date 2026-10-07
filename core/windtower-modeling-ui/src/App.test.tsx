import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { App, directChanges, hasPreparedEngineeringReview, PreparationConfirmBar, TechnicalDetails } from "./App";
import type { InputBatch, ParameterRecord } from "./types";

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe("单页三栏工程建模工作台", () => {
  it("在一个页面展示输入、网格、参数与输出区域", () => {
    render(<App />);

    expect(screen.getByText("输入与解析")).toBeTruthy();
    expect(screen.getByText("参考模型")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "解析工程资料与参考模型" })).toHaveLength(1);
    expect(screen.getByText("Excel、CSV、JSON、YAML、TXT、DOCX、文字层 PDF")).toBeTruthy();
    expect(screen.getByText("可上传参考 INP、选择已验证模型，或直接采用当前能力配置。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "工程参数" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "FEM 参数" })).toBeTruthy();
    expect(screen.getByText("确认参数后生成网格预览")).toBeTruthy();
    expect(screen.getByText("参数审查")).toBeTruthy();
    expect(screen.getByRole("button", { name: "模型" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "结果" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "文件" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "解析参考模型" })).toBeNull();
    expect(screen.queryByRole("button", { name: "合并双域参数并建立项目" })).toBeNull();
  });

  it("顶部只保留居中品牌，工程状态固定在左栏", () => {
    render(<App />);

    const brand = screen.getByTestId("centered-brand");
    const topbar = brand.closest("header");
    const statusPanel = screen.getByTestId("engineering-status-panel");

    expect(within(brand).getByText("AutoFEM WindTower")).toBeTruthy();
    expect(within(brand).getByText("LLM 辅助参数化有限元建模工作台")).toBeTruthy();
    expect(topbar && within(topbar).queryByText("项目")).toBeNull();
    expect(within(statusPanel).getByText("项目")).toBeTruthy();
    expect(within(statusPanel).getByText("Revision")).toBeTruthy();
    expect(within(statusPanel).getByText("当前步骤")).toBeTruthy();
    const workflow = screen.getByRole("navigation", { name: "建模工作流" });
    for (const label of ["输入资料", "确认参数", "网格与模型", "模型检查", "求解结果"]) {
      expect(within(workflow).getByText(label)).toBeTruthy();
    }
  });

  it("即使存在历史项目也默认进入新验证，不自动跳回旧工况", () => {
    window.localStorage.setItem("windtower.lastProjectId", "wt-old-project");

    render(<App />);

    expect(screen.getByLabelText("建模目标")).toBeTruthy();
    expect(screen.getByRole("button", { name: "解析工程资料与参考模型" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "恢复最近项目" })).toBeTruthy();
  });

  it("保留整体、门洞局部和四种工程视角", () => {
    render(<App />);

    expect(screen.getByRole("button", { name: "三维" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "正视" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "侧视" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "俯视" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "整体网格" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "门洞局部" })).toBeTruthy();
  });
});

describe("统一参数确认", () => {
  it("将唯一建项按钮固定在参数列表之前", () => {
    render(
      <PreparationConfirmBar
        busy=""
        engineeringCount={12}
        femCount={9}
        missingCount={0}
        onConfirm={() => undefined}
        ready
        referenceCount={5}
      />,
    );

    const bar = screen.getByTestId("preparation-confirm-bar");
    expect(within(bar).getByRole("button", { name: "确认全部参数并建立项目" })).toBeTruthy();
    expect(within(bar).getByText("参考模型")).toBeTruthy();
  });

  it("允许只有 FEM 候选且工程参数行为零的 L2 批次建项", () => {
    const l2Batch = {
      review_revision: 1,
      capability_id: "WT-FLANGE-BOLT-CONTACT-PRELOAD-V1",
    } as InputBatch;
    const l1Batch = {
      review_revision: 1,
      capability_id: "WT-SHELL-DOOR-STATIC-MODAL-V1",
    } as InputBatch;

    expect(hasPreparedEngineeringReview(l2Batch, 0)).toBe(true);
    expect(hasPreparedEngineeringReview(l1Batch, 0)).toBe(false);
  });
});

describe("技术信息", () => {
  it("默认折叠内部编号、路径和原始数据", () => {
    render(
      <TechnicalDetails>
        <pre>{"E:\\internal\\result.odb"}</pre>
      </TechnicalDetails>,
    );

    const details = screen.getByText("技术详情").closest("details");
    expect(details?.open).toBe(false);
  });
});

describe("直接参数修改", () => {
  it("只提交相对当前 revision 发生变化的参数", () => {
    const rows: ParameterRecord[] = [
      {
        key: "geometry.door.width_mm",
        label: "门洞宽度",
        group: "geometry",
        value: 1200,
        unit: "mm",
        status: "confirmed",
        domain: "engineering",
        origin: "engineering_evidence",
        confirmation_status: "confirmed",
        requires_confirmation: false,
      },
      {
        key: "mesh.global_size_mm",
        label: "全局网格尺寸",
        group: "mesh",
        value: 400,
        unit: "mm",
        status: "confirmed",
        domain: "fem",
        origin: "capability_default",
        confirmation_status: "confirmed",
        requires_confirmation: false,
      },
    ];

    expect(directChanges(rows, {
      "geometry.door.width_mm": 1260,
      "mesh.global_size_mm": "400",
    })).toEqual([
      { key: "geometry.door.width_mm", value: 1260, unit: "mm" },
    ]);
  });
});
