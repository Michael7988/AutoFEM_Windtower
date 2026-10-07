import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePolling } from "./usePolling";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function Harness({
  enabled,
  poll,
  onValue,
}: {
  enabled: boolean;
  poll: () => Promise<string>;
  onValue: (value: string) => void;
}) {
  usePolling({
    enabled,
    intervalMs: 100,
    poll,
    onValue,
    onError: () => undefined,
    pollKey: "project:submission",
  });
  return null;
}

describe("usePolling", () => {
  it("stops polling when the workflow becomes terminal", async () => {
    vi.useFakeTimers();
    const poll = vi.fn().mockResolvedValue("running");
    const onValue = vi.fn();
    const view = render(<Harness enabled poll={poll} onValue={onValue} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(poll).toHaveBeenCalledTimes(1);
    expect(onValue).toHaveBeenCalledWith("running");

    view.rerender(<Harness enabled={false} poll={poll} onValue={onValue} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(poll).toHaveBeenCalledTimes(1);
  });

  it("does not publish a late response after unmount", async () => {
    vi.useFakeTimers();
    let resolve!: (value: string) => void;
    const poll = vi.fn(() => new Promise<string>((done) => { resolve = done; }));
    const onValue = vi.fn();
    const view = render(<Harness enabled poll={poll} onValue={onValue} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    view.unmount();
    await act(async () => resolve("completed"));

    expect(onValue).not.toHaveBeenCalled();
  });
});
