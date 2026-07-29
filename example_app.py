"""
Minimal example - shows how to wire llmpivot into a FastAPI app.
Run with: uvicorn example_app:app --reload
Then visit: http://localhost:8000/prompts/list
"""

from fastapi import FastAPI
from llmpivot import PromptManager, aget_prompt_with_meta, log_prompt_usage

# 1. Initialize once at startup
manager = PromptManager(
    db_path="prompts.db",
    cache_ttl=5,
    protected_mode=False,
    auth_mode="rbac"
    # Optional LLM for AI suggestions + A/B testing:
    # llm_url="https://api.openai.com/v1/chat/completions",
    # llm_api_key="sk-...",
    # llm_model="gpt-4o",
)

app = FastAPI()

# 2. Mount the UI
app.mount("/prompts", manager.mount_ui())


# 3. Use prompts anywhere in your app - use await in async routes
@app.get("/test")
async def summarize(text: str = "hello"):
    meta = await aget_prompt_with_meta("my_prompt")  # returns content + version_id
    prompt = meta["content"]

    # ... call your LLM with the prompt ...
    output = f"[LLM output using prompt: {prompt[:40]}...]"

    # 4. Log usage with the correct version_id - no hardcoding needed
    log_prompt_usage("my_prompt", meta["version_id"], input_text=text, output_text=output)

    return {"prompt": prompt, "output": output}
