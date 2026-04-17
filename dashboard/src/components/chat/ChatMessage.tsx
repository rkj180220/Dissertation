import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import type { ChatMessage as ChatMessageType } from "@/types/api";
import { AGENT_LABELS, type AgentName } from "@/context/PipelineContext";

interface Props {
  message: ChatMessageType;
}

export function ChatMessage({ message }: Props) {
  const isUser = message.role === "user";
  const isSystem = message.role === "system";

  if (isSystem) {
    return (
      <div className="flex justify-center py-2">
        <p className="text-xs text-muted-foreground italic">
          {message.content}
        </p>
      </div>
    );
  }

  const agentLabel = message.agent_name
    ? AGENT_LABELS[message.agent_name as AgentName] ?? message.agent_name
    : null;

  return (
    <div
      className={cn("flex gap-3 py-3", isUser ? "flex-row-reverse" : "flex-row")}
    >
      {/* Avatar */}
      <Avatar className="h-8 w-8 shrink-0">
        <AvatarFallback
          className={cn(
            "text-xs font-medium",
            isUser
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-muted-foreground",
          )}
        >
          {isUser ? "U" : "AI"}
        </AvatarFallback>
      </Avatar>

      {/* Bubble */}
      <div
        className={cn(
          "max-w-[80%] rounded-lg px-4 py-2.5",
          isUser
            ? "bg-primary text-primary-foreground"
            : "bg-muted",
        )}
      >
        {/* Agent badge */}
        {agentLabel && (
          <Badge variant="outline" className="mb-1.5 text-[10px]">
            {agentLabel}
          </Badge>
        )}

        {/* Content */}
        <div
          className={cn(
            "prose prose-sm max-w-none",
            isUser ? "prose-invert" : "",
          )}
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {message.content}
          </ReactMarkdown>
        </div>

        {/* Timestamp */}
        <p
          className={cn(
            "mt-1.5 text-[10px]",
            isUser
              ? "text-primary-foreground/60"
              : "text-muted-foreground",
          )}
        >
          {new Date(message.timestamp).toLocaleTimeString()}
        </p>
      </div>
    </div>
  );
}
