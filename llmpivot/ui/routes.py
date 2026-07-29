"""
FastAPI router for the Prompt Manager UI.
All routes are relative - mount at any prefix with app.mount().
"""

import json
from typing import Optional, TYPE_CHECKING
from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from ..db import (
    fetch_all_prompts,
    fetch_prompt_versions,
    fetch_version_by_id,
    create_version,
    set_active_version,
    soft_delete_prompt,
    fetch_logs,
    export_prompts,
    import_prompts,
)
from .templates import (
    prompt_list,
    prompt_detail,
    edit_page,
    diff_page,
    ab_test_page,
    logs_page,
)

if TYPE_CHECKING:
    from ..manager import PromptManager


def _base(request: Request) -> str:
    """
    Return the mount prefix (e.g. '/prompts') so all links and redirects
    are absolute and work regardless of nesting depth.
    """
    # request.scope["root_path"] is set by Starlette when sub-mounted
    root = request.scope.get("root_path", "").rstrip("/")
    return root


def build_router(manager: "PromptManager") -> APIRouter:
    router = APIRouter()

    def _check_password(password: Optional[str]) -> bool:
        if not manager.protected_mode:
            return True
        return password == manager.admin_password

    # ------------------------------------------------------------------
    # Root redirect
    # ------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        return RedirectResponse(f"{_base(request)}/list")

    # ------------------------------------------------------------------
    # Prompt list
    # ------------------------------------------------------------------

    @router.get("/list", response_class=HTMLResponse)
    async def list_prompts(request: Request):
        prompts = await fetch_all_prompts(manager.db_path)
        return HTMLResponse(prompt_list(prompts, manager.protected_mode, _base(request)))

    # ------------------------------------------------------------------
    # Prompt detail
    # ------------------------------------------------------------------

    @router.get("/detail/{name}", response_class=HTMLResponse)
    async def detail(name: str, request: Request):
        versions = await fetch_prompt_versions(manager.db_path, name)
        return HTMLResponse(prompt_detail(name, versions, manager.protected_mode, _base(request)))

    # ------------------------------------------------------------------
    # Edit / create
    # ------------------------------------------------------------------

    @router.get("/edit/__new__", response_class=HTMLResponse)
    async def new_prompt_form(request: Request):
        return HTMLResponse(
            edit_page("__new__", "", manager.protected_mode, manager.has_llm,
                      _base(request), is_new=True)
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
        base = _base(request)
        tag = tag or None
        do_activate = set_active == "1"

        if manager.protected_mode and (do_activate or tag == "prod"):
            if not _check_password(password):
                return HTMLResponse(
                    edit_page("__new__", content, manager.protected_mode, manager.has_llm,
                              base, error="Incorrect password.", is_new=True)
                )

        await create_version(manager.db_path, prompt_name, content, edited_by, tag, do_activate)
        manager.cache.invalidate(prompt_name)
        return RedirectResponse(f"{base}/detail/{prompt_name}", status_code=303)

    @router.get("/edit/{name}", response_class=HTMLResponse)
    async def edit_form(name: str, request: Request):
        versions = await fetch_prompt_versions(manager.db_path, name)
        active = next((v for v in versions if v["is_active"]), None)
        current = active["content"] if active else ""
        return HTMLResponse(
            edit_page(name, current, manager.protected_mode, manager.has_llm, _base(request))
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
        base = _base(request)
        tag = tag or None
        do_activate = set_active == "1"

        if manager.protected_mode and (do_activate or tag == "prod"):
            if not _check_password(password):
                return HTMLResponse(
                    edit_page(name, content, manager.protected_mode, manager.has_llm,
                              base, error="Incorrect password.")
                )

        await create_version(manager.db_path, name, content, edited_by, tag, do_activate)
        manager.cache.invalidate(name)
        return RedirectResponse(f"{base}/detail/{name}", status_code=303)

    # ------------------------------------------------------------------
    # Activate version
    # ------------------------------------------------------------------

    @router.post("/activate/{name}/{version_id}", response_class=HTMLResponse)
    async def activate_version(
        name: str,
        version_id: int,
        request: Request,
        password: Optional[str] = Form(None),
    ):
        base = _base(request)
        if manager.protected_mode and not _check_password(password):
            versions = await fetch_prompt_versions(manager.db_path, name)
            return HTMLResponse(
                prompt_detail(name, versions, manager.protected_mode, base)
                .replace("</nav>", '</nav><div class="container"><div class="error">Incorrect password.</div></div>')
            )
        await set_active_version(manager.db_path, version_id)
        manager.cache.invalidate(name)
        return RedirectResponse(f"{base}/detail/{name}", status_code=303)

    # ------------------------------------------------------------------
    # Soft delete
    # ------------------------------------------------------------------

    @router.post("/delete/{name}")
    async def delete_prompt(name: str, request: Request, password: Optional[str] = Form(None)):
        base = _base(request)
        if manager.protected_mode and not _check_password(password):
            return JSONResponse({"error": "Incorrect password."}, status_code=403)
        await soft_delete_prompt(manager.db_path, name)
        manager.cache.invalidate(name)
        return RedirectResponse(f"{base}/list", status_code=303)

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    @router.get("/diff/{name}", response_class=HTMLResponse)
    async def diff_view(name: str, request: Request, v1: Optional[int] = None, v2: Optional[int] = None):
        versions = await fetch_prompt_versions(manager.db_path, name)
        ver1 = await fetch_version_by_id(manager.db_path, v1) if v1 else None
        ver2 = await fetch_version_by_id(manager.db_path, v2) if v2 else None
        return HTMLResponse(diff_page(name, versions, ver1, ver2, manager.protected_mode, _base(request)))

    # ------------------------------------------------------------------
    # A/B test
    # ------------------------------------------------------------------

    @router.get("/test/{name}", response_class=HTMLResponse)
    async def ab_test(name: str, request: Request):
        versions = await fetch_prompt_versions(manager.db_path, name)
        return HTMLResponse(ab_test_page(name, versions, manager.protected_mode, manager.has_llm, _base(request)))

    # ------------------------------------------------------------------
    # Export as JSON
    # ------------------------------------------------------------------

    @router.get("/export")
    async def export_json():
        data = await export_prompts(manager.db_path)
        content = json.dumps(data, indent=2, ensure_ascii=False)
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=prompts.json"},
        )

    # ------------------------------------------------------------------
    # Import from JSON
    # ------------------------------------------------------------------

    @router.get("/import", response_class=HTMLResponse)
    async def import_form(request: Request):
        base = _base(request)
        body = f"""
<h1>Import Prompts</h1>
<div class="card">
  <p class="text-muted" style="margin-bottom:16px;">
    Upload a JSON file in the format <code>{{"prompt_name": "prompt content", ...}}</code>.
    Each prompt will be imported as a new version and set active.
    Existing prompts are not overwritten - a new version is created instead.
  </p>
  <form method="post" action="{base}/import" enctype="multipart/form-data">
    <div class="form-group">
      <label for="imported_by">Imported By</label>
      <input type="text" id="imported_by" name="imported_by" placeholder="your name or team">
    </div>
    <div class="form-group">
      <label for="file">JSON File</label>
      <input type="file" id="file" name="file" accept=".json"
             style="background:#0f1117;border:1px solid #2d3148;border-radius:6px;color:#e2e8f0;padding:8px 12px;width:100%;">
    </div>
    <div class="flex">
      <button type="submit" class="btn btn-primary">Import</button>
      <a href="{base}/list" class="btn btn-ghost">Cancel</a>
    </div>
  </form>
</div>"""
        from .templates import _layout
        return HTMLResponse(_layout("Import Prompts", body, manager.protected_mode, base))

    @router.post("/import", response_class=HTMLResponse)
    async def import_json(
        request: Request,
        file: UploadFile = File(...),
        imported_by: str = Form("import"),
    ):
        base = _base(request)
        raw = await file.read()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("JSON must be an object at the top level.")
            # Validate all values are strings
            bad = [k for k, v in data.items() if not isinstance(v, str)]
            if bad:
                raise ValueError(f"All values must be strings. Bad keys: {', '.join(bad)}")
        except (json.JSONDecodeError, ValueError) as exc:
            from .templates import _layout
            body = f"""<h1>Import Prompts</h1>
<div class="error">Invalid JSON: {str(exc)}</div>
<a href="{base}/import" class="btn btn-ghost">← Try Again</a>"""
            return HTMLResponse(_layout("Import Prompts", body, manager.protected_mode, base), status_code=400)

        result = await import_prompts(manager.db_path, data, imported_by=imported_by)

        # Invalidate cache for all imported prompts
        for name in data:
            manager.cache.invalidate(name)

        from .templates import _layout
        created_list = "".join(f"<li>{n}</li>" for n in result["created"]) or "<li>none</li>"
        updated_list = "".join(f"<li>{n}</li>" for n in result["updated"]) or "<li>none</li>"
        body = f"""
<h1>Import Complete</h1>
<div class="card">
  <h2>Created ({len(result['created'])})</h2>
  <ul style="padding-left:20px;color:#9ae6b4;">{created_list}</ul>
  <h2 style="margin-top:16px;">Updated ({len(result['updated'])})</h2>
  <ul style="padding-left:20px;color:#fbd38d;">{updated_list}</ul>
</div>
<div class="flex mt-16">
  <a href="{base}/list" class="btn btn-primary">View All Prompts</a>
  <a href="{base}/import" class="btn btn-ghost">Import More</a>
</div>"""
        return HTMLResponse(_layout("Import Complete", body, manager.protected_mode, base))

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    @router.get("/logs", response_class=HTMLResponse)
    async def view_logs(request: Request, prompt: Optional[str] = None):
        logs = await fetch_logs(manager.db_path, prompt_name=prompt, limit=100)
        return HTMLResponse(logs_page(logs, manager.protected_mode, _base(request), prompt_filter=prompt or ""))

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
        if not version_id or not input_text:
            return JSONResponse({"detail": "version_id and input are required."}, status_code=400)
        version = await fetch_version_by_id(manager.db_path, version_id)
        if not version:
            return JSONResponse({"detail": "Version not found."}, status_code=404)
        try:
            output = await manager.llm.run(version["content"], input_text)
            return JSONResponse({"output": output})
        except Exception as exc:
            return JSONResponse({"detail": f"LLM error: {exc}"}, status_code=502)

    return router
