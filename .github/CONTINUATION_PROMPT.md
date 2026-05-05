# Cloud Orchestrator IDSS — Continuation Prompt

> **Last Updated**: 5 May 2026 (P13 fixes applied — AWS pricing cache poisoning root cause discovered and fixed. Per-service API filters added to `aws_provider.py` (EC2/RDS/ElastiCache/S3). `linux_only` fallback safety net, GPU exclusion, and ZIA pattern added to `sizer.py`. 8 RFP section numbers fixed in `rfp_writer.py`. Stale cache deleted. **Run #5 completed successfully** (254s, 44,057-char RFP, 100% WAF). 142/142 tests pass. **Next: refresh AWS credentials and run #6 to verify correct SKU selection.**)

---

## PASTE BELOW INTO NEW CHAT

```
I'm continuing work on my M.Tech dissertation project: "Agentic AI-Driven Intelligent Decision Support System for Cloud-Agnostic Resource Orchestration and Automated Procurement".

Project path: `/Users/ramkumarjayakumar/Dev/Dissertation/`
Student: Ramkumar J · BITS ID: 2024MT03027 · M.Tech Cloud Computing, BITS Pilani WILP

Please read `.github/copilot-instructions.md` first (tech stack rules, observability pattern, LLM abstraction rules). Then read `.github/PROJECT_SPEC.md` for complete per-file status, architecture diagram, and build order.

---

## What's Fully Built & Live-Tested ✅

### Infrastructure
- Python 3.13.0 · uv · hatchling · `.venv/` with all deps synced
- `pyproject.toml` · `uv.lock` · `.env.example` · `.gitignore` · git repo initialised

### Config Layer (`src/config/`)
- `settings.py` — 6 Pydantic-settings classes: AppSettings, LLMSettings, AWSSettings, AzureSettings, GCPSettings, LangFuseSettings. `get_settings()` with `@lru_cache`.
- `logging_config.py` — `configure_observability()` wires structlog JSON + LangFuse SDK v3. Graceful degradation when keys missing.

### LLM Factory (`src/llm/`)
- `factory.py` — `get_llm(provider, model, **kwargs) → BaseChatModel`. Lazy imports (bedrock / gemini / **vertexai**). Agents NEVER import provider classes directly.
- **`vertexai` provider**: `_create_vertexai()` passes `vertexai=True`, `project=project`, `location=location` to `ChatGoogleGenerativeAI`; reads `VERTEXAI_PROJECT` + `GCP_PROJECT_ID` (fallback) + `VERTEXAI_LOCATION` (default `us-central1`); uses ADC — no API key needed.
- **Live-tested**: `gemini-2.5-pro` via Google Cloud Vertex AI (`dissertation-rj`, `us-central1`) ✅
- **Gemini 3.1 Pro Preview**: `gemini-3.1-pro-preview` requires Model Garden enablement per GCP project — not yet accessible in `dissertation-rj`. Use `gemini-2.5-pro` until enabled via GCP Console → Vertex AI → Model Garden.

### Data Models (`src/models/`)
- `cloud_resource.py` — `CloudProvider` enum (aws/azure/gcp), `ServiceCategory` enum (**15 values** — P2 added `KUBERNETES` for managed control-plane fee, distinct from `CONTAINER` node costs). Legacy `ComputeSKU`/`StorageSKU` kept for backward compat with engines only.
- `pricing.py` — `PricingTier` enum (8 values). `NormalizedPriceItem` with `monthly_cost_estimate` property (handles reservation upfront vs hourly).
- `workload.py` — `EnvironmentType`, `WorkloadTier`, `ScalingPattern` enums. `ResourceSpec` (all 15 categories), `WorkloadRequirement` (**P2**: added `latency_p99_ms`, `throughput_rps`, `concurrent_users`, `uptime_sla`, `rpo_minutes`, `rto_minutes`, `data_growth_rate_pct`, `spot_eligible: bool = True`), `ComponentProfile`, `WorkloadProfile`, `WorkloadRequest`. Legacy VMWorkload/ContainerWorkload/StorageRequirement kept at bottom.
- `conversation.py` — `MessageRole`, `ClarificationStatus`, `ClarificationPriority` enums. `ChatMessage`, `ClarificationQuestion`, `ConversationState` (with `should_continue_clarifying` property).
- `recommendation.py` — **P2**: added `AncillaryCost` model (typed record for NAT/LB/transfer/K8s-mgmt costs); added `ancillary_costs: list[AncillaryCost]` to `ProviderCostBreakdown`. `PackedNode`, `BinPackingResult`, `CostComparison`, `ComplianceCheckResult`, `ComplianceReport`, `CloudRecommendation`. All use `NormalizedPriceItem`.
- **Verified**: 27/33 validate_all.py (up from 24/33; 6 remaining failures are all pre-existing unrelated to models)

### Cloud Provider Adapters (`src/providers/`)
- `base_provider.py` — Abstract `BaseCloudProvider`: `search_prices()`, `get_sku_prices()`, `list_regions()`
- `azure_provider.py` — Azure Retail Prices REST API (no auth). **Live-tested ✅**
- `aws_provider.py` — AWS Pricing API via boto3. 45 ServiceCode→ServiceCategory maps, 26 region↔location maps. **Live-tested 6/6 ✅**
- `gcp_provider.py` — GCP Cloud Billing Catalog via `google-cloud-billing` SDK. ADC auth (no service account key). **Live-tested 6/6 ✅**

### Pricing Cache & Service (`src/services/`)
- `pricing_cache.py` — Async SQLite cache (aiosqlite). WAL mode. Upsert/query/evict/stats.
- `pricing_service.py` — Cache-transparent facade. Per-tier TTLs: spot=6h, on-demand=24h, reserved=7d. `compare_across_providers()` for FinOps agent. Graceful degradation (stale cache on API failure).
- **Verified**: 25/25 checks passed. ~970× speedup (0.68s live → 0.0007s cached)

### Orchestrator State (`src/orchestrator/state.py`)
- `OrchestratorState` TypedDict with `Annotated[list, operator.add]` append-only reducers
- `AgentStatus` enum, `AgentExecution` model (timing + retry tracking)
- `SizedWorkloadResult` (fit_score, selected_sku, alternatives, rationale)
- `create_initial_state()` factory
- **Verified**: all imports, enum values, TypedDict fields, create_initial_state() ✅

### Clarifier Agent (`src/agents/clarifier.py`) — 596 lines
- Multi-turn requirement refinement loop
- 4 required + 4 recommended question templates
- Parsing helpers: `_parse_environment`, `_parse_tier`, `_parse_providers`, `_parse_budget`, `_parse_compliance`
- `_extract_workloads_from_text()` keyword-based bootstrap
- `run_clarifier_node(state, llm, pricing_service)` entry point
- `@observe()` + structlog throughout
- **Verified**: 12/12 checks passed ✅

### Profiler Agent (`src/agents/profiler.py`) — ~730 lines ✅ P9f DONE
- Takes `WorkloadRequest` → produces `WorkloadProfile` with `ComponentProfile` per workload
- **`_guard_ai_ml()`**: prevents AI_ML misclassification when `gpu_count == 0` and no explicit ML keywords
- **Container-aware resource estimation**: derives vCPU/memory from `cpu_request_millicores`/`memory_request_mb` × replicas (not defaults)
- **Cluster management fee**: workloads with `notes="cluster_management_fee"` → zero compute resources
- **Managed services zero-vCPU** (Bug 9f fix): `NETWORKING` and `STORAGE` categories now return `vcpus=0, memory_gb=0.0` — billed per-request/per-GB, not per-vCPU. STORAGE carries `storage_gb` through with tier multiplier applied.
- GPU detection from explicit `gpu_count > 0` ONLY (no longer auto-forced by AI_ML category)
- Priority-ordered category resolution, tier multipliers, environment scaling, instance-family recommendation
- LLM-enriched rationale with heuristic fallback on LLM failure
- `run_profiler_node(state, llm)` entry point
- `@observe()` + structlog throughout
- **Verified**: 23/23 checks passed, 142/142 tests pass ✅

- **Sizer Agent** (`src/agents/sizer.py`) — ~1470 lines ✅ P13 fixes applied
- Category-aware SKU selection: scored (COMPUTE, AI_ML), binpacked (CONTAINER with VM node SKUs), cheapest-price (all others)
- **Container node pool fix**: queries EC2/VMs/Compute Engine for node pools (not EKS/AKS/GKE service pricing)
- **Database engine propagation**: `_DATABASE_ENGINE_MAP` routes PostgreSQL/MySQL/etc. + **redis/elasticache → AmazonElastiCache** (Bug 9c fix)
- **Engine inference fallback** (Bug 11a fix): `_infer_engine_from_name()` parses engine from workload name when `resources.database_engine` is None
- **EC2 Linux OS filter** (Bug 11b fix): AWS candidates filtered to `operatingSystem=Linux` and `usagetype` not containing "UnusedBox"/"UnusedDed"
- **SQL license token filter** (Bug 12a fix ✅): `_SQL_LICENSE_TOKENS` checks `meter_name` to exclude SQL Standard/Enterprise/Web rows
- **linux_only silent fallback safety** (Bug 13b fix ✅): when `linux_only` filter produces empty list, `candidates` is set to `[]` — never silently reverts to garbage rows
- **GPU exclusion for non-GPU workloads** (Bug 13c fix ✅): `_GPU_PREFIXES` block (`g3–g7`, `p2–p5`, `trn`, `dl1`) excludes GPU instances when `not component.requires_gpu`
- **Storage quantity scaling** (Bug 11c fix): post-processing for STORAGE: `monthly = unit_price × storage_gb`
- **GIR exclusion** (Bug 12b fix ✅): `"-gir-"` and `"gir-bytehrs"` added to `_EXCLUDE_PATTERNS`
- **ZIA exclusion** (Bug 13d fix ✅): `"-zia-"` and `"zia-bytehrs"` added to `_EXCLUDE_PATTERNS` — S3 One Zone-Infrequent Access SKU uses acronym "ZIA"
- **Fixed-cost workloads**: K8s cluster management fee ($73/mo), load balancer ($18-22/mo), **CDN ($60-85/mo fixed estimate)**
- **Container vCPU ratio guard** (Bug 9a fix): `max_node_vcpus = max(8, total_needed_vcpus × 4)`; preferred families (m5/m6i/c5/c6i) sorted first
- **DATABASE hourly filter** (Bug 9b fix): RDS/ElastiCache candidates filtered to `unit_of_measure in ("1 Hour", "1 hour")`
- **STORAGE standard-tier filter** (Bug 9d fix): `_filter_storage_candidates()` excludes EarlyDelete/Glacier/GIR/Nearline/INT-AIA/ZIA rows
- **CDN detection** (Bug 9e fix): `_is_cdn_workload()` routes `notes="cdn"` or "cdn" in name to fixed-cost CDN path
- Ancillary costs: NAT gateway + data transfer estimates per provider
- **Verified**: 142/142 tests pass ✅

### FinOps Agent (`src/agents/finops.py`) — ~900 lines ✅ P1d DONE
- Groups `SizedWorkloadResult` by provider, queries RI/spot pricing, builds `ProviderCostBreakdown` per provider
- **`[Infra]` routing**: `_resolve_category_for_result()` now returns `NETWORKING` for any workload_name starting with `[Infra]` (NAT Gateway, Data Transfer injected by Sizer)
- **RI/Spot savings always populated**: queries live pricing first; when no data found, applies industry-standard fallback rates: AWS (30%/45%/70%), Azure (35%/50%/80%), GCP (25%/40%/60%) for 1yr-RI/3yr-RI/Spot
- **Spot eligibility filter**: DATABASE, STORAGE, NETWORKING, MANAGEMENT, SECURITY excluded; fixed-cost items (K8s fee, LB, `[Infra]`) bypass discounting entirely
- **TCO projections**: `_compute_tco(monthly, years, growth_pct)` computes 1yr/3yr/5yr with compound growth (default 15%/yr); stored in `state[kpis][tco_projections]`
- Updated LLM system prompt to explicitly request TCO + savings analysis
- LLM summary with heuristic fallback; summary message includes TCO line
- `run_finops_node(state, llm, pricing_service)` entry point
- `@observe()` + structlog throughout
- **Verified**: all imports OK, TCO math correct, [Infra] → NETWORKING routing correct ✅

### RFP Writer Agent (`src/agents/rfp_writer.py`) — ~2250 lines ✅ P11 DONE
- **17+ sections** producing ~25,000-char enterprise RFP
- **Gap 10a** ✅ — `_is_government_scenario()` detects Cal Fire / state / public-safety keywords + StateRAMP/FedRAMP compliance; changes document title from "Cloud Infrastructure Procurement" to "Proposed Cloud Solution" + doc type field
- **Gap 10b** ✅ — `_build_requirements_traceability_section()` — functional requirements table (F-01…) from workload components, compliance requirements table (C-01…) with WCAG/StateRAMP/FedRAMP/HIPAA/CJIS descriptions, NFR table (NFR-01–05) with HA/scaling/security/observability/cost requirements
- **Gap 10c** ✅ — `_is_greenfield_project()` detects absence of migration keywords; `_build_migration_section()` now emits contract-aligned phases (Phase 1 MVP → Phase 2 UAT → Phase 3 Launch → Phase 4 Managed Ops) for greenfield / government, legacy migration phases for migration projects
- **Gap 10d** ✅ — `_is_mobile_scenario()` detects mobile/iOS/Android/Flutter; `_build_mobile_subsection()` appends to architecture section — mobile data path diagram + offline-first/FCM/OAuth/WCAG/App Store principles
- **Gap 10e** ✅ — `_MANAGED_SERVICES` dict (3 providers × 14 categories) with specific service names (RDS Multi-AZ, EKS, ElastiCache, S3 Standard, CloudFront, etc.); `_build_managed_services_section()` renders per-provider tables with mobile/streaming rows when detected
- **Gap 10f** ✅ — `_build_certifications_section()` now detects wcag/stateramp/fedramp/hipaa/cjis frameworks and adds dedicated subsections: WCAG 2.2 AA table (11 success criteria with contrast/keyboard/ARIA specifics + testing approach), StateRAMP Moderate 7-step ATO pathway + 6 control family highlights, FedRAMP/HIPAA/CJIS summary controls
- **Bug 11d** ✅ — Fixed §11 collision: `_build_dr_section()` heading corrected to "## 12. Disaster Recovery & Business Continuity" (was duplicate §11)
- **Bug 11e** ✅ — Fixed Cache Layer → wrong managed service: component loop now detects cache/redis name keywords and uses `"CACHE"` key → "Amazon ElastiCache for Redis (cluster mode enabled)"
- **Verified**: 142/142 tests pass; §11 collision resolved; Cache Layer → ElastiCache correct ✅

### Algorithmic Engines (`src/engines/`) — migrated to new models
- `__init__.py` — Shared attribute extraction helpers: `extract_vcpus()`, `extract_memory_gb()`, `extract_gpu_count()`, `extract_generation()`. Handles standardised keys + AWS-style fallbacks.
- `vm_specs.py` — **NEW.** `parse_azure_vm_specs()` extracts vCPU/memory from ARM SKU names. `compose_gcp_vm_instances()` synthesizes 31 GCP machine types from component pricing.
- `bin_packing.py` — FFD + BFD. Now uses `NormalizedPriceItem` (node SKU) + `WorkloadRequirement` (container workloads via `resources.cpu_request_millicores`/`memory_request_mb`/`replicas`). Cost via `monthly_cost_estimate`.
- `scoring.py` — Weighted multi-criteria scorer. Now uses `NormalizedPriceItem` + `WorkloadRequirement`. Extracts specs via attribute helpers.
- `waf_compliance.py` — WAF pillar checks. Now filters `request.workloads` by `ServiceCategory` instead of deprecated fields.
- **Verified**: 60/60 checks passed ✅

### LangGraph Orchestrator (`src/orchestrator/`)
- `state.py` — `OrchestratorState` TypedDict, `AgentExecution`, `SizedWorkloadResult`, `create_initial_state()` ✅
- `graph.py` — **247 lines.** `StateGraph` with 5 nodes, conditional Clarifier loop. `build_graph(llm, pricing_service)` returns compiled graph. `@observe()` + structlog. **Imports verified ✅**

### API Layer (`src/api/`)
- `dependencies.py` — **114 lines.** ASGI `lifespan()`: loads settings, wires observability, creates LLM, registers providers, initialises PricingService, compiles graph. Dependency providers for routes. **Imports verified ✅**
- `routes/health.py` — **96 lines.** `GET /health` (liveness), `GET /ready` (deep readiness). **Imports verified ✅**
- `routes/orchestration.py` — **208 lines.** `POST /orchestrate` (full pipeline → JSON), `POST /orchestrate/stream` (SSE streaming). **Imports verified ✅**
- `routes/__init__.py` — Aggregates health + orchestration routers.
- `main.py` — **Rewritten.** FastAPI app with `lifespan`, CORS, router mount at `/api/v1`. **Imports verified ✅**

### Dashboard (`dashboard/`) ✅
- React + Vite v8 + TypeScript + Tailwind v3 + shadcn/ui (New York, slate)
- `src/types/api.ts` — TS interfaces matching all backend Pydantic models
- `src/lib/api.ts` — API client with `orchestrate()`, `streamOrchestrate()` (SSE via `@microsoft/fetch-event-source`), `checkHealth()`, `checkReady()`
- `src/context/PipelineContext.tsx` — React Context: messages, agent progress, result, streaming state
- `src/hooks/useHealth.ts` — Polls `GET /ready` every 30s
- Chat UI: `ChatMessage`, `ChatInput`, `AgentProgress` (5-step stepper), `ChatContainer`
- Results: `ExecutiveSummary`, `CostComparisonTable`, `CostComparisonChart` (Recharts), `ProviderCard`, `ComplianceReport`, `RfpDocument`
- Pages: `/chat` (ChatPage), `/results` (tabbed: Overview, Costs, Compliance, RFP), 404
- `npm run build` — **0 errors** ✅

---

## Completed Priority Queue — P0 through P9 (All Done)

### First Pipeline Run Results (17 Apr 2026)
The pipeline runs end-to-end but output quality is **subpar**:
- Clarifier doesn't ask enough questions (no architecture, performance, SLA, security depth)
- Profiler misclassifies K8s as AI_ML (gives it GPU, 76.8 GB RAM)
- Sizer picks wrong SKUs (SageMaker for K8s, MariaDB db.t2.micro for PostgreSQL)
- Costs unrealistically low ($18-47/mo vs expected $500-1,500/mo for production)
- RFP is thin (~4,690 chars) vs reference RFPs (30-60 pages)
- LangFuse traces NOT showing up (env var mismatch + no flush)

### Priority 0 — Fix LangFuse Tracing ✅ DONE
1. ✅ **Fixed env var**: `.env` `LANGFUSE_BASE_URL` → `LANGFUSE_HOST` (matches settings field + prefix)
2. ✅ **Added `flush()` call** — `_flush_langfuse()` in `finally` blocks on both sync and SSE route handlers
3. ✅ **Added parent trace** — `_execute_pipeline()` wrapped with `@observe(name="orchestrate_pipeline")` for sync path
4. ✅ **Removed double nesting** — `@observe()` removed from all 5 `run_*_node()` entry points; kept on graph node wrappers + internal sub-functions
- **Verified**: Trace `p0_verification_test` visible in LangFuse API at `http://localhost:3000`

### Priority 1 — Fix Agent Quality (CRITICAL)

#### 1a. Clarifier: Deep Requirement Gathering ✅ DONE
- ✅ **Rewrote `_extract_workloads_from_text()`**: count-aware microservice parsing ("3 microservices on K8s" → 3 CONTAINER workloads + K8s mgmt + LB), database engine propagation, serverless/AI-ML/networking detection
- ✅ **K8s node pool awareness**: cluster management fee is a separate `WorkloadRequirement` with `notes="cluster_management_fee"`
- ✅ **Enhanced LLM enrichment**: structured prompt extracts ARCHITECTURE, RESOURCE_ADJUSTMENTS, SCALING_NOTES, COST_ESTIMATE_RANGE, CONCERNS; `_apply_llm_adjustments()` safely applies numeric resource overrides
- ✅ **Added `_parse_count()` helper**: extracts numeric counts from patterns like "3 microservices", "2 VMs"
- **Validated**: 11/11 checks passed

#### 1b. Profiler: Fix Category Resolution ✅ DONE
- ✅ **Added `_guard_ai_ml()`**: prevents AI_ML assignment when `gpu_count == 0` AND no explicit ML/AI keywords in workload name/description
- ✅ **Container-aware resource estimation**: when `cpu_request_millicores`/`memory_request_mb` set by Clarifier, derives vCPU/memory from millicore specs × replicas (not blanket defaults). 3 microservices × 500m/512MB = ~1.5 vCPU, 1.5 GB (not 76.8 GB)
- ✅ **Cluster management fee handling**: workloads with `notes="cluster_management_fee"` return zero compute (fixed platform fee, not sized)
- ✅ **GPU detection fixed**: `requires_gpu` now based on `gpu_count > 0` ONLY — AI_ML category alone no longer forces GPU
- **Validated**: 23/23 checks passed

#### 1c. Sizer: Fix SKU Selection ✅ DONE
- ✅ **Container node pool fix**: queries VM SKUs (EC2/VMs/Compute Engine) for node pools instead of K8s service pricing (EKS/AKS/GKE)
- ✅ **Database engine propagation**: `_DATABASE_ENGINE_MAP` maps (provider, engine) → specific service+SKU filter. PostgreSQL queries go to `AmazonRDS`+`PostgreSQL`, `Azure Database for PostgreSQL`, `Cloud SQL`+`PostgreSQL`
- ✅ **Fixed-cost workloads**: `_is_fixed_cost_workload()` detects cluster management fee and load balancer workloads → known fixed costs ($73/mo K8s, $18-22/mo LB) without SKU lookup
- ✅ **Ancillary costs**: `_build_ancillary_results()` adds NAT gateway ($32-33/mo) and data transfer ($9-12/mo) estimates per provider. NAT only added when containers present.
- ✅ **VM enrichment extended**: GCP synthetic VMs and AWS/Azure hourly filter now apply to CONTAINER category too (not just COMPUTE/AI_ML)
- **Validated**: 11/11 new logic checks + full import chain verified

#### 1d. FinOps: Complete Cost Modeling ✅ DONE
- ✅ `[Infra]` ancillary workloads route to `networking_monthly_usd` bucket
- ✅ RI/spot savings always populated (live query + industry-standard fallback rates)
- ✅ Spot eligibility filter: stateful categories excluded, fixed-cost items bypass discounting
- ✅ `_compute_tco()` computes 1yr/3yr/5yr TCO with compound growth (default 15%/yr)
- ✅ TCO stored in `state[kpis][tco_projections]` and displayed in summary message

#### 1e. RFP Writer: Produce Enterprise-Grade Output ✅ DONE
- ✅ Added 10 new section builder functions targeting 15,000-30,000 char output
- ✅ `_build_toc_section()` — Table of Contents with 14 section entries
- ✅ `_build_architecture_section()` — ASCII topology diagram, data flow, network topology table
- ✅ `_build_tech_specs_section()` — per-component resource requirements + SKU attribute tables
- ✅ `_build_sla_section()` — tier-specific SLA targets, provider SLAs, measurement methodology
- ✅ `_build_security_section()` — IAM, encryption, network security, audit logging, open WAF findings
- ✅ `_build_migration_section()` — 4-phase plan (Discovery/Foundation/Migration/Cutover), per-phase tables, rollback strategy
- ✅ `_build_dr_section()` — DR pattern/RPO/RTO per tier, backup strategy, failover procedure, test cadence
- ✅ `_build_tco_section()` — 5-year on-demand + 3-yr RI tables using `kpis[tco_projections]`
- ✅ `_build_certifications_section()` — SOC2/ISO27001/FedRAMP/PCI-DSS per provider + shared responsibility model
- ✅ `_build_assumptions_section()` — 9 numbered assumptions + exclusions list
- ✅ Constants: `_SLA_TARGETS`, `_DR_STRATEGY`, `_PROVIDER_CERTIFICATIONS`
- ✅ Executive summary prompt updated to 4-6 paragraphs, 4000-char cap
- ✅ `run_rfp_writer_node` updated to assemble all 17 sections
- **Validated**: `scripts/test_rfp_size.py` → 24,230 chars ✅

### Priority 2 — Data Model Enhancements ✅ DONE
- ✅ `ServiceCategory.KUBERNETES` added (15th value) — managed K8s control-plane fee, distinct from `CONTAINER`
- ✅ `WorkloadRequirement` P2 SLA/perf fields: `latency_p99_ms`, `throughput_rps`, `concurrent_users`, `uptime_sla`, `rpo_minutes`, `rto_minutes`, `data_growth_rate_pct`, `spot_eligible: bool = True`
- ✅ `AncillaryCost` model in `recommendation.py` + `ancillary_costs: list[AncillaryCost]` on `ProviderCostBreakdown`
- ✅ `AncillaryCost` exported from `src/models/__init__.py`
- ✅ Clarifier: cluster management fee workloads now use `ServiceCategory.KUBERNETES` (+ `spot_eligible=False`)
- ✅ Profiler: early-return for `cluster_management_fee` → KUBERNETES; KUBERNETES added to `_INSTANCE_FAMILY_MAP` + `_RESOURCE_DEFAULTS`
- ✅ Sizer: `_SERVICE_NAME_MAP` includes KUBERNETES → EKS/AKS/GKE
- ✅ FinOps: `_CATEGORY_TO_COST_FIELD` includes KUBERNETES → `kubernetes_monthly_usd`; `_SPOT_INELIGIBLE` includes KUBERNETES
- ✅ RFP Writer `_build_sla_section()`: reads actual `uptime_sla`, `latency_p99_ms`, `throughput_rps`, `concurrent_users` from workloads; overrides tier defaults
- ✅ RFP Writer `_build_dr_section()`: reads actual `rpo_minutes`/`rto_minutes` from workloads; formats as hours/minutes
- ✅ `scripts/validate_all.py`: updated ServiceCategory count (14→15), recommendation test, clarifier K8s category
- **Validated**: 27/33 validate_all.py ✅ (3 new passes vs P1e), `test_rfp_size.py` 24,230 chars ✅

### Priority 3 — Testing ✅ DONE
- ✅ `tests/conftest.py` — 10 shared pytest fixtures: `make_price_item()` NormalizedPriceItem factory (includes required `retail_price`, `unit_of_measure`, `effective_date`), `mock_llm`, `mock_pricing_service`, `sample_workload_request`, `initial_state`, `state_with_workload_request`, etc.
- ✅ `tests/unit/test_clarifier.py` — 64 tests (all 6 parse functions + workload extraction)
- ✅ `tests/unit/test_profiler.py` — 20 tests (`_resolve_category`, `_guard_ai_ml`, `_estimate_resources`, `_heuristic_rationale`)
- ✅ `tests/unit/test_finops.py` — 19 tests (`_compute_tco`, category maps, spot ineligible, grouping, category resolution)
- ✅ `tests/unit/test_engines.py` — 15 tests (bin-packing FFD/BFD/replicas, scoring rank/filter/weights)
- ✅ `tests/unit/test_models.py` — 15 tests (ServiceCategory, WorkloadRequirement SLA fields, AncillaryCost, NormalizedPriceItem)
- ✅ `tests/integration/test_pipeline.py` — 13 tests (Clarifier node 5 async, Profiler node 5 async, state shape 3 sync)
- **Run**: `uv run pytest tests/` → **142 passed in 0.52s** ✅
- **Installed**: `pytest-asyncio==1.3.0` via `uv sync --extra dev` (required for `asyncio_mode = "auto"` in pyproject.toml)

---

### Priority 4 — LLM-Powered Multi-Turn Clarifier ✅ DONE

**What was built**: Full LLM-powered conversational clarifier for `POST /orchestrate/clarify`. The route now sends the full conversation history to the LLM on every turn. The LLM (Claude via Bedrock) decides what to ask next, stops when enough info is gathered, and emits a structured summary.

**Key additions to `src/agents/clarifier.py`**:
- `_CLARIFY_SYSTEM_PROMPT` — Pre-Sales Cloud Solutions Architect persona conducting a Well-Architected Review (WAR) discovery session. Covers all 6 AWS WAF pillars (see P5 below).
- `llm_clarify_turn(llm, history, user_input, log)` — turn handler that returns `{status, response, structured?}`. `structured` has 17 fields (11 base + 6 WAF pillar summaries).
- `build_enriched_input_from_structured(raw_input, structured)` — converts structured output into a rich text block including a "Well-Architected Framework Assessment" section.
- Helper parsers: `_extract_clarify_section()`, `_parse_providers_from_clarify()`, `_parse_compliance_from_clarify()`, `_parse_budget_from_clarify()`.

**Route rewrite** (`src/api/routes/orchestration.py`): In-memory session store; each turn calls `llm_clarify_turn`; when `status=ready` calls `build_enriched_input_from_structured` and returns enriched input for the pipeline.

---

### Priority 5 — WAF Framework Alignment ✅ DONE

**What was built**: Two-pass redesign of the clarifier's conversation strategy so it behaves like a real pre-sales architect.

**Design principle**: The architect has **two distinct roles**:
1. **Interviewer** — asks only minimal business-context questions the *client* can answer (4–8 turns max): platform type + users, scale (concurrent/peak), compliance, cloud provider, budget, availability/DR target.
2. **Architect** — when `STATUS: ready`, *infers and decides* all 6 WAF pillar recommendations from the gathered context. Never asks the client about CI/CD tooling, encryption algorithms, spot vs RI, CMK vs managed keys, caching strategy, etc.

**`_CLARIFY_SYSTEM_PROMPT`** key sections:
- **"What to Ask"**: 6 business-context topics (platform/users, scale, compliance, provider, budget, availability/DR)
- **"What NOT to Ask"** explicit list: CI/CD pipelines, monitoring stack, spot commitment, CMK choice, network CIDR blocks, caching design, backup retention, sustainability (unless client raises it)
- **WAF pillar fields** framed as architect-decided recommendations with concrete technology names:
  - `WAF_OPERATIONAL_EXCELLENCE`: recommended deployment strategy (GitOps/CodePipeline/Terraform), monitoring stack (CloudWatch+X-Ray+Datadog), alerting model — inferred from platform type + provider
  - `WAF_SECURITY`: auth mechanism (Cognito+SAML/Azure AD/IAP), network isolation design (public ALB/CDN subnets, private app+DB tiers), encryption (AES-256 at rest, TLS 1.3 in transit), WAF+DDoS layer — inferred from compliance + data classification
  - `WAF_RELIABILITY`: multi-AZ/multi-region topology, RTO/RPO (inferred from SLA if not stated), backup strategy, criticality tier — inferred from SLA + sector
  - `WAF_PERFORMANCE_EFFICIENCY`: auto-scaling strategy, caching architecture (ElastiCache Redis, CloudFront), estimated RPS at peak, p99 latency target — inferred from scale + use case
  - `WAF_COST_OPTIMIZATION`: RI/Savings Plan recommendation, spot eligibility (batch/non-critical only), tagging strategy — inferred from budget + workload pattern
  - `WAF_SUSTAINABILITY`: green region recommendation (gov/enterprise → us-west-2/eu-north-1 + Graviton3) or default if no constraints
- **Completion criteria**: 6 business signals (no longer requires "deployment/operations model" since that's inferred)

**Validated**: 142/142 tests still pass.

---

### Priority 6 — Provider Strategy + Logical Architecture First ✅ DONE

**What was built**: Three additions to `src/agents/clarifier.py` — a new `PROVIDER_STRATEGY` field, a new `ARCHITECTURE_PATTERN` field, and neutralised `WORKLOAD_SUMMARY` naming — plus updated enriched-output serialisation.

**Changes in `_CLARIFY_SYSTEM_PROMPT`**:
- Cloud provider question now explicitly includes: *"no preference — compare all and recommend the best price"* as a valid answer. Clarifies that best-price mode runs all three providers through FinOps and recommends cheapest.
- `PROVIDERS:` line now followed immediately by:
  ```
  PROVIDER_STRATEGY: <best_price_all | best_price_aws_azure | best_price_aws_gcp | best_price_azure_gcp | single_aws | single_azure | single_gcp>
  ```
- `WORKLOAD_SUMMARY` guidance changed to **provider-neutral component names** (kubernetes cluster, postgresql database, redis cache, CDN, object storage, API gateway, streaming pipeline) — NOT EKS/RDS/CloudFront/etc. Provider-specific service selection is deferred to the Sizer agent.
- `ARCHITECTURE_PATTERN` field added after `WORKLOAD_SUMMARY`: 1-3 sentence provider-neutral design pattern (e.g. "3-tier containerised web platform: managed Kubernetes for microservices, managed relational database with HA read replicas, Redis caching layer, global CDN").

**Changes in `llm_clarify_turn()` structured dict** (now 19 fields):
- Added `"provider_strategy"` parsed from `PROVIDER_STRATEGY:` line
- Added `"architecture_pattern"` parsed from `ARCHITECTURE_PATTERN:` line

**Changes in `build_enriched_input_from_structured()`**:
- Emits `Architecture pattern: ...` line before the Providers line
- Emits `Provider strategy: best_price_all` (or single_aws etc.) so `run_clarifier_node` downstream can pick it up

**Key invariant**: Sizer, FinOps, and RFP Writer needed **zero changes**. When `target_providers = [AWS, AZURE, GCP]` (driven by `PROVIDERS: aws, azure, gcp`), the pipeline already sizes all three, FinOps compares, and RFP Writer shows the comparison table + recommends cheapest.

**Validated**: 142/142 tests pass.

---

### Priority 7 — Pipeline Defects from Cal Fire E2E Test ✅ ALL DONE

> **Test scenario**: California state government wildfire platform (50K→2M users, AWS, StateRAMP Moderate + WCAG 2.2 AA, $3.2M/yr, 99.99% SLA, cross-region DR). Clarifier performed excellently (3 turns). All 7 defects fixed.

#### Fix 7a — Scale propagation ✅ DONE — `clarifier.py`
- New helpers: `_parse_peak_concurrent_users()`, `_parse_availability_sla()`, `_parse_rpo_rto()`
- New `_propagate_scale_and_sla(req, raw_input, log)` reads `Scale:` / `Availability:` / `DR requirements:` lines
- Writes `concurrent_users`, `uptime_sla`, `throughput_rps` (peak × 10 RPS), `scaling_pattern=BURSTY`, `rpo_minutes`, `rto_minutes` to all workloads

#### Fix 7b — Compliance tag propagation ✅ DONE — `clarifier.py`
- After parsing, `compliance_frameworks` propagated → `WorkloadRequirement.compliance_tags` for all workloads
- `_parse_compliance_extended()` superset parser handles stateramp/fedramp/wcag/gdpr/soc2/nist/fisma/cmmc

#### Fix 7c — CDN + geospatial workload inference ✅ DONE — `clarifier.py`
- `_extract_workloads_from_text()`: cdn/gis/geospatial/map tiles/wildfire keywords → CDN workload (NETWORKING, notes=cdn) + Geospatial/Tile Storage (STORAGE, 5 TB)

#### Fix 7d — Region propagation / provider_regions fix ✅ DONE — `clarifier.py`
- `_parse_explicit_values()`: distinct AWS/Azure/GCP region regexes; when region matched, updates BOTH `preferred_region` AND `provider_regions[provider]`
- Sizer's `_get_region_for_provider()` uses `provider_regions["aws"]` → now gets "us-west-2" not "us-east-1"

#### Fix 7e — AI/ML hallucination guard ✅ DONE — `profiler.py`
- `_guard_ai_ml()` now handles 3 cases: (a) GPU + AI keywords → confirmed AI_ML; (b) GPU but NO keywords → strip GPU, revert to `suggested_category`; (c) keywords only (no GPU) → AI_ML; (d) neither → revert

#### Fix 7f — WAF multi-cloud false positive ✅ DONE — `waf_compliance.py`
- `_check_operational_excellence()`: detects deliberate single-provider choice via `raw_user_input` markers (`provider strategy: single_*`, `aws only`, `azure only`) → marks check as PASS with "intentional client choice" note

#### Fix 7g — RFP section numbering ✅ DONE — `rfp_writer.py`
- All section builder headings corrected to match ToC sequence (§1-§15)
- ToC updated to 15 entries (added §15 WAF Compliance Report)

---

### Priority 8 — Cal Fire E2E Validation ✅ DONE (2 May 2026)

> Ran `scripts/test_cal_fire_e2e.py` — 10/10 acceptance criteria passed. Two additional defects found and fixed:

#### Fix 8a — AI/ML false-positive keyword match ✅ DONE — `clarifier.py`
- `_extract_workloads_from_text()`: `"ai" in text_lower` / `"ml" in text_lower` (substring) → whole-word regex `r'\b(?:ai|ml|llm|nlp|cv)\b'`
- Prevents false positives on "availability", "reliability", "sustainability", etc.

#### Fix 8b — Implicit CONTAINER workload not inferred ✅ DONE — `clarifier.py`
- `elif has_k8s:` branch: when text has "microservices"/"containeris*" without explicit count, now adds `CONTAINER` workload + `KUBERNETES` cluster-mgmt-fee (previously only cluster-mgmt-fee was added)

**Test**: `scripts/test_cal_fire_e2e.py` → 10/10 ✅ | Regression: 142/142 pytest tests pass

---

### Next: Full Live E2E Pipeline Run ✅ DONE (4 May 2026)

Full Cal Fire pipeline completed end-to-end with gemini-2.5-pro via Vertex AI.
- Clarifier: 4 turns → status: ready ✅
- Pipeline: 203s total, 5 agents, 34,942-char RFP, 100% WAF score
- 8/8 acceptance criteria passed ✅
- See `docs/test_prompt.md` for full turn-by-turn log and results

---

### Priority 9 — Sizer SKU Selection Bugs ✅ ALL DONE (4 May 2026)

> Fixed all 6 bugs discovered during the Cal Fire live run. True cost for Cal Fire scenario: ~$800–$2,500/mo (was $34,972/mo due to one wrong SKU).

#### Bug 9a ✅ — Container bin-packing vCPU ratio guard — `sizer.py`
- `total_needed_vcpus` computed from all container workloads (millicore × replicas)
- `max_node_vcpus = max(8, total_needed_vcpus × 4)` added to `viable_nodes` filter
- `_sort_key()` sorts preferred families (m5/m6i/c5/c6i) first, then by cost efficiency
- `_PREFERRED_CONTAINER_FAMILIES` dict defines preferred AWS/Azure families

#### Bug 9b ✅ — DATABASE candidates filtered to hourly instance rows — `sizer.py`
- After `search_prices()` for DATABASE category, filter: `unit_of_measure in ("1 Hour", "1 hour") and unit_price > 0`
- Excludes RDS storage (GP2/GP3), IOPS, and backup meters from candidate set

#### Bug 9c ✅ — Redis/ElastiCache routed to correct service — `sizer.py`
- `_DATABASE_ENGINE_MAP` now has: `("aws", "redis") → ("AmazonElastiCache", "cache.r6g")`, `("aws", "elasticache") → ("AmazonElastiCache", "cache.r6g")`, `("azure", "redis") → ("Azure Cache for Redis", None)`, `("gcp", "redis") → ("Cloud Memorystore", None)`

#### Bug 9d ✅ — Storage standard-tier filter — `sizer.py`
- `_filter_storage_candidates(candidates, provider)` new helper function
- Excludes: `earlydelete`, `glacier`, `deeparchive`, `coldline`, `nearline`, `archive`, `infrequent`
- Prefers: AWS `standardstorage`, Azure `hot/lrs`, GCP `standard`

#### Bug 9e ✅ — CDN workloads use fixed-cost estimate — `sizer.py`
- `_is_cdn_workload(workload)`: detects `notes="cdn"` or `"cdn"` in name
- `_CDN_COST_MONTHLY = {aws: $85, azure: $70, gcp: $60}` (fixed estimate ~500 GB/mo)
- CDN check runs before SKU lookup (same pattern as load_balancer fixed cost)

#### Bug 9f ✅ — Profiler: 0 vCPU for NETWORKING/STORAGE — `profiler.py`
- `_estimate_resources()`: early return for `NETWORKING` → `{vcpus: 0, memory_gb: 0.0, storage_gb: 0.0}`
- `STORAGE` → `{vcpus: 0, memory_gb: 0.0, storage_gb: storage_gb × tier_mult}`
- **Validated**: profiler unit tests 23/23 ✅, overall 142/142 ✅, Cal Fire E2E 10/10 ✅

---

---

### Priority 10 — RFP Writer Content Gaps ✅ ALL DONE (4 May 2026)

> Fixed all 6 content gaps vs Presidio reference. Doc is now a project delivery proposal, not just an infrastructure cost analysis.

#### Gap 10a ✅ — Government document framing — `rfp_writer.py`
- `_is_government_scenario()`: detects government/public-safety keywords + StateRAMP/FedRAMP compliance frameworks
- Header: title changes to "Proposed Cloud Solution" + "Document Type: Solution Proposal" for gov scenarios

#### Gap 10b ✅ — Requirements traceability matrix — `rfp_writer.py`
- `_build_requirements_traceability_section()`: functional (F-xx), compliance (C-xx with WCAG/StateRAMP/FedRAMP/HIPAA/CJIS), and NFR (NFR-01–05) rows
- Appended as section 17 when `compliance_frameworks` is non-empty

#### Gap 10c ✅ — Contract-aligned delivery phases — `rfp_writer.py`
- `_is_greenfield_project()`: detects absence of migration keywords
- Greenfield: Phase 1 MVP → Phase 2 UAT → Phase 3 Public Launch → Phase 4 Managed Ops
- Migration: original Discovery → Foundation → Migration → Cutover phases unchanged

#### Gap 10d ✅ — Mobile app architecture subsection — `rfp_writer.py`
- `_is_mobile_scenario()`: detects mobile/iOS/Android/Flutter in raw_user_input + workload names
- `_build_mobile_subsection()`: ASCII mobile data path + 6 principles (offline-first, FCM, OAuth PKCE, WCAG, App Store)
- Appended inside `_build_architecture_section()` when mobile detected

#### Gap 10e ✅ — Managed services specifics table — `rfp_writer.py`
- `_MANAGED_SERVICES` dict: 3 providers × 14 categories with specific service names (RDS Multi-AZ, EKS, ElastiCache, CloudFront, etc.)
- `_build_managed_services_section()`: new section 5 with per-provider tables + mobile/streaming rows + key benefits callout

#### Gap 10f ✅ — WCAG/StateRAMP implementation details — `rfp_writer.py`
- WCAG 2.2 AA: 11-row table (contrast ratio, keyboard, ARIA, focus, status messages) + testing approach
- StateRAMP Moderate: 7-step ATO pathway table + 6 NIST 800-53 control family highlights (AC/AU/IA/SC/SI/IR)
- FedRAMP, HIPAA, CJIS: summary control bullets when present in compliance_frameworks

---

### Priority 11 — Sizer & RFP Writer Bugs ✅ ALL DONE (4 May 2026)

> Fixed all 5 bugs from second Cal Fire live run. 142/142 tests pass. **Third run (5 May 2026) found 4 remaining bugs → Priority 12.**

#### Bug 11a ✅ — DATABASE $0 (PostgreSQL, Cache Layer) — `sizer.py`
- Added `_infer_engine_from_name(workload_name)` fallback function
- If `resources.database_engine` is None, parse engine from workload name keywords: "Postgresql Database" → "postgresql" → `AmazonRDS/PostgreSQL`; "Cache Layer" → "redis" → `AmazonElastiCache/cache.r6g`

#### Bug 11b ✅ — EC2 SQL Enterprise meter ($1,242/mo) — `sizer.py`
- Added AWS-specific Linux OS filter after the hourly VM candidate filter
- `attributes["operatingSystem"] == "Linux"` AND `usagetype` not containing `"UnusedBox"` or `"UnusedDed"`
- This removes SQL Enterprise license rows and reduces to standard Linux on-demand pricing (~$139/mo for m7i.xlarge)

#### Bug 11c ✅ — Storage $0 (INT-AIA tier, no quantity) — `sizer.py`
- Added `int-aia`, `int-fa`, `int-aa`, `int-da` to `_EXCLUDE_PATTERNS` in `_filter_storage_candidates()`
- Added post-processing after generic sizing for STORAGE: `monthly = unit_price × storage_gb` when unit is monthly/GB-based
- Uses `model_copy(update=...)` to return corrected SizedWorkloadResult without mutating the model

#### Bug 11d ✅ — §11 section numbering collision — `rfp_writer.py`
- `_build_dr_section()` hardcoded "## 11. Disaster Recovery" — changed to `"## 12. Disaster Recovery & Business Continuity\n"`
- Now matches ToC: §11 = Delivery Plan, §12 = Disaster Recovery

#### Bug 11e ✅ — Cache Layer → wrong managed service — `rfp_writer.py`
- In `_build_managed_services_section()` component loop: if `resolved_category == DATABASE` and name contains "cache"/"redis"/"elasticache", override `cat_key = "CACHE"` (not "DATABASE")
- Cache Layer now correctly maps to "Amazon ElastiCache for Redis (cluster mode enabled)"

---

## What Needs to Be Fixed/Built Next ❌

> **All Priority 13 code fixes are done.** Live run #6 is the next step — needs fresh AWS credentials.

### Priority 13 — COMPLETE (code fixes done, live run pending)

| Bug | Status | Fix location |
|-----|--------|-----|
| 13a — RDS/ElastiCache $0 (`instanceType` filter mismatch) | ✅ Fixed | `aws_provider.py` `search_prices()` — RDS uses `databaseEngine` filter; ElastiCache uses `cacheEngine=Redis` filter |
| 13b — EC2 `linux_only` silent fallback to garbage rows | ✅ Fixed | `sizer.py` ~line 1256 — `else: candidates = []` when `linux_only` empty |
| 13c — GPU instance (g5.4xlarge) selected for non-GPU VM | ✅ Fixed | `sizer.py` `_GPU_PREFIXES` block excludes `g3–g7`, `p2–p5`, `trn`, `dl1` for `not requires_gpu` |
| 13d — S3 ZIA tier not excluded | ✅ Fixed | `sizer.py` `_EXCLUDE_PATTERNS`: `"-zia-"`, `"zia-bytehrs"` added; `aws_provider.py` `storageClass=General Purpose` API filter |
| 13e — S3 API filter missing (cache poisoned with archival tiers) | ✅ Fixed | `aws_provider.py`: EC2 gets `operatingSystem=Linux` + `tenancy=Shared` + `preInstalledSw=NA` filters; S3 gets `storageClass=General Purpose` |
| 13f — RFP 8 duplicate/wrong section numbers | ✅ Fixed | `rfp_writer.py` lines 164, 201, 271, 317, 1135, 1219, 1600, 1844 |

### Next Immediate Task

**Refresh AWS credentials and run the sixth live Cal Fire run:**
1. Update `.env` with fresh `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` (run `aws sso login` or re-issue from AWS console)
2. Delete `data/sku_cache.db` if it somehow got recreated with old data
3. Restart API server: `uv run uvicorn src.main:app --port 8001`
4. Run 2-turn Cal Fire clarify session → `/orchestrate` pipeline
5. Verify sizer output: Container=m5/m6i family (~$70–150/mo), DB=RDS `db.m5.large` (~$120–200/mo), Cache=ElastiCache `cache.r6g.large` (~$100–150/mo), VMs=m5/m6i (~$280–350/mo), Storage=S3 Standard (NOT ZIA/GIR)
6. Total should be ~$15,000–50,000/mo (not $3,220)
7. Save RFP to `docs/test-rfp-document-6.md`


## Auth / Credentials (already set in `.env`)
- **AWS**: Temporary STS session credentials (`ASIA` prefix) — **Bedrock access is now DENIED** (IAM policy explicitly denies `bedrock:InvokeModel`). Still used for Pricing API. Do not attempt to restore Bedrock for LLM.
- **GCP / Vertex AI (PRIMARY LLM)**: ADC via `gcloud auth application-default login` → `~/.config/gcloud/application_default_credentials.json`. Account: `ramkumar.workemail@gmail.com`. Project: `dissertation-rj`. Vertex AI API enabled. Free trial: ~₹27,287 credit, 44 days remaining. LLM model: `gemini-2.5-pro`. Env vars: `VERTEXAI_PROJECT=dissertation-rj`, `VERTEXAI_LOCATION=us-central1`.
- **Azure**: No credentials needed (public REST API).
- **LangFuse**: Self-hosted via Docker Compose (`docker-compose.langfuse.yml`). Keys in `.env`. UI at `http://localhost:3000`. Start with `docker compose -f docker-compose.langfuse.yml up -d`.

---

## Dev Commands
```bash
# Run any script
uv run python scripts/test_aws_adapter.py

# Sync deps after pyproject.toml change
uv sync --all-extras

# Run tests (when they exist)
uv run pytest

# Start LangFuse (local Docker)
docker compose -f docker-compose.langfuse.yml up -d   # → http://localhost:3000

# Stop LangFuse
docker compose -f docker-compose.langfuse.yml down

# Dashboard dev server
cd dashboard && npm run dev   # → http://localhost:5173

# Dashboard build
cd dashboard && npm run build
```
```

---

## Notes for Future Me

- **P13 AWS pricing cache poisoning root cause (5 May 2026)**: AWS `GetProducts` for `AmazonEC2` WITHOUT `operatingSystem`/`tenancy`/`preInstalledSw` filters returns billing artifacts in alphabetical `usagetype` order. The first 100 items in `us-west-2` are ALL non-standard: `USW2-UnusedBox:m7i.xlarge` (unused RI placeholder, `operatingSystem=Linux`, $1.70/hr — PASSES linux_only filter because it IS Linux), DedicatedHost rows ($0 — filtered by `unit_price > 0`), SQL-licensed rows, etc. **The fix**: add `operatingSystem=Linux`, `tenancy=Shared`, `preInstalledSw=NA` as API-level filters in `search_prices()` BEFORE hitting the pricing endpoint. For RDS: never use engine name (e.g. "PostgreSQL") as `instanceType` filter — use `databaseEngine` field instead. For ElastiCache: use `cacheEngine=Redis`. For S3: use `storageClass=General Purpose` to get only Standard tier. **Key lesson**: always add service-specific attribute filters to AWS `GetProducts` — the API intentionally returns ALL product variants without them.
- **P13 linux_only silent fallback (5 May 2026)**: `if linux_only: candidates = linux_only` evaluates to `False` when `linux_only` is empty (all remaining rows were non-Linux after filter). The old code silently reverted to the pre-filter set (garbage rows including UnusedBox). Fix: `else: candidates = []` + log warning. This was the root cause of m7i.xlarge (UnusedBox RI placeholder at $1.70/hr) being selected for the Container workload across multiple runs.
- **P13 RFP section numbers (5 May 2026)**: When `_build_managed_services_section()` was added as §5, 8 downstream section headers were never renumbered. The ToC was already correct (1–17) but body section headers had `## 5.`, `## 6.` etc. in the wrong places. Fixed lines: 164 (§5→§6 SKU), 201 (§6→§7 Cost), 271 (§15→§16 WAF), 317 (§13→§14 Vendor), 1135 (§8→§9 SLA), 1219 (§9→§10 Security), 1600 (§7→§8 TCO), 1844 (§14→§15 Assumptions). Rule: after adding/reordering sections, grep for all `## \d+\.` and verify each matches the ToC.
- **P10 RFP Writer content gaps (4 May 2026)**: 6 gaps closed vs Presidio reference. Key lessons: (1) `ComponentProfile` has `recommended_instance_families: list[str]`, NOT `recommended_family` — always check model fields before using attribute access; (2) government/greenfield detection uses keyword sets on `raw_user_input` — keep `_GOVERNMENT_KEYWORDS` and `_MIGRATION_KEYWORDS` up to date as new scenarios arise; (3) WCAG + StateRAMP sections must be conditional on `compliance_frameworks` — don't add them for every run; (4) the `_MANAGED_SERVICES` dict is the key lookup for 10e — expand it when new service categories are added to `ServiceCategory` enum.
- **P10 live pipeline crash fix**: `comp.recommended_family` → `comp.recommended_instance_families[0] if comp.recommended_instance_families else comp.resolved_category.value` — always check model field names against `model_fields.keys()` before referencing.
- **LLM factory fix (5 May 2026)**: `_create_vertexai()` in `factory.py` now passes `vertexai=True` (routes to Vertex AI, not Google AI Studio), `project=project` (explicit — ADC project can be `None` in dev), and `location=location` (prevents wrong endpoint). Also added `GCP_PROJECT_ID` fallback for `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` env var (default `us-central1`).
- **Gemini 3.1 Pro Preview (5 May 2026)**: `gemini-3.1-pro-preview` is a restricted preview. `.env` `LLM_MODEL=gemini-3.1-pro-preview` returns 404 "Publisher Model not found" for project `dissertation-rj`. To enable: GCP Console → Vertex AI → Model Garden → search "gemini-3.1-pro-preview" → click Enable. The "Provisioned Throughput" modal that appears is NOT required (that's for guaranteed quota, not basic access). Until enabled, use `gemini-2.5-pro`.
- **AWS STS credentials expired (5 May 2026)**: `AWS_ACCESS_KEY_ID` in `.env` starts with `ASIA` = temporary STS token. These expire. Run `aws sso login` or re-issue from AWS console → update all three vars: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`. Verify with `aws sts get-caller-identity`.
- **Always use `uv run python` not `python3`**: System `python3` is macOS default Python 3.9 — incompatible with project (requires 3.13+). Always prefix: `/Users/ramkumarjayakumar/.local/bin/uv run python` or simply `uv run python` after `cd` to the project root.
- **Run #4 (`4d134426`, 5 May 2026)**: Pipeline ran correctly post-P12-fixes but all compute/DB/storage returned $0 — `GetCallerIdentity` returned `ExpiredToken`. Kubernetes ($73), LB ($22.27), NAT ($32.40), Data Transfer ($9) still correct (fixed-cost paths, no AWS API call). RFP saved to `docs/test-rfp-document-4.md`.
- **Bug 12d confirmed NOT a bug**: `_parse_explicit_values()` correctly sets `budget_monthly_usd = 266666.0` from `"Budget: $266,666/month"` in enriched_input. RFP §17 NFR-05 shows `Budget ceiling: $266,666/mo`. Previous checkpoint's "Not specified" claim was an incorrect inference from truncated run #3 RFP output.
- **P12 issues from third live Cal Fire run (5 May 2026)**: (1) EC2 SQL Standard meter still selected — P11b filter only excludes SQL Enterprise via `UnusedBox`/`UnusedDed` usagetypes, but SQL Standard rows have `operatingSystem=Linux` and pass the filter; need `meter_name` check. (2) `i3.4xlarge` is storage-optimized — must be excluded from compute/container preferred families. (3) GIR (Glacier Instant Retrieval) SKU not excluded — `GIR` absent from `_EXCLUDE_PATTERNS`. (4) DB/Cache still $0 — SQLite cache likely stale; clear `data/sku_cache.db` before next run. (5) NFR-05 budget not showing — `workload_request.budget_monthly_usd` is None at RFP Writer time.
- **P11 issues from live Cal Fire run**: DATABASE at $0 (PostgreSQL + Cache Layer), VM picked GPU instance (g5.4xlarge), Storage at $0 (INT-AIA tier selected, quantity not multiplied). These are the next 3 bugs to fix.
- **P9 Sizer/Profiler bug fixes (4 May 2026)**: All 6 SKU selection bugs fixed. Key lessons: (1) DATABASE candidates need hourly filter like VM candidates — RDS/ElastiCache return storage/IOPS/backup meters that have low unit prices and win cheapest-price selection; (2) CONTAINER bin-packing needs a vCPU cap `max(8, total_needed × 4)` — otherwise cost-efficiency metric favours oversized memory-optimized hosts (x8i at 192 vCPU wins $/vCPU×GB even for a 3-vCPU workload); (3) CDN/CloudFront is usage-based with no flat SKU row — must use fixed-cost estimate not pricing API; (4) Redis is NOT in `AmazonRDS` — it has its own service `AmazonElastiCache`; (5) NETWORKING and STORAGE are managed services — profiler must assign 0 vCPU (billing is per-GB/per-request, not per-vCPU).
- **LLM provider switch (4 May 2026)**: AWS Bedrock lost (IAM policy explicitly denies `bedrock:InvokeModel`). Switched to **Vertex AI `gemini-2.5-pro`** using existing GCP ADC. Changes: (1) `LLMProvider.VERTEXAI` added to `settings.py`; (2) `_create_vertexai()` added to `factory.py` (reads `VERTEXAI_PROJECT`/`VERTEXAI_LOCATION`); (3) `langchain-google-vertexai` installed via `uv add`; (4) Vertex AI API enabled: `gcloud services enable aiplatform.googleapis.com --project=dissertation-rj`; (5) `.env` updated: `LLM_PROVIDER=vertexai`, `LLM_MODEL=gemini-2.5-pro`, `VERTEXAI_PROJECT=dissertation-rj`, `VERTEXAI_LOCATION=us-central1`. AWS creds in `.env` are stale — only needed for Pricing API (not LLM).
- **P7+P8 fixes complete (2 May 2026)**: All 7 Cal Fire defects resolved + 2 new defects found and fixed by E2E test. 142/142 tests pass. Key lessons: (1) enriched input `Scale:` line needs dedicated parser for "50k normal, 2M peak" patterns; (2) `provider_regions["aws"]` must be updated alongside `preferred_region` — Sizer reads the dict not the field; (3) GPU hallucination stripping in `_guard_ai_ml` needs to handle both "GPU without AI keywords" AND "AI keywords without GPU" cases; (4) RFP section numbers must be kept in sync with the ToC when adding/reordering sections; (5) **CRITICAL**: keyword substring matching for short tokens like "ai"/"ml" will false-positive on "availability"/"reliability" — always use whole-word regex `r'\bai\b'`; (6) K8s without explicit count should infer CONTAINER app workloads when "microservices"/"containeris*" present in text.
- **Cal Fire E2E validation script**: `scripts/test_cal_fire_e2e.py` — runs clarifier node with mocked LLM against the realistic Cal Fire enriched input (10 checks: 8 acceptance criteria + 2 bonus). Run any time after clarifier changes.
- **Next priority**: Run a full live E2E (with AWS Bedrock) against the Cal Fire scenario to validate pipeline output quality — LLM profiler/sizer/finops enrichment, SKU selection from AWS pricing API, and complete RFP generation. Check RFP against Presidio reference doc.
- **NormalizedPriceItem in tests**: Always provide `retail_price` (float), `unit_of_measure` (str, e.g. `"1 Hour"`), and `effective_date` (datetime with tzinfo) — all are required fields with no defaults.
- **BinPackingResult field names**: `total_nodes` (not `node_count`), `nodes` (not `packed_nodes`), `packing_efficiency_pct` (not `avg_cpu_utilization_pct`).
- **WorkloadRequest.target_providers**: The providers field is called `target_providers`, not `providers`.
- **WorkloadRequirement.notes**: `str` type with default `""`, NOT `str | None` — pass `""` not `None`.
- **run_*_node() returns a patch**: Agents return only NEW state (patch dict for LangGraph reducer). In direct calls (integration tests), `result["messages"]` = only the new messages added (not full list). Assert `>= 1` not `> initial_count`.
- `.venv/` already created with Python 3.13.0 and all deps synced
- `get_settings.cache_clear()` resets the singleton in tests
- LangFuse gracefully degrades when keys missing (warns, doesn't crash)
- LangFuse SDK v3: `from langfuse import observe` (NOT `langfuse.decorators`)
- LangFuse self-hosted: `docker-compose.langfuse.yml` runs 6 containers (postgres, clickhouse, redis, minio, worker, web). ENCRYPTION_KEY must be real 64-char hex (not zeros). Pre-configured org/project via `LANGFUSE_INIT_*` env vars.
- **LangFuse env var**: `.env` now has `LANGFUSE_HOST` (was `LANGFUSE_BASE_URL` — fixed in P0). The env var name must match prefix + field name.
- **LangFuse flush**: `_flush_langfuse()` in `orchestration.py` calls `Langfuse().flush()` in `finally` blocks. SDK batches async, so flush is mandatory.
- **LangFuse @observe nesting**: `@observe()` lives ONLY on graph node wrappers (in `graph.py`) and agent internal sub-functions. NOT on `run_*_node()` entry points (removed in P0 to avoid double nesting).
- **LangFuse auth_check**: `client.auth_check()` throws `ValidationError` on self-hosted (missing `organization` field) — this is benign; tracing still works.
- Azure reserved prices: `retailPrice` = total upfront cost, NOT per-hour (even though `unitOfMeasure='1 Hour'`) — `monthly_cost_estimate` handles this by checking tier first
- WAF compliance engine public API is `evaluate_compliance()` (NOT `run_compliance_checks`)
- All 3 new agents (Sizer, FinOps, RFP Writer) use LLM summary with heuristic fallback — if LLM call fails, they produce a rule-based summary instead
- `PricingService.close()` must be called on shutdown (handled by lifespan)
- **First pipeline run lesson**: Garbage-in-garbage-out — if Profiler mis-categorizes (K8s→AI_ML), Sizer queries wrong service (SageMaker), FinOps sums wrong prices. Fix must cascade from Clarifier → Profiler → Sizer.
- **Reference RFPs**: See `docs/Mass Technology Collaborative Template FINAL (2) (1).html` and `docs/Presidio_Response_Cal Fire_Draft 10.7.25 (2) (1).html` for target quality/depth
- `build_graph()` returns a compiled `StateGraph` — call `.ainvoke(state)` to run
- SSE streaming endpoint at `POST /api/v1/orchestrate/stream` — uses `sse-starlette`
- CORS is wide open (`allow_origins=["*"]`) — tighten for production
- No test scripts exist yet for Sizer, FinOps, or RFP Writer agents
- **Sizer container fix**: Container workloads now query VM SKUs (EC2/VMs/Compute Engine) for node pools instead of K8s service SKUs. K8s management fee is a separate fixed-cost line item ($73/mo EKS/GKE, $0 AKS).
- **Sizer DB engine propagation**: `_DATABASE_ENGINE_MAP` maps `(provider, engine)` → `(service_name, sku_name_filter)`. Supports postgresql, mysql, mariadb, aurora-postgresql, aurora-mysql, sqlserver across all 3 providers.
- **Sizer fixed-cost workloads**: `_is_fixed_cost_workload()` detects `notes="cluster_management_fee"` and `name="Load Balancer"` → produces fixed-cost results without SKU query.
- **Sizer ancillary costs**: `_build_ancillary_results()` adds NAT gateway ($32-33/mo, only with containers) and data transfer ($9-12/mo, always) per provider.
- **FinOps RI/Spot fallback rates** (P1d): When live pricing returns no reserved/spot data (common), `_RI_DISCOUNT_RATES` (AWS 30/45%, Azure 35/50%, GCP 25/40%) and `_SPOT_DISCOUNT_RATES` (AWS 70%, Azure 80%, GCP 60%) are applied as estimates. `source: "estimate"` flag marks these entries. Fixed-cost items ([Infra], K8s fee, LB) pass through at on-demand cost for all tiers.
- **FinOps spot eligibility** (P1d): `_SPOT_INELIGIBLE` = {DATABASE, STORAGE, NETWORKING, MANAGEMENT, SECURITY}. Only stateless workload categories can use spot pricing.
- **FinOps TCO** (P1d): `_compute_tco(monthly, years, growth_pct)` uses compound growth. Default growth=15%/yr. TCO stored in `state["kpis"]["tco_projections"]` with 1yr, 3yr, 5yr values. Budget comparison: if `workload_request.budget_monthly_usd` is set, compare against 3yr TCO.
- **Next P1e task**: RFP Writer enterprise-grade output. Reference docs at `docs/Mass Technology Collaborative Template FINAL.html` and `docs/Presidio_Response_Cal Fire_Draft 10.7.25.html`. Target: 15,000-30,000 characters.

- AWS adapter: boto3 sync calls wrapped in `asyncio.to_thread()`, region filtering uses human-readable location names
- GCP adapter: gRPC sync calls wrapped in `asyncio.to_thread()`, price = `units + nanos/1e9`
- Bedrock model ID requires `us.` prefix (cross-region inference profile) — bare `anthropic.*` returns `ValidationException`
- PricingService is the single entry point for agents — never call adapters directly
- Cache DB at `data/sku_cache.db` (auto-created)
- `compare_across_providers()` designed for FinOps agent's cross-provider workflow
- Engines (`bin_packing.py`, `scoring.py`, `waf_compliance.py`) ✅ migrated to `NormalizedPriceItem`/`WorkloadRequirement` — 60/60 checks passed
- Engines use shared helpers from `src/engines/__init__.py` to extract compute specs from `NormalizedPriceItem.attributes` dict (keys: `vcpus`/`memory_gb`/`gpu_count`/`generation` standardised, `vcpu`/`memory`/`gpu`/`currentGeneration` AWS fallback)
- **GCP pricing is per-component** (per-vCPU, per-GB-RAM) — NOT per-instance like AWS/Azure. `vm_specs.compose_gcp_vm_instances()` synthesizes standard machine types with calculated hourly prices.
- **Azure API doesn't return vCPU/memory** — `vm_specs.parse_azure_vm_specs()` parses specs from ARM SKU names using regex + family→memory ratio table.
- SKU cache at `data/sku_cache.db` auto-recreates on startup. Delete it to force fresh data.
- Profiler category resolution uses priority ordering (AI_ML first, COMPUTE last) to prevent generic `vcpus` check from shadowing specific signals like `database_engine` or `gpu_count`
- Profiler degrades gracefully when LLM is unavailable — falls back to `_heuristic_rationale()` and heuristic summary notes
- `src/main.py` is broken (wrong import) — fix after API routes are ready
- Dashboard uses Tailwind v3 (NOT v4) — pinned for shadcn/ui compatibility
- Dashboard `@/` path alias: configured in both `tsconfig.app.json` (paths) and `vite.config.ts` (resolve alias)
- Dashboard SSE uses `@microsoft/fetch-event-source` because native `EventSource` only supports GET, backend streams on POST
- Dashboard `.env.example` has `VITE_API_BASE_URL=http://localhost:8000/api/v1`
- `tsconfig.app.json` has `ignoreDeprecations: "6.0"` for TS 7.0 baseUrl deprecation warning
