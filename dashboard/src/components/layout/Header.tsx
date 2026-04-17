import { Link, useLocation } from "react-router-dom";
import { useHealth } from "@/hooks/useHealth";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/chat", label: "Chat" },
  { to: "/results", label: "Results" },
] as const;

export function Header() {
  const { pathname } = useLocation();
  const { status } = useHealth();

  const dotColor =
    status === "healthy"
      ? "bg-green-500"
      : status === "degraded"
        ? "bg-yellow-500"
        : "bg-red-500";

  return (
    <header className="sticky top-0 z-50 w-full border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="container flex h-14 items-center">
        {/* Logo */}
        <Link to="/" className="mr-6 flex items-center space-x-2">
          <span className="text-lg font-bold">Cloud Orchestrator IDSS</span>
        </Link>

        {/* Nav */}
        <nav className="flex items-center space-x-6 text-sm font-medium">
          {NAV_ITEMS.map(({ to, label }) => (
            <Link
              key={to}
              to={to}
              className={cn(
                "transition-colors hover:text-foreground/80",
                pathname === to
                  ? "text-foreground"
                  : "text-foreground/60",
              )}
            >
              {label}
            </Link>
          ))}
        </nav>

        {/* Health indicator */}
        <div className="ml-auto flex items-center gap-2">
          <span
            className={cn("h-2.5 w-2.5 rounded-full", dotColor)}
            title={`Backend: ${status}`}
          />
          <span className="text-xs text-muted-foreground capitalize">
            {status}
          </span>
        </div>
      </div>
    </header>
  );
}
