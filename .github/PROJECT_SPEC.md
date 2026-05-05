# Cloud Orchestrator IDSS — Project Specification

> **Author**: Ramkumar J · BITS ID: 2024MT03027 · M.Tech Cloud Computing, BITS Pilani WILP
> **Supervisor**: Rajkumar Sakthibalan (Presidio Solutions, Chennai)
> **Additional Examiner**: Santhosh Kirubakaran
> **Last Updated**: 5 May 2026 (Priority 12 fixes applied + fourth run attempted. Bugs 12a/12b/12c fixed in `sizer.py`; Bug 12d confirmed NOT a bug — budget `266,666.0` propagates correctly to NFR-05 in RFP. Fourth live run (request_id `4d134426`) shows fixes are in code but pricing data is zero because AWS STS credentials expired between run #3 and run #4. Need fresh AWS credentials to complete final live verification. 142/142 tests pass.)
> **LLM**: Gemini 2.5 Pro (`gemini-2.5-pro`) via Google Cloud Vertex AI (`dissertation-rj`, `us-central1`) using ADC

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
| `ServiceCategory` | Enum | **15 values** (P2 added `KUBERNETES`): `COMPUTE`, `SERVERLESS_COMPUTE`, `CONTAINER`, `KUBERNETES`, `SERVERLESS_FUNCTION`, `DATABASE`, `STORAGE`, `NETWORKING`, `AI_ML`, `ANALYTICS`, `MANAGEMENT`, `SECURITY`, `INTEGRATION`, `IOT`, `OTHER`. `KUBERNETES` = managed control-plane fee (EKS/AKS/GKE); distinct from `CONTAINER` (node/workload costs). |
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
| `clarifier.py` | ✅ | **~1370 lines. Fully implemented + P7+P8 fixes.** `run_clarifier_node` — single-pass heuristic + LLM enrichment. **P4/WAF/P6 additions**: `llm_clarify_turn()`, `_CLARIFY_SYSTEM_PROMPT` two-role architect, 19-field structured dict, `build_enriched_input_from_structured()`. **P7 fixes**: (7a) `_propagate_scale_and_sla()` — parses `Scale:`/`Availability:`/`DR requirements:` lines from enriched input and writes `concurrent_users`, `uptime_sla`, `throughput_rps` (peak×10 RPS), `scaling_pattern=BURSTY`, `rpo_minutes`, `rto_minutes` to all workloads; (7b) compliance frameworks propagated to `WorkloadRequirement.compliance_tags` after parsing; (7c) CDN+geospatial detection in `_extract_workloads_from_text()` — keywords cdn/gis/geospatial/map tiles/wildfire/emergency → CDN (NETWORKING, notes=cdn) + geospatial tile storage (STORAGE 5 TB); (7d) `_parse_explicit_values()` upgraded — distinct AWS/Azure/GCP region regexes update both `preferred_region` AND `provider_regions[provider]` so Sizer uses the correct region; `_parse_compliance_extended()` superset parser handles stateramp/fedramp/wcag/gdpr/soc2/nist/fisma/cmmc. Helpers: `_parse_peak_concurrent_users()`, `_parse_availability_sla()`, `_parse_rpo_rto()`. **P8 fixes**: (8a) AI/ML keyword matching changed from substring `"ai"/"ml"` to whole-word regex (`r'\b(ai|ml|llm|nlp|cv)\b'`) — prevents false positives from "availability", "reliability", "sustainability" etc; (8b) K8s without explicit count: when text also contains "microservices" or "containeris*", a CONTAINER workload is now added alongside the KUBERNETES cluster-mgmt-fee. **Validated**: Cal Fire E2E 10/10 ✅, 142/142 tests pass. |
| `profiler.py` | ✅ | **~730 lines. Fully implemented + P7e + P9f fixes.** Takes `WorkloadRequest` → produces `WorkloadProfile` with one `ComponentProfile` per workload. **Category resolution**: priority-ordered with `_guard_ai_ml()` — prevents AI_ML misclassification when `gpu_count == 0` and no explicit ML keywords. **P7e**: `_guard_ai_ml()` strips hallucinated GPUs from LLM enrichment. **Container-aware resource estimation**: millicore specs × replicas instead of defaults. **Cluster management fee**: `notes="cluster_management_fee"` → zero compute. **P9f fix**: `_estimate_resources()` now returns `vcpus=0, memory_gb=0.0` for `NETWORKING` (managed, billed per-request/LCU) and `STORAGE` (managed, billed per-GB) categories. STORAGE carries `storage_gb` through with tier multiplier. GPU detection from explicit intent only. Tier multipliers, environment scaling, instance-family recommendation, LLM rationale with heuristic fallback. `run_profiler_node(state, llm)`. `@observe()` + structlog. **Validated: 23/23 checks passed, 142/142 tests pass.** |
| `sizer.py` | ✅ | **~1250 lines. Fully implemented + P9 all bugs fixed.** Category-aware SKU selection: scored (COMPUTE, AI_ML) use `scoring.score_skus()`, binpacked (CONTAINER) use `bin_packing.pack_workloads()` with VM node SKUs, others use cheapest-price. **P9a fix**: `_size_container_workload()` computes `total_needed_vcpus` across all container workloads → `max_node_vcpus = max(8, total × 4)`; preferred families (m5/m6i/c5 etc.) sorted first via `_sort_key()`; prevents x8i.48xlarge selection for 3-vCPU workloads. **P9b fix**: DATABASE candidates filtered to `unit_of_measure in ("1 Hour", "1 hour")` — excludes RDS storage/IOPS/backup meters. **P9c fix**: `_DATABASE_ENGINE_MAP` now routes `redis`/`elasticache` → `AmazonElastiCache`; azure `redis` → `Azure Cache for Redis`; gcp `redis` → `Cloud Memorystore`. **P9d fix**: `_filter_storage_candidates()` excludes Glacier/EarlyDelete/Nearline rows; prefers standard/hot-tier rows. **P9e fix**: `_is_cdn_workload()` detects CDN by notes/name → `_CDN_COST_MONTHLY` {aws: $85, azure: $70, gcp: $60} fixed estimate. **P9f**: DATABASE hourly filter + STORAGE tier filter applied after candidate fetch. Container node pool fix, DB engine propagation, K8s/LB fixed costs, ancillary costs all retained. **Validated: 142/142 tests pass, 10/10 Cal Fire E2E.** |
| `finops.py` | ✅ | **~900 lines. P1d complete.** Groups `SizedWorkloadResult` by provider; resolves `[Infra]` prefixed workloads to NETWORKING bucket. Queries RI/spot pricing per SKU; when live data absent, applies industry-standard discount rate fallbacks (`_RI_DISCOUNT_RATES`: AWS 30/45%, Azure 35/50%, GCP 25/40%; `_SPOT_DISCOUNT_RATES`: AWS 70%, Azure 80%, GCP 60%). Spot eligibility filter: DATABASE, STORAGE, NETWORKING, MANAGEMENT, SECURITY excluded. Fixed-cost workloads (K8s fee, LB, `[Infra]` items) bypass discounting. `_compute_tco()` computes 1yr/3yr/5yr TCO with compound growth (default 15%/yr). TCO projections stored in `state[kpis][tco_projections]` and displayed in summary message. Savings opportunities always populated (estimate or live). `_FINOPS_SYSTEM_PROMPT` updated for TCO and savings guidance. `@observe()` + structlog throughout. |
| `rfp_writer.py` | ✅ | **~2100 lines. P1e + P7g + P10 complete.** Generates ~25,000-char enterprise Markdown RFP. **P10 additions**: `_is_government_scenario()` (detects gov/public-safety + StateRAMP/FedRAMP → changes title to "Proposed Cloud Solution"); `_is_greenfield_project()` (detects absence of migration keywords → switches delivery phases); `_is_mobile_scenario()` (detects mobile/iOS/Android → appends mobile architecture subsection); `_MANAGED_SERVICES` dict (3 providers × 14 categories with specific service names); `_build_managed_services_section()` (new §5 with per-provider tables); `_build_requirements_traceability_section()` (F-xx functional, C-xx compliance, NFR-xx rows — appended as §17 when compliance frameworks present); `_build_mobile_subsection()` (ASCII mobile data path + 6 principles); expanded `_build_certifications_section()` with WCAG 2.2 AA (11-row criteria table + testing approach), StateRAMP Moderate (7-step ATO pathway + control family highlights), FedRAMP/HIPAA/CJIS summary bullets; `_build_migration_section()` now greenfield-aware (Phase 1 MVP → Phase 2 UAT → Phase 3 Launch → Phase 4 Managed Ops). Updated ToC to 16 entries. **Bug fixed**: `comp.recommended_family` → `comp.recommended_instance_families[0]` (AttributeError on live run). **Test: 142/142 pass; Cal Fire live pipeline completes ✅** |
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
| `routes/orchestration.py` | ✅ | **~350 lines. All three endpoints fully implemented.** `POST /orchestrate` (full pipeline → JSON). `POST /orchestrate/stream` (SSE streaming). `POST /orchestrate/clarify` — **P4 rewrite**: removed `_QUESTION_TEXT`, `_pop_next_question()`, `_format_answer_ack()`, `_build_enriched_input()`. Imports `llm_clarify_turn` and `build_enriched_input_from_structured` from clarifier. Session store holds `{project_name, raw_input, history: [(role, content)]}`. On each turn: calls `llm_clarify_turn(llm, history, user_input)` → if `status=clarifying`, appends to history and returns LLM's response; if `status=ready`, calls `build_enriched_input_from_structured()` and returns `enriched_input` for the pipeline. LLM accessed via `request.app.state.llm`. LangFuse flushed in all finally blocks. |
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
