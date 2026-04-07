# Cloud Orchestrator IDSS — Continuation Prompt

> **Purpose**: Paste this into a NEW Copilot chat window to resume work from exactly where we left off.
> **Last Updated**: 7 April 2026 (Phase 1 + Phase 2 complete — all agents, graph, API layer built)

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

---

## What Needs to Be Built Next ❌

### Next Immediate Task — Validation Test Scripts
Create validation scripts for the 3 new agents (similar to existing patterns in `scripts/`):
- `scripts/test_sizer_agent.py` — verify Sizer imports, state flow, SKU selection logic
- `scripts/test_finops_agent.py` — verify FinOps imports, cost breakdown assembly
- `scripts/test_rfp_writer_agent.py` — verify RFP Writer imports, section generation, compliance integration

### Then (in order)
1. **End-to-end integration test** — Run the full pipeline (`build_graph()` → `ainvoke()`) with a sample `WorkloadRequest` and validate all state fields populated
2. **Dashboard scaffold** (`dashboard/`) — React + Vite + TypeScript + Tailwind + shadcn/ui
3. **Chat UI + SSE streaming** — Connect to `POST /api/v1/orchestrate/stream`, display agent progress
4. **Cost comparison + recommendation display** — Render cost tables, charts, RFP document
5. **pytest fixtures** (`tests/conftest.py`) — shared mocks for LLM, PricingService, state
6. **Unit tests** (`tests/unit/`) — all agents + engines
7. **Integration tests** (`tests/integration/`) — full workflow tests

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
- **LangFuse**: Keys not yet set — system degrades gracefully (harmless 401 warning in logs).

---

## Dev Commands
```bash
# Run any script
uv run python scripts/test_aws_adapter.py

# Sync deps after pyproject.toml change
uv sync --all-extras

# Run tests (when they exist)
uv run pytest
```
```

---

## Notes for Future Me

- `.venv/` already created with Python 3.13.0 and all deps synced
- `get_settings.cache_clear()` resets the singleton in tests
- LangFuse gracefully degrades when keys missing (warns, doesn't crash)
- LangFuse SDK v3: `from langfuse import observe` (NOT `langfuse.decorators`)
- Azure reserved prices: `retailPrice` = total upfront cost, NOT per-hour (even though `unitOfMeasure='1 Hour'`) — `monthly_cost_estimate` handles this by checking tier first
- WAF compliance engine public API is `evaluate_compliance()` (NOT `run_compliance_checks`)
- All 3 new agents (Sizer, FinOps, RFP Writer) use LLM summary with heuristic fallback — if LLM call fails, they produce a rule-based summary instead
- `PricingService.close()` must be called on shutdown (handled by lifespan)
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
- Profiler category resolution uses priority ordering (AI_ML first, COMPUTE last) to prevent generic `vcpus` check from shadowing specific signals like `database_engine` or `gpu_count`
- Profiler degrades gracefully when LLM is unavailable — falls back to `_heuristic_rationale()` and heuristic summary notes
- `src/main.py` is broken (wrong import) — fix after API routes are ready
