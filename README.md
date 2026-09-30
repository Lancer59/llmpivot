# llmpivot

**Runtime prompt management for production LLM applications.**

Change a prompt in the web UI — see it reflected in your running app in seconds. No redeployment. No code changes.

---

## What it does

llmpivot sits alongside your FastAPI app. You mount it at a path (e.g. `/prompts`), and it gives you:

- A web UI to create, edit, version, and activate prompts
- An in-memory cache so your app reads prompts at zero latency
- Full version history with one-click rollback
- RBAC authentication (admin / editor / viewer)
- Usage logging with async batched writes
- A real `/healthz` endpoint for load balancers and Kubernetes

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
pip install llmpivot[security]

# With MongoDB backend
pip install llmpivot[mongo]

# Everything
pip install llmpivot[all]
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
    secret_key=os.environ["LLMPIVOT_SECRET"],  # generate: python -c "import secrets; print(secrets.token_hex(32))"
    cookie_secure=True,         # False only for local HTTP dev
    bootstrap_admin=True,
    bootstrap_password=os.environ.get("LLMPIVOT_PASSWORD", "changeme"),
)

app = FastAPI()

# Convert PromptNotFoundError to a clean 404 instead of unhandled 500
@app.exception_handler(PromptNotFoundError)
async def _not_found(request: Request, exc: PromptNotFoundError):
    return JSONResponse(status_code=404, content={"error": "prompt_not_found", "detail": str(exc)})

app.mount("/prompts", manager.mount_ui())

@app.get("/run")
async def run(text: str):
    meta = await aget_prompt_with_meta("my_prompt")
    output = call_your_llm(meta["content"], text)         # your LLM call here
    log_prompt_usage("my_prompt", meta["version_id"], text, output)
    return {"output": output}
```

Run:

```bash
uvicorn example_app:app --reload
```

Open **http://localhost:8000/prompts/list** — log in with `admin` / `changeme`.

---

## Configuration reference

All parameters passed to `PromptManager()`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str` | `"prompts.db"` | SQLite file path |
| `storage_type` | `str` | `"sqlite"` | `"sqlite"` or `"mongodb"` |
| `mongo_uri` | `str` | `None` | MongoDB connection URI |
| `mongo_db_name` | `str` | `"llmpivot"` | MongoDB database name |
| `tenant_id` | `str` | `"default"` | Namespace for multi-tenant isolation |
| `cache_ttl` | `int` | `5` | Seconds before cache re-fetches from DB |
| `auth_mode` | `str` | `"disabled"` | `"disabled"`, `"protected"`, or `"rbac"` |
| `secret_key` | `str` | **required for rbac** | HMAC key for session cookie signing — must be set explicitly when `auth_mode="rbac"`, raises `ValueError` if the default is used |
| `cookie_secure` | `bool` | `True` | Set session cookie with `Secure` flag (HTTPS only). Set `False` for local HTTP dev |
| `protected_mode` | `bool` | `False` | Legacy single-password mode |
| `admin_password` | `str` | `None` | Password for `protected_mode` |
| `bootstrap_admin` | `bool` | `False` | Create `admin` user on first startup if it doesn't exist |
| `bootstrap_password` | `str` | `"admin"` | Password for the bootstrapped admin |
| `log_sample_rate` | `float` | `1.0` | Fraction of calls to log (0.0–1.0). Use `0.1` to log 10% under heavy load |
| `llm_url` | `str` | `None` | OpenAI-compatible endpoint for AI suggestions and A/B testing |
| `llm_api_key` | `str` | `None` | API key for the LLM endpoint |
| `llm_model` | `str` | `"gpt-3.5-turbo"` | Model name sent to the LLM endpoint |

---

## Auth modes

### `"disabled"` (default)
No authentication. Anyone with network access can edit prompts. Only use behind a VPN or on localhost.

### `"protected"`
A single admin password is required for any write operation (activate, edit, delete). No user accounts.

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
| `editor` | Create and edit prompt versions, activate versions, import |
| `viewer` | Read-only: view prompts, version history, diffs, logs, export |

---

## Storage backends

### SQLite (default)
WAL mode is enabled automatically. Handles concurrent reads without locking. Good for single-server and small team deployments.

```python
PromptManager(db_path="prompts.db")
```

### MongoDB
Requires `pip install llmpivot[mongo]`.

```python
PromptManager(
    storage_type="mongodb",
    mongo_uri="mongodb://localhost:27017",
    mongo_db_name="llmpivot_prod",
    tenant_id="acme",
)
```

---

## Multi-tenancy

Every prompt, version, log, and user is scoped to a `tenant_id`. Different tenants are fully isolated.

```python
# Team A
manager_a = PromptManager(db_path="prompts.db", tenant_id="team_a")

# Team B — same DB, completely isolated
manager_b = PromptManager(db_path="prompts.db", tenant_id="team_b")
```

### Migrating an existing database

If you have existing data without `tenant_id` values:

```python
import asyncio
from llmpivot import PromptManager

manager = PromptManager(db_path="prompts.db")
result = asyncio.run(manager.storage.migrate_missing_tenant_ids(tenant_id="default"))
print(result)  # {"prompts": N, "prompt_versions": N, "users": N}
```

---

## Web UI routes

| Route | Auth required | Description |
|---|---|---|
| `/prompts/list` | viewer | All prompts, active versions, last editor |
| `/prompts/edit/__new__` | editor | Create a new prompt |
| `/prompts/edit/{name}` | editor | Add a new version to an existing prompt |
| `/prompts/detail/{name}` | viewer | Version history, activate/rollback |
| `/prompts/diff/{name}` | viewer | Side-by-side diff between any two versions |
| `/prompts/test/{name}` | viewer | A/B test two versions with live LLM calls |
| `/prompts/logs` | viewer | Usage log viewer |
| `/prompts/import` | editor | Bulk import from JSON file |
| `/prompts/export` | viewer | Download active prompts as JSON |
| `/prompts/users` | admin | Create users, assign roles |
| `/prompts/login` | — | Login page |
| `/prompts/logout` | — | Clears session cookie |
| `/prompts/healthz` | — | Liveness probe (DB + worker health) |

---

## Health check

`/prompts/healthz` performs real liveness checks — not just a static `{"status":"ok"}`.

```json
{
  "status": "ok",
  "cache_size": 12,
  "log_queue_depth": 0,
  "cache_worker_alive": true,
  "logger_worker_alive": true
}
```

Returns `503` with `"status": "degraded"` and an `"issues"` array if the database is unreachable or a background worker has died. Wire this to your load balancer or k8s liveness probe.

---

## Python API

### `aget_prompt(name) -> str`
Returns the active prompt content. Preferred in async routes.

```python
from llmpivot import aget_prompt
prompt = await aget_prompt("my_prompt")
```

### `aget_prompt_with_meta(name) -> dict`
Returns `{"content": str, "version_id": int}`. Use this when logging — it gives you the exact version that served the request.

```python
from llmpivot import aget_prompt_with_meta
meta = await aget_prompt_with_meta("my_prompt")
# meta["content"]    — the prompt text
# meta["version_id"] — tracks which version produced this output
```

### `get_prompt(name) -> str`
Sync wrapper. Use in plain scripts or non-async contexts.

```python
from llmpivot import get_prompt
prompt = get_prompt("my_prompt")
```

### `log_prompt_usage(name, version_id, input_text, output_text)`
Fire-and-forget usage logging. Non-blocking — items are queued in memory and flushed to DB in background batches. Safe to call from any route.

```python
from llmpivot import log_prompt_usage
log_prompt_usage("my_prompt", meta["version_id"], input_text=user_query, output_text=llm_response)
```

### `PromptNotFoundError`
Raised by `aget_prompt` and `aget_prompt_with_meta` when no active version exists. Register a handler so it becomes a 404 instead of a 500:

```python
from llmpivot import PromptNotFoundError
from fastapi.responses import JSONResponse

@app.exception_handler(PromptNotFoundError)
async def _handler(request, exc):
    return JSONResponse(status_code=404, content={"error": str(exc)})
```

---

## Security notes

- **`secret_key`**: Must be set explicitly when `auth_mode="rbac"`. Using the default raises a `ValueError` at startup. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"` and store it in your secrets manager or environment variable.
- **`cookie_secure`**: Defaults to `True` — the session cookie is HTTPS-only. Set `False` only for local HTTP development.
- **CSRF protection**: All state-mutating POST requests (edit, activate, delete, import, create user) are CSRF-protected. Tokens are session-bound and validated server-side.
- **Login rate limiting**: Brute-force protection is built in — 10 failed attempts per IP per 5 minutes triggers a 15-minute lockout.
- **Password hashing**: Uses Argon2id when `argon2-cffi` is installed (recommended), bcrypt if installed, and falls back to PBKDF2-SHA256 (stdlib). All three formats verify correctly side-by-side so you can migrate without breaking existing accounts.
- **Audit log**: Every create, activate, delete, import, and user-create action is recorded in the `audit_log` table with timestamp and actor.

---

## Production checklist

- [ ] `secret_key` loaded from environment variable or secrets manager
- [ ] `cookie_secure=True` (default) — app served over HTTPS
- [ ] `bootstrap_password` changed after first login
- [ ] `argon2-cffi` installed: `pip install argon2-cffi`
- [ ] `auth_mode="rbac"` — not `"disabled"`
- [ ] `/prompts/healthz` wired to load balancer / k8s liveness probe
- [ ] `cache_ttl` tuned for your update frequency (5s is a safe default)
- [ ] `log_sample_rate` reduced if log volume is high (e.g. `0.1` = 10%)
- [ ] For multi-worker deployments: keep `cache_ttl` low (≤5s) until Redis invalidation is added

---

## License

[MIT](LICENSE) © Sanath Goutham
