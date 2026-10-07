// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { LanguageSwitcher, LocalizationProvider } from "./i18n";

function LocalizedFixture() {
  return (
    <LocalizationProvider>
      <LanguageSwitcher />
      <section aria-label="建模工作流">
        <h1>输入与解析</h1>
        <p>工程资料和参考模型均可选；仅填写明确建模目标也可开始，由工程师确认全部建议值。</p>
        <input placeholder="例如：门洞宽度增加 5%，全局网格尺寸调整为 350 mm" />
      </section>
    </LocalizationProvider>
  );
}

describe("中英文界面切换", () => {
  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-language");
    document.documentElement.lang = "";
  });

  it("切换英文并保存语言偏好", async () => {
    render(<LocalizedFixture />);
    fireEvent.click(screen.getByRole("button", { name: "EN" }));

    await waitFor(() => {
      expect(screen.getByText("Input & Parsing")).toBeTruthy();
      expect(screen.getByLabelText("Modeling Workflow")).toBeTruthy();
      expect(screen.getByPlaceholderText(/increase the door width by 5%/i)).toBeTruthy();
    });
    expect(document.documentElement.lang).toBe("en-US");
    expect(window.localStorage.getItem("windtower.language")).toBe("en-US");
  });

  it("可以无刷新恢复中文", async () => {
    window.localStorage.setItem("windtower.language", "en-US");
    render(<LocalizedFixture />);
    await waitFor(() => expect(screen.getByText("Input & Parsing")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "中文" }));
    await waitFor(() => expect(screen.getByText("输入与解析")).toBeTruthy());
    expect(document.documentElement.lang).toBe("zh-CN");
  });
});
