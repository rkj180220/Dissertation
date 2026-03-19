# Cloud Orchestrator IDSS — Continuation Prompt

> **Purpose**: Paste this into a NEW Copilot chat window to resume work from exactly where we left off.
> **Last Updated**: 28 Feb 2026

---

## PASTE BELOW INTO NEW CHAT

```
I'm continuing work on my M.Tech dissertation project: "Agentic AI-Driven Intelligent Decision Support System for Cloud-Agnostic Resource Orchestration and Automated Procurement".

## What's Already Built & Verified

The project is at `/Users/ramkumarjayakumar/Dev/Dissertation/`.

### ✅ COMPLETE — Config Layer
- `src/config/settings.py` — 6 Pydantic-settings classes. LLMProvider enum. @lru_cache singleton.
- `src/config/logging_config.py` — configure_observability() wires structlog + LangFuse SDK.
- `src/config/__init__.py` — Re-exports.
- `.env.example` — All env var prefixes.

### ✅ COMPLETE — LLM Factory
- `src/llm/factory.py` — get_llm() → BaseChatModel. Lazy imports per provider.
- `src/llm/__init__.py` — Re-exports get_llm.

### ✅ COMPLETE — Normalized Pricing Models
- `src/models/cloud_resource.py` — CloudProvider enum (aws/azure/gcp), ServiceCategory enum (14 values: COMPUTE, SERVERLESS_COMPUTE, CONTAINER, SERVERLESS_FUNCTION, DATABASE, STORAGE, NETWORKING, AI_ML, ANALYTICS, MANAGEMENT, SECURITY, INTEGRATION, IOT, OTHER). Legacy ComputeSKU/StorageSKU kept for backward compat with engines/.
- `src/models/pricing.py` — PricingTier enum (8 values: ON_DEMAND, SPOT, LOW_PRIORITY, RESERVED_1YR, RESERVED_3YR, SAVINGS_PLAN_1YR, SAVINGS_PLAN_3YR, DEV_TEST). NormalizedPriceItem model with monthly_cost_estimate property (handles reservation total-upfront vs hourly correctly). Legacy SKUPricing kept for backward compat.
- `src/models/__init__.py` — Exports both new and legacy models.

### ✅ COMPLETE — Azure Provider Adapter
- `src/providers/base_provider.py` — Abstract BaseCloudProvider: search_prices(), get_sku_prices(), list_regions().
- `src/providers/azure_provider.py` — AzurePricingProvider: calls Azure Retail Prices REST API (public, no auth). Features: OData filter builder, automatic pagination, serviceName→ServiceCategory mapping, ServiceCategory→serviceNames reverse mapping (for category-only searches), PricingTier resolution, client-side post-filtering for Spot/LowPriority, @observe() tracing + structlog.

### ✅ COMPLETE — AWS Provider Adapter
- `src/providers/aws_provider.py` — AWSPricingProvider: calls AWS Pricing API via boto3. Features: 45 ServiceCode→ServiceCategory mappings, 26 region↔location name mappings, handles OnDemand + Reserved (1yr/3yr) terms, parses stringified JSON PriceList, all boto3 sync calls wrapped in asyncio.to_thread(), @observe() tracing + structlog. NOT live-tested (AWS creds expired).

### ✅ COMPLETE — GCP Provider Adapter
- `src/providers/gcp_provider.py` — GCPPricingProvider: calls GCP Cloud Billing Catalog via google-cloud-billing SDK. Features: 38 service display name→ServiceCategory mappings, usageType→PricingTier mapping (OnDemand/Preemptible/Commit1Yr/Commit3Yr), units+nanos→float price conversion, two-step discovery (list_services → list_skus), all gRPC sync calls wrapped in asyncio.to_thread(), @observe() tracing + structlog. NOT live-tested (GCP creds not set up).

- `src/providers/__init__.py` — Exports BaseCloudProvider + all 3 adapters.

### ✅ COMPLETE — Pricing Cache + Service Layer
- `src/services/pricing_cache.py` — PricingCache: async SQLite cache (aiosqlite). Schema: `price_items` (UNIQUE on provider+sku_id+region+pricing_tier) + `fetch_log` (TTL tracking per query pattern). WAL mode, deterministic cache keys, upsert/query/evict_stale/stats. @observe() tracing + structlog.
- `src/services/pricing_service.py` — PricingService: cache-aware facade that agents use instead of calling providers directly. Features: transparent cache-on-write/read-on-hit, per-tier TTL overrides (spot=6h, on-demand=24h, reserved=7d), graceful degradation (stale cache on API failure), `compare_across_providers()` for FinOps agent, force_refresh, evict_stale, cache_stats. @observe() tracing + structlog.
- `src/services/__init__.py` — Exports PricingCache, PricingService.

### Live-Tested (51 checks passed)
- All model + provider imports clean
- NormalizedPriceItem construction + monthly estimates (on-demand, reserved 1yr/3yr, per-request)
- Azure VM on-demand search (no spot/low-priority leaking)
- Azure D4s_v5 all tiers (11 rows: on-demand, reserved, spot, dev-test, Linux+Windows)
- Azure Functions → correctly categorized as SERVERLESS_FUNCTION
- Azure container category search → fires 3 API calls (AKS + CI + ACA), returns 39 items
- All 3 adapters subclass BaseCloudProvider correctly
- CloudProvider enums correct on each adapter
- All abstract methods present on all 3 adapters
- GCP tier resolution, price conversion, unit normalisation all verified
- AWS/GCP service→category mappings and reverse mappings verified
- **PricingService cache lifecycle**: miss→live→store→hit→stats→evict→clear (25 checks)
- **Cache speedup**: ~970x (0.68s live → 0.0007s cached)
- **Cross-provider compare**: Azure returns data, unregistered providers return empty
- **Graceful degradation**: stale cache fallback on API failure
- **State schema + models redesign**: 41/41 checks passed (all imports, instantiation, behavior)

### ✅ COMPLETE — State Schema + Models Redesign
- `src/models/workload.py` — REDESIGNED: ScalingPattern enum, ResourceSpec (generic compute/storage/db/k8s/serverless fields), WorkloadRequirement (category-agnostic per-component, maps to any of 14 ServiceCategories), ComponentProfile (Profiler output per workload), WorkloadProfile (with components list), WorkloadRequest (uses CloudProvider enum, provider_regions map, raw_user_input). Legacy VMWorkload/ContainerWorkload/StorageRequirement kept at bottom.
- `src/models/conversation.py` — NEW: MessageRole/ClarificationStatus/ClarificationPriority enums, ChatMessage (role, content, timestamp, agent_name, metadata), ClarificationQuestion (question_id, target_field, priority, status, default_value, resolved_value), ConversationState (messages, questions, current_turn, max_clarification_turns, requirements_complete, should_continue_clarifying property).
- `src/models/recommendation.py` — REDESIGNED: Uses NormalizedPriceItem instead of ComputeSKU/StorageSKU. ProviderCostBreakdown (6 cost categories: compute/database/storage/k8s/networking/serverless + RI/SP/spot savings). CostComparison (budget tracking + exceeded flag). CloudRecommendation (sku_selections with NormalizedPriceItem, rfp_document inline).
- `src/orchestrator/state.py` — NEW: OrchestratorState TypedDict with Annotated[list, operator.add] append-only reducers. AgentStatus/AgentExecution (lifecycle + timing tracking). SizedWorkloadResult (fit_score, rationale, selected_sku + alternatives). create_initial_state() factory. Fields for all 5 agents: messages, conversation, workload_request, workload_profile, sized_results, cost_comparison, rfp_document, compliance_report, kpis.
- `src/orchestrator/__init__.py` — UPDATED: exports OrchestratorState, create_initial_state, AgentStatus, AgentExecution, SizedWorkloadResult.
- `src/models/__init__.py` — UPDATED: exports all new + legacy models (~30 symbols in __all__).

### ⚠️ OLD SCAFFOLD — Needs Redesign
- `src/engines/` — bin_packing.py, scoring.py, waf_compliance.py (use old ComputeSKU)
- `src/main.py` — FastAPI shell

### 📝 PLACEHOLDERS — Not Yet Implemented
- `src/agents/` — clarifier.py, profiler.py, sizer.py, finops.py, rfp_writer.py
- `src/orchestrator/graph.py` — LangGraph workflow (state.py is done)
- `src/api/` — dependencies.py, routes/health.py, routes/orchestration.py
- `tests/` — conftest.py, unit/, integration/
- `dashboard/` — React not scaffolded

## Key Decisions Already Made
1. Python ≥3.13, uv, hatchling build backend
2. LangGraph (langchain-core ONLY, NOT full langchain)
3. AWS Bedrock Claude primary, Gemini optional backup
4. LLM Factory with lazy imports — agents use BaseChatModel only
5. LangFuse SDK v3 + structlog (@observe() on every agent node, `from langfuse import observe`)
6. 5 Agents: Clarifier (conditional loop), Profiler, Sizer, FinOps, RFP Writer
7. React dashboard (Vite + TS + Tailwind + shadcn/ui), chat-first, SSE streaming
8. Normalize at adapter level — every provider returns NormalizedPriceItem only
9. SQLite pricing cache via aiosqlite — PricingService facade, agents NEVER call providers directly
10. TTL-based cache: 24h default, 6h spot/low-priority, 168h reserved/savings-plan
11. LangGraph TypedDict state with Annotated[list, operator.add] for append-only reducers
12. Category-agnostic WorkloadRequirement + ResourceSpec (supports all 14 ServiceCategories)
13. ConversationState with ClarificationQuestion tracking + should_continue_clarifying gate
14. AgentExecution tracking per agent (status, timing, retry_count) for observability
15. Recommendation output uses NormalizedPriceItem everywhere (ComputeSKU/StorageSKU deprecated)

## What to Build Next

The state schema and all data models are now complete. The natural next step is:
- **Agents** — Build the 5 agents one at a time: Clarifier → Profiler → Sizer → FinOps → RFP Writer
  - Each agent reads from / writes to OrchestratorState (TypedDict)
  - Each must use @observe() + structlog + BaseChatModel (from factory)
  - Clarifier uses ConversationState for multi-turn loop gating
  - Sizer calls PricingService (never adapters directly) and produces SizedWorkloadResult
- **Orchestrator graph** — LangGraph StateGraph wiring the 5 agents, conditional edges for Clarifier loop
- **API routes** — FastAPI SSE endpoint that invokes the orchestrator
- **Live-test AWS/GCP** — Set up credentials and run live smoke tests

Please read `.github/copilot-instructions.md` and `docs/CURRENT_STATE_REVIEW.md` first, then ask me which direction to take.
```

---

## Notes for Future Me

- The `.venv` is already created with Python 3.13.0 and all 148 deps synced
- Run `uv sync --all-extras` if deps change
- All ✅ files were verified with `uv run python` — imports and defaults work
- `get_settings.cache_clear()` resets the singleton in tests
- LangFuse gracefully degrades when keys are missing (warns, doesn't crash)
- LangFuse SDK v3 import: `from langfuse import observe` (NOT `langfuse.decorators`)
- Azure reserved prices: `retailPrice` = total upfront, NOT per-hour (despite `unitOfMeasure='1 Hour'`)
- `monthly_cost_estimate` checks reservation tier BEFORE hourly to handle this correctly
- AWS adapter: boto3 sync calls wrapped in `asyncio.to_thread()`, region filtering uses human-readable location names
- GCP adapter: gRPC sync calls wrapped in `asyncio.to_thread()`, price = `units + nanos/1e9`
- AWS/GCP adapters NOT live-tested — need to set up credentials to verify against real APIs
- PricingService is the single entry point for agents — never call adapters directly
- Cache DB at `data/sku_cache.db` (auto-created). Per-tier TTL: spot=6h, on-demand=24h, reserved=7d
- `compare_across_providers()` is designed for the FinOps agent's cross-provider workflow
- Graceful degradation: live API failure + stale cache available → serve stale with warning log
