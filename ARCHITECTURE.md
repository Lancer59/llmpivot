# llmpivot — Architecture

How the system is structured, how data flows through it, and why key design decisions were made.

---

## 1. High-level picture

```
Your FastAPI App
│
├── GET /run  ────────────────────────────────────────────────────────────┐
│                                                                          │
│   aget_prompt_with_meta("my_prompt")                                    │
│        │                                                                 │
│        ▼                                                                 │
│   PromptManager.get_with_meta()                                         │
│        │                                                                 │
│        ▼                                                                 │
│   PromptCache._store  ──── hit ──► return {"content", "version_id"}    │
│        │                                                                 │
│       miss                                                               │
│        │                                                                 │
│        ▼                                                                 │
│   Storage.fetch_active_version()  ◄── SQLite / MongoDB                  │
│        │                                                                 │
│        ▼                                                                 │
│   Cache updated, value returned ─────────────────────────────────────────┘
│
│   aget_prompt_with_fallback("my_prompt")
│        └── same as above; on any error → reads prompts_fallback.json
│
└── app.mount("/prompts", manager.mount_ui())
         │
         ├── /prompts/list, /tree, /edit, /detail, /diff, /test
         ├── /prompts/metadata/{name}, /changelog/{name}, /context
         ├── /prompts/logs, /import, /export, /health, /search
         ├── /prompts/users, /login, /logout
         ├── /prompts/pivot/observe  (SSE — proactive observations)
         ├── /prompts/pivot/chat     (chunked HTTP — Assistant replies)
         └── /prompts/healthz        (liveness probe)
```

---

## 2. Module map

```
llmpivot/
├── __init__.py        Public API surface:
│                      PromptManager, get_prompt, aget_prompt,
│                      aget_prompt_with_meta, aget_prompt_with_fallback,
│                      log_prompt_usage, PromptNotFoundError
│
├── manager.py         Central coordinator. Owns the singleton.
│                      Wires storage, cache, logger, LLM client together.
│                      Exposes mount_ui(), get(), get_with_meta(),
│                      get_with_fallback(), get_sync(), log_usage(),
│                      _write_fallback_snapshot(), schedule_snapshot_refresh(),
│                      _shutdown().
│
├── storage.py         Data persistence layer.
│                      BaseStorage  — abstract interface (ABC)
│                      SQLiteStorage — WAL-mode SQLite, aiosqlite or thread-pool fallback
│                      MongoStorage  — Motor async MongoDB driver
│
│                      Tables (SQLite):
│                        prompts, prompt_versions, prompt_logs, users, audit_log
│                        prompt_metadata, prompt_changelog, app_context
│                        pivot_observations, pivot_conversations
│
├── cache.py           PromptCache — in-memory dict with TTL.
│                      Background asyncio.Task refreshes all keys every ttl seconds.
│                      Explicit invalidate() on every write.
│                      Serves stale values if DB unreachable (fail-safe).
│
├── logger.py          PromptLogger — async queue-buffered usage logger.
│                      asyncio.Queue(maxsize=10_000) — bounded to prevent OOM.
│                      Background worker drains in batches of 50 every 2 seconds.
│
├── llm.py             LLMClient — calls any OpenAI-compatible endpoint.
│                      Auto-detects Azure vs standard OpenAI from URL.
│                      Selects max_tokens vs max_completion_tokens from model name.
│                      All params (temperature, top_p, token limit, api_type,
│                      extra_params) are configurable via PromptManager constructor.
│                      3-retry exponential backoff on 5xx / 429 / timeout / connect.
│
├── pivot.py           PivotAgent — the Assistant backend.
│                      observe(context) → async generator of typed observations
│                      chat(session_id, message, context) → async generator of text chunks
│                      Tool-calling loop: up to 4 rounds, 10s per tool.
│                      Write tools require explicit user confirmation.
│
├── auth.py            Password hashing: Argon2id → bcrypt → PBKDF2.
│                      HMAC-SHA256 session tokens (no JWT library dependency).
│                      CSRF token generation and validation.
│                      RBAC permission checks.
│
├── db.py              DEPRECATED backward-compat shim.
│
└── ui/
    ├── __init__.py    Exports build_router.
    ├── routes.py      All HTTP handlers. Closure over PromptManager instance.
    │                  Phase 1 routes: /metadata, /changelog, /context, /tree, /health
    │                  Phase 2 routes: /pivot/observe (SSE), /pivot/chat (chunked HTTP)
    │                  CSRF validation on every POST.
    │                  Per-IP login rate limiting.
    │                  Input size limits: 500 KB content, 10 KB log text, 5 MB import.
    │                  Auto-changelog triggered on every version save.
    ├── templates.py   Server-rendered HTML (pure Python f-strings, no Jinja2).
    │                  Alpine.js CDN tag + pivot_widget_html() injected into base layout.
    │                  _escape() wraps all user-supplied values to prevent XSS.
    └── helpers.py     escape() and render_user_badge() utilities.
```

---

## 3. Startup sequence

```
PromptManager.__init__()
  1. Resolve auth_mode
  2. Guard: raise ValueError if default secret_key used with auth_mode="rbac"
  3. Pick storage backend (SQLiteStorage or MongoStorage)
  4. storage.init_db_sync() — create tables, run auto-migrations, enable WAL
  5. Create PromptCache(storage, ttl, tenant_id)
  6. Create PromptLogger(storage, sample_rate, tenant_id)
  7. Create LLMClient(url, api_key, model, api_type, ...) if llm_url is set
  8. Resolve fallback_path (same dir as db_path by default)
  9. Register self as global _instance singleton

mount_ui() lifespan startup:
  10. _bootstrap_admin() — create admin user if bootstrap_admin=True and none exists
  11. cache.start()        — launch background cache refresh asyncio.Task
  12. usage_logger.start() — launch background batch writer asyncio.Task
  13. _warm_cache()        — pre-fetch all active prompts before serving traffic
  14. _write_fallback_snapshot() — write prompts_fallback.json from warmed cache

mount_ui() lifespan shutdown:
  15. _shutdown():
        usage_logger.drain() — flush queue to DB (up to 10s)
        usage_logger.stop()  — cancel worker task
        cache.stop()         — cancel refresh task
```

---

## 4. Hot path — prompt retrieval

Every call to `aget_prompt_with_meta("name")`:

```
__init__.py: aget_prompt_with_meta(name)
  └── manager.py: get_instance().get_with_meta(name)
        └── cache.py: PromptCache.get(name)
              ├── _store[name] exists and not stale? → return immediately (no I/O)
              └── stale or missing:
                    storage.fetch_active_version(name, tenant_id)
                    _store[name] = {content, version_id, fetched_at}
                    return entry
```

`aget_prompt_with_fallback("name")` wraps the above and catches any exception, then reads `prompts_fallback.json` as a last resort.

---

## 5. Write path — prompt update via UI

```
POST /prompts/edit/{name}
  1. _check_auth(request, min_role="editor")
  2. _validate_csrf(request, csrf_token)
  3. size check: len(content.encode()) > 500 KB → 400
  4. storage.create_version(name, content, editor, tag, set_active, tenant_id)
  5. storage.insert_audit_log(action="version_created", ...)
  6. cache.invalidate(name)
  7. schedule_snapshot_refresh()         ← fire-and-forget, writes prompts_fallback.json
  8. _generate_changelog() task created  ← background, calls LLM, saves to prompt_changelog
  9. RedirectResponse to /detail/{name}
```

---

## 6. Fallback snapshot path

```
_write_fallback_snapshot()
  ├── storage.export_prompts(tenant_id)   → {name: content, ...}
  ├── tempfile.mkstemp(dir=snapshot_dir)  → write JSON to .tmp file
  └── os.replace(tmp_path, fallback_path) → atomic rename (POSIX + Windows)

On failure: WARNING logged, exception suppressed, snapshot not written.
On success: next call to _load_fallback_snapshot() returns fresh data.
```

Triggered on:
- Startup warm (`_warm_cache` → `_write_fallback_snapshot`)
- Every `set_active_version` (via `schedule_snapshot_refresh`)
- Every `create_version` with `set_active=True`

---

## 7. Assistant (Pivot) — observation flow (SSE)

```
Browser widget opens EventSource → GET /prompts/pivot/observe?page=...&prompt_name=...
  │
  ├── _check_auth(request, min_role="viewer")
  │
  └── PivotAgent.observe(context)
        │
        ├── _rule_based_observations(page, prompt_name, tenant_id)
        │     Rule checks run synchronously against storage (no LLM):
        │     • No active version → yield WARNING
        │     • Active version has no changelog → yield SUGGESTION
        │     • Editing a prompt that has children → yield ALERT
        │     • Prompt has no metadata → yield SUGGESTION
        │     • Sensitivity = high → yield ALERT
        │     • Version count and last editor → yield OBSERVATION
        │
        └── yield each obs as: event: {obs_type}\ndata: {json}\n\n
              ↓
        yield: event: done\ndata: {}\n\n   ← client closes EventSource
```

Observations are rule-based and appear instantly, with no LLM call.

---

## 8. Assistant (Pivot) — chat flow (chunked HTTP)

```
Browser POST /prompts/pivot/chat  { session_id, message, context }
  │
  ├── _check_auth(request, min_role="viewer")
  │
  └── PivotAgent.chat(session_id, message, context, tenant_id)
        │
        ├── Load last 20 conversation turns from pivot_conversations
        ├── Build messages list: system prompt + history + current message
        │
        ├── Tool-calling loop (max 4 rounds):
        │     │
        │     ├── LLMClient._call(system, flattened_messages)    [30s timeout]
        │     │
        │     ├── _extract_tool_call(reply) → tool name + args?
        │     │     YES → execute tool (10s timeout) → inject result → loop
        │     │     NO  → stream reply word by word → break
        │     │
        │     └── yield "data: {type, content}\n\n" for each chunk
        │
        ├── yield "data: {type: done}\n\n"
        └── Save user + assistant turns to pivot_conversations
```

**Tool execution** is synchronous within the request. Available tools:
`get_prompt`, `get_hierarchy`, `get_version_history`, `get_usage_stats`,
`get_app_context`, `list_all_prompts`, `suggest_edit`

Write tools (`create_version`) require an explicit confirm POST before executing — the agent streams a diff preview and waits.

---

## 9. Auto-changelog generation

```
POST /prompts/edit/{name}  (on successful save)
  │
  └── asyncio.get_event_loop().create_task(
        _generate_changelog(manager, name, version_id, old_content, new_content, editor)
      )

_generate_changelog():
  1. Build unified diff (old → new, capped at 80 lines)
  2. Read prompt purpose from prompt_metadata
  3. Build user message: "Prompt: {name}\nPurpose: {purpose}\nDiff:\n{diff}\nNew content:\n{content}"
  4. LLMClient._call(_CHANGELOG_SYSTEM_PROMPT, user_message)
  5. storage.upsert_changelog_entry(version_id, entry, generated_by="pivot")

On failure: WARNING logged, silently continues. Never blocks the HTTP response.
```

---

## 10. LLM client — payload construction

```python
LLMClient._build_payload(system, user):

  payload = {messages: [{system}, {user}]}

  # API type
  if not is_azure:
      payload["model"] = self._model    # Azure uses deployment in URL

  # Token limit (first explicit, then auto-detect, then nothing)
  if max_completion_tokens set explicitly:
      payload["max_completion_tokens"] = value
  elif max_tokens set explicitly:
      payload["max_tokens"] = value
  elif auto-detect:
      if model starts with gpt-5 / o1 / o3 / o4:
          payload["max_completion_tokens"] = 2048
      else:
          payload["max_tokens"] = 2048

  # Optional params (only added when explicitly set — new-gen models reject temperature)
  if temperature is not None: payload["temperature"] = temperature
  if top_p is not None:       payload["top_p"] = top_p

  # Caller extra params (merged last, can override anything above)
  payload.update(extra_params)
```

---

## 11. Database schema (SQLite)

```sql
-- Core (unchanged from v1)
prompts          (id, name, tenant_id, is_deleted)
prompt_versions  (id, prompt_id, tenant_id, content, version_number,
                  created_at, created_by, tag, is_active)
prompt_logs      (id, prompt_id, version_id, tenant_id, input, output, timestamp)
users            (id, username, email, password_hash, role, tenant_id, is_active, created_at)
audit_log        (id, action, performed_by, prompt_name, version_id, detail, tenant_id, created_at)

-- Phase 1 — metadata, changelogs, context
prompt_metadata  (id, prompt_id, tenant_id,
                  purpose, prompt_type, parent_prompt_id,
                  feature_area, called_from, model_used,
                  input_variables, owner, sensitivity, notes, updated_at)
prompt_changelog (id, version_id, tenant_id,
                  entry, generated_by, created_at, created_by)
app_context      (id, tenant_id, content, version, created_at, created_by)

-- Phase 2 — Assistant state
pivot_observations  (id, tenant_id, user_id, page_context, obs_type,
                     content, action_payload, dismissed, created_at)
pivot_conversations (id, tenant_id, session_id, role, content,
                     tool_calls, tool_results, created_at)
```

All tables include `tenant_id` for multi-tenant isolation. All new tables added additively — no changes to existing schema.

SQLite is opened with `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=10000` on every connection.

---

## 12. Authentication flow

```
POST /prompts/login
  ├── _get_client_ip(request)               — X-Forwarded-For aware
  ├── _is_rate_limited(ip)                  — 10 failures / 5 min → 15 min lockout
  ├── storage.get_user(username, tenant_id)
  ├── verify_password(submitted, stored_hash)
  │     ├── "argon2$..." → argon2-cffi verify (VerificationError caught by name)
  │     ├── "bcrypt$..." → bcrypt checkpw
  │     ├── "pbkdf2$..." → PBKDF2 260k iterations
  │     └── legacy "{salt}${hex}" → PBKDF2 100k iterations (backward compat)
  └── set_cookie("llmpivot_session", HMAC token, httponly=True, samesite="lax")
```

---

## 13. CSRF protection

CSRF tokens are stateless — derived from the session token:

```
generate_csrf_token(session_token, secret_key):
    raw = (session_token + ":csrf").encode()
    return base64url(HMAC-SHA256(secret_key, raw))
```

On every GET that renders a form the server embeds this token as a hidden field. On every POST it re-derives and compares with `hmac.compare_digest`. No separate token store needed.

---

## 14. Frontend stack

The dashboard is server-rendered HTML (pure Python f-strings, no Jinja2). Two lightweight JS libraries are loaded from CDN — nothing in `requirements.txt`:

**HTMX (~14 KB, CDN)** — *planned for Phase 3*. Server-driven partial updates. One attribute per element, no build step.

**Alpine.js (~15 KB, CDN)** — powers the Assistant floating widget. Used for:
- Widget open/closed state (persisted in `localStorage`)
- Observation feed — new SSE events appended to a reactive array
- Chat message stream — fetch() with ReadableStream reader
- Diff preview accept/reject flow

The widget is injected as a `<div id="pivot-widget" x-data="pivotWidget()">` block just before `</body>` on every page where `pivot_enabled=True`. Pages where it isn't needed (`login`, `logout`) don't receive it.

---

## 15. Key design decisions

**No JWT library** — auth uses stdlib `hmac`, `hashlib`, `base64`, `json`. Zero extra dependencies for the auth core.

**No template engine** — all HTML is pure Python f-strings. No Jinja2, no build step. All user values go through `html.escape()`.

**Pluggable password hashing** — works out of the box with PBKDF2 (stdlib), upgrades silently to Argon2id or bcrypt when available. All three formats verify side-by-side — no migration required.

**LLM config is fully externalised** — `LLMClient` has no hardcoded assumptions. `api_type`, token limit field, temperature, extra params are all caller-controlled. Auto-detection is a default, not a constraint.

**Singleton PromptManager** — `_instance` enables module-level helpers without requiring callers to thread the manager object through the call stack.

**Soft deletes** — `is_deleted=1` on the prompt row. Data is never destroyed. A `create_version` call on the same name restores it.

**Bounded log queue** — `asyncio.Queue(maxsize=10_000)` prevents unbounded memory growth under a write storm.

**Atomic snapshot writes** — `tempfile.mkstemp` + `os.replace` ensures `prompts_fallback.json` is never partially written, even if the process dies mid-write.

**Observations are rule-based, not LLM-based** — they appear instantly. LLM is only called for chat replies and auto-changelog. The widget is useful even when no LLM is configured.

**SSE for observations, chunked HTTP for chat** — observations are a persistent server-push feed (fires unprompted, auto-reconnects). Chat is request/response (one message, one streamed reply, closes). Right transport for each pattern.

---

## 16. Known gaps and future work

| Gap | Notes |
|-----|-------|
| Cross-worker cache invalidation | Each process has its own `_store`. Keep `cache_ttl ≤ 5s`, or add Redis pub/sub invalidation |
| PostgreSQL / MySQL | Only SQLite and MongoDB. SQLAlchemy async would be the right addition |
| Prompt template rendering | `{{variable}}` substitution not yet implemented |
| Phase 3: embeddings / semantic search | `embeddings.py` + `/prompts/search` planned |
| Phase 3: prompt health score | Computed 0–100 badge on the list view |
| Phase 3: Assistant write tool | `create_version` tool with diff preview + confirm flow |
| HTMX partial updates | Tree view and metadata forms; planned for Phase 3 |
| Webhook on activation | No callback when a prompt goes live |
| CLI tool | No `llmpivot` command-line interface |
