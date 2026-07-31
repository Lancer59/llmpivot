# LLMPivot
## Production-Grade Runtime Prompt Control, Versioning & Multi-Tenant Platform

Open-source, high-concurrency prompt management system for production LLM & AI applications.  
Change a prompt in the web UI and see it reflected in your running app instantly — no redeployments needed.

---

## Key Features

- **Runtime Control**: Dynamic prompt iteration with instant in-memory caching.
- **Multi-User Concurrency & Multi-Tenancy**: Built for high-traffic apps with SQLite WAL mode, async queue batch logging, and workspace isolation (`tenant_id`).
- **Authentication & RBAC**: Optional admin bootstrap with explicit credentials, session JWT cookies, PBKDF2 password security, and Role-Based Access Control (`admin`, `editor`, `viewer`).
- **Pluggable Storage Engines**: Support for file-based **SQLite** out-of-the-box and **MongoDB** NoSQL database backends.
- **AI Prompt Suggestions & A/B Testing**: Integrated OpenAI-compatible AI prompt improver and side-by-side version comparison.
- **Fail-Safe Resilience**: Stale-cache serving if the database goes down — your application never crashes.

---

## Installation

Standard installation (SQLite included):
```bash
pip install llmpivot
```

With MongoDB support:
```bash
pip install llmpivot[mongo]
```

Full installation with all extras:
```bash
pip install llmpivot[all]
```

---

## Quick Start

### 1. Minimal Setup (FastAPI + SQLite)

```python
from fastapi import FastAPI
from llmpivot import PromptManager, aget_prompt_with_meta, log_prompt_usage

# Initialize once at app startup
manager = PromptManager(
    db_path="prompts.db",
    cache_ttl=5,
)

app = FastAPI()

# Mount the Web UI
app.mount("/prompts", manager.mount_ui())

# Use active prompts in your API endpoints
@app.get("/run")
async def run(text: str = "hello"):
    meta = await aget_prompt_with_meta("summary_prompt")

    # Call your LLM model using meta["content"]
    output = f"[LLM output for input '{text}']"

    # Async, non-blocking usage logging
    log_prompt_usage("summary_prompt", meta["version_id"], input_text=text, output_text=output)

    return {"output": output}
```

Visit `http://localhost:8000/prompts/list` to view and edit active prompts.

---

## Storage Options (SQL & MongoDB)

### SQLite (Default)
Enables **Write-Ahead Logging (WAL mode)** automatically for high-concurrency web requests without database locking:
```python
manager = PromptManager(
    storage_type="sqlite",
    db_path="prompts.db",
)
```

### MongoDB (NoSQL)
Pass your MongoDB connection string and database name:
```python
manager = PromptManager(
    storage_type="mongodb",
    mongo_uri="mongodb://localhost:27017",
    mongo_db_name="llmpivot_production",
    tenant_id="acme_corp",
)
```

---

## Authentication & User Management (RBAC)

Enable multi-user authentication with Role-Based Access Control:

```python
manager = PromptManager(
    db_path="prompts.db",
    auth_mode="rbac",
    secret_key="your-secure-secret-key-here",
)
```

### Admin Bootstrap
When authentication is enabled, you can optionally bootstrap an initial super-admin user explicitly:

```python
manager = PromptManager(
    db_path="prompts.db",
    auth_mode="rbac",
    secret_key="your-secure-secret-key-here",
    bootstrap_admin=True,
    bootstrap_password="choose-a-strong-password",
)
```

What these values mean:
- `secret_key`: a long random string used to sign login sessions. Think of it as the private key for your app's auth cookies.
- `bootstrap_admin`: whether to create an initial admin account automatically on first startup.
- `bootstrap_password`: the password for that initial admin account.
- Username: the initial admin username is always `admin`.

If you do not enable `bootstrap_admin`, no default admin account is created.

### Authentication Flow
1. Navigating to any `/prompts` route redirects unauthenticated users directly to `/prompts/login`.
2. Log in with the admin account you created during bootstrap.
3. Once logged in as `admin`, an **"Users"** button appears in the top navigation bar.
4. Click **Users** (`/prompts/users`) to create new team members and assign roles (`admin`, `editor`, `viewer`).

### Roles & Permissions Hierarchy

| Role | Permissions |
|---|---|
| 👑 `Admin` | Full access: User Management (`/prompts/users`), prompt deletion, import/export, editing, tag management. |
| ✍️ `Editor` | Create prompt versions, edit content, test prompts, set active versions, import prompts. |
| 👁️ `Viewer` | Read-only access to prompts, version history, diffs, export JSON, and usage logs. |

### Upgrade note for existing installations
If you are upgrading from an older version and your database already contains rows with missing or empty `tenant_id` values, the stricter tenant isolation rules may hide those rows until they are assigned a tenant.

To avoid surprises, run the migration helper once after upgrading:

```python
from llmpivot import PromptManager

manager = PromptManager(db_path="prompts.db")
print(await manager.storage.migrate_missing_tenant_ids(tenant_id="default"))
```

This assigns a default tenant to legacy rows so they remain accessible after the upgrade. For production deployments, replace `"default"` with your intended tenant name.

---

## API Reference

### `aget_prompt(name: str) -> str`
Async helper returning the active prompt version content.

```python
from llmpivot import aget_prompt

prompt = await aget_prompt("summary_prompt")
```

### `aget_prompt_with_meta(name: str) -> dict`
Async helper returning content and version ID together. Recommended for accurate usage logging.

```python
from llmpivot import aget_prompt_with_meta

meta = await aget_prompt_with_meta("summary_prompt")
# Returns: {"content": "...", "version_id": 4}
```

### `get_prompt(name: str) -> str`
Sync convenience wrapper for plain scripts outside an event loop.

```python
from llmpivot import get_prompt

prompt = get_prompt("summary_prompt")
```

### `log_prompt_usage(name: str, version_id: int | str, input_text: str, output_text: str)`
Enqueue usage logs to an in-memory queue. Non-blocking and fire-and-forget — flushed in background batches to prevent database bottlenecks.

```python
from llmpivot import log_prompt_usage

log_prompt_usage("summary_prompt", meta["version_id"], input_text=user_input, output_text=llm_output)
```

---

## Web UI Overview

| Route | Description | Navigation Button |
|---|---|---|
| `/prompts/list` | All prompts, active versions, last editors, and timestamps. | **Prompts** |
| `/prompts/edit/__new__` | Create a new prompt. | **+ New Prompt** |
| `/prompts/import` | Upload JSON file to bulk import prompt versions. | **Import** |
| `/prompts/export` | Download active prompts as a JSON file. | **Export** |
| `/prompts/logs` | High-concurrency usage log viewer with prompt filtering. | **Logs** |
| `/prompts/users` | Admin user management and role assignment dashboard. | **Users** *(Admin Only)* |
| `/prompts/login` | User login screen. | - |
| `/prompts/detail/{name}` | Complete version history, activation control, and rollback. | - |
| `/prompts/edit/{name}` | Edit prompt, create new version, AI suggestions. | - |
| `/prompts/diff/{name}` | Side-by-side line diff between any two versions. | - |
| `/prompts/test/{name}` | A/B test prompt versions side-by-side. | - |

---

## Configuration Options

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str` | `"prompts.db"` | SQLite database file path |
| `storage_type` | `str` | `"sqlite"` | Database engine: `"sqlite"` or `"mongodb"` |
| `mongo_uri` | `str` | `None` | MongoDB connection URI (e.g. `mongodb://localhost:27017`) |
| `mongo_db_name` | `str` | `"llmpivot"` | MongoDB database name |
| `tenant_id` | `str` | `"default"` | Organization or workspace namespace isolation |
| `cache_ttl` | `int` | `5` | In-memory cache TTL in seconds |
| `auth_mode` | `str` | `"disabled"` | Authentication mode: `"disabled"`, `"protected"`, or `"rbac"` |
| `secret_key` | `str` | internal default | HMAC secret key for signing JWT session cookies |
| `protected_mode` | `bool` | `False` | Legacy password protection mode |
| `admin_password` | `str` | `None` | Required password if `protected_mode=True` |
| `log_sample_rate` | `float` | `1.0` | Sampling rate for log storage (0.0 to 1.0) |
| `llm_url` | `str` | `None` | OpenAI-compatible endpoint for AI suggestions |
| `llm_api_key` | `str` | `None` | LLM API Key |
| `llm_model` | `str` | `"gpt-3.5-turbo"` | LLM model name |

---

## License

[MIT License](LICENSE) © Sanath Goutham
