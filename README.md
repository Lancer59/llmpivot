# LLMPivot
## Large Language Model Prompt Iteration & Versioning Optimization Toolkit

Open-source, local-first, Production grade intelligent prompt management system.

Runtime prompt control for LLM/AI apps. Change a prompt in the UI, see it reflected in your running app within seconds — no redeployment needed.

## Install

```bash
pip install llmpivot
```

## Quick Start

```python
from fastapi import FastAPI
from llmpivot import PromptManager, aget_prompt_with_meta, log_prompt_usage

# 1. Initialize once at startup
manager = PromptManager(
    db_path="prompts.db",
    cache_ttl=5,
)

app = FastAPI()

# 2. Mount the UI
app.mount("/prompts", manager.mount_ui())

# 3. Use prompts in your routes
@app.get("/run")
async def run(text: str = "hello"):
    meta = await aget_prompt_with_meta("my_prompt")

    output = your_llm(meta["content"], text)  # your LLM call

    # 4. Log usage with the correct version — no hardcoding
    log_prompt_usage("my_prompt", meta["version_id"], input_text=text, output_text=output)

    return {"output": output}
```

Visit `http://localhost:8000/prompts/list` to manage prompts.

---

## API Reference

### `aget_prompt(name) -> str`

Returns the active version content for the named prompt. Use inside async functions (FastAPI routes).

```python
from llmpivot import aget_prompt

prompt = await aget_prompt("my_prompt")
```

### `aget_prompt_with_meta(name) -> dict`

Returns both the content and the version_id of the active prompt. Preferred when you need to log usage accurately — no hardcoded IDs.

```python
from llmpivot import aget_prompt_with_meta

meta = await aget_prompt_with_meta("my_prompt")
# meta = {"content": "...", "version_id": 3}

output = your_llm(meta["content"], user_input)
log_prompt_usage("my_prompt", meta["version_id"], input_text=user_input, output_text=output)
```

### `get_prompt(name) -> str`

Sync version. Works in plain scripts outside an event loop. Inside FastAPI routes, use `aget_prompt` or `aget_prompt_with_meta` instead.

```python
from llmpivot import get_prompt

prompt = get_prompt("my_prompt")
```

### `log_prompt_usage(name, version_id, input_text, output_text)`

Writes a usage log entry to the `prompt_logs` table. Async, non-blocking — safe to call from sync or async code. Failures are silently swallowed and never propagate to the caller.

```python
from llmpivot import log_prompt_usage

log_prompt_usage("my_prompt", meta["version_id"], input_text=text, output_text=output)
```

Logs are viewable in the UI at `/prompts/logs`, filterable by prompt name.

---

## UI Pages

Once mounted, the UI is available at your mount prefix (e.g. `/prompts`):

| Route | Description |
|---|---|
| `/prompts/list` | All prompts with active version, last editor, last updated |
| `/prompts/detail/{name}` | Version history, make active, rollback |
| `/prompts/edit/{name}` | Edit prompt, create new version, AI suggestion |
| `/prompts/edit/__new__` | Create a new prompt |
| `/prompts/diff/{name}` | Line-by-line diff between any two versions |
| `/prompts/test/{name}` | A/B test two versions side by side |
| `/prompts/logs` | Usage log viewer, filterable by prompt name |

---

## LLM Suggestions

Add LLM config to `PromptManager` and the "✨ Get AI Suggestion" button appears automatically on every edit page. Works with any OpenAI-compatible endpoint.

```python
manager = PromptManager(
    llm_url="https://api.openai.com/v1/chat/completions",
    llm_api_key="sk-...",
    llm_model="gpt-4o",
)
```

To override the system prompt used for suggestions:

```python
manager = PromptManager(
    llm_url="...",
    llm_api_key="...",
    llm_suggester_prompt="You are an expert at writing concise RAG system prompts. Return only the improved prompt.",
)
```

If `llm_url` is not set, the suggestion button and A/B test panel are hidden automatically.

---

## Protected Mode

Require a password to set active versions or assign the `prod` tag.

```python
manager = PromptManager(
    protected_mode=True,
    admin_password="your-password",
)
```

No sessions or tokens — a simple password check per action. Wrong password re-renders the form with an error.

---

## Version Tagging

Versions can be tagged `prod`, `staging`, or `experiment`.

- Only one `prod` tag is active per prompt at a time — assigning it removes the tag from the previous version automatically.
- In protected mode, assigning `prod` or setting a version active requires the admin password.

---

## Resilience

If the database is unreachable, `get_prompt` / `aget_prompt` serve the last cached value and log a warning. Your app never crashes due to a DB failure. If there is no cached value and the DB is down, a `PromptNotFoundError` is raised.

---

## Config Reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str` | `"prompts.db"` | SQLite file path (auto-created) |
| `cache_ttl` | `int` | `5` | Seconds between cache refreshes |
| `protected_mode` | `bool` | `False` | Require password for prod actions |
| `admin_password` | `str` | `None` | Required if `protected_mode=True` |
| `log_sample_rate` | `float` | `1.0` | Fraction of usages to log (0.0–1.0) |
| `llm_url` | `str` | `None` | OpenAI-compatible chat completions endpoint |
| `llm_api_key` | `str` | `None` | Bearer token for LLM API |
| `llm_model` | `str` | `"gpt-3.5-turbo"` | Model name |
| `llm_suggester_prompt` | `str` | built-in | System prompt used by the AI suggester |
