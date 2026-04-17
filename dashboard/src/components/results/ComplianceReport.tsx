import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { ComplianceReport as ComplianceReportType } from "@/types/api";

interface Props {
  report: ComplianceReportType;
}

const SEVERITY_COLOR: Record<string, string> = {
  critical: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
  high: "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300",
  medium: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300",
  low: "bg-slate-100 text-slate-800 dark:bg-slate-800/30 dark:text-slate-300",
};

export function ComplianceReport({ report }: Props) {
  const { framework, checks, total_checks, passed_checks, compliance_score_pct } =
    report;

  // Group checks by pillar
  const grouped = checks.reduce<Record<string, typeof checks>>(
    (acc, check) => {
      (acc[check.pillar] ??= []).push(check);
      return acc;
    },
    {},
  );

  return (
    <div className="space-y-6">
      {/* Score header */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-lg">{framework}</CardTitle>
            <div className="flex items-center gap-3">
              <span className="text-sm text-muted-foreground">
                {passed_checks}/{total_checks} checks passed
              </span>
              <Badge
                className={cn(
                  "text-lg px-3 py-1",
                  compliance_score_pct >= 80
                    ? "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300"
                    : compliance_score_pct >= 50
                      ? "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300"
                      : "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
                )}
              >
                {compliance_score_pct.toFixed(0)}%
              </Badge>
            </div>
          </div>
        </CardHeader>
      </Card>

      {/* Pillar sections */}
      {Object.entries(grouped).map(([pillar, pillarChecks]) => (
        <Card key={pillar}>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">{pillar}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[40px]">Status</TableHead>
                    <TableHead>Check</TableHead>
                    <TableHead className="w-[80px]">Severity</TableHead>
                    <TableHead>Finding</TableHead>
                    <TableHead>Recommendation</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pillarChecks.map((check, idx) => (
                    <TableRow key={idx}>
                      <TableCell className="text-center">
                        {check.passed ? (
                          <span className="text-green-500">&#10003;</span>
                        ) : (
                          <span className="text-red-500">&#10007;</span>
                        )}
                      </TableCell>
                      <TableCell className="font-medium text-sm">
                        {check.check_name}
                      </TableCell>
                      <TableCell>
                        <Badge
                          className={cn(
                            "text-[10px]",
                            SEVERITY_COLOR[check.severity] ?? SEVERITY_COLOR.low,
                          )}
                        >
                          {check.severity}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-sm">{check.finding}</TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {check.recommendation}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
