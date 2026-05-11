import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ChatMessage } from "./ChatMessage";
import { ChatInput } from "./ChatInput";
import { AgentProgress } from "./AgentProgress";
import { usePipeline } from "@/context/PipelineContext";

export function ChatContainer() {
  const {
    phase,
    messages,
    agentProgress,
    error,
    durationMs,
    sendMessage,
  } = usePipeline();

  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const showProgress = phase === "pipeline" || phase === "complete";
  const pipelineComplete = phase === "complete";
  const inputDisabled = phase === "pipeline";
  const placeholder =
    phase === "clarifying"
      ? "Type your answer..."
      : "Describe your cloud infrastructure requirements...";

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col">
      {/* Agent progress bar (show during/after pipeline) */}
      {showProgress && (
        <div className="shrink-0 px-4 py-3">
          <AgentProgress progress={agentProgress} />
        </div>
      )}

      {/* Messages area */}
      <ScrollArea className="flex-1 px-4">
        {messages.length === 0 ? (
          <div className="flex h-full items-center justify-center py-20">
            <div className="text-center">
              <h2 className="text-2xl font-semibold tracking-tight">
                Cloud Orchestrator IDSS
              </h2>
              <p className="mt-2 text-muted-foreground">
                Describe your cloud infrastructure requirements to get started.
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                The system will analyze your needs across AWS, Azure, and GCP to
                recommend optimal resource configurations.
              </p>
            </div>
          </div>
        ) : (
          <div className="space-y-1 pb-4">
            {messages.map((msg, idx) => (
              <ChatMessage key={idx} message={msg} />
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </ScrollArea>

      {/* Error alert */}
      {error && (
        <div className="shrink-0 px-4">
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        </div>
      )}

      {/* Pipeline complete banner */}
      {pipelineComplete && (
        <div className="shrink-0 px-4 py-2">
          <div className="flex items-center justify-between rounded-lg border bg-green-50 px-4 py-2.5 dark:bg-green-950/30">
            <span className="text-sm text-green-700 dark:text-green-300">
              Pipeline completed
              {durationMs != null && ` in ${(durationMs / 1000).toFixed(1)}s`}
            </span>
            <Button asChild size="sm" variant="outline">
              <Link to="/results">View Results &rarr;</Link>
            </Button>
          </div>
        </div>
      )}

      {/* Input */}
      <div className="shrink-0">
        <ChatInput
          onSend={sendMessage}
          disabled={inputDisabled}
          placeholder={placeholder}
        />
      </div>
    </div>
  );
}
