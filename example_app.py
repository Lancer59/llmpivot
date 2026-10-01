"""
llmpivot — production-ready example app with Assistant agent enabled.

Run locally:
    uvicorn example_app:app --reload

Then open: http://localhost:8000/prompts/list
Agent Skills: http://localhost:8000/prompts/skills
Login with: admin / changeme  (change the password immediately after first login)

Environment variables (put in .env or export directly):

  Standard OpenAI:
    OPENAI_API_KEY              — OpenAI API key
    OPENAI_MODEL                — model name (default: gpt-4o)

  Azure OpenAI (used when AZURE_OPENAI_ENDPOINT is set):
    AZURE_OPENAI_API_KEY        — Azure API key
    AZURE_OPENAI_ENDPOINT       — e.g. https://myresource.openai.azure.com/
    AZURE_OPENAI_DEPLOYMENT_NAME — deployment name, e.g. gpt-4o or gpt-5.3-chat
    AZURE_OPENAI_API_VERSION    — e.g. 2024-08-01-preview

  llmpivot:
    LLMPIVOT_SECRET   — HMAC signing key for session cookies (required in production)
    LLMPIVOT_DB       — path to the SQLite database file (default: instruction_studio.db)
    LLMPIVOT_PASSWORD — initial admin bootstrap password (default: changeme)
"""

import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from llmpivot import (
    LLMAssetManager,
    PromptNotFoundError,
    aget_prompt_with_meta,
    log_prompt_usage,
)

# ---------------------------------------------------------------------------
# Load .env if present (stdlib only — no python-dotenv needed)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> None:
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

_load_dotenv()

# ---------------------------------------------------------------------------
# Resolve LLM config from environment
# ---------------------------------------------------------------------------

def _resolve_llm() -> dict:
    """
    Returns a dict of keyword args to pass directly to LLMAssetManager.

    Azure OpenAI takes precedence when AZURE_OPENAI_ENDPOINT is set.
    The URL is built from endpoint + deployment + api-version so callers
    never have to construct it manually.
    """
    azure_endpoint    = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    azure_key         = os.environ.get("AZURE_OPENAI_API_KEY", "")
    azure_deployment  = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "")
    azure_version     = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")

    if azure_endpoint and azure_key and azure_deployment:
        url = (
            f"{azure_endpoint}/openai/deployments/{azure_deployment}"
            f"/chat/completions?api-version={azure_version}"
        )
        print(f"[llmpivot] Azure OpenAI: {azure_endpoint} / deployment: {azure_deployment}")
        return {
            "llm_url":      url,
            "llm_api_key":  azure_key,
            "llm_model":    azure_deployment,   # used for model-gen detection
            "llm_api_type": "azure",            # skip auto-detection
        }

    openai_key   = os.environ.get("OPENAI_API_KEY", "")
    openai_model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    if openai_key:
        print(f"[llmpivot] OpenAI: model={openai_model}")
        return {
            "llm_url":      "https://api.openai.com/v1/chat/completions",
            "llm_api_key":  openai_key,
            "llm_model":    openai_model,
            "llm_api_type": "openai",
        }

    print("[llmpivot] No LLM configured — AI features and Assistant chat will be unavailable.")
    print("           Set AZURE_OPENAI_* or OPENAI_API_KEY in your .env to enable.")
    return {}


_llm_kwargs = _resolve_llm()

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

SECRET_KEY         = os.environ.get("LLMPIVOT_SECRET", secrets.token_hex(32))
DB_PATH            = os.environ.get("LLMPIVOT_DB", "instruction_studio.db")
BOOTSTRAP_PASSWORD = os.environ.get("LLMPIVOT_PASSWORD", "changeme")

# ---------------------------------------------------------------------------
# LLMAssetManager — initialise once at module level
# ---------------------------------------------------------------------------

manager = LLMAssetManager(
    # --- storage ---
    db_path=DB_PATH,
    cache_ttl=5,

    # --- auth ---
    auth_mode="rbac",
    secret_key=SECRET_KEY,
    cookie_secure=False,        # True in production (requires HTTPS)

    # --- first-run admin bootstrap ---
    bootstrap_admin=True,
    bootstrap_password=BOOTSTRAP_PASSWORD,

    # --- usage logging ---
    log_sample_rate=1.0,

    # --- LLM (spread resolved env config in) ---
    **_llm_kwargs,

    # --- LLM request tuning (all optional, override auto-detection) ---
    # llm_temperature=0.7,              # omit = use model default
    # llm_max_tokens=1024,              # for GPT-4 and earlier
    # llm_max_completion_tokens=1024,   # for GPT-5 / o1 / o3+
    # llm_timeout=60.0,                 # seconds
    # llm_extra_params={"response_format": {"type": "json_object"}},

    # --- Assistant agent ---
    pivot_enabled=True,         # show the floating Assistant widget
    pivot_proactive=True,       # auto-analyse the current page
    auto_changelog=True,        # auto-generate changelog on every version save
    skill_loading_mode="progressive",  # load skill descriptions first, then instructions on demand

    # --- Fallback snapshot ---
    fallback_snapshot=True,     # write prompts_fallback.json on every activation
)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="My LLM App",
    description="Example app using versioned prompts and Agent Skills.",
)

@app.exception_handler(PromptNotFoundError)
async def _prompt_not_found(request: Request, exc: PromptNotFoundError):
    return JSONResponse(
        status_code=404,
        content={"error": "prompt_not_found", "detail": str(exc)},
    )

app.mount("/prompts", manager.mount_ui())


# ---------------------------------------------------------------------------
# Your application routes
# ---------------------------------------------------------------------------

@app.get("/summarize")
async def summarize(prompt_name: str, text: str = "hello world"):
    """
    Example route using a managed prompt.
    Uses the active version so the request can be attributed in usage logs.
    """
    meta = await aget_prompt_with_meta(prompt_name)
    content = meta["content"]
    llm_output = f"[LLM output for: '{text[:60]}' using prompt: '{content[:40]}...']"
    log_prompt_usage(prompt_name, meta["version_id"], text, llm_output)
    await manager.usage_logger.flush()
    return {"prompt_name": prompt_name, "version_id": meta["version_id"], "prompt_preview": content[:80], "output": llm_output}


@app.get("/health")
async def health():
    return {"status": "ok"}
