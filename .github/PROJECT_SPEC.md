# Cloud Orchestrator IDSS — Project Specification

> **Author**: Ramkumar J · BITS ID: 2024MT03027 · M.Tech Cloud Computing, BITS Pilani WILP
> **Supervisor**: Rajkumar Sakthibalan (Presidio Solutions, Chennai)
> **Additional Examiner**: Santhosh Kirubakaran
> **Last Updated**: 17 April 2026 (Full pipeline gap analysis — agent quality, SKU selection, RFP depth, LangFuse tracing)
> **LLM**: Claude Sonnet 4.5 (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) via AWS Bedrock

---

## 0. System Architecture

```
User (React chat) → FastAPI (SSE) → LangGraph Orchestrator
                                         │
                  ┌──────────────────────┤
                  │    Clarifier Agent    │ ← Multi-turn requirement refinement
                  │  (conditional loop)   │   Asks follow-up questions until
                  └──────────┬───────────┘   requirements are unambiguous
                             │ complete
                             ▼
                  ┌──────────────────────┐
                  │    Profiler Agent     │ ← Analyzes workload → WorkloadProfile
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │     Sizer Agent      │ ← Calls PricingService → scoring → bin-packing
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │    FinOps Agent      │ ← Multi-provider cost comparison
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │   RFP Writer Agent   │ ← Generates procurement document
                  └──────────────────────┘

  All agents share OrchestratorState (LangGraph TypedDict):
  ┌─────────────┐    ┌──────────────┐    ┌─────────────────────────┐
  │ PricingCache│ ←→ │PricingService│ ←  │  Sizer / FinOps agents  │
  │  (SQLite)   │    │   (facade)   │    │  (never call adapters)  │
  └─────────────┘    └──────┬───────┘    └─────────────────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        AWS Pricing    Azure Retail    GCP Billing
         (boto3)       Prices API      Catalog API
```

---

## Legend

| Symbol | Meaning |
|--------|---------|
| ✅ | Built, tested, and verified at runtime |
| ⚠️ | Exists but needs rewrite (uses deprecated models) |
| 🔧 | Scaffolded shell (imports/class structure only, no logic) |
| ❌ | Not yet created |

---

## 1. Infrastructure & Tooling

| Item | Status | Notes |
|------|--------|-------|
| Python 3.13.0 venv | ✅ | `.venv/` created, 148 deps synced |
| `pyproject.toml` | ✅ | hatchling build, all deps, uv scripts, pytest config |
| `uv.lock` | ✅ | Locked, reproducible |
| `.env.example` | ✅ | All 6 prefixes: `APP_`, `LLM_`, `AWS_`, `AZURE_`, `GCP_`, `LANGFUSE_` |
| `docker-compose.langfuse.yml` | ✅ | Self-hosted LangFuse v3.130.0 — 6 containers (postgres, clickhouse, redis, minio, worker, web). SDK v3 verified. |
| `.github/copilot-instructions.md` | ✅ | Tech stack rules, observability pattern, LLM abstraction rules |

---

## 2. Config Layer — `src/config/`

| File | Status | What it does |
|------|--------|-------------|
| `settings.py` | ✅ | 6 Pydantic-settings classes: `AppSettings`, `LLMSettings`, `AWSSettings`, `AzureSettings`, `GCPSettings`, `LangFuseSettings`. `get_settings()` with `@lru_cache` singleton. |
| `logging_config.py` | ✅ | `configure_observability()` — wires `structlog` JSON renderer + LangFuse SDK v3 integration. Gracefully degrades when LangFuse keys missing. |
| `__init__.py` | ✅ | Re-exports `get_settings`, `configure_observability`. |

---

## 3. LLM Factory — `src/llm/`

| File | Status | What it does |
|------|--------|-------------|
| `factory.py` | ✅ | `get_llm(provider, model, **kwargs) → BaseChatModel`. Lazy imports: `bedrock` branch imports `ChatBedrockConverse`, `gemini` branch imports `ChatGoogleGenerativeAI`. Never imported directly in agent code. |
| `__init__.py` | ✅ | Re-exports `get_llm`. |

---

## 4. Data Models — `src/models/`

### 4a. `cloud_resource.py` ✅
| Symbol | Kind | Description |
|--------|------|-------------|
| `CloudProvider` | Enum | `aws`, `azure`, `gcp` |
| `ServiceCategory` | Enum | 14 values: `COMPUTE`, `SERVERLESS_COMPUTE`, `CONTAINER`, `SERVERLESS_FUNCTION`, `DATABASE`, `STORAGE`, `NETWORKING`, `AI_ML`, `ANALYTICS`, `MANAGEMENT`, `SECURITY`, `INTEGRATION`, `IOT`, `OTHER` |
| `ComputeSKU` | Pydantic | **Deprecated** — engines now use `NormalizedPriceItem` with attribute extraction helpers |
| `StorageSKU` | Pydantic | **Deprecated** — engines now use `NormalizedPriceItem` with attribute extraction helpers |

### 4b. `pricing.py` ✅
| Symbol | Kind | Description |
|--------|------|-------------|
| `PricingTier` | Enum | 8 values: `ON_DEMAND`, `SPOT`, `LOW_PRIORITY`, `RESERVED_1YR`, `RESERVED_3YR`, `SAVINGS_PLAN_1YR`, `SAVINGS_PLAN_3YR`, `DEV_TEST` |
| `NormalizedPriceItem` | Pydantic | Universal pricing row from all 3 providers. Has `monthly_cost_estimate` property (handles reservation upfront vs. hourly correctly). |
| `SKUPricing` | Pydantic | **Deprecated** — kept for backward compat only |

### 4c. `workload.py` ✅ (redesigned)
| Symbol | Kind | Description |
|--------|------|-------------|
| `EnvironmentType` | Enum | `production`, `staging`, `development`, `disaster_recovery` |
| `WorkloadTier` | Enum | `mission_critical`, `business_critical`, `non_critical` |
| `ScalingPattern` | Enum | `steady`, `bursty`, `growing`, `unpredictable`, `batch` |
| `ResourceSpec` | Pydantic | Generic resource fields covering all 14 service categories: `vcpus`, `memory_gb`, `storage_gb`, `gpu_count`, `database_engine`, `high_availability`, `cpu_request_millicores`, `memory_request_mb`, `replicas`, `network_bandwidth_gbps`, `invocations_per_month`, `avg_duration_ms`, `memory_mb` |
| `WorkloadRequirement` | Pydantic | One per logical component. Category-agnostic. Has `name`, `description`, `suggested_category`, `scaling_pattern`, `count`, `resources: ResourceSpec`, `region_affinity`, `provider_preference`, `compliance_tags`, `notes`. |
| `ComponentProfile` | Pydantic | Profiler's output for one workload. Has `resolved_category`, estimated compute/storage/iops, `requires_gpu`, `recommended_instance_families`, `rationale`. |
| `WorkloadProfile` | Pydantic | Aggregated Profiler output. Has `components: list[ComponentProfile]`, totals, `environment`, `tier`, `profiler_notes`. |
| `WorkloadRequest` | Pydantic | Top-level user submission. Has `project_name`, `environment`, `tier`, `target_providers: list[CloudProvider]`, `preferred_region`, `provider_regions: dict`, `workloads: list[WorkloadRequirement]`, `budget_monthly_usd`, `compliance_frameworks`, `raw_user_input`. |
| `VMWorkload`, `ContainerWorkload`, `StorageRequirement` | Pydantic | **Deprecated** — kept for `engines/` backward compat only |

### 4d. `conversation.py` ✅ (new)
| Symbol | Kind | Description |
|--------|------|-------------|
| `MessageRole` | Enum | `user`, `assistant`, `system` |
| `ClarificationStatus` | Enum | `pending`, `answered`, `skipped`, `inferred` |
| `ClarificationPriority` | Enum | `required`, `recommended`, `optional` |
| `ChatMessage` | Pydantic | `role`, `content`, `timestamp`, `agent_name`, `metadata` |
| `ClarificationQuestion` | Pydantic | `question_id`, `question_text`, `target_field`, `priority`, `status`, `default_value`, `user_answer`, `resolved_value` |
| `ConversationState` | Pydantic | Full session state. Properties: `pending_questions`, `has_pending_questions`, `should_continue_clarifying`. Tracks `current_turn` vs `max_clarification_turns`. |

### 4e. `recommendation.py` ✅ (redesigned)
| Symbol | Kind | Description |
|--------|------|-------------|
| `PackedNode` | Pydantic | One physical node. `node_sku: NormalizedPriceItem`, `assigned_workloads`, CPU/memory utilization %. |
| `BinPackingResult` | Pydantic | One provider's packing result. `nodes: list[PackedNode]`, `total_nodes`, `packing_efficiency_pct`, `total_monthly_cost_usd`, `algorithm_used`. |
| `ProviderCostBreakdown` | Pydantic | 6 cost categories (compute/database/storage/kubernetes/networking/serverless) + RI/SP/spot savings with %. |
| `CostComparison` | Pydantic | All-provider comparison. `cheapest_provider`, `savings_vs_most_expensive_pct`, `budget_monthly_usd`, `budget_exceeded`. |
| `ComplianceCheckResult` | Pydantic | One WAF check. `pillar`, `check_name`, `passed`, `severity`, `finding`, `recommendation`. |
| `ComplianceReport` | Pydantic | Full WAF report. `framework`, `checks[]`, `total_checks`, `passed_checks`, `compliance_score_pct`. |
| `CloudRecommendation` | Pydantic | Final pipeline output. `bin_packing_results`, `sku_selections: list[NormalizedPriceItem]`, `cost_comparison`, `compliance_report`, `recommended_provider`, `executive_summary`, `rfp_document`, `kpis`. |

### 4f. `__init__.py` ✅
Exports all of the above (90+ symbols). Both new models and deprecated legacy models re-exported.

---

## 5. Cloud Provider Adapters — `src/providers/`

| File | Status | Live-Tested | What it does |
|------|--------|-------------|-------------|
| `base_provider.py` | ✅ | N/A | Abstract `BaseCloudProvider` with 3 methods: `search_prices()`, `get_sku_prices()`, `list_regions()` |
| `azure_provider.py` | ✅ | **Yes** | Azure Retail Prices REST API (no auth). OData filter builder, autopagination, `serviceName → ServiceCategory` mapping, `ServiceCategory → serviceNames` reverse map, PricingTier resolution, Spot/LowPriority post-filtering. **VM spec enrichment**: `_to_normalized()` calls `parse_azure_vm_specs()` to inject `vcpus`, `memory_gb`, `generation` into attributes from ARM SKU name. `@observe()` + structlog. |
| `aws_provider.py` | ✅ | **Yes (6/6 checks ✅)** | AWS Pricing API via boto3. 45 `ServiceCode → ServiceCategory` mappings, 26 `region ↔ location` name mappings, OnDemand + Reserved 1yr/3yr term parsing, stringified JSON PriceList parsing. `asyncio.to_thread()` for all sync boto3 calls. |
| `gcp_provider.py` | ✅ | **Yes (6/6 checks ✅)** | GCP Cloud Billing Catalog via `google-cloud-billing` SDK. 38 `service display name → ServiceCategory` mappings. Two-step discovery (`list_services → list_skus`). `units + nanos/1e9` price conversion. `asyncio.to_thread()` for all gRPC calls. |
| `__init__.py` | ✅ | — | Re-exports all 4. |

---

## 6. Pricing Cache & Service — `src/services/`

| File | Status | What it does |
|------|--------|-------------|
| `pricing_cache.py` | ✅ | Async SQLite cache via `aiosqlite`. `price_items` table (UNIQUE on `provider+sku_id+region+tier`), `fetch_log` TTL tracking. WAL mode. `upsert()`, `query()`, `evict_stale()`, `stats()`. `@observe()` + structlog. |
| `pricing_service.py` | ✅ | Cache-transparent facade. Per-tier TTLs: spot=6 h, on-demand=24 h, reserved=168 h. Graceful degradation (stale cache on API failure). `compare_across_providers()` for FinOps agent. `force_refresh`, `evict_stale()`, `clear_cache()`, `cache_stats()`. |
| `__init__.py` | ✅ | Re-exports `PricingCache`, `PricingService`. |

**Verified**: 25 checks passed — cache miss → live fetch → store → hit → speedup (~970×) → evict → clear.

---

## 7. Orchestrator State — `src/orchestrator/`

| File | Status | What it does |
|------|--------|-------------|
| `state.py` | ✅ | `OrchestratorState` TypedDict. Append-only lists via `Annotated[list, operator.add]`: `messages`, `sized_results`, `savings_opportunities`. Last-writer-wins fields: `conversation`, `workload_request`, `workload_profile`, `cost_comparison`, `rfp_document`, `compliance_report`, `kpis`. `AgentStatus` enum, `AgentExecution` model (timing + retry tracking), `SizedWorkloadResult` (fit score, selected SKU + alternatives). `create_initial_state()` factory. |
| `graph.py` | ✅ | **247 lines. Fully implemented.** `StateGraph` with 5 nodes (clarifier, profiler, sizer, finops, rfp_writer). Conditional edge for Clarifier loop via `_should_continue_clarifying()`. Node wrapper factories inject LLM + PricingService via closures. `build_graph(llm, pricing_service)` returns compiled graph. `@observe()` + structlog throughout. |
| `__init__.py` | ✅ | Exports `OrchestratorState`, `create_initial_state`, `AgentStatus`, `AgentExecution`, `SizedWorkloadResult`. |

### State Fields per Agent

| Agent | Reads | Writes |
|-------|-------|--------|
| **Clarifier** | `messages`, `conversation` | `messages`, `conversation`, `workload_request` |
| **Profiler** | `workload_request` | `workload_profile`, `messages` |
| **Sizer** | `workload_profile` | `sized_results`, `messages` |
| **FinOps** | `sized_results` | `cost_comparison`, `recommended_provider`, `savings_opportunities`, `messages` |
| **RFP Writer** | all above | `rfp_document`, `executive_summary`, `compliance_report`, `messages` |

Append-only (reducer): `messages`, `sized_results`, `savings_opportunities`  
Last-writer-wins: `conversation`, `workload_request`, `workload_profile`, `cost_comparison`, `compliance_report`

---

## 8. Agents — `src/agents/`

| File | Status | What it does / will do |
|------|--------|----------------------|
| `clarifier.py` | ✅ | **596 lines. Fully implemented.** Multi-turn requirement refinement loop. 4 required + 4 recommended question templates. Parsing utilities: `_parse_environment`, `_parse_tier`, `_parse_providers`, `_parse_budget`, `_parse_compliance`. `_extract_workloads_from_text()` keyword-based bootstrap. `run_clarifier_node(state, llm, pricing_service)` entry point. `@observe()` + structlog throughout. **Validated: 12/12 checks passed.** |
| `profiler.py` | ✅ | **~600 lines. Fully implemented.** Takes `WorkloadRequest` → produces `WorkloadProfile` with one `ComponentProfile` per workload. Priority-ordered category resolution (AI_ML > DATABASE > CONTAINER > COMPUTE), tier-based resource multipliers, environment scaling factors, instance-family recommendation per provider, LLM-enriched rationale with heuristic fallback. `run_profiler_node(state, llm)` entry point. `@observe()` + structlog throughout. **Validated: 23/23 checks passed.** |
| `sizer.py` | ✅ | **~950 lines. Fully implemented.** Category-aware SKU selection: scored categories (COMPUTE, AI_ML) use `scoring.score_skus()`, binpacked categories (CONTAINER) use `bin_packing.pack_workloads()` with cost-efficient node selection, all others use cheapest-price selection. **Compute candidate enrichment**: GCP candidates replaced with synthetic VMs from `compose_gcp_vm_instances()`; AWS/Azure candidates filtered to hourly-billed VM SKUs only (excludes storage/network meters). `_SERVICE_NAME_MAP` (24 provider×category→service_name), `_get_region_for_provider()`, `_build_workload_requirement_for_component()`, `_select_best_by_price()`, `_size_compute_workload()`, `_size_container_workload()`, `_size_generic_workload()`, `_generate_sizer_summary()` (LLM with heuristic fallback). `run_sizer_node(state, llm, pricing_service)` entry point. `@observe()` + structlog throughout. |
| `finops.py` | ✅ | **744 lines. Fully implemented.** Groups `SizedWorkloadResult` by provider, queries RI/spot pricing per SKU, builds `ProviderCostBreakdown` per provider, assembles `CostComparison`. `_CATEGORY_TO_COST_FIELD` mapping, `_group_results_by_provider()`, `_build_provider_breakdown()`, `_generate_finops_summary()` (LLM with heuristic fallback). `run_finops_node(state, llm, pricing_service)` entry point. `@observe()` + structlog throughout. |
| `rfp_writer.py` | ✅ | **653 lines. Fully implemented.** Generates Markdown RFP with 7 sections (header, exec summary, workload summary, SKU selections, cost comparison, compliance, vendor shortlist). Uses `evaluate_compliance()` from `waf_compliance`. `_build_header_section()`, `_build_workload_summary_section()`, `_build_sku_selection_section()`, `_build_cost_comparison_section()`, `_build_compliance_section()`, `_build_vendor_shortlist_section()`, `_generate_executive_summary()` (LLM with heuristic fallback). `run_rfp_writer_node(state, llm)` entry point. `@observe()` + structlog throughout. |
| `__init__.py` | ✅ | Exists (empty re-export shell). |

---

## 9. Algorithmic Engines — `src/engines/`

> ✅ All 3 engines have been **migrated** from deprecated models (`ComputeSKU`, `VMWorkload`, `ContainerWorkload`) to `NormalizedPriceItem` and `WorkloadRequirement`. Shared attribute extraction helpers in `__init__.py`.

| File | Status | What it does |
|------|--------|-------------|
| `__init__.py` | ✅ | Shared attribute extraction helpers: `extract_vcpus()`, `extract_memory_gb()`, `extract_gpu_count()`, `extract_generation()`. Handles standardised keys + AWS-style fallbacks. |
| `vm_specs.py` | ✅ | **VM specification enrichment.** `parse_azure_vm_specs()` extracts vCPU/memory from ARM SKU names (regex parser + family→memory ratio table). `compose_gcp_vm_instances()` synthesizes 31 predefined GCP machine types (E2/N2/C3/N4 families) from per-vCPU + per-GB-RAM component pricing rates. Both enable the scoring engine to evaluate all 3 providers with comparable attributes. |
| `bin_packing.py` | ✅ | 307→313 lines. FFD + BFD algorithms. Uses `NormalizedPriceItem` (node SKU) + `WorkloadRequirement` (container workloads, via `resources.cpu_request_millicores`/`memory_request_mb`/`replicas`). Cost via `monthly_cost_estimate`. **Validated: 60/60 checks passed (shared script).** |
| `scoring.py` | ✅ | 176→182 lines. Weighted multi-criteria scorer (cost 40%, CPU fit 25%, memory fit 25%, generation 10%). Uses `NormalizedPriceItem` + `WorkloadRequirement`. Extracts specs via attribute helpers. AWS `currentGeneration` → "current"/"previous" scoring. **Validated: 60/60 checks passed (shared script).** |
| `waf_compliance.py` | ✅ | 291→303 lines. Rule-based WAF pillar checks. Now filters `request.workloads` by `ServiceCategory` (CONTAINER/COMPUTE/STORAGE) instead of deprecated `request.container_workloads`/`vm_workloads`/`storage_requirements`. **Validated: 60/60 checks passed (shared script).** |

---

## 10. API Layer — `src/api/`

| File | Status | What it does / will do |
|------|--------|----------------------|
| `dependencies.py` | ✅ | **114 lines. Fully implemented.** ASGI `lifespan()` context manager: loads settings, configures observability, creates LLM, registers AWS/Azure/GCP providers with `PricingService`, initialises cache, compiles LangGraph, stores singletons on `app.state`. Dependency providers: `get_app_settings()`, `get_llm_dep()`, `get_pricing_service()`, `get_compiled_graph()`. |
| `routes/health.py` | ✅ | **96 lines. Fully implemented.** `GET /health` (liveness — always 200). `GET /ready` (deep readiness — checks pricing service providers, LLM instance, compiled graph). |
| `routes/orchestration.py` | ✅ | **~350 lines. Fully implemented.** `POST /orchestrate` (full pipeline → JSON result). `POST /orchestrate/stream` (SSE streaming via `sse-starlette` — streams agent progress events + final result). `POST /orchestrate/clarify` (multi-turn REST clarification with in-memory session store). Custom `_json_default()` serializer handles datetime + Pydantic models in SSE events. Uses `create_initial_state()`, `graph.astream()`. |
| `routes/__init__.py` | ✅ | Exists. |
| `__init__.py` | ✅ | Exists. |

---

## 11. Application Entry Point — `src/main.py`

| Status | Notes |
|--------|-------|
| ✅ | **Rewritten.** FastAPI app with `lifespan=lifespan` (from `dependencies.py`), CORS middleware, router mount at `/api/v1`. `run()` launches uvicorn with settings. All imports verified. |

---

## 12. Tests — `tests/`

| Path | Status | Notes |
|------|--------|-------|
| `conftest.py` | ❌ | Placeholder only — no fixtures |
| `unit/` | ❌ | Empty (only `__init__.py`) |
| `integration/` | ❌ | Empty (only `__init__.py`) |

**Validation scripts** (in `scripts/` — not pytest, run manually):

| Script | Checks | Status |
|--------|--------|--------|
| `test_pricing_service.py` | 25 — full cache lifecycle + 970× speedup | ✅ Passed |
| `test_state_models.py` | 41 — all imports, enums, models, instantiation, behavior | ✅ Passed |
| `test_clarifier_agent.py` | 12 — parsing, workload extraction, question generation, state flow | ✅ Passed |
| `test_profiler_agent.py` | 23 — category resolution, resource estimation, instance families, profile assembly, node function, LLM fallback, imports, decorators | ✅ Passed |
| `test_engines.py` | 60 — attribute extraction (standardised + AWS + MiB + missing), bin-packing (FFD/BFD/empty/incomplete/AWS), scoring (fit/filter/GPU/weights/AWS), WAF compliance (all 6 pillars, category filtering, over-provisioning) | ✅ Passed |
| `test_azure_adapter.py` | 16 — live Azure API calls + assertions | ✅ Passed |
| `test_aws_adapter.py` | 6 — EC2 search, m5.xlarge SKU, reserved tiers, RDS, fields, regions | ✅ Passed |
| `test_gcp_adapter.py` | 6 — Compute Engine search, DATABASE, tiers, monthly estimate, fields, regions | ✅ Passed |
| `test_all_providers.py` | Structural — adapter imports + hierarchy + mappings | ✅ Passed |
| `test_monthly_estimate.py` | Monthly cost calculations (on-demand, reserved 1yr/3yr) | ✅ Passed |
| `explore_aws_pricing.py` | AWS API shape exploration | 🔧 Exploratory |
| `explore_gcp_pricing.py` | GCP Billing Catalog shape exploration | 🔧 Exploratory |

---

## 13. Dashboard — `dashboard/`

| Status | Notes |
|--------|-------|
| ✅ | **Fully built.** React + Vite + TypeScript + Tailwind v3 + shadcn/ui (New York style). Multi-page layout with React Router v6 (`/chat`, `/results`). Chat-first interface with SSE streaming via `@microsoft/fetch-event-source`. Recharts cost comparison charts. Markdown RFP rendering via `react-markdown`. |

### Files

| File | What it does |
|------|-------------|
| `src/types/api.ts` | TypeScript interfaces matching all backend Pydantic models (enums, request/response, SSE events) |
| `src/lib/api.ts` | API client: `orchestrate()`, `streamOrchestrate()` (SSE), `checkHealth()`, `checkReady()` |
| `src/lib/utils.ts` | shadcn `cn()` utility (clsx + tailwind-merge) |
| `src/context/PipelineContext.tsx` | React Context: messages, agent progress, result, streaming state. Actions: `startPipeline()`, `reset()` |
| `src/hooks/useHealth.ts` | Polls `GET /ready` every 30s, returns `status: healthy/degraded/offline` |
| `src/components/layout/Header.tsx` | Nav bar: logo, Chat/Results links, backend health indicator dot |
| `src/components/layout/RootLayout.tsx` | Header + `<Outlet />` wrapper |
| `src/components/chat/ChatMessage.tsx` | Message bubble: user (right/blue), assistant (left/gray + agent badge), Markdown rendering |
| `src/components/chat/ChatInput.tsx` | Textarea, project name input, send button with loading state |
| `src/components/chat/AgentProgress.tsx` | 5-step horizontal stepper: Clarifier → Profiler → Sizer → FinOps → RFP Writer |
| `src/components/chat/ChatContainer.tsx` | Orchestrates chat: scroll area, auto-scroll, agent progress bar, empty state, pipeline complete banner |
| `src/components/results/ExecutiveSummary.tsx` | Hero card: summary (Markdown), recommended provider badge, budget status |
| `src/components/results/CostComparisonTable.tsx` | Cost breakdown table by category per provider, cheapest highlighted green |
| `src/components/results/CostComparisonChart.tsx` | Recharts grouped bar charts: cost by category + pricing tier comparison |
| `src/components/results/ProviderCard.tsx` | Provider summary card: total cost, SKU list, savings opportunities |
| `src/components/results/ComplianceReport.tsx` | WAF compliance score + checks grouped by pillar with severity badges |
| `src/components/results/RfpDocument.tsx` | Markdown RFP viewer with Copy + Download buttons |
| `src/pages/ChatPage.tsx` | `/chat` route — renders ChatContainer |
| `src/pages/ResultsPage.tsx` | `/results` route — tabbed layout: Overview, Cost Analysis, Compliance, RFP Document |
| `src/pages/NotFoundPage.tsx` | 404 fallback |
| `src/App.tsx` | React Router routes: `/` → redirect `/chat`, `/chat`, `/results`, `*` → 404 |
| `src/main.tsx` | Entry point: BrowserRouter + PipelineProvider + App |
| `src/components/ui/*.tsx` | 12 shadcn/ui components: Button, Card, Badge, Progress, Input, Tabs, Table, ScrollArea, Separator, Alert, Skeleton, Avatar |

### Tech Stack

| Dependency | Version | Purpose |
|-----------|---------|--------|
| `react` + `react-dom` | ^19 | UI framework |
| `react-router-dom` | ^7 | Multi-page routing |
| `@microsoft/fetch-event-source` | ^2 | POST-based SSE streaming (native EventSource is GET-only) |
| `recharts` | ^2 | Cost comparison bar charts |
| `react-markdown` + `remark-gfm` | ^9 / ^4 | RFP Markdown rendering |
| `tailwindcss` | ^3 | Utility-first CSS |
| `@tailwindcss/typography` | ^0.5 | Prose styling for Markdown content |
| `class-variance-authority` | ^0.7 | Variant props for shadcn components |
| `clsx` + `tailwind-merge` | latest | Class name merging utilities |

### Build Verification

- `npm run build` — **0 TypeScript errors, 3 output files** (index.html, CSS, JS) ✅
- `npm run dev` — Vite dev server at `http://localhost:5173/` ✅
- Routing: `/` → redirect `/chat`, `/results` → results page, `/foo` → 404 ✅

---

## 14. Build Order — Remaining Work

Items in recommended implementation order:

### Phase 1 — Complete the Agent Pipeline ✅ DONE

| # | Task | Status |
|---|------|--------|
| ~~1~~ | ~~**Profiler agent**~~ (`src/agents/profiler.py`) | ✅ ~600 lines, 23/23 checks |
| ~~2~~ | ~~**Migrate engines**~~ (`bin_packing.py`, `scoring.py`, `waf_compliance.py`) | ✅ 60/60 checks |
| ~~3~~ | ~~**Sizer agent**~~ (`src/agents/sizer.py`) | ✅ 920 lines, imports verified |
| ~~4~~ | ~~**FinOps agent**~~ (`src/agents/finops.py`) | ✅ 744 lines, imports verified |
| ~~5~~ | ~~**RFP Writer agent**~~ (`src/agents/rfp_writer.py`) | ✅ 653 lines, imports verified |

### Phase 2 — Wire the Orchestrator ✅ DONE

| # | Task | Status |
|---|------|--------|
| ~~6~~ | ~~**LangGraph graph**~~ (`src/orchestrator/graph.py`) | ✅ 247 lines, imports verified |
| ~~7~~ | ~~**API dependencies**~~ (`src/api/dependencies.py`) | ✅ 114 lines, imports verified |
| ~~8~~ | ~~**API routes**~~ (`src/api/routes/health.py`, `orchestration.py`) | ✅ 96 + 208 lines, imports verified |
| ~~9~~ | ~~**Fix `src/main.py`**~~ | ✅ Rewritten with lifespan |

### Phase 3 — Frontend ✅ DONE

| # | Task | Status |
|---|------|--------|
| ~~10~~ | ~~**React scaffold**~~ (`dashboard/`) | ✅ Vite + TS + Tailwind v3 + shadcn/ui |
| ~~11~~ | ~~**Chat UI + SSE streaming**~~ | ✅ ChatContainer + AgentProgress + `@microsoft/fetch-event-source` |
| ~~12~~ | ~~**Cost comparison + recommendation display**~~ | ✅ Recharts charts + tables + compliance + RFP viewer |

### Phase 4 — Testing & Hardening

| # | Task |
|---|------|
| 13 | `tests/conftest.py` — shared pytest fixtures |
| 14 | `tests/unit/` — unit tests for all agents + engines |
| 15 | `tests/integration/` — end-to-end workflow tests |

### Phase 5 — Quality Gaps (Identified 17 Apr 2026)

> **Context**: First full pipeline run produced a subpar RFP — wrong SKU selections,
> unrealistic costs ($18-47/mo for production K8s+DB+S3), thin 4,690-char output
> vs. the 30-60 page reference RFPs (Mass Tech Collaborative, Cal Fire/Presidio).

#### 5a. Clarifier Agent — Missing Depth ❌

| Gap | Description | Severity |
|-----|-------------|----------|
| **Application architecture** | Doesn't ask microservice count, traffic patterns, data flow, API gateway needs | CRITICAL |
| **Performance requirements** | No latency targets (p50/p95/p99), throughput (req/sec), concurrent users | CRITICAL |
| **Availability / SLA** | Only `WorkloadTier` enum — no RPO/RTO, uptime target (99.9% vs 99.99%) | HIGH |
| **Security architecture** | Only `compliance_frameworks` keyword — no VPC isolation, encryption standards, IAM model, WAF rules | HIGH |
| **Data requirements** | Defaults `storage_gb=1000` — no growth rate, retention policy, backup frequency, data classification | HIGH |
| **Integration requirements** | Nothing — no third-party APIs, SSO, CI/CD, monitoring stack questions | MEDIUM |
| **Migration timeline** | Nothing — no lift-and-shift vs. re-architect, downtime windows, legacy deps | MEDIUM |
| **Disaster recovery** | `EnvironmentType.DR` exists but never queried — no DR strategy, cross-region replication | HIGH |
| **Cost model preferences** | Only `budget_monthly_usd` — no reserved/on-demand preference, commitment terms, spot tolerance | MEDIUM |

**Root cause**: `_extract_workloads_from_text()` is purely keyword-based. "3 microservices on K8s" creates ONE `WorkloadRequirement` with `replicas=3` instead of 3 separate container workloads.

#### 5b. Profiler Agent — Wrong Category Resolution ❌

| Gap | Description | Severity |
|-----|-------------|----------|
| **K8s classified as AI_ML** | Priority-ordered `_CATEGORY_PRIORITY` checks AI_ML first; LLM enrichment may cascade into GPU defaults | CRITICAL |
| **No component decomposition** | "3 microservices" → should produce 3 `ComponentProfile`s (api-gw, biz-logic, worker), not 1 | CRITICAL |
| **Resource over-estimation** | K8s got 9 vCPU, 76.8 GB RAM, GPU — should be ~0.5-2 vCPU, 1-4 GB per microservice | HIGH |
| **No K8s cluster-level costs** | Doesn't model EKS/AKS/GKE management fee ($72-100/mo), node pool sizing, or cluster networking | HIGH |

#### 5c. Sizer Agent — Wrong SKU Selection ❌

| Gap | Description | Severity |
|-----|-------------|----------|
| **SageMaker for K8s** | `_SERVICE_NAME_MAP[("aws", AI_ML)]` = `"AmazonSageMaker"` — queried because Profiler mis-categorized | CRITICAL |
| **Azure ML "PB" for containers** | Same category mismatch | CRITICAL |
| **MariaDB for PostgreSQL** | `db.t2.micro` MariaDB selected — database engine not propagated from clarifier | HIGH |
| **e2-micro for production** | 1 vCPU / 1 GB instance for a 9-vCPU workload — scoring/filtering broken | HIGH |
| **Missing K8s node pool modeling** | Should size node pools (3× m5.large) not single instances | HIGH |
| **Missing ancillary costs** | No load balancer, NAT gateway, data transfer, container registry, monitoring | HIGH |

**Expected correct sizing**:
- K8s: EKS + 3× m5.large ($70-90/node/mo) | AKS + 3× Standard_D4s_v3 | GKE + 3× n2-standard-4
- DB: RDS PostgreSQL db.m5.large ($130-180/mo) | Azure PostgreSQL Flex ($130/mo) | Cloud SQL PostgreSQL ($100/mo)
- S3: S3 Standard ($23/TB/mo) | Blob Hot ($18/TB/mo) | GCS Standard ($20/TB/mo)
- **Expected total**: $500-1,500/mo — NOT $18-47/mo

#### 5d. FinOps Agent — Garbage In / Garbage Out ❌

| Gap | Description | Severity |
|-----|-------------|----------|
| **Costs unrealistically low** | $18-47/mo for production — caused by wrong SKUs from Sizer | CRITICAL |
| **No ancillary cost modeling** | Missing: load balancer ($20-50), NAT gateway ($32-100), data transfer ($50-200), monitoring ($30-100), DNS ($5-10), backup ($20-50) | HIGH |
| **No reserved/spot pricing** | Savings table shows "N/A" for all providers — not querying RI/spot variants | HIGH |
| **No multi-year TCO** | Only monthly + annual — no 3-year or 5-year projection | MEDIUM |

#### 5e. RFP Writer — Thin Output ❌

Current: ~4,690 characters (≈2 pages). Reference RFPs: 30-60 pages.

| Missing Section | In Reference RFPs | In Our Output | Severity |
|-----------------|-------------------|---------------|----------|
| **Cover letter** | Formal addressee, RFP reference number | None | MEDIUM |
| **Table of contents** | Numbered, multi-level | None | MEDIUM |
| **Architecture description** | Topology diagrams, data flow, network architecture | None | CRITICAL |
| **Detailed tech specs** | Component-by-component: version, config, capacity | Basic SKU/cost/fit table | HIGH |
| **SLA / uptime guarantees** | 99.9%/99.99% targets, penalty clauses | None | HIGH |
| **Security architecture** | Encryption (AES-256, TLS 1.3), IAM, network segmentation, DDoS, WAF | Basic WAF compliance check | HIGH |
| **Migration / implementation plan** | Phased plan (4 phases), milestones, dates | None | HIGH |
| **Staffing / support model** | Named roles, FTE allocation, escalation matrix | None | MEDIUM |
| **Disaster recovery / backup** | DR strategy, RPO/RTO, failover procedures, backup schedule | None | HIGH |
| **Multi-year cost projection** | Per-component, per-phase, per-year, 3-5 year TCO | Monthly/annual per provider | HIGH |
| **Compliance certifications** | SOC2, ISO 27001, FedRAMP, HIPAA evidence | WAF pillar check only | HIGH |
| **Assumptions & exclusions** | Explicit scope boundaries | None | MEDIUM |
| **Vendor qualifications** | Past performance, references, partnerships | Rank by cost only | MEDIUM |

#### 5f. LangFuse Tracing — Not Working ❌

| Issue | Description | Severity |
|-------|-------------|----------|
| **Wrong env var name** | `.env` has `LANGFUSE_BASE_URL` but settings reads `LANGFUSE_HOST` (prefix `LANGFUSE_` + field `host`). SDK also expects `LANGFUSE_HOST`. | CRITICAL |
| **No `flush()` at pipeline end** | SDK batches events async — if process exits before flush, traces are lost. No shutdown hook in `graph.py` or route handler. | HIGH |
| **No parent trace context** | Each `@observe()` creates independent traces. No `langfuse_context.update_current_trace()` at top-level to nest spans. | HIGH |
| **No `session_id` / `user_id`** | Cannot correlate traces with request_id or user sessions | MEDIUM |
| **Double nesting** | Graph node wrappers have `@observe()` AND inner `run_*_node` functions also have `@observe()` — confusing trace hierarchy | LOW |

#### 5g. Frontend — Runtime Errors ❌ (FIXED: ProviderCard)

| Issue | Description | Severity | Status |
|-------|-------------|----------|--------|
| **ProviderCard crash** | `sku.monthly_cost_estimate` is a Python `@property` — not serialized by `model_dump()`. Frontend gets `undefined`, calls `toLocaleString()` on it → TypeError crash | HIGH | ✅ FIXED — added `skuMonthlyCost()` helper that falls back to `unit_price × 730` for hourly or `unit_price` for monthly |
| **`monthly_cost_estimate` missing from API** | `NormalizedPriceItem.monthly_cost_estimate` is a `@property` that Pydantic v2 excludes from `model_dump(mode="json")`. Need to either add `@computed_field` decorator or serialize it explicitly in the API layer. | HIGH | Backend fix still needed |
| **No error boundaries** | React tree has no error boundaries — any component crash kills the entire page | MEDIUM | Not yet fixed |

---

## 15. Key Invariants (Never Break These)

1. **Agents use `BaseChatModel` only** — never import `ChatBedrockConverse` or `ChatGoogleGenerativeAI` in agent files.
2. **Agents call `PricingService`** — never call `AzurePricingProvider` / `AWSPricingProvider` / `GCPPricingProvider` directly.
3. **All LangGraph nodes are decorated with `@observe()`** — every agent step traced in LangFuse.
4. **Every public function uses `structlog`** — no `print()`, no stdlib `logging`.
5. **`NormalizedPriceItem` is the only pricing model** agents and engines see — `ComputeSKU`/`StorageSKU` are engines-internal deprecated.
6. **State lists are append-only** — never replace `messages`, `sized_results`, or `savings_opportunities`; let the LangGraph reducer merge.
7. **`uv sync --all-extras`** after any `pyproject.toml` change.
