"""
PromptManager - central coordinator for llmpivot.
Initialize once at app startup; get_prompt() uses the singleton.

Production hardening in this version:
- cookie_secure param (default True) — passed through to routes layer.
- secret_key validation: CRITICAL warning + ValueError if default key used in rbac mode.
- /healthz endpoint does real liveness checks (DB query + worker task health).
- Cache pre-warm on startup: fetches all active prompts before serving traffic.
- Graceful shutdown: drains logger queue before cancelling background tasks.
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

_DEFAULT_SECRET_KEY = "llmpivot-secret-key-change-me"


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
        auth_mode: str = "disabled",  # "disabled" | "protected" | "rbac"
        secret_key: Optional[str] = None,
        cookie_secure: bool = True,
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
        self.cookie_secure = cookie_secure
        self.log_sample_rate = log_sample_rate
        self.bootstrap_admin = bootstrap_admin
        self.bootstrap_password = bootstrap_password

        if protected_mode and auth_mode == "disabled":
            self.auth_mode = "protected"
        else:
            self.auth_mode = auth_mode

        # ---------- secret_key safety ----------
        resolved_key = secret_key or _DEFAULT_SECRET_KEY
        if resolved_key == _DEFAULT_SECRET_KEY and self.auth_mode == "rbac":
            logger.critical(
                "SECURITY: PromptManager is using the default secret_key with auth_mode='rbac'. "
                "Session tokens can be forged by anyone who knows this public default. "
                "Set a strong random secret_key before deploying to production. "
                "Example: secret_key=secrets.token_hex(32)"
            )
            raise ValueError(
                "secret_key must be set explicitly when auth_mode='rbac'. "
                "The default key 'llmpivot-secret-key-change-me' is publicly known. "
                "Pass secret_key=<your-strong-random-key> to PromptManager()."
            )
        self.secret_key = resolved_key

        # ---------- storage ----------
        if storage_type == "mongodb" or mongo_uri is not None:
            self.storage: BaseStorage = MongoStorage(
                mongo_uri=mongo_uri or "mongodb://localhost:27017",
                db_name=mongo_db_name,
            )
        else:
            self.storage = SQLiteStorage(db_path)

        try:
            self.storage.init_db_sync()
        except Exception as exc:
            raise RuntimeError(
                f"PromptManager failed to initialise storage at '{db_path}': {exc}. "
                "Check that the path is writable and the database is not corrupted."
            ) from exc

        self.cache = PromptCache(self.storage, cache_ttl, tenant_id=self.tenant_id)
        self.usage_logger = PromptLogger(self.storage, log_sample_rate, tenant_id=self.tenant_id)

        # ---------- LLM client ----------
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
            "PromptManager initialised (storage=%s, tenant=%s, ttl=%ds, auth_mode=%s, cookie_secure=%s)",
            storage_type,
            tenant_id,
            cache_ttl,
            self.auth_mode,
            cookie_secure,
        )

    @property
    def has_llm(self) -> bool:
        return self.llm is not None

    async def _bootstrap_admin(self) -> None:
        """Create the default admin user if bootstrap_admin=True and no admin exists yet."""
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
                logger.info("Bootstrap: admin user created.")
        except Exception as exc:
            logger.debug("Bootstrap admin check skipped: %s", exc)

    async def _warm_cache(self) -> None:
        """
        Pre-fetch all active prompts into the cache before serving traffic.
        Prevents cold-start DB spikes on the first request after startup/restart.
        """
        try:
            prompts = await self.storage.fetch_all_prompts(tenant_id=self.tenant_id)
            warmed = 0
            for p in prompts:
                name = p.get("name")
                if name:
                    await self.cache._fetch_and_store(name)
                    warmed += 1
            logger.info("Cache pre-warm complete: %d prompt(s) loaded.", warmed)
        except Exception as exc:
            logger.warning("Cache pre-warm failed (non-fatal): %s", exc)

    async def _shutdown(self) -> None:
        """
        Graceful shutdown: drain the usage-log queue, then stop background tasks.
        Call this from the lifespan teardown or a SIGTERM handler.
        """
        logger.info("PromptManager shutting down — draining log queue...")
        try:
            await self.usage_logger.drain()
        except Exception as exc:
            logger.warning("Error during log drain: %s", exc)

        self.usage_logger.stop()
        self.cache.stop()
        logger.info("PromptManager shutdown complete.")

    def mount_ui(self):
        """
        Return a FastAPI sub-application for mounting.

        Usage:
            app.mount("/prompts", manager.mount_ui())
        """
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from .ui.routes import build_router

        mgr = self

        @asynccontextmanager
        async def lifespan(_app):
            # Startup
            await mgr._bootstrap_admin()
            mgr.cache.start()
            mgr.usage_logger.start()
            await mgr._warm_cache()
            yield
            # Shutdown — drain before tasks are cancelled
            await mgr._shutdown()

        sub = FastAPI(lifespan=lifespan)
        sub.include_router(build_router(self))

        @sub.get("/healthz")
        async def _healthz():
            """
            Liveness probe suitable for load-balancer and k8s health checks.
            Returns 200 when healthy, 503 when any critical component is degraded.
            """
            issues = []

            # 1. DB reachability
            try:
                await mgr.storage.fetch_all_prompts(tenant_id=mgr.tenant_id)
            except Exception as exc:
                issues.append(f"storage_error: {exc}")

            # 2. Cache background worker
            cache_task = mgr.cache._task
            if cache_task is not None and cache_task.done():
                task_exc = None if cache_task.cancelled() else cache_task.exception()
                issues.append(f"cache_worker_dead: {task_exc}")

            # 3. Logger background worker
            log_task = mgr.usage_logger._worker_task
            if log_task is not None and log_task.done():
                task_exc = None if log_task.cancelled() else log_task.exception()
                issues.append(f"logger_worker_dead: {task_exc}")

            payload = {
                "status": "ok" if not issues else "degraded",
                "cache_size": len(mgr.cache._store),
                "log_queue_depth": mgr.usage_logger._queue.qsize(),
                "cache_worker_alive": cache_task is not None and not cache_task.done(),
                "logger_worker_alive": log_task is not None and not log_task.done(),
            }
            if issues:
                payload["issues"] = issues
                return JSONResponse(payload, status_code=503)
            return payload

        return sub

    # ------------------------------------------------------------------
    # Public prompt API
    # ------------------------------------------------------------------

    async def get(self, name: str) -> str:
        """Async — preferred inside async code."""
        entry = await self.cache.get(name)
        if entry is None:
            raise PromptNotFoundError(f"No active prompt found for '{name}'")
        return entry["content"]

    async def get_with_meta(self, name: str) -> dict:
        """
        Async — returns both content and version_id.
        Returns: {"content": str, "version_id": int/str}
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
                f"Inside async code, use `await aget_prompt('{name}')` instead of `get_prompt()`."
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
