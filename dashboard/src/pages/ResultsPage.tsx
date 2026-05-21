import { Link } from "react-router-dom";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { usePipeline } from "@/context/PipelineContext";
import { ExecutiveSummary } from "@/components/results/ExecutiveSummary";
import { CostComparisonTable } from "@/components/results/CostComparisonTable";
import { CostComparisonChart } from "@/components/results/CostComparisonChart";
import { ProviderCard } from "@/components/results/ProviderCard";
import { ComplianceReport } from "@/components/results/ComplianceReport";
import { RfpDocument } from "@/components/results/RfpDocument";
import { ArchitectureRadarChart } from "@/components/results/ArchitectureRadarChart";
import { FeedbackWidget } from "@/components/results/FeedbackWidget";
import { ProcessorArchitecturePanel } from "@/components/results/ProcessorArchitecturePanel";
import type {
  CostComparison,
  ComplianceReport as ComplianceReportType,
  ArchitectureAlternative,
  ProcessorArchitectureEntry,
} from "@/types/api";

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

  const architectureAlternatives: ArchitectureAlternative[] =
    result?.architecture_alternatives ?? [];

  const processorArchInsights: ProcessorArchitectureEntry[] =
    result?.processor_architecture_insights ?? [];

  const requestId = result?.request_id ?? "";

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
      <TabsList className="grid w-full grid-cols-6">
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="architecture">Architecture</TabsTrigger>
        <TabsTrigger value="costs">Cost Analysis</TabsTrigger>
        <TabsTrigger value="compliance">Compliance</TabsTrigger>
        <TabsTrigger value="rfp">RFP Document</TabsTrigger>
        <TabsTrigger value="processor">Arch Insights</TabsTrigger>
      </TabsList>

      {/* Overview Tab */}
      <TabsContent value="overview" className="space-y-6">
        <ErrorBoundary title="Overview panel error">
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
        </ErrorBoundary>
      </TabsContent>

      {/* Architecture Tab — P16e radar chart */}
      <TabsContent value="architecture" className="space-y-4">
        <ErrorBoundary title="Architecture chart error">
          <div>
            <h2 className="text-lg font-semibold tracking-tight">
              Architecture Comparison
            </h2>
            <p className="text-sm text-muted-foreground">
              WAF pillar scores across the four candidate patterns.
              Higher is better for all axes (cost axis = cost efficiency).
            </p>
          </div>
          <ArchitectureRadarChart
            alternatives={architectureAlternatives}
            recommendedName={
              result?.recommended_provider
                ? undefined
                : architectureAlternatives[0]?.name
            }
          />
        </ErrorBoundary>
      </TabsContent>

      {/* Cost Analysis Tab */}
      <TabsContent value="costs" className="space-y-6">
        <ErrorBoundary title="Cost analysis error">
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
        </ErrorBoundary>
      </TabsContent>

      {/* Compliance Tab */}
      <TabsContent value="compliance">
        <ErrorBoundary title="Compliance report error">
          {complianceReport ? (
            <ComplianceReport report={complianceReport} />
          ) : (
            <p className="text-center text-muted-foreground py-10">
              No compliance data available.
            </p>
          )}
        </ErrorBoundary>
      </TabsContent>

      {/* RFP Document Tab */}
      <TabsContent value="rfp" className="space-y-4">
        <ErrorBoundary title="RFP document error">
          {rfpDocument ? (
            <RfpDocument document={rfpDocument} />
          ) : (
            <p className="text-center text-muted-foreground py-10">
              No RFP document generated yet.
            </p>
          )}
          {requestId && <FeedbackWidget requestId={requestId} />}
        </ErrorBoundary>
      </TabsContent>

      {/* Processor Architecture Insights Tab — P17 */}
      <TabsContent value="processor" className="space-y-4">
        <ErrorBoundary title="Processor architecture error">
          <ProcessorArchitecturePanel insights={processorArchInsights} />
        </ErrorBoundary>
      </TabsContent>
    </Tabs>
  );
}
