# llmpivot

**Runtime prompt management for production LLM applications — with a built-in AI assistant.**

Change a prompt in the web UI — see it reflected in your running app in seconds. No redeployment. No code changes. And when you need help, the **Assistant** is already watching the screen.

---

## What it does

llmpivot sits alongside your FastAPI app. You mount it at a path (e.g. `/prompts`) and it gives you:

- A web UI to create, edit, version, and activate prompts
- An in-memory cache so your app reads prompts at zero latency
- Full version history with one-click rollback
- RBAC authentication (admin / editor / viewer)
- A **prompt hierarchy** — organise prompts as agent → tool → utility trees
- Per-prompt **metadata** (purpose, type, owner, sensitivity, call location)
- Auto-generated **changelogs** on every version save
- An **Application Context** document so the Assistant understands your whole system
- A **floating Assistant widget** that watches what you're doing and surfaces observations, alerts, and suggestions without being asked
- A **fallback snapshot** (`prompts_fallback.json`) written on every activation — if the DB goes down your app keeps serving the last known good prompts
- Usage logging, A/B testing, audit log, import/export, and a real `/healthz` endpoint

Your app code just does:

```python
meta = await aget_prompt_with_meta("my_prompt")
# use meta["content"] as your LLM system prompt
```

---

## Installation

```bash
# Standard (SQLite, recommended for most setups)
pip install llmpivot

# With strong password hashing (recommended for production)
pip install "llmpivot[security]"

# With MongoDB backend
pip install "llmpivot[mongo]"

# Everything
pip install "llmpivot[all]"
```

Or from the repo:

```bash
pip install -r requirements.txt
pip install -e .
```

---

## Quick start

```python
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from llmpivot import PromptManager, PromptNotFoundError, aget_prompt_with_meta, log_prompt_usage

manager = PromptManager(
    db_path="prompts.db",
    cache_ttl=5,
    auth_mode="rbac",
    secret_key=os.environ["LLMPIVOT_SECRET"],
    cookie_secure=True,
    bootstrap_admin=True,
    bootstrap_password=os.environ.get("LLMPIVOT_PASSWORD", "changeme"),

    # LLM — enables AI suggestions, A/B testing, and the Assistant widget
    llm_url="https://api.openai.com/v1/chat/completions",
    llm_api_key=os.environ["OPENAI_API_KEY"],
    llm_model="gpt-4o",

    # Assistant widget
    pivot_enabled=True,
    pivot_proactive=True,
    auto_changelog=True,

    # Fallback snapshot
    fallback_snapshot=True,
)

app = FastAPI()

@app.exception_handler(PromptNotFoundError)
async def _not_found(request: Request, exc: PromptNotFoundError):
    return JSONResponse(status_code=404, content={"error": "prompt_not_found", "detail": str(exc)})

app.mount("/prompts", manager.mount_ui())

@app.get("/run")
async def run(text: str):
    meta = await aget_prompt_with_meta("my_prompt")
    output = call_your_llm(meta["content"], text)
    log_prompt_usage("my_prompt", meta["version_id"], text, output)
    return {"output": output}
```

Run:

```bash
uvicorn example_app:app --reload
```

Open **http://localhost:8000/prompts/list** — log in with `admin` / `changeme`.

---

## Azure OpenAI

Pass the Azure endpoint directly — llmpivot detects Azure URLs automatically, uses the correct `api-key` header, and selects `max_completion_tokens` vs `max_tokens` based on the model generation:

```python
manager = PromptManager(
    db_path="prompts.db",
    llm_url="https://myresource.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-08-01-preview",
    llm_api_key=os.environ["AZURE_OPENAI_API_KEY"],
    llm_model="gpt-4o",           # used for model-generation detection
    llm_api_type="azure",          # explicit; or omit and let auto-detection handle it
    pivot_enabled=True,
)
```

Or let `example_app.py` handle everything from your `.env`:

```
AZURE_OPENAI_ENDPOINT=https://myresource.openai.azure.com/
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o
AZURE_OPENAI_API_VERSION=2024-08-01-preview
```

---

## The Assistant widget

When `pivot_enabled=True`, a floating **Assistant** pill appears in the bottom-right corner of every dashboard page. It has two modes:

**Proactive** (default) — opens a page and the Assistant immediately analyses it:
- Flags prompts with no active version (red warning)
- Alerts when a parent prompt changed but its children haven't been reviewed (amber)
- Suggests adding metadata or changelog entries (blue)
- Shows version count, last editor, and sensitivity level as context (grey)

**Chat** — type a question in the input at the bottom of the expanded panel:
- "What does this prompt do and where is it called?"
- "What would break if I changed the tone here?"
- "Make this more specific about the output format" → returns a diff preview, never saves without confirmation
- "Show me all prompts under the support agent"

The widget state (open/closed) is persisted per browser in `localStorage`. No page refresh needed.

---

## Prompt hierarchy

Prompts can have parent-child relationships. This lets you model real multi-agent systems:

```
customer_support_agent    [agent]
├── classify_intent       [tool]
├── draft_response        [tool]
└── escalation_check      [tool]
```

Set hierarchy in **Metadata** (`/prompts/metadata/{name}`). The tree view is at `/prompts/tree`. When you edit a parent prompt, the Assistant automatically warns you about child prompts that may need review.

---

## Fallback snapshot

`prompts_fallback.json` lives next to your database and contains all currently active prompt contents:

```json
{
  "my_prompt": "You are a helpful assistant...",
  "classify_intent": "Classify the user intent into..."
}
```

Written atomically on every activation and on startup. Use `aget_prompt_with_fallback` instead of `aget_prompt` for zero-downtime resilience:

```python
from llmpivot import aget_prompt_with_fallback

# Tries live DB/cache first; serves from snapshot if DB is unreachable
content = await aget_prompt_with_fallback("my_prompt")
```

---

## Configuration reference

All parameters passed to `PromptManager()`.

### Storage

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str` | `"prompts.db"` | SQLite file path |
| `storage_type` | `str` | `"sqlite"` | `"sqlite"` or `"mongodb"` |
| `mongo_uri` | `str` | `None` | MongoDB connection URI |
| `mongo_db_name` | `str` | `"llmpivot"` | MongoDB database name |
| `tenant_id` | `str` | `"default"` | Namespace for multi-tenant isolation |
| `cache_ttl` | `int` | `5` | Seconds before cache re-fetches from DB |

### Auth

| Parameter | Type | Default | Description |
|---|---|---|---|
| `auth_mode` | `str` | `"disabled"` | `"disabled"`, `"protected"`, or `"rbac"` |
| `secret_key` | `str` | **required for rbac** | HMAC key for session cookie signing |
| `cookie_secure` | `bool` | `True` | HTTPS-only session cookie. Set `False` for local HTTP dev |
| `bootstrap_admin` | `bool` | `False` | Create `admin` user on first startup |
| `bootstrap_password` | `str` | `"admin"` | Password for the bootstrapped admin |

### Logging

| Parameter | Type | Default | Description |
|---|---|---|---|
| `log_sample_rate` | `float` | `1.0` | Fraction of calls to log (0.0–1.0) |

### LLM

| Parameter | Type | Default | Description |
|---|---|---|---|
| `llm_url` | `str` | `None` | Chat completions endpoint URL |
| `llm_api_key` | `str` | `None` | API key |
| `llm_model` | `str` | `"gpt-3.5-turbo"` | Model name (also used for token-field auto-detection) |
| `llm_api_type` | `str` | auto | `"openai"` or `"azure"`. Auto-detected from URL if omitted |
| `llm_max_tokens` | `int` | `None` | Set for GPT-4 and earlier. Auto-selected if both are `None` |
| `llm_max_completion_tokens` | `int` | `None` | Set for GPT-5 / o1 / o3+. Auto-selected if both are `None` |
| `llm_temperature` | `float` | `None` | Omitted from request if `None` (new-gen models reject it) |
| `llm_top_p` | `float` | `None` | Omitted from request if `None` |
| `llm_timeout` | `float` | `30.0` | Request timeout in seconds |
| `llm_extra_params` | `dict` | `None` | Extra body params, e.g. `{"response_format": {"type": "json_object"}}` |
| `llm_suggester_prompt` | `str` | `None` | Override the system prompt for the AI suggest feature |

### Assistant

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pivot_enabled` | `bool` | `False` | Master on/off switch. `False` = no widget, no LLM calls |
| `pivot_proactive` | `bool` | `True` | Auto-analyse current page. `False` = chat-only mode |
| `auto_changelog` | `bool` | `True` | Auto-generate changelog entry on every version save (requires LLM) |
| `pivot_model` | `str` | `None` | Override model for the Assistant specifically |

### Fallback snapshot

| Parameter | Type | Default | Description |
|---|---|---|---|
| `fallback_snapshot` | `bool` | `True` | Write `prompts_fallback.json` on activation and startup |
| `fallback_path` | `str` | `None` | Override file path. Default: same directory as `db_path` |

---

## Auth modes

### `"disabled"` (default)
No authentication. Use only behind a VPN or on localhost.

### `"protected"`
Single admin password required for all write operations.

```python
PromptManager(protected_mode=True, admin_password="your-password")
```

### `"rbac"` (recommended for production)
Full multi-user RBAC with login/logout, session cookies, and role enforcement.

```python
PromptManager(
    auth_mode="rbac",
    secret_key=os.environ["LLMPIVOT_SECRET"],
    bootstrap_admin=True,
    bootstrap_password="first-login-password",
)
```

**Roles:**

| Role | Access |
|---|---|
| `admin` | Everything: user management, delete prompts, import/export, edit, activate |
| `editor` | Create and edit versions, activate versions, import, edit metadata and context |
| `viewer` | Read-only: view prompts, versions, diffs, logs, export, browse hierarchy |

---

## Web UI routes

| Route | Auth | Description |
|---|---|---|
| `/prompts/list` | viewer | All prompts, active versions, last editor |
| `/prompts/tree` | viewer | Prompt hierarchy tree view |
| `/prompts/edit/__new__` | editor | Create a new prompt |
| `/prompts/edit/{name}` | editor | Add a new version (hierarchy warning shown if children exist) |
| `/prompts/detail/{name}` | viewer | Version history, activate/rollback |
| `/prompts/metadata/{name}` | editor | Edit prompt metadata and hierarchy |
| `/prompts/changelog/{name}` | viewer | Per-version changelog feed |
| `/prompts/diff/{name}` | viewer | Side-by-side diff between any two versions |
| `/prompts/test/{name}` | viewer | A/B test two versions with live LLM calls |
| `/prompts/context` | editor | Application Context document (read/write) |
| `/prompts/health` | viewer | Prompt health dashboard |
| `/prompts/search` | viewer | Semantic search across all prompts |
| `/prompts/logs` | viewer | Usage log viewer |
| `/prompts/import` | editor | Bulk import from JSON |
| `/prompts/export` | viewer | Download active prompts as JSON |
| `/prompts/users` | admin | Create users, assign roles |
| `/prompts/login` | — | Login page |
| `/prompts/logout` | — | Clears session cookie |
| `/prompts/healthz` | — | Liveness probe (DB + worker health) |
| `/prompts/pivot/observe` | viewer | SSE stream: proactive observations for current page |
| `/prompts/pivot/chat` | viewer | Chunked HTTP: Assistant chat reply stream |

---

## Python API

### `aget_prompt(name) → str`
Returns the active prompt content. Preferred in async routes.

### `aget_prompt_with_meta(name) → dict`
Returns `{"content": str, "version_id": int}`. Use this when logging.

### `aget_prompt_with_fallback(name) → str`
Tries live cache/DB first. Falls back to `prompts_fallback.json` if unavailable. Raises `PromptNotFoundError` only if absent from both.

### `get_prompt(name) → str`
Sync wrapper. Use in plain scripts or non-async contexts.

### `log_prompt_usage(name, version_id, input_text, output_text)`
Fire-and-forget usage logging. Non-blocking.

---

## Security notes

- **`secret_key`** must be set explicitly when `auth_mode="rbac"`. The default raises `ValueError`. Generate: `python -c "import secrets; print(secrets.token_hex(32))"`
- **`cookie_secure`** defaults to `True` — HTTPS-only. Set `False` only for local HTTP dev.
- **CSRF protection** on all state-mutating POST requests. Tokens are session-bound.
- **Login rate limiting** — 10 failed attempts per IP per 5 minutes → 15-minute lockout.
- **Password hashing** — Argon2id → bcrypt → PBKDF2 (auto-selected, backward compatible).
- **Audit log** — every create, activate, delete, import, and user-create action is recorded.

---

## Production checklist

- [ ] `secret_key` loaded from environment variable or secrets manager
- [ ] `cookie_secure=True` — app served over HTTPS
- [ ] `bootstrap_password` changed after first login
- [ ] `argon2-cffi` installed: `pip install argon2-cffi`
- [ ] `auth_mode="rbac"`
- [ ] `/prompts/healthz` wired to load balancer / k8s liveness probe
- [ ] `cache_ttl` ≤ 5s for multi-worker deployments
- [ ] `log_sample_rate` reduced if log volume is high (e.g. `0.1`)
- [ ] `prompts.db` on a persistent volume with scheduled backups
- [ ] `prompts_fallback.json` included in deployment for offline resilience

---

## License

[MIT](LICENSE) © Sanath Goutham
