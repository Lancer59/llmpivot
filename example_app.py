"""
Production-ready example — shows how to wire llmpivot into a FastAPI app.

Run with: uvicorn example_app:app --reload
Then visit: http://localhost:8000/prompts/list

Production notes demonstrated here:
- secret_key must be set explicitly when auth_mode="rbac"; use secrets.token_hex(32).
- cookie_secure=True (default) ensures the session cookie is HTTPS-only.
- A global exception handler converts PromptNotFoundError to a clean 404 JSON response.
- log_prompt_usage logs the exact version that served the request for accurate tracking.
"""

import secrets
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from llmpivot import PromptManager, PromptNotFoundError, aget_prompt_with_meta, log_prompt_usage

# ---------------------------------------------------------------------------
# 1. Initialize once at startup
#    - Replace the secret_key with a real random value (e.g. from an env var).
#    - Set cookie_secure=False only during local HTTP development.
# ---------------------------------------------------------------------------
manager = PromptManager(
    db_path="prompts.db",
    cache_ttl=5,
    auth_mode="rbac",
    secret_key=secrets.token_hex(32),   # In production: load from env/secrets manager
    cookie_secure=False,                # Set True (default) in production behind HTTPS
    bootstrap_admin=True,
    bootstrap_password="changeme",      # Change immediately after first login
    # Optional LLM for AI suggestions + A/B testing:
    # llm_url="https://api.openai.com/v1/chat/completions",
    # llm_api_key="sk-...",
    # llm_model="gpt-4o",
)

app = FastAPI()

# ---------------------------------------------------------------------------
# 2. Global handler: PromptNotFoundError → clean 404 (not an unhandled 500)
#    Register this BEFORE mounting the sub-app so it applies to all routes.
# ---------------------------------------------------------------------------
@app.exception_handler(PromptNotFoundError)
async def prompt_not_found_handler(request: Request, exc: PromptNotFoundError):
    return JSONResponse(
        status_code=404,
        content={"error": "prompt_not_found", "detail": str(exc)},
    )

# ---------------------------------------------------------------------------
# 3. Mount the UI
# ---------------------------------------------------------------------------
app.mount("/prompts", manager.mount_ui())


# ---------------------------------------------------------------------------
# 4. Use prompts anywhere in your application
# ---------------------------------------------------------------------------
@app.get("/summarize")
async def summarize(text: str = "hello"):
    # get_with_meta returns content + version_id in a single cache hit
    meta = await aget_prompt_with_meta("summarize_prompt")
    prompt = meta["content"]

    # --- call your LLM here ---
    output = f"[LLM output using prompt v{meta['version_id']}: {prompt[:40]}...]"

    # Log usage with the exact version that served this request
    log_prompt_usage("summarize_prompt", meta["version_id"], input_text=text, output_text=output)

    return {"prompt_version": meta["version_id"], "output": output}
