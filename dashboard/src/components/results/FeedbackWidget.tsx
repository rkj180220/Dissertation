import { useState } from "react";
import { Button } from "@/components/ui/button";
import { submitFeedback } from "@/lib/api";

function StarIcon({ filled }: { filled: boolean }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      className="h-7 w-7 transition-colors"
      fill={filled ? "#f59e0b" : "none"}
      stroke={filled ? "#f59e0b" : "#9ca3af"}
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
    </svg>
  );
}

interface FeedbackWidgetProps {
  requestId: string;
}

export function FeedbackWidget({ requestId }: FeedbackWidgetProps) {
  const [hovered, setHovered] = useState(0);
  const [selected, setSelected] = useState(0);
  const [comment, setComment] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!selected) return;
    setSubmitting(true);
    setError(null);
    try {
      await submitFeedback(requestId, selected, comment || undefined);
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Submission failed");
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <div className="rounded-lg border bg-muted/40 p-4 text-center text-sm text-muted-foreground">
        ✅ Thank you for your feedback!
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-4 space-y-3">
      <p className="text-sm font-medium">Rate this recommendation</p>

      {/* Star picker */}
      <div className="flex gap-1">
        {[1, 2, 3, 4, 5].map((star) => (
          <button
            key={star}
            type="button"
            aria-label={`${star} star${star !== 1 ? "s" : ""}`}
            onMouseEnter={() => setHovered(star)}
            onMouseLeave={() => setHovered(0)}
            onClick={() => setSelected(star)}
            className="p-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded"
          >
            <StarIcon filled={star <= (hovered || selected)} />
          </button>
        ))}
        {selected > 0 && (
          <span className="ml-2 text-sm text-muted-foreground self-center">
            {["", "Poor", "Fair", "Good", "Very good", "Excellent"][selected]}
          </span>
        )}
      </div>

      {/* Optional comment */}
      {selected > 0 && (
        <textarea
          placeholder="Optional: any additional comments?"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          className="w-full resize-none rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          rows={2}
          maxLength={2000}
        />
      )}

      {error && <p className="text-xs text-destructive">{error}</p>}

      <Button
        size="sm"
        disabled={!selected || submitting}
        onClick={handleSubmit}
      >
        {submitting ? "Submitting…" : "Submit feedback"}
      </Button>
    </div>
  );
}
