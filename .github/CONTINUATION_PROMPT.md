# Cloud Orchestrator IDSS — Continuation Prompt

> **Last Updated**: 11 May 2026 (P16a–P16f COMPLETE. All Dissertation Completeness Features done. Bug fix: `architecture_alternatives` always empty — fixed `ranked[0]['option']` → `ranked[0]['name']` KeyError in validator.py. 142 tests, 0 TS build errors.)

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
- `cloud_resource.py` — `CloudProvider` enum (aws/azure/gcp), `ServiceCategory` enum (**16 values** — P15b added `SERVERLESS` for Lambda+DynamoDB+APIGW architectural pattern, distinct from `SERVERLESS_FUNCTION` = single function billing). Legacy `ComputeSKU`/`StorageSKU` kept for backward compat with engines only.
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

### Sizer Agent (`src/agents/sizer.py`) — ~1800 lines ✅ Pricing accuracy fixes applied
- Category-aware SKU selection: scored (COMPUTE, AI_ML), binpacked (CONTAINER with VM node SKUs), cheapest-price (all others)
- **Container node pool fix**: queries EC2/VMs/Compute Engine for node pools (not EKS/AKS/GKE service pricing)
- **Database engine propagation**: `_DATABASE_ENGINE_MAP` routes PostgreSQL/MySQL/etc. + **redis/elasticache → AmazonElastiCache** (Bug 9c fix)
- **Engine inference fallback** (Bug 11a fix): `_infer_engine_from_name()` parses engine from workload name when `resources.database_engine` is None
- **EC2 Linux OS filter** (Bug 11b fix): AWS candidates filtered to `operatingSystem=Linux` and `usagetype` not containing "UnusedBox"/"UnusedDed"
- **SQL license token filter** (Bug 12a fix ✅): `_SQL_LICENSE_TOKENS` checks `meter_name` to exclude SQL Standard/Enterprise/Web rows
- **linux_only silent fallback safety** (Bug 13b fix ✅): when `linux_only` filter produces empty list, `candidates` is set to `[]` — never silently reverts to garbage rows
- **GPU exclusion for non-GPU workloads** (Bug 13c fix ✅): `_GPU_PREFIXES` block (`g3–g7`, `p2–p5`, `trn`, `dl1`) excludes GPU instances when `not component.requires_gpu`
- **Storage quantity scaling** (Bug 11c fix): post-processing for STORAGE: `monthly = unit_price × storage_gb` (guarded to per-GB units only — see pricing accuracy fixes below)
- **GIR exclusion** (Bug 12b fix ✅): `"-gir-"` and `"gir-bytehrs"` added to `_EXCLUDE_PATTERNS`
- **ZIA exclusion** (Bug 13d fix ✅): `"-zia-"` and `"zia-bytehrs"` added to `_EXCLUDE_PATTERNS` — S3 One Zone-Infrequent Access SKU uses acronym "ZIA"
- **Fixed-cost workloads**: K8s cluster management fee ($73/mo), load balancer ($18-22/mo), **CDN ($60-85/mo fixed estimate)**
- **Container vCPU ratio guard** (Bug 9a fix): `max_node_vcpus = max(8, total_needed_vcpus × 4)`; preferred families (m5/m6i/c5/c6i) sorted first
- **DATABASE hourly filter** (Bug 9b fix → improved): RDS/ElastiCache candidates filtered using `c.is_hourly` (flex match handles Azure "1/Hour" unit format, not just "1 Hour")
- **STORAGE standard-tier filter** (Bug 9d fix): `_filter_storage_candidates()` excludes EarlyDelete/Glacier/GIR/Nearline/INT-AIA/ZIA rows; **now also pre-filters to per-GB capacity rows** (unit_of_measure contains "GB"/"GiB") before provider-tier preference — eliminates per-IOPS and per-disk items from candidate selection
- **CDN detection** (Bug 9e fix): `_is_cdn_workload()` routes `notes="cdn"` or "cdn" in name to fixed-cost CDN path
- **Azure PostgreSQL "Auto Tune" exclusion** (new ✅): after `_filter_database_candidates`, Azure compute-instance rows are isolated by requiring `product_name` to contain "Compute" and excluding add-on keywords ("autonomous", "auto tune", "extended support", "backup", "iops", "tuning service", "throughput") — fixes $8.76/mo → correct ~$300/mo for GP instance
- **GCP Redis service name fix** (new ✅): `_DATABASE_ENGINE_MAP` corrected: `("gcp", "redis")` → `("Cloud Memorystore for Redis", None)`, `("gcp", "memcached")` → `("Cloud Memorystore for Memcached", None)`
- **Cross-provider region mapping** (new ✅): `_AWS_TO_AZURE_REGION` + `_AWS_TO_GCP_REGION` tables added; `_get_region_for_provider()` auto-translates AWS preferred_region (e.g. us-west-2) to geographically equivalent Azure (westus2) and GCP (us-west1) regions when provider-specific region is still the model default
- **Storage scaling per-GB guard** (new ✅): storage cost scaling only applied when `unit_of_measure` contains "GB"/"GiB"; fixes $0.03 → correct ~$540/mo for 10TB Azure blob storage
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
- `architecture_selector.py` — **P15a ✅** 4-option scorer (managed-serverless, self-hosted-serverless, containers, hybrid). 3 signal groups, binary-search crossover. `derive_weights_from_workload()` for P15j dynamic weights.
- `pricing_validator.py` — **P15k ✅** 7-check pricing integrity validator. `PricingValidationFinding` + `PricingValidationResult` Pydantic models. Pure algorithmic.
- **Verified**: 60/60 checks passed ✅

### New Agents (`src/agents/`) — P15d + P15e
- `router.py` — **P15d ✅** Router+Orchestrator Agent. LLM intent classifier + `ExecutionPlan` producer. 5 intents. Principal Architect Reasoning (§22g). Writes `execution_plan`, `routing_decision`, `pipeline_mode`, `turn_number` to state.
- `validator.py` — **P15e ✅** Architecture quality gate. 5 checks: [0] Pricing data integrity, [1] Architecture correctness (with P15j dynamic weights), [2] Sizing adequacy, [3] Budget fit, [4] WAF compliance. Principal Architect Reasoning. Writes `validation_report` + `architecture_alternatives`.

### Session Store (`src/services/session_store.py`) — P15f ✅
- SQLite-backed session registry (`data/sessions.db`) via aiosqlite. `SessionInfo` Pydantic model.
- `create_session()`, `get_session()`, `update_session()`, `list_sessions()`, `cleanup_expired_sessions()` (7-day TTL).
- `make_checkpointer()` → `MemorySaver` (upgrade path to SqliteSaver when dep available).

### Profiler Updates — P15c + P15g ✅
- `_llm_decompose_container_workload()` — LLM decomposes CONTAINER workload into 5–8 named microservices (JSON response, capped at 8).
- `_detect_extra_workloads()` — keyword detection for streaming (Kinesis/Kafka/EventBridge), message queues (SQS/PubSub/RabbitMQ), analytics (Redshift/BigQuery/Synapse). Injects new `WorkloadRequirement` objects.
- Both run as pre-processing steps in `run_profiler_node` before the main profiling loop.

### LangGraph Orchestrator (`src/orchestrator/`)
- `state.py` — `OrchestratorState` TypedDict, `AgentExecution`, `SizedWorkloadResult`, `ExecutionPlan` TypedDict (P15h), `create_initial_state()` with 7 new fields (session_id, turn_number, execution_plan, routing_decision, pipeline_mode, validation_report, architecture_alternatives) ✅
- `graph.py` — **~305 lines.** `StateGraph` with 6 nodes (+ router when `include_router=True`). `build_graph(llm, pricing_service, include_router=False)`. Validator node before rfp_writer. Conditional router edges (`_route_after_router()`). `@observe()` + structlog. ✅

### API Layer (`src/api/`)
- `dependencies.py` — **114 lines.** ASGI `lifespan()`: loads settings, wires observability, creates LLM, registers providers, initialises PricingService, compiles graph. Dependency providers for routes. **Imports verified ✅**
- `routes/health.py` — **96 lines.** `GET /health` (liveness), `GET /ready` (deep readiness). **Imports verified ✅**
- `routes/orchestration.py` — **~404 lines.** `POST /orchestrate` (full pipeline → JSON), `POST /orchestrate/stream` (SSE streaming), `POST /orchestrate/clarify` (LLM multi-turn session). **P15h changes needed**: add `session_id`, stop deleting session on `status=ready`. **Imports verified ✅**
- `routes/__init__.py` — Aggregates health + orchestration routers.
- `main.py` — **Rewritten.** FastAPI app with `lifespan`, CORS, router mount at `/api/v1`. **Imports verified ✅**

### Dashboard (`dashboard/`) ✅
- React + Vite v8 + TypeScript + Tailwind v3 + shadcn/ui (New York, slate)
- `src/types/api.ts` — TS interfaces matching all backend Pydantic models; includes `ArchitectureAlternative` (11 fields: name, label, score, 5 pillar scores, monthly_cost_estimate, rationale, trade_offs, recommended)
- `src/lib/api.ts` — API client with `orchestrate()`, `streamOrchestrate()` (SSE via `@microsoft/fetch-event-source`), `checkHealth()`, `checkReady()`
- `src/context/PipelineContext.tsx` — React Context: messages, agent progress, result, streaming state; `architecture_alternatives` passed through from SSE pipeline_complete
- `src/hooks/useHealth.ts` — Polls `GET /ready` every 30s
- Chat UI: `ChatMessage`, `ChatInput`, `AgentProgress` (5-step stepper), `ChatContainer`
- Results: `ExecutiveSummary`, `CostComparisonTable`, `CostComparisonChart` (Recharts), `ProviderCard`, `ComplianceReport`, `RfpDocument`, **`ArchitectureRadarChart`** (P16e — Recharts RadarChart 4 options × 5 WAF axes, score table, rationale cards), **`FeedbackWidget`** (P16f — 1-5 star rating, optional comment, logs to LangFuse via `POST /feedback`)
- Pages: `/chat` (ChatPage), `/results` (tabbed: **Overview, Architecture, Cost Analysis, Compliance, RFP Document** — 5 tabs), 404
- `npm run build` — **0 errors** ✅

---

## Completed Priority Queue — P0 through P14 (All Done)

All priorities through P14 are complete. Full per-fix breakdowns are in `PROJECT_SPEC.md` §17–§20.

| Phase | Summary |
|-------|---------|
| P0 — LangFuse tracing | Fixed env var (`LANGFUSE_HOST`), flush calls, parent trace |
| P1–P3 — Agent quality | Clarifier multi-turn, Profiler category/GPU guard, Sizer SKU selection fixed |
| P4–P6 — LLM clarifier + WAF | `llm_clarify_turn()`, WAF two-role architect, provider strategy field |
| P7–P8 — Cal Fire E2E fixes | Scale propagation, compliance tags, CDN detection, AI/ML guard, 10/10 E2E ✅ |
| P9 — Sizer SKU bugs | 6 bugs fixed (CDN, container vCPU cap, DB hourly filter, Redis routing, storage tier) |
| P10 — RFP content gaps | Gov framing, traceability matrix, greenfield phases, WCAG/StateRAMP tables |
| P11–P13 — Sizer bugs cont. | DB $0, EC2 SQL license, storage quantity, GIR/ZIA tiers, cache poisoning fixed |
| P14 — DB undersizing | Memory/vCPU filter added; `db.r6i.large` ($182/mo) replaces `db.t3.micro` ($13) |
| P15 — Agentic enhancements | SERVERLESS sizer path, architecture_selector, pricing_validator, profiler decomposition+streaming detection, Router agent, Validator agent, session store, graph+state updates, RFP alternatives section, dynamic WAF weights — 11 items, 142 tests ✅ |


## What Needs to Be Fixed/Built Next ❌

> **P15 COMPLETE ✅ — All 11 items done. 142 tests passing. Move to P16.**

### Priority 16 — Dissertation Completeness Features (Next)

| ID | Feature | Description | Priority |
|----|---------|-------------|----------|
| **P16a** | Real-cost architecture comparison | `_extract_costs_from_sized_results()` in `architecture_selector.py` sums actual `SizedWorkloadResult.monthly_cost_usd` per provider, grouped by category (SERVERLESS vs always-on). Container cost = sum of all non-serverless AWS results. Knative = 90% of container cost. Lambda = actual SERVERLESS-category results (falls back to heuristic). Bug fix: containers `scale_score` now capped at 1.0 when `burst_ratio < 5`. 142/142 tests pass. | ✅ Done |
| **P16b** | Self-hosted serverless workload type | `SERVERLESS_COMPUTE` added to `_BINPACKED_CATEGORIES` + `_SERVICE_NAME_MAP` in sizer (EKS/AKS/GKE nodes). `_relabel_for_knative()` helper in profiler relabels CONTAINER → SERVERLESS_COMPUTE when `execution_plan.preferred_architecture == "self_hosted_serverless"` OR when prior architecture winner is `self_hosted_serverless`. `ExecutionPlan` TypedDict gains `preferred_architecture` field. Sizer rationale says "Knative/KEDA on EKS/AKS/GKE" for SERVERLESS_COMPUTE workloads. 142/142 tests pass. | ✅ Done |
| **P16c** | RFP compliance verification | Post-generation LLM pass against StateRAMP Moderate control list (17 NIST 800-53 Rev 5 families). `_generate_stateramp_gap_analysis()` async LLM function + `_heuristic_gap_analysis()` keyword fallback + `_render_gap_analysis_section()` renderer. `_STATERAMP_MODERATE_FAMILIES` constant with 17 entries. TOC updated with section 18 when has_stateramp. Appendix A appended after traceability matrix in `run_rfp_writer_node`. 142/142 tests pass. | ✅ Done |
| **P16d** | Multi-scenario benchmark script | `scripts/benchmark_scenarios.py` — 3 scenarios (Cal Fire/Government, Healthcare SaaS, E-Commerce). Bypasses Clarifier by injecting pre-built WorkloadRequest. `_merge_state()` helper applies LangGraph append semantics for list fields when calling agents directly. `--dry-run` mocks both LLM AND pricing APIs. Writes `data/benchmark_results.json` + `data/benchmark_report.md`. Dry-run: 3/3 ✅ (WAF 67-100%, RFPs 36-39KB, all 5 agents pass). 142/142 tests pass. | ✅ Done |
| **P16e** | Architecture radar chart (dashboard) | React component: 4 options x 5 WAF axes as spider chart. Makes the comparison visual and tangible. `ArchitectureRadarChart.tsx` + `ArchitectureAlternative` TS interface. Validator emits per-pillar scores (reliability/cost/scale/compliance/latency). Results page has 5 tabs (Architecture tab added). SSE pipeline_complete + OrchestrationResponse carry architecture_alternatives. npm build: 0 errors ✅ | ✅ Done |
| **P16f** | User feedback capture | 1-5 star rating widget (`FeedbackWidget.tsx`) on Results page RFP tab. `POST /api/v1/feedback` FastAPI route logs a LangFuse score event (name=`user_satisfaction`, value=rating). Inline SVG stars (no extra deps). Optional comment textarea. 142 tests, 0 TS errors ✅ | ✅ Done |

### Next Immediate Task

**All P16 items are complete ✅**

The system is feature-complete for dissertation submission. What remains is optional polish:
- P17 (optional): End-to-end live demo run with real LLM — record/screenshot results for the dissertation appendix.
- P17 (optional): Write the dissertation final report in `docs/FINAL_REPORT_CONTENT.md` — the scaffolding already exists.



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

### Pricing Accuracy Bugs Fixed (7 May 2026)
5 root-cause fixes in `src/agents/sizer.py`:
1. **Azure PostgreSQL "Auto Tune"**: Azure DB pricing includes add-on meters ("Auto Tune" at $0.012/hr = $8.76/mo) that look like the cheapest compute option but are not. Post-`_filter_database_candidates` Azure-specific filter now keeps only `product_name` containing "Compute" and excludes add-on keywords.
2. **GCP Redis service name**: `_DATABASE_ENGINE_MAP` had `"Cloud Memorystore"` (wrong) → corrected to `"Cloud Memorystore for Redis"` / `"Cloud Memorystore for Memcached"`.
3. **DATABASE hourly filter**: Was `unit_of_measure in ("1 Hour", "1 hour")` — missed Azure's `"1/Hour"` format. Changed to `c.is_hourly` (string-contains check handles all variants).
4. **Storage per-GB guard** (two parts): (a) `_filter_storage_candidates` now pre-filters to items where unit contains "GB"/"GiB" before applying tier preferences — eliminates per-IOPS, per-disk, per-request SKUs. (b) Storage scaling code now checks `any(u in unit.lower() for u in ("gb","gib"))` before multiplying by `storage_gb`.
5. **Cross-provider region mapping**: Added `_AWS_TO_AZURE_REGION` + `_AWS_TO_GCP_REGION` dicts. `_get_region_for_provider()` auto-maps AWS preferred_region to correct Azure/GCP region when provider-specific entry is still the model default.
- Pricing cache cleared (`data/sku_cache.db`) — will re-fetch with corrected regions/filters on next run.

### P15 Complete — Key Lessons (25 May 2026)
- **`langgraph-checkpoint-sqlite` not installed**: `langgraph.checkpoint.sqlite` module does not exist. `SessionStore.make_checkpointer()` returns `MemorySaver` instead. For real cross-request session persistence, add `langgraph-checkpoint-sqlite` to `pyproject.toml` and update `make_checkpointer()` to return `SqliteSaver`.
- **`include_router=False` default in `build_graph()`**: Router is opt-in to avoid breaking existing pipeline. Turn-1 sessions go START → clarifier → profiler → sizer → finops → validator → rfp_writer. Multi-turn sessions need `build_graph(..., include_router=True)`.
- **`ExecutionPlan` is TypedDict not Pydantic**: Cannot use `.model_dump()` — use direct dict access `plan["intent"]`. Matches LangGraph state pattern.
- **Dynamic WAF weights (P15j)**: Additive deltas from `_PRIORITY_BOOSTS`, clamped to [0.01, 0.70] then normalised to 1.0. Multiple keyword signals stack. `derive_weights_from_workload()` only logs when signals are actually detected.
- **State list invariant**: `agent_executions` is `Annotated[list, operator.add]` — always append, never replace. `create_initial_state()` now seeds 7 agent names: `{"clarifier", "profiler", "sizer", "finops", "rfp_writer", "validator", "router"}`.

### P15 Design (9 May 2026 — final pre-implementation)
- **Router+Orchestrator Agent** (`src/agents/router.py`): LLM intent classifier **and execution planner** in one step. Produces `ExecutionPlan` (not just a route label): intent, `agents_to_run`, `scope_components` (specific component names), `rfp_amendment_sections`, `amendment_delta`, `confidence`. Downstream agents read `execution_plan` to understand exactly what to reprocess. Entry conditional: if `state.get("rfp_document")` → router+orchestrator, else → clarifier. Uses Principal Architect Reasoning (§22g). Full design in PROJECT_SPEC §22b.
- **Validator Agent** (`src/agents/validator.py`): 5-check quality gate. Check 0 is pricing integrity (calls `pricing_validator.py` FIRST — prevents wrong vendor selection from bad SKU matches). Checks 1–4: architecture_selector, sizing adequacy, budget fit, WAF compliance. Full design in PROJECT_SPEC §22c.
- **Pricing Comparison Validator** (`src/engines/pricing_validator.py`): 7-check pure algorithmic engine. No LLM. Validates that cross-provider cost comparison is valid before FinOps picks a vendor. Key checks: size_adequacy (memory/vCPU ≥ 80% of required), price_anomaly (> 5× median = wrong SKU family), category_match (DATABASE workload → RDS, not EC2). Full design in PROJECT_SPEC §22h.
- **Principal Architect Reasoning** (§22g): ALL agent system prompts now include a structured `<architect_reasoning>` CoT template — 5 steps: understand, identify risks, evaluate alternatives, challenge assumptions, commit with rationale. Scratchpad captured in LangFuse as `reasoning` span. NOT returned to user. Each agent has a key forcing question specific to its role.
- **Session Persistence** (`src/services/session_store.py`): `langgraph-checkpoint-sqlite` → `SqliteSaver`. `graph.compile(checkpointer=checkpointer)`. Thread ID = `session_id`. CRITICAL: current `orchestration.py` deletes `_clarify_sessions[request_id]` on `status=ready` — must NOT do this. Full design in PROJECT_SPEC §22d.
- **Architecture Selector** (`src/engines/architecture_selector.py`): 4-option unbiased scoring. Managed Lambda wins at low avg RPS (< ~300 crossover); Knative wins at sustained high throughput. 3 signal groups: traffic pattern + workload characteristics + compliance. Dynamic WAF weights via P15j. Full design in PROJECT_SPEC §22f.
- **Build order**: P15b → P15a → P15k → P15c → P15g → P15d → P15e → P15f → P15h → P15i → P15j
- **Biggest gap vs Presidio**: Serverless never evaluated; no session continuity; amendments trigger full re-run; no pricing integrity check before vendor selection.

### Credentials & Environment
- **AWS STS**: Temporary creds (`ASIA` prefix) expire. Run `aws sso login` → update `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`. Bedrock is DENIED (IAM policy). AWS used only for Pricing API.
- **GCP / Vertex AI (PRIMARY LLM)**: ADC via `gcloud auth application-default login`. Project: `dissertation-rj`. Model: `gemini-2.5-pro`. Env: `VERTEXAI_PROJECT=dissertation-rj`, `VERTEXAI_LOCATION=us-central1`.
- **Gemini 3.1 Pro Preview**: Returns 404 for project `dissertation-rj`. Enable via GCP Console → Vertex AI → Model Garden. Use `gemini-2.5-pro` until enabled.
- **Always use `uv run python`**: System `python3` is macOS 3.9 — incompatible (requires 3.13+). Use `uv run python` from project root.
- **Cal Fire E2E script**: `uv run python scripts/test_cal_fire_e2e.py` — 10 checks against mocked LLM + Cal Fire enriched input. Run after any clarifier changes.
- **Reference RFPs**: `docs/Presidio_Response_Cal Fire_Draft 10.7.25 (2) (1).html` and `docs/Mass Technology Collaborative Template FINAL (2) (1).html`.

### Test & Model Gotchas
- **NormalizedPriceItem**: Always provide `retail_price` (float), `unit_of_measure` (str), `effective_date` (datetime with tzinfo) — all required, no defaults.
- **BinPackingResult fields**: `total_nodes` (not `node_count`), `nodes` (not `packed_nodes`), `packing_efficiency_pct` (not `avg_cpu_utilization_pct`).
- **WorkloadRequest.target_providers**: field is `target_providers`, not `providers`.
- **WorkloadRequirement.notes**: `str` with default `""`, NOT `str | None` — pass `""` not `None`.
- **run_*_node() returns a patch**: Only NEW state fields returned (LangGraph reducer). `result["messages"]` = only new messages added. Assert `>= 1`.
- **ComponentProfile.recommended_instance_families**: It's a `list[str]`, NOT `recommended_family` (that attribute doesn't exist).
- `get_settings.cache_clear()` resets the lru_cache singleton in tests.

### LangFuse
- SDK v3: `from langfuse import observe` (NOT `langfuse.decorators`)
- Self-hosted: `docker-compose.langfuse.yml` (6 containers). `ENCRYPTION_KEY` must be real 64-char hex. Pre-configured via `LANGFUSE_INIT_*` env vars.
- Env var: `LANGFUSE_HOST` (was `LANGFUSE_BASE_URL` — old name doesn't work).
- Flush: `_flush_langfuse()` in `orchestration.py` calls `Langfuse().flush()` in `finally` blocks — mandatory (SDK batches async).
- `@observe()` nesting: Lives ONLY on graph node wrappers and agent sub-functions. NOT on `run_*_node()` entry points (removed in P0 to avoid double nesting).
- `auth_check()` throws `ValidationError` on self-hosted (missing `organization` field) — benign, tracing still works.

### Pricing & Provider Notes
- **GCP pricing**: per-vCPU + per-GB-RAM components, not per-instance. `vm_specs.compose_gcp_vm_instances()` synthesizes 31 standard machine types with calculated hourly prices.
- **Azure pricing**: API returns no vCPU/memory. `vm_specs.parse_azure_vm_specs()` parses specs from ARM SKU names using regex + family→memory ratio table.
- **Azure reserved prices**: `retailPrice` = total upfront cost, NOT per-hour — `monthly_cost_estimate` handles by checking tier first.
- **AWS adapter**: boto3 sync calls wrapped in `asyncio.to_thread()`. Bedrock model ID requires `us.` prefix (cross-region inference profile) — bare `anthropic.*` returns `ValidationException`.
- **GCP adapter**: gRPC sync calls wrapped in `asyncio.to_thread()`. Price = `units + nanos/1e9`.
- **PricingService**: Single entry point for agents — never call adapters directly. `PricingService.close()` on shutdown (handled by lifespan).
- **SKU cache**: `data/sku_cache.db` auto-created. Delete to force fresh API data.
- `compare_across_providers()` designed for FinOps agent's cross-provider workflow.

### Implementation Notes
- **WAF compliance API**: `evaluate_compliance()` (NOT `run_compliance_checks`).
- **Profiler category priority**: AI_ML first, COMPUTE last — prevents generic `vcpus` check from shadowing `database_engine` or `gpu_count`.
- **Profiler graceful degradation**: Falls back to `_heuristic_rationale()` when LLM unavailable.
- **FinOps RI/Spot fallback rates**: `_RI_DISCOUNT_RATES` (AWS 30/45%, Azure 35/50%, GCP 25/40%) and `_SPOT_DISCOUNT_RATES` (AWS 70%, Azure 80%, GCP 60%) applied when live pricing returns no reserved/spot data. `source: "estimate"` flag marks these entries.
- **FinOps spot eligibility**: `_SPOT_INELIGIBLE` = {DATABASE, STORAGE, NETWORKING, MANAGEMENT, SECURITY}.
- **FinOps TCO**: `_compute_tco(monthly, years, growth_pct)` uses compound growth (default 15%/yr). Stored in `state["kpis"]["tco_projections"]` with 1yr/3yr/5yr values.
- **Dashboard**: Tailwind v3 (NOT v4) — pinned for shadcn/ui compatibility. SSE via `@microsoft/fetch-event-source` (native `EventSource` is GET-only). `VITE_API_BASE_URL=http://localhost:8000/api/v1` in dashboard `.env`. `tsconfig.app.json` has `ignoreDeprecations: "6.0"` for TS 7.0 warning.
- **CORS**: Wide open `allow_origins=["*"]` — tighten for production.
- `.venv/` at Python 3.13.0, all deps synced via `uv sync --all-extras`.
