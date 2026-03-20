# Cloud Orchestrator IDSS — Continuation Prompt

> **Purpose**: Paste this into a NEW Copilot chat window to resume work from exactly where we left off.
> **Last Updated**: 20 March 2026

---

## PASTE BELOW INTO NEW CHAT

```
I'm continuing work on my M.Tech dissertation project: "Agentic AI-Driven Intelligent Decision Support System for Cloud-Agnostic Resource Orchestration and Automated Procurement".

Project path: `/Users/ramkumarjayakumar/Dev/Dissertation/`
Student: Ramkumar J · BITS ID: 2024MT03027 · M.Tech Cloud Computing, BITS Pilani WILP

Please read `.github/copilot-instructions.md` first (tech stack rules, observability pattern, LLM abstraction rules). Then read `docs/PROJECT_SPEC.md` for complete per-file status, architecture diagram, and build order.

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

---

## What Needs to Be Built Next ❌

### Next Immediate Task — Profiler Agent (`src/agents/profiler.py`)
Currently a blank placeholder (3-line comment). Build it:
- **Input**: `state["workload_request"]` (`WorkloadRequest`)
- **Output**: writes `WorkloadProfile` (with `list[ComponentProfile]`) to `state["workload_profile"]`
- For each `WorkloadRequirement`:
  - Confirm/resolve `suggested_category` → `ComponentProfile.resolved_category`
  - Estimate compute/storage/IOPS based on `ResourceSpec`
  - Recommend instance families per cloud provider (via LLM + heuristics)
  - Flag `requires_gpu`, note HA requirements
- Entry point: `run_profiler_node(state, llm)` — same pattern as clarifier
- Must use `@observe()` + structlog + `BaseChatModel` (never import ChatBedrockConverse)

### Then (in order)
2. **Migrate engines** (`src/engines/bin_packing.py`, `scoring.py`) — replace `ComputeSKU`/`VMWorkload` with `NormalizedPriceItem`/`WorkloadRequirement`
3. **Sizer agent** (`src/agents/sizer.py`) — `WorkloadProfile` → calls `PricingService.search_prices()` per component → bins K8s workloads → produces `list[SizedWorkloadResult]`
4. **FinOps agent** (`src/agents/finops.py`) — `list[SizedWorkloadResult]` → `compare_across_providers()` → `CostComparison` with RI/SP/spot savings
5. **RFP Writer agent** (`src/agents/rfp_writer.py`) — all upstream outputs → Markdown procurement document → `rfp_document` + `executive_summary`
6. **LangGraph graph** (`src/orchestrator/graph.py`) — StateGraph wiring all 5 agents, conditional edge for Clarifier loop
7. **API layer** — `dependencies.py`, `routes/health.py`, `routes/orchestration.py` (SSE streaming)
8. **Fix `src/main.py`** — currently broken (wrong import path for configure_logging, missing router)
9. **Dashboard** (`dashboard/`) — React + Vite + TypeScript + Tailwind + shadcn/ui, chat-first

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
- AWS adapter: boto3 sync calls wrapped in `asyncio.to_thread()`, region filtering uses human-readable location names
- GCP adapter: gRPC sync calls wrapped in `asyncio.to_thread()`, price = `units + nanos/1e9`
- Bedrock model ID requires `us.` prefix (cross-region inference profile) — bare `anthropic.*` returns `ValidationException`
- PricingService is the single entry point for agents — never call adapters directly
- Cache DB at `data/sku_cache.db` (auto-created)
- `compare_across_providers()` designed for FinOps agent's cross-provider workflow
- Engines (`bin_packing.py`, `scoring.py`) use deprecated `ComputeSKU` — needs migration before Sizer agent can call them
- `src/main.py` is broken (wrong import) — fix after API routes are ready
