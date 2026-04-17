import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CostComparison } from "@/types/api";

interface Props {
  costComparison: CostComparison;
}

const PROVIDER_COLORS: Record<string, string> = {
  aws: "#f97316",   // orange-500
  azure: "#3b82f6", // blue-500
  gcp: "#ef4444",   // red-500
};

const CATEGORIES = [
  { key: "compute_monthly_usd", label: "Compute" },
  { key: "database_monthly_usd", label: "Database" },
  { key: "storage_monthly_usd", label: "Storage" },
  { key: "kubernetes_monthly_usd", label: "Kubernetes" },
  { key: "networking_monthly_usd", label: "Networking" },
  { key: "serverless_monthly_usd", label: "Serverless" },
] as const;

export function CostComparisonChart({ costComparison }: Props) {
  const { providers } = costComparison;

  // Category breakdown chart data
  const categoryData = CATEGORIES.map(({ key, label }) => {
    const row: Record<string, string | number> = { category: label };
    for (const p of providers) {
      row[p.provider] = p[key as keyof typeof p] as number;
    }
    return row;
  });

  // Pricing tier comparison chart data
  const tierData = providers.map((p) => ({
    provider: p.provider.toUpperCase(),
    "On-Demand": p.total_monthly_usd,
    "RI 1-Year": p.reserved_1yr_monthly_usd ?? 0,
    "RI 3-Year": p.reserved_3yr_monthly_usd ?? 0,
    Spot: p.spot_monthly_usd ?? 0,
  }));

  const providerKeys = providers.map((p) => p.provider);

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      {/* Category breakdown */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Cost by Category</CardTitle>
        </CardHeader>
        <CardContent>
          <ResponsiveContainer width="100%" height={320}>
            <BarChart data={categoryData}>
              <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
              <XAxis dataKey="category" tick={{ fontSize: 12 }} />
              <YAxis tick={{ fontSize: 12 }} tickFormatter={(v) => `$${v}`} />
              <Tooltip
                formatter={(value) =>
                  `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}`
                }
              />
              <Legend />
              {providerKeys.map((prov) => (
                <Bar
                  key={prov}
                  dataKey={prov}
                  name={prov.toUpperCase()}
                  fill={PROVIDER_COLORS[prov] ?? "#94a3b8"}
                />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      {/* Pricing tier comparison */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Pricing Tier Comparison</CardTitle>
        </CardHeader>
        <CardContent>
          <ResponsiveContainer width="100%" height={320}>
            <BarChart data={tierData}>
              <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
              <XAxis dataKey="provider" tick={{ fontSize: 12 }} />
              <YAxis tick={{ fontSize: 12 }} tickFormatter={(v) => `$${v}`} />
              <Tooltip
                formatter={(value) =>
                  `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}`
                }
              />
              <Legend />
              <Bar dataKey="On-Demand" fill="#6366f1" />
              <Bar dataKey="RI 1-Year" fill="#22c55e" />
              <Bar dataKey="RI 3-Year" fill="#14b8a6" />
              <Bar dataKey="Spot" fill="#eab308" />
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>
    </div>
  );
}
