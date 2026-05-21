import React from "react";
import type { ProcessorArchitectureEntry } from "../../types/api";

interface Props {
  insights: ProcessorArchitectureEntry[];
}

function ArchBadge({ arch_type }: { arch_type: ProcessorArchitectureEntry["arch_type"] }) {
  if (arch_type === "graviton") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-purple-100 px-2.5 py-0.5 text-xs font-semibold text-purple-800">
        <svg className="h-3 w-3" viewBox="0 0 24 24" fill="currentColor">
          <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
        </svg>
        ARM / Graviton
      </span>
    );
  }
  if (arch_type === "x86") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-2.5 py-0.5 text-xs font-semibold text-blue-800">
        x86 / Intel
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-semibold text-gray-700">
      Unknown
    </span>
  );
}

function SmtBadge({ match }: { match: boolean }) {
  return match ? (
    <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800">
      ✓ SMT match
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
      ⚠ SMT mismatch
    </span>
  );
}

function LatencyRiskBadge({ risk }: { risk: ProcessorArchitectureEntry["breaking_latency_risk"] }) {
  const styles: Record<string, string> = {
    LOW: "bg-green-100 text-green-800",
    MEDIUM: "bg-amber-100 text-amber-800",
    HIGH: "bg-red-100 text-red-800",
  };
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${styles[risk]}`}>
      {risk}
    </span>
  );
}

function ScoreBar({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const colour = pct >= 80 ? "bg-green-500" : pct >= 60 ? "bg-amber-500" : "bg-red-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-24 rounded-full bg-gray-200">
        <div className={`h-2 rounded-full ${colour}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-600">{pct}%</span>
    </div>
  );
}

export const ProcessorArchitecturePanel: React.FC<Props> = ({ insights }) => {
  if (!insights || insights.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-gray-300 bg-gray-50 p-8 text-center text-sm text-gray-500">
        No processor architecture insights available for this recommendation.
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Summary banner */}
      <div className="rounded-lg border border-purple-200 bg-purple-50 p-4">
        <h3 className="mb-1 text-sm font-semibold text-purple-900">
          Processor Architecture Awareness (P17)
        </h3>
        <p className="text-xs text-purple-700">
          AWS Graviton (ARM) instances deliver up to 40% better performance at 20% lower cost and 60%
          less energy than comparable x86 instances. However, Graviton is single-threaded — workloads
          requiring Simultaneous Multi-Threading (SMT) perform better on x86. The table below shows the
          architecture recommendation per workload.
        </p>
      </div>

      {/* Insights table */}
      <div className="overflow-x-auto rounded-lg border border-gray-200 shadow-sm">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-gray-600">
                Workload
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-gray-600">
                SKU Family
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-gray-600">
                Architecture
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-gray-600">
                SMT
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-gray-600">
                Breaking Latency Risk
              </th>
              <th className="px-4 py-3 text-right text-xs font-semibold uppercase tracking-wider text-gray-600">
                Cost / mo
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-gray-600">
                Arch Score
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 bg-white">
            {insights.map((entry, idx) => (
              <tr key={idx} className="hover:bg-gray-50">
                <td className="px-4 py-3 font-medium text-gray-900">
                  {entry.workload_name}
                </td>
                <td className="px-4 py-3 font-mono text-gray-700">
                  {entry.sku_family}
                </td>
                <td className="px-4 py-3">
                  <ArchBadge arch_type={entry.arch_type} />
                </td>
                <td className="px-4 py-3">
                  <SmtBadge match={entry.smt_match} />
                </td>
                <td className="px-4 py-3">
                  <LatencyRiskBadge risk={entry.breaking_latency_risk} />
                </td>
                <td className="px-4 py-3 text-right font-mono text-gray-700">
                  ${entry.cost_monthly_usd.toFixed(2)}
                </td>
                <td className="px-4 py-3">
                  <ScoreBar score={entry.architecture_score} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Rationale cards */}
      <div className="grid gap-4 sm:grid-cols-2">
        {insights.map((entry, idx) => (
          <div
            key={idx}
            className={`rounded-lg border p-4 text-sm ${
              entry.arch_type === "graviton"
                ? "border-purple-200 bg-purple-50"
                : "border-blue-200 bg-blue-50"
            }`}
          >
            <div className="mb-1 flex items-center justify-between">
              <span className="font-semibold text-gray-900">{entry.workload_name}</span>
              <ArchBadge arch_type={entry.arch_type} />
            </div>
            <p className="text-xs text-gray-600">{entry.rationale}</p>
          </div>
        ))}
      </div>

      {/* Reference footnote */}
      <p className="text-xs text-gray-400">
        Sources: AWS EC2 Graviton Fast Start; AWS Graviton Sustainability Page; AWS Graviton Getting Started
        Perfrunbook.
      </p>
    </div>
  );
};

export default ProcessorArchitecturePanel;
