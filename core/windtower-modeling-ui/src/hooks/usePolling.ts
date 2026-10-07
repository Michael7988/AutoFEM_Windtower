import { useEffect, useRef } from "react";

interface PollingOptions<T> {
  enabled: boolean;
  intervalMs: number;
  poll: () => Promise<T>;
  onValue: (value: T) => void;
  onError: (reason: unknown) => void;
  pollKey?: string | null;
}

export function usePolling<T>({
  enabled,
  intervalMs,
  poll,
  onValue,
  onError,
  pollKey = null,
}: PollingOptions<T>) {
  const pollRef = useRef(poll);
  const valueRef = useRef(onValue);
  const errorRef = useRef(onError);
  pollRef.current = poll;
  valueRef.current = onValue;
  errorRef.current = onError;

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    let inFlight = false;
    const tick = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const value = await pollRef.current();
        if (active) valueRef.current(value);
      } catch (reason) {
        if (active) errorRef.current(reason);
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => void tick(), intervalMs);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [enabled, intervalMs, pollKey]);
}
