import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { clarifyRequirements, streamOrchestrate } from "@/lib/api";
import type {
  ChatMessage,
  AgentStatus,
  OrchestrationResponse,
  CostComparison,
  ComplianceReport,
  SSEEvent,
} from "@/types/api";

// ── Agent pipeline order ─────────────────────────

export const AGENT_ORDER = [
  "clarifier",
  "profiler",
  "sizer",
  "finops",
  "rfp_writer",
] as const;

export type AgentName = (typeof AGENT_ORDER)[number];

export const AGENT_LABELS: Record<AgentName, string> = {
  clarifier: "Clarifier",
  profiler: "Profiler",
  sizer: "Sizer",
  finops: "FinOps",
  rfp_writer: "RFP Writer",
};

// ── Context shape ────────────────────────────────

export type Phase = "idle" | "clarifying" | "pipeline" | "complete" | "error";

interface PipelineState {
  phase: Phase;
  messages: ChatMessage[];
  agentProgress: Record<string, AgentStatus>;
  result: OrchestrationResponse | null;
  costComparison: CostComparison | null;
  complianceReport: ComplianceReport | null;
  isStreaming: boolean;
  error: string | null;
  durationMs: number | null;
}

interface PipelineActions {
  sendMessage: (text: string, projectName?: string) => Promise<void>;
  reset: () => void;
}

type PipelineContextValue = PipelineState & PipelineActions;

const PipelineContext = createContext<PipelineContextValue | null>(null);

// ── Provider ─────────────────────────────────────

const initialProgress: Record<string, AgentStatus> = Object.fromEntries(
  AGENT_ORDER.map((a) => [a, "pending" as AgentStatus]),
);

export function PipelineProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [agentProgress, setAgentProgress] =
    useState<Record<string, AgentStatus>>(initialProgress);
  const [result, setResult] = useState<OrchestrationResponse | null>(null);
  const [costComparison, setCostComparison] =
    useState<CostComparison | null>(null);
  const [complianceReport, setComplianceReport] =
    useState<ComplianceReport | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [durationMs, setDurationMs] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef<string | null>(null);
  const projectNameRef = useRef<string>("untitled");
  const isSendingRef = useRef(false);

  // ── Internal: start SSE pipeline stream ────────
  const startStream = useCallback(
    (enrichedInput: string) => {
      setAgentProgress({ ...initialProgress });
      setIsStreaming(true);
      setPhase("pipeline");

      abortRef.current?.abort();

      const ctrl = streamOrchestrate(
        {
          user_input: enrichedInput,
          project_name: projectNameRef.current,
        },
        {
          onEvent: (event: SSEEvent) => {
            switch (event.event) {
              case "agent_update": {
                const { agent } = event.data;
                setAgentProgress((prev) => ({
                  ...prev,
                  [agent]: "running",
                }));
                break;
              }
              case "message": {
                const { agent, content } = event.data;
                setMessages((prev) => [
                  ...prev,
                  {
                    role: "assistant",
                    content,
                    timestamp: new Date().toISOString(),
                    agent_name: agent,
                    metadata: {},
                  },
                ]);
                setAgentProgress((prev) => ({
                  ...prev,
                  [agent]: "completed",
                }));
                break;
              }
              case "pipeline_complete": {
                setDurationMs(event.data.duration_ms);
                if (
                  event.data.rfp_document ||
                  event.data.executive_summary ||
                  event.data.cost_comparison
                ) {
                  setResult({
                    request_id: event.data.request_id,
                    status: "completed",
                    rfp_document: event.data.rfp_document ?? "",
                    executive_summary: event.data.executive_summary ?? "",
                    recommended_provider:
                      event.data.recommended_provider ?? null,
                    cost_comparison: event.data.cost_comparison ?? {},
                    compliance_report: event.data.compliance_report ?? {},
                    architecture_alternatives:
                      event.data.architecture_alternatives ?? [],
                    processor_architecture_insights:
                      event.data.processor_architecture_insights ?? [],
                    duration_ms: event.data.duration_ms,
                  } as OrchestrationResponse);
                  if (event.data.cost_comparison) {
                    setCostComparison(event.data.cost_comparison);
                  }
                  if (event.data.compliance_report) {
                    setComplianceReport(event.data.compliance_report);
                  }
                }
                setIsStreaming(false);
                setPhase("complete");
                setAgentProgress(
                  Object.fromEntries(
                    AGENT_ORDER.map((a) => [a, "completed" as AgentStatus]),
                  ),
                );
                break;
              }
              case "error": {
                setError(event.data.error);
                setDurationMs(event.data.duration_ms);
                setIsStreaming(false);
                setPhase("error");
                break;
              }
            }
          },
          onError: (errMsg: string) => {
            setError(errMsg);
            setIsStreaming(false);
            setPhase("error");
          },
          onOpen: () => {
            setError(null);
          },
        },
      );

      abortRef.current = ctrl;
    },
    [],
  );

  // ── Public: send a chat message (clarification or initial) ──
  const sendMessage = useCallback(
    async (text: string, projectName?: string) => {
      if (isSendingRef.current) return;
      isSendingRef.current = true;

      if (projectName) {
        projectNameRef.current = projectName;
      }

      // Add user message to chat
      setMessages((prev) => [
        ...prev,
        {
          role: "user",
          content: text,
          timestamp: new Date().toISOString(),
          agent_name: null,
          metadata: {},
        },
      ]);
      setError(null);

      try {
        const response = await clarifyRequirements({
          user_input: text,
          project_name: projectNameRef.current,
          request_id: requestIdRef.current ?? undefined,
        });

        requestIdRef.current = response.request_id;

        // Add clarifier response as assistant message
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: response.message,
            timestamp: new Date().toISOString(),
            agent_name: "clarifier",
            metadata: {},
          },
        ]);

        if (response.status === "clarifying") {
          setPhase("clarifying");
        } else if (response.status === "ready" && response.enriched_input) {
          // Auto-start the pipeline
          startStream(response.enriched_input);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Unknown error");
        setPhase("error");
      } finally {
        isSendingRef.current = false;
      }
    },
    [startStream],
  );

  const reset = useCallback(() => {
    abortRef.current?.abort();
    requestIdRef.current = null;
    projectNameRef.current = "untitled";
    isSendingRef.current = false;
    setPhase("idle");
    setMessages([]);
    setAgentProgress({ ...initialProgress });
    setResult(null);
    setCostComparison(null);
    setComplianceReport(null);
    setIsStreaming(false);
    setError(null);
    setDurationMs(null);
  }, []);

  return (
    <PipelineContext.Provider
      value={{
        phase,
        messages,
        agentProgress,
        result,
        costComparison,
        complianceReport,
        isStreaming,
        error,
        durationMs,
        sendMessage,
        reset,
      }}
    >
      {children}
    </PipelineContext.Provider>
  );
}

// ── Hook ─────────────────────────────────────────

export function usePipeline(): PipelineContextValue {
  const ctx = useContext(PipelineContext);
  if (!ctx) throw new Error("usePipeline must be used within PipelineProvider");
  return ctx;
}
