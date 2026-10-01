"""Helpers for portable, versioned Agent Skills bundles.

The on-disk exchange format follows Agent Skills: a ``SKILL.md`` with YAML
frontmatter, plus optional Markdown files below ``references/``. Bundles are
stored as JSON text so both built-in storage backends can version them without
an additional runtime dependency.
"""

from __future__ import annotations

import json
import re
import zipfile
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

SKILL_CHANGELOG_MARKER = "skill::"
MAX_SKILL_NAME = 64
MAX_SKILL_DESCRIPTION = 1024
MAX_SKILL_BUNDLE_BYTES = 5 * 1024 * 1024
MAX_SKILL_FILES = 100
MAX_SKILL_FILE_BYTES = 1024 * 1024


class SkillValidationError(ValueError):
    """Raised when a skill bundle does not meet the supported format."""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = value[1:-1]
    elif len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1]
    return value.strip()


def parse_skill_markdown(markdown: str) -> Dict[str, str]:
    """Read the required name/description fields from simple YAML frontmatter."""
    if not markdown.startswith("---\n") and not markdown.startswith("---\r\n"):
        raise SkillValidationError("SKILL.md must start with YAML frontmatter (---).")

    lines = markdown.splitlines()
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise SkillValidationError("SKILL.md frontmatter is missing its closing ---.") from exc

    fields: Dict[str, str] = {}
    i = 1
    while i < end:
        line = lines[i]
        match = re.match(r"^([a-zA-Z][a-zA-Z0-9_-]*):(?:\s*(.*))?$", line)
        if not match:
            i += 1
            continue
        key, value = match.group(1), (match.group(2) or "").strip()
        if key in ("name", "description"):
            if value in (">", "|", ">-", "|-", ">+", "|+"):
                parts = []
                i += 1
                while i < end and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                    if lines[i].strip():
                        parts.append(lines[i].strip())
                    i += 1
                fields[key] = " ".join(parts)
                continue
            fields[key] = _unquote(value)
        i += 1

    name = fields.get("name", "")
    description = fields.get("description", "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > MAX_SKILL_NAME:
        raise SkillValidationError("Skill name must be a lowercase slug of up to 64 characters.")
    if not description or len(description) > MAX_SKILL_DESCRIPTION:
        raise SkillValidationError("Skill description must be between 1 and 1024 characters.")
    return {"name": name, "description": description, "body": "\n".join(lines[end + 1:]).strip()}


def normalize_skill_files(files: Dict[str, str], expected_name: Optional[str] = None) -> Dict[str, str]:
    """Validate and normalize a skill's SKILL.md and Markdown references."""
    if not isinstance(files, dict) or len(files) > MAX_SKILL_FILES:
        raise SkillValidationError(f"A skill can contain at most {MAX_SKILL_FILES} files.")
    normalized: Dict[str, str] = {}
    total = 0
    for raw_path, value in files.items():
        if not isinstance(raw_path, str) or not isinstance(value, str):
            raise SkillValidationError("Skill files must map relative paths to UTF-8 text.")
        path = PurePosixPath(raw_path.replace("\\", "/"))
        if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
            raise SkillValidationError("Skill file paths must be relative and cannot traverse directories.")
        if len(path.parts) > 5:
            raise SkillValidationError("Skill reference paths may be at most 5 directories deep.")
        clean_path = path.as_posix()
        if clean_path.lower() == "skill.md":
            clean_path = "SKILL.md"
        elif not (clean_path.startswith("references/") and clean_path.lower().endswith(".md")):
            raise SkillValidationError("Only SKILL.md and Markdown files under references/ are supported.")
        data = value.encode("utf-8")
        if len(data) > MAX_SKILL_FILE_BYTES:
            raise SkillValidationError(f"{clean_path} exceeds the 1 MiB per-file limit.")
        total += len(data)
        normalized[clean_path] = value
    if total > MAX_SKILL_BUNDLE_BYTES:
        raise SkillValidationError("Skill bundle exceeds the 5 MiB uncompressed limit.")
    markdown = normalized.get("SKILL.md")
    if markdown is None:
        raise SkillValidationError("Skill bundle must contain SKILL.md at its root.")
    manifest = parse_skill_markdown(markdown)
    if expected_name and manifest["name"] != expected_name:
        raise SkillValidationError(f"SKILL.md name must match '{expected_name}'.")
    return dict(sorted(normalized.items()))


def encode_skill(files: Dict[str, str]) -> str:
    return json.dumps({"format": "agent-skill-v1", "files": files}, ensure_ascii=False, separators=(",", ":"))


def decode_skill(content: str) -> Dict[str, str]:
    try:
        value = json.loads(content)
        if value.get("format") == "agent-skill-v1":
            return normalize_skill_files(value.get("files", {}))
    except (ValueError, AttributeError, TypeError):
        pass
    # Older/imported single-file versions remain usable.
    return normalize_skill_files({"SKILL.md": content})


def skill_from_zip(data: bytes, expected_name: Optional[str] = None) -> Dict[str, str]:
    if len(data) > MAX_SKILL_BUNDLE_BYTES:
        raise SkillValidationError("Uploaded skill archive exceeds the 5 MiB limit.")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise SkillValidationError("Upload a valid .zip skill bundle.") from exc
    with archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(entries) > MAX_SKILL_FILES:
            raise SkillValidationError(f"A skill can contain at most {MAX_SKILL_FILES} files.")
        files: Dict[str, str] = {}
        total = 0
        roots = set()
        for entry in entries:
            path = PurePosixPath(entry.filename.replace("\\", "/"))
            parts = path.parts
            # Skill folders are commonly zipped as a single top-level directory.
            if len(parts) > 1 and parts[0].lower() not in ("references", "skill.md"):
                roots.add(parts[0])
                parts = parts[1:]
            clean_path = PurePosixPath(*parts).as_posix()
            if not clean_path or clean_path == ".":
                continue
            if entry.file_size > MAX_SKILL_FILE_BYTES:
                raise SkillValidationError(f"{clean_path} exceeds the 1 MiB per-file limit.")
            total += entry.file_size
            if total > MAX_SKILL_BUNDLE_BYTES:
                raise SkillValidationError("Skill archive expands beyond the 5 MiB limit.")
            try:
                files[clean_path] = archive.read(entry).decode("utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                raise SkillValidationError("Skill files must be UTF-8 text; binary assets are not supported yet.") from exc
    normalized = normalize_skill_files(files, expected_name=expected_name)
    manifest_name = parse_skill_markdown(normalized["SKILL.md"])["name"]
    if roots and (len(roots) != 1 or next(iter(roots)) != manifest_name):
        raise SkillValidationError("The ZIP folder name must match the SKILL.md name.")
    return normalized


def reference_for(files: Dict[str, str], path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise SkillValidationError("Reference path must stay inside the skill bundle.")
    if not normalized.startswith("references/"):
        raise SkillValidationError("Only files under references/ can be loaded as references.")
    if normalized not in files:
        raise SkillValidationError(f"Reference '{normalized}' was not found in this skill version.")
    return files[normalized]

