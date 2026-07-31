"""
PromptManager - central coordinator for llmpivot.
Initialize once at app startup; get_prompt() uses the singleton.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from .storage import BaseStorage, SQLiteStorage, MongoStorage
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
        storage_type: str = "sqlite",
        mongo_uri: Optional[str] = None,
        mongo_db_name: str = "llmpivot",
        tenant_id: str = "default",
        cache_ttl: int = 5,
        protected_mode: bool = False,
        admin_password: Optional[str] = None,
        auth_mode: str = "disabled",  # "disabled", "protected", "rbac"
        secret_key: Optional[str] = None,
        log_sample_rate: float = 1.0,
        bootstrap_admin: bool = False,
        bootstrap_password: Optional[str] = None,
        # LLM suggester (all optional)
        llm_url: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        llm_model: str = "gpt-3.5-turbo",
        llm_suggester_prompt: Optional[str] = None,
    ):
        global _instance

        self.db_path = db_path
        self.tenant_id = tenant_id
        self.cache_ttl = cache_ttl
        self.protected_mode = protected_mode
        self.admin_password = admin_password
        self.secret_key = secret_key or "llmpivot-secret-key-change-me"
        self.log_sample_rate = log_sample_rate
        self.bootstrap_admin = bootstrap_admin
        self.bootstrap_password = bootstrap_password

        if protected_mode and auth_mode == "disabled":
            self.auth_mode = "protected"
        else:
            self.auth_mode = auth_mode

        # Initialize storage backend
        if storage_type == "mongodb" or mongo_uri is not None:
            self.storage: BaseStorage = MongoStorage(
                mongo_uri=mongo_uri or "mongodb://localhost:27017",
                db_name=mongo_db_name,
            )
        else:
            self.storage = SQLiteStorage(db_path)

        self.storage.init_db_sync()

        self.cache = PromptCache(self.storage, cache_ttl, tenant_id=self.tenant_id)
        self.usage_logger = PromptLogger(self.storage, log_sample_rate, tenant_id=self.tenant_id)

        # LLM client
        self.llm: Optional[LLMClient] = None
        if llm_url:
            self.llm = LLMClient(
                url=llm_url,
                api_key=llm_api_key or "",
                model=llm_model,
                system_prompt=llm_suggester_prompt,
            )

        _instance = self
        logger.info(
            "PromptManager initialized (storage=%s, tenant=%s, ttl=%ds, auth_mode=%s)",
            storage_type,
            tenant_id,
            cache_ttl,
            self.auth_mode,
        )

    @property
    def has_llm(self) -> bool:
        return self.llm is not None

    async def _bootstrap_admin(self) -> None:
        try:
            if not self.bootstrap_admin:
                return

            admin_user = await self.storage.get_user("admin", tenant_id=self.tenant_id)
            if not admin_user:
                from .auth import hash_password
                password = self.bootstrap_password or "admin"
                pwd_hash = hash_password(password)
                await self.storage.create_user(
                    username="admin",
                    password_hash=pwd_hash,
                    role="admin",
                    email="admin@llmpivot.local",
                    tenant_id=self.tenant_id,
                )
                logger.info("Admin user 'admin' created successfully.")
        except Exception as exc:
            logger.debug("Bootstrap admin check skipped: %s", exc)

    def mount_ui(self):
        """
        Return a FastAPI sub-application for mounting.

        Usage:
            app.mount("/prompts", manager.mount_ui())
        """
        from fastapi import FastAPI
        from .ui.routes import build_router

        @asynccontextmanager
        async def lifespan(_app):
            await self._bootstrap_admin()
            self.cache.start()
            self.usage_logger.start()
            yield

        sub = FastAPI(lifespan=lifespan)
        sub.include_router(build_router(self))

        @sub.get("/healthz")
        async def _healthz():
            return {"status": "ok"}

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
        Returns:
            {"content": str, "version_id": int/str}
        """
        entry = await self.cache.get(name)
        if entry is None:
            raise PromptNotFoundError(f"No active prompt found for '{name}'")
        return {"content": entry["content"], "version_id": entry["version_id"]}

    def get_sync(self, name: str) -> str:
        """Sync convenience wrapper."""
        try:
            loop = asyncio.get_running_loop()
            entry = self.cache._store.get(name)
            if entry:
                return entry["content"]
            raise PromptNotFoundError(
                f"No cached value for '{name}'. "
                "Inside async code, use `await aget_prompt('{name}')` instead of `get_prompt()`."
            )
        except RuntimeError:
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
