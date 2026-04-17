# Cloud Orchestrator IDSS - Copilot Instructions

## Project Overview
An Agentic AI-Driven Intelligent Decision Support System for Cloud-Agnostic Resource Orchestration and Automated Procurement.

## Tech Stack
- **Language**: Python 3.13+ (`requires-python = ">=3.13"`)
- **Backend API**: FastAPI with Uvicorn
- **Agentic Core**: LangGraph (`langchain-core` only — NOT full `langchain`)
- **LLM Provider (Primary)**: AWS Bedrock Claude via `langchain-aws[anthropic]`
- **LLM Provider (Backup)**: Google Gemini via `langchain-google-genai` (optional dep)
- **Observability**: LangFuse (SDK v3) for LLM tracing + `structlog` for structured logging
- **Algorithms**: Custom bin-packing (FFD/BFD) for Kubernetes node pool optimization
- **Cloud Providers**: AWS/Azure/GCP pricing API adapters with local caching
- **Dashboard**: React (Vite + TypeScript + Tailwind + shadcn/ui) — chat-first interface
- **API Streaming**: SSE (Server-Sent Events) via `sse-starlette` for real-time agent responses
- **Database**: SQLite for SKU catalog caching
- **Testing**: pytest

## Architecture
- `src/config/` — Pydantic-settings + structlog/langfuse wiring
- `src/llm/` — LLM factory (model-agnostic abstraction layer)
- `src/models/` — Pydantic data models (workload, pricing, cloud_resource, recommendation, conversation)
- `src/agents/` — Five autonomous agents: Clarifier, Profiler, Sizer, FinOps, RFP Writer
- `src/orchestrator/` — LangGraph workflow graph + shared state schema
- `src/engines/` — Algorithmic engines: bin-packing, scoring, WAF compliance
- `src/providers/` — Cloud provider adapters with normalized data output
- `src/services/` — PricingService (cache-transparent facade) + PricingCache (SQLite via aiosqlite)
- `src/api/` — FastAPI routes + dependency injection wiring
- `src/main.py` — App factory + uvicorn entry point
- `tests/` — Unit and integration tests (pytest)
- `dashboard/` — React frontend (Vite + TypeScript + Tailwind + shadcn/ui)

## Observability (CRITICAL — #1 Non-Functional Requirement)
Observability is the most crucial aspect of this project. Every step must be traceable.
Just by reading the logs, anyone should be able to understand exactly what happened.

### Rules
1. **Every public function** must log entry (with input params) and exit (with result summary).
2. **Every agent step** must emit a structured log with: agent name, step name, input summary, output summary, duration.
3. **All LLM calls** must be traced through LangFuse — every chain/agent invocation must have a LangFuse trace with spans for each step.
4. **Error paths** must log the full exception context with `structlog` `exc_info=True`.
5. **Use `structlog` bound loggers** — bind contextual fields (request_id, agent_name, workflow_id) at the entry point and carry them through the call chain.
6. **Log levels**: DEBUG for internal steps, INFO for business events (agent started/completed, recommendation generated), WARNING for degraded paths, ERROR for failures.
7. **LangFuse decorators/context managers** must wrap every LangGraph node function so traces are automatically captured.
8. **No silent failures** — every `except` block must log before re-raising or handling.

### Implementation Pattern
```python
import structlog
from langfuse import observe

logger = structlog.get_logger()

@observe()  # LangFuse trace
async def my_agent_step(input_data):
    log = logger.bind(agent="profiler", step="analyze_workload")
    log.info("step_started", input_summary=str(input_data)[:200])
    try:
        result = await do_work(input_data)
        log.info("step_completed", output_summary=str(result)[:200])
        return result
    except Exception:
        log.error("step_failed", exc_info=True)
        raise
```

## LLM Abstraction (Model-Agnostic Design)
All agent code MUST program against the `BaseChatModel` interface from `langchain-core`.
Never import a provider-specific class (e.g., `ChatBedrockConverse`, `ChatGoogleGenerativeAI`) directly in agent code.

### Rules
1. A **single LLM factory function** resolves the provider from config and returns a `BaseChatModel`.
2. Agent/node code receives the model instance via dependency injection — never constructs it.
3. **Lazy imports** — provider packages are imported inside the factory branch, so `langchain-google-genai` is not required unless Gemini is selected.
4. **Switching model = changing config** — zero code changes in agents/nodes.
5. Gemini is an **optional dependency**: `uv sync --extra gemini`.

### Factory Pattern
```python
from langchain_core.language_models import BaseChatModel
from src.config.settings import LLMSettings, AWSSettings

def get_llm(llm_settings: LLMSettings, aws_settings: AWSSettings | None = None) -> BaseChatModel:
    """Lazy-import factory. Agents receive this via dependency injection."""
    provider = llm_settings.provider.value
    if provider == "bedrock":
        from langchain_aws import ChatBedrockConverse  # lazy import
        return ChatBedrockConverse(
            model=llm_settings.model,
            region_name=aws_settings.resolved_bedrock_region,
            # credentials passed if explicitly set; otherwise IAM role / SSO
        )
    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI  # lazy import
        return ChatGoogleGenerativeAI(model=llm_settings.model)
    raise ValueError(f"Unsupported LLM provider: {provider!r}")
```

## Coding Conventions
- Use Pydantic v2 for all data models
- Type hints on all function signatures
- Docstrings (Google style) on all public functions and classes
- Use `async`/`await` for I/O-bound operations (API calls)
- Use `structlog` for ALL logging — never use `print()` or stdlib `logging` directly
- Use `langfuse` decorators (`@observe()`) on ALL LangGraph nodes and LLM call functions
- All cloud provider data must go through the normalizer before use
- Environment variables via `.env` file loaded with `pydantic-settings`
- **Never import LLM provider classes directly in agent code** — use the factory
- LangGraph depends on `langchain-core` only — do NOT add `langchain` as a dependency

## Dependency Version Policy
- Always verify package `requires_python` on PyPI before adding a new dependency
- Pin minimum versions to known-good releases; avoid upper-bound pins unless required
- Run `pip check` after any dependency change to catch conflicts early

## Documentation Maintenance (MANDATORY — Do This After Every Task)

After completing any meaningful task (new file, agent, migration, bug fix), you MUST update:

### 1. `.github/PROJECT_SPEC.md` — Source of Truth
Update the relevant section(s) to reflect the new state:
- Change the status symbol for any file you built or modified (❌/🔧 → ✅)
- Update the "What it does" description if behaviour changed
- Add new exported symbols to the symbol tables
- Mark live-test results when scripts are run (e.g. "Yes (6/6 ✅)")
- Remove completed items from the build order phases; update phase tables
- Update the `Last Updated` date in the header

### 2. `.github/CONTINUATION_PROMPT.md` — New Chat Starter
Keep the paste-block accurate so any new Copilot chat can immediately pick up the work:
- Move newly completed items from "What Needs to Be Built Next" to "What's Fully Built"
- Update "Next Immediate Task" to the correct next item
- Add any new gotchas or lessons learned to "Notes for Future Me"
- Update the `Last Updated` date in the header

### When to Update
| Event | Update PROJECT_SPEC | Update CONTINUATION_PROMPT |
|-------|--------------------|-----------------------------|
| New file fully implemented + verified | ✅ | ✅ |
| Existing file significantly changed | ✅ | Only if it affects what to build next |
| Live test run (pass or fail) | ✅ (update test results) | ✅ (if it unblocks next task) |
| Bug fixed in existing file | ✅ (update status/notes) | Only if it was blocking progress |
| New decision made (tech, design) | ✅ (add to invariants if needed) | ✅ (add to Key Rules or Notes) |

### Rule
**Never leave a completed task undocumented.** A new chat agent reading only
`copilot-instructions.md` + `PROJECT_SPEC.md` + `CONTINUATION_PROMPT.md` must be able
to understand exactly what is done, what is next, and how to proceed — without asking
any clarifying questions
