# llmpivot — Architecture

This document explains how llmpivot is structured internally, how data flows through the system, and why key design decisions were made.

---

## 1. High-level picture

```
Your FastAPI App
│
├── GET /run  ──────────────────────────────────────────────────────────┐
│                                                                        │
│   aget_prompt_with_meta("my_prompt")                                  │
│        │                                                               │
│        ▼                                                               │
│   PromptManager.get_with_meta()                                       │
│        │                                                               │
│        ▼                                                               │
│   PromptCache._store  ──── hit ──► return {"content", "version_id"}  │
│        │                                                               │
│       miss                                                             │
│        │                                                               │
│        ▼                                                               │
│   Storage.fetch_active_version()  ◄── SQLite / MongoDB                │
│        │                                                               │
│        ▼                                                               │
│   Cache updated, value returned ──────────────────────────────────────┘
│
└── app.mount("/prompts", manager.mount_ui())
         │
         ├── /prompts/list, /edit, /detail, /diff, /test, /logs ...  (UI routes)
         ├── /prompts/login, /logout, /users                          (auth routes)
         ├── /prompts/import, /export                                  (bulk routes)
         ├── /prompts/api/suggest, /api/run                            (LLM routes)
         └── /prompts/healthz                                          (liveness probe)
```

---

## 2. Module map

```
llmpivot/
├── __init__.py        Public API surface: PromptManager, get_prompt, aget_prompt,
│                      aget_prompt_with_meta, log_prompt_usage, PromptNotFoundError
│
├── manager.py         Central coordinator. Owns the singleton. Wires storage,
│                      cache, logger, and LLM client together. Exposes mount_ui(),
│                      get(), get_with_meta(), get_sync(), log_usage(), _shutdown().
│
├── storage.py         Data persistence layer.
│                      BaseStorage  — abstract interface (ABC)
│                      SQLiteStorage — WAL-mode SQLite, aiosqlite or thread-pool fallback
│                      MongoStorage  — Motor async MongoDB driver
│
├── cache.py           PromptCache — in-memory dict with TTL.
│                      Background asyncio.Task refreshes all keys every ttl seconds.
│                      Explicit invalidate() called by routes on every write.
│                      Serves stale values if DB is unreachable (fail-safe).
│
├── logger.py          PromptLogger — async queue-buffered usage logger.
│                      asyncio.Queue(maxsize=10_000) — bounded to prevent OOM.
│                      Background worker drains in batches of 50 every 2 seconds.
│                      drain() method for graceful shutdown.
│
├── llm.py             LLMClient — calls any OpenAI-compatible /v1/chat/completions.
│                      3-retry exponential backoff on 5xx / 429 / timeout / connect.
│                      API key never exposed in error messages.
│
├── auth.py            Password hashing: Argon2id → bcrypt → PBKDF2 (auto-selected).
│                      HMAC-SHA256 session tokens (no JWT library dependency).
│                      CSRF token generation and validation.
│                      RBAC permission checks.
│
├── db.py              DEPRECATED backward-compat shim. Emits DeprecationWarning.
│                      All calls default to tenant_id="default". Use manager.storage.
│
└── ui/
    ├── __init__.py    Exports build_router.
    ├── routes.py      All HTTP handlers. Closure over PromptManager instance.
    │                  CSRF validation on every POST.
    │                  Per-IP login rate limiting (10 attempts / 5 min window).
    │                  Input size limits: 500 KB content, 10 KB log text, 5 MB import.
    │                  Audit log written on every admin action.
    ├── templates.py   Server-rendered HTML (pure Python f-strings, no Jinja2).
    │                  _csrf_field() embeds hidden CSRF token in every form.
    │                  _escape() wraps all user-supplied values to prevent XSS.
    └── helpers.py     escape() and render_user_badge() utilities.
```

---

## 3. Startup sequence

When your app starts and the first request hits the ASGI lifespan:

```
PromptManager.__init__()
  1. Resolve auth_mode (promote protected_mode → "protected" if needed)
  2. Guard: raise ValueError if default secret_key used with auth_mode="rbac"
  3. Pick storage backend (SQLiteStorage or MongoStorage)
  4. storage.init_db_sync() — create tables, run auto-migrations, enable WAL
  5. Create PromptCache(storage, ttl, tenant_id)
  6. Create PromptLogger(storage, sample_rate, tenant_id)
  7. Optionally create LLMClient(url, api_key, model)
  8. Register self as global _instance singleton

mount_ui() lifespan startup:
  9.  _bootstrap_admin() — create admin user if bootstrap_admin=True and no admin exists
  10. cache.start()       — launch background cache refresh asyncio.Task
  11. usage_logger.start() — launch background batch writer asyncio.Task
  12. _warm_cache()       — pre-fetch all active prompts into cache before serving traffic

mount_ui() lifespan shutdown:
  13. _shutdown():
        usage_logger.drain() — flush queue to DB (up to 10s timeout)
        usage_logger.stop()  — cancel worker task
        cache.stop()         — cancel refresh task
```

---

## 4. Hot path — prompt retrieval

Every call to `aget_prompt_with_meta("name")` follows this path:

```
__init__.py: aget_prompt_with_meta(name)
  └── manager.py: get_instance().get_with_meta(name)
        └── cache.py: PromptCache.get(name)
              ├── _store[name] exists and not stale? → return immediately (no I/O)
              └── stale or missing:
                    storage.fetch_active_version(name, tenant_id)
                      ├── aiosqlite path: async SQL JOIN (prompts + prompt_versions)
                      └── to_thread fallback: same SQL via thread pool
                    _store[name] = {content, version_id, fetched_at}
                    return entry
```

**Cache miss** hits the DB exactly once per prompt per TTL period, regardless of concurrent requests (no thundering herd — the first call populates, others read from `_store` immediately after).

**DB unreachable**: `_fetch_and_store` catches exceptions and logs a WARNING. The stale value in `_store` is returned unchanged — the application keeps serving the last known good prompt.

---

## 5. Write path — prompt update via UI

```
POST /prompts/edit/{name}
  1. _check_auth(request, min_role="editor") — RBAC redirect if unauthorized
  2. _validate_csrf(request, csrf_token)     — 403 if token missing or wrong
  3. len(content.encode()) > 500 KB?         — 400 if oversized
  4. storage.create_version(name, content, editor, tag, set_active, tenant_id)
       ├── INSERT OR IGNORE INTO prompts
       ├── SELECT MAX(version_number) + 1
       ├── UPDATE prompt_versions SET is_active=0 WHERE prompt_id=?   (if set_active)
       └── INSERT INTO prompt_versions (...)
  5. storage.insert_audit_log(action="version_created", ...)
  6. cache.invalidate(name)   ← removes key from _store, next get() re-fetches
  7. RedirectResponse to /detail/{name}
```

---

## 6. Usage logging path

```
log_prompt_usage("name", version_id, input_text, output_text)
  └── manager.log_usage(...)
        └── logger.log(...)
              ├── sample_rate check (random.random() > rate → skip)
              ├── asyncio.get_running_loop() found?
              │     queue.put_nowait(item)  ← non-blocking
              │     QueueFull?  → WARNING logged, item dropped
              │     logger.start() (idempotent)
              └── no running loop:
                    asyncio.new_event_loop().run_until_complete(insert_logs_batch([item]))

Background _batch_worker:
  loop:
    wait up to flush_interval (2s) for first item
    drain up to batch_size (50) more items
    storage.insert_logs_batch(batch)  ← single DB write per batch
```

---

## 7. Authentication flow

```
POST /prompts/login
  ├── _get_client_ip(request)           — X-Forwarded-For aware
  ├── _is_rate_limited(ip)              — 10 failures / 5 min → 15 min lockout
  ├── storage.get_user(username, tenant_id)
  ├── verify_password(submitted, stored_hash)
  │     ├── "argon2$..." → argon2-cffi verify
  │     ├── "bcrypt$..." → bcrypt checkpw
  │     ├── "pbkdf2$..." → PBKDF2 260k iterations
  │     └── legacy "{salt}${hex}" → PBKDF2 100k iterations (backward compat)
  ├── create_token({id, username, role}, secret_key, expires_in=86400)
  │     payload → base64url(JSON) + "." + HMAC-SHA256(payload, secret_key)
  └── set_cookie("llmpivot_session", token, httponly=True, samesite="lax",
                  secure=manager.cookie_secure, max_age=86400)

Every subsequent protected request:
  get_current_user_from_request(request, secret_key)
    ├── read cookie "llmpivot_session" (or Authorization: Bearer header)
    ├── verify_token → HMAC verify + exp check
    └── return user dict or None → redirect to /login
```

---

## 8. CSRF protection

CSRF tokens are session-bound — derived from the session token itself:

```
generate_csrf_token(session_token, secret_key):
    raw = (session_token + ":csrf").encode()
    return base64url(HMAC-SHA256(secret_key, raw))
```

On every GET that renders a form, the server computes this token and embeds it as a hidden field. On every POST, the server re-derives it from the current session and compares with constant-time `hmac.compare_digest`. No separate token store needed — the derivation is stateless.

---

## 9. Database schema (SQLite)

```sql
prompts (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    tenant_id  TEXT DEFAULT 'default',
    is_deleted INTEGER DEFAULT 0,        -- soft delete flag
    UNIQUE(tenant_id, name)
)

prompt_versions (
    id             INTEGER PRIMARY KEY,
    prompt_id      INTEGER REFERENCES prompts(id),
    tenant_id      TEXT DEFAULT 'default',
    content        TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by     TEXT,
    tag            TEXT CHECK(tag IN ('prod','staging','experiment')),
    is_active      INTEGER DEFAULT 0
)

prompt_logs (
    id         INTEGER PRIMARY KEY,
    prompt_id  INTEGER REFERENCES prompts(id),
    version_id INTEGER REFERENCES prompt_versions(id),
    tenant_id  TEXT DEFAULT 'default',
    input      TEXT,   -- truncated to 10 KB
    output     TEXT,   -- truncated to 10 KB
    timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
)

users (
    id            INTEGER PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    email         TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT DEFAULT 'editor',
    tenant_id     TEXT DEFAULT 'default',
    is_active     INTEGER DEFAULT 1,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
)

audit_log (
    id           INTEGER PRIMARY KEY,
    action       TEXT NOT NULL,    -- version_created | version_activated | prompt_deleted
    performed_by TEXT NOT NULL,    --                    prompts_imported | user_created
    prompt_name  TEXT,
    version_id   TEXT,
    detail       TEXT,
    tenant_id    TEXT DEFAULT 'default',
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
)
```

SQLite is opened with `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=10000` on every connection, enabling concurrent reads without blocking writes.

---

## 10. Key design decisions

**No JWT library** — auth uses stdlib `hmac`, `hashlib`, `base64`, `json`. Zero extra dependencies for the auth core. The token format is `base64url(json_payload).HMAC-SHA256(sig)`.

**No template engine** — all HTML is pure Python f-strings in `templates.py`. No Jinja2, no build step. All user values go through `html.escape()` before rendering.

**Pluggable password hashing** — the library works out of the box with PBKDF2 (stdlib), upgrades silently to Argon2id or bcrypt when available, and reads all three formats for backward compatibility. No migration required when you add argon2-cffi.

**In-process rate limiting** — login rate limiting uses module-level dicts in `routes.py`. Fast and zero-dependency, but per-process. In a multi-worker deployment, use Redis-backed rate limiting instead.

**Singleton PromptManager** — `_instance` in `manager.py` enables the module-level `get_prompt()` / `aget_prompt()` helpers without requiring callers to pass the manager around. Only one instance should exist per process.

**Soft deletes** — `soft_delete_prompt` sets `is_deleted=1` on the prompt row and `is_active=0` on all versions. The data is never destroyed. The prompt disappears from all list and fetch queries. A new `create_version` call on the same name restores it.

**Bounded log queue** — `asyncio.Queue(maxsize=10_000)` prevents unbounded memory growth under a write storm. Items beyond the limit are dropped with a WARNING log rather than blocking the caller or crashing the process.

**Cache pre-warming** — on startup, `_warm_cache()` fetches all active prompts before the app starts serving traffic. This prevents a cold-start DB spike on the first request after a restart.

---

## 11. What is NOT in scope (known gaps)

| Gap | Notes |
|-----|-------|
| Cross-worker cache invalidation | Each worker process has its own `_store`. Keep `cache_ttl` ≤ 5s, or add Redis pub/sub invalidation |
| PostgreSQL / MySQL | Only SQLite and MongoDB backends exist. SQLAlchemy async would be the right addition |
| Prompt templating | `{{variable}}` substitution not yet implemented |
| Analytics dashboard | `prompt_logs` table exists but has no aggregation UI |
| Webhook on version change | No callback when a prompt goes live |
| CLI tool | No `llmpivot` command-line interface yet |
