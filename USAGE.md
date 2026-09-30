# llmpivot — Usage Guide

Step-by-step instructions for every scenario, from local dev to production deployment.

---

## Table of contents

1. [Installation](#1-installation)
2. [Running the example app](#2-running-the-example-app)
3. [Creating your first prompt](#3-creating-your-first-prompt)
4. [Using prompts in your application](#4-using-prompts-in-your-application)
5. [Fallback snapshot](#5-fallback-snapshot)
6. [Prompt hierarchy](#6-prompt-hierarchy)
7. [Prompt metadata](#7-prompt-metadata)
8. [Changelog](#8-changelog)
9. [Application Context](#9-application-context)
10. [The Assistant widget](#10-the-assistant-widget)
11. [LLM configuration](#11-llm-configuration)
12. [AI suggestions and A/B testing](#12-ai-suggestions-and-ab-testing)
13. [Editing and versioning prompts](#13-editing-and-versioning-prompts)
14. [User management](#14-user-management)
15. [Multi-tenancy](#15-multi-tenancy)
16. [MongoDB backend](#16-mongodb-backend)
17. [Import and export](#17-import-and-export)
18. [Usage logging](#18-usage-logging)
19. [Audit log](#19-audit-log)
20. [Health checks](#20-health-checks)
21. [Production deployment](#21-production-deployment)
22. [Upgrading from an older version](#22-upgrading-from-an-older-version)

---

## 1. Installation

```bash
pip install llmpivot

# Recommended: strong password hashing
pip install "llmpivot[security]"

# Everything
pip install "llmpivot[all]"
```

From source:

```bash
git clone https://github.com/Lancer59/llmpivot
cd llmpivot
pip install -r requirements.txt
pip install -e .
```

---

## 2. Running the example app

The included `example_app.py` reads LLM credentials from a `.env` file automatically. Create one:

```
# .env

# Azure OpenAI (takes precedence if AZURE_OPENAI_ENDPOINT is set)
AZURE_OPENAI_ENDPOINT=https://myresource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o
AZURE_OPENAI_API_VERSION=2024-08-01-preview

# Or standard OpenAI
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o

# llmpivot session security
LLMPIVOT_SECRET=    # generate: python -c "import secrets; print(secrets.token_hex(32))"
LLMPIVOT_PASSWORD=changeme
```

Then run:

```bash
uvicorn example_app:app --reload
```

Open **http://localhost:8000/prompts/list** — log in with `admin` / `changeme`.

Set a stable `LLMPIVOT_SECRET` so sessions survive restarts:

```bash
# Windows
set LLMPIVOT_SECRET=<token>
# Linux / macOS
export LLMPIVOT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
```

---

## 3. Creating your first prompt

**Via the UI:**

1. Go to `/prompts/edit/__new__`
2. Enter a prompt name (e.g. `summarize_prompt`) — lowercase, underscores only
3. Enter the prompt content
4. Set tag to `prod` and active to `Yes`
5. Click **Save New Version**

**Via code (seed script):**

```python
import asyncio
from llmpivot import PromptManager

manager = PromptManager(db_path="prompts.db", auth_mode="disabled")

async def seed():
    await manager.storage.create_version(
        name="summarize_prompt",
        content="Summarize the following text in 3 bullet points:\n\n{text}",
        created_by="seed-script",
        tag="prod",
        set_active=True,
        tenant_id="default",
    )

asyncio.run(seed())
```

---

## 4. Using prompts in your application

### Async (FastAPI — recommended)

```python
from llmpivot import aget_prompt_with_meta, log_prompt_usage

@app.get("/chat")
async def chat(user_input: str):
    meta = await aget_prompt_with_meta("chat_system_prompt")
    response = await your_llm.complete(system=meta["content"], user=user_input)
    log_prompt_usage("chat_system_prompt", meta["version_id"], user_input, response)
    return {"response": response}
```

### Content only (no logging)

```python
from llmpivot import aget_prompt

prompt = await aget_prompt("summarize_prompt")
```

### Sync context (scripts, background jobs)

```python
from llmpivot import get_prompt

prompt = get_prompt("summarize_prompt")
```

### Handling missing prompts

```python
from llmpivot import PromptNotFoundError

@app.exception_handler(PromptNotFoundError)
async def _handler(request, exc):
    return JSONResponse(status_code=404, content={"error": str(exc)})
```

---

## 5. Fallback snapshot

llmpivot writes `prompts_fallback.json` next to your database file every time a version is activated and on startup. The file contains only the currently active content — no history, no metadata:

```json
{
  "my_prompt": "You are a helpful assistant...",
  "classify_intent": "Classify the user intent into one of: ..."
}
```

Use `aget_prompt_with_fallback` to serve from this file when the DB is unreachable:

```python
from llmpivot import aget_prompt_with_fallback

# Tries live DB/cache first; falls back to snapshot on any error
content = await aget_prompt_with_fallback("my_prompt")
```

This raises `PromptNotFoundError` only if the prompt is absent from both the live DB and the snapshot.

**Configuration:**

```python
PromptManager(
    fallback_snapshot=True,          # default — set False to disable writes
    fallback_path="/custom/path.json",  # default: same dir as db_path
)
```

> Include `prompts_fallback.json` in your deployment artefact or persistent volume so it survives container restarts.

---

## 6. Prompt hierarchy

Prompts can have parent-child relationships, matching the structure of real multi-agent systems:

```
customer_support_agent    [agent]
├── classify_intent       [tool]
├── draft_response        [tool]
└── escalation_check      [tool]

shared/
└── brand_voice           [utility — referenced by multiple agents]
```

**Setting a hierarchy:**
1. Open a prompt's **Metadata** page (`/prompts/metadata/{name}`)
2. Set **Prompt Type** (`agent`, `tool`, `sub-agent`, `utility`, `system`)
3. Choose a **Parent Prompt** from the dropdown

**Tree view:** `/prompts/tree` shows all prompts as an expandable tree with type badges, active version, and quick-edit links.

**Hierarchy-aware editing:** When you open a parent prompt for editing, a yellow warning panel lists its child prompts and reminds you to review them before activating changes.

**Branch operations** (from the tree view):
- Assign or detach parents
- Activate all prompts in a subtree at once
- Export a whole agent + its tools as a single JSON bundle

---

## 7. Prompt metadata

Every prompt has a structured metadata panel at `/prompts/metadata/{name}`:

| Field | Description |
|---|---|
| Purpose | One sentence: what this prompt does |
| Prompt Type | `agent`, `tool`, `sub-agent`, `utility`, `system`, `unclassified` |
| Parent Prompt | Defines the hierarchy |
| Feature Area | e.g. `onboarding`, `support-chat` |
| Called From | File path or route where `aget_prompt_with_meta()` is called |
| Model Used | LLM model and temperature this prompt is paired with |
| Input Variables | Placeholder names used in the prompt content |
| Owner | Team or person responsible |
| Sensitivity | `low`, `medium`, `high` — high triggers a warning before edits |
| Notes | Free-form design decisions |

Metadata is shown as a context strip on the edit page and used by the Assistant for all analysis.

---

## 8. Changelog

Every version can have a changelog entry explaining what changed and why. Entries are shown in version history at `/prompts/changelog/{name}`.

**Auto-generated** (requires LLM configured + `auto_changelog=True`):
When you save a new version, the LLM diffs the old and new content and writes a changelog entry automatically. It's marked `✨ auto`. You can always edit or replace it.

**Manual:**
Go to `/prompts/changelog/{name}/edit/{version_id}` and write your own entry.

Example auto-generated entry:
> v4 → v5: Removed the phrase "I apologize for any inconvenience" (passive/generic) and replaced with a direct acknowledgment. Tightens alignment with the brand voice guidelines. Impact: check `draft_response` (child prompt) — it references tone guidance from this prompt.

---

## 9. Application Context

A single Markdown document you write once at `/prompts/context`. The Assistant reads it as its background knowledge on every analysis and chat turn.

Include:
- What your application does
- Each agent's name, purpose, and when it's invoked
- Which routes call which prompts
- Tone, brand voice, and domain vocabulary rules
- Default model, temperature, and tool-calling patterns

The document is versioned (every save creates a new version). The Assistant uses it without you having to repeat yourself.

---

## 10. The Assistant widget

When `pivot_enabled=True`, a floating **Assistant** pill appears at the bottom-right of every dashboard page.

**Collapsed** — shows a coloured dot (idle/thinking/alert/warn), the label "Assistant", and a preview of the latest observation.

**Expanded** — shows:
- A live observation feed for the current page
- Chat messages
- A text input at the bottom

**Observation types:**
| Colour | Type | Meaning |
|---|---|---|
| Grey | Observation | Neutral context — version count, last editor |
| Amber | Alert | Worth knowing before acting — child prompts not reviewed |
| Red | Warning | Active risk — no active version, PromptNotFoundError will be thrown |
| Blue | Suggestion | Actionable recommendation with an Apply link |

**Chat examples:**
```
"What does this prompt do and where is it called?"
"What would break if I made this more formal?"
"Make this more specific about the output format"   → returns diff preview
"Show me all prompts under the support agent"
"Which prompts haven't been touched in 90 days but still have traffic?"
```

The Assistant never saves anything without showing you a diff and getting an explicit confirmation click first.

**Configuration:**
```python
PromptManager(
    pivot_enabled=True,      # show the widget
    pivot_proactive=True,    # auto-analyse current page (False = chat-only)
    auto_changelog=True,     # generate changelog on save (requires LLM)
    pivot_model="gpt-4o",    # override model for Assistant specifically
)
```

Set `pivot_enabled=False` to completely disable the widget — no rendering, no LLM calls, zero behaviour change to existing features.

---

## 11. LLM configuration

### Standard OpenAI

```python
PromptManager(
    llm_url="https://api.openai.com/v1/chat/completions",
    llm_api_key=os.environ["OPENAI_API_KEY"],
    llm_model="gpt-4o",
)
```

### Azure OpenAI

```python
PromptManager(
    llm_url="https://myresource.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-08-01-preview",
    llm_api_key=os.environ["AZURE_OPENAI_API_KEY"],
    llm_model="gpt-4o",
    llm_api_type="azure",   # or omit — auto-detected from URL
)
```

### Token field auto-detection

llmpivot inspects the model name and automatically picks the correct token limit field:
- `max_tokens` for GPT-4 and earlier
- `max_completion_tokens` for GPT-5, o1, o3, o4 series

You can always override:

```python
PromptManager(
    llm_max_completion_tokens=1024,   # explicit override for new-gen models
    # llm_max_tokens=1024,            # explicit override for older models
)
```

### Temperature and other params

```python
PromptManager(
    llm_temperature=0.7,              # omit entirely if None (new-gen models reject it)
    llm_top_p=0.95,
    llm_timeout=60.0,
    llm_extra_params={                # merged into every request body
        "response_format": {"type": "json_object"},
    },
)
```

### Any OpenAI-compatible endpoint

```python
PromptManager(
    llm_url="http://localhost:11434/v1/chat/completions",  # Ollama
    llm_api_key="ollama",
    llm_model="llama3.2",
    llm_api_type="openai",
)
```

---

## 12. AI suggestions and A/B testing

**AI suggestion (edit page):**
With `llm_url` configured, a **✨ Get AI Suggestion** button appears on the edit page. It sends the current content to the LLM with a prompt-engineering system prompt and replaces the textarea. You still save manually.

**A/B testing:**
1. Go to `/prompts/test/{name}`
2. Select Version A and Version B
3. Enter test input
4. Click **⚡ Run A/B Comparison** — both outputs appear side by side

The LLM client retries up to 3 times with exponential backoff on 5xx, 429, timeout, and connection errors.

---

## 13. Editing and versioning prompts

Every save creates a **new version** — nothing is overwritten.

**Activating a previous version (rollback):**
1. Go to `/prompts/detail/{name}`
2. Find the target version
3. Click **Make Active** — cache is invalidated immediately, fallback snapshot is refreshed

**Comparing versions:** `/prompts/diff/{name}` — select two versions, get a unified diff.

**Cache behaviour:** The cache key is invalidated immediately on save or activation. No TTL wait.

---

## 14. User management

Only with `auth_mode="rbac"`.

**Via UI:** `/prompts/users` → fill in username, email, password, role → **Create User**

**Via code:**

```python
from llmpivot.auth import hash_password

await manager.storage.create_user(
    username="alice",
    password_hash=hash_password("her-strong-password"),
    role="editor",
    email="alice@example.com",
    tenant_id="default",
)
```

**Roles:**

| Role | Can do |
|---|---|
| `admin` | Everything: users, delete prompts, import/export, edit, activate |
| `editor` | Create/edit versions, activate, import, metadata, context, changelog |
| `viewer` | Read-only: prompts, versions, diffs, logs, export, tree, hierarchy |

---

## 15. Multi-tenancy

Each `tenant_id` is fully isolated. Prompts, versions, logs, and users from one tenant are invisible to another.

```python
manager_a = PromptManager(db_path="prompts.db", tenant_id="org_acme")
manager_b = PromptManager(db_path="prompts.db", tenant_id="org_globex")
```

> Only the last `PromptManager(...)` sets the global `_instance`. For multiple tenants in one process, call `manager.get_with_meta()` directly instead of using module-level helpers.

---

## 16. MongoDB backend

```bash
pip install "llmpivot[mongo]"
```

```python
manager = PromptManager(
    storage_type="mongodb",
    mongo_uri="mongodb://localhost:27017",
    mongo_db_name="llmpivot_prod",
    tenant_id="acme",
)
```

Recommended indexes:

```javascript
db.prompts.createIndex({ tenant_id: 1, name: 1 }, { unique: true })
db.prompt_versions.createIndex({ prompt_name: 1, tenant_id: 1, is_active: 1 })
db.prompt_versions.createIndex({ prompt_name: 1, tenant_id: 1, version_number: -1 })
db.prompt_logs.createIndex({ prompt_name: 1, tenant_id: 1, timestamp: -1 })
```

---

## 17. Import and export

**Export:** `/prompts/export` downloads `prompts.json` with all active prompt contents.

**Import:** `/prompts/import` — upload a JSON file in the same format:

```json
{
  "prompt_name": "prompt content here",
  "another_prompt": "another content"
}
```

Existing prompts get a new version. New prompts are created. Cache is invalidated and the fallback snapshot is refreshed automatically.

**Via code:**

```python
with open("prompts.json") as f:
    data = json.load(f)

result = await manager.storage.import_prompts(data, imported_by="deploy-script")
for name in result["created"] + result["updated"]:
    manager.cache.invalidate(name)
manager.schedule_snapshot_refresh()
```

---

## 18. Usage logging

```python
from llmpivot import log_prompt_usage

log_prompt_usage(
    "my_prompt",
    meta["version_id"],
    input_text=user_query,
    output_text=llm_response,
)
```

- **Non-blocking** — queued in memory, never blocks requests
- **Batched** — flushed to DB every 2 seconds in groups of 50
- **Bounded** — queue capped at 10,000 items; drops with `WARNING` if exceeded
- **Sampled** — `log_sample_rate=0.1` logs 10% of calls

View at `/prompts/logs`. Filter by prompt name: `/prompts/logs?prompt=name`.

---

## 19. Audit log

Every admin action is recorded automatically:

| Action | Trigger |
|---|---|
| `version_created` | Prompt saved (new or edit) |
| `version_activated` | Version made active |
| `prompt_deleted` | Soft delete |
| `prompts_imported` | Bulk import |
| `user_created` | New user added |
| `metadata_updated` | Prompt metadata saved |
| `app_context_updated` | Application Context saved |

```python
logs = await manager.storage.fetch_audit_logs(limit=100, tenant_id="default")
```

---

## 20. Health checks

```bash
curl http://localhost:8000/prompts/healthz
```

```json
{
  "status": "ok",
  "cache_size": 8,
  "log_queue_depth": 0,
  "cache_worker_alive": true,
  "logger_worker_alive": true
}
```

Returns `503` with `"status": "degraded"` and an `"issues"` array if anything is wrong.

**Kubernetes liveness probe:**

```yaml
livenessProbe:
  httpGet:
    path: /prompts/healthz
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 30
  failureThreshold: 3
```

---

## 21. Production deployment

```python
manager = PromptManager(
    db_path=os.environ.get("LLMPIVOT_DB", "/data/prompts.db"),
    cache_ttl=5,
    auth_mode="rbac",
    secret_key=os.environ["LLMPIVOT_SECRET"],
    cookie_secure=True,
    bootstrap_admin=True,
    bootstrap_password=os.environ["LLMPIVOT_PASSWORD"],
    log_sample_rate=float(os.environ.get("LLMPIVOT_LOG_RATE", "1.0")),
    llm_url=os.environ.get("LLM_URL"),
    llm_api_key=os.environ.get("LLM_API_KEY"),
    llm_model=os.environ.get("LLM_MODEL", "gpt-4o"),
    pivot_enabled=True,
    fallback_snapshot=True,
)
```

```bash
uvicorn myapp:app --workers 4 --host 0.0.0.0 --port 8000
```

> With multiple workers, keep `cache_ttl ≤ 5s`. Each worker has its own cache; stale values resolve within one TTL period.

**Production checklist:**

- [ ] `LLMPIVOT_SECRET` is a 64-char random hex string — never the default
- [ ] `cookie_secure=True` — app behind HTTPS
- [ ] `bootstrap_password` changed after first login
- [ ] `argon2-cffi` installed
- [ ] `auth_mode="rbac"`
- [ ] `/prompts/healthz` wired to load balancer
- [ ] `prompts.db` on a persistent volume with scheduled backups
- [ ] `prompts_fallback.json` included in deployment artifact

---

## 22. Upgrading from an older version

**New tables** (`prompt_metadata`, `prompt_changelog`, `app_context`, `pivot_observations`, `pivot_conversations`) are created automatically on first startup. No manual migration needed.

**If you have existing data with missing `tenant_id` values:**

```python
import asyncio
from llmpivot import PromptManager

manager = PromptManager(db_path="prompts.db")
result = asyncio.run(manager.storage.migrate_missing_tenant_ids(tenant_id="default"))
print(result)  # {"prompts": N, "prompt_versions": N, "users": N}
```

**`secret_key` now required for `auth_mode="rbac"`** — omitting it raises `ValueError`. Set `secret_key=os.environ["LLMPIVOT_SECRET"]`.

**New `PromptManager` LLM params** — `llm_url`, `llm_api_key`, `llm_model` remain unchanged. New optional params (`llm_api_type`, `llm_max_tokens`, `llm_temperature`, etc.) all default to `None` (auto-detect). Existing configurations work without changes.

**`pivot_enabled` defaults to `False`** — the Assistant widget does not appear unless you opt in.
