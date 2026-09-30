"""
llmpivot — production-ready example app.

Run locally:
    uvicorn example_app:app --reload

Then open: http://localhost:8000/prompts/list
Login with: admin / changeme  (change the password immediately after first login)

Environment variables:
    LLMPIVOT_SECRET   — HMAC signing key for session cookies (required in production)
    LLMPIVOT_DB       — path to the SQLite database file (default: prompts.db)
    LLMPIVOT_PASSWORD — initial admin bootstrap password (default: changeme)
"""

import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from llmpivot import (
    PromptManager,
    PromptNotFoundError,
    aget_prompt_with_meta,
    log_prompt_usage,
)

# ---------------------------------------------------------------------------
# Configuration — load from environment in production, sensible defaults for dev
# ---------------------------------------------------------------------------

# secret_key signs all session cookies. A new random value is generated each
# run when the env var is not set — fine for dev, but means sessions are
# invalidated on every restart. Set LLMPIVOT_SECRET in production.
#   python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = os.environ.get("LLMPIVOT_SECRET", secrets.token_hex(32))

DB_PATH = os.environ.get("LLMPIVOT_DB", "prompts.db")
BOOTSTRAP_PASSWORD = os.environ.get("LLMPIVOT_PASSWORD", "changeme")

# ---------------------------------------------------------------------------
# 1. Initialize PromptManager — do this exactly once at module level
# ---------------------------------------------------------------------------
manager = PromptManager(
    # --- storage ---
    db_path=DB_PATH,
    cache_ttl=5,            # seconds before a cached prompt is re-fetched from DB

    # --- auth ---
    auth_mode="rbac",       # "disabled" | "protected" | "rbac"
    secret_key=SECRET_KEY,
    cookie_secure=False,    # True in production (requires HTTPS)

    # --- first-run admin bootstrap ---
    bootstrap_admin=True,
    bootstrap_password=BOOTSTRAP_PASSWORD,

    # --- tenancy ---
    tenant_id="default",    # change per org/workspace in multi-tenant deployments

    # --- usage logging ---
    log_sample_rate=1.0,    # 1.0 = log every call; 0.1 = log 10% (reduce DB writes)

    # --- optional LLM for AI suggestions + A/B testing ---
    # llm_url="https://api.openai.com/v1/chat/completions",
    # llm_api_key=os.environ.get("OPENAI_API_KEY", ""),
    # llm_model="gpt-4o",
)

# ---------------------------------------------------------------------------
# 2. Create the FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="My LLM App",
    description="Powered by llmpivot for runtime prompt management.",
)

# ---------------------------------------------------------------------------
# 3. Register the PromptNotFoundError handler BEFORE mounting the sub-app.
#    Without this, a missing prompt raises an unhandled 500. With it: clean 404.
# ---------------------------------------------------------------------------
@app.exception_handler(PromptNotFoundError)
async def _prompt_not_found(request: Request, exc: PromptNotFoundError):
    return JSONResponse(
        status_code=404,
        content={"error": "prompt_not_found", "detail": str(exc)},
    )

# ---------------------------------------------------------------------------
# 4. Mount the Prompt Manager UI at /prompts
#    This also registers /prompts/healthz for load-balancer liveness probes.
# ---------------------------------------------------------------------------
app.mount("/prompts", manager.mount_ui())


# ---------------------------------------------------------------------------
# 5. Your application routes — use prompts anywhere
# ---------------------------------------------------------------------------

@app.get("/summarize")
async def summarize(text: str = "hello world"):
    """
    Example route that fetches a prompt and uses it with an LLM.

    Steps:
    1. aget_prompt_with_meta() — single cache hit, returns content + version_id
    2. Call your LLM with the prompt content
    3. log_prompt_usage() — fire-and-forget, non-blocking
    """
    meta = await aget_prompt_with_meta("summarize_prompt")

    # --- Replace this with your actual LLM call ---
    llm_output = f"[LLM output for: '{text[:60]}' using prompt v{meta['version_id']}]"

    # Log usage with the exact version that served this request.
    # version_id is tracked automatically — no hardcoding needed.
    log_prompt_usage(
        "summarize_prompt",
        meta["version_id"],
        input_text=text,
        output_text=llm_output,
    )

    return {
        "prompt_version": meta["version_id"],
        "prompt_preview": meta["content"][:80],
        "output": llm_output,
    }


@app.get("/health")
async def health():
    """Application-level health check. See /prompts/healthz for deeper checks."""
    return {"status": "ok"}
