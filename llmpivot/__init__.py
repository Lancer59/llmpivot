"""
llmpivot - versioned prompt and agent skill management for production apps.

Quick start:
    from llmpivot import LLMAssetManager, get_prompt

    LLMAssetManager(db_path="prompts.db", cache_ttl=5)
    prompt = get_prompt("summary")
"""

from .manager import LLMAssetManager, PromptManager, PromptNotFoundError, get_instance
from .tokens import estimate_tokens


def get_prompt(name: str) -> str:
    """
    Return the active version content for the named prompt.
    LLMAssetManager must be initialized before calling this.
    """
    return get_instance().get_sync(name)


async def aget_prompt(name: str) -> str:
    """Async version of get_prompt - preferred inside async code."""
    return await get_instance().get(name)


async def aget_prompt_with_meta(name: str) -> dict:
    """
    Async - returns content and version_id together.
    Use this when you need to log usage with the correct version.

    Example:
        meta = await aget_prompt_with_meta("summary")
        output = your_llm(meta["content"], user_input)
        log_prompt_usage("summary", meta["version_id"], user_input, output)

    Returns:
        {"content": str, "version_id": int}
    """
    return await get_instance().get_with_meta(name)


async def aget_prompt_with_fallback(name: str) -> str:
    """
    Async - tries the live cache/DB first; falls back to prompts_fallback.json on any error.

    Use this instead of aget_prompt when you want zero-downtime resilience.
    If llmpivot's DB is unreachable, the last known good version is served from the
    fallback snapshot written to disk on every activation.

    Example:
        content = await aget_prompt_with_fallback("summary")
        output = your_llm(content, user_input)

    Returns:
        str — the prompt content
    Raises:
        PromptNotFoundError — only when the prompt is absent from both live DB and snapshot
    """
    return await get_instance().get_with_fallback(name)


def log_prompt_usage(
    prompt_name: str,
    version_id: int,
    input_text: str,
    output_text: str,
) -> None:
    """Fire-and-forget usage log. Safe to call from sync or async code."""
    get_instance().log_usage(prompt_name, version_id, input_text, output_text)


async def alist_skills() -> list:
    """Return active skill names and descriptions without loading instructions."""
    return await get_instance().list_skills()


async def aget_skill(name: str) -> dict:
    """Load one active skill's SKILL.md instructions and version metadata."""
    return await get_instance().get_skill(name)


async def aget_skill_reference(name: str, path: str) -> str:
    """Load one Markdown reference file from an active skill on demand."""
    return await get_instance().get_skill_reference(name, path)


__all__ = [
    "LLMAssetManager",
    "PromptManager",
    "PromptNotFoundError",
    "estimate_tokens",
    "get_prompt",
    "aget_prompt",
    "aget_prompt_with_meta",
    "aget_prompt_with_fallback",
    "log_prompt_usage",
    "alist_skills",
    "aget_skill",
    "aget_skill_reference",
]
