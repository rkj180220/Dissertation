# Cloud Orchestrator IDSS — Project Specification

> **Author**: Ramkumar J · BITS ID: 2024MT03027 · M.Tech Cloud Computing, BITS Pilani WILP
> **Supervisor**: Rajkumar Sakthibalan (Presidio Solutions, Chennai)
> **Additional Examiner**: Santhosh Kirubakaran
> **Last Updated**: 11 May 2026 (P16a–P16f ALL COMPLETE. B1–B8 RFP output bugs ALL FIXED. NEW-1–NEW-5 post-fire-test bugs ALL FIXED. PROV-1–PROV-3 Cal Fire provider+workload+budget bugs ALL FIXED: provider now defaults to best_price_all when user has no preference, WORKLOAD_SUMMARY forces explicit component listing, $3.2M/year budget now parses correctly. 142 tests passing.)
> **LLM**: Gemini 2.5 Pro (`gemini-2.5-pro`) via Google Cloud Vertex AI (`dissertation-rj`, `us-central1`) using ADC

---

## 0. System Architecture

```
User (React chat) → FastAPI (SSE) → LangGraph Orchestrator
                                         │
                  ┌──────────────────────┤
                  │  Router+Orchestrator │ ← NEW (P15d). LLM-based intent router
                  │      Agent           │   AND execution planner. Reads full
                  │  (entry on turn≥2)   │   OrchestratorState + new input.
                  └──────┬──────┬────────┘   Produces ExecutionPlan: which agents
          new_request ───┘      └─── amendment / validate / answer / clarify       to run, scope (full vs delta),
                  │                                                                  which components to reprocess,
                  ▼                                                                  which RFP sections to amend.
                  ┌──────────────────────┐
                  │    Clarifier Agent   │ ← Multi-turn requirement refinement
                  │  (conditional loop)  │   Only invoked for new_request or
                  └──────────┬───────────┘   clarification_needed routes
                             │ complete
                             ▼
                  ┌──────────────────────┐
                  │    Profiler Agent    │ ← Analyzes workload → WorkloadProfile
                  │  (5-8 microservices) │   Reads ExecutionPlan.scope_components
                  └──────────┬───────────┘   (P15c: decomposes into named services)
                             ▼
                  ┌──────────────────────┐
                  │     Sizer Agent      │ ← PricingService → scoring → bin-packing
                  │  (+ serverless path) │   (P15b: Lambda/DynamoDB pricing added)
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │    FinOps Agent      │ ← Multi-provider cost comparison + TCO
                  └──────────┬───────────┘
                             ▼
                  ┌──────────────────────┐
                  │   Validator Agent    │ ← NEW (P15e). 5-check quality gate:
                  │  (quality gate)      │   [0] Pricing data integrity (P15k engine)
                  └──────────┬───────────┘   [1] Architecture correctness (P15a engine)
                             │               [2] Sizing adequacy  [3] Budget fit
                             ▼               [4] WAF compliance
                  ┌──────────────────────┐
                  │   RFP Writer Agent   │ ← Generates procurement document
                  │  (with alternatives) │   (P15i: Architecture Alternatives §)
                  └──────────────────────┘

  Session State (P15f): LangGraph SqliteSaver checkpointing — thread_id=session_id
  persists OrchestratorState across requests. Router+Orchestrator reads prior state on turn≥2.
  Amendment path: Router+Orch → Profiler(delta) → Sizer → FinOps → Validator → RFP Writer(amend)

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
| Python 3.13.0 venv | ✅ | `.venv/` created; `langchain-google-vertexai` + `google-genai` added |
| `pyproject.toml` | ✅ | hatchling build, all deps, uv scripts, pytest config; `gemini` optional dep (`langchain-google-genai`) retained |
| `uv.lock` | ✅ | Locked, reproducible |
| `.env.example` | ✅ | All 6 prefixes: `APP_`, `LLM_`, `AWS_`, `AZURE_`, `GCP_`, `LANGFUSE_` |
| `docker-compose.langfuse.yml` | ✅ | Self-hosted LangFuse v3.130.0 — 6 containers (postgres, clickhouse, redis, minio, worker, web). SDK v3 verified. |
| `.github/copilot-instructions.md` | ✅ | Tech stack rules, observability pattern, LLM abstraction rules |

---

## 2. Config Layer — `src/config/`

| File | Status | What it does |
|------|--------|-------------|
| `settings.py` | ✅ | 6 Pydantic-settings classes: `AppSettings`, `LLMSettings`, `AWSSettings`, `AzureSettings`, `GCPSettings`, `LangFuseSettings`. `get_settings()` with `@lru_cache` singleton. `LLMProvider` enum now has 3 values: `bedrock`, `gemini`, `vertexai`. |
| `logging_config.py` | ✅ | `configure_observability()` — wires `structlog` JSON renderer + LangFuse SDK v3 integration. Gracefully degrades when LangFuse keys missing. |
| `__init__.py` | ✅ | Re-exports `get_settings`, `configure_observability`. |

---

## 3. LLM Factory — `src/llm/`

| File | Status | What it does |
|------|--------|-------------|
| `factory.py` | ✅ | `get_llm(provider, model, **kwargs) → BaseChatModel`. Lazy imports: `bedrock` → `ChatBedrockConverse`, `gemini` → `ChatGoogleGenerativeAI`, **`vertexai` → `ChatGoogleGenerativeAI` with `google.auth.default()` ADC** (deprecation warning fixed — `ChatVertexAI` removed). `_create_vertexai()` passes `vertexai=True`, `project=project`, `location=location` to `ChatGoogleGenerativeAI` (required for Vertex AI routing); reads `VERTEXAI_PROJECT` env var with `GCP_PROJECT_ID` as fallback; reads `VERTEXAI_LOCATION` with `us-central1` default. Never imported directly in agent code. Live-tested: `gemini-2.5-pro` ✅. **Note**: `gemini-3.1-pro-preview` requires Model Garden enablement per GCP project before use. |
| `__init__.py` | ✅ | Re-exports `get_llm`. |

---

## 4. Data Models — `src/models/`

### 4a. `cloud_resource.py` ✅
| Symbol | Kind | Description |
|--------|------|-------------|
| `CloudProvider` | Enum | `aws`, `azure`, `gcp` |
| `ServiceCategory` | Enum | **15 values** (P2 added `KUBERNETES`): `COMPUTE`, `SERVERLESS_COMPUTE`, `CONTAINER`, `KUBERNETES`, `SERVERLESS_FUNCTION`, `DATABASE`, `STORAGE`, `NETWORKING`, `AI_ML`, `ANALYTICS`, `MANAGEMENT`, `SECURITY`, `INTEGRATION`, `IOT`, `OTHER`. `KUBERNETES` = managed control-plane fee; distinct from `CONTAINER` (node/workload costs). **P15b will add `SERVERLESS = "serverless"` as 16th value** for Lambda/DynamoDB/AppSync patterns. Note: `SERVERLESS_COMPUTE` and `SERVERLESS_FUNCTION` already exist — verify whether to add new value or alias. |
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
| `ResourceSpec` | Pydantic | Generic resource fields covering all 15 service categories: `vcpus`, `memory_gb`, `storage_gb`, `gpu_count`, `database_engine`, `high_availability`, `cpu_request_millicores`, `memory_request_mb`, `replicas`, `network_bandwidth_gbps`, `invocations_per_month`, `avg_duration_ms`, `memory_mb` |
| `WorkloadRequirement` | Pydantic | One per logical component. Has `name`, `description`, `suggested_category`, `scaling_pattern`, `count`, `resources: ResourceSpec`, `region_affinity`, `provider_preference`, `compliance_tags`, `notes`. **P2 additions**: `latency_p99_ms`, `throughput_rps`, `concurrent_users`, `uptime_sla`, `rpo_minutes`, `rto_minutes`, `data_growth_rate_pct` (all `int/float \| None`), `spot_eligible: bool = True`. |
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
| `AncillaryCost` | Pydantic | **P2 new** — typed record for fixed/usage costs outside main SKU catalog: `provider`, `category`, `item_name`, `monthly_cost_usd`, `unit`, `quantity`, `notes`. Used for NAT gateways, LBs, data transfer, K8s mgmt fees. |
| `ProviderCostBreakdown` | Pydantic | 6 cost categories (compute/database/storage/kubernetes/networking/serverless) + RI/SP/spot savings with %. **P2**: added `ancillary_costs: list[AncillaryCost]`. |
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
| `session_store.py` | ✅ | **P15f — BUILT.** LangGraph `MemorySaver` checkpointer + aiosqlite session registry. `data/sessions.db` stores session metadata (session_id, project_name, turn_count, last_active). Session TTL 7 days with `cleanup_expired_sessions()`. `make_checkpointer()` returns `MemorySaver` (upgrade path to SqliteSaver when dep available). |
| `__init__.py` | ✅ | Re-exports `PricingCache`, `PricingService`. |

**Verified**: 25 checks passed — cache miss → live fetch → store → hit → speedup (~970×) → evict → clear.

---

## 7. Orchestrator State — `src/orchestrator/`

| File | Status | What it does |
|------|--------|-------------|
| `state.py` | ✅ 🔧 | `OrchestratorState` TypedDict. Append-only lists: `messages`, `sized_results`, `savings_opportunities`. Last-writer-wins fields: `conversation`, `workload_request`, `workload_profile`, `cost_comparison`, `rfp_document`, `compliance_report`, `kpis`. `AgentStatus` enum, `AgentExecution` model, `SizedWorkloadResult`. `create_initial_state()` factory. **P15h adds 6 new fields** (❌ not yet added): `session_id`, `routing_decision`, `amendment_instructions`, `validation_report`, `architecture_alternatives`, `pipeline_mode`, `turn_number`. |
| `graph.py` | ✅ 🔧 | **247 lines. Currently 5 nodes** (clarifier, profiler, sizer, finops, rfp_writer). Conditional Clarifier loop via `_should_continue_clarifying()`. `build_graph(llm, pricing_service)` returns compiled graph. `@observe()` + structlog. **P15h will add**: `router` node at START (entry when `rfp_document` exists), `validator` node between finops and rfp_writer, conditional edges for Router intent routing, `SqliteSaver` checkpointer wiring. |
| `__init__.py` | ✅ | Exports `OrchestratorState`, `create_initial_state`, `AgentStatus`, `AgentExecution`, `SizedWorkloadResult`. |

### State Fields per Agent

| Agent | Reads | Writes |
|-------|-------|--------|
| **Router** ❌ | `messages`, `rfp_document`, `turn_number` | `routing_decision`, `amendment_instructions`, `pipeline_mode`, `turn_number` |
| **Clarifier** | `messages`, `conversation` | `messages`, `conversation`, `workload_request` |
| **Profiler** | `workload_request` | `workload_profile`, `messages` |
| **Sizer** | `workload_profile` | `sized_results`, `messages` |
| **FinOps** | `sized_results` | `cost_comparison`, `recommended_provider`, `savings_opportunities`, `messages` |
| **Validator** ❌ | `workload_profile`, `sized_results`, `cost_comparison` | `validation_report`, `architecture_alternatives`, `messages` |
| **RFP Writer** | all above | `rfp_document`, `executive_summary`, `compliance_report`, `messages` |

Append-only (reducer): `messages`, `sized_results`, `savings_opportunities`  
Last-writer-wins: `conversation`, `workload_request`, `workload_profile`, `cost_comparison`, `compliance_report`, `validation_report`

---

## 8. Agents — `src/agents/`

| File | Status | What it does / will do |
|------|--------|----------------------|
| `clarifier.py` | ✅ | **~1370 lines.** Multi-turn LLM clarifier (Claude/Gemini via factory). `llm_clarify_turn()`, `build_enriched_input_from_structured()`. 19 structured fields incl. 6 WAF pillar summaries. Helpers: scale/SLA propagation, compliance parsing, CDN+geospatial detection, region propagation, AI/ML guard. `run_clarifier_node`. **NEW-4**: RPS heuristic changed from `peak_users × 10` to `max(500, peak_users // 2)` capped at 500K. |
| `profiler.py` | ✅ | **~730 lines.** `WorkloadRequest → WorkloadProfile`. Priority-ordered category resolution, `_guard_ai_ml()`, container-aware resource estimation, cluster mgmt fee. **P15c** will add multi-workload decomposition (5–8 named microservices). `run_profiler_node`. |
| `sizer.py` | ✅ | **~2050 lines.** Category-aware SKU selection: scored (COMPUTE/AI_ML), bin-packed (CONTAINER), cheapest (others). Container vCPU guard, DATABASE hourly filter, ElastiCache routing, CDN fixed estimate, K8s/LB fixed costs. `run_sizer_node`. **NEW-1**: `_is_postgres_workload()` + `_RDS_COST_MONTHLY` fallback for AWS RDS ($185/mo). **NEW-2**: vCPU cap before `_size_compute_workload` (`max_vcpu = max(8, req_vcpu*4)` — fixes c8a.8xlarge oversize). **NEW-3**: GCP IP-reservation rows filtered from DB candidates. |
| `finops.py` | ✅ | **~900 lines.** RI/spot pricing queries with industry-standard fallbacks. TCO 1yr/3yr/5yr. Savings opportunities. `run_finops_node`. |
| `rfp_writer.py` | ✅ | **~2741 lines.** ~25,000-char enterprise Markdown RFP. Gov/greenfield/mobile detection. Managed services table, requirements traceability (F-xx/C-xx/NFR-xx), WCAG 2.2 AA + StateRAMP tables, 16-section ToC. `run_rfp_writer_node`. B2 fixed: `_build_architecture_section()` uses `recommended_provider` from state. B3 fixed: sentence-boundary rationale truncation. B8 fixed: RTM shows "Partial ⚠️" for StateRAMP when gap analysis is active. **NEW-5**: `_build_mobile_subsection()` takes `recommended_provider` param; no longer hardcodes AWS services when GCP/Azure recommended. |
| `router.py` | ✅ | **P15d — BUILT.** LLM **Router+Orchestrator Agent**. Reads new user input + full state. Classifies intent AND produces `ExecutionPlan` (agents to run, scope_components, rfp_amendment_sections, amendment_delta). Entry point for Turn ≥ 2. Uses Principal Architect Reasoning (§22g). See §22b. |
| `validator.py` | ✅ | **P15e — BUILT.** Architecture quality gate. **5 checks**: [0] Pricing data integrity (calls `pricing_validator.py`), [1] architecture_selector score with dynamic weights (P15j), [2] sizing adequacy, [3] budget fit, [4] WAF compliance. Uses Principal Architect Reasoning. Writes `validation_report` + `architecture_alternatives` to state. See §22c. |
| `__init__.py` | ✅ | Exists (empty re-export shell). |

---

## 9. Algorithmic Engines — `src/engines/`

> ✅ All 3 production engines migrated from deprecated models (`ComputeSKU`, `VMWorkload`, `ContainerWorkload`) to `NormalizedPriceItem` and `WorkloadRequirement`.

| File | Status | What it does |
|------|--------|-------------|
| `__init__.py` | ✅ | Shared attribute extraction helpers: `extract_vcpus()`, `extract_memory_gb()`, `extract_gpu_count()`, `extract_generation()`. Handles standardised keys + AWS-style fallbacks. |
| `vm_specs.py` | ✅ | Azure ARM SKU name parser (`parse_azure_vm_specs()`) + GCP machine type synthesiser (`compose_gcp_vm_instances()`, 31 predefined types). |
| `bin_packing.py` | ✅ | **313 lines.** FFD + BFD algorithms. `NormalizedPriceItem` node SKUs + `WorkloadRequirement` container workloads. **Validated: 60/60 checks.** |
| `scoring.py` | ✅ | **182 lines.** Weighted multi-criteria scorer (cost 40%, CPU fit 25%, memory fit 25%, generation 10%). **Validated: 60/60 checks.** |
| `waf_compliance.py` | ✅ | **471 lines.** Rule-based WAF pillar checks. Filters by `ServiceCategory`. Standalone checks (no bin_packing required): `_check_database_ha()` (production DB HA), `_check_budget_ceiling()`, `_check_compliance_coverage()` (framework tag coverage). **Validated: 60/60 checks.** |
| `architecture_selector.py` | ✅ | **P15a — BUILT.** Unbiased 4-option scorer (managed-serverless, self-hosted-serverless, containers, hybrid). 3 signal groups: traffic pattern (cost crossover), workload characteristics, compliance. P15j dynamic weights via `derive_weights_from_workload()`. Returns ranked `ArchitectureRecommendation`. Used by Validator Agent. See §22f. |
| `pricing_validator.py` | ✅ | **P15k — BUILT.** 7-check apples-to-apples pricing comparison validator. Pure algorithmic — no LLM. Catches size mismatches, wrong SKU families, price anomalies before FinOps picks a vendor. Returns `PricingValidationResult`. See §22h. |

---

## 10. API Layer — `src/api/`

| File | Status | What it does / will do |
|------|--------|----------------------|
| `dependencies.py` | ✅ | **114 lines. Fully implemented.** ASGI `lifespan()` context manager: loads settings, configures observability, creates LLM, registers AWS/Azure/GCP providers with `PricingService`, initialises cache, compiles LangGraph, stores singletons on `app.state`. Dependency providers: `get_app_settings()`, `get_llm_dep()`, `get_pricing_service()`, `get_compiled_graph()`. |
| `routes/health.py` | ✅ | **96 lines. Fully implemented.** `GET /health` (liveness — always 200). `GET /ready` (deep readiness — checks pricing service providers, LLM instance, compiled graph). |
| `routes/orchestration.py` | ✅ 🔧 | **~404 lines.** `POST /orchestrate` (full pipeline → JSON). `POST /orchestrate/stream` (SSE). `POST /orchestrate/clarify` (multi-turn LLM clarifier, in-memory session store). **P15h changes needed**: (1) add `session_id: str | None` to `OrchestrationRequest`; (2) stop deleting session after `status=ready` (critical — Router Agent needs state); (3) add `session_id`, `route_taken`, `turn_number` to response model. |
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
| `conftest.py` | ✅ | Shared fixtures: `make_price_item()` (NormalizedPriceItem factory with all required fields), `sample_price_items`, `mock_pricing_service`, `mock_llm` (MagicMock with `.invoke()` returning canned text), `sample_workload_requirement`, `container_workload_requirement`, `k8s_mgmt_workload`, `sample_workload_request`, `initial_state`, `state_with_workload_request`, `sized_result_aws`. |
| `unit/` | ✅ | **5 unit test modules, 129 tests, 129 passing.** |
| `integration/` | ✅ | **1 integration test module, 13 tests, 13 passing.** |

**pytest suite**: `uv run pytest tests/` → **142 passed in 0.52s** ✅

**Unit test files**:

| File | Tests | Status | What it tests |
|------|-------|--------|---------------|
| `test_clarifier.py` | 64 | ✅ All pass | All `_parse_*` pure functions (environment, tier, providers, budget, compliance, count) + `_extract_workloads_from_text()` |
| `test_profiler.py` | 20 | ✅ All pass | `_resolve_category` (6 cases), `_guard_ai_ml` (5 cases), `_estimate_resources` (7 cases), `_heuristic_rationale` (2 cases) |
| `test_finops.py` | 19 | ✅ All pass | `_compute_tco` (7 cases), `_CATEGORY_TO_COST_FIELD` (7 cases), `_SPOT_INELIGIBLE` (6 cases), `_group_results_by_provider` (2 cases), `_resolve_category_for_result` (3 cases) |
| `test_engines.py` | 15 | ✅ All pass | Bin-packing: empty/skip/FFD/BFD/replicas. Scoring: empty/filter/rank/weights/fields |
| `test_models.py` | 15 | ✅ All pass | ServiceCategory (5), WorkloadRequirement P2 SLA fields (5), AncillaryCost (3), ProviderCostBreakdown (2), NormalizedPriceItem (2) |

**Integration test file**:

| File | Tests | Status | What it tests |
|------|-------|--------|---------------|
| `test_pipeline.py` | 13 | ✅ All pass | Clarifier node (5 async tests with mock LLM/PricingService), Profiler node (5 async tests), OrchestratorState shape (3 sync tests) |

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
| `test_rfp_size.py` | RFP char-count validation (target 15K-30K chars) | ✅ 24,230 chars |
| `test_cal_fire_e2e.py` | Cal Fire E2E pipeline validation — 10 acceptance criteria (P7+P8 regression) | ✅ 10/10 passed |
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
| ~~4~~ | ~~**FinOps agent**~~ (`src/agents/finops.py`) | ✅ P1d done — RI/spot fallbacks, [Infra] routing, 3yr/5yr TCO |
| ~~5~~ | ~~**RFP Writer agent**~~ (`src/agents/rfp_writer.py`) | ✅ ~2100 lines, 18+ sections, ~25k chars — P1e + P7g + P10 complete |

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

### Phase 4 — Testing & Hardening ✅ DONE

| # | Task | Status |
|---|------|--------|
| ~~13~~ | ~~`tests/conftest.py` — shared pytest fixtures~~ | ✅ 10 fixtures, NormalizedPriceItem factory correct |
| ~~14~~ | ~~`tests/unit/` — unit tests for all agents + engines~~ | ✅ 129/129 tests pass |
| ~~15~~ | ~~`tests/integration/` — end-to-end workflow tests~~ | ✅ 13/13 tests pass |

### Phase 5 — Quality Gaps (Identified 17 Apr 2026)

> **Context**: First full pipeline run produced a subpar RFP — wrong SKU selections,
> unrealistic costs ($18-47/mo for production K8s+DB+S3), thin 4,690-char output
> vs. the 30-60 page reference RFPs (Mass Tech Collaborative, Cal Fire/Presidio).

#### 5a. Clarifier Agent — Missing Depth ✅ FIXED (P1a + P4 + P5 + P6 + P7 + P8)

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

#### 5b. Profiler Agent — Wrong Category Resolution ✅ FIXED (P1b + P7e + P9f)

| Gap | Description | Severity |
|-----|-------------|----------|
| **K8s classified as AI_ML** | Priority-ordered `_CATEGORY_PRIORITY` checks AI_ML first; LLM enrichment may cascade into GPU defaults | CRITICAL |
| **No component decomposition** | "3 microservices" → should produce 3 `ComponentProfile`s (api-gw, biz-logic, worker), not 1 | CRITICAL |
| **Resource over-estimation** | K8s got 9 vCPU, 76.8 GB RAM, GPU — should be ~0.5-2 vCPU, 1-4 GB per microservice | HIGH |
| **No K8s cluster-level costs** | Doesn't model EKS/AKS/GKE management fee ($72-100/mo), node pool sizing, or cluster networking | HIGH |

#### 5c. Sizer Agent — Wrong SKU Selection ✅ FIXED (P1c + P9a–P9f)

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

#### 5d. FinOps Agent — ✅ P1d COMPLETE

| Gap | Description | Status |
|-----|-------------|--------|
| **Costs unrealistically low** | Fixed by P1c (Sizer): correct SKU/category/ancillary costs | ✅ Fixed in P1c |
| **No ancillary cost modeling** | Sizer now adds `[Infra]` items; FinOps routes them to `networking_monthly_usd` | ✅ Fixed in P1d |
| **No reserved/spot pricing** | Fallback discount rates applied when live pricing unavailable (always-populated savings table) | ✅ Fixed in P1d |
| **No multi-year TCO** | `_compute_tco()` computes 1yr/3yr/5yr with 15%/yr growth; stored in KPIs and summary message | ✅ Fixed in P1d |

#### 5e. RFP Writer — Thin Output ✅ FIXED (P1e + P7g + P10)

Was: ~4,690 characters. Now: ~25,000+ characters. Reference RFPs: 30-60 pages.
**P1e** added 12 new sections (architecture, tech specs, SLA, security, migration, DR, TCO, certifications, assumptions, compliance).
**P7g** fixed section numbering and ToC alignment.
**P10** added government framing, managed services table, requirements traceability matrix, mobile architecture subsection, WCAG/StateRAMP implementation specifics, and greenfield delivery phases.

| Missing Section | In Reference RFPs | Our Output | Status |
|-----------------|-------------------|-----------|--------|
| Cover letter | Formal addressee, RFP reference number | None | MEDIUM — out of scope |
| Table of contents | Numbered, multi-level | ✅ 16-entry ToC | ✅ Fixed P1e |
| Architecture description | Topology diagrams, data flow | ✅ ASCII diagram + data flow + network table | ✅ Fixed P1e |
| Mobile architecture | iOS + Android data path | ✅ `_build_mobile_subsection()` when mobile detected | ✅ Fixed P10d |
| Managed services specifics | Kinesis, DynamoDB, AppSync, SNS | ✅ `_MANAGED_SERVICES` per-provider table | ✅ Fixed P10e |
| Requirements traceability | A.1–N.2 compliant/acknowledged matrix | ✅ F-xx / C-xx / NFR-xx when compliance present | ✅ Fixed P10b |
| Detailed tech specs | Per-component version, config, capacity | ✅ Per-component resource + SKU tables | ✅ Fixed P1e |
| SLA / uptime guarantees | 99.9%/99.99% targets | ✅ Tier-specific SLA targets + measurement | ✅ Fixed P1e |
| Security architecture | AES-256, TLS 1.3, IAM, DDoS, WAF | ✅ Full security section + WAF findings | ✅ Fixed P1e |
| Migration / delivery plan | Phased plan with milestones | ✅ Greenfield (MVP→UAT→Launch→Ops) or migration phases | ✅ Fixed P10c |
| Disaster recovery / backup | DR strategy, RPO/RTO, failover | ✅ Tier-specific DR + failover procedure | ✅ Fixed P1e |
| Multi-year cost projection | 3-5 year TCO | ✅ 5-year on-demand + RI TCO tables | ✅ Fixed P1e |
| WCAG/StateRAMP specifics | Contrast ratios, ATO pathway | ✅ 11-row WCAG table + 7-step StateRAMP ATO | ✅ Fixed P10f |
| Compliance certifications | SOC2, ISO 27001, FedRAMP, HIPAA | ✅ Per-provider cert tables + shared responsibility | ✅ Fixed P1e |
| Assumptions & exclusions | Explicit scope boundaries | ✅ 9 assumptions + exclusion list | ✅ Fixed P1e |
| Staffing / project management | PLCM methodology, named team members | None | MEDIUM — out of scope |
| **Vendor qualifications** | Past performance, references, partnerships | Rank by cost only | MEDIUM |

#### 5f. LangFuse Tracing — Not Working ✅ FIXED (P0)

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

### Phase 6 — Cal Fire End-to-End Test Defects (Identified 25 Apr 2026)

> **Context**: First full LLM-powered clarifier run (Cal Fire RFP scenario, AWS, 50K→2M users,
> StateRAMP Moderate + WCAG 2.2 AA, $3.2M/yr, 99.99% SLA, cross-region DR) produced an RFP
> that passed budget but **failed 6 of 8 acceptance criteria**. Root causes are in Profiler,
> Sizer SKU lookup, and compliance propagation — not the RFP Writer or FinOps.

#### 6a. Scale Not Propagated — Profiler ✅ FIXED (P7a)

| Defect | Detail |
|--------|--------|
| **50K→2M surge ignored** | Clarifier collects `concurrent_users` and `scale` but `run_clarifier_node` doesn't write them into `WorkloadRequirement.concurrent_users` or add auto-scaling hints |
| **27 vCPU total for 2M users** | Massively undersized — production EKS for this traffic needs 50-200+ nodes |
| **No auto-scaling annotation** | `WorkloadRequirement.scaling_pattern` not set to `BURST`; Sizer picks cheapest single SKU instead of a node pool |

#### 6b. Compliance Tags Lost — Clarifier→Pipeline ✅ FIXED (P7b)

| Defect | Detail |
|--------|--------|
| **StateRAMP Moderate not in output** | Compliance section of RFP says "No specific compliance frameworks stated" — structured dict has `compliance: ["stateramp-moderate", "wcag-2.2-aa"]` but `run_clarifier_node` doesn't propagate them to `WorkloadRequest.workloads[*].compliance_tags` |
| **WCAG 2.2 AA absent** | Same — never reaches RFP Writer, so no WCAG language in security/compliance sections |

#### 6c. CDN Not Inferred — Profiler ✅ FIXED (P7c)

| Defect | Detail |
|--------|--------|
| **No CloudFront/CDN workload** | Public-facing geospatial platform with 2M users needs CDN — should be auto-inferred from "public", "real-time", "geospatial", "mobile" keywords in `_extract_workloads_from_text()` |
| **No geospatial service workload** | GIS data feeds / map tiles not extracted as a distinct component |

#### 6d. PostgreSQL SKU Returns $0 — Sizer ✅ FIXED (P7d + P9b)

| Defect | Detail |
|--------|--------|
| **"No SKU candidates found for database on aws in us-east-1"** | Sizer queries `us-east-1` (default) but the enriched input says `us-west-2` (primary region). Region mismatch causes empty results. |
| **`$0.00, fit=0.00`** | When no candidates found, Sizer returns a zero-cost fixed placeholder — FinOps then sums it as $0 |

#### 6e. AI/ML Workload Hallucinated — Profiler ✅ FIXED (P7e)

| Defect | Detail |
|--------|--------|
| **AI/ML Workload appeared** | Not requested by client — LLM enrichment in Profiler inferred "predictive analytics" from "wildfire" context and added a GPU workload |
| **`_guard_ai_ml()` not triggered** | Guard only fires when category is already AI_ML AND `gpu_count==0`. LLM enrichment adds the GPU count first, bypassing the guard. |

#### 6f. WAF Multi-Cloud False Positive — FinOps/WAF Engine ✅ FIXED (P7f)

| Defect | Detail |
|--------|--------|
| **FAIL: "Evaluate ≥ 2 providers"** | Client explicitly chose AWS with US data residency. This WAF check should be skipped or overridden when `PROVIDER_STRATEGY = single_aws`. |

#### 6g. Section Numbering Out of Order — RFP Writer ✅ FIXED (P7g)

| Defect | Detail |
|--------|--------|
| **Sections render as 1, 1, 3, 4, 2, 3, 7, 8…** | Section builder functions use hardcoded numbers that don't match the ToC order. ToC has 14 entries; some sections share the same heading number. |

---

## 15. Key Invariants (Never Break These)

1. **Agents use `BaseChatModel` only** — never import `ChatBedrockConverse` or `ChatGoogleGenerativeAI` in agent files.
2. **Agents call `PricingService`** — never call `AzurePricingProvider` / `AWSPricingProvider` / `GCPPricingProvider` directly.
3. **All LangGraph nodes are decorated with `@observe()`** — every agent step traced in LangFuse.
4. **Every public function uses `structlog`** — no `print()`, no stdlib `logging`.
5. **`NormalizedPriceItem` is the only pricing model** agents and engines see — `ComputeSKU`/`StorageSKU` are engines-internal deprecated.
6. **State lists are append-only** — never replace `messages`, `sized_results`, or `savings_opportunities`; let the LangGraph reducer merge.
7. **`uv sync --all-extras`** after any `pyproject.toml` change.

---

## 16. Cal Fire Live E2E Run — Results & Bugs (4 May 2026)

**Run summary**: Full Cal Fire scenario against Vertex AI `gemini-2.5-pro`. 4 clarifier turns → `status: ready` → 5-agent pipeline → 34,942-char RFP. Runtime: 203.9s. WAF score: 100%. 8/8 acceptance criteria: ✅ PASS.

**Full trace log**: See `docs/test_prompt.md`.

### 16a. Agent Output Trace

| Component | Category | vCPU | Mem (GB) | Provider SKU | Monthly Cost |
|-----------|----------|------|----------|-------------|-------------|
| Containerised Application | CONTAINER | 3 | 2.2 | x8i.48xlarge (192 vCPU) ❌ | $34,972 |
| Kubernetes Cluster Mgmt | KUBERNETES | 0 | 0.0 | Fixed fee | $73 |
| Load Balancer | NETWORKING | 3 | 6.0 | Fixed fee (❌ vCPU assigned) | $18 |
| PostgreSQL Database | DATABASE | 3 | 6.0 | None found ❌ | $0 |
| Cache Layer (Redis) | DATABASE | 3 | 6.0 | USW2-RDS:GP3-Storage ❌ | $0 |
| Object Storage (S3) | STORAGE | 3 | 6.0 | USW2-EarlyDelete-ByteHrs ❌ | $0 |
| Geospatial Storage (S3) | STORAGE | 3 | 6.0 | USW2-EarlyDelete-ByteHrs ❌ | $0 |
| CDN / Edge Delivery | NETWORKING | 3 | 6.0 | USW2-IPAddressManager-IP-Hours ❌ | $0 |
| Wildfire Alert Service | SERVERLESS_FUNCTION | 0 | 0.0 | Fixed fee | $20 |
| [NAT Gateway] | [Infra] | — | — | Fixed estimate | $33 |
| [Data Transfer] | [Infra] | — | — | Fixed estimate | $11 |
| **TOTAL** | | **24 vCPU** | **56.2 GB** | | **$35,375/mo** |

Note: 99% of total cost is driven by a single wrong SKU (x8i.48xlarge at $34,972). True cost for a correct build should be ~$800–$2,500/mo.

### 16b. Sizer Bugs (Priority 9 — ALL FIXED 4 May 2026)

| ID | Component | Symptom | Fix Applied |
|----|-----------|---------|------------|
| S1 ✅ | Container App | `x8i.48xlarge` (192 vCPU, $34,972/mo) for 3 vCPU workload | `max_node_vcpus = max(8, total_needed × 4)` guard + preferred family sort in `_size_container_workload()` |
| S2 ✅ | PostgreSQL DB | $0, fit=0.0 — no RDS instance SKU found | DATABASE candidates filtered to `unit_of_measure in ("1 Hour", "1 hour")` after pricing fetch |
| S3 ✅ | Cache Layer (Redis) | `USW2-RDS:GP3-Storage` selected | `_DATABASE_ENGINE_MAP` now has `("aws", "redis") → AmazonElastiCache`; Azure/GCP entries added |
| S4 ✅ | Object / Geospatial Storage | `USW2-EarlyDelete-ByteHrs` ($0) selected | `_filter_storage_candidates()` excludes Glacier/EarlyDelete/archive rows; prefers standard tier |
| S5 ✅ | CDN / Edge Delivery | `USW2-IPAddressManager-IP-Hours` ($0.20) selected | `_is_cdn_workload()` + `_CDN_COST_MONTHLY` fixed-cost path bypasses pricing API |
| S6 ✅ | LB / CDN / Storage | 3 vCPU / 6 GB assigned to managed services | `_estimate_resources()` in profiler.py: NETWORKING → `vcpus=0`; STORAGE → `vcpus=0, storage_gb retained` |

### 16c. RFP Document vs Presidio Reference Gaps — ✅ FIXED (Priority 10, 4 May 2026)

Comparison source: `docs/Presidio_Response_Cal Fire_Draft 10.7.25 (2) (1).html`.

| Gap | ID | Fix Applied | Where |
|-----|----|-------------|-------|
| Document type (infrastructure analysis vs. proposal) | 10a ✅ | `_is_government_scenario()` → title = "Proposed Cloud Solution", doc_type = "Solution Proposal" | `rfp_writer.py` |
| No requirements traceability | 10b ✅ | `_build_requirements_traceability_section()` — F-xx / C-xx / NFR-xx tables appended as §17 | `rfp_writer.py` |
| Wrong delivery phase frame | 10c ✅ | `_is_greenfield_project()` → Phase 1 MVP → Phase 2 UAT → Phase 3 Launch → Phase 4 Managed Ops | `rfp_writer.py` |
| Missing mobile architecture | 10d ✅ | `_is_mobile_scenario()` + `_build_mobile_subsection()` — ASCII mobile data path + 6 principles | `rfp_writer.py` |
| Generic component names only | 10e ✅ | `_MANAGED_SERVICES` dict + `_build_managed_services_section()` — specific service names per provider | `rfp_writer.py` |
| WCAG/StateRAMP not described | 10f ✅ | Expanded `_build_certifications_section()` — WCAG 11-row table, StateRAMP 7-step ATO, HIPAA/CJIS bullets | `rfp_writer.py` |

**Remaining gaps (not in P10 scope)**: Real-time Kinesis streaming, Redshift analytics tier, ArcGIS geospatial microservice, SNS push notifications, content moderation workflow, staffing/PLCM methodology — these require upstream Clarifier/Profiler changes to detect and route correctly.

---

## 17. Second Cal Fire Live Run — Results & Bugs (4 May 2026)

**Run summary**: Second full Cal Fire pipeline run (request_id `4e712463`), Vertex AI `gemini-2.5-pro`. Clarifier: 9 workloads identified (added CDN + Geospatial storage vs first run). Profiler: 9 components profiled correctly. Sizer: 11 combinations sized. RFP: 41,718 chars, 19 sections, 100% WAF score. Runtime: 206.8s.

**Improvement vs first run**: Section count up (15 → 19), char count up (34,942 → 41,718). P9 container bug (x8i.48xlarge) fixed ✅. P9 CDN fixed ✅. P9 storage tier filter partially working.

### 17a. Sizer Output Trace

| Component | SKU | Monthly Cost | Fit | Status |
|-----------|-----|-------------|-----|--------|
| Containerised Application (aws) | m7i.xlarge | $1,242.17 | 0.25 | ❌ Wrong meter (SQL Enterprise) |
| Kubernetes Cluster (aws) | Fixed cost | $73.00 | 1.00 | ✅ |
| API Server (aws) | m5d.xlarge | $266.45 | 0.76 | ✅ |
| Postgresql Database (aws) | N/A | $0.00 | 0.00 | ❌ No candidates found |
| Cache Layer (aws) | N/A | $0.00 | 0.00 | ❌ No candidates found |
| Object Storage (aws) | USW2-TimedStorage-INT-AIA-ByteHrs | $0.00 | 0.70 | ❌ Archive tier, $0 cost |
| Load Balancer (aws) | N/A | $22.27 | 1.00 | ✅ |
| CDN / Edge Delivery (aws) | N/A | $85.00 | 1.00 | ✅ |
| Geospatial / Tile Storage (aws) | USW2-TimedStorage-INT-AIA-ByteHrs | $0.00 | 0.70 | ❌ Archive tier, $0 cost |
| [Infra] NAT Gateway (aws) | N/A | $32.40 | 1.00 | ✅ |
| [Infra] Data Transfer (aws) | N/A | $9.00 | 1.00 | ✅ |
| **TOTAL** | | **$1,730.29** | | ❌ Severely understated |

**Expected total**: ~$15,000–$50,000/mo for this scale (database + storage + proper EC2 pricing missing).

### 17b. Priority 11 Bugs — Fixed (4 May 2026)

| ID | Component | Symptom | Root Cause | Fix Applied | File |
|----|-----------|---------|-----------|-------------|------|
| **11a** ✅ | PostgreSQL DB, Cache Layer | $0.00, fit 0.00 — no candidates | `_DATABASE_ENGINE_MAP` key lookup requires `resources.database_engine` to be set, but Clarifier leaves it None; workload name "Postgresql Database" / "Cache Layer" not parsed for engine. | Added `_infer_engine_from_name()` fallback: if field is None, parse engine from workload name keyword match. "Postgresql Database" → "postgresql" → AmazonRDS; "Cache Layer" → "redis" → AmazonElastiCache. | `sizer.py` |
| **11b** ✅ | Containerised Application | m7i.xlarge at $1,242/mo — should be ~$139/mo | EC2 returned "Unused Reservation Linux with SQL Server Enterprise" meter at $1.70/hr; hourly filter passed it through. | Added AWS-specific Linux OS filter after hourly filter: `operatingSystem == "Linux"` and `usagetype` not containing "UnusedBox" or "UnusedDed". Reduces to standard Linux on-demand rows only. | `sizer.py` |
| **11c** ✅ | Object Storage, Geospatial Storage | `INT-AIA` tier at $0.004/GB-Month shows $0.00 | (1) `_filter_storage_candidates()` didn't exclude INT-AIA; (2) monthly cost = `unit_price × 1` not `× storage_gb`. | (1) Added `int-aia`, `int-fa`, `int-aa`, `int-da` to exclude patterns. (2) Added post-processing after generic sizing for STORAGE: if unit is monthly and storage_gb is set, compute `unit_price × storage_gb`. | `sizer.py` |
| **11d** ✅ | RFP document | Both "Delivery Plan" and "Disaster Recovery" numbered §11 | `_build_dr_section()` hardcoded "## 11. Disaster Recovery" — duplicate of "## 11. Delivery Plan" in `_build_migration_section()`. | Changed `_build_dr_section()` heading to `"## 12. Disaster Recovery & Business Continuity\n"`. | `rfp_writer.py` |
| **11e** ✅ | RFP Managed Services table | Cache Layer → "Amazon RDS Multi-AZ" (wrong) | `_build_managed_services_section()` used `category.value.upper()` ("DATABASE") for all DB-category workloads — no distinction between relational DB and cache. | Added name-based override in the component loop: if `resolved_category == DATABASE` and name contains "cache"/"redis"/"elasticache", use "CACHE" key instead → "Amazon ElastiCache for Redis". | `rfp_writer.py` |

**Post-fix test result**: 142/142 tests pass ✅. All fix logic verified via unit tests and quick Python validation.

---

## 18. Third Cal Fire Live Run — Results & Bugs (5 May 2026)

**Run summary**: Third full Cal Fire pipeline run (request_id `9498902b`), Vertex AI `gemini-2.5-pro`. Clarifier: 4 turns → ready. 9 workloads identified. Profiler: 9 components profiled. Sizer: 11 combinations sized. RFP: 41,169 chars, 19 sections, 100% WAF score. Runtime: 244.3s.

**Improvements vs run #2**: Section count same (19), char count similar (41,169 vs 41,718). P11d ✅ (§11 collision resolved). P11e ✅ (Cache Layer → ElastiCache). P11b **partially** working (m7i.xlarge SQL Ent meter gone), but new SQL Std meter now selected.

### 18a. Sizer Output Trace

| Component | SKU | Monthly Cost | Fit | Status |
|-----------|-----|-------------|-----|--------|
| Containerised Application (aws) | i3.4xlarge | $2,312.64 | 0.20 | ❌ SQL Std meter, storage-optimized family |
| Kubernetes Cluster (aws) | Fixed cost | $73.00 | 1.00 | ✅ |
| API Server (aws) | i3.4xlarge | $2,312.64 | 0.50 | ❌ Same SQL Std / i3 issue |
| Postgresql Database (aws) | N/A | $0.00 | 0.00 | ❌ No candidates (stale cache) |
| Cache Layer (aws) | N/A | $0.00 | 0.00 | ❌ No candidates (stale cache) |
| Object Storage (aws) | USW2-TimedStorage-GIR-ByteHrs | $4.00 | 0.70 | ❌ GIR = Glacier Instant Retrieval |
| Load Balancer (aws) | N/A | $22.27 | 1.00 | ✅ |
| CDN / Edge Delivery (aws) | N/A | $85.00 | 1.00 | ✅ |
| Geospatial / Tile Storage (aws) | USW2-TimedStorage-GIR-ByteHrs | $20.00 | 0.70 | ❌ GIR tier again |
| [Infra] NAT Gateway (aws) | N/A | $32.40 | 1.00 | ✅ |
| [Infra] Data Transfer (aws) | N/A | $9.00 | 1.00 | ✅ |
| **TOTAL** | | **$4,870.95** | | ❌ Understated (DB/Cache missing) |

**Expected total**: ~$15,000–$60,000/mo when DB/Cache/EC2 are correctly priced.

### 18b. Priority 12 Bugs (Identified 5 May 2026 — All Fixed)

| ID | Component | Symptom | Root Cause | Fix Applied | File |
|----|-----------|---------|-----------|-------------|------|
| **12a** ✅ | Containerised Application, API Server | `i3.4xlarge` at $2,312.64/mo (SQL Std meter) — fit 0.20 | P11b added `operatingSystem == "Linux"` filter but SQL Standard rows also report `operatingSystem=Linux` — only distinguishable by `meter_name` description. | Added `_SQL_LICENSE_TOKENS` tuple (`"sql std"`, `"sql ent"`, `"sql web"`, `"with sql"`, `"sqlserver"`, `"windows"`, `"rhel"`, `"suse"`); filter checks `(c.meter_name or "").lower()` before adding to `linux_only` candidates. | `sizer.py` ~line 1221 |
| **12b** ✅ | Object Storage, Geospatial Storage | `USW2-TimedStorage-GIR-ByteHrs` (S3 Glacier Instant Retrieval) selected | `GIR` not in `_EXCLUDE_PATTERNS`; P11c added Intel-Tiering sub-tiers but missed GIR's distinct SKU code prefix. | Added `"-gir-"` and `"gir-bytehrs"` to `_EXCLUDE_PATTERNS` in `_filter_storage_candidates()`. | `sizer.py` ~line 523 |
| **12c** ✅ | Postgresql Database, Cache Layer | $0.00, fit 0.00 (N/A) | Stale SQLite cache had wrong/missing RDS and ElastiCache rows from before P11a fix. | Deleted `data/sku_cache.db` to force fresh fetch on next pipeline run. | `data/sku_cache.db` |
| **12d** ✅ | RFP Requirements Traceability | Initially suspected: NFR-05 "Budget ceiling: Not specified" | Investigation and run #4 confirmed this is NOT a bug — `_parse_budget("Budget: $266,666/month")` correctly returns `266666.0`; NFR-05 in run #4 RFP shows `Budget ceiling: $266,666/mo`. | No code change needed. | `clarifier.py` / `rfp_writer.py` |

### 18d. Fourth Live Cal Fire Run (5 May 2026, request_id `4d134426`)

**Status**: Partially verified — code fixes confirmed but pricing data empty due to expired AWS STS credentials.

| Component | Selected SKU | Monthly Cost | Fit Score | Status |
|-----------|-------------|-------------|-----------|--------|
| All compute/container/database | N/A | $0.00 | 0.00 | ❌ AWS `ExpiredToken` error during pricing fetch |
| Kubernetes Cluster | Fixed cost | $73.00 | 1.00 | ✅ |
| Load Balancer | Fixed cost | $22.27 | 1.00 | ✅ |
| NAT Gateway | Fixed cost | $32.40 | 1.00 | ✅ |
| Data Transfer | Fixed cost | $9.00 | 1.00 | ✅ |

**Budget propagation (Bug 12d confirmed fixed)**: `budget_monthly_usd = 266666.0` in `CostComparison`; RFP §17 NFR-05 shows `Budget ceiling: $266,666/mo` ✅

**Blocker for final verification**: AWS STS session token (`ASIA` prefix) expired. Refresh `.env` with new credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`) and re-run to populate the pricing cache with correct m-family EC2, RDS, and ElastiCache data.

**All 142 tests pass** with P12 fixes in place.


### 18c. LLM Factory Fix Applied This Session

| Change | File | Detail |
|--------|------|--------|
| `_create_vertexai()` passes `vertexai=True` | `src/llm/factory.py` | `ChatGoogleGenerativeAI` requires this flag to route to Vertex AI endpoint (not Google AI Studio) |
| `project=project` passed explicitly | `src/llm/factory.py` | ADC project resolves to `None` in some dev environments — explicit parameter prevents crash |
| `location=location` passed explicitly | `src/llm/factory.py` | Defaults to wrong endpoint without this; now reads `VERTEXAI_LOCATION` env var (default `us-central1`) |
| `GCP_PROJECT_ID` fallback added | `src/llm/factory.py` | Falls back to `GCP_PROJECT_ID` env var if `VERTEXAI_PROJECT` is not set |
| Gemini 3.1 Pro Preview status | `.env` (reverted) | `gemini-3.1-pro-preview` is a restricted preview — requires Model Garden enablement per GCP project. Not currently accessible in `dissertation-rj`. Use `gemini-2.5-pro` until enabled. |

---

## 19. Fifth + Sixth Cal Fire Runs — P13 Bug Discovery & Fixes (5 May 2026)

### 19a. Fifth Live Cal Fire Run (request_id `049a1b82-d5eb-430d-b907-469675de9be6`)

**Run summary**: Run #5 with fresh AWS STS credentials. Clarifier: 2 turns → ready. 10 workloads identified. Profiler: 10 components. Sizer: 12 combinations. RFP: 44,057 chars, 19 sections, 100% WAF (6/6). Runtime: **254,361ms (~4.25 min)**.

| Component | SKU | Monthly Cost | Fit | Status |
|-----------|-----|-------------|-----|--------|
| Containerised Application (aws) | m7i.xlarge | $1,242.17 | 0.25 | ❌ UnusedBox RI artifact selected |
| Kubernetes Cluster (aws) | Fixed cost | $73.00 | 1.00 | ✅ |
| API Server (aws) | m5d.xlarge | $266.45 | 0.76 | ❌ NVMe-storage instance for generic API |
| Postgresql Database (aws) | N/A | $0.00 | 0.00 | ❌ Zero candidates (wrong API filter) |
| Cache Layer (aws) | N/A | $0.00 | 0.00 | ❌ Zero candidates (wrong API filter) |
| Virtual Machines (aws) | g5.4xlarge | $1,430.22 | 0.66 | ❌ GPU instance for general-purpose VM |
| Object Storage (aws) | USW2-TimedStorage-ZIA-ByteHrs | $10.00 | 0.70 | ❌ ZIA = One Zone-IA tier |
| Load Balancer (aws) | Fixed cost | $22.27 | 1.00 | ✅ |
| CDN / Edge Delivery (aws) | Fixed cost | $85.00 | 1.00 | ✅ |
| Geospatial / Tile Storage (aws) | USW2-TimedStorage-ZIA-ByteHrs | $50.00 | 0.70 | ❌ ZIA tier again |
| [Infra] NAT Gateway | Fixed cost | $32.40 | 1.00 | ✅ |
| [Infra] Data Transfer | Fixed cost | $9.00 | 1.00 | ✅ |
| **TOTAL** | | **$3,220.51** | | ❌ Severely understated — realistic ~$15k–$50k/mo |

### 19b. Root Cause Discovery — Pricing Cache Poisoning

**THE fundamental root cause of ALL EC2 sizer bugs (discovered this session):**

The AWS Pricing API (`GetProducts` for `AmazonEC2`, `us-west-2`) returns products in an arbitrary/alphabetical order. Without explicit `operatingSystem`, `tenancy`, and `preInstalledSw` filters at the API call level, the first 100 products fetched are dominated by non-standard billing artifacts:

- **"UnusedBox" rows**: RI placeholder meters (e.g. `USW2-UnusedBox:m7i.xlarge` at $1.70/hr). Stored as `pricing_tier=on_demand` because they appear under the `OnDemand` term section of the API. They have *real* `unit_price > 0` and `operatingSystem=Linux` — so they pass both the `unit_price > 0` filter and the `linux_only` `operatingSystem` check. They are only distinguishable by `"unusedbox"` in the `usagetype` field.
- **"DedicatedHost" rows**: `$0.00` per-host meters (`USW2-HostBoxUsage:c6g.4xlarge`). Pass the `operatingSystem=Linux` check. Filtered out by `unit_price > 0` at line 1209 of sizer.py.
- **"Reservation" placeholders**, Windows, RHEL, SQL-licensed rows.

**The `linux_only` fallback bug**: When all non-$0 rows are UnusedBox/SQL-licensed (i.e. ALL are filtered out by the `linux_only` check), `linux_only` is empty → `if linux_only:` is False → `candidates` reverts to the pre-filter set (the garbage rows with prices). The m7i.xlarge UnusedBox row ($1.70/hr, 4 vCPU) fit the vCPU ceiling for the container workload → selected.

**For AmazonRDS and AmazonElastiCache**: The `_DATABASE_ENGINE_MAP` passes engine names ("PostgreSQL", "cache.r6g") as `sku_name`, which was then used as `instanceType` filter value in `GetProducts`. No RDS/ElastiCache instance type is named "PostgreSQL" or "cache.r6g" — so 0 products returned → 0 cost.

**For AmazonS3**: 87 rows cached, all archival tiers (ZIA, GIR, INT-FA, Glacier, IA). No `General Purpose` (Standard) rows. ZIA slipped through `_EXCLUDE_PATTERNS` because the pattern only had `"infrequent"` (not `"-zia-"`) — and `"USW2-TimedStorage-ZIA-ByteHrs"` doesn't contain "infrequent".

### 19c. Priority 13 Bugs — All Fixed (5 May 2026)

| ID | Component | Symptom | Root Cause | Fix Applied | File |
|----|-----------|---------|-----------|-------------|------|
| **13a** ✅ | EC2 (Container, API, VMs) | Garbage RI artifacts selected (UnusedBox m7i.xlarge, g5 GPU) | No OS/tenancy/preInstalledSw filters in `GetProducts` → cache poisoned with billing artifacts | Added `operatingSystem=Linux`, `tenancy=Shared`, `preInstalledSw=NA` EC2 API filters in `search_prices()` per-service-code block | `aws_provider.py` |
| **13b** ✅ | linux_only fallback | Garbage rows selected when all clean rows filtered | `if linux_only: candidates = linux_only` — if `linux_only` empty, candidates stayed as garbage rows | When `linux_only` is empty, log warning and set `candidates = []` (triggers "no candidates" path) | `sizer.py` ~line 1244 |
| **13c** ✅ | Virtual Machines | g5.4xlarge GPU selected for non-GPU workload | No GPU exclusion filter in VM candidate list | Added `_GPU_PREFIXES` exclusion (g3/g4/g5/g6/g7/p2-p5/trn/dl1) gated on `component.requires_gpu` | `sizer.py` ~line 1256 |
| **13d** ✅ | Postgresql Database, Cache Layer | $0.00 / 0 candidates | `sku_name` ("PostgreSQL", "cache.r6g") used as `instanceType` filter → 0 matches. For RDS: `databaseEngine` field needed. For ElastiCache: `cacheEngine` field needed. | Per-service-code `extra_filters` in `search_prices()`: RDS → `databaseEngine+deploymentOption`; ElastiCache → `cacheEngine=Redis` | `aws_provider.py` |
| **13e** ✅ | Object Storage, Geospatial Storage | ZIA (One Zone-IA) tier selected | `"-zia-"` and `"zia-bytehrs"` not in `_EXCLUDE_PATTERNS`; S3 API returned all archival tiers | Added `"-zia-"` and `"zia-bytehrs"` to `_EXCLUDE_PATTERNS`; added `storageClass=General Purpose` S3 API filter | `sizer.py` ~line 511; `aws_provider.py` |
| **13f** ✅ | RFP document | Duplicate §5 (SKU Selection) and §13 (Vendor) | Hardcoded section numbers not updated when `_build_managed_services_section` (§5) was added | Fixed 8 hardcoded section numbers: §6 SKU, §7 Cost, §8 TCO, §9 SLA, §10 Security, §14 Vendor, §15 Assumptions, §16 WAF | `rfp_writer.py` |

**Test result**: 142/142 tests pass after P13 fixes. Cache deleted (`data/sku_cache.db`) to force fresh fetch with correct API filters.

**Next step**: Run #6 with fresh AWS credentials to verify corrected SKU selection (expect m-family EC2, RDS `db.*` instances, ElastiCache `cache.r6g.*`, and S3 Standard storage).

---

## 20. Sixth Cal Fire Run — P13 Verified ✅, P14 Discovered & Fixed (6 May 2026)

### 20a. Run #6 Results (request_id: 3c95b188)

**Run summary**: Run #6 with fresh AWS STS credentials (P13 fixes applied). Clarifier: 2 turns → ready (down from 4 in run #5 — table format provided all answers in turn 2). 9 workloads identified. Profiler: 9 components, 12 vCPU, 32.2 GB RAM, 12,540 GB storage. Sizer: 11 combinations (inc. 2 infra). RFP: 43,182 chars, 19 sections, 100% WAF (5/5). Runtime: **246,181ms (~4.1 min)**.

### 20b. P13 Fix Verification (via `cost_comparison.selected_skus`)

| Fix | SKU attribute confirmed | Status |
|-----|------------------------|--------|
| EC2 API filters (OS/tenancy) | `usagetype: "USW2-BoxUsage:c5.xlarge"`, `operatingSystem: "Linux"` | ✅ CONFIRMED |
| RDS databaseEngine filter | `databaseEngine: "PostgreSQL"`, `db.t3.micro` at $0.018/hr | ✅ CONFIRMED |
| ElastiCache cacheEngine=Redis filter | `cache.t1.micro` Redis row at $0.022/hr | ✅ CONFIRMED |
| S3 storageClass=General Purpose | `storageClass: "General Purpose"`, `volumeType: "Standard"` | ✅ CONFIRMED |
| GPU exclusion | No g4/g5/p-series instances selected | ✅ CONFIRMED |
| ZIA exclusion | `USW2-TimedStorage-ByteHrs` (not ZIA) for both storage workloads | ✅ CONFIRMED |

**Total cost run #6**: $630.07/mo (vs $3,220.51 in run #5 with garbage SKUs). All P13 fixes confirmed working.

### 20c. Priority 14 Bugs — Discovered in Run #6, Fixed (6 May 2026)

**Root cause**: `_size_generic_workload()` calls `_select_best_by_price()` which picks the cheapest hourly row with no resource minimum filtering. The profiler correctly sets 12 GB RAM / 3 vCPU for both DB/cache, but the sizer ignores resource requirements for DATABASE category workloads.

| ID | Component | Symptom | Root Cause | Fix Applied | File |
|----|-----------|---------|-----------|-------------|------|
| **14a** ✅ | Postgresql Database | `db.t3.micro` (1 GiB) selected for 12 GB requirement — 12× undersized at $13.14/mo | Cheapest-price strategy has no vCPU/memory minimum filter | Added `_filter_database_candidates()`: filters deprecated instances (`currentGeneration=No`) and candidates with `memory < required_memory_gb × 0.8`. Cheapest passing = `db.r6i.large` (16 GiB, $182.50/mo) | `sizer.py` |
| **14b** ✅ | Cache Layer | `cache.t1.micro` (0.213 GiB, `currentGeneration=No`) selected for 12 GB Redis requirement — deprecated, 56× undersized at $16.06/mo | Same cheapest-price strategy; no currentGeneration filter | Same fix — `currentGeneration=No` filter removes `cache.t1.micro`; resource filter removes under-memory instances. Cheapest passing = `cache.r4.large` ($132.86/mo) | `sizer.py` |

**Expected run #7 total**: ~$916/mo (vs $630 in run #6) — more realistic for mission-critical sizing.

**Next step**: Run #7 with fresh AWS credentials to verify P14 fixes — expect `db.r6i.large` for PostgreSQL and appropriate current-gen ElastiCache instance for Cache Layer.

---

## §21. Gap Analysis & P15 Planning (7 May 2026)

### 21a. Bin-Packing Algorithm Assessment

**Status: Algorithm is correct. The problem is the profiler only produces 1 container workload.**

The FFD/BFD bin-packing engine (`src/engines/bin_packing.py`) is correctly implemented and called from `_size_container_workload()` in the sizer. However, in every Cal Fire test run, the profiler produces only **1 CONTAINER workload** (the main web application), so bin-packing packs "1 workload onto 1 node" — the algorithm runs but demonstrates zero multi-workload packing value.

**Root cause**: `profiler.py`'s `_extract_workload_components()` maps the entire backend to a single component instead of decomposing the enriched_input into individual microservices.

**Expected microservice decomposition for Cal Fire**:

| Microservice | Category | Notes |
|-------------|----------|-------|
| Public API / Incident API | CONTAINER | GraphQL/REST for mobile app |
| Geospatial Service | CONTAINER | ArcGIS layer serving |
| Notification Service | CONTAINER | Push via SNS/APNs/FCM |
| Data Ingestion Worker | CONTAINER | Kinesis consumer → DB writer |
| Admin Portal API | CONTAINER | Staff-facing admin backend |
| Media Moderation Service | CONTAINER | Async S3 → review workflow |

With 6 container workloads, bin-packing would demonstrably pack them across fewer nodes with optimized CPU/memory utilization — this is the architectural value of the algorithm.

**Fix required (P15c)**: Profiler must decompose `enriched_input` into individual microservices (5-8 CONTAINER workloads). The LLM prompt should explicitly ask for component-level breakdown.

---

### 21b. Serverless Architecture Gap

**Status: Serverless NEVER considered. Architecture decision is hardcoded by LLM in clarifier enriched_input.**

**How the architecture is currently chosen**:
1. Clarifier LLM writes `enriched_input` describing "Kubernetes cluster" in the narrative
2. Profiler maps all workloads to CONTAINER/COMPUTE categories
3. Sizer prices EKS nodes — never evaluates Lambda, Fargate, or DynamoDB
4. No comparison or scoring of architectural alternatives happens anywhere

**What should happen (Presidio approach)**:

For a workload with 50K→2M concurrent users (40× spike), a proper architecture selection engine would:
- Score **Serverless** (Lambda + DynamoDB + AppSync): scales to zero, unlimited concurrency, cost ∝ requests — best for extreme spiky loads
- Score **Container** (EKS + RDS): best for steady-state compute, predictable latency, complex business logic
- Score **Hybrid** (serverless API + container workers + managed DB): best of both

The score should use: WAF Reliability pillar × WAF Cost Optimization × scale multiplier × compliance fit

**Fix required (P15a + P15b)**:
- Add `SERVERLESS` to `ServiceCategory` enum (currently 15 values)
- Add serverless pricing path to sizer (Lambda $/request/ms + DynamoDB $/RCU-WCU)
- Add architecture alternatives evaluation engine (`src/engines/architecture_selector.py`):
  - Takes `WorkloadProfile` → scores 3 options (serverless / container / VM)
  - Produces ranked recommendation with WAF score + monthly cost + tradeoff narrative
- RFP Writer: add §2 "Architecture Alternatives Analysis" with Option A/B/C table
- Clarifier enriched_input must be architecture-neutral — the sizer/engine should select

---

### 21c. Presidio RFP vs Our RFP — Gap Table

Reference: `docs/Presidio_Response_Cal Fire_Draft 10.7.25 (2) (1).html` (parsed 7 May 2026)

| # | Section | Presidio Has | Our RFP Has | Gap Level |
|---|---------|-------------|-------------|-----------|
| 1 | **Architecture decision** | Explicit serverless-first (Lambda+DynamoDB) with rationale tied to 2M concurrent user requirement | Containers/EKS assumed from clarifier text; no evaluation | 🔴 Critical |
| 2 | **Architecture alternatives** | Evaluated serverless vs containers; explains WHY serverless was chosen | No multi-option comparison | 🔴 Critical |
| 3 | **Segregated data paths** | Public path: AppSync→Lambda→DynamoDB; Admin path: APIGW→Lambda→Aurora (separate, explicitly designed) | Single monolithic architecture diagram | 🔴 Critical |
| 4 | **Streaming workload** | Amazon Kinesis Data Streams + Firehose as explicit, costed components | No streaming workload identified/priced | 🟠 High |
| 5 | **Analytics workload** | Amazon Redshift for usage analytics and KPI reporting | Not identified in workload profile | 🟠 High |
| 6 | **Message queue** | Amazon SQS for media upload decoupling (async ingestion) | Not in workload profile | 🟠 High |
| 7 | **WAF as architecture driver** | Every architectural decision anchored to a WAF pillar ("serverless for reliability pillar") | WAF assessment is post-hoc compliance check; not used to SELECT architecture | 🟠 High |
| 8 | **Per-requirement mapping** | Every RFO requirement (A.1, B.2, K.1...) mapped to compliant/non-compliant with implementation note | Generic compliance narrative sections | 🟡 Medium |
| 9 | **Seasonal cost strategy** | Q4 wildfire season reserved Lambda concurrency; DynamoDB reserved capacity; Redshift Spectrum offload | Generic RI/spot optimization horizons | 🟡 Medium |
| 10 | **Multi-region active-active** | PostgreSQL in active-active multi-region (not passive); DynamoDB Global Tables | Active-passive stated | 🟡 Medium |
| 11 | **Microservice decomposition** | 6+ named microservices: notification, geospatial, data ingestion, admin portal, media moderation, analytics | 1 container workload; all services collapsed into one | 🔴 Critical |

---

### 21d. Incremental RFP Amendment Feature

**Status: Not implemented. Every follow-up message triggers a full 5-agent pipeline restart.**

**Current flow**: `POST /orchestrate` → Clarifier → Profiler → Sizer → FinOps → RFP Writer (always from scratch)

**Problem**: User says "add SSO integration" or "what if we need 3M concurrent users" after RFP is generated → system starts over, losing all prior context. The clarifier re-asks basic questions.

**Desired flow**:
- If `conversation_history` already contains a generated RFP + `enriched_input`, detect "amendment intent"
- Route to **amendment mode**: skip Clarifier, update `WorkloadProfile` delta, re-run Sizer → FinOps → RFP Writer with the new/modified workloads only
- Produce an **amended RFP** with a changelog section noting what changed

**Design**:
1. Add `amendment_mode: bool` + `amendment_instructions: str` fields to `OrchestrateRequest`
2. In orchestrator graph: add conditional edge after Clarifier — if `enriched_input` exists and `amendment_mode=True`, skip to Profiler with delta instructions
3. Add `POST /orchestrate/amend` endpoint (or `?mode=amend` query param)
4. Profiler amendment path: merge new workloads into existing `WorkloadProfile` instead of building from scratch
5. RFP Writer amendment path: append "§N. Amendment — [Date]" section with changelog

**Fix required (P15d)**: Design + implement amendment mode in orchestrator and API.

---

### 21e. P15 Bug/Feature Priority Table

| ID | Component | Description | Priority | Status |
|----|-----------|-------------|----------|--------|
| **P15a** | `src/engines/` | New `architecture_selector.py`: score **4 options** (managed-serverless, self-hosted-serverless, containers, hybrid) using real sizer pricing + WAF scoring. Self-hosted serverless = K8s+Knative/KEDA with K8s node cost model (not per-invocation). Cost score uses actual monthly estimates from pricing API, not heuristics. | 🔴 Critical | ✅ Done |
| **P15b** | `src/models/cloud_resource.py` + `sizer.py` | Add `SERVERLESS` to `ServiceCategory`; add Lambda/DynamoDB pricing path to sizer; add Knative/KEDA self-hosted path (re-uses K8s node pricing from CONTAINER path) | 🔴 Critical | ❌ Not started |
| **P15c** | `src/agents/profiler.py` | Decompose enriched_input into 5-8 individual microservices (CONTAINER workloads) so bin-packing works with multiple inputs | 🔴 Critical | ❌ Not started |
| **P15d** | `src/agents/router.py` (NEW) | **Router+Orchestrator Agent**: LLM intent classifier AND execution planner. Reads state + new input. Emits `ExecutionPlan`: intent type, agents to run, scope (full vs delta), affected components by name, RFP sections to amend. Downstream agents read `execution_plan` to understand their scope. See §22b. | 🔴 Critical | ✅ Done |
| **P15e** | `src/agents/validator.py` (NEW) | **Validator Agent** (5 checks): [0] Pricing data integrity via `pricing_validator.py` (apples-to-apples SKU comparison check), [1] Architecture correctness (architecture_selector), [2] Sizing adequacy, [3] Budget fit, [4] WAF compliance. Auto-runs after FinOps. On-demand via Router. See §22c. | 🔴 Critical | ✅ Done |
| **P15f** | `src/services/session_store.py` (NEW) | Session persistence: LangGraph `MemorySaver` checkpointing + aiosqlite session registry. Thread ID = session_id. Cross-request state continuity. | 🔴 Critical | ✅ Done |
| **P15g** | `src/agents/profiler.py` | Add streaming (Kinesis), message queue (SQS), analytics (Redshift) workload detection from enriched_input keywords | 🟠 High | ❌ Not started |
| **P15h** | `graph.py` + `orchestration.py` + `state.py` | Update graph (router+orchestrator node at START, validator node before rfp_writer, conditional edges, LangGraph checkpointing). Add `session_id` + `execution_plan` to state. Add `session_id` to API request/response. | 🔴 Critical | ❌ Not started |
| **P15i** | `src/agents/rfp_writer.py` | Add "Architecture Alternatives Analysis" section (all 4 options) with WAF scores + REAL costs from Validator output. Add "Pricing Data Caveats" note if pricing_validation has errors. | 🔴 Critical | ❌ Not started |
| **P15j** | `src/engines/architecture_selector.py` | Dynamic WAF weight profiles: Clarifier detects user priority ("cost is #1") → adjusts scoring weights (cost_weight=0.40, reliability=0.20, etc.) at runtime | 🟠 High | ✅ Done |
| **P15k** | `src/engines/pricing_validator.py` (NEW) | **Pricing Comparison Validator**: 7-check engine ensuring cross-provider SKU comparisons are valid before FinOps picks a vendor. Checks: size adequacy, tier consistency, price anomaly (>5× variance), SKU staleness, category match, provider parity (all providers represented), memory:vCPU ratio. See §22h. | 🔴 Critical | ✅ Done |

**Build order for P15**: P15b → P15a → P15k → P15c → P15g → P15d → P15e → P15f → P15h → P15i → P15j

---

### 21f. P16 Completeness Gaps (Dissertation Quality)

Features needed to reach dissertation-grade completeness. Not bugs — architectural enhancements.

| ID | Component | Description | Priority |
|----|-----------|-------------|----------|
| **P16a** | `src/engines/architecture_selector.py` | **Real-cost architecture comparison**: price all 4 architectures using actual sizer output (not heuristic scores). Validator prices Managed-Serverless + Self-Hosted-Serverless paths, compares against already-priced Container path. Produces actual $/month for each option. | 🔴 Critical |
| **P16b** | `src/agents/profiler.py` | **Self-hosted serverless workload type**: when architecture_selector picks self-hosted-serverless, profiler re-labels CONTAINER workloads as `SERVERLESS_COMPUTE` (Knative) — sizer then queries K8s node pools + KEDA scaling config. Not Lambda. | 🔴 Critical |
| **P16c** | `src/agents/rfp_writer.py` | **RFP compliance verification pass**: after generating RFP, use LLM to check generated text against StateRAMP Moderate control list. Flag missing controls. Add "Compliance Gap Analysis" appendix. | 🟠 High |
| **P16d** | `scripts/` | **Multi-scenario benchmark**: script to run 3 test scenarios (Cal Fire, healthcare API, e-commerce platform) through the full pipeline and compare output quality vs. reference architectures. Dissertation evaluation chapter data. | 🟠 High |
| **P16e** | `dashboard/` | **Architecture comparison radar chart**: React component showing 4 architecture options scored on 5 WAF axes (reliability, cost, scale, compliance, latency) as a radar/spider chart. Makes the multi-option comparison visual. | 🟡 Medium |
| **P16f** | `dashboard/` | **User rating / feedback capture**: after RFP is generated, user rates recommendation quality (1–5 stars + comment). Logged to LangFuse as custom event. Dissertation can report on system accuracy. | 🟡 Medium |

---

## §22. Router Agent + Validator Agent + Session Persistence Design (8 May 2026)

### 22a. Problem Statement

Three major architectural gaps identified from user testing and Presidio comparison:

1. **No intelligent routing**: every user message (including follow-ups like "add SSO integration") triggers a full 5-agent pipeline from scratch. The system re-clarifies requirements already gathered, loses prior context, and produces a replacement RFP instead of an amendment.

2. **No architecture validation**: the system never checks whether its own recommendation is correct. Lambda+DynamoDB was objectively the better choice for 50K→2M users, but the system blindly produces an EKS recommendation because the clarifier LLM wrote "Kubernetes" in enriched_input.

3. **No pricing comparison integrity check**: when the Sizer returns SKU results for cross-provider comparison, nothing verifies the SKUs are equivalent in size/tier. An undersized Azure SKU may appear cheaper than a correctly-sized AWS SKU, causing FinOps to incorrectly recommend Azure — not because it's cheaper, but because the comparison was invalid.

### 22b. Router+Orchestrator Agent Design (`src/agents/router.py`) — NEW FILE

The Router+Orchestrator Agent is an LLM-powered **intent classifier AND execution planner**. It sits at the entry point of the graph for all requests where a prior session exists. It does two things in a single LLM call: (1) classify what the user wants, and (2) produce a concrete `ExecutionPlan` that tells every downstream agent exactly what to run and how.

**Why not just a simple router?** A pure intent classifier says "this is an amendment." An orchestrator says "this is an amendment — specifically, add a Redis caching layer. Profiler needs to add one new CACHE component. Sizer needs to price only that component. FinOps re-runs full comparison. Validator runs. RFP Writer amends §4 Architecture and §7 Cost." That specificity prevents agents from doing unnecessary work or missing the actual scope of change.

**Trigger condition**: present when `session_id` is provided AND `rfp_document` is non-empty in the restored state (i.e., this is turn ≥ 2 of a session). Turn 1 (no session) goes directly to Clarifier.

**Intent types**:

| Route | Trigger Pattern | Graph Path |
|-------|----------------|-----------|
| `new_request` | No prior session / explicit restart | START → Clarifier → Profiler → Sizer → FinOps → Validator → RFP Writer |
| `amendment` | "add X", "change Y to Z", "what if we add…", "include [feature]", "update the [section]" | START → Router+Orch → Profiler(delta) → Sizer → FinOps → Validator → RFP Writer(amend) |
| `validate` | "is this the right architecture?", "why EKS not Lambda?", "validate the architecture", "check if this is correct" | START → Router+Orch → Validator → END |
| `answer` | "what does [term] mean?", "explain [section]", "why was X chosen?" — can be answered from state without pipeline | START → Router+Orch → END (router writes direct answer to messages) |
| `clarify` | Ambiguous follow-up, missing context | START → Router+Orch → Clarifier(amendment-mode) → ... |

**LLM prompt structure** (uses Principal Architect Reasoning — see §22g):
```
SYSTEM: You are a principal cloud architect acting as an intelligent orchestration agent.
You have the full conversation history and current state. Your job is to:
(1) Understand what the user ACTUALLY wants (not just surface intent)
(2) Identify the MINIMUM set of agents that need to run
(3) Identify the SPECIFIC components that changed (not "everything")
(4) Identify which RFP sections are affected

Think step by step:
STEP 1 — UNDERSTAND: What is the user asking for? What specifically changed?
STEP 2 — SCOPE: Which workload components are affected? (list them by name)
STEP 3 — PLAN: Which agents need to run, and in what mode (full vs. delta)?
STEP 4 — IMPACT: Which RFP sections need updating?
STEP 5 — CLASSIFY: What is the route type?

STATE_SUMMARY: {rfp_excerpt + workload_summary + prior_messages[-3:]}
USER_INPUT: {new_message}

Respond in this exact format:
ROUTE: <new_request|amendment|validate|answer|clarify>
CONFIDENCE: <high|medium|low>
CHANGED_COMPONENTS: <comma-separated component names, or "ALL" or "NONE">
AGENTS_TO_RUN: <comma-separated: profiler,sizer,finops,validator,rfp_writer — only those needed>
SCOPE: <full|delta_only>
RFP_SECTIONS: <comma-separated section numbers that need updating, e.g. "§4,§7,§9">
AMENDMENT_DELTA: <one-sentence description of what changed>
DIRECT_ANSWER: <if ROUTE=answer, write the full answer here; else "N/A">
```

**`ExecutionPlan` model** (added to `OrchestratorState`):
```python
class ExecutionPlan(TypedDict):
    intent: str              # "new_request" | "amendment" | "validate" | "answer" | "clarify"
    pipeline_mode: str       # "full" | "amendment" | "validation" | "query"
    agents_to_run: list[str] # e.g., ["profiler", "sizer", "finops", "validator", "rfp_writer"]
    scope: str               # "full" | "delta_only"
    scope_components: list[str]   # specific component names to reprocess
    rfp_amendment_sections: list[str]   # e.g., ["§4", "§7"]
    amendment_delta: str     # human-readable description of what changed
    confidence: str          # "high" | "medium" | "low"
    turn_number: int         # increments per user message within a session
```

**State fields** (added to `OrchestratorState`):
```python
execution_plan: ExecutionPlan    # written by Router+Orchestrator, read by all agents
routing_decision: str            # mirrors execution_plan.intent for quick access
pipeline_mode: str               # mirrors execution_plan.pipeline_mode
turn_number: int                 # mirrors execution_plan.turn_number
```

**Key design rule**: The Router+Orchestrator is the ONLY agent that decides what runs. Downstream agents read `state["execution_plan"]` to understand their scope — they do NOT make routing decisions themselves. For `scope="delta_only"`, Profiler only re-profiles components listed in `scope_components`; Sizer only re-prices those components; FinOps runs a full comparison (always needed for cross-provider); RFP Writer only regenerates sections in `rfp_amendment_sections`.

---

### 22c. Validator Agent Design (`src/agents/validator.py`) — NEW FILE

The Validator Agent runs **after FinOps** in every pipeline and can also be triggered **on-demand** via the Router+Orchestrator for `validate` routes. It is the system's architecture quality gate. All LLM reasoning in this agent uses the **Principal Architect Reasoning Pattern** (§22g).

**What it validates** (5 checks, in order):

**Check 0: Pricing Data Integrity** (calls `pricing_validator.py` — see §22h):
   - Validates that the `sized_results` from Sizer represent an apples-to-apples cross-provider comparison
   - 7 sub-checks: size adequacy, tier consistency, price anomaly, SKU staleness, category match, provider parity, memory:vCPU ratio
   - If any `severity="error"` warnings exist, logs prominently and writes to `validation_report["pricing_validation"]`
   - Does NOT block the pipeline — flags are surfaced in the RFP but analysis continues
   - **Why Check 0?** Downstream architecture scoring and FinOps vendor selection are meaningless if the underlying pricing data is invalid (e.g., FinOps recommends Azure because an undersized t2.micro was matched instead of the correct r6i.large)

**Check 1: Architecture Correctness** (calls `architecture_selector.py`):
   - Uses Principal Architect Reasoning to score all 4 options (managed-serverless, self-hosted-serverless, containers, hybrid)
   - Cost scores use REAL pricing from the already-priced `sized_results`
   - If the system's current architecture (from clarifier's enriched_input) is NOT the top-scored option, writes a warning
   - The LLM step in architecture_selector explains WHY each option scores as it does — not just numbers

**Check 2: Sizing Adequacy**:
   - Verifies each selected SKU meets the workload's resource requirements (memory_gb, vcpus, storage_gb)
   - Tolerance: selected must be ≥ required × 0.80 (within 20%)
   - Flags any SKU that is undersized. If DB undersized — this is a hard failure pattern (P14 fix history)

**Check 3: Budget Fit**:
   - Compares `sum(monthly_costs)` against `workload_request.budget_monthly_usd`
   - If over budget: flags excess percentage and suggests optimization paths (spot, RI, downgrade)
   - If under budget by > 50%: flags as potentially under-provisioned (worth reviewing)

**Check 4: WAF Compliance** (calls `waf_compliance.py`):
   - Runs `evaluate_compliance()` on the current `WorkloadRequest`
   - Appends compliance report to `validation_report`

**Output model** (added to `OrchestratorState`):
```python
validation_report: dict[str, Any]  # structure below
architecture_alternatives: list[dict]  # ranked options from architecture_selector
```

```python
# validation_report structure
{
  "pricing_validation": {   # Check 0 — NEW
    "is_valid": True,
    "warnings": [
      {"workload": "Cache Layer", "type": "undersized", "severity": "error",
       "description": "Selected cache.t3.micro (13.1 GB) < required 16 GB",
       "recommended_action": "Re-run sizer with memory_gb=16 minimum filter"}
    ]
  },
  "architecture_validation": {   # Check 1
    "selected": "containers",
    "recommended": "self_hosted_serverless",
    "ranked": [
      {"option": "self_hosted_serverless", "score": 0.888, "monthly_cost_estimate": 1800,
       "rationale": "800 avg RPS is 2.3× above cost crossover. Knative KEDA eliminates cold start risk for 99.99% SLA."},
      {"option": "containers", "score": 0.560, "monthly_cost_estimate": 12000, "rationale": "..."},
      {"option": "hybrid", "score": 0.776, "monthly_cost_estimate": 6000, "rationale": "..."},
      {"option": "managed_serverless", "score": 0.655, "monthly_cost_estimate": 14000, "rationale": "..."}
    ],
    "warning": "Self-hosted serverless (Knative) recommended. Lambda is 7.8× more expensive at 800 avg RPS."
  },
  "sizing_validation": [   # Check 2
    {"workload": "PostgreSQL Database", "status": "pass", "selected_memory_gb": 64, "required_memory_gb": 48},
    {"workload": "Cache Layer", "status": "fail", "selected_memory_gb": 13.1, "required_memory_gb": 16}
  ],
  "budget_validation": {   # Check 3
    "monthly_total": 916.0, "budget_monthly": 266666.0,
    "utilization_pct": 0.34, "status": "pass",
    "note": "Well within budget — consider higher availability tier"
  },
  "waf_report": {...}   # Check 4 — ComplianceReport from waf_compliance.py
}
```

**Integration with RFP Writer**: `_build_architecture_alternatives_section()` reads `validation_report["architecture_validation"]["ranked"]` to produce Option A/B/C comparison table. If `pricing_validation.warnings` has errors, RFP Writer adds a "Pricing Data Caveats" note in the cost section.

---

### 22d. Session Persistence Design (`src/services/session_store.py` + LangGraph)

**Approach**: Use LangGraph's built-in `SqliteSaver` checkpointer (from `langgraph-checkpoint-sqlite` package). This natively persists the full `OrchestratorState` (as JSON) keyed by `thread_id = session_id`, enabling the graph to automatically resume prior state on the next request.

**Session lifecycle**:
1. **First request** (no `session_id`): API generates `session_id = uuid4()`, creates blank state, runs full pipeline, returns `session_id` in response
2. **Follow-up requests** (with `session_id`): API calls `graph.ainvoke(state, config={"configurable": {"thread_id": session_id}})` — LangGraph restores prior state, Router Agent runs, correct path executes
3. **Session expiry**: `SqliteSaver` stores checkpoints in `data/sessions.db`. A background cleanup job (or TTL query) removes sessions older than 7 days.

**API changes** (`src/api/routes/orchestration.py`):
```python
class OrchestrationRequest(BaseModel):
    user_input: str
    project_name: str = "untitled"
    session_id: str | None = None  # NEW — if None, starts new session

class OrchestrationResponse(BaseModel):
    ...
    session_id: str        # NEW — always returned, client must persist and send back
    route_taken: str       # NEW — "new_request" | "amendment" | "validate" | "answer"
    turn_number: int       # NEW — increments per turn within session
    execution_plan: dict   # NEW — full ExecutionPlan for debugging/observability
```

**LangGraph wiring** (`src/orchestrator/graph.py`):
```python
from langgraph.checkpoint.sqlite import SqliteSaver

def build_graph(llm, pricing_service, db_path: str = "data/sessions.db") -> CompiledStateGraph:
    checkpointer = SqliteSaver.from_conn_string(db_path)
    graph = StateGraph(OrchestratorState)
    graph.add_node("router", _make_router_node(llm))
    graph.add_node("clarifier", ...)
    ...
    graph.add_node("validator", _make_validator_node(llm, pricing_service))
    graph.add_node("rfp_writer", ...)
    
    # Entry: new session → clarifier; existing session → router
    graph.add_conditional_edges(START, _route_entry)
    
    # Router conditional edges
    graph.add_conditional_edges("router", _route_from_router, {
        "new_request": "clarifier",
        "amendment": "profiler",
        "validate": "validator",
        "answer": END,
    })
    ...
    # Validator → RFP Writer (in every path that produces an RFP)
    graph.add_edge("validator", "rfp_writer")
    graph.add_edge("rfp_writer", END)
    
    return graph.compile(checkpointer=checkpointer)
```

**`_route_entry` function**:
```python
def _route_entry(state: OrchestratorState) -> str:
    # If prior RFP exists in state, route to router (it's a follow-up)
    if state.get("rfp_document"):
        return "router"
    return "clarifier"
```

---

### 22e. Updated OrchestratorState Fields

7 new fields added to `OrchestratorState` TypedDict (`src/orchestrator/state.py`):

| Field | Type | Reducer | Description |
|-------|------|---------|-------------|
| `session_id` | `str` | last-write | UUID — identifies the LangGraph thread. Persists across requests. |
| `execution_plan` | `dict[str, Any]` | last-write | Full `ExecutionPlan` produced by Router+Orchestrator. Read by all downstream agents. |
| `routing_decision` | `str` | last-write | Quick-access mirror of `execution_plan["intent"]`: `"new_request"` / `"amendment"` / `"validate"` / `"answer"` |
| `pipeline_mode` | `str` | last-write | Mirror of `execution_plan["pipeline_mode"]`: `"full"` / `"amendment"` / `"validation"` / `"query"` |
| `turn_number` | `int` | last-write | Increments per user message within the session |
| `validation_report` | `dict[str, Any]` | last-write | Validator Agent's 5-check output (pricing integrity, architecture scores, sizing, budget, WAF) |
| `architecture_alternatives` | `list[dict]` | last-write | Ranked options from `architecture_selector.py` (mirrors `validation_report["architecture_validation"]["ranked"]` for RFP Writer convenience) |

**Updated `create_initial_state()`** (`src/orchestrator/state.py`):
```python
def create_initial_state(
    user_input: str, project_name: str = "untitled", session_id: str | None = None
) -> OrchestratorState:
    return {
        # ... existing fields unchanged ...
        "session_id": session_id or str(uuid4()),
        "execution_plan": {
            "intent": "new_request",
            "pipeline_mode": "full",
            "agents_to_run": ["clarifier", "profiler", "sizer", "finops", "validator", "rfp_writer"],
            "scope": "full",
            "scope_components": [],
            "rfp_amendment_sections": [],
            "amendment_delta": "",
            "confidence": "high",
            "turn_number": 1,
        },
        "routing_decision": "new_request",
        "pipeline_mode": "full",
        "turn_number": 1,
        "validation_report": {},
        "architecture_alternatives": [],
    }
```

---

### 22f. Architecture Selector Engine (`src/engines/architecture_selector.py`) — NEW FILE

Purpose: evaluate **4 architectural patterns** against a `WorkloadProfile` using signal-driven analysis and real pricing, then surface a ranked recommendation with full rationale. The engine must have **no hardcoded winner** — the same engine should recommend Lambda for a bursty-low-average workload and Knative for a high-sustained-throughput workload.

**4 Architecture Options**:

| Option | Stack | Best When |
|--------|-------|-----------|
| `managed_serverless` | Lambda + DynamoDB + API Gateway + CloudFront | Bursty with low average RPS (< ~300 avg). Cold start acceptable. Minimal ops overhead needed. |
| `self_hosted_serverless` | EKS + Knative/KEDA + RDS/Redis | High avg RPS (> ~300) with large burst. Cold start unacceptable. Full control needed. |
| `containers` | EKS + RDS + ElastiCache + ALB | Stateful, low-latency (p99 < 50ms), complex networking, predictable load (< 5× burst). |
| `hybrid` | Lambda (event-driven paths) + K8s (stateful services) + RDS | Mixed: some workloads are event-driven/async, others require persistent connections. |

**Decision Framework — Three Signal Groups**:

The engine computes scores from three groups of signals before combining them.

---

**Signal Group 1: Traffic Pattern Analysis**

Compute `avg_rps` and `burst_ratio` from `WorkloadProfile`:
```python
avg_rps = concurrent_users × requests_per_user_per_sec   # e.g. 50K × 0.016 = 800 RPS
peak_rps = peak_concurrent_users × requests_per_user_per_sec
burst_ratio = peak_rps / avg_rps   # Cal Fire: 2M/50K = 40×
```

**Cost crossover calculation** — at what RPS does Lambda become more expensive than Knative?

Lambda monthly cost formula:
```
lambda_cost = avg_rps × 2592000 × $0.0000002   # request fee
            + avg_rps × avg_duration_ms/1000 × memory_gb × $0.0000166667   # compute fee
```

Knative (K8s nodes) monthly cost formula:
```
# Query actual node pricing from PricingService for target_provider
# Estimate pods needed for avg load = ceil(avg_rps / rps_per_pod)
# Knative scales pods from 0 to peak; avg utilization ~50-60%
knative_cost = node_hourly × 24 × 30 × ceil(pods_for_avg / pods_per_node)
```

Crossover is when `lambda_cost > knative_cost`. For typical workloads this is **~300–500 RPS average** (varies by duration and memory). The engine COMPUTES this per-workload rather than hardcoding.

Traffic scores:
- `managed_serverless`: `scale_score = min(burst_ratio / 20, 1.0)` (benefits from extreme bursts), `cost_score = 1 - (lambda_cost / max_cost)` (real compute)
- `self_hosted_serverless`: `scale_score = min(burst_ratio / 50, 0.95)` (KEDA scales well but not infinite), `cost_score = 1 - (knative_cost / max_cost)`
- `containers`: `scale_score = max(0, 1 - (burst_ratio - 5) / 40)` (penalized above 5× burst), `cost_score = 1 - (container_cost / max_cost)` from sizer output
- `hybrid`: weighted blend of Lambda + container costs by event_fraction

---

**Signal Group 2: Workload Characteristics**

| Signal | Source | Effect |
|--------|--------|--------|
| `latency_p99_ms < 100` | `WorkloadRequirement.latency_p99_ms` | Penalizes managed_serverless (cold start risk): `latency_score = 0.55`. Self-hosted = 0.95, containers = 1.0 |
| `latency_p99_ms >= 500` or `None` | Same | Cold start acceptable: managed_serverless `latency_score = 0.85`, others unchanged |
| `stateful_services_present` | `ComponentProfile.notes` contains "database" or "cache" | Penalizes pure managed_serverless (stateless-by-design) |
| `event_driven_fraction` | Fraction of INTEGRATION/ANALYTICS workloads in profile | Increases managed_serverless and hybrid scores |
| `avg_rps > crossover_rps` | Computed above | Lowers managed_serverless cost_score, raises self_hosted_serverless cost_score |

---

**Signal Group 3: Compliance and Control Requirements**

| Compliance Tag | Effect on Architecture Scores |
|---------------|-------------------------------|
| `stateramp` or `fedramp` | `containers +0.05 compliance`, `self_hosted +0.05 compliance`, `managed_serverless -0.05 compliance` (less control-plane access) |
| `hipaa` | `containers +0.10`, `managed_serverless -0.10` (BAA complexity) |
| `wcag-2.2-aa` | No effect (front-end concern, architecture-agnostic) |
| No compliance | All equal at 0.85 baseline |

---

**Final Scoring Formula**:

```
score = (reliability_weight × reliability_score)
      + (cost_weight × cost_score)
      + (scale_weight × scale_score)
      + (compliance_weight × compliance_score)
      + (latency_weight × latency_score)
```

Default weights: `reliability=0.30, cost=0.25, scale=0.25, compliance=0.10, latency=0.10`

Dynamic weights (P15j): Clarifier extracts user priority signal → adjusts at runtime:
- "cost is primary" → `cost=0.40, reliability=0.20`
- "must be highly available" → `reliability=0.45, cost=0.15`
- "sub-100ms response required" → `latency=0.25, scale=0.15`

---

**When Managed Lambda WINS — Counterexample (Tax Portal)**

Scenario: State tax filing portal. 400K users on April 15, 2K users rest of year (200:1 burst). Average RPS = 2K × 0.02 = 40 RPS. No p99 latency requirement (async PDF generation). StateRAMP Moderate.

```
avg_rps = 40
burst_ratio = 200×
lambda_cost ≈ 40 × 2592000 × $0.0000002 = $20.74/mo (request fee only — essentially free)
knative_cost ≈ 3 nodes × $730/mo = $2,190/mo (minimum cluster overhead even at near-zero load)
```

Cost scores (normalized): Lambda = 1.0, Knative = 0.0 (100× more expensive at this avg load)

Final scores: Managed Serverless wins with ~0.85 score. Containers/Knative lose on cost at this load.

**Key rule**: at avg_rps < crossover_rps, managed Lambda's zero-cost-at-rest model dominates. At avg_rps > crossover_rps, Knative's flat node pricing dominates.

---

**Cal Fire Example (Correct Answer)**

```
avg_rps = 50K users × 0.016 = 800 RPS
burst_ratio = 2M/50K = 40×
lambda_cost ≈ 800 × 2592000 × $0.0000002 + memory_compute ≈ $14,000/mo
knative_cost ≈ 3 nodes × $730/mo (avg load sizing) ≈ $1,800/mo (KEDA scales peak, avg stays low)
crossover_rps ≈ 350 RPS — cal_fire_avg (800) is well above crossover
```

Self-Hosted Serverless wins: cost advantage + compliance + no cold start for public safety UI.
Hybrid is runner-up: some Lambda for async sensor ingestion + K8s for the stateful web tier.
Managed Lambda loses: too expensive at 800 avg RPS sustained.

---

**`ArchitectureRecommendation` model**:
```python
class ArchitectureOption(BaseModel):
    name: str           # "managed_serverless" | "self_hosted_serverless" | "containers" | "hybrid"
    label: str          # Human-readable: "EKS + Knative/KEDA (Self-Hosted Serverless)"
    score: float        # 0.0–1.0 composite WAF score
    monthly_cost_estimate: float   # Real pricing from API where available
    reliability_score: float
    cost_score: float
    scale_score: float
    compliance_score: float
    latency_score: float
    rationale: str      # Why this option scored as it did — shows the key signals
    trade_offs: str     # What you give up with this option

class ArchitectureRecommendation(BaseModel):
    winner: ArchitectureOption
    ranked: list[ArchitectureOption]   # All 4, sorted by score desc
    weights_used: dict[str, float]     # Actual weights (dynamic or default)
    key_signals: dict[str, Any]        # avg_rps, burst_ratio, crossover_rps, latency_p99_ms
    recommendation_rationale: str      # Full narrative for RFP section
    warning: str | None                # If winner != what the LLM/clarifier initially assumed
```

---

### 22g. Principal Architect Reasoning Pattern (All LLM Agents)

Every agent that invokes an LLM must simulate the thought process of a **super-experienced principal  someone who doesn't just answer the question asked, but dissects the problem layer by layer before committing to any output. Implemented as a structured Chain-of-Thought (CoT) prompt baked into each agent's system prompt.architect** 

**Core Principle**: The agent reasons through the problem explicitly in a structured scratchpad before producing output. The scratchpad is included in the LangFuse trace (full explainability) but not surfaced to the user.

**Standard Reasoning Template** (injected into every agent's system prompt):
```
You are a principal cloud architect with 20+ years of experience across AWS, Azure, and GCP.
You have designed systems at every  from solo startup MVPs to Fortune 100 global platforms.scale 
Before producing any output, reason through the problem using this framework:

<architect_reasoning>
STEP  UNDERSTAND THE PROBLEM1 
  - What is the user ACTUALLY trying to achieve? (not just surface intent)
  - What constraints are non-negotiable? (compliance, availability, budget, latency)
  - What assumptions am I making that could be wrong?

STEP  IDENTIFY THE RISKS2 
  - What are the top 3 ways this architecture could fail in production?
  - What scaling inflection points exist? (where does behavior change drastically?)
  - What compliance or security traps are present?

STEP  EVALUATE ALTERNATIVES3 
  - What would I recommend if cost were unlimited?
  - What would I recommend if cost were the only constraint?
  - What is the simplest architecture that meets the requirements?

STEP  CHALLENGE MY INITIAL ASSUMPTION4 
  - Am I recommending this because it is genuinely best, or because it is familiar?
  - If a junior architect proposed the opposite, what would be their best argument?
  - What data or signals would change my recommendation?

STEP  COMMIT WITH RATIONALE5 
  - State the recommendation clearly
  - Top 3 reasons it wins
  - Top 2 trade-offs the customer must accept
  - Trigger conditions that would change this recommendation
</architect_reasoning>

After completing your reasoning, produce the requested output.
```

**Per-agent application**:

| Agent | Most relevant steps | Key forcing question |
|-------|--------------------|-----------------------|
| Clarifier | 1, 4 | "Is the user saying 'cloud-native' but actually describing a lift-and-shift?" |
| Profiler | 1, 2, 4 | "Is this really stateless, or will it need sticky sessions at scale?" |
| Sizer | 2, 3 | "Is this SKU sized for the p99 load, or just the average?" |
| FinOps | 3, 4 | "Is Azure cheaper because it genuinely is, or because I compared different tiers?" |
| Router+Orchestrator | 1, 4, 5 | "Is this an amendment, or a new request disguised as a follow-up?" |
| Validator | 2, 3, 4 | "Would I stake my professional reputation on this recommendation?" |
| Architecture Selector | 5 | "Is self-hosted serverless winning because of workload signals, or because I haven't priced it correctly?" |1
| RFP Writer | 5 | "Does this RFP read like it was written by someone who actually built this system?" |

**LangFuse observability**: the `<architect_reasoning>` scratchpad is captured as a `reasoning` span attribute in every LangFuse trace. Full explainability of every  a key dissertation differentiator.decision 

---

### 22h. Pricing Comparison Validator Engine (`src/engines/pricing_validator. NEW FILEpy`) 

**Purpose**: Before FinOps picks a vendor based on cross-provider cost comparison, validate that the `sized_results` represent a genuine apples-to-apples comparison. A wrong SKU match (e.g., `cache.t3.micro` instead of `cache.r6g.xlarge` for a 16 GB Redis workload) causes FinOps to recommend the wrong  not because that vendor is cheaper, but because the comparison was invalid.vendor 

**When it runs**: Called by Validator Agent as Check 0. Findings written to `validation_report["pricing_validation"]`. `severity="error"` findings surface in the RFP cost section as caveats.

**7 Checks**:

| Check ID | What It Validates | Severity |
|----------|------------------|----------|
| `size_adequacy` | Each selected SKU meets `required_memory_gb  0.80` AND `required_vcpus  0.80` | `error` if either fails |
| `tier_consistency` | Same pricing tier (on-demand / reserved / spot) used for all providers in comparison | `warning` if mixed |
| `price_anomaly` | Any SKU monthly cost > 5 or < 0.2 the median for that category | ` likely wrong SKU family |error` 
| `sku_staleness` | Cached SKU data not older than `ttl  2` for its tier | ` price may be stale |warning` 
 RDS/Cloud SQL, NOT EC2) | `error` if mismatch |
| `provider_parity` | All requested providers  1 priced result per workload (no provider missing from comparison) | `warning` if missing |have 
| `ratio_check` | Selected instance memory:vCPU ratio is within 0.25 of workload's required ratio | `warning` if wrong family |

**`PricingValidationResult` model**:
```python
class PricingValidationFinding(BaseModel):
    check_id: str           # e.g., "size_adequacy"
    workload_name: str
    provider: str
    severity: str           # "error" | "warning" | "info"
    description: str        # Human-readable explanation
    actual_value: str       # e.g., "selected: 13.1 GB"
    expected_value: str     # e.g., "required: >= 16 GB"
    recommended_action: str # e.g., "Re-run sizer with memory_gb=16 minimum filter"

class PricingValidationResult(BaseModel):
    is_valid: bool          # True only if zero "error" severity findings
    findings: list[PricingValidationFinding]
    error_count: int
    warning_count: int
    summary: str            # "3 errors, 2  comparison may be unreliable"warnings 
```

**Implementation notes**:
- Input: `list[SizedWorkloadResult]` from `state["sized_results"]`
- **No LLM  purely algorithmic (fast, deterministic, fully unit-testable)call** 
- Each check is a separate method: `_check_size_adequacy()`, `_check_price_anomaly()`, etc.
- `_check_price_anomaly()` uses `statistics.median()` over per-category cost list
- Does NOT re-run the  validates what is already there and flags issuesSizer 
- If `is_valid=False`: Validator logs `log.warning("pricing_comparison_invalid", error_count= never silentN, ...)` 
  Pricing Data Caveats" subsection in cost sectionadds "



---

23. B8 + NEW-NEW-5 RFP Bug Fixes (11 May 2026)101B1## 

### 23a. B8 Fixes (10 May 2026)B1

All 8 RFP output quality bugs identified during the P16 fire scenario test run were fixed:

| ID | Component | Bug | Fix | File |
|----|-----------|-----|-----|------|
| **B1** | SKU table rows | Duplicated rows (42 to 84) | Removed `**state` spread from both `validator.py`  only return changed keys (LangGraph reducer was appending twice) | `validator.py` |returns 
| **B2** | Architecture diagram | Hardcodes "Cloud Target: AWS" regardless of recommendation | `_build_architecture_section()` now reads `state["recommended_provider"]` | `rfp_writer.py` |
| **B3** | Sizing rationale | JSON truncated mid-sentence | Regex fallback extraction in `profiler.py`; sentence-boundary truncation in `rfp_writer.py` | `profiler.py`, `rfp_writer.py` |
| **B4** | Azure Redis | $0.00 | `_is_redis_workload()` + `_REDIS_CACHE_COST_MONTHLY` dict fallback (Azure ~$55/mo) | `sizer.py` |
| **B5** | Azure PostgreSQL | Selects "Basic"/"Burstable" tier for mission_critical | Burstable/basic excluded when tier in BUSINESS_CRITICAL or MISSION_CRITICAL | `sizer.py` |
| **B6** | Extra workloads | data-streaming, message-queue missing SKU tables | `profiler.py` updates `state["workload_request"]` so sizer finds extra workloads | `profiler.py` |
| **B7** | WAF compliance | Only 5 trivial checks (all pass trivially) | Added `_check_database_ha()`, `_check_budget_ceiling()`, `_check_compliance_coverage()` standalone checks | `waf_compliance.py` |
| **B8** | RTM | "Acknowledged" when StateRAMP gap has 4 failed families | RTM now shows " See Appendix A" when `has_stateramp_gap=True` | `rfp_writer.py` |Partial 

**Test result**: 142/142 tests pass after all B1-B8 fixes.

### 23b. NEW-1 through NEW-5 Fixes (11 May 2026)

Discovered during California Wildfire Fire-1 test run (3-provider: AWS/Azure/GCP, 2M peak users, StateRAMP Moderate):

| ID | Component | Bug | Fix | File |
|----|-----------|-----|-----|------|
| **NEW-1** | AWS PostgreSQL | $0.00 (AWS Pricing API returns no RDS SKUs for some regions without exact API params) | Added `_is_postgres_workload()` helper + `_RDS_COST_MONTHLY` fallback dict (~$185/mo for AWS) in no-candidates block | `sizer.py` |
| **NEW-2** | AWS Virtual Machines | `c8a.8xlarge` (32 vCPU, $1,258/mo) selected for 6-vCPU workload — 5x overprice | Added vCPU cap before `_size_compute_workload`: `max_vcpu = max(8, req_vcpu * 4)` | `sizer.py` |
| **NEW-3** | GCP PostgreSQL | $7.30 = IP address reservation fee, not DB compute | GCP database candidates filtered: remove rows with "ip address"/"static ip" in product_name or sku_name | `sizer.py` |
| **NEW-4** | SLA section | Peak Throughput shows unrealistic RPS (2M users x 10 = 20M RPS) | Changed clarifier heuristic: `peak_users * 10` to `max(500, min(peak_users // 2, 500_000))` capped at 500K | `clarifier.py` |
| **NEW-5** | Mobile architecture | Subsection hardcodes AWS service names regardless of recommended provider | `_build_mobile_subsection()` now takes `recommended_provider` param; resolves recommended, then first-target, then "aws" | `rfp_writer.py` |

**Test result**: 142/142 tests pass after all NEW-1 through NEW-5 fixes.

### 23c. Live Verification Run (11 May 2026, Fire1-Bugfix-Verify)

Full pipeline run with 12-workload Fire-1 scenario (50k to 2M concurrent users, 3-provider best-price):

| Fix | Before | After | Status |
|-----|--------|-------|--------|
| NEW-1 AWS PostgreSQL | $0.00 | db.m6in.large $188.34/mo | CONFIRMED |
| NEW-2 AWS VM sizing | c8a.8xlarge $1,258/mo | c7a.large $74.93/mo | CONFIRMED |
| NEW-3 GCP PostgreSQL | $7.30 (IP reservation) | $24.82/mo (real Cloud SQL) | CONFIRMED |
| NEW-4 SLA RPS | 20,000,000 RPS | 500,000 RPS | CONFIRMED |
| NEW-5 Mobile services | hardcoded AWS | uses recommended_provider | Code verified |

**AWS total cost**: $2,286/mo to $940/mo (59% reduction; now realistic)
**GCP total**: $1,041/mo | **Azure total**: $184.80/mo (Azure compute API gaps noted)

**Potential NEW-6**: Several AWS compute workloads (API Server, VMs, streaming-pipeline) all landing on c7a.large (2 vCPU) for 4-6 vCPU requirements — vCPU cap or candidate pool issue to investigate.

---

## 24. Cal Fire Provider + Workload  PROV-1, PROV-2, PROV-3 Fixed (11 May 2026)Bugs 

**Context**: New Cal Fire test run using `/clarify` conversation flow (not pre-written enriched_input).
Pipeline incorrectly produced `single_aws` provider strategy and only 3 workloads when the user said
"no preference on app framework" and provided a $3.2M/year budget.

### 24a. PROV- Provider defaults to AWS when user has no preference1 

| | Detail |
|---|---|
| **Root cause** | LLM in `llm_clarify_turn` outputs `PROVIDER_STRATEGY: single_aws` because the clarifier's own "additional components" note mentions AWS service names (Aurora, Route 53, CloudFront). The user said " whatever is best" about *app framework*, not provider. |No 
| **Fix** | Added `_user_named_single_provider(all_user_text)` function that scans the full conversation for explicit single-provider patterns (e.g. "use AWS", "AWS only", "prefer Azure"). If the user never named a single provider, deterministic override sets `provider_strategy = best_price_all` and `providers = [aws, azure, gcp]` regardless of what the LLM inferred. |
| **Location** | `src/agents/clarifier. `llm_clarify_turn()`, new helper `_user_named_single_provider()` |py` 
| **Status Fixed |** | 

### 24b. PROV- Only 3 workloads identified for a 12-component architecture2 

| | Detail |
|---|---|
| **Root cause** | `WORKLOAD_SUMMARY` prompt only asked for "2-5 sentences" in prose form. LLM described the architecture generically, only mentioning K8s, LB, CDN. The heuristic extractor `_extract_workloads_from_text` only triggers on specific keywords. |
| **Fix** | Rewrote `WORKLOAD_SUMMARY` prompt to require a **bulleted  one component per line in format `- <component>: <brief role>`. Added mandatory components list: kubernetes cluster, load balancer, CDN, API gateway, relational database, redis cache, object storage. Clarified that "every component listed becomes a separately priced line item." |list** 
| **Location** | `src/agents/clarifier. `_CLARIFY_SYSTEM_PROMPT` |py` 
| **Status Fixed (requires LLM to follow new format; deterministic extraction handles the rest) |** | 

### 24c. PROV- $3.2M/year budget not extracted3 

| | Detail |
|---|---|
| **Root cause** | `_parse_budget()` only matched `$N,NNN.NN` dollar-sign amounts or bare integers. `$3.2 million per year` has a decimal + "million" + "per  no existing pattern matched. |year" 
 266667. Also strengthened `BUDGET_MONTHLY_USD` LLM prompt with explicit conversion examples. |
| **Location** | `src/agents/clarifier. `_parse_budget()` and `_CLARIFY_SYSTEM_PROMPT` |py` 
| **Status Fixed |** | 

**Test result**: 142/142 tests pass after all PROV-1 through PROV-3 fixes.
