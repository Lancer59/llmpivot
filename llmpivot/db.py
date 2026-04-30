"""
SQLite storage layer — schema creation and all DB queries.
Uses aiosqlite for async access and sqlite3 for sync bootstrap.
"""

import sqlite3
import aiosqlite
from typing import Optional

DDL = """
CREATE TABLE IF NOT EXISTS prompts (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id      INTEGER NOT NULL REFERENCES prompts(id),
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
    input      TEXT,
    output     TEXT,
    timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db(db_path: str) -> None:
    """Synchronous bootstrap — creates tables if they don't exist."""
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Async read helpers
# ---------------------------------------------------------------------------

async def fetch_active_version(db_path: str, name: str) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT pv.id, pv.content, pv.version_number, pv.tag, pv.created_by, pv.created_at
            FROM prompt_versions pv
            JOIN prompts p ON p.id = pv.prompt_id
            WHERE p.name = ? AND pv.is_active = 1
            LIMIT 1
            """,
            (name,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def fetch_all_prompts(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT p.id, p.name,
                   pv.version_number AS active_version,
                   pv.created_by     AS last_edited_by,
                   pv.created_at     AS last_updated
            FROM prompts p
            LEFT JOIN prompt_versions pv
                ON pv.prompt_id = p.id AND pv.is_active = 1
            ORDER BY p.name
            """
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def fetch_prompt_versions(db_path: str, name: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT pv.id, pv.version_number, pv.content, pv.created_at,
                   pv.created_by, pv.tag, pv.is_active
            FROM prompt_versions pv
            JOIN prompts p ON p.id = pv.prompt_id
            WHERE p.name = ?
            ORDER BY pv.version_number DESC
            """,
            (name,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def fetch_version_by_id(db_path: str, version_id: int) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM prompt_versions WHERE id = ?", (version_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------------------
# Async write helpers
# ---------------------------------------------------------------------------

async def create_version(
    db_path: str,
    name: str,
    content: str,
    created_by: str,
    tag: Optional[str],
    set_active: bool,
) -> int:
    """Create prompt if needed, insert new version, optionally set it active."""
    async with aiosqlite.connect(db_path) as db:
        # Upsert prompt
        await db.execute(
            "INSERT OR IGNORE INTO prompts (name) VALUES (?)", (name,)
        )
        async with db.execute(
            "SELECT id FROM prompts WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
            prompt_id = row[0]

        # Next version number
        async with db.execute(
            "SELECT COALESCE(MAX(version_number), 0) FROM prompt_versions WHERE prompt_id = ?",
            (prompt_id,),
        ) as cur:
            row = await cur.fetchone()
            next_version = row[0] + 1

        # If tag is prod, clear existing prod tag on this prompt
        if tag == "prod":
            await db.execute(
                "UPDATE prompt_versions SET tag = NULL WHERE prompt_id = ? AND tag = 'prod'",
                (prompt_id,),
            )

        # If setting active, deactivate others
        if set_active:
            await db.execute(
                "UPDATE prompt_versions SET is_active = 0 WHERE prompt_id = ?",
                (prompt_id,),
            )

        await db.execute(
            """
            INSERT INTO prompt_versions (prompt_id, content, version_number, created_by, tag, is_active)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (prompt_id, content, next_version, created_by, tag, 1 if set_active else 0),
        )
        await db.commit()
        return next_version


async def set_active_version(db_path: str, version_id: int) -> bool:
    """Set a specific version as active, deactivating all others for that prompt."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT prompt_id FROM prompt_versions WHERE id = ?", (version_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            prompt_id = row[0]

        await db.execute(
            "UPDATE prompt_versions SET is_active = 0 WHERE prompt_id = ?",
            (prompt_id,),
        )
        await db.execute(
            "UPDATE prompt_versions SET is_active = 1 WHERE id = ?",
            (version_id,),
        )
        await db.commit()
        return True


async def soft_delete_prompt(db_path: str, name: str) -> None:
    """Deactivate all versions for a prompt (soft delete)."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT id FROM prompts WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE prompt_versions SET is_active = 0 WHERE prompt_id = ?",
                    (row[0],),
                )
                await db.commit()


async def insert_log(
    db_path: str,
    prompt_id: int,
    version_id: int,
    input_text: str,
    output_text: str,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO prompt_logs (prompt_id, version_id, input, output) VALUES (?, ?, ?, ?)",
            (prompt_id, version_id, input_text, output_text),
        )
        await db.commit()


async def fetch_prompt_id(db_path: str, name: str) -> Optional[int]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT id FROM prompts WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def fetch_logs(db_path: str, prompt_name: Optional[str] = None, limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if prompt_name:
            async with db.execute(
                """
                SELECT pl.id, p.name AS prompt_name, pv.version_number,
                       pl.input, pl.output, pl.timestamp
                FROM prompt_logs pl
                JOIN prompts p ON p.id = pl.prompt_id
                JOIN prompt_versions pv ON pv.id = pl.version_id
                WHERE p.name = ?
                ORDER BY pl.timestamp DESC LIMIT ?
                """,
                (prompt_name, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                """
                SELECT pl.id, p.name AS prompt_name, pv.version_number,
                       pl.input, pl.output, pl.timestamp
                FROM prompt_logs pl
                JOIN prompts p ON p.id = pl.prompt_id
                JOIN prompt_versions pv ON pv.id = pl.version_id
                ORDER BY pl.timestamp DESC LIMIT ?
                """,
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def export_prompts(db_path: str) -> dict:
    """Export all active prompt versions as {name: content}."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT p.name, pv.content
            FROM prompt_versions pv
            JOIN prompts p ON p.id = pv.prompt_id
            WHERE pv.is_active = 1
            ORDER BY p.name
            """
        ) as cur:
            rows = await cur.fetchall()
            return {row["name"]: row["content"] for row in rows}


async def import_prompts(db_path: str, data: dict, imported_by: str = "import") -> dict:
    """
    Import prompts from {name: content} dict.
    Creates a new version for each prompt and sets it active.
    Returns {"created": [...], "updated": [...]} summary.
    """
    created = []
    updated = []
    for name, content in data.items():
        # Check if prompt already exists
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT id FROM prompts WHERE name = ?", (name,)
            ) as cur:
                row = await cur.fetchone()
            exists = row is not None

        await create_version(db_path, name, content, imported_by, None, set_active=True)

        if exists:
            updated.append(name)
        else:
            created.append(name)

    return {"created": created, "updated": updated}
