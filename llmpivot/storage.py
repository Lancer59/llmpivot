"""
Pluggable Storage Layer for llmpivot.
Supports SQLite (with WAL mode for high concurrency) and MongoDB.

Production changes in this version:
- is_deleted column on prompts table — soft-deleted prompts are filtered from all list queries.
- audit_log table — records admin actions (create_version, activate, delete, import, user_create).
- MongoStorage.get_user accepts tenant_id, matching the BaseStorage interface.
- insert_logs_batch emits a WARNING when a log entry is dropped (prompt not found in DB).
"""

from abc import ABC, abstractmethod
import asyncio
import logging
import sqlite3
import time
from typing import Optional, List, Dict, Any

logger = logging.getLogger("llmpivot.storage")

try:
    import aiosqlite
except ImportError:
    aiosqlite = None

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = None


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseStorage(ABC):
    @abstractmethod
    def init_db_sync(self) -> None:
        pass

    @abstractmethod
    async def fetch_active_version(self, name: str, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    async def fetch_all_prompts(self, tenant_id: str = "default") -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    async def fetch_prompt_versions(self, name: str, tenant_id: str = "default") -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    async def fetch_version_by_id(self, version_id: Any, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    async def create_version(
        self,
        name: str,
        content: str,
        created_by: str,
        tag: Optional[str],
        set_active: bool,
        tenant_id: str = "default",
    ) -> int:
        pass

    @abstractmethod
    async def set_active_version(self, version_id: Any, tenant_id: str = "default") -> bool:
        pass

    @abstractmethod
    async def soft_delete_prompt(self, name: str, tenant_id: str = "default") -> None:
        pass

    @abstractmethod
    async def insert_logs_batch(self, logs: List[Dict[str, Any]]) -> None:
        pass

    @abstractmethod
    async def fetch_logs(self, prompt_name: Optional[str] = None, limit: int = 100, tenant_id: str = "default") -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    async def export_prompts(self, tenant_id: str = "default") -> Dict[str, str]:
        pass

    @abstractmethod
    async def import_prompts(self, data: Dict[str, str], imported_by: str = "import", tenant_id: str = "default") -> Dict[str, List[str]]:
        pass

    @abstractmethod
    async def get_user(self, username: str, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    async def create_user(
        self, username: str, password_hash: str, role: str = "editor", email: str = "", tenant_id: str = "default"
    ) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def fetch_users(self, tenant_id: str = "default") -> List[Dict[str, Any]]:
        pass

    # Audit log — optional; default implementation is a no-op so backends that
    # haven't added it yet don't break.
    async def insert_audit_log(
        self,
        action: str,
        performed_by: str,
        prompt_name: Optional[str] = None,
        version_id: Optional[Any] = None,
        detail: Optional[str] = None,
        tenant_id: str = "default",
    ) -> None:
        pass

    async def fetch_audit_logs(
        self, limit: int = 100, tenant_id: str = "default"
    ) -> List[Dict[str, Any]]:
        return []

    # ------------------------------------------------------------------
    # Prompt metadata (Phase 1 Pivot)
    # ------------------------------------------------------------------

    async def get_prompt_metadata(self, prompt_name: str, tenant_id: str = "default") -> Dict[str, Any]:
        return {}

    async def upsert_prompt_metadata(self, prompt_name: str, fields: Dict[str, Any], tenant_id: str = "default") -> None:
        pass

    # ------------------------------------------------------------------
    # Prompt changelog (Phase 1 Pivot)
    # ------------------------------------------------------------------

    async def get_changelog(self, prompt_name: str, tenant_id: str = "default") -> List[Dict[str, Any]]:
        return []

    async def get_changelog_for_version(self, version_id: Any, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        return None

    async def upsert_changelog_entry(
        self,
        version_id: Any,
        entry: str,
        generated_by: str = "user",
        created_by: str = "",
        tenant_id: str = "default",
    ) -> None:
        pass

    # ------------------------------------------------------------------
    # Application context (Phase 1 Pivot)
    # ------------------------------------------------------------------

    async def get_app_context(self, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        return None

    async def save_app_context(self, content: str, created_by: str = "", tenant_id: str = "default") -> None:
        pass

    # ------------------------------------------------------------------
    # Hierarchy helpers (Phase 1 Pivot)
    # ------------------------------------------------------------------

    async def fetch_prompt_children(self, prompt_name: str, tenant_id: str = "default") -> List[Dict[str, Any]]:
        """Return all prompts whose parent_prompt_id points to prompt_name."""
        return []

    async def fetch_all_prompts_with_metadata(self, tenant_id: str = "default") -> List[Dict[str, Any]]:
        """fetch_all_prompts enriched with prompt_type and parent info for the tree view."""
        return []


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

class SQLiteStorage(BaseStorage):
    def __init__(self, db_path: str = "prompts.db"):
        self.db_path = db_path

    def init_db_sync(self) -> None:
        """Synchronous bootstrap — enables WAL mode and creates/updates tables."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS prompts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            tenant_id  TEXT DEFAULT 'default',
            is_deleted INTEGER DEFAULT 0,
            UNIQUE(tenant_id, name)
        );

        CREATE TABLE IF NOT EXISTS prompt_versions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id      INTEGER NOT NULL REFERENCES prompts(id),
            tenant_id      TEXT DEFAULT 'default',
            content        TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
            created_by     TEXT,
            tag            TEXT CHECK(tag IN ('prod', 'staging', 'experiment')),
            is_active      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS prompt_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id  INTEGER NOT NULL REFERENCES prompts(id),
            version_id INTEGER NOT NULL REFERENCES prompt_versions(id),
            tenant_id  TEXT DEFAULT 'default',
            input      TEXT,
            output     TEXT,
            timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            role          TEXT DEFAULT 'editor',
            tenant_id     TEXT DEFAULT 'default',
            is_active     INTEGER DEFAULT 1,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            action       TEXT NOT NULL,
            performed_by TEXT NOT NULL,
            prompt_name  TEXT,
            version_id   TEXT,
            detail       TEXT,
            tenant_id    TEXT DEFAULT 'default',
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS prompt_metadata (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id         INTEGER NOT NULL REFERENCES prompts(id),
            tenant_id         TEXT DEFAULT 'default',
            purpose           TEXT DEFAULT '',
            prompt_type       TEXT DEFAULT 'unclassified',
            parent_prompt_id  INTEGER REFERENCES prompts(id),
            feature_area      TEXT DEFAULT '',
            called_from       TEXT DEFAULT '',
            model_used        TEXT DEFAULT '',
            input_variables   TEXT DEFAULT '[]',
            owner             TEXT DEFAULT '',
            sensitivity       TEXT DEFAULT 'low',
            notes             TEXT DEFAULT '',
            updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(prompt_id, tenant_id)
        );

        CREATE TABLE IF NOT EXISTS prompt_changelog (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id     INTEGER NOT NULL REFERENCES prompt_versions(id),
            tenant_id      TEXT DEFAULT 'default',
            entry          TEXT NOT NULL,
            generated_by   TEXT DEFAULT 'user',
            created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
            created_by     TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS app_context (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id  TEXT DEFAULT 'default',
            content    TEXT NOT NULL DEFAULT '',
            version    INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT DEFAULT ''
        );
        """)

        # Auto-migrations for existing databases
        cur = conn.cursor()
        for table, col, col_def in [
            ("prompts", "tenant_id", "TEXT DEFAULT 'default'"),
            ("prompts", "is_deleted", "INTEGER DEFAULT 0"),
            ("prompt_versions", "tenant_id", "TEXT DEFAULT 'default'"),
            ("prompt_logs", "tenant_id", "TEXT DEFAULT 'default'"),
        ]:
            cur.execute(f"PRAGMA table_info({table});")
            cols = [row[1] for row in cur.fetchall()]
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def};")
                except sqlite3.OperationalError:
                    pass

        # Phase 1 Pivot migrations — add new tables columns if upgrading an existing DB
        # prompt_metadata: add any missing columns (table created above via CREATE IF NOT EXISTS)
        cur.execute("PRAGMA table_info(prompt_metadata);")
        pm_cols = [row[1] for row in cur.fetchall()]
        for col, col_def in [
            ("purpose", "TEXT DEFAULT ''"),
            ("prompt_type", "TEXT DEFAULT 'unclassified'"),
            ("parent_prompt_id", "INTEGER"),
            ("feature_area", "TEXT DEFAULT ''"),
            ("called_from", "TEXT DEFAULT ''"),
            ("model_used", "TEXT DEFAULT ''"),
            ("input_variables", "TEXT DEFAULT '[]'"),
            ("owner", "TEXT DEFAULT ''"),
            ("sensitivity", "TEXT DEFAULT 'low'"),
            ("notes", "TEXT DEFAULT ''"),
            ("updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ]:
            if col not in pm_cols:
                try:
                    conn.execute(f"ALTER TABLE prompt_metadata ADD COLUMN {col} {col_def};")
                except sqlite3.OperationalError:
                    pass

        conn.commit()
        conn.close()

    def _run_sqlite(self, func, *args, **kwargs):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            res = func(conn, *args, **kwargs)
            conn.commit()
            return res
        finally:
            conn.close()

    async def _async_exec(self, func, *args, **kwargs):
        if aiosqlite is not None:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                return await func(db, *args, **kwargs)
        else:
            return await asyncio.to_thread(self._run_sqlite, func, *args, **kwargs)

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    async def fetch_active_version(self, name: str, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        if aiosqlite is not None:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT pv.id, pv.content, pv.version_number, pv.tag, pv.created_by, pv.created_at
                    FROM prompt_versions pv
                    JOIN prompts p ON p.id = pv.prompt_id
                    WHERE p.name = ? AND p.tenant_id = ? AND pv.tenant_id = ?
                      AND pv.is_active = 1 AND p.is_deleted = 0
                    LIMIT 1
                    """,
                    (name, tenant_id, tenant_id),
                ) as cur:
                    row = await cur.fetchone()
                    return dict(row) if row else None
        else:
            def _fn(conn):
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT pv.id, pv.content, pv.version_number, pv.tag, pv.created_by, pv.created_at
                    FROM prompt_versions pv
                    JOIN prompts p ON p.id = pv.prompt_id
                    WHERE p.name = ? AND p.tenant_id = ? AND pv.tenant_id = ?
                      AND pv.is_active = 1 AND p.is_deleted = 0
                    LIMIT 1
                    """,
                    (name, tenant_id, tenant_id),
                )
                row = cur.fetchone()
                return dict(row) if row else None
            return await asyncio.to_thread(self._run_sqlite, _fn)

    async def fetch_all_prompts(self, tenant_id: str = "default") -> List[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                """
                SELECT p.id, p.name,
                       pv.version_number AS active_version,
                       pv.created_by     AS last_edited_by,
                       pv.created_at     AS last_updated
                FROM prompts p
                LEFT JOIN prompt_versions pv
                    ON pv.prompt_id = p.id AND pv.is_active = 1 AND pv.tenant_id = ?
                WHERE p.tenant_id = ? AND p.is_deleted = 0
                ORDER BY p.name
                """,
                (tenant_id, tenant_id),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def fetch_prompt_versions(self, name: str, tenant_id: str = "default") -> List[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                """
                SELECT pv.id, pv.version_number, pv.content, pv.created_at,
                       pv.created_by, pv.tag, pv.is_active
                FROM prompt_versions pv
                JOIN prompts p ON p.id = pv.prompt_id
                WHERE p.name = ? AND p.tenant_id = ? AND pv.tenant_id = ?
                ORDER BY pv.version_number DESC
                """,
                (name, tenant_id, tenant_id),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def fetch_version_by_id(self, version_id: Any, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        try:
            v_id = int(version_id)
        except (ValueError, TypeError):
            return None
        def _fn(conn):
            cur = conn.cursor()
            cur.execute("SELECT * FROM prompt_versions WHERE id = ? AND tenant_id = ?", (v_id, tenant_id))
            row = cur.fetchone()
            return dict(row) if row else None
        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def create_version(
        self,
        name: str,
        content: str,
        created_by: str,
        tag: Optional[str],
        set_active: bool,
        tenant_id: str = "default",
    ) -> int:
        def _fn(conn):
            cur = conn.cursor()
            # Ensure prompt row exists (restore if previously deleted)
            cur.execute(
                "INSERT OR IGNORE INTO prompts (name, tenant_id, is_deleted) VALUES (?, ?, 0)",
                (name, tenant_id),
            )
            # Un-delete if it was soft-deleted
            cur.execute(
                "UPDATE prompts SET is_deleted = 0 WHERE name = ? AND tenant_id = ?",
                (name, tenant_id),
            )
            cur.execute("SELECT id FROM prompts WHERE name = ? AND tenant_id = ?", (name, tenant_id))
            prompt_id = cur.fetchone()[0]

            cur.execute(
                "SELECT COALESCE(MAX(version_number), 0) FROM prompt_versions WHERE prompt_id = ?",
                (prompt_id,),
            )
            next_version = cur.fetchone()[0] + 1

            if tag == "prod":
                cur.execute(
                    "UPDATE prompt_versions SET tag = NULL WHERE prompt_id = ? AND tag = 'prod'",
                    (prompt_id,),
                )

            if set_active:
                cur.execute(
                    "UPDATE prompt_versions SET is_active = 0 WHERE prompt_id = ?",
                    (prompt_id,),
                )

            cur.execute(
                """
                INSERT INTO prompt_versions (prompt_id, tenant_id, content, version_number, created_by, tag, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (prompt_id, tenant_id, content, next_version, created_by, tag, 1 if set_active else 0),
            )
            return next_version

        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def set_active_version(self, version_id: Any, tenant_id: str = "default") -> bool:
        try:
            v_id = int(version_id)
        except (ValueError, TypeError):
            return False

        def _fn(conn):
            cur = conn.cursor()
            cur.execute("SELECT prompt_id FROM prompt_versions WHERE id = ? AND tenant_id = ?", (v_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return False
            prompt_id = row[0]
            cur.execute("UPDATE prompt_versions SET is_active = 0 WHERE prompt_id = ? AND tenant_id = ?", (prompt_id, tenant_id))
            cur.execute("UPDATE prompt_versions SET is_active = 1 WHERE id = ? AND tenant_id = ?", (v_id, tenant_id))
            return True

        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def soft_delete_prompt(self, name: str, tenant_id: str = "default") -> None:
        """Mark prompt as deleted and deactivate all versions."""
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM prompts WHERE name = ? AND tenant_id = ?",
                (name, tenant_id),
            )
            row = cur.fetchone()
            if row:
                prompt_id = row[0]
                cur.execute("UPDATE prompt_versions SET is_active = 0 WHERE prompt_id = ?", (prompt_id,))
                cur.execute("UPDATE prompts SET is_deleted = 1 WHERE id = ?", (prompt_id,))

        await asyncio.to_thread(self._run_sqlite, _fn)

    # ------------------------------------------------------------------
    # Usage logs
    # ------------------------------------------------------------------

    async def insert_logs_batch(self, logs: List[Dict[str, Any]]) -> None:
        if not logs:
            return

        def _fn(conn):
            cur = conn.cursor()
            dropped = 0
            for log in logs:
                prompt_name = log.get("prompt_name")
                tenant_id = log.get("tenant_id", "default")
                version_id = log.get("version_id")
                input_text = log.get("input_text", "")
                output_text = log.get("output_text", "")

                # Truncate oversized fields to avoid unbounded storage growth
                input_text = (input_text or "")[:10_000]
                output_text = (output_text or "")[:10_000]

                cur.execute(
                    "SELECT id FROM prompts WHERE name = ? AND tenant_id = ?",
                    (prompt_name, tenant_id),
                )
                row = cur.fetchone()
                if not row:
                    dropped += 1
                    continue

                prompt_id = row[0]
                try:
                    v_id = int(version_id)
                except (ValueError, TypeError):
                    v_id = 0
                cur.execute(
                    "INSERT INTO prompt_logs (prompt_id, version_id, tenant_id, input, output) VALUES (?, ?, ?, ?, ?)",
                    (prompt_id, v_id, tenant_id, input_text, output_text),
                )
            return dropped

        dropped = await asyncio.to_thread(self._run_sqlite, _fn)
        if dropped:
            logger.warning(
                "insert_logs_batch: %d log entr%s dropped because the prompt name was not found "
                "in the database. Check that tenant_id and prompt_name match what was used when "
                "the prompt was created.",
                dropped,
                "y was" if dropped == 1 else "ies were",
            )

    async def fetch_logs(self, prompt_name: Optional[str] = None, limit: int = 100, tenant_id: str = "default") -> List[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            if prompt_name:
                cur.execute(
                    """
                    SELECT pl.id, p.name AS prompt_name, pv.version_number,
                           pl.input, pl.output, pl.timestamp
                    FROM prompt_logs pl
                    JOIN prompts p ON p.id = pl.prompt_id
                    JOIN prompt_versions pv ON pv.id = pl.version_id
                    WHERE p.name = ? AND p.tenant_id = ? AND pl.tenant_id = ?
                    ORDER BY pl.timestamp DESC LIMIT ?
                    """,
                    (prompt_name, tenant_id, tenant_id, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT pl.id, p.name AS prompt_name, pv.version_number,
                           pl.input, pl.output, pl.timestamp
                    FROM prompt_logs pl
                    JOIN prompts p ON p.id = pl.prompt_id
                    JOIN prompt_versions pv ON pv.id = pl.version_id
                    WHERE p.tenant_id = ? AND pl.tenant_id = ?
                    ORDER BY pl.timestamp DESC LIMIT ?
                    """,
                    (tenant_id, tenant_id, limit),
                )
            rows = cur.fetchall()
            return [dict(r) for r in rows]

        return await asyncio.to_thread(self._run_sqlite, _fn)

    # ------------------------------------------------------------------
    # Export / Import
    # ------------------------------------------------------------------

    async def export_prompts(self, tenant_id: str = "default") -> Dict[str, str]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                """
                SELECT p.name, pv.content
                FROM prompt_versions pv
                JOIN prompts p ON p.id = pv.prompt_id
                WHERE pv.is_active = 1 AND p.tenant_id = ? AND pv.tenant_id = ?
                  AND p.is_deleted = 0
                ORDER BY p.name
                """,
                (tenant_id, tenant_id),
            )
            rows = cur.fetchall()
            return {row["name"]: row["content"] for row in rows}

        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def import_prompts(self, data: Dict[str, str], imported_by: str = "import", tenant_id: str = "default") -> Dict[str, List[str]]:
        created, updated = [], []
        for name, content in data.items():
            def _check(conn, _name=name):
                cur = conn.cursor()
                cur.execute("SELECT id FROM prompts WHERE name = ? AND tenant_id = ?", (_name, tenant_id))
                return cur.fetchone() is not None

            exists = await asyncio.to_thread(self._run_sqlite, _check)
            await self.create_version(name, content, imported_by, None, set_active=True, tenant_id=tenant_id)
            if exists:
                updated.append(name)
            else:
                created.append(name)
        return {"created": created, "updated": updated}

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def get_user(self, username: str, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE username = ? AND tenant_id = ?", (username, tenant_id))
            row = cur.fetchone()
            return dict(row) if row else None

        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def create_user(
        self, username: str, password_hash: str, role: str = "editor", email: str = "", tenant_id: str = "default"
    ) -> Dict[str, Any]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO users (username, email, password_hash, role, tenant_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, email, password_hash, role, tenant_id),
            )

        await asyncio.to_thread(self._run_sqlite, _fn)
        return await self.get_user(username, tenant_id=tenant_id)

    async def fetch_users(self, tenant_id: str = "default") -> List[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT id, username, email, role, tenant_id, is_active, created_at FROM users WHERE tenant_id = ? ORDER BY username",
                (tenant_id,),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]

        return await asyncio.to_thread(self._run_sqlite, _fn)

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def insert_audit_log(
        self,
        action: str,
        performed_by: str,
        prompt_name: Optional[str] = None,
        version_id: Optional[Any] = None,
        detail: Optional[str] = None,
        tenant_id: str = "default",
    ) -> None:
        def _fn(conn):
            conn.execute(
                """
                INSERT INTO audit_log (action, performed_by, prompt_name, version_id, detail, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (action, performed_by, prompt_name, str(version_id) if version_id is not None else None, detail, tenant_id),
            )
        try:
            await asyncio.to_thread(self._run_sqlite, _fn)
        except Exception as exc:
            logger.warning("Failed to write audit log: %s", exc)

    async def fetch_audit_logs(self, limit: int = 100, tenant_id: str = "default") -> List[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, action, performed_by, prompt_name, version_id, detail, tenant_id, created_at
                FROM audit_log WHERE tenant_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (tenant_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]
        return await asyncio.to_thread(self._run_sqlite, _fn)

    # ------------------------------------------------------------------
    # Prompt metadata — Phase 1 Pivot
    # ------------------------------------------------------------------

    async def get_prompt_metadata(self, prompt_name: str, tenant_id: str = "default") -> Dict[str, Any]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                """
                SELECT pm.*
                FROM prompt_metadata pm
                JOIN prompts p ON p.id = pm.prompt_id
                WHERE p.name = ? AND p.tenant_id = ? AND pm.tenant_id = ?
                """,
                (prompt_name, tenant_id, tenant_id),
            )
            row = cur.fetchone()
            if not row:
                return {}
            d = dict(row)
            # Resolve parent_prompt_id → parent_prompt_name
            if d.get("parent_prompt_id"):
                cur.execute("SELECT name FROM prompts WHERE id = ?", (d["parent_prompt_id"],))
                pr = cur.fetchone()
                d["parent_prompt_name"] = pr["name"] if pr else None
            else:
                d["parent_prompt_name"] = None
            return d
        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def upsert_prompt_metadata(self, prompt_name: str, fields: Dict[str, Any], tenant_id: str = "default") -> None:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM prompts WHERE name = ? AND tenant_id = ?",
                (prompt_name, tenant_id),
            )
            row = cur.fetchone()
            if not row:
                return
            prompt_id = row[0]

            # Resolve parent name → id if provided as a string
            parent_id = fields.get("parent_prompt_id")
            if isinstance(parent_id, str) and parent_id:
                cur.execute("SELECT id FROM prompts WHERE name = ? AND tenant_id = ?", (parent_id, tenant_id))
                pr = cur.fetchone()
                parent_id = pr[0] if pr else None
            elif not parent_id:
                parent_id = None

            # Check if row exists
            cur.execute(
                "SELECT id FROM prompt_metadata WHERE prompt_id = ? AND tenant_id = ?",
                (prompt_id, tenant_id),
            )
            existing = cur.fetchone()

            allowed = {"purpose", "prompt_type", "feature_area", "called_from",
                       "model_used", "input_variables", "owner", "sensitivity", "notes"}
            safe_fields = {k: v for k, v in fields.items() if k in allowed}
            safe_fields["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

            if existing:
                set_clause = ", ".join(f"{k} = ?" for k in safe_fields)
                set_clause += ", parent_prompt_id = ?"
                vals = list(safe_fields.values()) + [parent_id, prompt_id, tenant_id]
                conn.execute(
                    f"UPDATE prompt_metadata SET {set_clause} WHERE prompt_id = ? AND tenant_id = ?",
                    vals,
                )
            else:
                safe_fields["parent_prompt_id"] = parent_id
                safe_fields["prompt_id"] = prompt_id
                safe_fields["tenant_id"] = tenant_id
                cols = ", ".join(safe_fields.keys())
                placeholders = ", ".join("?" for _ in safe_fields)
                conn.execute(
                    f"INSERT INTO prompt_metadata ({cols}) VALUES ({placeholders})",
                    list(safe_fields.values()),
                )
        await asyncio.to_thread(self._run_sqlite, _fn)

    # ------------------------------------------------------------------
    # Prompt changelog — Phase 1 Pivot
    # ------------------------------------------------------------------

    async def get_changelog(self, prompt_name: str, tenant_id: str = "default") -> List[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                """
                SELECT pc.id, pc.version_id, pc.entry, pc.generated_by,
                       pc.created_by, pc.created_at, pv.version_number
                FROM prompt_changelog pc
                JOIN prompt_versions pv ON pv.id = pc.version_id
                JOIN prompts p ON p.id = pv.prompt_id
                WHERE p.name = ? AND p.tenant_id = ? AND pc.tenant_id = ?
                ORDER BY pv.version_number DESC
                """,
                (prompt_name, tenant_id, tenant_id),
            )
            return [dict(r) for r in cur.fetchall()]
        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def get_changelog_for_version(self, version_id: Any, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        try:
            v_id = int(version_id)
        except (ValueError, TypeError):
            return None
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM prompt_changelog WHERE version_id = ? AND tenant_id = ?",
                (v_id, tenant_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def upsert_changelog_entry(
        self,
        version_id: Any,
        entry: str,
        generated_by: str = "user",
        created_by: str = "",
        tenant_id: str = "default",
    ) -> None:
        try:
            v_id = int(version_id)
        except (ValueError, TypeError):
            return
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM prompt_changelog WHERE version_id = ? AND tenant_id = ?",
                (v_id, tenant_id),
            )
            existing = cur.fetchone()
            if existing:
                conn.execute(
                    "UPDATE prompt_changelog SET entry = ?, generated_by = ?, created_by = ? WHERE version_id = ? AND tenant_id = ?",
                    (entry, generated_by, created_by, v_id, tenant_id),
                )
            else:
                conn.execute(
                    "INSERT INTO prompt_changelog (version_id, tenant_id, entry, generated_by, created_by) VALUES (?, ?, ?, ?, ?)",
                    (v_id, tenant_id, entry, generated_by, created_by),
                )
        await asyncio.to_thread(self._run_sqlite, _fn)

    # ------------------------------------------------------------------
    # Application context — Phase 1 Pivot
    # ------------------------------------------------------------------

    async def get_app_context(self, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM app_context WHERE tenant_id = ? ORDER BY version DESC LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def save_app_context(self, content: str, created_by: str = "", tenant_id: str = "default") -> None:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) FROM app_context WHERE tenant_id = ?",
                (tenant_id,),
            )
            next_v = cur.fetchone()[0] + 1
            conn.execute(
                "INSERT INTO app_context (tenant_id, content, version, created_by) VALUES (?, ?, ?, ?)",
                (tenant_id, content, next_v, created_by),
            )
        await asyncio.to_thread(self._run_sqlite, _fn)

    # ------------------------------------------------------------------
    # Hierarchy helpers — Phase 1 Pivot
    # ------------------------------------------------------------------

    async def fetch_prompt_children(self, prompt_name: str, tenant_id: str = "default") -> List[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            # Get parent prompt id
            cur.execute("SELECT id FROM prompts WHERE name = ? AND tenant_id = ?", (prompt_name, tenant_id))
            row = cur.fetchone()
            if not row:
                return []
            parent_id = row[0]
            cur.execute(
                """
                SELECT p.name, pm.prompt_type, pm.purpose,
                       pv.version_number AS active_version, pv.created_by AS last_edited_by, pv.created_at AS last_updated
                FROM prompt_metadata pm
                JOIN prompts p ON p.id = pm.prompt_id
                LEFT JOIN prompt_versions pv ON pv.prompt_id = p.id AND pv.is_active = 1 AND pv.tenant_id = ?
                WHERE pm.parent_prompt_id = ? AND pm.tenant_id = ? AND p.is_deleted = 0
                ORDER BY p.name
                """,
                (tenant_id, parent_id, tenant_id),
            )
            return [dict(r) for r in cur.fetchall()]
        return await asyncio.to_thread(self._run_sqlite, _fn)

    async def fetch_all_prompts_with_metadata(self, tenant_id: str = "default") -> List[Dict[str, Any]]:
        def _fn(conn):
            cur = conn.cursor()
            cur.execute(
                """
                SELECT p.id, p.name,
                       pv.version_number AS active_version,
                       pv.created_by     AS last_edited_by,
                       pv.created_at     AS last_updated,
                       COALESCE(pm.prompt_type, 'unclassified') AS prompt_type,
                       pm.purpose,
                       pm.parent_prompt_id,
                       pm.sensitivity,
                       pp.name AS parent_name
                FROM prompts p
                LEFT JOIN prompt_versions pv
                    ON pv.prompt_id = p.id AND pv.is_active = 1 AND pv.tenant_id = ?
                LEFT JOIN prompt_metadata pm
                    ON pm.prompt_id = p.id AND pm.tenant_id = ?
                LEFT JOIN prompts pp
                    ON pp.id = pm.parent_prompt_id
                WHERE p.tenant_id = ? AND p.is_deleted = 0
                ORDER BY p.name
                """,
                (tenant_id, tenant_id, tenant_id),
            )
            return [dict(r) for r in cur.fetchall()]
        return await asyncio.to_thread(self._run_sqlite, _fn)

    # ------------------------------------------------------------------
    # Tenant migration helper
    # ------------------------------------------------------------------

    async def migrate_missing_tenant_ids(self, tenant_id: str = "default") -> Dict[str, int]:
        def _fn(conn):
            cur = conn.cursor()
            p = cur.execute(
                "UPDATE prompts SET tenant_id = ? WHERE tenant_id IS NULL OR tenant_id = ''",
                (tenant_id,),
            ).rowcount
            pv = cur.execute(
                "UPDATE prompt_versions SET tenant_id = ? WHERE tenant_id IS NULL OR tenant_id = ''",
                (tenant_id,),
            ).rowcount
            u = cur.execute(
                "UPDATE users SET tenant_id = ? WHERE tenant_id IS NULL OR tenant_id = ''",
                (tenant_id,),
            ).rowcount
            return {"prompts": p, "prompt_versions": pv, "users": u}

        return await asyncio.to_thread(self._run_sqlite, _fn)


# ---------------------------------------------------------------------------
# MongoDB implementation
# ---------------------------------------------------------------------------

class MongoStorage(BaseStorage):
    def __init__(self, mongo_uri: str = "mongodb://localhost:27017", db_name: str = "llmpivot"):
        if AsyncIOMotorClient is None:
            raise ImportError(
                "MongoDB support requires the 'motor' library. "
                "Install it with `pip install motor` or `pip install llmpivot[mongo]`."
            )
        self.client = AsyncIOMotorClient(mongo_uri)
        self.db = self.client[db_name]

    def init_db_sync(self) -> None:
        """No-op for Mongo; indexes are created lazily or via a separate migration."""
        pass

    async def fetch_active_version(self, name: str, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        doc = await self.db.prompt_versions.find_one(
            {"prompt_name": name, "tenant_id": tenant_id, "is_active": True}
        )
        if not doc:
            return None
        return {
            "id": str(doc["_id"]),
            "content": doc["content"],
            "version_number": doc["version_number"],
            "tag": doc.get("tag"),
            "created_by": doc.get("created_by"),
            "created_at": doc.get("created_at"),
        }

    async def fetch_all_prompts(self, tenant_id: str = "default") -> List[Dict[str, Any]]:
        prompts = await self.db.prompts.find(
            {"tenant_id": tenant_id, "is_deleted": {"$ne": True}}
        ).to_list(length=1000)
        results = []
        for p in prompts:
            name = p["name"]
            active = await self.db.prompt_versions.find_one(
                {"prompt_name": name, "tenant_id": tenant_id, "is_active": True}
            )
            results.append({
                "id": str(p["_id"]),
                "name": name,
                "active_version": active["version_number"] if active else None,
                "last_edited_by": active.get("created_by") if active else None,
                "last_updated": active.get("created_at") if active else None,
            })
        return sorted(results, key=lambda x: x["name"])

    async def fetch_prompt_versions(self, name: str, tenant_id: str = "default") -> List[Dict[str, Any]]:
        cursor = self.db.prompt_versions.find(
            {"prompt_name": name, "tenant_id": tenant_id}
        ).sort("version_number", -1)
        docs = await cursor.to_list(length=1000)
        return [
            {
                "id": str(d["_id"]),
                "version_number": d["version_number"],
                "content": d["content"],
                "created_at": d.get("created_at"),
                "created_by": d.get("created_by"),
                "tag": d.get("tag"),
                "is_active": d.get("is_active", False),
            }
            for d in docs
        ]

    async def fetch_version_by_id(self, version_id: Any, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        from bson import ObjectId
        try:
            oid = ObjectId(str(version_id))
        except Exception:
            return None
        doc = await self.db.prompt_versions.find_one({"_id": oid})
        if not doc:
            return None
        return {
            "id": str(doc["_id"]),
            "version_number": doc["version_number"],
            "content": doc["content"],
            "created_at": doc.get("created_at"),
            "created_by": doc.get("created_by"),
            "tag": doc.get("tag"),
            "is_active": doc.get("is_active", False),
            "prompt_name": doc.get("prompt_name"),
        }

    async def create_version(
        self,
        name: str,
        content: str,
        created_by: str,
        tag: Optional[str],
        set_active: bool,
        tenant_id: str = "default",
    ) -> int:
        await self.db.prompts.update_one(
            {"name": name, "tenant_id": tenant_id},
            {"$setOnInsert": {"name": name, "tenant_id": tenant_id, "is_deleted": False, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}},
            upsert=True,
        )
        # Restore if previously deleted
        await self.db.prompts.update_one(
            {"name": name, "tenant_id": tenant_id},
            {"$set": {"is_deleted": False}},
        )

        last_v = await self.db.prompt_versions.find_one(
            {"prompt_name": name, "tenant_id": tenant_id},
            sort=[("version_number", -1)],
        )
        next_v = (last_v["version_number"] + 1) if last_v else 1

        if tag == "prod":
            await self.db.prompt_versions.update_many(
                {"prompt_name": name, "tenant_id": tenant_id, "tag": "prod"},
                {"$set": {"tag": None}},
            )

        if set_active:
            await self.db.prompt_versions.update_many(
                {"prompt_name": name, "tenant_id": tenant_id},
                {"$set": {"is_active": False}},
            )

        await self.db.prompt_versions.insert_one({
            "prompt_name": name,
            "tenant_id": tenant_id,
            "content": content,
            "version_number": next_v,
            "created_by": created_by,
            "tag": tag,
            "is_active": bool(set_active),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        return next_v

    async def set_active_version(self, version_id: Any, tenant_id: str = "default") -> bool:
        from bson import ObjectId
        try:
            oid = ObjectId(str(version_id))
        except Exception:
            return False
        doc = await self.db.prompt_versions.find_one({"_id": oid})
        if not doc:
            return False
        name = doc["prompt_name"]
        t_id = doc.get("tenant_id", tenant_id)
        await self.db.prompt_versions.update_many(
            {"prompt_name": name, "tenant_id": t_id},
            {"$set": {"is_active": False}},
        )
        await self.db.prompt_versions.update_one({"_id": oid}, {"$set": {"is_active": True}})
        return True

    async def soft_delete_prompt(self, name: str, tenant_id: str = "default") -> None:
        await self.db.prompt_versions.update_many(
            {"prompt_name": name, "tenant_id": tenant_id},
            {"$set": {"is_active": False}},
        )
        await self.db.prompts.update_one(
            {"name": name, "tenant_id": tenant_id},
            {"$set": {"is_deleted": True}},
        )

    async def insert_logs_batch(self, logs: List[Dict[str, Any]]) -> None:
        if not logs:
            return
        docs = []
        for log in logs:
            docs.append({
                "prompt_name": log.get("prompt_name"),
                "version_id": str(log.get("version_id")),
                "tenant_id": log.get("tenant_id", "default"),
                "input": (log.get("input_text", "") or "")[:10_000],
                "output": (log.get("output_text", "") or "")[:10_000],
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        await self.db.prompt_logs.insert_many(docs)

    async def fetch_logs(self, prompt_name: Optional[str] = None, limit: int = 100, tenant_id: str = "default") -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"tenant_id": tenant_id}
        if prompt_name:
            query["prompt_name"] = prompt_name
        cursor = self.db.prompt_logs.find(query).sort("timestamp", -1).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [
            {
                "id": str(d["_id"]),
                "prompt_name": d.get("prompt_name"),
                "version_number": "-",
                "input": d.get("input"),
                "output": d.get("output"),
                "timestamp": d.get("timestamp"),
            }
            for d in docs
        ]

    async def export_prompts(self, tenant_id: str = "default") -> Dict[str, str]:
        cursor = self.db.prompt_versions.find({"tenant_id": tenant_id, "is_active": True})
        docs = await cursor.to_list(length=1000)
        return {d["prompt_name"]: d["content"] for d in docs}

    async def import_prompts(self, data: Dict[str, str], imported_by: str = "import", tenant_id: str = "default") -> Dict[str, List[str]]:
        created, updated = [], []
        for name, content in data.items():
            exists = await self.db.prompts.find_one({"name": name, "tenant_id": tenant_id}) is not None
            await self.create_version(name, content, imported_by, None, set_active=True, tenant_id=tenant_id)
            if exists:
                updated.append(name)
            else:
                created.append(name)
        return {"created": created, "updated": updated}

    # ------------------------------------------------------------------
    # Users — tenant_id param now present to match BaseStorage interface
    # ------------------------------------------------------------------

    async def get_user(self, username: str, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
        doc = await self.db.users.find_one({"username": username, "tenant_id": tenant_id})
        if not doc:
            return None
        doc["id"] = str(doc["_id"])
        return doc

    async def create_user(
        self, username: str, password_hash: str, role: str = "editor", email: str = "", tenant_id: str = "default"
    ) -> Dict[str, Any]:
        user_doc = {
            "username": username,
            "email": email,
            "password_hash": password_hash,
            "role": role,
            "tenant_id": tenant_id,
            "is_active": True,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        res = await self.db.users.insert_one(user_doc)
        user_doc["id"] = str(res.inserted_id)
        return user_doc

    async def fetch_users(self, tenant_id: str = "default") -> List[Dict[str, Any]]:
        docs = await self.db.users.find({"tenant_id": tenant_id}).to_list(length=1000)
        return [
            {
                "id": str(d["_id"]),
                "username": d.get("username"),
                "email": d.get("email"),
                "role": d.get("role"),
                "tenant_id": d.get("tenant_id"),
                "is_active": d.get("is_active", True),
                "created_at": d.get("created_at"),
            }
            for d in docs
        ]

    async def insert_audit_log(
        self,
        action: str,
        performed_by: str,
        prompt_name: Optional[str] = None,
        version_id: Optional[Any] = None,
        detail: Optional[str] = None,
        tenant_id: str = "default",
    ) -> None:
        try:
            await self.db.audit_log.insert_one({
                "action": action,
                "performed_by": performed_by,
                "prompt_name": prompt_name,
                "version_id": str(version_id) if version_id is not None else None,
                "detail": detail,
                "tenant_id": tenant_id,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as exc:
            logger.warning("Failed to write audit log: %s", exc)

    async def fetch_audit_logs(self, limit: int = 100, tenant_id: str = "default") -> List[Dict[str, Any]]:
        cursor = self.db.audit_log.find({"tenant_id": tenant_id}).sort("created_at", -1).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [
            {
                "id": str(d["_id"]),
                "action": d.get("action"),
                "performed_by": d.get("performed_by"),
                "prompt_name": d.get("prompt_name"),
                "version_id": d.get("version_id"),
                "detail": d.get("detail"),
                "tenant_id": d.get("tenant_id"),
                "created_at": d.get("created_at"),
            }
            for d in docs
        ]
