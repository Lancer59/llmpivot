# llmpivot — Usage Guide

Step-by-step instructions for every scenario, from local dev to production deployment.

---

## Table of contents

1. [Installation](#1-installation)
2. [Running the example app](#2-running-the-example-app)
3. [Creating your first prompt](#3-creating-your-first-prompt)
4. [Using prompts in your application](#4-using-prompts-in-your-application)
5. [Editing and versioning prompts](#5-editing-and-versioning-prompts)
6. [User management](#6-user-management)
7. [Multi-tenancy](#7-multi-tenancy)
8. [MongoDB backend](#8-mongodb-backend)
9. [LLM suggestions and A/B testing](#9-llm-suggestions-and-ab-testing)
10. [Import and export](#10-import-and-export)
11. [Usage logging](#11-usage-logging)
12. [Audit log](#12-audit-log)
13. [Health checks](#13-health-checks)
14. [Production deployment](#14-production-deployment)
15. [Upgrading from an older version](#15-upgrading-from-an-older-version)

---

## 1. Installation

```bash
pip install llmpivot

# Recommended: add strong password hashing
pip install argon2-cffi

# Or install everything at once
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

```bash
uvicorn example_app:app --reload
```

Open **http://localhost:8000/prompts/list**

Default login: `admin` / `changeme`

> Change the password immediately — go to `/prompts/users`, delete the default admin, and create a new one with a strong password. Or set `LLMPIVOT_PASSWORD` before first run.

Set a stable secret key so sessions survive restarts:

```bash
# Windows
set LLMPIVOT_SECRET=<output of: python -c "import secrets; print(secrets.token_hex(32))">
uvicorn example_app:app --reload

# Linux / macOS
export LLMPIVOT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
uvicorn example_app:app --reload
```

---

## 3. Creating your first prompt

**Via the UI:**

1. Go to `/prompts/edit/__new__`
2. Enter a prompt name (e.g. `summarize_prompt`) — lowercase, underscores, no spaces
3. Enter the prompt content
4. Set tag to `prod`, set active to `Yes`
5. Click **Save New Version**

**Via code (programmatic seed):**

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

### Async routes (FastAPI, recommended)

```python
from llmpivot import aget_prompt_with_meta, log_prompt_usage, PromptNotFoundError
from fastapi.responses import JSONResponse

@app.get("/chat")
async def chat(user_input: str):
    meta = await aget_prompt_with_meta("chat_system_prompt")

    response = await your_llm_client.complete(
        system=meta["content"],
        user=user_input,
    )

    log_prompt_usage(
        "chat_system_prompt",
        meta["version_id"],
        input_text=user_input,
        output_text=response,
    )
    return {"response": response}
```

### When you only need the content (no logging)

```python
from llmpivot import aget_prompt

prompt = await aget_prompt("summarize_prompt")
```

### Sync context (plain scripts, background jobs)

```python
from llmpivot import get_prompt

prompt = get_prompt("summarize_prompt")
```

> `get_prompt` reads from the in-memory cache if already populated, or runs the async fetch synchronously. It works outside any event loop.

### Handling missing prompts

Always register this handler — without it a missing prompt returns an unhandled 500:

```python
from llmpivot import PromptNotFoundError
from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(PromptNotFoundError)
async def _handler(request: Request, exc: PromptNotFoundError):
    return JSONResponse(status_code=404, content={"error": str(exc)})
```

---

## 5. Editing and versioning prompts

Every save creates a **new version** — nothing is overwritten.

**Via UI:**
1. Go to `/prompts/edit/{name}` or click **Edit** on the prompt list
2. Modify the content
3. Choose a tag (`prod`, `staging`, `experiment`) and whether to set it active
4. Click **Save New Version**

**Activating a previous version (rollback):**
1. Go to `/prompts/detail/{name}`
2. Find the version you want
3. Click **Make Active** — the cache is invalidated immediately

**Comparing versions:**
1. Go to `/prompts/diff/{name}`
2. Select Version A and Version B
3. Click **Compare** — a unified diff is shown

**Cache behavior after an edit:**  
The cache key is invalidated immediately when a version is saved or activated. The next call to `aget_prompt` fetches from the DB. No TTL wait needed.

---

## 6. User management

Only available when `auth_mode="rbac"`.

**Create a user:**
1. Log in as admin
2. Go to `/prompts/users`
3. Fill in username, email, password, and role
4. Click **Create User**

**Roles:**

| Role | Can do |
|---|---|
| `admin` | Everything: manage users, delete prompts, import/export, edit, activate |
| `editor` | Create versions, edit content, activate versions, import prompts |
| `viewer` | View prompts, versions, diffs, logs, export JSON — read only |

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

---

## 7. Multi-tenancy

Each `tenant_id` is a fully isolated namespace — prompts, versions, logs, and users from one tenant are never visible to another.

```python
# Tenant A
manager_a = PromptManager(db_path="prompts.db", tenant_id="org_acme", auth_mode="disabled")

# Tenant B — same DB file, completely separate data
manager_b = PromptManager(db_path="prompts.db", tenant_id="org_globex", auth_mode="disabled")
```

All storage calls are automatically scoped. You never need to pass `tenant_id` to `aget_prompt` — it is resolved from the `PromptManager` instance.

**Multiple managers in one process:**  
Only the last `PromptManager(...)` call sets the global `_instance`. If you need multiple tenants served by the same process, use `manager.get()` / `manager.get_with_meta()` directly instead of the module-level helpers:

```python
meta_a = await manager_a.get_with_meta("prompt_name")
meta_b = await manager_b.get_with_meta("prompt_name")
```

---

## 8. MongoDB backend

```bash
pip install "llmpivot[mongo]"
```

```python
manager = PromptManager(
    storage_type="mongodb",
    mongo_uri="mongodb://localhost:27017",
    mongo_db_name="llmpivot_prod",
    tenant_id="acme",
    auth_mode="rbac",
    secret_key=os.environ["LLMPIVOT_SECRET"],
)
```

Everything else is identical. The storage backend is transparent to your application code.

**Recommended indexes** (run once on your MongoDB instance):

```javascript
db.prompts.createIndex({ tenant_id: 1, name: 1 }, { unique: true })
db.prompt_versions.createIndex({ prompt_name: 1, tenant_id: 1, is_active: 1 })
db.prompt_versions.createIndex({ prompt_name: 1, tenant_id: 1, version_number: -1 })
db.prompt_logs.createIndex({ prompt_name: 1, tenant_id: 1, timestamp: -1 })
db.audit_log.createIndex({ tenant_id: 1, created_at: -1 })
```

---

## 9. LLM suggestions and A/B testing

Requires an OpenAI-compatible endpoint.

```python
manager = PromptManager(
    db_path="prompts.db",
    llm_url="https://api.openai.com/v1/chat/completions",
    llm_api_key=os.environ["OPENAI_API_KEY"],
    llm_model="gpt-4o",
)
```

**AI suggestion:**  
On the edit page, a **✨ Get AI Suggestion** button appears. It sends the current content to the LLM and replaces the textarea with the improved version. You still save manually.

**A/B testing:**
1. Go to `/prompts/test/{name}`
2. Select Version A and Version B
3. Enter test input text
4. Click **⚡ Run A/B Comparison**
5. Both outputs appear side by side

The LLM client retries up to 3 times with exponential backoff on 5xx, 429, timeout, and connection errors. Non-retryable 4xx errors (bad API key, bad request) fail immediately.

---

## 10. Import and export

**Export all active prompts:**

Via UI: click **Export** in the nav bar → downloads `prompts.json`

Via API:
```bash
curl -b "llmpivot_session=<token>" http://localhost:8000/prompts/export -o prompts.json
```

Format:
```json
{
  "summarize_prompt": "Summarize in 3 bullet points: ...",
  "chat_system_prompt": "You are a helpful assistant..."
}
```

**Import:**

Via UI: click **Import** → upload a `.json` file in the above format

- Existing prompts get a new version (content updated, old versions preserved)
- New prompts are created
- The result page shows created vs updated counts

Via code:
```python
with open("prompts.json") as f:
    data = json.load(f)

result = await manager.storage.import_prompts(
    data,
    imported_by="deploy-script",
    tenant_id="default",
)
print(result)  # {"created": [...], "updated": [...]}

# Invalidate cache for all imported names
for name in result["created"] + result["updated"]:
    manager.cache.invalidate(name)
```

---

## 11. Usage logging

Usage logs record every prompt call with input, output, version, and timestamp.

```python
log_prompt_usage(
    "summarize_prompt",
    meta["version_id"],   # always use version_id from aget_prompt_with_meta
    input_text=user_query,
    output_text=llm_response,
)
```

- **Non-blocking** — items are queued in memory (`asyncio.Queue`), never blocks the request
- **Batched** — flushed to DB in groups of 50 every 2 seconds
- **Bounded** — queue capped at 10,000 items; drops with WARNING if exceeded
- **Sampled** — set `log_sample_rate=0.1` to log 10% of calls under heavy load

View logs at `/prompts/logs`. Filter by prompt name with the `?prompt=name` query param.

**Logs are truncated** at 10 KB per field (input and output) to prevent unbounded DB growth.

---

## 12. Audit log

Every admin action is recorded automatically:

| Action | Trigger |
|---|---|
| `version_created` | Prompt saved (new or edit) |
| `version_activated` | Version made active |
| `prompt_deleted` | Soft delete |
| `prompts_imported` | Bulk import |
| `user_created` | New user added |

Query programmatically:

```python
logs = await manager.storage.fetch_audit_logs(limit=100, tenant_id="default")
for entry in logs:
    print(entry["created_at"], entry["action"], entry["performed_by"], entry["prompt_name"])
```

---

## 13. Health checks

`/prompts/healthz` performs real checks — not just a static response.

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

Returns `200` when healthy, `503` when degraded (DB unreachable or a background worker has died), with an `"issues"` array explaining what failed.

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

## 14. Production deployment

### Minimal production setup

```python
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from llmpivot import PromptManager, PromptNotFoundError, aget_prompt_with_meta, log_prompt_usage

manager = PromptManager(
    db_path=os.environ.get("LLMPIVOT_DB", "/data/prompts.db"),
    cache_ttl=5,
    auth_mode="rbac",
    secret_key=os.environ["LLMPIVOT_SECRET"],   # required — never use default
    cookie_secure=True,                          # HTTPS only
    bootstrap_admin=True,
    bootstrap_password=os.environ["LLMPIVOT_PASSWORD"],
    tenant_id=os.environ.get("LLMPIVOT_TENANT", "default"),
    log_sample_rate=float(os.environ.get("LLMPIVOT_LOG_RATE", "1.0")),
)

app = FastAPI()

@app.exception_handler(PromptNotFoundError)
async def _not_found(request: Request, exc: PromptNotFoundError):
    return JSONResponse(status_code=404, content={"error": str(exc)})

app.mount("/prompts", manager.mount_ui())
```

### Running with multiple workers

```bash
uvicorn myapp:app --workers 4 --host 0.0.0.0 --port 8000
```

> Each worker has its own in-memory cache. With `cache_ttl=5`, stale prompts resolve within 5 seconds across all workers. Keep TTL low (≤5s) until Redis-backed cross-worker invalidation is added.

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV LLMPIVOT_SECRET=""
ENV LLMPIVOT_DB="/data/prompts.db"
ENV LLMPIVOT_PASSWORD="changeme"
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "example_app:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t myapp .
docker run -p 8000:8000 \
  -e LLMPIVOT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))") \
  -e LLMPIVOT_PASSWORD=strongpassword \
  -v $(pwd)/data:/data \
  myapp
```

### Production checklist

- [ ] `LLMPIVOT_SECRET` set to a 64-char random hex string — never the default
- [ ] `cookie_secure=True` — app served over HTTPS
- [ ] `bootstrap_password` is strong and changed after first login
- [ ] `argon2-cffi` installed: `pip install argon2-cffi`
- [ ] `auth_mode="rbac"` — not `"disabled"`
- [ ] `/prompts/healthz` wired to load balancer / k8s liveness probe
- [ ] `cache_ttl=5` or lower for multi-worker deployments
- [ ] `log_sample_rate` reduced if log volume is very high (e.g. `0.1`)
- [ ] SQLite DB file stored on a persistent volume (not ephemeral container storage)
- [ ] Backups of `prompts.db` scheduled (it contains all your prompt history)

---

## 15. Upgrading from an older version

### Database migration

New columns (`is_deleted` on `prompts`, `audit_log` table) are added automatically on first startup via `init_db_sync()`. No manual migration needed for new installs.

If you have existing data with missing `tenant_id` values, run this once after upgrading:

```python
import asyncio
from llmpivot import PromptManager

manager = PromptManager(db_path="prompts.db")
result = asyncio.run(manager.storage.migrate_missing_tenant_ids(tenant_id="default"))
print(result)  # {"prompts": N, "prompt_versions": N, "users": N}
```

### Password hash migration

Existing PBKDF2 hashes (from older versions) continue to work — `verify_password` detects the format automatically. No re-hashing of existing accounts is needed. New passwords will use Argon2id automatically once `argon2-cffi` is installed.

### `secret_key` now required for `auth_mode="rbac"`

In older versions, omitting `secret_key` silently used a public default. Now it raises a `ValueError`. Set `secret_key=os.environ["LLMPIVOT_SECRET"]` explicitly.

### `db.py` shim deprecated

If you were importing from `llmpivot.db` directly, you'll see a `DeprecationWarning`. Those functions still work but all default to `tenant_id="default"`. Migrate to `manager.storage` methods for tenant-aware operations.
