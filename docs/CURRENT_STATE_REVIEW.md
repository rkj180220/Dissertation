# Cloud Orchestrator IDSS — Current State Review

> **Last Updated**: 28 Feb 2026 · **Status**: Config ✅ · LLM Factory ✅ · Pricing Models ✅ · All 3 Provider Adapters ✅ · Pricing Cache ✅ · State Schema + Models ✅ · Agents next

---

## 1. Project Identity

- **Title**: Agentic AI-Driven Intelligent Decision Support System for Cloud-Agnostic Resource Orchestration and Automated Procurement
- **Author**: Ramkumar J (BITS ID: 2024MT03027, M.Tech Cloud Computing, BITS Pilani WILP)
- **Supervisor**: Rajkumar Sakthibalan (Presidio Solutions, Chennai)
- **Additional Examiner**: Santhosh Kirubakaran

---

## 2. Decided Tech Stack

| Layer | Choice | Status |
|---|---|---|
| Language | Python ≥3.13 | ✅ Verified |
| Package Manager | uv 0.7.19 | ✅ venv created, 148 deps synced |
| Build Backend | hatchling | ✅ |
| Backend API | FastAPI + Uvicorn | ✅ In pyproject.toml |
| Agentic Core | LangGraph (depends on langchain-core ONLY, NOT full langchain) | ✅ |
| Primary LLM | AWS Bedrock Claude via langchain-aws[anthropic] | ✅ |
| Backup LLM | Google Gemini via langchain-google-genai (optional dep) | ✅ |
| Observability | LangFuse SDK v3 + structlog | ✅ Config wired |
| Streaming | SSE via sse-starlette | ✅ In pyproject.toml |
| Dashboard | React (Vite + TypeScript + Tailwind + shadcn/ui) — chat-first | Decided, not scaffolded |
| Database | SQLite for SKU catalog caching | ✅ Built & live-tested (aiosqlite) |
| Testing | pytest + pytest-asyncio | ✅ In pyproject.toml |

---

## 3. Architecture — 5 Agents

```
User (React chat) → FastAPI (SSE) → LangGraph Orchestrator
                                         │
                  ┌──────────────────────┐│
                  │    Clarifier Agent    ││ ← Multi-turn requirement refinement
                  │  (conditional loop)   ││   Asks follow-up questions until
                  └──────────┬───────────┘│   requirements are unambiguous
                             │ complete    │
                             ▼            │
                  ┌──────────────────────┐│
                  │    Profiler Agent     ││ ← Analyzes workload → WorkloadProfile
                  └──────────┬───────────┘│
                             ▼            │
                  ┌──────────────────────┐│
                  │     Sizer Agent      ││ ← Calls providers → scoring → bin-packing
                  └──────────┬───────────┘│
                             ▼            │
                  ┌──────────────────────┐│
                  │    FinOps Agent      ││ ← Multi-provider cost comparison
                  └──────────┬───────────┘│
                             ▼            │
                  ┌──────────────────────┐│
                  │   RFP Writer Agent   ││ ← Generates procurement document
                  └──────────────────────┘│
```

---

## 4. File Tree — What Exists vs. What's Placeholder

```
Dissertation/
├── .env.example                    ✅ UPDATED — all prefixes (APP_, LLM_, AWS_, AZURE_, GCP_, LANGFUSE_)
├── .github/copilot-instructions.md ✅ COMPLETE — tech stack, architecture, observability rules, LLM patterns
├── pyproject.toml                  ✅ COMPLETE — all deps, scripts, tool config
├── README.md                       ✅ Basic
├── uv.lock                         ✅ Generated
├── .venv/                          ✅ Python 3.13.0
│
├── src/
│   ├── config/
│   │   ├── __init__.py             ✅ BUILT — re-exports get_settings, configure_observability
│   │   ├── settings.py             ✅ BUILT — 6 Pydantic-settings classes, @lru_cache singleton
│   │   └── logging_config.py       ✅ BUILT — configure_observability() wires structlog + LangFuse
│   │
│   ├── llm/
│   │   ├── __init__.py             ✅ BUILT — re-exports get_llm
│   │   └── factory.py              ✅ BUILT — get_llm() → BaseChatModel (lazy imports per provider)
│   │
│   ├── models/
│   │   ├── __init__.py             ✅ UPDATED — exports all new + legacy models (90+ symbols)
│   │   ├── cloud_resource.py       ✅ UPDATED — CloudProvider, ServiceCategory (14 values),
│   │   │                                        + legacy ComputeSKU/StorageSKU (deprecated)
│   │   ├── pricing.py              ✅ REWRITTEN — PricingTier (8 values), NormalizedPriceItem
│   │   │                                          with monthly_cost_estimate, is_hourly, is_monthly
│   │   │                                          + legacy SKUPricing (deprecated)
│   │   ├── workload.py             ✅ REDESIGNED — Multi-service support:
│   │   │                                           - ScalingPattern enum (steady/bursty/growing/unpredictable/batch)
│   │   │                                           - ResourceSpec (generic: compute/storage/db/k8s/serverless)
│   │   │                                           - WorkloadRequirement (category-agnostic per-component)
│   │   │                                           - ComponentProfile (Profiler output per workload)
│   │   │                                           - WorkloadProfile (aggregated with components list)
│   │   │                                           - WorkloadRequest (uses CloudProvider enum, provider_regions)
│   │   │                                           - Legacy VMWorkload/ContainerWorkload/StorageRequirement kept
│   │   ├── recommendation.py       ✅ REDESIGNED — Uses NormalizedPriceItem (not legacy SKUs):
│   │   │                                           - ProviderCostBreakdown (6 cost categories + RI/SP/spot savings)
│   │   │                                           - CostComparison (budget tracking + exceeded flag)
│   │   │                                           - CloudRecommendation (sku_selections, rfp_document inline)
│   │   │                                           - PackedNode uses NormalizedPriceItem for node_sku
│   │   └── conversation.py         ✅ NEW — Multi-turn clarification dialogue:
│   │                                         - MessageRole, ClarificationStatus, ClarificationPriority enums
│   │                                         - ChatMessage (role, content, timestamp, agent_name, metadata)
│   │                                         - ClarificationQuestion (question_id, target_field, priority, status)
│   │                                         - ConversationState (messages, questions, turn tracking)
│   │                                         - Properties: pending_questions, should_continue_clarifying
│   │
│   ├── providers/
│   │   ├── __init__.py             ✅ UPDATED — exports BaseCloudProvider + all 3 adapters
│   │   ├── base_provider.py        ✅ REWRITTEN — abstract interface: search_prices, get_sku_prices,
│   │   │                                          list_regions (provider-agnostic contract)
│   │   ├── azure_provider.py       ✅ NEW — Live adapter for Azure Retail Prices API:
│   │   │                                     - OData filter builder, automatic pagination
│   │   │                                     - serviceName → ServiceCategory mapping
│   │   │                                     - ServiceCategory → Azure serviceNames reverse mapping
│   │   │                                     - PricingTier resolution (on_demand/spot/reserved/devtest)
│   │   │                                     - @observe() tracing + structlog throughout
│   │   ├── aws_provider.py         ✅ NEW — AWS Pricing API adapter (boto3):
│   │   │                                     - 45 ServiceCode → ServiceCategory mappings
│   │   │                                     - 26 region↔location name mappings
│   │   │                                     - Handles OnDemand + Reserved (1yr/3yr) terms
│   │   │                                     - Parses stringified JSON PriceList items
│   │   │                                     - boto3 sync calls wrapped in asyncio.to_thread()
│   │   │                                     - @observe() tracing + structlog throughout
│   │   │                                     - NOT live-tested (AWS creds expired)
│   │   └── gcp_provider.py         ✅ NEW — GCP Cloud Billing Catalog adapter:
│   │                                         - 38 service display name → ServiceCategory mappings
│   │                                         - usageType → PricingTier (OnDemand/Preemptible/Commit)
│   │                                         - units+nanos → float price conversion
│   │                                         - Two-step discovery (list_services → list_skus)
│   │                                         - gRPC sync calls wrapped in asyncio.to_thread()
│   │                                         - @observe() tracing + structlog throughout
│   │                                         - NOT live-tested (GCP creds not set up)
│   │
│   ├── services/
│   │   ├── __init__.py             ✅ NEW — exports PricingCache, PricingService
│   │   ├── pricing_cache.py       ✅ NEW — Async SQLite cache layer:
│   │   │                                    - price_items table (UNIQUE on provider+sku+region+tier)
│   │   │                                    - fetch_log table (TTL tracking per query pattern)
│   │   │                                    - WAL mode + NORMAL sync for performance
│   │   │                                    - upsert, query, evict_stale, stats
│   │   │                                    - @observe() tracing + structlog throughout
│   │   └── pricing_service.py     ✅ NEW — Cache-aware pricing facade:
│   │                                         - Transparent SQLite caching for all providers
│   │                                         - Per-tier TTL (spot 6h, on-demand 24h, reserved 7d)
│   │                                         - Graceful degradation (stale cache on API failure)
│   │                                         - compare_across_providers() for FinOps agent
│   │                                         - refresh(), evict_stale(), cache_stats()
│   │                                         - Live-tested: 25 checks passed, 970x cache speedup
│   │
│   ├── agents/
│   │   ├── clarifier.py            📝 PLACEHOLDER
│   │   ├── profiler.py             📝 PLACEHOLDER
│   │   ├── sizer.py                📝 PLACEHOLDER
│   │   ├── finops.py               📝 PLACEHOLDER
│   │   └── rfp_writer.py           📝 PLACEHOLDER
│   │
│   ├── orchestrator/
│   │   ├── __init__.py             ✅ UPDATED — exports OrchestratorState, create_initial_state,
│   │   │                                        AgentStatus, AgentExecution, SizedWorkloadResult
│   │   ├── graph.py                📝 PLACEHOLDER
│   │   └── state.py                ✅ NEW — LangGraph shared state schema:
│   │                                         - OrchestratorState TypedDict (Annotated list reducers)
│   │                                         - AgentStatus/AgentExecution (per-agent timing + lifecycle)
│   │                                         - SizedWorkloadResult (Sizer output per workload per provider)
│   │                                         - create_initial_state() factory helper
│   │                                         - All 5 agent slots: messages, conversation, workload_request,
│   │                                           workload_profile, sized_results, cost_comparison,
│   │                                           rfp_document, compliance_report, kpis
│   │
│   ├── engines/
│   │   ├── bin_packing.py          ⚠️  OLD SCAFFOLD — FFD/BFD, uses old ComputeSKU
│   │   ├── scoring.py              ⚠️  OLD SCAFFOLD — weighted scorer, uses old ComputeSKU
│   │   └── waf_compliance.py       ⚠️  OLD SCAFFOLD — rule-based WAF checker
│   │
│   ├── api/
│   │   ├── dependencies.py         📝 PLACEHOLDER
│   │   └── routes/
│   │       ├── health.py           📝 PLACEHOLDER
│   │       └── orchestration.py    📝 PLACEHOLDER
│   │
│   └── main.py                     ⚠️  OLD SCAFFOLD — FastAPI shell
│
├── scripts/
│   ├── explore_aws_pricing.py    📝 Research script — explores AWS Pricing API shapes (needs creds)
│   ├── explore_azure_pricing.py    📝 Research script — fetches live Azure prices for 8 services
│   ├── explore_azure_tiers.py      📝 Research script — fetches reserved/spot/savings plan shapes
│   ├── explore_gcp_pricing.py      📝 Research script — explores GCP Billing Catalog shapes (needs creds)
│   ├── test_all_providers.py       📝 Smoke test — validates all 3 adapters import + structural checks
│   ├── test_azure_adapter.py       📝 Live adapter smoke test (imports + API calls + assertions)
│   ├── test_monthly_estimate.py    📝 Verifies monthly cost calculations
│   └── test_pricing_service.py     📝 Full cache lifecycle test (25 checks, live Azure API)
│
├── tests/
│   ├── conftest.py                 📝 PLACEHOLDER
│   ├── unit/                       📝 EMPTY
│   └── integration/                📝 EMPTY
│
├── dashboard/                      📝 EMPTY (React not scaffolded)
└── docs/
    ├── CURRENT_STATE_REVIEW.md     📄 THIS FILE
    └── CONTINUATION_PROMPT.md      📄 Copy-paste prompt for new chat windows
```

Legend: ✅ BUILT & VERIFIED | ⚠️ OLD SCAFFOLD (needs review/rewrite) | 📝 PLACEHOLDER | 📄 Doc

---

## 5. What Was Verified at Runtime

All ✅ BUILT files were tested with `uv run python`:

### Config + LLM Layer
1. **Settings**: All defaults load without `.env` file ✅
2. **Singleton**: `get_settings() is get_settings()` ✅
3. **Enums**: `LLMProvider` (bedrock/gemini), `Environment` (dev/staging/prod) ✅
4. **structlog**: Coloured, timestamped, context-bound logs output correctly ✅
5. **LangFuse**: Gracefully warns when keys missing (no crash) ✅
6. **LLM Factory**: Imports resolve, provider lazy-loading works ✅

### Models + Providers Layer
7. **All model imports**: Both new (ServiceCategory, NormalizedPriceItem, PricingTier) and legacy (ComputeSKU, StorageSKU, SKUPricing) import cleanly ✅
8. **NormalizedPriceItem construction**: Hand-built item with all fields ✅
9. **monthly_cost_estimate**: On-demand $0.192/hr → $140.16/mo ✅
10. **monthly_cost_estimate**: Reserved 1yr $992 total → $82.67/mo ✅
11. **monthly_cost_estimate**: Reserved 3yr $1867 total → $51.86/mo ✅
12. **monthly_cost_estimate**: Per-request pricing → None (correct) ✅
13. **Azure search_prices (VM, on-demand)**: Returns only ON_DEMAND items, no Spot/LowPriority leaking ✅
14. **Azure get_sku_prices (D4s_v5)**: Returns all 11 tiers (on-demand, reserved, spot, dev-test, Linux+Windows) ✅
15. **Azure search by ServiceCategory (CONTAINER)**: Fires 3 API calls (AKS + Container Instances + Container Apps), returns 39 items ✅
16. **Azure Functions**: Correctly categorized as SERVERLESS_FUNCTION ✅

### All 3 Provider Adapters — Import & Structural Checks
17. **All 4 exports import cleanly**: BaseCloudProvider, AWSPricingProvider, AzurePricingProvider, GCPPricingProvider ✅
18. **Class hierarchy**: All 3 adapters → BaseCloudProvider ✅
19. **CloudProvider enum**: AWS.provider=AWS, Azure.provider=AZURE, GCP.provider=GCP ✅
20. **Abstract methods**: All 3 have search_prices, get_sku_prices, list_regions ✅
21. **GCP mappings**: 38 service→category, 13 category→service reverse ✅
22. **GCP pricing tier resolution**: OnDemand/Preemptible/Commit1Yr/Commit3Yr all correct ✅
23. **GCP units+nanos→float**: 1+500M=1.5, 0+250M=0.25 ✅
24. **GCP usage unit normalisation**: h→"1 Hour", GiBy→"1 GB", GiBy.mo→"1 GB/Month" ✅
25. **AWS mappings**: 45 service→category, 13 category→service reverse, 26 region→location ✅
26. **Category coverage**: AWS 13/14, GCP 13/14, Azure 5/14 (Azure has fewer native service mappings) ✅

### PricingService + SQLite Cache (25 checks)
27. **Service initialization**: DB created, tables + indexes auto-created ✅
28. **Cache MISS**: Live API fetch (Azure VMs, eastus), items stored in SQLite ✅
29. **Cache HIT**: Same query served from SQLite, same item count ✅
30. **Cache speedup**: ~970x faster (0.68s → 0.0007s) ✅
31. **SKU lookup**: D4s_v5 returns 11 tiers (on-demand, reserved, spot, dev-test) ✅
32. **Cache stats**: total_items, items_by_provider, fetch_entries, db_size ✅
33. **Force refresh**: Bypasses cache, re-fetches from live API ✅
34. **Cache key determinism**: Same params → same key, different params → different key ✅
35. **Cross-provider compare**: Azure returns data, unregistered AWS/GCP return empty lists ✅
36. **Evict stale**: TTL=0h evicts everything correctly ✅
37. **Clear cache**: Provider-scoped deletion works ✅

### State Schema + Models Redesign (41 checks)
38. **All model imports**: models.__init__ loads cleanly with all new + legacy exports ✅
39. **All 8 enums**: CloudProvider, ServiceCategory, PricingTier, EnvironmentType, WorkloadTier, ScalingPattern, MessageRole, ClarificationStatus ✅
40. **New workload models**: WorkloadRequirement, ResourceSpec, ComponentProfile importable ✅
41. **Conversation models**: ChatMessage, ClarificationQuestion, ConversationState importable ✅
42. **Recommendation models**: PackedNode, BinPackingResult, CostComparison, ComplianceReport, CloudRecommendation importable ✅
43. **Orchestrator package**: OrchestratorState, AgentStatus, AgentExecution, SizedWorkloadResult, create_initial_state importable ✅
44. **Legacy backward compat**: ComputeSKU, StorageSKU, VMWorkload, ContainerWorkload, StorageRequirement still importable ✅
45. **WorkloadRequirement instantiation**: Database workload with ResourceSpec(database_engine='postgresql', high_availability=True) ✅
46. **ConversationState behavior**: should_continue_clarifying, pending_questions, turn limits all work ✅
47. **create_initial_state**: Produces valid state with messages, agent_executions, kpis ✅
48. **Recommendation uses NormalizedPriceItem**: sku_selections field, old vm_selections/storage_selections removed ✅
49. **WorkloadProfile with ComponentProfile**: Per-workload resolved_category + recommended_instance_families ✅
50. **SizedWorkloadResult**: fit_score, rationale, selected_sku + alternative_skus ✅
51. **AgentExecution lifecycle**: PENDING → RUNNING → COMPLETED, elapsed_ms auto-calculated ✅
52. **CostComparison new fields**: budget_monthly_usd, budget_exceeded, database/networking/serverless breakdowns ✅

---

## 6. Key Design — Normalized Pricing Model

### The Core Model: `NormalizedPriceItem`

Every cloud provider adapter normalizes raw API responses into `NormalizedPriceItem`. This is the **only** pricing model that agents and engines see.

```python
class NormalizedPriceItem(BaseModel):
    provider: CloudProvider          # aws | azure | gcp
    service_name: str                # "Virtual Machines", "AWSLambda", etc.
    service_category: ServiceCategory # 14 normalized categories
    sku_id: str                      # provider-native SKU id
    sku_name: str                    # "Standard_D4s_v5"
    product_name: str                # "Virtual Machines Dsv5 Series"
    meter_name: str                  # billing meter name
    region: str                      # "eastus", "us-east-1", etc.
    retail_price: float              # list price
    unit_price: float                # effective price
    currency: str                    # "USD"
    unit_of_measure: str             # "1 Hour", "1 GB Second", "10K", etc.
    pricing_tier: PricingTier        # 8 tiers: on_demand, spot, reserved_1yr, etc.
    reservation_term: str | None     # "1 Year" / "3 Years"
    effective_date: datetime
    effective_end_date: datetime | None
    is_primary_meter: bool
    attributes: dict[str, Any]       # provider-specific extras
```

### ServiceCategory (14 values)
COMPUTE, SERVERLESS_COMPUTE, CONTAINER, SERVERLESS_FUNCTION, DATABASE, STORAGE, NETWORKING, AI_ML, ANALYTICS, MANAGEMENT, SECURITY, INTEGRATION, IOT, OTHER

### PricingTier (8 values)
ON_DEMAND, SPOT, LOW_PRIORITY, RESERVED_1YR, RESERVED_3YR, SAVINGS_PLAN_1YR, SAVINGS_PLAN_3YR, DEV_TEST

### Provider Interface: `BaseCloudProvider`
```python
class BaseCloudProvider(ABC):
    async def search_prices(*, service_name, service_category, region, sku_name, pricing_tier, max_results) -> list[NormalizedPriceItem]
    async def get_sku_prices(sku_name, region) -> list[NormalizedPriceItem]
    async def list_regions() -> list[str]
```

---

## 7. Azure Pricing API — Key Properties

- **URL**: `https://prices.azure.com/api/retail/prices`
- **Auth**: None required (public API)
- **Filter**: OData `$filter` (e.g. `serviceName eq 'Virtual Machines' and armRegionName eq 'eastus'`)
- **Pagination**: 1000 items/page, `NextPageLink` for next page
- **Response**: Always 20 fields per item, consistent across ALL service types
- **Commitment tiers**: Separate rows — `type` field = `Consumption`, `Reservation`, `DevTestConsumption`
- **Spot/Low Priority**: `type = Consumption` but `skuName` contains "Spot" or "Low Priority"
- **Reserved prices**: `retailPrice` = total upfront cost (not hourly), needs division by term months

---

## 7b. AWS Pricing API — Key Properties

- **SDK**: `boto3.client('pricing', region_name='us-east-1')`
- **Auth**: AWS credentials (access key + secret + optional session token)
- **Endpoint regions**: Only `us-east-1` and `ap-south-1`
- **API call**: `get_products(ServiceCode=..., Filters=[{Type, Field, Value}], MaxResults=100)`
- **Response**: `PriceList` — list of **stringified JSON** (must `json.loads()` each)
- **Product structure**: `product.attributes` (service-specific), `terms.OnDemand`, `terms.Reserved`
- **Price location**: `terms.OnDemand.{sku}.priceDimensions.{dim}.pricePerUnit.USD` (string)
- **Reserved terms**: `termAttributes.LeaseContractLength` = "1yr" or "3yr"
- **Region filtering**: Uses human-readable location names (e.g. "US East (N. Virginia)"), NOT region codes
- **Pagination**: `NextToken` field for subsequent pages
- **Status**: Adapter built, NOT live-tested (AWS creds expired)

---

## 7c. GCP Cloud Billing Catalog — Key Properties

- **SDK**: `google.cloud.billing_v1.CloudCatalogClient()`
- **Auth**: OAuth2 (service-account JSON or `gcloud auth application-default login`)
- **Discovery**: Two-step — `list_services()` → `list_skus(parent=service.name)`
- **SKU category**: `category.resourceFamily` (Compute/Storage/Network), `category.usageType` (OnDemand/Preemptible/Commit1Yr/Commit3Yr)
- **Pricing**: `pricingInfo[].pricingExpression.tieredRates[].unitPrice.{units, nanos}`
- **Price calculation**: `float(units) + nanos / 1e9`
- **Units**: Short codes — `h` (hour), `GiBy` (GB), `GiBy.mo` (GB/month), `mo` (month)
- **Regions**: In `service_regions` list per SKU
- **Pagination**: SDK handles internally via page tokens
- **Status**: Adapter built, NOT live-tested (GCP creds not set up)

---

## 7d. Pricing Cache Architecture

```
Agents / Engines
       │
       ▼
  PricingService  ← cache-transparent facade
       │
  ┌────┴────┐
  │ SQLite  │  ← NormalizedPriceItem rows + TTL metadata
  │  Cache  │
  └────┬────┘
       │ cache miss / expired
       ▼
  Provider Adapters (AWS / Azure / GCP)
```

- **Database**: SQLite via `aiosqlite` at `data/sku_cache.db` (configurable)
- **Schema**: `price_items` (UNIQUE on provider+sku_id+region+pricing_tier) + `fetch_log` (TTL tracking)
- **Default TTL**: 24 hours (from `APP_SKU_CACHE_TTL_HOURS`)
- **Per-tier TTL overrides**: Spot/Low Priority = 6h, On-demand/DevTest = 24h, Reserved/Savings Plan = 168h (7 days)
- **Graceful degradation**: If live API fails AND stale cache exists → serve stale data with warning
- **Performance**: WAL mode + NORMAL sync, ~970x cache speedup on hits
- **Maintenance**: `evict_stale()`, `clear_cache()`, `cache_stats()`

### PricingService Interface
```python
class PricingService:
    def register_provider(provider: BaseCloudProvider) -> None
    async def initialize() -> None
    async def search_prices(provider, *, service_name, service_category, region, ..., force_refresh) -> list[NormalizedPriceItem]
    async def get_sku_prices(provider, sku_name, region, *, force_refresh) -> list[NormalizedPriceItem]
    async def compare_across_providers(*, service_category, regions, ...) -> dict[CloudProvider, list[NormalizedPriceItem]]
    async def refresh(provider, ...) -> int
    async def evict_stale(ttl_hours) -> int
    async def clear_cache(provider) -> int
    async def cache_stats() -> dict
    async def close() -> None
```

---

## 8. Build Order — Remaining Work

```
COMPLETED                          NEXT
─────────                          ────
✅ config/settings.py              → agents/ (5 agents, starting with Clarifier)
✅ config/logging_config.py        → orchestrator/graph.py (LangGraph workflow)
✅ config/__init__.py              → api/ (FastAPI routes + SSE)
✅ llm/factory.py                  → engines/ (update to use new NormalizedPriceItem)
✅ models/pricing.py (new)         → dashboard/ (React scaffold)
✅ models/cloud_resource.py (new)  → tests/ (unit + integration)
✅ models/workload.py (redesigned) → Live-test AWS adapter (when creds available)
✅ models/conversation.py (new)    → Live-test GCP adapter (when creds available)
✅ models/recommendation.py (new)
✅ providers/base_provider.py
✅ providers/azure_provider.py
✅ providers/aws_provider.py
✅ providers/gcp_provider.py
✅ services/pricing_cache.py
✅ services/pricing_service.py
✅ orchestrator/state.py (new)
```

---

## 9. Decisions Already Locked In

| # | Decision | Outcome |
|---|---|---|
| 1 | Python version | ≥3.13 (verified zero clash across all deps) |
| 2 | LLM provider | Bedrock Claude primary, Gemini optional backup |
| 3 | LLM abstraction | Factory pattern with lazy imports, agents use BaseChatModel only |
| 4 | Project structure | src/ contains all code, config/ inside src/ |
| 5 | Package manager | uv |
| 6 | Dashboard | React (Vite + TS + Tailwind + shadcn/ui), chat-first |
| 7 | Streaming | SSE via sse-starlette |
| 8 | Observability | LangFuse + structlog, @observe() on every agent node |
| 9 | Agent count | 5: Clarifier, Profiler, Sizer, FinOps, RFP Writer |
| 10 | Clarifier pattern | LangGraph conditional loop until requirements are clear |
| 11 | Provider normalization | Normalize at adapter level → NormalizedPriceItem |
| 12 | Model-agnostic LangFuse | `from langfuse import observe` (SDK v3 import path) |
| 13 | Pricing cache | SQLite via aiosqlite, TTL-based (24h default, 6h spot, 7d reserved) |
| 14 | Service facade | PricingService wraps providers — agents NEVER call adapters directly |
| 15 | State schema | LangGraph TypedDict with `Annotated[list, operator.add]` append-only reducers |
| 16 | Workload model | Category-agnostic `WorkloadRequirement` + `ResourceSpec` (covers all 14 ServiceCategories) |
| 17 | Conversation tracking | `ConversationState` with `ClarificationQuestion` tracking + `should_continue_clarifying` gate |
| 18 | Agent execution tracking | `AgentExecution` per agent with status, timing, retry_count — for observability |
| 19 | Recommendation output | Uses `NormalizedPriceItem` everywhere (ComputeSKU/StorageSKU deprecated) |

---

## 10. State Schema — OrchestratorState

The shared state that flows through every LangGraph node. Uses `TypedDict` with `Annotated` fields for reducer semantics.

### Fields Written Per Agent

| Agent | Reads | Writes |
|---|---|---|
| **Clarifier** | messages, conversation | messages, conversation, workload_request |
| **Profiler** | workload_request | workload_profile, messages |
| **Sizer** | workload_profile | sized_results, messages |
| **FinOps** | sized_results | cost_comparison, recommended_provider, savings_opportunities, messages |
| **RFP Writer** | all above | rfp_document, executive_summary, compliance_report, messages |

### Append-Only Lists (operator.add reducer)
- `messages: list[ChatMessage]` — full conversation log
- `sized_results: list[SizedWorkloadResult]` — per-workload, per-provider SKU selections
- `savings_opportunities: list[dict]` — RI/SP/spot savings

### Last-Writer-Wins Fields
- `conversation: ConversationState` — Clarifier's multi-turn state
- `workload_request: WorkloadRequest` — parsed requirements
- `workload_profile: WorkloadProfile` — Profiler output
- `cost_comparison: dict` — FinOps output
- `compliance_report: dict` — WAF checks

### Factory: `create_initial_state(request_id, project_name, raw_user_input)`
Produces a fully-initialized state dict with sensible defaults for all fields.
