# Cloud Orchestrator IDSS

An Agentic AI-Driven Intelligent Decision Support System for Cloud-Agnostic Resource Orchestration and Automated Procurement.

**M.Tech Dissertation — Ramkumar J (2024MT03027), BITS Pilani WILP**

---

## Quick Start

```bash
# 1. Clone & install
git clone <repo>
cd Dissertation
pip install uv
uv sync

# 2. Configure environment
cp .env.example .env
# → Fill in credentials as described below

# 3. Run the API server
uv run python -m src.main
```

---

## Authentication Setup

### Azure — No Credentials Required

Azure pricing data is fetched from the **public Azure Retail Prices API**
(`https://prices.azure.com/api/retail/prices`) — no subscription, service
principal, or API key is needed.

Leave all `AZURE_*` fields in `.env` blank.

---

### AWS — IAM Credentials (Pricing API + Bedrock)

AWS credentials are used for **two services**:

| Service | API | Region |
|---------|-----|--------|
| EC2 / RDS / EKS pricing | `boto3` Pricing API | `us-east-1` (only available here) |
| Claude LLM (Bedrock) | `boto3` Bedrock Runtime | configurable (default `us-east-1`) |

#### Option A — Temporary / SSO Session Credentials (Recommended for dev)

If you access AWS via **AWS SSO / IAM Identity Center** (e.g. a company or
lab account), your credentials are temporary and include a session token.

1. Log in via your AWS SSO portal (or `aws sso login --profile <profile>`).
2. Find or export three values:

   ```dotenv
   AWS_ACCESS_KEY_ID=ASIA...          # starts with ASIA for temporary creds
   AWS_SECRET_ACCESS_KEY=...
   AWS_SESSION_TOKEN=...              # required when key starts with ASIA
   AWS_DEFAULT_REGION=us-east-1
   ```

3. Paste them into your `.env` file.

> ⚠️ **Temporary credentials expire** (typically 1–8 hours). Re-run the
> SSO portal flow to get fresh ones when API calls start returning
> `ExpiredTokenException`.

#### Option B — Long-term IAM User Credentials

If you have a dedicated IAM user (not recommended for production):

```dotenv
AWS_ACCESS_KEY_ID=AKIA...            # starts with AKIA for long-term creds
AWS_SECRET_ACCESS_KEY=...
# AWS_SESSION_TOKEN — leave blank / omit
AWS_DEFAULT_REGION=us-east-1
```

#### Required IAM Permissions

The IAM principal needs the following permissions:

```json
{
  "Effect": "Allow",
  "Action": [
    "pricing:GetProducts",
    "pricing:DescribeServices",
    "pricing:GetAttributeValues",
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream"
  ],
  "Resource": "*"
}
```

#### Bedrock — Cross-Region Inference Profile

All newer Bedrock models **require a cross-region inference profile ID** —
the bare `anthropic.*` model ID will return a `ValidationException`. The
`.env.example` already has the correct value:

```dotenv
LLM_MODEL=us.anthropic.claude-sonnet-4-5-20250929-v1:0   # ← note "us." prefix
```

Make sure this model is **enabled** in your AWS account:
`AWS Console → Bedrock → Model access → Enable "Claude Sonnet 4.5"`.

---

### GCP — Application Default Credentials (ADC)

GCP pricing data comes from the **Cloud Billing Catalog API**
(`cloudbilling.googleapis.com`).

#### Setup (one-time)

```bash
# 1. Install gcloud CLI
brew install --cask google-cloud-sdk   # macOS
# or: https://cloud.google.com/sdk/docs/install

# 2. Log in and create ADC credentials
gcloud auth login
gcloud auth application-default login

# 3. Set your project
gcloud config set project YOUR_PROJECT_ID

# 4. Enable the Billing Catalog API
gcloud services enable cloudbilling.googleapis.com
```

Then set in `.env`:

```dotenv
GCP_PROJECT_ID=your-gcp-project-id
GCP_CREDENTIALS_PATH=          # leave blank — ADC is used automatically
```

The ADC credential file is stored at:
`~/.config/gcloud/application_default_credentials.json`

> ℹ️ **Why ADC and not a service account JSON?**
> Organisation policies often block service account key creation
> (`iam.disableServiceAccountKeyCreation`). ADC is the recommended
> alternative for developer machines — no key file to rotate or leak.

#### Optional — Service Account JSON

If you have a service account key file (e.g. for CI/CD), set:

```dotenv
GCP_CREDENTIALS_PATH=/path/to/service-account-key.json
```

The adapter will use the key file when this path is set, and fall back to
ADC when it is blank.

---

### LangFuse — LLM Observability (Optional)

LangFuse provides trace-level visibility into every LLM call and agent step.

1. Create a free account at <https://cloud.langfuse.com>
2. Create a project and copy the keys:

```dotenv
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

If the keys are left blank, the system degrades gracefully — a harmless
`Authentication error` warning is logged and all other functionality
continues normally.

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_PROVIDER` | ✅ | `bedrock` | `bedrock` or `gemini` |
| `LLM_MODEL` | ✅ | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Model / inference profile ID |
| `AWS_ACCESS_KEY_ID` | ✅ (Bedrock) | — | IAM access key |
| `AWS_SECRET_ACCESS_KEY` | ✅ (Bedrock) | — | IAM secret key |
| `AWS_SESSION_TOKEN` | ⚠️ STS only | — | Required when key starts with `ASIA` |
| `AWS_DEFAULT_REGION` | ✅ (Bedrock) | `us-east-1` | Default AWS region |
| `GCP_PROJECT_ID` | ✅ (GCP) | — | GCP project ID |
| `GCP_CREDENTIALS_PATH` | ❌ | `` (ADC) | Path to service account JSON; blank = ADC |
| `LANGFUSE_PUBLIC_KEY` | ❌ | — | LangFuse project public key |
| `LANGFUSE_SECRET_KEY` | ❌ | — | LangFuse project secret key |
| `APP_SKU_CACHE_DB_PATH` | ❌ | `data/sku_cache.db` | SQLite SKU cache path |
| `APP_SKU_CACHE_TTL_HOURS` | ❌ | `24` | Cache TTL in hours |

---

## Project Structure

```
src/
├── agents/          # Five autonomous LangGraph agents
│   ├── clarifier.py     ✅ live
│   ├── profiler.py      🔧 in progress
│   ├── sizer.py         ❌ pending
│   ├── finops.py        ❌ pending
│   └── rfp_writer.py    ❌ pending
├── orchestrator/    # LangGraph workflow graph + shared state
├── providers/       # Cloud pricing adapters (AWS ✅, Azure ✅, GCP ✅)
├── services/        # PricingService + SQLite cache (970× speedup)
├── models/          # Pydantic v2 data models
├── engines/         # Bin-packing, scoring, WAF compliance
├── llm/             # Model-agnostic LLM factory (Bedrock / Gemini)
├── config/          # Settings + structlog + LangFuse wiring
└── api/             # FastAPI SSE routes
dashboard/           # React (Vite + TypeScript + Tailwind + shadcn/ui)
```

See [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) for the full per-file
status, exported symbols, and build roadmap.
