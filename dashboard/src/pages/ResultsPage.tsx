import { Link } from "react-router-dom";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { usePipeline } from "@/context/PipelineContext";
import { ExecutiveSummary } from "@/components/results/ExecutiveSummary";
import { CostComparisonTable } from "@/components/results/CostComparisonTable";
import { CostComparisonChart } from "@/components/results/CostComparisonChart";
import { ProviderCard } from "@/components/results/ProviderCard";
import { ComplianceReport } from "@/components/results/ComplianceReport";
import { RfpDocument } from "@/components/results/RfpDocument";
import type { CostComparison, ComplianceReport as ComplianceReportType } from "@/types/api";

export default function ResultsPage() {
  const { result, messages } = usePipeline();

  // Extract data from the pipeline result OR from messages
  const rfpDocument = result?.rfp_document ?? "";
  const executiveSummary = result?.executive_summary ?? "";
  const recommendedProvider = result?.recommended_provider ?? null;

  // Try to get structured data from result
  const costComparison: CostComparison | null =
    result?.cost_comparison ?? null;

  const complianceReport: ComplianceReportType | null =
    result?.compliance_report ?? null;

  const hasResults =
    messages.length > 0 && (rfpDocument || executiveSummary || costComparison);

  if (!hasResults) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-20">
        <h2 className="text-2xl font-semibold tracking-tight">No Results Yet</h2>
        <p className="text-muted-foreground">
          Run a pipeline from the Chat page to see results here.
        </p>
        <Button asChild>
          <Link to="/chat">Go to Chat</Link>
        </Button>
      </div>
    );
  }

  return (
    <Tabs defaultValue="overview" className="space-y-6">
      <TabsList className="grid w-full grid-cols-4">
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="costs">Cost Analysis</TabsTrigger>
        <TabsTrigger value="compliance">Compliance</TabsTrigger>
        <TabsTrigger value="rfp">RFP Document</TabsTrigger>
      </TabsList>

      {/* Overview Tab */}
      <TabsContent value="overview" className="space-y-6">
        {executiveSummary && (
          <ExecutiveSummary
            summary={executiveSummary}
            recommendedProvider={recommendedProvider}
            costComparison={costComparison}
          />
        )}

        {costComparison && costComparison.providers.length > 0 && (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {costComparison.providers.map((p) => (
              <ProviderCard
                key={p.provider}
                breakdown={p}
                isCheapest={p.provider === costComparison.cheapest_provider}
              />
            ))}
          </div>
        )}
      </TabsContent>

      {/* Cost Analysis Tab */}
      <TabsContent value="costs" className="space-y-6">
        {costComparison ? (
          <>
            <CostComparisonChart costComparison={costComparison} />
            <CostComparisonTable costComparison={costComparison} />
          </>
        ) : (
          <p className="text-center text-muted-foreground py-10">
            No cost data available.
          </p>
        )}
      </TabsContent>

      {/* Compliance Tab */}
      <TabsContent value="compliance">
        {complianceReport ? (
          <ComplianceReport report={complianceReport} />
        ) : (
          <p className="text-center text-muted-foreground py-10">
            No compliance data available.
          </p>
        )}
      </TabsContent>

      {/* RFP Document Tab */}
      <TabsContent value="rfp">
        {rfpDocument ? (
          <RfpDocument document={rfpDocument} />
        ) : (
          <p className="text-center text-muted-foreground py-10">
            No RFP document generated yet.
          </p>
        )}
      </TabsContent>
    </Tabs>
  );
}
