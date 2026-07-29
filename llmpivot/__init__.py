"""
llmpivot - runtime prompt control for production apps.

Quick start:
    from llmpivot import PromptManager, get_prompt

    PromptManager(db_path="prompts.db", cache_ttl=5)
    prompt = get_prompt("summary")
"""

from .manager import PromptManager, PromptNotFoundError, get_instance


def get_prompt(name: str) -> str:
    """
    Return the active version content for the named prompt.
    PromptManager must be initialized before calling this.
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


def log_prompt_usage(
    prompt_name: str,
    version_id: int,
    input_text: str,
    output_text: str,
) -> None:
    """Fire-and-forget usage log. Safe to call from sync or async code."""
    get_instance().log_usage(prompt_name, version_id, input_text, output_text)


__all__ = [
    "PromptManager",
    "PromptNotFoundError",
    "get_prompt",
    "aget_prompt",
    "aget_prompt_with_meta",
    "log_prompt_usage",
]
