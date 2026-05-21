import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
  /** Optional custom heading shown in the fallback card. */
  title?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Class-based React error boundary.
 *
 * Catches render/lifecycle errors in the subtree and shows a clean
 * fallback instead of an unmounted white page.  Provides a "Try again"
 * button that resets the boundary state so the user can retry without a
 * full page reload.
 */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[ErrorBoundary] Uncaught render error:", error, info.componentStack);
  }

  private handleReset = (): void => {
    this.setState({ hasError: false, error: null });
  };

  render(): ReactNode {
    if (!this.state.hasError) {
      return this.props.children;
    }

    const title = this.props.title ?? "Something went wrong";
    const message =
      this.state.error?.message ?? "An unexpected error occurred in this section.";

    return (
      <div className="flex flex-col items-center justify-center gap-4 rounded-lg border border-destructive/30 bg-destructive/5 px-6 py-10 text-center">
        {/* Warning triangle — inline SVG, no extra dep needed */}
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="32"
          height="32"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="text-destructive"
          aria-hidden="true"
        >
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
        <div className="space-y-1">
          <h3 className="text-base font-semibold">{title}</h3>
          <p className="max-w-md text-sm text-muted-foreground">{message}</p>
        </div>
        <Button variant="outline" size="sm" onClick={this.handleReset}>
          Try again
        </Button>
      </div>
    );
  }
}
