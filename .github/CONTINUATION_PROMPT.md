# Cloud Orchestrator IDSS — Continuation Prompt

> **Purpose**: Paste this into a NEW Copilot chat window to resume work from exactly where we left off.
> **Last Updated**: 17 April 2026 (Full pipeline gap analysis — agent quality, SKU selection, RFP depth, LangFuse tracing)

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
- `factory.py` — `get_llm(provider, model, **kwargs) → BaseChatModel`. Lazy imports (bedrock / gemini). Agents NEVER import provider classes directly.
- **Live-tested**: `us.anthropic.claude-sonnet-4-5-20250929-v1:0` via AWS Bedrock ✅

### Data Models (`src/models/`)
- `cloud_resource.py` — `CloudProvider` enum (aws/azure/gcp), `ServiceCategory` enum (14 values). Legacy `ComputeSKU`/`StorageSKU` kept for backward compat with engines only.
- `pricing.py` — `PricingTier` enum (8 values). `NormalizedPriceItem` with `monthly_cost_estimate` property (handles reservation upfront vs hourly).
- `workload.py` — `EnvironmentType`, `WorkloadTier`, `ScalingPattern` enums. `ResourceSpec` (all 14 categories), `WorkloadRequirement`, `ComponentProfile`, `WorkloadProfile`, `WorkloadRequest`. Legacy VMWorkload/ContainerWorkload/StorageRequirement kept at bottom.
- `conversation.py` — `MessageRole`, `ClarificationStatus`, `ClarificationPriority` enums. `ChatMessage`, `ClarificationQuestion`, `ConversationState` (with `should_continue_clarifying` property).
- `recommendation.py` — `PackedNode`, `BinPackingResult`, `ProviderCostBreakdown`, `CostComparison`, `ComplianceCheckResult`, `ComplianceReport`, `CloudRecommendation`. All use `NormalizedPriceItem`.
- **Verified**: 41/41 checks passed (all imports, instantiation, behavior)

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

### Profiler Agent (`src/agents/profiler.py`) — ~600 lines
- Takes `WorkloadRequest` → produces `WorkloadProfile` with `ComponentProfile` per workload
- Priority-ordered category resolution (AI_ML > DATABASE > CONTAINER > COMPUTE)
- Tier-based resource multipliers + environment scaling factors
- Instance-family recommendation per provider via `_INSTANCE_FAMILY_MAP`
- LLM-enriched rationale with heuristic fallback on LLM failure
- `run_profiler_node(state, llm)` entry point
- `@observe()` + structlog throughout
- **Verified**: 23/23 checks passed ✅

### Sizer Agent (`src/agents/sizer.py`) — 920 lines
- Category-aware SKU selection: scored (COMPUTE, AI_ML), binpacked (CONTAINER), cheapest-price (all others)
- `_SERVICE_NAME_MAP` (24 provider×category→service_name), scoring engine + bin-packing engine integration
- LLM summary with heuristic fallback
- `run_sizer_node(state, llm, pricing_service)` entry point
- `@observe()` + structlog throughout
- **Verified**: imports OK ✅

### FinOps Agent (`src/agents/finops.py`) — 744 lines
- Groups `SizedWorkloadResult` by provider, queries RI/spot pricing, builds `ProviderCostBreakdown` per provider
- Assembles `CostComparison` with budget analysis + savings identification
- LLM summary with heuristic fallback
- `run_finops_node(state, llm, pricing_service)` entry point
- `@observe()` + structlog throughout
- **Verified**: imports OK ✅

### RFP Writer Agent (`src/agents/rfp_writer.py`) — 653 lines
- Generates Markdown RFP with 7 sections (header, exec summary, workloads, SKU selection, costs, compliance, vendor shortlist)
- Uses `evaluate_compliance()` from `waf_compliance` engine
- LLM executive summary with heuristic fallback
- `run_rfp_writer_node(state, llm)` entry point
- `@observe()` + structlog throughout
- **Verified**: imports OK ✅

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

## What Needs to Be Fixed/Built Next ❌

### First Pipeline Run Results (17 Apr 2026)
The pipeline runs end-to-end but output quality is **subpar**:
- Clarifier doesn't ask enough questions (no architecture, performance, SLA, security depth)
- Profiler misclassifies K8s as AI_ML (gives it GPU, 76.8 GB RAM)
- Sizer picks wrong SKUs (SageMaker for K8s, MariaDB db.t2.micro for PostgreSQL)
- Costs unrealistically low ($18-47/mo vs expected $500-1,500/mo for production)
- RFP is thin (~4,690 chars) vs reference RFPs (30-60 pages)
- LangFuse traces NOT showing up (env var mismatch + no flush)

### Priority 0 — Fix LangFuse Tracing (BLOCKING)
1. **Fix env var**: `.env` has `LANGFUSE_BASE_URL` — rename to `LANGFUSE_HOST` (settings field = `host`, prefix = `LANGFUSE_`)
2. **Add `flush()` call** at end of pipeline in `orchestration.py` route handlers (both sync and SSE)
3. **Add parent trace context** — wrap top-level `graph.ainvoke()` / `graph.astream()` with `@observe()` and pass `session_id=request_id`
4. **Remove double nesting** — `@observe()` on graph node wrappers AND `run_*_node` creates duplicate traces. Keep only one level.

### Priority 1 — Fix Agent Quality (CRITICAL)

#### 1a. Clarifier: Deep Requirement Gathering
- Add questions for: **architecture** (microservice topology, API patterns, data flow), **performance** (latency p99, throughput, concurrent users), **availability** (uptime SLA, RPO/RTO), **security** (VPC isolation, encryption, IAM), **data** (volume, growth rate, retention, classification), **DR strategy**, **cost model preferences** (reserved vs on-demand, spot tolerance)
- Fix `_extract_workloads_from_text()`: "3 microservices on K8s" → 3 separate CONTAINER workloads + 1 K8S_CLUSTER workload (not a single workload with replicas=3)
- Add K8s node pool awareness: cluster management fee + node pool + container workloads are separate cost items

#### 1b. Profiler: Fix Category Resolution
- Fix `_CATEGORY_PRIORITY` — K8s/container workloads should NEVER resolve to AI_ML unless user explicitly mentions GPU/ML
- Add guard: if `ResourceSpec.gpu_count == 0` and no ML/AI keywords, NEVER assign AI_ML
- Fix resource estimation: 3 microservices = 3× (0.5-2 vCPU, 1-4 GB) = 1.5-6 vCPU total, NOT 9 vCPU + 76.8 GB + GPU
- Add K8s cluster-level profiling: management fee, ingress controller, monitoring sidecar overhead

#### 1c. Sizer: Fix SKU Selection
- Fix `_SERVICE_NAME_MAP` — CONTAINER/K8s workloads should map to: `AmazonEKS`+`AmazonEC2` (AWS), `Azure Kubernetes Service`+`Virtual Machines` (Azure), `Kubernetes Engine`+`Compute Engine` (GCP)
- Fix database engine propagation: clarifier captures "PostgreSQL" → must flow to sizer so it queries RDS PostgreSQL / Azure PostgreSQL / Cloud SQL PostgreSQL specifically
- Add **node pool sizing**: for K8s, size `N × instance_type` not a single SKU
- Add **ancillary cost items**: load balancer, NAT gateway, data transfer, container registry, monitoring, DNS
- Fix scoring: e2-micro should NEVER be selected for a 9-vCPU workload — min-spec filter is broken

#### 1d. FinOps: Complete Cost Modeling
- Add ancillary costs: EKS/AKS/GKE fee ($72-100/mo), load balancer, NAT gateway, data transfer, monitoring, backup
- Actually query reserved/spot pricing and populate the savings table (currently all "N/A")
- Add 3-year and 5-year TCO projections
- Model growth: if user provides growth rate, project costs forward

#### 1e. RFP Writer: Produce Enterprise-Grade Output
Based on Mass Tech Collaborative and Cal Fire/Presidio reference RFPs, add sections:
- **Architecture description** — topology, data flow, network architecture (can be Mermaid/ASCII)
- **Detailed tech specs** — per-component: service, version, config, capacity, instance type
- **SLA / uptime guarantees** — targets, measurement methodology, penalty implications
- **Security architecture** — encryption standards, IAM policies, network segmentation, WAF, DDoS
- **Migration / implementation plan** — phased (discovery → build → test → launch), milestones, timeline
- **Disaster recovery / backup** — DR strategy, RPO/RTO, failover procedure, backup schedule
- **Multi-year cost projection** — per-component, per-year, 3-5 year TCO with growth
- **Compliance evidence** — SOC2, ISO 27001, HIPAA, FedRAMP (per provider capabilities)
- **Assumptions & exclusions** — scope boundaries
- Target output: **15-30 pages** (15,000-30,000 characters)

### Priority 2 — Data Model Enhancements
- Add to `WorkloadRequirement`: `database_engine`, `latency_p99_ms`, `throughput_rps`, `concurrent_users`, `uptime_sla`, `rpo_minutes`, `rto_minutes`, `data_growth_rate_pct`, `spot_eligible`
- Add `ServiceCategory.KUBERNETES` (separate from CONTAINER) to model cluster management fees
- Add `AncillaryCost` model for load balancers, NAT gateways, data transfer, etc.

### Priority 3 — Testing
1. `tests/conftest.py` — shared pytest fixtures with mock LLM/PricingService
2. `tests/unit/` — all agents + engines
3. `tests/integration/` — full workflow tests

---

## Key Rules (from `.github/copilot-instructions.md`)
1. **`@observe()` + structlog on every LangGraph node** — log entry (inputs) + exit (output summary) + errors with `exc_info=True`
2. **Agents use `BaseChatModel` only** — never import `ChatBedrockConverse` etc. in agent files
3. **Agents call `PricingService`** — never call adapters directly
4. **State lists are append-only** — use `Annotated[list, operator.add]` reducers, never replace
5. **`from langfuse import observe`** — SDK v3 import (NOT `langfuse.decorators`)

---

## Auth / Credentials (already set in `.env`)
- **AWS**: Temporary STS session credentials (`ASIA` prefix) — expire after ~8h. Refresh from SSO portal if you get `ExpiredTokenException`. Used for both Pricing API AND Bedrock.
- **GCP**: ADC via `gcloud auth application-default login` → `~/.config/gcloud/application_default_credentials.json`. Project: `dissertation-rj`.
- **Azure**: No credentials needed (public REST API).
- **LangFuse**: Self-hosted via Docker Compose (`docker-compose.langfuse.yml`). Keys: `pk-lf-local-dissertation` / `sk-lf-local-dissertation`. UI at `http://localhost:3000`. Start with `docker compose -f docker-compose.langfuse.yml up -d`.

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

- `.venv/` already created with Python 3.13.0 and all deps synced
- `get_settings.cache_clear()` resets the singleton in tests
- LangFuse gracefully degrades when keys missing (warns, doesn't crash)
- LangFuse SDK v3: `from langfuse import observe` (NOT `langfuse.decorators`)
- LangFuse self-hosted: `docker-compose.langfuse.yml` runs 6 containers (postgres, clickhouse, redis, minio, worker, web). ENCRYPTION_KEY must be real 64-char hex (not zeros). Pre-configured org/project via `LANGFUSE_INIT_*` env vars.
- **LangFuse env var**: Settings reads `LANGFUSE_HOST` (not `LANGFUSE_BASE_URL`). The env var name must match prefix + field name.
- **LangFuse flush**: Must call `langfuse.flush()` at end of pipeline or traces are lost (SDK batches async).
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
