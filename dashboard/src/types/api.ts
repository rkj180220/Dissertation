// ────────────────────────────────────────────────
// TypeScript interfaces matching backend Pydantic models
// ────────────────────────────────────────────────

// --- Enums ---

export type CloudProvider = "aws" | "azure" | "gcp";

export type ServiceCategory =
  | "COMPUTE"
  | "SERVERLESS_COMPUTE"
  | "CONTAINER"
  | "SERVERLESS_FUNCTION"
  | "DATABASE"
  | "STORAGE"
  | "NETWORKING"
  | "AI_ML"
  | "ANALYTICS"
  | "MANAGEMENT"
  | "SECURITY"
  | "INTEGRATION"
  | "IOT"
  | "OTHER";

export type PricingTier =
  | "ON_DEMAND"
  | "SPOT"
  | "LOW_PRIORITY"
  | "RESERVED_1YR"
  | "RESERVED_3YR"
  | "SAVINGS_PLAN_1YR"
  | "SAVINGS_PLAN_3YR"
  | "DEV_TEST";

export type EnvironmentType =
  | "production"
  | "staging"
  | "development"
  | "disaster_recovery";

export type WorkloadTier =
  | "mission_critical"
  | "business_critical"
  | "non_critical";

export type ScalingPattern =
  | "steady"
  | "bursty"
  | "growing"
  | "unpredictable"
  | "batch";

export type MessageRole = "user" | "assistant" | "system";

export type AgentStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "skipped";

export type ClarificationStatus =
  | "pending"
  | "answered"
  | "skipped"
  | "inferred";

export type ClarificationPriority = "required" | "recommended" | "optional";

// --- Conversation Models ---

export interface ChatMessage {
  role: MessageRole;
  content: string;
  timestamp: string;
  agent_name: string | null;
  metadata: Record<string, unknown>;
}

export interface ClarificationQuestion {
  question_id: string;
  question_text: string;
  target_field: string;
  priority: ClarificationPriority;
  status: ClarificationStatus;
  default_value: string | null;
  user_answer: string | null;
  resolved_value: unknown;
}

export interface ConversationState {
  conversation_id: string;
  messages: ChatMessage[];
  clarification_questions: ClarificationQuestion[];
  current_turn: number;
  max_clarification_turns: number;
  requirements_complete: boolean;
}

// --- Pricing Models ---

export interface NormalizedPriceItem {
  provider: CloudProvider;
  sku_id: string;
  sku_name: string;
  service_name: string;
  category: ServiceCategory;
  region: string;
  tier: PricingTier;
  unit_price: number;
  unit: string;
  currency: string;
  attributes: Record<string, string>;
  monthly_cost_estimate: number | null;
}

// --- Workload Models ---

export interface ResourceSpec {
  vcpus: number | null;
  memory_gb: number | null;
  gpu_count: number;
  gpu_type: string | null;
  architecture: string;
  os: string;
  storage_gb: number | null;
  storage_type: string | null;
  iops: number | null;
  throughput_mbps: number | null;
  redundancy: string | null;
  database_engine: string | null;
  database_version: string | null;
  high_availability: boolean;
  read_replicas: number;
  cpu_request_millicores: number | null;
  cpu_limit_millicores: number | null;
  memory_request_mb: number | null;
  memory_limit_mb: number | null;
  replicas: number;
  network_bandwidth_gbps: number | null;
  public_endpoint: boolean;
  invocations_per_month: number | null;
  avg_duration_ms: number | null;
  memory_mb: number | null;
}

export interface WorkloadRequirement {
  name: string;
  description: string;
  suggested_category: ServiceCategory;
  scaling_pattern: ScalingPattern;
  count: number;
  resources: ResourceSpec;
  region_affinity: string | null;
  provider_preference: CloudProvider | null;
  compliance_tags: string[];
  notes: string;
}

export interface WorkloadRequest {
  project_name: string;
  environment: EnvironmentType;
  tier: WorkloadTier;
  target_providers: CloudProvider[];
  preferred_region: string;
  provider_regions: Record<string, string>;
  workloads: WorkloadRequirement[];
  budget_monthly_usd: number | null;
  compliance_frameworks: string[];
  raw_user_input: string;
}

// --- Profiler Models ---

export interface ComponentProfile {
  workload_name: string;
  resolved_category: ServiceCategory;
  estimated_vcpus: number;
  estimated_memory_gb: number;
  estimated_storage_gb: number;
  estimated_iops: number | null;
  requires_gpu: boolean;
  recommended_instance_families: string[];
  rationale: string;
}

export interface WorkloadProfile {
  components: ComponentProfile[];
  total_vcpus: number;
  total_memory_gb: number;
  total_storage_gb: number;
  total_gpu_count: number;
  requires_gpu: boolean;
  environment: EnvironmentType;
  tier: WorkloadTier;
  profiler_notes: string;
}

// --- Recommendation Models ---

export interface PackedNode {
  node_sku: NormalizedPriceItem;
  assigned_workloads: string[];
  cpu_utilization_pct: number;
  memory_utilization_pct: number;
  wasted_cpu_millicores: number;
  wasted_memory_mb: number;
}

export interface BinPackingResult {
  provider: CloudProvider;
  node_pool_name: string;
  nodes: PackedNode[];
  total_nodes: number;
  packing_efficiency_pct: number;
  total_monthly_cost_usd: number;
  algorithm_used: string;
}

export interface ProviderCostBreakdown {
  provider: CloudProvider;
  compute_monthly_usd: number;
  database_monthly_usd: number;
  storage_monthly_usd: number;
  kubernetes_monthly_usd: number;
  networking_monthly_usd: number;
  serverless_monthly_usd: number;
  other_monthly_usd: number;
  total_monthly_usd: number;
  total_annual_usd: number;
  reserved_1yr_monthly_usd: number | null;
  reserved_1yr_savings_pct: number | null;
  reserved_3yr_monthly_usd: number | null;
  reserved_3yr_savings_pct: number | null;
  spot_monthly_usd: number | null;
  spot_savings_pct: number | null;
  selected_skus: NormalizedPriceItem[];
}

export interface CostComparison {
  providers: ProviderCostBreakdown[];
  cheapest_provider: CloudProvider | null;
  savings_vs_most_expensive_pct: number;
  budget_monthly_usd: number | null;
  budget_exceeded: boolean;
  generated_at: string;
}

export interface ComplianceCheckResult {
  pillar: string;
  check_name: string;
  passed: boolean;
  severity: string;
  finding: string;
  recommendation: string;
}

export interface ComplianceReport {
  framework: string;
  checks: ComplianceCheckResult[];
  total_checks: number;
  passed_checks: number;
  compliance_score_pct: number;
}

// --- API Request / Response ---

export interface OrchestrationRequest {
  user_input: string;
  project_name?: string;
  request_id?: string;
}

export interface ArchitectureAlternative {
  name: string;
  label: string;
  score: number;
  monthly_cost_estimate: number;
  rationale: string;
  trade_offs: string;
  reliability_score: number;
  cost_score: number;
  scale_score: number;
  compliance_score: number;
  latency_score: number;
}

export interface ProcessorArchitectureEntry {
  workload_name: string;
  provider: string;
  sku_family: string;
  arch_type: "graviton" | "x86" | "unknown";
  smt_suitable: boolean;
  smt_match: boolean;
  breaking_latency_risk: "LOW" | "MEDIUM" | "HIGH";
  cost_monthly_usd: number;
  architecture_score: number;
  rationale: string;
}

export interface OrchestrationResponse {
  request_id: string;
  status: "completed" | "failed";
  rfp_document: string;
  executive_summary: string;
  recommended_provider: string | null;
  cost_comparison: CostComparison | null;
  compliance_report: ComplianceReport | null;
  architecture_alternatives?: ArchitectureAlternative[];
  processor_architecture_insights?: ProcessorArchitectureEntry[];
  error: string | null;
  duration_ms: number | null;
}

// --- Clarification ---

export interface ClarifyRequest {
  user_input: string;
  project_name?: string;
  request_id?: string;
}

export interface ClarifyResponse {
  request_id: string;
  status: "clarifying" | "ready";
  message: string;
  enriched_input?: string;
}

// --- SSE Event Types ---

export interface SSEAgentUpdate {
  event: "agent_update";
  data: {
    request_id: string;
    agent: string;
    timestamp: string;
    keys_updated: string[];
  };
}

export interface SSEMessage {
  event: "message";
  data: {
    request_id: string;
    agent: string;
    content: string;
  };
}

export interface SSEPipelineComplete {
  event: "pipeline_complete";
  data: {
    request_id: string;
    status: "completed";
    duration_ms: number;
    rfp_document?: string;
    executive_summary?: string;
    recommended_provider?: string;
    cost_comparison?: CostComparison;
    compliance_report?: ComplianceReport;
    architecture_alternatives?: ArchitectureAlternative[];
    processor_architecture_insights?: ProcessorArchitectureEntry[];
  };
}

export interface SSEError {
  event: "error";
  data: {
    request_id: string;
    error: string;
    duration_ms: number;
  };
}

export type SSEEvent =
  | SSEAgentUpdate
  | SSEMessage
  | SSEPipelineComplete
  | SSEError;

// --- Health ---

export interface HealthResponse {
  status: string;
  service: string;
  timestamp: string;
}

export interface ReadyCheck {
  status: "ok" | "error" | "unavailable";
  providers?: string[];
  detail?: string;
}

export interface ReadyResponse {
  status: "ready" | "degraded";
  checks: {
    pricing_service: ReadyCheck;
    llm: ReadyCheck;
    graph: ReadyCheck;
  };
  timestamp: string;
}
