import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { CostComparison, CloudProvider } from "@/types/api";

interface Props {
  costComparison: CostComparison;
}

const COST_ROWS: {
  label: string;
  key: string;
}[] = [
  { label: "Compute", key: "compute_monthly_usd" },
  { label: "Database", key: "database_monthly_usd" },
  { label: "Storage", key: "storage_monthly_usd" },
  { label: "Kubernetes", key: "kubernetes_monthly_usd" },
  { label: "Networking", key: "networking_monthly_usd" },
  { label: "Serverless", key: "serverless_monthly_usd" },
  { label: "Other", key: "other_monthly_usd" },
];

const PROVIDER_LABEL: Record<CloudProvider, string> = {
  aws: "AWS",
  azure: "Azure",
  gcp: "GCP",
};

function fmt(val: number): string {
  return val > 0 ? `$${val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—";
}

export function CostComparisonTable({ costComparison }: Props) {
  const { providers, cheapest_provider } = costComparison;

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-[160px]">Category</TableHead>
            {providers.map((p) => (
              <TableHead
                key={p.provider}
                className={cn(
                  "text-right",
                  p.provider === cheapest_provider && "text-green-600 dark:text-green-400 font-semibold",
                )}
              >
                {PROVIDER_LABEL[p.provider] ?? p.provider.toUpperCase()}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {COST_ROWS.map(({ label, key }) => (
            <TableRow key={key}>
              <TableCell className="font-medium">{label}</TableCell>
              {providers.map((p) => {
                const val = p[key as keyof typeof p] as number;
                return (
                  <TableCell key={p.provider} className="text-right tabular-nums">
                    {fmt(val)}
                  </TableCell>
                );
              })}
            </TableRow>
          ))}

          {/* Total row */}
          <TableRow className="border-t-2 font-bold">
            <TableCell>Total (Monthly)</TableCell>
            {providers.map((p) => (
              <TableCell
                key={p.provider}
                className={cn(
                  "text-right tabular-nums",
                  p.provider === cheapest_provider && "text-green-600 dark:text-green-400",
                )}
              >
                {fmt(p.total_monthly_usd)}
              </TableCell>
            ))}
          </TableRow>

          {/* Annual */}
          <TableRow className="text-muted-foreground">
            <TableCell>Total (Annual)</TableCell>
            {providers.map((p) => (
              <TableCell key={p.provider} className="text-right tabular-nums">
                {fmt(p.total_annual_usd)}
              </TableCell>
            ))}
          </TableRow>

          {/* Savings rows */}
          <TableRow>
            <TableCell className="text-sm">RI 1-Year Savings</TableCell>
            {providers.map((p) => (
              <TableCell key={p.provider} className="text-right text-sm tabular-nums">
                {p.reserved_1yr_savings_pct != null
                  ? `${p.reserved_1yr_savings_pct.toFixed(1)}%`
                  : "—"}
              </TableCell>
            ))}
          </TableRow>
          <TableRow>
            <TableCell className="text-sm">RI 3-Year Savings</TableCell>
            {providers.map((p) => (
              <TableCell key={p.provider} className="text-right text-sm tabular-nums">
                {p.reserved_3yr_savings_pct != null
                  ? `${p.reserved_3yr_savings_pct.toFixed(1)}%`
                  : "—"}
              </TableCell>
            ))}
          </TableRow>
          <TableRow>
            <TableCell className="text-sm">Spot Savings</TableCell>
            {providers.map((p) => (
              <TableCell key={p.provider} className="text-right text-sm tabular-nums">
                {p.spot_savings_pct != null
                  ? `${p.spot_savings_pct.toFixed(1)}%`
                  : "—"}
              </TableCell>
            ))}
          </TableRow>
        </TableBody>
      </Table>
    </div>
  );
}
