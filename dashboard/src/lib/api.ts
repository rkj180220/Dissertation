import { fetchEventSource } from "@microsoft/fetch-event-source";
import type {
  OrchestrationRequest,
  OrchestrationResponse,
  HealthResponse,
  ReadyResponse,
  ClarifyRequest,
  ClarifyResponse,
  SSEEvent,
} from "@/types/api";

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";

// ── JSON endpoints ───────────────────────────────

export async function orchestrate(
  request: OrchestrationRequest,
): Promise<OrchestrationResponse> {
  const res = await fetch(`${API_BASE_URL}/orchestrate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Orchestration failed (${res.status}): ${text}`);
  }
  return res.json();
}

export async function clarifyRequirements(
  request: ClarifyRequest,
): Promise<ClarifyResponse> {
  const res = await fetch(`${API_BASE_URL}/orchestrate/clarify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Clarification failed (${res.status}): ${text}`);
  }
  return res.json();
}

export async function checkHealth(): Promise<HealthResponse> {
  const res = await fetch(`${API_BASE_URL}/health`);
  if (!res.ok) throw new Error(`Health check failed (${res.status})`);
  return res.json();
}

export async function checkReady(): Promise<ReadyResponse> {
  const res = await fetch(`${API_BASE_URL}/ready`);
  if (!res.ok) throw new Error(`Readiness check failed (${res.status})`);
  return res.json();
}

export async function submitFeedback(
  requestId: string,
  rating: number,
  comment?: string,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: requestId, rating, comment }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Feedback submission failed (${res.status}): ${text}`);
  }
}

// ── SSE streaming endpoint ───────────────────────

export interface StreamCallbacks {
  onEvent: (event: SSEEvent) => void;
  onError: (error: string) => void;
  onOpen?: () => void;
}

class FatalError extends Error {}

export function streamOrchestrate(
  request: OrchestrationRequest,
  callbacks: StreamCallbacks,
): AbortController {
  const ctrl = new AbortController();

  fetchEventSource(`${API_BASE_URL}/orchestrate/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal: ctrl.signal,

    onopen: async (response) => {
      if (response.ok) {
        callbacks.onOpen?.();
        return;
      }
      throw new FatalError(
        `Stream failed to open (${response.status})`,
      );
    },

    onmessage: (msg) => {
      if (!msg.data) return;
      try {
        const parsed = JSON.parse(msg.data);
        const eventType = msg.event || parsed.event;
        const event: SSEEvent = { event: eventType, data: parsed.data ?? parsed };
        callbacks.onEvent(event);
      } catch {
        console.warn("Failed to parse SSE message:", msg.data);
      }
    },

    onerror: (err) => {
      if (err instanceof FatalError) {
        callbacks.onError(err.message);
        throw err; // stop retrying
      }
      callbacks.onError(
        err instanceof Error ? err.message : "Connection lost",
      );
      throw err; // stop retrying on all errors
    },

    openWhenHidden: true,
  });

  return ctrl;
}
