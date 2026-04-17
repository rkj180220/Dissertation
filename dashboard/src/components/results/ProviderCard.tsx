import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { ProviderCostBreakdown } from "@/types/api";

interface Props {
  breakdown: ProviderCostBreakdown;
  isCheapest?: boolean;
}

const PROVIDER_LABEL: Record<string, string> = {
  aws: "Amazon Web Services",
  azure: "Microsoft Azure",
  gcp: "Google Cloud Platform",
};

function fmt(val: number | null | undefined): string {
  if (val == null) return "N/A";
  return `$${val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Derive a monthly estimate from a raw SKU when the backend property is missing. */
function skuMonthlyCost(sku: Record<string, unknown>): number | null {
  // Prefer the pre-computed property if present
  if (typeof sku.monthly_cost_estimate === "number") return sku.monthly_cost_estimate;
  const price = (typeof sku.unit_price === "number" ? sku.unit_price : null)
    ?? (typeof sku.retail_price === "number" ? sku.retail_price : null);
  if (price == null) return null;
  const unit = String(sku.unit_of_measure ?? "").toLowerCase();
  if (unit.includes("hour")) return price * 730;
  if (unit.includes("month")) return price;
  return null;
}

export function ProviderCard({ breakdown, isCheapest }: Props) {
  const { provider, total_monthly_usd, total_annual_usd, selected_skus } =
    breakdown;

  const savings = [
    breakdown.reserved_1yr_savings_pct != null && {
      label: "RI 1-Year",
      pct: breakdown.reserved_1yr_savings_pct,
    },
    breakdown.reserved_3yr_savings_pct != null && {
      label: "RI 3-Year",
      pct: breakdown.reserved_3yr_savings_pct,
    },
    breakdown.spot_savings_pct != null && {
      label: "Spot",
      pct: breakdown.spot_savings_pct,
    },
  ].filter(Boolean) as { label: string; pct: number }[];

  return (
    <Card className={isCheapest ? "border-green-500 ring-1 ring-green-500/20" : ""}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-lg">
            {PROVIDER_LABEL[provider] ?? provider.toUpperCase()}
          </CardTitle>
          {isCheapest && (
            <Badge className="bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300">
              Cheapest
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Cost summary */}
        <div className="grid grid-cols-2 gap-4">
          <div>
            <p className="text-xs text-muted-foreground">Monthly</p>
            <p className="text-2xl font-bold tabular-nums">{fmt(total_monthly_usd)}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Annual</p>
            <p className="text-2xl font-bold tabular-nums">{fmt(total_annual_usd)}</p>
          </div>
        </div>

        {/* Savings opportunities */}
        {savings.length > 0 && (
          <div>
            <p className="mb-1.5 text-xs font-medium text-muted-foreground">
              Savings Opportunities
            </p>
            <div className="flex flex-wrap gap-1.5">
              {savings.map(({ label, pct }) => (
                <Badge key={label} variant="outline" className="text-xs">
                  {label}: {pct.toFixed(1)}%
                </Badge>
              ))}
            </div>
          </div>
        )}

        {/* SKUs */}
        {selected_skus.length > 0 && (
          <div>
            <p className="mb-1.5 text-xs font-medium text-muted-foreground">
              Selected SKUs ({selected_skus.length})
            </p>
            <div className="space-y-1">
              {selected_skus.slice(0, 5).map((sku, idx) => (
                <div
                  key={idx}
                  className="flex items-center justify-between rounded border px-2 py-1 text-xs"
                >
                  <span className="truncate font-mono">
                    {sku.sku_name}
                  </span>
                  <span className="ml-2 shrink-0 tabular-nums text-muted-foreground">
                    {fmt(skuMonthlyCost(sku as unknown as Record<string, unknown>))}/mo
                  </span>
                </div>
              ))}
              {selected_skus.length > 5 && (
                <p className="text-xs text-muted-foreground">
                  +{selected_skus.length - 5} more
                </p>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
