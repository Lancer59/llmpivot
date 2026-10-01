"""
FastAPI router for the LLM Pivot UI.
Supports Auth & RBAC session management, multi-tenancy, and pluggable storage engines.

Production hardening in this version:
- cookie_secure passed from LLMAssetManager (defaults True).
- CSRF token generated at login and validated on every state-mutating POST.
- Login rate limiting: max 10 attempts per IP per 5-minute window; 15-minute lockout.
- Input size limits: prompt content capped at 500 KB, log text at 10 KB.
- Audit log written for: create_version, activate, delete, import, create_user.
"""

import json
import time
from collections import defaultdict
from typing import Optional, TYPE_CHECKING
from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, Response
import asyncio

try:
    from sse_starlette.sse import EventSourceResponse as SSEResponse  # type: ignore
    _HAS_SSE_STARLETTE = True
except ImportError:
    _HAS_SSE_STARLETTE = False

from ..auth import (
    hash_password,
    verify_password,
    create_token,
    get_current_user_from_request,
    get_session_token_from_request,
    generate_csrf_token,
    verify_csrf_token,
    has_permission,
)
from .templates import (
    prompt_list,
    prompt_detail,
    edit_page,
    diff_page,
    ab_test_page,
    logs_page,
    login_page,
    users_page,
    metadata_page,
    changelog_page,
    changelog_edit_page,
    app_context_page,
    tree_page,
    pivot_widget_html,
    skills_page,
    skill_edit_page,
    skill_import_page,
    skill_detail_page,
    skill_diff_page,
    skill_test_page,
)
from .helpers import escape as _escape
from ..skills import (
    MAX_SKILL_BUNDLE_BYTES, SKILL_CHANGELOG_MARKER, SkillValidationError, decode_skill, encode_skill,
    normalize_skill_files, parse_skill_markdown, skill_from_zip,
)

if TYPE_CHECKING:
    from ..manager import LLMAssetManager

# ---------------------------------------------------------------------------
# Input size constants
# ---------------------------------------------------------------------------
_MAX_CONTENT_BYTES = 500 * 1024   # 500 KB — prompt content
_MAX_LOG_BYTES = 10 * 1024        # 10 KB  — log input/output text
_MAX_IMPORT_BYTES = 5 * 1024 * 1024  # 5 MB  — JSON import file upload cap
_MAX_SKILL_UPLOAD_BYTES = 5 * 1024 * 1024

# ---------------------------------------------------------------------------
# Login rate limiter (in-process; replace with Redis for multi-worker)
# ---------------------------------------------------------------------------
_RATE_WINDOW = 300       # 5-minute sliding window
_RATE_MAX_ATTEMPTS = 10  # max failures within the window before lockout
_LOCKOUT_SECONDS = 900   # 15-minute lockout after exceeding limit

# { ip: [(timestamp, ...)] }
_login_attempts: dict = defaultdict(list)
# { ip: lockout_until_timestamp }
_login_lockouts: dict = {}


def _get_client_ip(request: Request) -> str:
    """Return the best available client IP, respecting X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_rate_limited(ip: str) -> bool:
    """Return True if this IP is currently locked out."""
    now = time.monotonic()
    lockout_until = _login_lockouts.get(ip, 0)
    if now < lockout_until:
        return True

    # Prune old attempts outside the window for this IP
    window_start = now - _RATE_WINDOW
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > window_start]

    # Periodically evict expired lockout entries to prevent unbounded memory growth.
    # Do this roughly 1% of the time (cheap enough to not matter).
    if len(_login_lockouts) > 1000:
        expired = [k for k, v in _login_lockouts.items() if v < now]
        for k in expired:
            _login_lockouts.pop(k, None)
            _login_attempts.pop(k, None)

    return False


def _record_login_failure(ip: str) -> None:
    """Record a failed login attempt and apply lockout if threshold exceeded."""
    now = time.monotonic()
    _login_attempts[ip].append(now)
    if len(_login_attempts[ip]) >= _RATE_MAX_ATTEMPTS:
        _login_lockouts[ip] = now + _LOCKOUT_SECONDS
        _login_attempts[ip] = []


def _clear_login_failures(ip: str) -> None:
    """Clear failed-attempt history after a successful login."""
    _login_attempts.pop(ip, None)
    _login_lockouts.pop(ip, None)


# ---------------------------------------------------------------------------
# Auto-changelog helper (Phase 1 Pivot)
# ---------------------------------------------------------------------------

_CHANGELOG_SYSTEM_PROMPT = """\
You are an instruction asset change assistant. A new version of a prompt or Agent Skill has been saved.
Given the old and new instruction asset content, write a concise changelog entry (2–4 sentences) that explains:
1. What changed (specific wording, tone, structure, instructions)
2. Why the change was likely made (inferred from the diff)
3. Any downstream impact to watch for

Write in past tense. Be specific. Do not pad with filler phrases.
Return only the changelog entry text, no headers or bullet points."""


async def _generate_changelog(
    manager: "LLMAssetManager",
    prompt_name: str,
    version_id: int,
    old_content: str,
    new_content: str,
    editor: str,
) -> None:
    """
    Background coroutine: calls the LLM to generate a changelog entry and saves it.
    Failures are logged as WARNING and never propagate.
    """
    import logging as _logging
    _log = _logging.getLogger("llmpivot.changelog")
    try:
        import difflib
        display_name = prompt_name
        is_skill = prompt_name.startswith(SKILL_CHANGELOG_MARKER)
        if is_skill:
            display_name = prompt_name[len(SKILL_CHANGELOG_MARKER):]
            old_content = decode_skill(old_content)["SKILL.md"] if old_content else ""
            new_content = decode_skill(new_content)["SKILL.md"]
        diff_lines = list(difflib.unified_diff(
            (old_content or "").splitlines(),
            (new_content or "").splitlines(),
            lineterm="",
        ))
        diff_text = "\n".join(diff_lines[:80]) or "(new instruction asset — no previous version)"

        # Optionally include metadata context
        meta = {} if is_skill else await manager.storage.get_prompt_metadata(prompt_name, tenant_id=manager.tenant_id)
        purpose = meta.get("purpose", "") if meta else ""
        meta_context = f"\nPrompt purpose: {purpose}" if purpose else ""

        user_msg = (
            f"Asset name: {display_name}{meta_context}\n\n"
            f"Diff (unified format):\n{diff_text}\n\n"
            f"New content:\n{new_content[:1500]}"
        )

        # Use a Pivot-specific LLM client if model differs, otherwise reuse manager.llm
        if manager.pivot_model and manager.pivot_model != manager.llm._model:
            from ..llm import LLMClient
            llm = LLMClient(
                url=manager.llm._url,
                api_key=manager.llm._api_key,
                model=manager.pivot_model,
                system_prompt=_CHANGELOG_SYSTEM_PROMPT,
            )
        else:
            # Temporarily override system prompt
            from ..llm import LLMClient
            llm = LLMClient(
                url=manager.llm._url,
                api_key=manager.llm._api_key,
                model=manager.llm._model,
                system_prompt=_CHANGELOG_SYSTEM_PROMPT,
            )

        entry = await llm._call(_CHANGELOG_SYSTEM_PROMPT, user_msg)
        entry = entry.strip()

        changelog_upsert = (manager.storage.upsert_skill_changelog_entry if is_skill
                            else manager.storage.upsert_changelog_entry)
        await changelog_upsert(version_id=version_id, entry=entry, generated_by="pivot",
                               created_by=editor, tenant_id=manager.tenant_id)
        _log.info("Auto-changelog generated for %s v%s", prompt_name, version_id)
    except Exception as exc:
        _log.warning("Auto-changelog generation failed for %s (non-fatal): %s", prompt_name, exc)


def _build_router(manager: "LLMAssetManager") -> APIRouter:
    router = APIRouter()

    def _base(request: Request) -> str:
        return request.scope.get("root_path", "").rstrip("/")

    def _get_user(request: Request) -> Optional[dict]:
        if manager.auth_mode == "rbac":
            return get_current_user_from_request(request, manager.secret_key)
        return None

    def _check_auth(request: Request, min_role: str = "viewer") -> Optional[RedirectResponse]:
        if manager.auth_mode == "rbac":
            user = _get_user(request)
            if not user:
                return RedirectResponse(f"{_base(request)}/login", status_code=303)
            if not has_permission(user.get("role", "viewer"), min_role):
                return RedirectResponse(f"{_base(request)}/list", status_code=303)
        return None

    def _check_password(password: Optional[str]) -> bool:
        if manager.protected_mode or manager.auth_mode == "protected":
            return password == manager.admin_password
        return True

    def _get_csrf(request: Request) -> Optional[str]:
        """Return the CSRF token for the current session, or None if not in RBAC mode."""
        if manager.auth_mode != "rbac":
            return None
        token = get_session_token_from_request(request)
        if not token:
            return None
        return generate_csrf_token(token, manager.secret_key)

    def _validate_csrf(request: Request, submitted: Optional[str]) -> bool:
        """Validate CSRF token. Always passes when auth_mode is not rbac."""
        if manager.auth_mode != "rbac":
            return True
        token = get_session_token_from_request(request)
        if not token:
            return False
        return verify_csrf_token(submitted, token, manager.secret_key)

    def _actor(request: Request, fallback: str = "anonymous") -> str:
        """Return username of logged-in user, or the fallback string."""
        user = _get_user(request)
        return user.get("username", fallback) if user else fallback

    def _widget(base: str) -> str:
        """Generate the Pivot widget HTML for injection into pages."""
        return pivot_widget_html(
            base=base,
            pivot_enabled=manager.pivot_enabled,
            pivot_proactive=manager.pivot_proactive,
        )

    # ------------------------------------------------------------------
    # Auth routes (/login, /logout)
    # ------------------------------------------------------------------

    @router.get("/login", response_class=HTMLResponse)
    async def get_login(request: Request):
        return HTMLResponse(login_page(_base(request)))

    @router.post("/login", response_class=HTMLResponse)
    async def post_login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        base = _base(request)
        ip = _get_client_ip(request)

        if _is_rate_limited(ip):
            return HTMLResponse(
                login_page(base, error="Too many failed login attempts. Please wait 15 minutes before trying again."),
                status_code=429,
            )

        user = await manager.storage.get_user(username, tenant_id=manager.tenant_id)
        if not user or not verify_password(password, user.get("password_hash", "")):
            _record_login_failure(ip)
            return HTMLResponse(login_page(base, error="Invalid username or password."), status_code=400)

        _clear_login_failures(ip)
        token = create_token(
            {"id": user["id"], "username": user["username"], "role": user.get("role", "editor")},
            manager.secret_key,
        )
        resp = RedirectResponse(f"{base}/list", status_code=303)
        resp.set_cookie(
            "llmpivot_session",
            token,
            httponly=True,
            samesite="lax",
            secure=manager.cookie_secure,
            max_age=86400,
        )
        return resp

    @router.get("/logout")
    async def logout(request: Request):
        resp = RedirectResponse(f"{_base(request)}/login", status_code=303)
        resp.delete_cookie("llmpivot_session", path="/", samesite="lax")
        return resp

    # ------------------------------------------------------------------
    # User Management (/users)
    # ------------------------------------------------------------------

    @router.get("/users", response_class=HTMLResponse)
    async def list_users_route(request: Request):
        auth_redirect = _check_auth(request, min_role="admin")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        users = await manager.storage.fetch_users(tenant_id=manager.tenant_id)
        csrf = _get_csrf(request)
        return HTMLResponse(users_page(users, user, _base(request), csrf_token=csrf))

    @router.post("/users", response_class=HTMLResponse)
    async def create_user_route(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form("editor"),
        email: str = Form(""),
        csrf_token: Optional[str] = Form(None),
    ):
        auth_redirect = _check_auth(request, min_role="admin")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        csrf = _get_csrf(request)

        if not _validate_csrf(request, csrf_token):
            users = await manager.storage.fetch_users(tenant_id=manager.tenant_id)
            return HTMLResponse(users_page(users, user, base, error="Invalid or missing CSRF token.", csrf_token=csrf), status_code=403)

        existing = await manager.storage.get_user(username, tenant_id=manager.tenant_id)
        if existing:
            users = await manager.storage.fetch_users(tenant_id=manager.tenant_id)
            return HTMLResponse(users_page(users, user, base, error=f"Username '{username}' already exists.", csrf_token=csrf), status_code=400)

        pwd_hash = hash_password(password)
        await manager.storage.create_user(
            username=username, password_hash=pwd_hash, role=role, email=email, tenant_id=manager.tenant_id,
        )
        await manager.storage.insert_audit_log(
            action="user_created", performed_by=_actor(request),
            detail=f"username={username} role={role}", tenant_id=manager.tenant_id,
        )
        users = await manager.storage.fetch_users(tenant_id=manager.tenant_id)
        return HTMLResponse(users_page(users, user, base, success=f"User '{username}' created successfully.", csrf_token=csrf))

    # ------------------------------------------------------------------
    # Root redirect & Prompt List
    # ------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        return RedirectResponse(f"{_base(request)}/list")

    @router.get("/list", response_class=HTMLResponse)
    async def list_prompts(request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        prompts = await manager.storage.fetch_all_prompts(tenant_id=manager.tenant_id)
        base = _base(request)
        return HTMLResponse(prompt_list(prompts, manager.protected_mode, base, user=user, pivot_widget=_widget(base)))

    # ------------------------------------------------------------------
    # Prompt detail
    # ------------------------------------------------------------------

    @router.get("/detail/{name}", response_class=HTMLResponse)
    async def detail(name: str, request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        csrf = _get_csrf(request)
        base = _base(request)
        versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
        return HTMLResponse(prompt_detail(name, versions, manager.protected_mode, base, user=user, csrf_token=csrf, pivot_widget=_widget(base)))

    # ------------------------------------------------------------------
    # Edit / Create
    # ------------------------------------------------------------------

    @router.get("/edit/__new__", response_class=HTMLResponse)
    async def new_prompt_form(request: Request):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        csrf = _get_csrf(request)
        base = _base(request)
        return HTMLResponse(
            edit_page("__new__", "", manager.protected_mode, manager.has_llm, base, is_new=True, user=user, csrf_token=csrf, pivot_widget=_widget(base))
        )

    @router.post("/edit/__new__", response_class=HTMLResponse)
    async def new_prompt_submit(
        request: Request,
        prompt_name: str = Form(...),
        content: str = Form(...),
        tag: str = Form(""),
        set_active: str = Form("1"),
        password: Optional[str] = Form(None),
        csrf_token: Optional[str] = Form(None),
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        csrf = _get_csrf(request)

        if not _validate_csrf(request, csrf_token):
            return HTMLResponse(
                edit_page("__new__", content, manager.protected_mode, manager.has_llm, base,
                          error="Invalid or missing CSRF token.", is_new=True, user=user, csrf_token=csrf),
                status_code=403,
            )

        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            return HTMLResponse(
                edit_page("__new__", content, manager.protected_mode, manager.has_llm, base,
                          error=f"Prompt content exceeds maximum allowed size ({_MAX_CONTENT_BYTES // 1024} KB).",
                          is_new=True, user=user, csrf_token=csrf),
                status_code=400,
            )

        tag = tag or None
        do_activate = set_active == "1"
        creator = _actor(request)

        if manager.protected_mode and (do_activate or tag == "prod"):
            if not _check_password(password):
                return HTMLResponse(
                    edit_page("__new__", content, manager.protected_mode, manager.has_llm, base,
                              error="Incorrect password.", is_new=True, user=user, csrf_token=csrf)
                )

        version_num = await manager.storage.create_version(prompt_name, content, creator, tag, do_activate, tenant_id=manager.tenant_id)
        await manager.storage.insert_audit_log(
            action="version_created", performed_by=creator,
            prompt_name=prompt_name, version_id=version_num,
            detail=f"tag={tag} active={do_activate}", tenant_id=manager.tenant_id,
        )
        manager.cache.invalidate(prompt_name)
        if do_activate:
            manager.schedule_snapshot_refresh()

        # Auto-changelog: generate asynchronously, don't block the redirect
        if manager.auto_changelog and manager.llm:
            versions_after = await manager.storage.fetch_prompt_versions(prompt_name, tenant_id=manager.tenant_id)
            new_ver = next((v for v in versions_after if v["version_number"] == version_num), None)
            if new_ver:
                asyncio.get_event_loop().create_task(
                    _generate_changelog(manager, prompt_name, new_ver["id"], "", content, creator)
                )

        return RedirectResponse(f"{base}/detail/{prompt_name}", status_code=303)

    @router.get("/edit/{name}", response_class=HTMLResponse)
    async def edit_form(name: str, request: Request):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        csrf = _get_csrf(request)
        base = _base(request)
        versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
        active = next((v for v in versions if v["is_active"]), None)
        current = active["content"] if active else ""
        children = await manager.storage.fetch_prompt_children(name, tenant_id=manager.tenant_id)
        metadata = await manager.storage.get_prompt_metadata(name, tenant_id=manager.tenant_id)
        return HTMLResponse(
            edit_page(name, current, manager.protected_mode, manager.has_llm, base,
                      user=user, csrf_token=csrf, children=children, metadata=metadata, pivot_widget=_widget(base))
        )

    @router.post("/edit/{name}", response_class=HTMLResponse)
    async def edit_submit(
        name: str,
        request: Request,
        content: str = Form(...),
        tag: str = Form(""),
        set_active: str = Form("1"),
        password: Optional[str] = Form(None),
        csrf_token: Optional[str] = Form(None),
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        csrf = _get_csrf(request)

        if not _validate_csrf(request, csrf_token):
            return HTMLResponse(
                edit_page(name, content, manager.protected_mode, manager.has_llm, base,
                          error="Invalid or missing CSRF token.", user=user, csrf_token=csrf),
                status_code=403,
            )

        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            return HTMLResponse(
                edit_page(name, content, manager.protected_mode, manager.has_llm, base,
                          error=f"Prompt content exceeds maximum allowed size ({_MAX_CONTENT_BYTES // 1024} KB).",
                          user=user, csrf_token=csrf),
                status_code=400,
            )

        tag = tag or None
        do_activate = set_active == "1"
        editor_name = _actor(request)

        if manager.protected_mode and (do_activate or tag == "prod"):
            if not _check_password(password):
                return HTMLResponse(
                    edit_page(name, content, manager.protected_mode, manager.has_llm, base,
                              error="Incorrect password.", user=user, csrf_token=csrf)
                )

        version_num = await manager.storage.create_version(name, content, editor_name, tag, do_activate, tenant_id=manager.tenant_id)
        await manager.storage.insert_audit_log(
            action="version_created", performed_by=editor_name,
            prompt_name=name, version_id=version_num,
            detail=f"tag={tag} active={do_activate}", tenant_id=manager.tenant_id,
        )
        manager.cache.invalidate(name)
        if do_activate:
            manager.schedule_snapshot_refresh()

        # Auto-changelog: fetch old content for diff, generate in background
        if manager.auto_changelog and manager.llm:
            versions_after = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
            new_ver = next((v for v in versions_after if v["version_number"] == version_num), None)
            # Previous version is the one before the new one
            prev_ver = next((v for v in versions_after if v["version_number"] == version_num - 1), None)
            old_content = prev_ver["content"] if prev_ver else ""
            if new_ver:
                asyncio.get_event_loop().create_task(
                    _generate_changelog(manager, name, new_ver["id"], old_content, content, editor_name)
                )

        return RedirectResponse(f"{base}/detail/{name}", status_code=303)

    # ------------------------------------------------------------------
    # Activate version
    # ------------------------------------------------------------------

    @router.post("/activate/{name}/{version_id}", response_class=HTMLResponse)
    async def activate_version(
        name: str,
        version_id: str,
        request: Request,
        password: Optional[str] = Form(None),
        csrf_token: Optional[str] = Form(None),
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        csrf = _get_csrf(request)

        if not _validate_csrf(request, csrf_token):
            versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
            return HTMLResponse(
                prompt_detail(name, versions, manager.protected_mode, base, user=user, csrf_token=csrf)
                .replace("</nav>", '</nav><div class="container"><div class="error">Invalid or missing CSRF token.</div></div>'),
                status_code=403,
            )

        if manager.protected_mode and not _check_password(password):
            versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
            return HTMLResponse(
                prompt_detail(name, versions, manager.protected_mode, base, user=user, csrf_token=csrf)
                .replace("</nav>", '</nav><div class="container"><div class="error">Incorrect password.</div></div>')
            )

        await manager.storage.set_active_version(version_id, tenant_id=manager.tenant_id)
        await manager.storage.insert_audit_log(
            action="version_activated", performed_by=_actor(request),
            prompt_name=name, version_id=version_id,
            tenant_id=manager.tenant_id,
        )
        manager.cache.invalidate(name)
        manager.schedule_snapshot_refresh()
        return RedirectResponse(f"{base}/detail/{name}", status_code=303)

    # ------------------------------------------------------------------
    # Soft delete
    # ------------------------------------------------------------------

    @router.post("/delete/{name}")
    async def delete_prompt(
        name: str,
        request: Request,
        password: Optional[str] = Form(None),
        csrf_token: Optional[str] = Form(None),
    ):
        auth_redirect = _check_auth(request, min_role="admin")
        if auth_redirect:
            return auth_redirect

        base = _base(request)

        if not _validate_csrf(request, csrf_token):
            return JSONResponse({"error": "Invalid or missing CSRF token."}, status_code=403)

        if manager.protected_mode and not _check_password(password):
            return JSONResponse({"error": "Incorrect password."}, status_code=403)

        await manager.storage.soft_delete_prompt(name, tenant_id=manager.tenant_id)
        await manager.storage.insert_audit_log(
            action="prompt_deleted", performed_by=_actor(request),
            prompt_name=name, tenant_id=manager.tenant_id,
        )
        manager.cache.invalidate(name)
        return RedirectResponse(f"{base}/list", status_code=303)

    # ------------------------------------------------------------------
    # Agent Skills — versioned SKILL.md bundles share the prompt history backend.
    # ------------------------------------------------------------------

    @router.get("/skills", response_class=HTMLResponse)
    async def skills_index(request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        items = await manager.storage.fetch_all_skills(tenant_id=manager.tenant_id)
        for item in items:
            try:
                files = decode_skill(item.get("content") or "")
                item["description"] = parse_skill_markdown(files["SKILL.md"])["description"]
            except (ValueError, KeyError, TypeError):
                item["description"] = "Invalid active skill manifest"
        return HTMLResponse(skills_page(items, manager.protected_mode, _base(request), _get_user(request)))

    @router.get("/skills/import", response_class=HTMLResponse)
    async def skill_import_get(request: Request):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        return HTMLResponse(skill_import_page(manager.protected_mode, _base(request), _get_user(request), csrf_token=_get_csrf(request)))

    @router.post("/skills/import", response_class=HTMLResponse)
    async def skill_import_post(request: Request, skill_file: UploadFile = File(...),
                                set_active: str = Form("1"), csrf_token: Optional[str] = Form(None)):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        base, user, csrf = _base(request), _get_user(request), _get_csrf(request)
        if not _validate_csrf(request, csrf_token):
            return HTMLResponse(skill_import_page(manager.protected_mode, base, user, "Invalid or missing CSRF token.", csrf), status_code=403)
        try:
            data = await skill_file.read(_MAX_SKILL_UPLOAD_BYTES + 1)
            if len(data) > _MAX_SKILL_UPLOAD_BYTES:
                raise SkillValidationError("Uploaded archive exceeds the 5 MiB limit.")
            files = skill_from_zip(data)
            name = parse_skill_markdown(files["SKILL.md"])["name"]
            content = encode_skill(files)
            number = await manager.storage.create_skill_version(name, content, _actor(request), None,
                                                                set_active == "1", tenant_id=manager.tenant_id)
        except (SkillValidationError, ValueError) as exc:
            return HTMLResponse(skill_import_page(manager.protected_mode, base, user, str(exc), csrf), status_code=400)
        versions = await manager.storage.fetch_skill_versions(name, tenant_id=manager.tenant_id)
        version = next((item for item in versions if item.get("version_number") == number), None)
        await manager.storage.insert_audit_log(action="skill_imported", performed_by=_actor(request),
                                               prompt_name=name, version_id=version.get("id") if version else number,
                                               detail=f"active={set_active == '1'}", tenant_id=manager.tenant_id)
        if manager.auto_changelog and manager.llm and version:
            asyncio.get_event_loop().create_task(_generate_changelog(
                manager, SKILL_CHANGELOG_MARKER + name, version["id"], "", content, _actor(request)))
        return RedirectResponse(f"{base}/skills/detail/{name}", status_code=303)

    @router.get("/skills/export/{name}")
    async def skill_export(request: Request, name: str):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        active = await manager.storage.fetch_active_skill_version(name, tenant_id=manager.tenant_id)
        if not active:
            return JSONResponse({"detail": "Active skill not found."}, status_code=404)
        try:
            files = decode_skill(active["content"])
        except SkillValidationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        import io
        import zipfile
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, content in files.items():
                archive.writestr(f"{name}/{path}", content.encode("utf-8"))
        return Response(stream.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{name}.zip"',
        })

    @router.get("/skills/detail/{name}", response_class=HTMLResponse)
    async def skill_detail(request: Request, name: str):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        versions = await manager.storage.fetch_skill_versions(name, tenant_id=manager.tenant_id)
        if not versions:
            return RedirectResponse(f"{_base(request)}/skills", status_code=303)
        active = await manager.storage.fetch_active_skill_version(name, tenant_id=manager.tenant_id)
        display_version = active
        if not display_version and versions:
            display_version = versions[0]
        skill = None
        if display_version:
            files = decode_skill(display_version["content"])
            skill = parse_skill_markdown(files["SKILL.md"])
            skill.update({"version": display_version["version_number"] if active else None,
                          "references": [path for path in files if path.startswith("references/")]})
        changelog = await manager.storage.fetch_skill_changelog(name, tenant_id=manager.tenant_id)
        current_user = _get_user(request)
        can_delete = manager.auth_mode != "rbac" or bool(current_user and current_user.get("role") == "admin")
        return HTMLResponse(skill_detail_page(name, skill, versions, changelog, manager.protected_mode,
                                              _base(request), current_user, _get_csrf(request), can_delete))

    @router.get("/skills/edit/{name}", response_class=HTMLResponse)
    async def skill_edit_get(request: Request, name: str):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        if name == "__new__":
            return HTMLResponse(skill_edit_page("", "", "", {}, manager.protected_mode, _base(request),
                                                _get_user(request), is_new=True, csrf_token=_get_csrf(request),
                                                pivot_widget=_widget(_base(request))))
        active = await manager.storage.fetch_active_skill_version(name, tenant_id=manager.tenant_id)
        if not active:
            versions = await manager.storage.fetch_skill_versions(name, tenant_id=manager.tenant_id)
            active = versions[0] if versions else None
        files = decode_skill(active["content"]) if active else {}
        manifest = parse_skill_markdown(files["SKILL.md"]) if files else {"description": "", "body": ""}
        refs = {path: value for path, value in files.items() if path.startswith("references/")}
        return HTMLResponse(skill_edit_page(name, manifest["description"], manifest["body"], refs,
                                            manager.protected_mode, _base(request), _get_user(request),
                                            csrf_token=_get_csrf(request), pivot_widget=_widget(_base(request))))

    # ------------------------------------------------------------------
    async def _save_skill_version(request: Request, name: str, description: str, instructions: str,
                                  references_json: str, tag: str, set_active: str,
                                  csrf_token: Optional[str], password: Optional[str], is_new: bool):
        base, user, csrf = _base(request), _get_user(request), _get_csrf(request)
        safe_name = name.strip()

        def render_error(message: str, status_code: int = 400, references: Optional[dict] = None):
            return HTMLResponse(skill_edit_page(safe_name, description, instructions, references or {},
                                                manager.protected_mode, base, user, error=message,
                                                is_new=is_new, csrf_token=csrf, pivot_widget=_widget(base)),
                                status_code=status_code)

        if not _validate_csrf(request, csrf_token):
            return render_error("Invalid or missing CSRF token.", 403)
        if manager.protected_mode and (set_active == "1" or tag == "prod") and not _check_password(password):
            return render_error("Incorrect admin password.")
        if len(instructions.encode("utf-8")) > MAX_SKILL_BUNDLE_BYTES:
            return render_error("Skill instructions exceed the 5 MiB bundle limit.")
        if tag not in ("", "prod", "staging", "experiment") or set_active not in ("0", "1"):
            return render_error("Choose a valid release tag and activation state.")
        try:
            refs = json.loads(references_json or "{}")
            if not isinstance(refs, dict):
                raise SkillValidationError("References must be a JSON object mapping paths to Markdown text.")
            if any(not isinstance(path, str) or not path.startswith("references/") for path in refs):
                raise SkillValidationError("The editor accepts only files under references/.")
            markdown = (f"---\nname: {safe_name}\ndescription: {json.dumps(description, ensure_ascii=False)}"
                        f"\n---\n\n{instructions.strip()}\n")
            files = normalize_skill_files({"SKILL.md": markdown, **refs}, expected_name=safe_name)
            bundle = encode_skill(files)
        except (json.JSONDecodeError, SkillValidationError, TypeError) as exc:
            return render_error(str(exc), references=refs if "refs" in locals() and isinstance(refs, dict) else {})
        creator = _actor(request)
        version_number = await manager.storage.create_skill_version(
            safe_name, bundle, creator, tag or None, set_active == "1", tenant_id=manager.tenant_id,
        )
        versions = await manager.storage.fetch_skill_versions(safe_name, tenant_id=manager.tenant_id)
        new_version = next((item for item in versions if item.get("version_number") == version_number), None)
        await manager.storage.insert_audit_log(
            action="skill_version_created", performed_by=creator, prompt_name=safe_name,
            version_id=new_version.get("id") if new_version else version_number,
            detail=f"tag={tag or None} active={set_active == '1'}", tenant_id=manager.tenant_id,
        )
        if manager.auto_changelog and manager.llm and new_version:
            older = [item for item in versions if item.get("version_number", 0) < version_number]
            old_content = older[0].get("content", "") if older else ""
            asyncio.get_event_loop().create_task(_generate_changelog(
                manager, SKILL_CHANGELOG_MARKER + safe_name, new_version["id"], old_content, bundle, creator))
        return RedirectResponse(f"{base}/skills/detail/{safe_name}", status_code=303)

    @router.post("/skills/edit/__new__")
    async def skill_create_post(request: Request, skill_name: str = Form(...), description: str = Form(...),
                                instructions: str = Form(...), references_json: str = Form("{}"),
                                tag: str = Form(""), set_active: str = Form("0"), password: Optional[str] = Form(None),
                                csrf_token: Optional[str] = Form(None)):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        return await _save_skill_version(request, skill_name, description, instructions, references_json,
                                         tag, set_active, csrf_token, password, True)

    @router.post("/skills/edit/{name}")
    async def skill_version_post(request: Request, name: str, description: str = Form(...),
                                 instructions: str = Form(...), references_json: str = Form("{}"),
                                 tag: str = Form(""), set_active: str = Form("0"), password: Optional[str] = Form(None),
                                 csrf_token: Optional[str] = Form(None)):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        return await _save_skill_version(request, name, description, instructions, references_json,
                                         tag, set_active, csrf_token, password, False)

    @router.post("/skills/activate/{name}/{version_id}")
    async def skill_activate(request: Request, name: str, version_id: str, csrf_token: Optional[str] = Form(None)):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        if not _validate_csrf(request, csrf_token):
            return JSONResponse({"detail": "Invalid or missing CSRF token."}, status_code=403)
        versions = await manager.storage.fetch_skill_versions(name, tenant_id=manager.tenant_id)
        if not any(str(version.get("id")) == str(version_id) for version in versions):
            return JSONResponse({"detail": "Skill version not found."}, status_code=404)
        await manager.storage.activate_skill_version(version_id, tenant_id=manager.tenant_id)
        await manager.storage.insert_audit_log(action="skill_activated", performed_by=_actor(request),
                                               prompt_name=name, version_id=version_id,
                                               tenant_id=manager.tenant_id)
        return RedirectResponse(f"{_base(request)}/skills/detail/{name}", status_code=303)

    @router.post("/skills/delete/{name}")
    async def skill_delete(request: Request, name: str, csrf_token: Optional[str] = Form(None)):
        auth_redirect = _check_auth(request, min_role="admin")
        if auth_redirect:
            return auth_redirect
        if not _validate_csrf(request, csrf_token):
            return JSONResponse({"detail": "Invalid or missing CSRF token."}, status_code=403)
        await manager.storage.delete_skill(name, tenant_id=manager.tenant_id)
        await manager.storage.insert_audit_log(action="skill_deleted", performed_by=_actor(request),
                                               prompt_name=name, tenant_id=manager.tenant_id)
        return RedirectResponse(f"{_base(request)}/skills", status_code=303)

    @router.get("/skills/diff/{name}", response_class=HTMLResponse)
    async def skill_diff(request: Request, name: str, v1: Optional[str] = None, v2: Optional[str] = None):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        versions = await manager.storage.fetch_skill_versions(name, tenant_id=manager.tenant_id)
        one = next((v for v in versions if str(v.get("id")) == str(v1)), None) if v1 else None
        two = next((v for v in versions if str(v.get("id")) == str(v2)), None) if v2 else None
        if one:
            one = dict(one); one["content"] = decode_skill(one["content"])["SKILL.md"]
        if two:
            two = dict(two); two["content"] = decode_skill(two["content"])["SKILL.md"]
        return HTMLResponse(skill_diff_page(name, versions, one, two, _base(request), _get_user(request)))

    @router.get("/skills/test/{name}", response_class=HTMLResponse)
    async def skill_test_get(request: Request, name: str):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        versions = await manager.storage.fetch_skill_versions(name, tenant_id=manager.tenant_id)
        return HTMLResponse(skill_test_page(name, versions, manager.protected_mode, manager.has_llm,
                                            _base(request), _get_user(request)))

    @router.post("/skills/test/{name}", response_class=HTMLResponse)
    async def skill_test_post(request: Request, name: str, v1: str = Form(...), v2: str = Form(...),
                              task: str = Form(...)):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        versions = await manager.storage.fetch_skill_versions(name, tenant_id=manager.tenant_id)
        first = next((v for v in versions if str(v.get("id")) == str(v1)), None)
        second = next((v for v in versions if str(v.get("id")) == str(v2)), None)
        if not manager.llm:
            return HTMLResponse(skill_test_page(name, versions, manager.protected_mode, False,
                                                _base(request), _get_user(request)))
        if not first or not second:
            return JSONResponse({"detail": "Select versions belonging to this skill."}, status_code=400)
        if len(task.encode("utf-8")) > _MAX_LOG_BYTES:
            return JSONResponse({"detail": "Evaluation task is too long."}, status_code=400)
        first_files, second_files = decode_skill(first["content"]), decode_skill(second["content"])
        first_manifest = parse_skill_markdown(first_files["SKILL.md"])
        second_manifest = parse_skill_markdown(second_files["SKILL.md"])
        first_context = f"Skill: {name}\n{first_manifest['description']}\n\n{first_manifest['body']}"
        second_context = f"Skill: {name}\n{second_manifest['description']}\n\n{second_manifest['body']}"
        for path, content in first_files.items():
            if path.startswith("references/"):
                first_context += f"\n\nReference {path}:\n{content}"
        for path, content in second_files.items():
            if path.startswith("references/"):
                second_context += f"\n\nReference {path}:\n{content}"
        try:
            output_a, output_b = await asyncio.gather(
                manager.llm.run(first_context, task),
                manager.llm.run(second_context, task),
            )
        except Exception as exc:
            output_a = output_b = f"Evaluation failed: {exc}"
        return HTMLResponse(skill_test_page(name, versions, manager.protected_mode, True, _base(request),
                                            _get_user(request), output_a, output_b, v1, v2, task))

    # Prompt Diff & A/B test & Logs
    # ------------------------------------------------------------------

    @router.get("/diff/{name}", response_class=HTMLResponse)
    async def diff_view(name: str, request: Request, v1: Optional[str] = None, v2: Optional[str] = None):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
        ver1 = await manager.storage.fetch_version_by_id(v1, tenant_id=manager.tenant_id) if v1 else None
        ver2 = await manager.storage.fetch_version_by_id(v2, tenant_id=manager.tenant_id) if v2 else None
        return HTMLResponse(diff_page(name, versions, ver1, ver2, manager.protected_mode, _base(request), user=user))

    @router.get("/test/{name}", response_class=HTMLResponse)
    async def ab_test(name: str, request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
        return HTMLResponse(ab_test_page(name, versions, manager.protected_mode, manager.has_llm, _base(request), user=user))

    @router.get("/logs", response_class=HTMLResponse)
    async def view_logs(request: Request, prompt: Optional[str] = None):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        logs = await manager.storage.fetch_logs(prompt_name=prompt, limit=100, tenant_id=manager.tenant_id)
        return HTMLResponse(logs_page(logs, manager.protected_mode, _base(request), prompt_filter=prompt or "", user=user))

    # ------------------------------------------------------------------
    # Export / Import JSON
    # ------------------------------------------------------------------

    @router.get("/export")
    async def export_json(request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        data = await manager.storage.export_prompts(tenant_id=manager.tenant_id)
        content = json.dumps(data, indent=2, ensure_ascii=False)
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=prompts.json"},
        )

    @router.get("/import", response_class=HTMLResponse)
    async def import_form(request: Request):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        user = _get_user(request)
        base = _base(request)
        csrf = _get_csrf(request)
        csrf_field = f'<input type="hidden" name="csrf_token" value="{_escape(csrf)}">' if csrf else ""
        body = f"""
<h1>Import Prompts</h1>
<div class="card">
  <p class="text-muted" style="margin-bottom:16px;">Upload a JSON file formatted as <code>{{"prompt_name": "prompt content"}}</code>.</p>
  <form method="post" action="{base}/import" enctype="multipart/form-data">
    {csrf_field}
    <div class="form-group">
      <label for="file">JSON File</label>
      <input type="file" id="file" name="file" accept=".json">
    </div>
    <div class="flex mt-16">
      <button type="submit" class="btn btn-primary">Import</button>
      <a href="{base}/list" class="btn btn-ghost">Cancel</a>
    </div>
  </form>
</div>"""
        from .templates import _layout
        return HTMLResponse(_layout("Import Prompts", body, manager.protected_mode, base, user=user))

    @router.post("/import", response_class=HTMLResponse)
    async def import_json_submit(
        request: Request,
        file: UploadFile = File(...),
        csrf_token: Optional[str] = Form(None),
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)

        if not _validate_csrf(request, csrf_token):
            from .templates import _layout
            body = '<div class="error">Invalid or missing CSRF token.</div>'
            return HTMLResponse(_layout("Import Error", body, manager.protected_mode, base, user=user), status_code=403)

        raw = await file.read()
        if len(raw) > _MAX_IMPORT_BYTES:
            from .templates import _layout
            body = '<div class="error">Uploaded file exceeds the 5 MB size limit.</div>'
            return HTMLResponse(_layout("Import Error", body, manager.protected_mode, base, user=user), status_code=400)

        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            from .templates import _layout
            body = f'<div class="error">Invalid JSON file: {_escape(str(exc))}</div>'
            return HTMLResponse(_layout("Import Error", body, manager.protected_mode, base, user=user), status_code=400)

        if not isinstance(data, dict):
            from .templates import _layout
            body = '<div class="error">JSON must be an object mapping prompt names to content strings.</div>'
            return HTMLResponse(_layout("Import Error", body, manager.protected_mode, base, user=user), status_code=400)

        actor = _actor(request)
        result = await manager.storage.import_prompts(data, imported_by=actor, tenant_id=manager.tenant_id)

        # Invalidate cache for all imported prompts
        for name in list(result.get("created", [])) + list(result.get("updated", [])):
            manager.cache.invalidate(name)

        await manager.storage.insert_audit_log(
            action="prompts_imported", performed_by=actor,
            detail=f"created={len(result.get('created', []))} updated={len(result.get('updated', []))}",
            tenant_id=manager.tenant_id,
        )

        from .templates import _layout
        created = result.get("created", [])
        updated = result.get("updated", [])
        body = f"""
<h1>Import Complete</h1>
<div class="card">
  <p class="success">Imported successfully: {len(created)} created, {len(updated)} updated.</p>
  {'<p><strong>Created:</strong> ' + ', '.join(created) + '</p>' if created else ''}
  {'<p><strong>Updated:</strong> ' + ', '.join(updated) + '</p>' if updated else ''}
  <div class="mt-16"><a href="{base}/list" class="btn btn-primary">Back to List</a></div>
</div>"""
        return HTMLResponse(_layout("Import Complete", body, manager.protected_mode, base, user=user))

    # ------------------------------------------------------------------
    # LLM API endpoints
    # ------------------------------------------------------------------

    @router.post("/api/suggest")
    async def api_suggest(request: Request):
        if not manager.has_llm:
            return JSONResponse({"detail": "LLM provider not configured."}, status_code=403)
        try:
            body = await request.json()
            content = str(body.get("content", ""))[:_MAX_CONTENT_BYTES]
        except Exception:
            return JSONResponse({"detail": "Invalid JSON body."}, status_code=400)

        try:
            suggestion = await manager.llm.suggest(content)
            return JSONResponse({"suggestion": suggestion})
        except Exception as exc:
            return JSONResponse({"detail": "LLM request failed. Please try again later."}, status_code=502)

    @router.post("/api/run")
    async def api_run(request: Request):
        if not manager.has_llm:
            return JSONResponse({"detail": "LLM provider not configured."}, status_code=403)
        try:
            body = await request.json()
            version_id = body.get("version_id")
            user_input = str(body.get("input", ""))[:_MAX_LOG_BYTES]
        except Exception:
            return JSONResponse({"detail": "Invalid JSON body."}, status_code=400)

        version = await manager.storage.fetch_version_by_id(version_id, tenant_id=manager.tenant_id)
        if not version:
            return JSONResponse({"detail": "Version not found."}, status_code=404)

        try:
            output = await manager.llm.run(version["content"], user_input)
            return JSONResponse({"output": output})
        except Exception as exc:
            return JSONResponse({"detail": "LLM request failed. Please try again later."}, status_code=502)

    # ------------------------------------------------------------------
    # Phase 1 Pivot routes — Metadata, Changelog, App Context, Tree
    # ------------------------------------------------------------------

    @router.get("/tree", response_class=HTMLResponse)
    async def tree_view(request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        prompts = await manager.storage.fetch_all_prompts_with_metadata(tenant_id=manager.tenant_id)
        return HTMLResponse(tree_page(prompts, manager.protected_mode, _base(request), user=user))

    @router.get("/metadata/{name}", response_class=HTMLResponse)
    async def metadata_get(name: str, request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        csrf = _get_csrf(request)
        meta = await manager.storage.get_prompt_metadata(name, tenant_id=manager.tenant_id)
        all_prompts = await manager.storage.fetch_all_prompts_with_metadata(tenant_id=manager.tenant_id)
        return HTMLResponse(
            metadata_page(name, meta, all_prompts, manager.protected_mode,
                          _base(request), user=user, csrf_token=csrf)
        )

    @router.post("/metadata/{name}", response_class=HTMLResponse)
    async def metadata_post(
        name: str,
        request: Request,
        prompt_type: str = Form("unclassified"),
        parent_prompt_id: str = Form(""),
        sensitivity: str = Form("low"),
        feature_area: str = Form(""),
        owner: str = Form(""),
        purpose: str = Form(""),
        called_from: str = Form(""),
        model_used: str = Form(""),
        input_variables: str = Form(""),
        notes: str = Form(""),
        csrf_token: Optional[str] = Form(None),
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        csrf = _get_csrf(request)

        if not _validate_csrf(request, csrf_token):
            all_prompts = await manager.storage.fetch_all_prompts_with_metadata(tenant_id=manager.tenant_id)
            return HTMLResponse(
                metadata_page(name, {}, all_prompts, manager.protected_mode, base,
                              user=user, csrf_token=csrf, error="Invalid or missing CSRF token."),
                status_code=403,
            )

        fields = {
            "prompt_type": prompt_type or "unclassified",
            "parent_prompt_id": parent_prompt_id or None,
            "sensitivity": sensitivity or "low",
            "feature_area": feature_area,
            "owner": owner,
            "purpose": purpose,
            "called_from": called_from,
            "model_used": model_used,
            "input_variables": input_variables,
            "notes": notes,
        }
        await manager.storage.upsert_prompt_metadata(name, fields, tenant_id=manager.tenant_id)
        await manager.storage.insert_audit_log(
            action="metadata_updated", performed_by=_actor(request),
            prompt_name=name, tenant_id=manager.tenant_id,
        )

        meta = await manager.storage.get_prompt_metadata(name, tenant_id=manager.tenant_id)
        all_prompts = await manager.storage.fetch_all_prompts_with_metadata(tenant_id=manager.tenant_id)
        return HTMLResponse(
            metadata_page(name, meta, all_prompts, manager.protected_mode, base,
                          user=user, csrf_token=csrf, success="Metadata saved.")
        )

    @router.get("/changelog/{name}", response_class=HTMLResponse)
    async def changelog_get(name: str, request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        csrf = _get_csrf(request)
        changelog = await manager.storage.get_changelog(name, tenant_id=manager.tenant_id)
        versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
        return HTMLResponse(
            changelog_page(name, changelog, versions, manager.protected_mode,
                           _base(request), user=user, csrf_token=csrf)
        )

    @router.get("/changelog/{name}/edit/{version_id}", response_class=HTMLResponse)
    async def changelog_edit_get(name: str, version_id: str, request: Request):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        csrf = _get_csrf(request)
        version = await manager.storage.fetch_version_by_id(version_id, tenant_id=manager.tenant_id)
        if not version:
            return HTMLResponse("Version not found.", status_code=404)
        existing = await manager.storage.get_changelog_for_version(version_id, tenant_id=manager.tenant_id)
        entry_text = existing.get("entry", "") if existing else ""
        return HTMLResponse(
            changelog_edit_page(name, version_id, version["version_number"], entry_text,
                                manager.protected_mode, _base(request), user=user, csrf_token=csrf)
        )

    @router.post("/changelog/{name}/edit/{version_id}", response_class=HTMLResponse)
    async def changelog_edit_post(
        name: str,
        version_id: str,
        request: Request,
        entry: str = Form(...),
        csrf_token: Optional[str] = Form(None),
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        csrf = _get_csrf(request)

        if not _validate_csrf(request, csrf_token):
            version = await manager.storage.fetch_version_by_id(version_id, tenant_id=manager.tenant_id)
            v_num = version["version_number"] if version else "?"
            return HTMLResponse(
                changelog_edit_page(name, version_id, v_num, entry,
                                    manager.protected_mode, base, user=user, csrf_token=csrf),
                status_code=403,
            )

        await manager.storage.upsert_changelog_entry(
            version_id=version_id,
            entry=entry,
            generated_by="user",
            created_by=_actor(request),
            tenant_id=manager.tenant_id,
        )
        return RedirectResponse(f"{base}/changelog/{name}", status_code=303)

    @router.get("/context", response_class=HTMLResponse)
    async def context_get(request: Request):
        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        csrf = _get_csrf(request)
        ctx = await manager.storage.get_app_context(tenant_id=manager.tenant_id)
        return HTMLResponse(
            app_context_page(ctx, manager.protected_mode, _base(request), user=user, csrf_token=csrf)
        )

    @router.post("/context", response_class=HTMLResponse)
    async def context_post(
        request: Request,
        content: str = Form(...),
        csrf_token: Optional[str] = Form(None),
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        csrf = _get_csrf(request)

        if not _validate_csrf(request, csrf_token):
            ctx = await manager.storage.get_app_context(tenant_id=manager.tenant_id)
            return HTMLResponse(
                app_context_page(ctx, manager.protected_mode, base, user=user, csrf_token=csrf,
                                 error="Invalid or missing CSRF token."),
                status_code=403,
            )

        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            ctx = await manager.storage.get_app_context(tenant_id=manager.tenant_id)
            return HTMLResponse(
                app_context_page(ctx, manager.protected_mode, base, user=user, csrf_token=csrf,
                                 error=f"Content exceeds maximum size ({_MAX_CONTENT_BYTES // 1024} KB)."),
                status_code=400,
            )

        await manager.storage.save_app_context(
            content=content,
            created_by=_actor(request),
            tenant_id=manager.tenant_id,
        )
        await manager.storage.insert_audit_log(
            action="app_context_updated", performed_by=_actor(request),
            tenant_id=manager.tenant_id,
        )
        ctx = await manager.storage.get_app_context(tenant_id=manager.tenant_id)
        return HTMLResponse(
            app_context_page(ctx, manager.protected_mode, base, user=user, csrf_token=csrf,
                             success="Application context saved.")
        )

    # ------------------------------------------------------------------
    # Phase 2 — Pivot observe (SSE) and chat (chunked HTTP)
    # ------------------------------------------------------------------

    @router.get("/pivot/observe")
    async def pivot_observe(request: Request, page: str = "", prompt_name: str = ""):
        """
        SSE endpoint: stream proactive observations for the current page context.
        The client opens a persistent EventSource connection; events are streamed
        as they become available and a 'done' event closes the stream.
        """
        if not manager.pivot_enabled:
            return JSONResponse({"error": "Pivot is not enabled."}, status_code=404)

        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            # Can't redirect SSE — return 401
            return JSONResponse({"error": "Authentication required."}, status_code=401)

        context = {
            "page": page,
            "prompt_name": prompt_name,
            "tenant_id": manager.tenant_id,
        }

        from ..pivot import PivotAgent

        async def event_generator():
            agent = PivotAgent(manager)
            try:
                async for obs in agent.observe(context):
                    obs_type = obs.get("type", "observation")
                    yield {
                        "event": obs_type,
                        "data": json.dumps(obs),
                    }
                    # Small yield so the loop can flush
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
            finally:
                yield {"event": "done", "data": "{}"}

        # Use sse-starlette if available, otherwise fall back to StreamingResponse
        if _HAS_SSE_STARLETTE:
            return SSEResponse(event_generator(), ping=15)

        # Fallback: manual SSE via StreamingResponse
        async def manual_sse():
            async for event in event_generator():
                evt_name = event.get("event", "message")
                evt_data = event.get("data", "")
                yield f"event: {evt_name}\ndata: {evt_data}\n\n"

        return StreamingResponse(
            manual_sse(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @router.post("/pivot/chat")
    async def pivot_chat(request: Request):
        """
        Chunked HTTP endpoint: stream Pivot's chat reply token by token.
        Request body: {"session_id": str, "message": str, "context": {...}}
        Each chunk is a JSON line: {"type": "text"|"tool_call"|"error"|"done", "content": str}
        """
        if not manager.pivot_enabled:
            return JSONResponse({"error": "Pivot is not enabled."}, status_code=404)

        auth_redirect = _check_auth(request, min_role="viewer")
        if auth_redirect:
            return JSONResponse({"error": "Authentication required."}, status_code=401)

        try:
            body = await request.json()
            session_id = str(body.get("session_id", "default"))
            message = str(body.get("message", "")).strip()
            context = body.get("context", {})
        except Exception:
            return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

        if not message:
            return JSONResponse({"error": "message is required."}, status_code=400)

        from ..pivot import PivotAgent

        async def stream_reply():
            agent = PivotAgent(manager)
            try:
                async for chunk in agent.chat(
                    session_id=session_id,
                    message=message,
                    context=context,
                    tenant_id=manager.tenant_id,
                ):
                    yield chunk
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                yield "data: " + json.dumps({"type": "error", "content": str(exc)}) + "\n\n"

        return StreamingResponse(
            stream_reply(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router


# Public name kept for backward compatibility
build_router = _build_router
