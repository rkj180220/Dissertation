import { cn } from "@/lib/utils";
import {
  AGENT_ORDER,
  AGENT_LABELS,
  type AgentName,
} from "@/context/PipelineContext";
import type { AgentStatus } from "@/types/api";

interface Props {
  progress: Record<string, AgentStatus>;
}

function StepIcon({ status }: { status: AgentStatus }) {
  switch (status) {
    case "completed":
      return (
        <svg className="h-5 w-5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
        </svg>
      );
    case "running":
      return (
        <span className="flex h-5 w-5 items-center justify-center">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        </span>
      );
    case "failed":
      return (
        <svg className="h-5 w-5 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      );
    default:
      return (
        <span className="flex h-5 w-5 items-center justify-center">
          <span className="h-2.5 w-2.5 rounded-full bg-muted-foreground/30" />
        </span>
      );
  }
}

export function AgentProgress({ progress }: Props) {
  return (
    <div className="flex items-center justify-center gap-1 rounded-lg border bg-muted/50 px-4 py-3">
      {AGENT_ORDER.map((agent, idx) => {
        const status = progress[agent] ?? "pending";
        return (
          <div key={agent} className="flex items-center">
            {/* Step */}
            <div className="flex flex-col items-center gap-1">
              <StepIcon status={status} />
              <span
                className={cn(
                  "text-[10px] font-medium whitespace-nowrap",
                  status === "running"
                    ? "text-primary"
                    : status === "completed"
                      ? "text-green-600"
                      : status === "failed"
                        ? "text-red-500"
                        : "text-muted-foreground",
                )}
              >
                {AGENT_LABELS[agent as AgentName]}
              </span>
            </div>

            {/* Connector line */}
            {idx < AGENT_ORDER.length - 1 && (
              <div
                className={cn(
                  "mx-2 h-px w-8",
                  status === "completed"
                    ? "bg-green-500"
                    : "bg-muted-foreground/20",
                )}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
