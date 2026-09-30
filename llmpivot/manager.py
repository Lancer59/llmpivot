"""
PromptManager - central coordinator for llmpivot.
Initialize once at app startup; get_prompt() uses the singleton.

Production hardening in this version:
- cookie_secure param (default True) — passed through to routes layer.
- secret_key validation: CRITICAL warning + ValueError if default key used in rbac mode.
- /healthz endpoint does real liveness checks (DB query + worker task health).
- Cache pre-warm on startup: fetches all active prompts before serving traffic.
- Graceful shutdown: drains logger queue before cancelling background tasks.

Phase 1 additions:
- pivot_enabled / pivot_proactive / auto_changelog params.
- fallback_snapshot: writes prompts_fallback.json atomically on every activation + warm.
- aget_prompt_with_fallback: tries live cache, falls back to snapshot file on any error.
"""

import asyncio
import json
import logging
import os
import tempfile
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
        # Phase 1 Pivot params
        pivot_enabled: bool = False,          # master switch for all Pivot features
        pivot_proactive: bool = True,          # proactive observations; False = chat-only
        auto_changelog: bool = True,           # auto-generate changelog on version save
        pivot_model: Optional[str] = None,     # LLM model for Pivot; defaults to llm_model
        # Fallback snapshot
        fallback_snapshot: bool = True,        # write prompts_fallback.json on activation + warm
        fallback_path: Optional[str] = None,   # override path; default = next to db_path
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

        # Phase 1 Pivot
        self.pivot_enabled = pivot_enabled
        self.pivot_proactive = pivot_proactive
        self.auto_changelog = auto_changelog and pivot_enabled
        self.pivot_model = pivot_model or llm_model

        # Fallback snapshot
        self.fallback_snapshot = fallback_snapshot
        if fallback_path:
            self.fallback_path = fallback_path
        else:
            # Default: same directory as the SQLite db file
            db_dir = os.path.dirname(os.path.abspath(db_path)) if storage_type != "mongodb" else os.getcwd()
            self.fallback_path = os.path.join(db_dir, "prompts_fallback.json")

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
            "PromptManager initialised (storage=%s, tenant=%s, ttl=%ds, auth_mode=%s, "
            "cookie_secure=%s, pivot_enabled=%s, fallback_snapshot=%s)",
            storage_type,
            tenant_id,
            cache_ttl,
            self.auth_mode,
            cookie_secure,
            pivot_enabled,
            fallback_snapshot,
        )

    @property
    def has_llm(self) -> bool:
        return self.llm is not None

    @property
    def has_pivot_llm(self) -> bool:
        """True when pivot is enabled AND an LLM is configured."""
        return self.pivot_enabled and self.llm is not None

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

        # Write fallback snapshot after warm so the file is always current post-restart
        await self._write_fallback_snapshot()

    async def _write_fallback_snapshot(self) -> None:
        """
        Atomically write prompts_fallback.json with all currently active prompt contents.

        Format: {"prompt_name": "content", ...}

        Uses write-to-temp + os.replace() so the file is never partially written.
        Failures are logged as WARNING and never propagate — the snapshot is best-effort.
        """
        if not self.fallback_snapshot:
            return
        try:
            data = await self.storage.export_prompts(tenant_id=self.tenant_id)
            snapshot_dir = os.path.dirname(self.fallback_path)
            # Write atomically: temp file in same dir → os.replace (atomic on POSIX + Windows)
            fd, tmp_path = tempfile.mkstemp(dir=snapshot_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.fallback_path)
                logger.debug("Fallback snapshot written: %d prompt(s) → %s", len(data), self.fallback_path)
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning("Failed to write fallback snapshot (non-fatal): %s", exc)

    def _load_fallback_snapshot(self) -> dict:
        """
        Read prompts_fallback.json synchronously.
        Returns empty dict if file doesn't exist or is unreadable.
        Used by get_with_fallback on cold start or DB failure.
        """
        try:
            with open(self.fallback_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

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

    async def get_with_fallback(self, name: str) -> str:
        """
        Async — tries live cache/DB first; falls back to prompts_fallback.json on any error.
        Returns the prompt content string.
        Raises PromptNotFoundError only if the prompt is absent from both sources.
        """
        try:
            return await self.get(name)
        except Exception as live_exc:
            logger.warning(
                "Live prompt fetch failed for '%s' (%s); trying fallback snapshot.", name, live_exc
            )
            snapshot = self._load_fallback_snapshot()
            if name in snapshot:
                logger.info("Serving '%s' from fallback snapshot.", name)
                return snapshot[name]
            raise PromptNotFoundError(
                f"No active prompt found for '{name}' in live DB or fallback snapshot."
            ) from live_exc

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

    def schedule_snapshot_refresh(self) -> None:
        """
        Fire-and-forget: schedule a fallback snapshot refresh on the running event loop.
        Called by routes after any activation or version creation.
        Never blocks the caller.
        """
        if not self.fallback_snapshot:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._write_fallback_snapshot())
        except RuntimeError:
            # No running loop — skip silently (only happens in sync test contexts)
            pass


def get_instance() -> PromptManager:
    if _instance is None:
        raise RuntimeError("PromptManager has not been initialized. Call PromptManager(...) first.")
    return _instance
