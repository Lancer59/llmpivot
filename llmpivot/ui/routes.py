"""
FastAPI router for the Prompt Manager UI.
Supports Auth & RBAC session management, multi-tenancy, and pluggable storage engines.
"""

import json
from typing import Optional, TYPE_CHECKING
from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from ..auth import (
    hash_password,
    verify_password,
    create_token,
    get_current_user_from_request,
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

if TYPE_CHECKING:
    from ..manager import PromptManager


def _base(request: Request) -> str:
    root = request.scope.get("root_path", "").rstrip("/")
    return root


def build_router(manager: "PromptManager") -> APIRouter:
    router = APIRouter()

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
        user = await manager.storage.get_user(username)
        if not user or not verify_password(password, user.get("password_hash", "")):
            return HTMLResponse(login_page(base, error="Invalid username or password."), status_code=400)

        token = create_token(
            {"id": user["id"], "username": user["username"], "role": user.get("role", "editor")},
            manager.secret_key,
        )
        resp = RedirectResponse(f"{base}/list", status_code=303)
        resp.set_cookie("llmpivot_session", token, httponly=True, samesite="lax")
        return resp

    @router.get("/logout")
    async def logout(request: Request):
        resp = RedirectResponse(f"{_base(request)}/login", status_code=303)
        resp.delete_cookie("llmpivot_session")
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
        return HTMLResponse(users_page(users, user, _base(request)))

    @router.post("/users", response_class=HTMLResponse)
    async def create_user_route(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form("editor"),
        email: str = Form(""),
    ):
        auth_redirect = _check_auth(request, min_role="admin")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        existing = await manager.storage.get_user(username)
        if existing:
            users = await manager.storage.fetch_users(tenant_id=manager.tenant_id)
            return HTMLResponse(users_page(users, user, base, error=f"Username '{username}' already exists."), status_code=400)

        pwd_hash = hash_password(password)
        await manager.storage.create_user(
            username=username,
            password_hash=pwd_hash,
            role=role,
            email=email,
            tenant_id=manager.tenant_id,
        )
        users = await manager.storage.fetch_users(tenant_id=manager.tenant_id)
        return HTMLResponse(users_page(users, user, base, success=f"User '{username}' created successfully."))

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
        versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
        return HTMLResponse(prompt_detail(name, versions, manager.protected_mode, _base(request), user=user))

    # ------------------------------------------------------------------
    # Edit / Create
    # ------------------------------------------------------------------

    @router.get("/edit/__new__", response_class=HTMLResponse)
    async def new_prompt_form(request: Request):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        return HTMLResponse(
            edit_page("__new__", "", manager.protected_mode, manager.has_llm, _base(request), is_new=True, user=user)
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
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        tag = tag or None
        do_activate = set_active == "1"
        creator = edited_by or (user.get("username") if user else "") or "anonymous"

        if manager.protected_mode and (do_activate or tag == "prod"):
            if not _check_password(password):
                return HTMLResponse(
                    edit_page("__new__", content, manager.protected_mode, manager.has_llm, base, error="Incorrect password.", is_new=True, user=user)
                )

        await manager.storage.create_version(prompt_name, content, creator, tag, do_activate, tenant_id=manager.tenant_id)
        manager.cache.invalidate(prompt_name)
        return RedirectResponse(f"{base}/detail/{prompt_name}", status_code=303)

    @router.get("/edit/{name}", response_class=HTMLResponse)
    async def edit_form(name: str, request: Request):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect
        user = _get_user(request)
        versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
        active = next((v for v in versions if v["is_active"]), None)
        current = active["content"] if active else ""
        return HTMLResponse(
            edit_page(name, current, manager.protected_mode, manager.has_llm, _base(request), user=user)
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
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        tag = tag or None
        do_activate = set_active == "1"
        editor_name = edited_by or (user.get("username") if user else "") or "anonymous"

        if manager.protected_mode and (do_activate or tag == "prod"):
            if not _check_password(password):
                return HTMLResponse(
                    edit_page(name, content, manager.protected_mode, manager.has_llm, base, error="Incorrect password.", user=user)
                )

        await manager.storage.create_version(name, content, editor_name, tag, do_activate, tenant_id=manager.tenant_id)
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
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        if manager.protected_mode and not _check_password(password):
            versions = await manager.storage.fetch_prompt_versions(name, tenant_id=manager.tenant_id)
            return HTMLResponse(
                prompt_detail(name, versions, manager.protected_mode, base, user=user)
                .replace("</nav>", '</nav><div class="container"><div class="error">Incorrect password.</div></div>')
            )
        await manager.storage.set_active_version(version_id, tenant_id=manager.tenant_id)
        manager.cache.invalidate(name)
        return RedirectResponse(f"{base}/detail/{name}", status_code=303)

    # ------------------------------------------------------------------
    # Soft delete
    # ------------------------------------------------------------------

    @router.post("/delete/{name}")
    async def delete_prompt(name: str, request: Request, password: Optional[str] = Form(None)):
        auth_redirect = _check_auth(request, min_role="admin")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        if manager.protected_mode and not _check_password(password):
            return JSONResponse({"error": "Incorrect password."}, status_code=403)
        await manager.storage.soft_delete_prompt(name, tenant_id=manager.tenant_id)
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
        body = f"""
<h1>Import Prompts</h1>
<div class="card">
  <p class="text-muted" style="margin-bottom:16px;">Upload a JSON file formatted as <code>{{"prompt_name": "prompt content"}}</code>.</p>
  <form method="post" action="{base}/import" enctype="multipart/form-data">
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
    ):
        auth_redirect = _check_auth(request, min_role="editor")
        if auth_redirect:
            return auth_redirect

        base = _base(request)
        user = _get_user(request)
        raw = await file.read()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("JSON must be a dictionary.")
        except Exception as exc:
            from .templates import _layout
            return HTMLResponse(_layout("Import Prompts", f'<div class="error">Invalid JSON: {exc}</div>', manager.protected_mode, base, user=user), status_code=400)

        imported_by = user.get("username") if user else "import"
        result = await manager.storage.import_prompts(data, imported_by=imported_by, tenant_id=manager.tenant_id)
        for name in data:
            manager.cache.invalidate(name)

        from .templates import _layout
        body = f"""
<h1>Import Complete</h1>
<div class="card">
  <p style="color:#9ae6b4;">Created {len(result['created'])} prompts. Updated {len(result['updated'])} prompts.</p>
</div>
<div class="flex mt-16"><a href="{base}/list" class="btn btn-primary">View All Prompts</a></div>"""
        return HTMLResponse(_layout("Import Complete", body, manager.protected_mode, base, user=user))

    # ------------------------------------------------------------------
    # API: LLM suggest
    # ------------------------------------------------------------------

    @router.post("/api/suggest")
    async def suggest(request: Request):
        if not manager.has_llm:
            return JSONResponse({"detail": "LLM not configured."}, status_code=403)
        body = await request.json()
        content = body.get("content", "").strip()
        if not content:
            return JSONResponse({"detail": "content is required."}, status_code=400)
        try:
            suggestion = await manager.llm.suggest(content)
            return JSONResponse({"suggestion": suggestion})
        except Exception as exc:
            return JSONResponse({"detail": f"LLM error: {exc}"}, status_code=502)

    # ------------------------------------------------------------------
    # API: A/B run
    # ------------------------------------------------------------------

    @router.post("/api/run")
    async def ab_run(request: Request):
        if not manager.has_llm:
            return JSONResponse({"detail": "LLM not configured."}, status_code=403)
        body = await request.json()
        version_id = body.get("version_id")
        input_text = body.get("input", "").strip()
        if not version_id:
            return JSONResponse({"detail": "version_id is required."}, status_code=400)
        version = await manager.storage.fetch_version_by_id(version_id, tenant_id=manager.tenant_id)
        if not version:
            return JSONResponse({"detail": "Version not found."}, status_code=404)
        try:
            output = await manager.llm.run(version["content"], input_text)
            return JSONResponse({"output": output})
        except Exception as exc:
            return JSONResponse({"detail": f"LLM error: {exc}"}, status_code=502)

    return router
