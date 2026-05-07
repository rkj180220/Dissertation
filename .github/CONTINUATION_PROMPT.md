# Cloud Orchestrator IDSS — Continuation Prompt

> **Last Updated**: 9 May 2026 (Router+Orchestrator Agent with ExecutionPlan; Pricing Comparison Validator engine P15k; Principal Architect Reasoning pattern §22g/§22h; P15 table updated; build order finalised.)

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

### Sizer Agent (`src/agents/sizer.py`) — ~1470 lines ✅ P13 fixes applied
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
- `graph.py` — **247 lines.** `StateGraph` with 5 nodes, conditional Clarifier loop. `build_graph(llm, pricing_service)` returns compiled graph. `@observe()` + structlog. **P15h will expand to 7 nodes** (+ Router at START, + Validator before rfp_writer) with `SqliteSaver` checkpointer. **Imports verified ✅**

### API Layer (`src/api/`)
- `dependencies.py` — **114 lines.** ASGI `lifespan()`: loads settings, wires observability, creates LLM, registers providers, initialises PricingService, compiles graph. Dependency providers for routes. **Imports verified ✅**
- `routes/health.py` — **96 lines.** `GET /health` (liveness), `GET /ready` (deep readiness). **Imports verified ✅**
- `routes/orchestration.py` — **~404 lines.** `POST /orchestrate` (full pipeline → JSON), `POST /orchestrate/stream` (SSE streaming), `POST /orchestrate/clarify` (LLM multi-turn session). **P15h changes needed**: add `session_id`, stop deleting session on `status=ready`. **Imports verified ✅**
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


## What Needs to Be Fixed/Built Next ❌

> **Run #6 complete (P13 ✅). P14 fixed (DB undersizing). Run #7 needed to verify P14. P15 = 10 items (adds self-hosted serverless + dynamic weights). P16 = dissertation-completeness features.**

### Priority 15 — NOT STARTED ❌ (Next Major Phase — 10 items)

Full design in PROJECT_SPEC §21 (gap analysis) and §22 (Router/Validator/Session/Architecture design):

| ID | Feature | Component(s) | Description | Priority |
|----|---------|-------------|-------------|----------|
| **P15a** | Architecture alternatives engine | `src/engines/architecture_selector.py` (NEW) | Score **4 options**: managed-serverless (Lambda), **self-hosted-serverless (Knative/KEDA)**, containers (EKS), hybrid. **Unbiased**: managed Lambda wins at low avg RPS (< ~300 RPS crossover); Knative wins at sustained high RPS. Uses REAL pricing for cost scores — 3 signal groups: traffic pattern (burst ratio + cost crossover), workload characteristics (latency, stateful), compliance. | 🔴 Critical |
| **P15b** | Serverless pricing path | `src/models/cloud_resource.py` + `src/agents/sizer.py` | Add `SERVERLESS` to `ServiceCategory`; add Lambda/DynamoDB pricing path; add Knative self-hosted path (reuses K8s node pricing) | 🔴 Critical |
| **P15c** | Profiler microservice decomposition | `src/agents/profiler.py` | Decompose `enriched_input` into 5-8 individual microservices so bin-packing works with multiple inputs | 🔴 Critical |
| **P15d** | **Router+Orchestrator Agent** | `src/agents/router.py` (NEW) | LLM intent classifier **AND execution planner**. Produces `ExecutionPlan`: agents to run, scope (full/delta), affected component names, RFP sections to amend. Downstream agents read `execution_plan` — they never make routing decisions themselves. Uses Principal Architect Reasoning (§22g). | 🔴 Critical |
| **P15e** | **Validator Agent** | `src/agents/validator.py` (NEW) | 5-check quality gate: [0] Pricing integrity (calls `pricing_validator.py`), [1] Architecture correctness (architecture_selector), [2] Sizing adequacy, [3] Budget fit, [4] WAF compliance. Auto-runs after FinOps; on-demand via Router. | 🔴 Critical |
| **P15f** | **Session persistence** | `src/services/session_store.py` (NEW) + `graph.py` | LangGraph `SqliteSaver` checkpointing. Thread ID = `session_id`. State persists across requests. | 🔴 Critical |
| **P15g** | Streaming/queue/analytics | `src/agents/profiler.py` | Detect Kinesis, SQS, Redshift from `enriched_input` keywords | 🟠 High |
| **P15h** | Graph + API refactor | `graph.py` + `state.py` + `orchestration.py` | Add router+orchestrator node, validator node, conditional edges, checkpointing. `ExecutionPlan` + `session_id` in state. `session_id` in API request/response. | 🔴 Critical |
| **P15i** | RFP Architecture Alternatives | `src/agents/rfp_writer.py` | All 4 options with WAF scores + REAL costs from `validation_report`. Pricing caveats subsection if `error_count > 0`. | 🔴 Critical |
| **P15j** | Dynamic WAF weights | `src/engines/architecture_selector.py` | Clarifier detects user priority → adjusts scoring weights at runtime. | 🟠 High |
| **P15k** | **Pricing Comparison Validator** | `src/engines/pricing_validator.py` (NEW) | 7-check apples-to-apples validator (size adequacy, tier consistency, price anomaly, staleness, category match, provider parity, memory:vCPU ratio). Pure algorithmic — no LLM. Prevents wrong vendor selection from bad SKU matches. See §22h. | 🔴 Critical |

**Build order**: P15b → P15a → P15k → P15c → P15g → P15d → P15e → P15f → P15h → P15i → P15j

### Priority 16 — Dissertation Completeness Features (After P15)

| ID | Feature | Description | Priority |
|----|---------|-------------|----------|
| **P16a** | Real-cost architecture comparison | Price all 4 architectures using actual sizer output — Lambda per-invocation math + Knative K8s node pricing + container always-on. (Note: partially covered by P15a + P15k — P16a ensures the pricing flow is wired end-to-end in Validator.) | 🔴 Critical |
| **P16b** | Self-hosted serverless workload type | After architecture_selector picks self-hosted-serverless, profiler relabels CONTAINER workloads as SERVERLESS_COMPUTE (Knative). Sizer queries K8s nodes at pod density, not Lambda pricing. | 🔴 Critical |
| **P16c** | RFP compliance verification | Post-generation LLM pass against StateRAMP Moderate control list. Outputs Compliance Gap Analysis appendix in RFP. | 🟠 High |
| **P16d** | Multi-scenario benchmark script | Run Cal Fire + healthcare + e-commerce through full pipeline. Compare vs. reference architectures. Dissertation evaluation chapter data. | 🟠 High |
| **P16e** | Architecture radar chart (dashboard) | React component: 4 options x 5 WAF axes as spider chart. Makes the comparison visual and tangible. | 🟡 Medium |
| **P16f** | User feedback capture | 1-5 star rating after RFP generated, logged to LangFuse. Dissertation accuracy reporting. | 🟡 Medium |

### Next Immediate Task

**Run #7 first (to verify P14), then start P15b:**

**Run #7:**
1. Refresh AWS credentials (`aws sso login` or re-issue from AWS console)
2. **Do NOT delete `data/sku_cache.db`** — it has good P13-era data
3. Restart API: `uv run uvicorn src.main:app --port 8001`
4. Run Cal Fire clarify session → `/orchestrate`
5. Verify: PostgreSQL = `db.r6i.large` (~$182/mo), Cache = current-gen >9.6 GiB, NOT `cache.t1.micro`
6. Expected total: ~$916/mo
7. Save to `docs/test-rfp-7.md`

**Then P15b (serverless model):**
- Add `SERVERLESS = "serverless"` to `ServiceCategory` in `cloud_resource.py`
- Add Lambda + DynamoDB pricing path in `sizer.py` (new `_size_serverless_workload()` method)
- Lambda: price = requests/mo × avg_duration_ms × memory_gb × $0.0000166667/GB-sec
- DynamoDB: price = (read_units × $0.25/RCU + write_units × $1.25/WCU) per million
- **Also add Knative/self-hosted path**: reuse existing K8s/CONTAINER node pricing; pod_density factor (e.g. 8 pods/node) divides node cost across workloads
- CRITICAL: architecture_selector will call BOTH paths — one for Lambda cost estimate, one for Knative cost estimate



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
