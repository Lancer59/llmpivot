"""
FastAPI router for the Prompt Manager UI.
Supports Auth & RBAC session management, multi-tenancy, and pluggable storage engines.

Production hardening in this version:
- cookie_secure passed from PromptManager (defaults True).
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

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
)
from .helpers import escape as _escape

if TYPE_CHECKING:
    from ..manager import PromptManager

# ---------------------------------------------------------------------------
# Input size constants
# ---------------------------------------------------------------------------
_MAX_CONTENT_BYTES = 500 * 1024   # 500 KB — prompt content
_MAX_LOG_BYTES = 10 * 1024        # 10 KB  — log input/output text
_MAX_IMPORT_BYTES = 5 * 1024 * 1024  # 5 MB  — JSON import file upload cap

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


def _build_router(manager: "PromptManager") -> APIRouter:
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
        return HTMLResponse(prompt_list(prompts, manager.protected_mode, _base(request), user=user))

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
        versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
        return HTMLResponse(prompt_detail(name, versions, manager.protected_mode, _base(request), user=user, csrf_token=csrf))

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
        return HTMLResponse(
            edit_page("__new__", "", manager.protected_mode, manager.has_llm, _base(request), is_new=True, user=user, csrf_token=csrf)
        )

    @router.post("/edit/__new__", response_class=HTMLResponse)
    async def new_prompt_submit(
        request: Request,
        prompt_name: str = Form(...),
        content: str = Form(...),
        edited_by: str = Form(""),
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
        creator = edited_by or (user.get("username") if user else "") or "anonymous"

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
        return RedirectResponse(f"{base}/detail/{prompt_name}", status_code=303)

    @router.get("/edit/{name}", response_class=HTMLResponse)
    async def edit_form(name: str, request: Request):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        csrf = _get_csrf(request)
        versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
        active = next((v for v in versions if v["is_active"]), None)
        current = active["content"] if active else ""
        return HTMLResponse(
            edit_page(name, current, manager.protected_mode, manager.has_llm, _base(request), user=user, csrf_token=csrf)
        )

    @router.post("/edit/{name}", response_class=HTMLResponse)
    async def edit_submit(
        name: str,
        request: Request,
        content: str = Form(...),
        edited_by: str = Form(""),
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
        editor_name = edited_by or (user.get("username") if user else "") or "anonymous"

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
    # Diff & A/B test & Logs
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

    return router


# Public name kept for backward compatibility
build_router = _build_router
