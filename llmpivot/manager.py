"""
PromptManager - central coordinator.
Initialize once at app startup; get_prompt() uses the singleton.
"""

import asyncio
import logging
from typing import Optional

from .db import init_db
from .cache import PromptCache
from .logger import PromptLogger
from .llm import LLMClient

logger = logging.getLogger("llmpivot")

_instance: Optional["PromptManager"] = None


class PromptNotFoundError(Exception):
    pass


class PromptManager:
    def __init__(
        self,
        db_path: str = "prompts.db",
        cache_ttl: int = 5,
        protected_mode: bool = False,
        admin_password: Optional[str] = None,
        log_sample_rate: float = 1.0,
        # LLM suggester (all optional)
        llm_url: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        llm_model: str = "gpt-3.5-turbo",
        llm_suggester_prompt: Optional[str] = None,
    ):
        global _instance

        self.db_path = db_path
        self.cache_ttl = cache_ttl
        self.protected_mode = protected_mode
        self.admin_password = admin_password
        self.log_sample_rate = log_sample_rate

        # Bootstrap DB synchronously so it's ready before any async calls
        init_db(db_path)

        self.cache = PromptCache(db_path, cache_ttl)
        self.usage_logger = PromptLogger(db_path, log_sample_rate)

        # LLM client - only created if url is provided
        self.llm: Optional[LLMClient] = None
        if llm_url:
            self.llm = LLMClient(
                url=llm_url,
                api_key=llm_api_key or "",
                model=llm_model,
                system_prompt=llm_suggester_prompt,
            )

        _instance = self
        logger.info("PromptManager initialized (db=%s, ttl=%ds, protected=%s)", db_path, cache_ttl, protected_mode)

    @property
    def has_llm(self) -> bool:
        return self.llm is not None

    def mount_ui(self):
        """
        Return a FastAPI sub-application for mounting.

        Usage:
            app.mount("/prompts", manager.mount_ui())
        """
        from fastapi import FastAPI
        from .ui.routes import build_router

        sub = FastAPI()
        sub.include_router(build_router(self))

        @sub.on_event("startup")
        async def _start_cache():
            self.cache.start()

        return sub

    async def get(self, name: str) -> str:
        """Async version - preferred inside async code."""
        entry = await self.cache.get(name)
        if entry is None:
            raise PromptNotFoundError(f"No active prompt found for '{name}'")
        return entry["content"]

    async def get_with_meta(self, name: str) -> dict:
        """
        Async - returns both content and version_id.
        Use this when you need to log usage with the correct version.

        Returns:
            {"content": str, "version_id": int}
        """
        entry = await self.cache.get(name)
        if entry is None:
            raise PromptNotFoundError(f"No active prompt found for '{name}'")
        return {"content": entry["content"], "version_id": entry["version_id"]}

    def get_sync(self, name: str) -> str:
        """
        Sync convenience wrapper.
        Works in plain sync scripts. In async contexts (FastAPI), use aget_prompt() instead.
        """
        try:
            loop = asyncio.get_running_loop()
            # We're inside a running event loop - can't block it.
            # Return stale cache if available, otherwise raise a clear error.
            entry = self.cache._store.get(name)
            if entry:
                return entry["content"]
            raise PromptNotFoundError(
                f"No cached value for '{name}'. "
                "Inside async code, use `await aget_prompt('{name}')` instead of `get_prompt()`."
            )
        except RuntimeError:
            # No running loop - safe to block
            try:
                loop = asyncio.get_event_loop()
                return loop.run_until_complete(self.get(name))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(self.get(name))
                finally:
                    loop.close()

    def log_usage(self, prompt_name: str, version_id: int, input_text: str, output_text: str) -> None:
        self.usage_logger.log(prompt_name, version_id, input_text, output_text)


def get_instance() -> PromptManager:
    if _instance is None:
        raise RuntimeError("PromptManager has not been initialized. Call PromptManager(...) first.")
    return _instance
