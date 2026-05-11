import {
  Radar,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  ResponsiveContainer,
  Legend,
  Tooltip,
} from "recharts";
import type { ArchitectureAlternative } from "@/types/api";

interface ArchitectureRadarChartProps {
  alternatives: ArchitectureAlternative[];
  recommendedName?: string;
}

const OPTION_COLORS: Record<string, string> = {
  managed_serverless: "#6366f1",    // indigo
  self_hosted_serverless: "#f59e0b", // amber
  containers: "#10b981",             // emerald
  hybrid: "#ef4444",                 // red
};

const OPTION_LABELS: Record<string, string> = {
  managed_serverless: "Managed Serverless",
  self_hosted_serverless: "Self-Hosted Serverless",
  containers: "Containers",
  hybrid: "Hybrid",
};

const AXES = [
  { key: "reliability_score", label: "Reliability" },
  { key: "cost_score",        label: "Cost Efficiency" },
  { key: "scale_score",       label: "Scalability" },
  { key: "compliance_score",  label: "Compliance" },
  { key: "latency_score",     label: "Latency" },
] as const;

type AxisKey = (typeof AXES)[number]["key"];

export function ArchitectureRadarChart({
  alternatives,
  recommendedName,
}: ArchitectureRadarChartProps) {
  if (!alternatives || alternatives.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No architecture alternatives available.
      </p>
    );
  }

  // Build recharts radar data: one object per WAF axis
  const radarData = AXES.map(({ key, label }) => {
    const entry: Record<string, string | number> = { axis: label };
    for (const alt of alternatives) {
      entry[alt.name] = Math.round((alt[key as AxisKey] ?? 0) * 100);
    }
    return entry;
  });

  return (
    <div className="space-y-4">
      <ResponsiveContainer width="100%" height={380}>
        <RadarChart data={radarData} margin={{ top: 20, right: 30, bottom: 20, left: 30 }}>
          <PolarGrid gridType="polygon" />
          <PolarAngleAxis
            dataKey="axis"
            tick={{ fontSize: 13, fill: "hsl(var(--foreground))" }}
          />
          <PolarRadiusAxis
            angle={90}
            domain={[0, 100]}
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            tickCount={6}
          />
          {alternatives.map((alt) => (
            <Radar
              key={alt.name}
              name={OPTION_LABELS[alt.name] ?? alt.label ?? alt.name}
              dataKey={alt.name}
              stroke={OPTION_COLORS[alt.name] ?? "#888"}
              fill={OPTION_COLORS[alt.name] ?? "#888"}
              fillOpacity={0.15}
              strokeWidth={alt.name === recommendedName ? 3 : 1.5}
              dot={alt.name === recommendedName}
            />
          ))}
          <Legend
            wrapperStyle={{ fontSize: 13 }}
            formatter={(value: string) => {
              const isRec = alternatives.find((a) => (OPTION_LABELS[a.name] ?? a.name) === value)?.name === recommendedName;
              return isRec ? `${value} ★` : value;
            }}
          />
          <Tooltip
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            formatter={(value: any) => [`${value}%`, ""]}
            contentStyle={{
              backgroundColor: "hsl(var(--background))",
              border: "1px solid hsl(var(--border))",
              borderRadius: 6,
              fontSize: 12,
            }}
          />
        </RadarChart>
      </ResponsiveContainer>

      {/* Score table */}
      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-muted/50">
              <th className="px-4 py-2 text-left font-medium">Architecture</th>
              {AXES.map(({ label }) => (
                <th key={label} className="px-3 py-2 text-right font-medium">
                  {label}
                </th>
              ))}
              <th className="px-3 py-2 text-right font-medium">Composite</th>
            </tr>
          </thead>
          <tbody>
            {[...alternatives]
              .sort((a, b) => b.score - a.score)
              .map((alt, i) => (
                <tr
                  key={alt.name}
                  className={`border-t ${alt.name === recommendedName ? "bg-primary/5 font-medium" : ""}`}
                >
                  <td className="px-4 py-2 flex items-center gap-1">
                    <span
                      className="inline-block h-2.5 w-2.5 rounded-full"
                      style={{ background: OPTION_COLORS[alt.name] ?? "#888" }}
                    />
                    {OPTION_LABELS[alt.name] ?? alt.label ?? alt.name}
                    {alt.name === recommendedName && (
                      <span className="ml-1 text-xs text-primary">★ Recommended</span>
                    )}
                    {i === 0 && alt.name !== recommendedName && (
                      <span className="ml-1 text-xs text-muted-foreground">(top score)</span>
                    )}
                  </td>
                  {AXES.map(({ key, label }) => (
                    <td key={label} className="px-3 py-2 text-right tabular-nums">
                      {Math.round((alt[key as AxisKey] ?? 0) * 100)}%
                    </td>
                  ))}
                  <td className="px-3 py-2 text-right tabular-nums font-semibold">
                    {Math.round(alt.score * 100)}%
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>

      {/* Rationale cards */}
      <div className="grid gap-3 sm:grid-cols-2">
        {alternatives.map((alt) => (
          <div key={alt.name} className="rounded-md border p-3 text-sm space-y-1">
            <p className="font-medium" style={{ color: OPTION_COLORS[alt.name] }}>
              {OPTION_LABELS[alt.name] ?? alt.label}
              {alt.name === recommendedName && " ★"}
            </p>
            <p className="text-muted-foreground leading-snug">{alt.rationale}</p>
            {alt.trade_offs && (
              <p className="text-xs text-muted-foreground/70 leading-snug">
                <span className="font-medium">Trade-offs: </span>
                {alt.trade_offs}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
