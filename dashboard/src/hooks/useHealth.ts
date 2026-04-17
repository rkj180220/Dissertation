import { useCallback, useEffect, useRef, useState } from "react";
import { checkReady } from "@/lib/api";
import type { ReadyResponse } from "@/types/api";

const POLL_INTERVAL_MS = 30_000;

export function useHealth() {
  const [data, setData] = useState<ReadyResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const poll = useCallback(async () => {
    try {
      const res = await checkReady();
      setData(res);
    } catch {
      setData(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    poll();
    timerRef.current = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [poll]);

  const status: "healthy" | "degraded" | "offline" = data
    ? data.status === "ready"
      ? "healthy"
      : "degraded"
    : "offline";

  return { status, data, isLoading, refresh: poll };
}
