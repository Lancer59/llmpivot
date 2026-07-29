"""
Backward-compatible wrapper routing SQLite queries to SQLiteStorage engine.
"""

from typing import Optional, List, Dict, Any
from .storage import SQLiteStorage

_default_storages: Dict[str, SQLiteStorage] = {}


def _get_storage(db_path: str) -> SQLiteStorage:
    if db_path not in _default_storages:
        storage = SQLiteStorage(db_path)
        storage.init_db_sync()
        _default_storages[db_path] = storage
    return _default_storages[db_path]


def init_db(db_path: str) -> None:
    storage = _get_storage(db_path)
    storage.init_db_sync()


async def fetch_active_version(db_path: str, name: str) -> Optional[dict]:
    return await _get_storage(db_path).fetch_active_version(name)


async def fetch_all_prompts(db_path: str) -> List[dict]:
    return await _get_storage(db_path).fetch_all_prompts()


async def fetch_prompt_versions(db_path: str, name: str) -> List[dict]:
    return await _get_storage(db_path).fetch_prompt_versions(name)


async def fetch_version_by_id(db_path: str, version_id: int) -> Optional[dict]:
    return await _get_storage(db_path).fetch_version_by_id(version_id)


async def create_version(
    db_path: str,
    name: str,
    content: str,
    created_by: str,
    tag: Optional[str],
    set_active: bool,
) -> int:
    return await _get_storage(db_path).create_version(name, content, created_by, tag, set_active)


async def set_active_version(db_path: str, version_id: int) -> bool:
    return await _get_storage(db_path).set_active_version(version_id)


async def soft_delete_prompt(db_path: str, name: str) -> None:
    await _get_storage(db_path).soft_delete_prompt(name)


async def insert_log(
    db_path: str,
    prompt_id: int,
    version_id: int,
    input_text: str,
    output_text: str,
) -> None:
    log_item = {
        "prompt_name": str(prompt_id),
        "version_id": version_id,
        "input_text": input_text,
        "output_text": output_text,
    }
    await _get_storage(db_path).insert_logs_batch([log_item])


async def fetch_prompt_id(db_path: str, name: str) -> Optional[int]:
    # Convenience function used in legacy logger
    prompts = await _get_storage(db_path).fetch_all_prompts()
    for p in prompts:
        if p["name"] == name:
            return p["id"]
    return None


async def fetch_logs(db_path: str, prompt_name: Optional[str] = None, limit: int = 100) -> List[dict]:
    return await _get_storage(db_path).fetch_logs(prompt_name=prompt_name, limit=limit)


async def export_prompts(db_path: str) -> dict:
    return await _get_storage(db_path).export_prompts()


async def import_prompts(db_path: str, data: dict, imported_by: str = "import") -> dict:
    return await _get_storage(db_path).import_prompts(data, imported_by=imported_by)
