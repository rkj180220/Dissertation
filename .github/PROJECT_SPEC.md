# Cloud Orchestrator IDSS — Project Specification

> **Author**: Ramkumar J · BITS ID: 2024MT03027 · M.Tech Cloud Computing, BITS Pilani WILP
> **Supervisor**: Rajkumar Sakthibalan (Presidio Solutions, Chennai)
> **Additional Examiner**: Santhosh Kirubakaran
> **Last Updated**: 7 April 2026
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
| `azure_provider.py` | ✅ | **Yes** | Azure Retail Prices REST API (no auth). OData filter builder, autopagination, `serviceName → ServiceCategory` mapping, `ServiceCategory → serviceNames` reverse map, PricingTier resolution, Spot/LowPriority post-filtering. `@observe()` + structlog. |
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
| `sizer.py` | ✅ | **920 lines. Fully implemented.** Category-aware SKU selection: scored categories (COMPUTE, AI_ML) use `scoring.score_skus()`, binpacked categories (CONTAINER) use `bin_packing.pack_workloads()` with cost-efficient node selection, all others use cheapest-price selection. `_SERVICE_NAME_MAP` (24 provider×category→service_name), `_get_region_for_provider()`, `_build_workload_requirement_for_component()`, `_select_best_by_price()`, `_size_compute_workload()`, `_size_container_workload()`, `_size_generic_workload()`, `_generate_sizer_summary()` (LLM with heuristic fallback). `run_sizer_node(state, llm, pricing_service)` entry point. `@observe()` + structlog throughout. |
| `finops.py` | ✅ | **744 lines. Fully implemented.** Groups `SizedWorkloadResult` by provider, queries RI/spot pricing per SKU, builds `ProviderCostBreakdown` per provider, assembles `CostComparison`. `_CATEGORY_TO_COST_FIELD` mapping, `_group_results_by_provider()`, `_build_provider_breakdown()`, `_generate_finops_summary()` (LLM with heuristic fallback). `run_finops_node(state, llm, pricing_service)` entry point. `@observe()` + structlog throughout. |
| `rfp_writer.py` | ✅ | **653 lines. Fully implemented.** Generates Markdown RFP with 7 sections (header, exec summary, workload summary, SKU selections, cost comparison, compliance, vendor shortlist). Uses `evaluate_compliance()` from `waf_compliance`. `_build_header_section()`, `_build_workload_summary_section()`, `_build_sku_selection_section()`, `_build_cost_comparison_section()`, `_build_compliance_section()`, `_build_vendor_shortlist_section()`, `_generate_executive_summary()` (LLM with heuristic fallback). `run_rfp_writer_node(state, llm)` entry point. `@observe()` + structlog throughout. |
| `__init__.py` | ✅ | Exists (empty re-export shell). |

---

## 9. Algorithmic Engines — `src/engines/`

> ✅ All 3 engines have been **migrated** from deprecated models (`ComputeSKU`, `VMWorkload`, `ContainerWorkload`) to `NormalizedPriceItem` and `WorkloadRequirement`. Shared attribute extraction helpers in `__init__.py`.

| File | Status | What it does |
|------|--------|-------------|
| `__init__.py` | ✅ | Shared attribute extraction helpers: `extract_vcpus()`, `extract_memory_gb()`, `extract_gpu_count()`, `extract_generation()`. Handles standardised keys + AWS-style fallbacks. |
| `bin_packing.py` | ✅ | 307→313 lines. FFD + BFD algorithms. Uses `NormalizedPriceItem` (node SKU) + `WorkloadRequirement` (container workloads, via `resources.cpu_request_millicores`/`memory_request_mb`/`replicas`). Cost via `monthly_cost_estimate`. **Validated: 60/60 checks passed (shared script).** |
| `scoring.py` | ✅ | 176→182 lines. Weighted multi-criteria scorer (cost 40%, CPU fit 25%, memory fit 25%, generation 10%). Uses `NormalizedPriceItem` + `WorkloadRequirement`. Extracts specs via attribute helpers. AWS `currentGeneration` → "current"/"previous" scoring. **Validated: 60/60 checks passed (shared script).** |
| `waf_compliance.py` | ✅ | 291→303 lines. Rule-based WAF pillar checks. Now filters `request.workloads` by `ServiceCategory` (CONTAINER/COMPUTE/STORAGE) instead of deprecated `request.container_workloads`/`vm_workloads`/`storage_requirements`. **Validated: 60/60 checks passed (shared script).** |

---

## 10. API Layer — `src/api/`

| File | Status | What it does / will do |
|------|--------|----------------------|
| `dependencies.py` | ✅ | **114 lines. Fully implemented.** ASGI `lifespan()` context manager: loads settings, configures observability, creates LLM, registers AWS/Azure/GCP providers with `PricingService`, initialises cache, compiles LangGraph, stores singletons on `app.state`. Dependency providers: `get_app_settings()`, `get_llm_dep()`, `get_pricing_service()`, `get_compiled_graph()`. |
| `routes/health.py` | ✅ | **96 lines. Fully implemented.** `GET /health` (liveness — always 200). `GET /ready` (deep readiness — checks pricing service providers, LLM instance, compiled graph). |
| `routes/orchestration.py` | ✅ | **208 lines. Fully implemented.** `POST /orchestrate` (full pipeline → JSON result). `POST /orchestrate/stream` (SSE streaming via `sse-starlette` — streams agent progress events + final result). Uses `create_initial_state()`, `graph.ainvoke()`. |
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
| ❌ | Empty directory. Planned: React + Vite + TypeScript + Tailwind + shadcn/ui. Chat-first interface. Connects to FastAPI SSE endpoint. Displays agent progress, cost comparison tables, recommendation cards. |

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

### Phase 3 — Frontend

| # | Task | Depends on |
|---|------|-----------|
| 10 | **React scaffold** (`dashboard/`) | API routes ✅ |
| 11 | **Chat UI + SSE streaming** | Scaffold ✅ |
| 12 | **Cost comparison + recommendation display** | Chat UI ✅ |

### Phase 4 — Testing & Hardening

| # | Task |
|---|------|
| 13 | `tests/conftest.py` — shared pytest fixtures |
| 14 | `tests/unit/` — unit tests for all agents + engines |
| 15 | `tests/integration/` — end-to-end workflow tests |

---

## 15. Key Invariants (Never Break These)

1. **Agents use `BaseChatModel` only** — never import `ChatBedrockConverse` or `ChatGoogleGenerativeAI` in agent files.
2. **Agents call `PricingService`** — never call `AzurePricingProvider` / `AWSPricingProvider` / `GCPPricingProvider` directly.
3. **All LangGraph nodes are decorated with `@observe()`** — every agent step traced in LangFuse.
4. **Every public function uses `structlog`** — no `print()`, no stdlib `logging`.
5. **`NormalizedPriceItem` is the only pricing model** agents and engines see — `ComputeSKU`/`StorageSKU` are engines-internal deprecated.
6. **State lists are append-only** — never replace `messages`, `sized_results`, or `savings_opportunities`; let the LangGraph reducer merge.
7. **`uv sync --all-extras`** after any `pyproject.toml` change.
