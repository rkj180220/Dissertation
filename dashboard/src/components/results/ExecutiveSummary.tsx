import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { CostComparison } from "@/types/api";

const PROVIDER_COLOR: Record<string, string> = {
  aws: "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300",
  azure: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300",
  gcp: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
};

interface Props {
  summary: string;
  recommendedProvider: string | null;
  costComparison: CostComparison | null;
  projectName?: string;
}

export function ExecutiveSummary({
  summary,
  recommendedProvider,
  costComparison,
  projectName,
}: Props) {
  const budgetExceeded = costComparison?.budget_exceeded ?? false;
  const budget = costComparison?.budget_monthly_usd;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-xl">Executive Summary</CardTitle>
          <div className="flex items-center gap-2">
            {projectName && (
              <Badge variant="secondary">{projectName}</Badge>
            )}
            {recommendedProvider && (
              <Badge
                className={
                  PROVIDER_COLOR[recommendedProvider] ?? "bg-muted"
                }
              >
                Recommended: {recommendedProvider.toUpperCase()}
              </Badge>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Budget status */}
        {budget != null && (
          <div className="flex items-center gap-2">
            <Badge variant={budgetExceeded ? "destructive" : "secondary"}>
              {budgetExceeded ? "Over Budget" : "Within Budget"}
            </Badge>
            <span className="text-sm text-muted-foreground">
              Budget: ${budget.toLocaleString()}/mo
            </span>
          </div>
        )}

        {/* Summary */}
        <div className="prose prose-sm max-w-none dark:prose-invert">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {summary}
          </ReactMarkdown>
        </div>
      </CardContent>
    </Card>
  );
}
