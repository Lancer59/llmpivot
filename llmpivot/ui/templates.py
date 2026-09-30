"""
Server-rendered HTML templates - pure Python strings, no template engine needed.
All link/form helpers take a `base` prefix (e.g. '/prompts') so paths are always absolute.

CSRF: every state-mutating form now accepts an optional `csrf_token` parameter.
When present, it is embedded as a hidden input field. The routes layer generates
and validates the token; templates are just responsible for emitting it.
"""

import difflib
from typing import Optional, List, Dict, Any

try:
    from .helpers import escape as _escape, render_user_badge
except ImportError:  # pragma: no cover - supports direct module loading in tests
    import importlib.util
    from pathlib import Path

    helper_path = Path(__file__).resolve().with_name("helpers.py")
    spec = importlib.util.spec_from_file_location("llmpivot.ui.helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _escape = module.escape
    render_user_badge = module.render_user_badge

APP_NAME = "Prompt Manager"


def _csrf_field(csrf_token: Optional[str]) -> str:
    """Render a hidden CSRF input, or empty string when no token is provided."""
    if not csrf_token:
        return ""
    return f'<input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">'


def _layout(title: str, body: str, protected: bool = False, base: str = "", user: Optional[dict] = None) -> str:
    badge = '<span class="nav-badge">PROTECTED</span>' if protected else ""
    user_nav = render_user_badge(user, base) if user else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - {APP_NAME}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    color-scheme: dark;
    --bg: #0b1020;
    --surface: #111a2b;
    --surface-raised: #162237;
    --surface-soft: #0e1728;
    --line: #29374d;
    --line-soft: rgba(150, 175, 207, .12);
    --text: #edf4fc;
    --muted: #8fa1b8;
    --muted-strong: #becbdd;
    --accent: #91c8ff;
    --accent-strong: #c1e4ff;
    --accent-ink: #091522;
    --success: #82d6b2;
    --danger-bg: #351d2a;
    --danger-text: #ffb4c2;
    --warning: #f2cd8f;
    --radius: 12px;
    --shadow: 0 24px 70px rgba(0, 0, 0, .24);
  }}
  html {{ background: var(--bg); scroll-behavior: smooth; }}
  body {{
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: radial-gradient(ellipse at 50% -24%, rgba(61, 105, 158, .22), transparent 54%), var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}
  a {{ color: var(--accent); text-decoration: none; transition: color .16s ease, background .16s ease, border-color .16s ease; }}
  a:hover {{ color: var(--accent-strong); }}
  ::selection {{ background: rgba(120, 190, 255, .28); color: #fff; }}
  .nav {{
    background: rgba(11, 16, 32, .84);
    border-bottom: 1px solid rgba(150, 175, 207, .13);
    backdrop-filter: blur(18px) saturate(140%);
    position: sticky;
    top: 0;
    z-index: 10;
  }}
  .nav-inner {{
    max-width: 1200px;
    min-height: 68px;
    margin: 0 auto;
    padding: 10px 30px;
    display: flex;
    align-items: center;
    gap: 14px;
  }}
  .nav-brand {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-weight: 760;
    letter-spacing: -.035em;
    font-size: 1.02rem;
    color: var(--text);
    white-space: nowrap;
  }}
  .nav-brand::before {{ content: ""; width: 10px; height: 10px; border-radius: 3px; background: linear-gradient(145deg, #b5e3ff, #6caeff); box-shadow: 0 0 18px rgba(109, 176, 255, .5); transform: rotate(-8deg); }}
  .nav-links {{ margin-left: auto; display: flex; align-items: center; gap: 4px; }}
  .nav-link {{
    color: #9fb0c6;
    font-size: .84rem;
    font-weight: 580;
    padding: 8px 11px;
    border-radius: 8px;
  }}
  .nav-link:hover {{ background: rgba(142, 190, 246, .09); color: var(--text); text-decoration: none; }}
  .nav-badge {{ font-size: .65rem; letter-spacing: .08em; background: rgba(239, 185, 105, .12); border: 1px solid rgba(239, 185, 105, .22); color: var(--warning); padding: 4px 8px; border-radius: 99px; font-weight: 750; }}
  .user-nav {{ display: flex; align-items: center; gap: 8px; margin-left: 9px; padding-left: 12px; border-left: 1px solid var(--line); }}
  .user-avatar {{ display: inline-grid; place-items: center; width: 27px; height: 27px; border-radius: 8px; background: rgba(145, 200, 255, .12); color: var(--accent-strong); font-size: .73rem; font-weight: 750; }}
  .user-name {{ color: #dce7f4; font-size: .8rem; font-weight: 620; }}
  .user-nav .nav-link {{ padding: 7px 8px; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 44px 30px 72px; }}
  h1 {{ font-size: clamp(1.55rem, 2.5vw, 2rem); line-height: 1.2; letter-spacing: -.045em; font-weight: 720; margin-bottom: 10px; }}
  h2 {{ font-size: 1rem; line-height: 1.35; font-weight: 680; margin-bottom: 12px; color: #dae5f2; letter-spacing: -.015em; }}
  p {{ color: var(--muted-strong); }}
  .card {{ background: linear-gradient(145deg, rgba(19, 30, 48, .96), rgba(15, 25, 41, .96)); border: 1px solid var(--line-soft); border-radius: var(--radius); padding: 22px; margin-bottom: 18px; box-shadow: var(--shadow); }}
  table {{ width: 100%; min-width: 680px; border-collapse: collapse; font-size: .86rem; }}
  th {{ text-align: left; padding: 13px 16px; color: #8fa2bb; background: rgba(7, 13, 25, .38); border-bottom: 1px solid var(--line); font-size: .7rem; letter-spacing: .09em; text-transform: uppercase; font-weight: 720; white-space: nowrap; }}
  td {{ padding: 14px 16px; border-bottom: 1px solid rgba(150, 175, 207, .085); vertical-align: middle; color: #d2deec; }}
  tbody tr {{ transition: background .14s ease; }}
  tbody tr:hover {{ background: rgba(145, 200, 255, .045); }}
  tr:last-child td {{ border-bottom: none; }}
  .badge {{ display: inline-flex; align-items: center; font-size: .66rem; letter-spacing: .045em; text-transform: uppercase; padding: 4px 9px; border-radius: 99px; font-weight: 720; border: 1px solid transparent; white-space: nowrap; }}
  .badge-prod {{ background: rgba(78, 190, 137, .12); border-color: rgba(78, 190, 137, .22); color: #91e0b8; }}
  .badge-staging {{ background: rgba(229, 176, 82, .12); border-color: rgba(229, 176, 82, .22); color: #f2cb82; }}
  .badge-experiment {{ background: rgba(174, 144, 255, .12); border-color: rgba(174, 144, 255, .22); color: #c9b4ff; }}
  .badge-active {{ background: rgba(106, 177, 247, .12); border-color: rgba(106, 177, 247, .2); color: #a6d5ff; }}
  .btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 38px; padding: 8px 14px; border-radius: 9px; font: inherit; font-size: .83rem; line-height: 1.2; font-weight: 660; cursor: pointer; border: 1px solid transparent; transition: background .15s, border-color .15s, transform .15s, box-shadow .15s; white-space: nowrap; }}
  .btn:hover {{ transform: translateY(-1px); text-decoration: none; }}
  .btn-primary {{ background: linear-gradient(180deg, #a9d9ff, #85c2f6); color: var(--accent-ink); box-shadow: 0 5px 18px rgba(90, 163, 229, .14); }}
  .btn-primary:hover {{ background: #c0e4ff; color: #081421; box-shadow: 0 7px 22px rgba(90, 163, 229, .22); }}
  .btn-ghost {{ background: rgba(161, 187, 220, .035); color: #c0cede; border-color: rgba(150, 175, 207, .2); }}
  .btn-ghost:hover {{ background: rgba(145, 200, 255, .09); color: #f2f8ff; border-color: rgba(145, 200, 255, .32); }}
  .btn-danger {{ background: var(--danger-bg); color: var(--danger-text); border-color: rgba(255, 124, 151, .24); }}
  .btn-danger:hover {{ background: #512536; }}
  .btn:disabled {{ opacity: .48; cursor: not-allowed; transform: none; box-shadow: none; }}
  .btn-sm {{ min-height: 31px; padding: 6px 10px; font-size: .76rem; }}
  .flex {{ display: flex; align-items: center; gap: 10px; }}
  .flex-between {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
  .mt-16 {{ margin-top: 16px; }}
  .error {{ background: rgba(125, 43, 66, .2); color: var(--danger-text); border: 1px solid rgba(255, 124, 151, .25); padding: 12px 15px; border-radius: 9px; margin-bottom: 18px; font-size: .86rem; }}
  .success {{ background: rgba(39, 121, 85, .16); color: #9be4bf; border: 1px solid rgba(93, 202, 145, .23); padding: 12px 15px; border-radius: 9px; margin-bottom: 18px; font-size: .86rem; }}
  .text-muted {{ color: var(--muted); }}
  textarea, input[type="text"], input[type="password"], input[type="email"], select {{ background: #0b1423; border: 1px solid rgba(150, 175, 207, .22); color: var(--text); border-radius: 9px; padding: 10px 12px; font: inherit; font-size: .88rem; width: 100%; transition: border-color .15s, box-shadow .15s, background .15s; }}
  textarea {{ min-height: 120px; resize: vertical; line-height: 1.6; }}
  textarea:focus, input:focus, select:focus {{ outline: none; border-color: #78baf2; background: #0d192a; box-shadow: 0 0 0 3px rgba(120, 186, 242, .13); }}
  textarea::placeholder, input::placeholder {{ color: #61748d; }}
  label {{ display: block; font-size: .77rem; font-weight: 660; color: #c1cede; margin-bottom: 7px; }}
  .form-group {{ margin-bottom: 18px; }}
  pre, code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
  :focus-visible {{ outline: 2px solid #8dcaff; outline-offset: 3px; }}
  @media (max-width: 760px) {{
    .nav-inner {{ padding: 10px 18px; align-items: flex-start; flex-wrap: wrap; }}
    .nav-links {{ width: 100%; margin: 0; overflow-x: auto; padding-bottom: 2px; }}
    .nav-link {{ flex: 0 0 auto; }}
    .container {{ padding: 30px 18px 52px; }}
    .flex-between {{ align-items: flex-start; flex-direction: column; }}
    .flex-between > .flex {{ flex-wrap: wrap; }}
    .card {{ padding: 17px; }}
    [style*="grid-template-columns"] {{ grid-template-columns: 1fr !important; }}
    [style*="overflow:hidden"] {{ overflow-x: auto !important; }}
  }}
  @media (prefers-reduced-motion: reduce) {{ *, *::before, *::after {{ scroll-behavior: auto !important; transition-duration: .01ms !important; }} }}
</style>
</head>
<body>
<nav class="nav">
  <div class="nav-inner">
    <a href="{base}/list" class="nav-brand">{APP_NAME}</a>
    {badge}
    <div class="nav-links">
      <a href="{base}/list" class="nav-link">Prompts</a>
      <a href="{base}/edit/__new__" class="nav-link">+ New Prompt</a>
      <a href="{base}/import" class="nav-link">Import</a>
      <a href="{base}/export" class="nav-link">Export</a>
      <a href="{base}/logs" class="nav-link">Logs</a>
      {user_nav}
    </div>
  </div>
</nav>
<main class="container">
{body}
</main>
</body>
</html>"""


def prompt_list(prompts: list, protected: bool = False, base: str = "", user: Optional[dict] = None) -> str:
    rows = ""
    for p in prompts:
        v = p.get("active_version")
        v_str = f"v{v}" if v else "<span class='text-muted'>none</span>"
        editor = p.get("last_edited_by") or "-"
        updated = p.get("last_updated") or "-"
        name = _escape(p["name"])
        rows += f"""
        <tr>
          <td><a href="{base}/detail/{name}" style="font-weight:600;">{name}</a></td>
          <td>{v_str}</td>
          <td>{_escape(editor)}</td>
          <td class="text-muted" style="font-size:0.82rem;">{_escape(updated)}</td>
          <td style="text-align:right;">
            <a href="{base}/edit/{name}" class="btn btn-ghost btn-sm">Edit</a>
          </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="5" style="text-align:center;padding:32px;color:var(--muted);">No prompts yet. <a href="' + base + '/edit/__new__">Create one</a>.</td></tr>'

    body = f"""
<div class="flex-between" style="margin-bottom:24px;">
  <div>
    <h1>Prompts</h1>
    <p class="text-muted" style="font-size:0.88rem;">Manage and monitor active prompts across runtime applications.</p>
  </div>
  <div class="flex">
    <a href="{base}/export" class="btn btn-ghost">Export JSON</a>
    <a href="{base}/edit/__new__" class="btn btn-primary">+ New Prompt</a>
  </div>
</div>
<div class="card" style="padding:0;overflow:hidden;">
  <table>
    <thead>
      <tr>
        <th>Prompt Name</th>
        <th>Active Version</th>
        <th>Last Edited By</th>
        <th>Last Updated</th>
        <th></th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""
    return _layout("Home", body, protected, base, user)


def prompt_detail(
    name: str,
    versions: list,
    protected: bool = False,
    base: str = "",
    user: Optional[dict] = None,
    csrf_token: Optional[str] = None,
) -> str:
    csrf = _csrf_field(csrf_token)
    v_rows = ""
    for v in versions:
        is_act = v.get("is_active")
        act_badge = '<span class="badge badge-active">ACTIVE</span>' if is_act else ""
        tag = v.get("tag")
        tag_badge = f'<span class="badge badge-{tag}">{tag}</span>' if tag else ""
        v_id = v["id"]
        v_num = v["version_number"]

        act_btn = ""
        if not is_act:
            if protected:
                act_btn = f"""
                <form method="post" action="{base}/activate/{name}/{v_id}" class="flex" style="display:inline-flex;">
                  {csrf}
                  <input type="password" name="password" placeholder="Password" style="width:110px;padding:4px 8px;font-size:0.78rem;">
                  <button type="submit" class="btn btn-ghost btn-sm">Make Active</button>
                </form>"""
            else:
                act_btn = f"""
                <form method="post" action="{base}/activate/{name}/{v_id}">
                  {csrf}
                  <button type="submit" class="btn btn-ghost btn-sm">Make Active</button>
                </form>"""

        created_by = _escape(v.get("created_by") or "-")
        created_at = _escape(v.get("created_at") or "-")
        content_preview = _escape((v.get("content", "") or "")[:120])
        v_rows += f"""
        <tr>
          <td><strong>v{v_num}</strong> {act_badge} {tag_badge}</td>
          <td>{created_by}</td>
          <td class="text-muted" style="font-size:0.82rem;">{created_at}</td>
          <td><pre style="max-height:60px;overflow:hidden;font-size:0.8rem;color:var(--muted-strong);">{content_preview}</pre></td>
          <td style="text-align:right;">{act_btn}</td>
        </tr>"""

    body = f"""
<div class="flex-between" style="margin-bottom:20px;">
  <div>
    <h1>Prompt: <span style="color:var(--accent);">{name}</span></h1>
  </div>
  <div class="flex">
    <a href="{base}/edit/{name}" class="btn btn-primary">Create New Version</a>
    <a href="{base}/diff/{name}" class="btn btn-ghost">Diff Versions</a>
    <a href="{base}/test/{name}" class="btn btn-ghost">A/B Test</a>
  </div>
</div>
<div class="card" style="padding:0;overflow:hidden;">
  <table>
    <thead>
      <tr>
        <th>Version</th>
        <th>Edited By</th>
        <th>Date</th>
        <th>Content Preview</th>
        <th></th>
      </tr>
    </thead>
    <tbody>{v_rows}</tbody>
  </table>
</div>"""
    return _layout(f"Detail: {name}", body, protected, base, user)


def edit_page(
    name: str,
    content: str,
    protected: bool = False,
    has_llm: bool = False,
    base: str = "",
    error: Optional[str] = None,
    is_new: bool = False,
    user: Optional[dict] = None,
    csrf_token: Optional[str] = None,
) -> str:
    err_div = f'<div class="error">{error}</div>' if error else ""
    csrf = _csrf_field(csrf_token)
    action = f"{base}/edit/__new__" if is_new else f"{base}/edit/{name}"
    title_text = "Create New Prompt" if is_new else f"Edit: {name}"

    name_input = f"""
    <div class="form-group">
      <label for="prompt_name">Prompt Name</label>
      <input type="text" id="prompt_name" name="prompt_name" value="" required placeholder="e.g. summary_prompt">
    </div>""" if is_new else ""

    pwd_field = f"""
    <div class="form-group">
      <label for="password">Admin Password (required for Prod tag or Make Active)</label>
      <input type="password" id="password" name="password" placeholder="Enter admin password">
    </div>""" if protected else ""

    llm_btn = """
    <button type="button" id="suggest-btn" class="btn btn-ghost btn-sm" style="color:var(--accent);">✨ Get AI Suggestion</button>
    """ if has_llm else ""

    script_tag = f"""
    <script>
    document.getElementById('suggest-btn')?.addEventListener('click', async () => {{
      const contentEl = document.getElementById('content');
      const btn = document.getElementById('suggest-btn');
      if (!contentEl.value.trim()) {{ alert('Please enter prompt content first.'); return; }}
      btn.innerText = '✨ Suggesting...';
      btn.disabled = true;
      try {{
        const res = await fetch('{base}/api/suggest', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ content: contentEl.value }})
        }});
        const data = await res.json();
        if (data.suggestion) {{ contentEl.value = data.suggestion; }}
        else {{ alert(data.detail || 'Error getting suggestion'); }}
      }} catch (err) {{ alert('Error: ' + err); }}
      finally {{ btn.innerText = '✨ Get AI Suggestion'; btn.disabled = false; }}
    }});
    </script>
    """ if has_llm else ""

    body = f"""
<h1>{_escape(title_text)}</h1>
{err_div}
<form method="post" action="{action}">
  {csrf}
  {name_input}
  <div class="form-group">
    <div class="flex-between" style="margin-bottom:6px;">
      <label for="content" style="margin:0;">Prompt Content</label>
      {llm_btn}
    </div>
    <textarea id="content" name="content" rows="12">{_escape(content)}</textarea>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:16px;">
    <div class="form-group">
      <label for="edited_by">Your Name / Team</label>
      <input type="text" id="edited_by" name="edited_by" value="{_escape(user.get('username', '') if user else '')}" placeholder="e.g. alex">
    </div>
    <div class="form-group">
      <label for="tag">Environment Tag</label>
      <select id="tag" name="tag">
        <option value="">(None)</option>
        <option value="prod">prod</option>
        <option value="staging">staging</option>
        <option value="experiment">experiment</option>
      </select>
    </div>
    <div class="form-group">
      <label for="set_active">Set as Active Version?</label>
      <select id="set_active" name="set_active">
        <option value="1">Yes - Set Active</option>
        <option value="0">No - Save as Draft</option>
      </select>
    </div>
  </div>

  {pwd_field}

  <div class="flex mt-16">
    <button type="submit" class="btn btn-primary">Save New Version</button>
    <a href="{base}/list" class="btn btn-ghost">Cancel</a>
  </div>
</form>
{script_tag}"""
    return _layout(title_text, body, protected, base, user)


def diff_page(
    name: str,
    versions: list,
    v1: Optional[dict],
    v2: Optional[dict],
    protected: bool,
    base: str,
    user: Optional[dict] = None,
) -> str:
    diff_html = ""
    if v1 and v2:
        lines1 = (v1.get("content") or "").splitlines()
        lines2 = (v2.get("content") or "").splitlines()
        diff = list(difflib.unified_diff(lines1, lines2, lterm=""))
        diff_text = "\n".join(diff) or "No differences found."
        diff_html = f'<div class="card"><pre style="font-family:monospace;font-size:0.88rem;color:#e2e8f0;">{_escape(diff_text)}</pre></div>'

    opts = "".join(
        f'<option value="{_escape(str(v["id"]))}">v{v["version_number"]} ({_escape(v.get("created_at") or "")})</option>'
        for v in versions
    )

    body = f"""
<h1>Diff Versions: <span style="color:var(--accent);">{_escape(name)}</span></h1>
<form method="get" action="{base}/diff/{name}" class="card flex" style="margin-bottom:20px;">
  <div>
    <label>Version A</label>
    <select name="v1">{opts}</select>
  </div>
  <div>
    <label>Version B</label>
    <select name="v2">{opts}</select>
  </div>
  <button type="submit" class="btn btn-primary" style="margin-top:20px;">Compare</button>
</form>
{diff_html}"""
    return _layout("Diff", body, protected, base, user)


def ab_test_page(
    name: str,
    versions: list,
    protected: bool,
    has_llm: bool,
    base: str,
    user: Optional[dict] = None,
) -> str:
    v_opts = "".join(
        f'<option value="{_escape(str(v["id"]))}">v{v["version_number"]} ({_escape(v.get("tag") or "no tag")})</option>'
        for v in versions
    )

    llm_notice = "" if has_llm else '<div class="error" style="margin-bottom:16px;">LLM provider is not configured. Configure <code>llm_url</code> in PromptManager to enable live A/B test runs.</div>'

    body = f"""
<h1>A/B Test Prompt: <span style="color:var(--accent);">{_escape(name)}</span></h1>
{llm_notice}
<div class="card" style="margin-bottom:20px;">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
    <div>
      <label for="v1-select">Version A</label>
      <select id="v1-select">{v_opts}</select>
    </div>
    <div>
      <label for="v2-select">Version B</label>
      <select id="v2-select">{v_opts}</select>
    </div>
  </div>
  <div class="form-group">
    <label for="test-input">Test Input Text</label>
    <textarea id="test-input" rows="3" placeholder="Enter test input variable or prompt context..."></textarea>
  </div>
  <button id="run-ab-btn" class="btn btn-primary" {'disabled' if not has_llm else ''}>⚡ Run A/B Comparison</button>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
  <div class="card">
    <h2>Version A Output</h2>
    <pre id="out-a" style="font-family:monospace;font-size:0.85rem;color:var(--muted-strong);min-height:120px;white-space:pre-wrap;">(Select Version A and click Run)</pre>
  </div>
  <div class="card">
    <h2>Version B Output</h2>
    <pre id="out-b" style="font-family:monospace;font-size:0.85rem;color:var(--muted-strong);min-height:120px;white-space:pre-wrap;">(Select Version B and click Run)</pre>
  </div>
</div>

<script>
document.getElementById('run-ab-btn')?.addEventListener('click', async () => {{
  const v1 = document.getElementById('v1-select').value;
  const v2 = document.getElementById('v2-select').value;
  const input = document.getElementById('test-input').value;
  const outA = document.getElementById('out-a');
  const outB = document.getElementById('out-b');

  outA.innerText = 'Running Version A...';
  outB.innerText = 'Running Version B...';

  const runOne = async (vId) => {{
    const res = await fetch('{base}/api/run', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ version_id: vId, input: input }})
    }});
    const data = await res.json();
    return data.output || data.detail || 'Error running prompt';
  }};

  try {{
    const [resA, resB] = await Promise.all([runOne(v1), runOne(v2)]);
    outA.innerText = resA;
    outB.innerText = resB;
  }} catch (err) {{
    outA.innerText = 'Error: ' + err;
    outB.innerText = 'Error: ' + err;
  }}
}});
</script>
"""
    return _layout("A/B Test", body, protected, base, user)


def logs_page(
    logs: list,
    protected: bool,
    base: str,
    prompt_filter: str = "",
    user: Optional[dict] = None,
) -> str:
    log_rows = ""
    for l in logs:
        log_rows += f"""
        <tr>
          <td><strong>{_escape(l.get("prompt_name"))}</strong> (v{_escape(str(l.get("version_number", "-")))})</td>
          <td><pre style="max-height:50px;overflow:hidden;font-size:0.78rem;">{_escape((l.get("input", "") or "")[:100])}</pre></td>
          <td><pre style="max-height:50px;overflow:hidden;font-size:0.78rem;">{_escape((l.get("output", "") or "")[:100])}</pre></td>
          <td class="text-muted" style="font-size:0.8rem;">{_escape(str(l.get("timestamp") or ""))}</td>
        </tr>"""

    if not log_rows:
        log_rows = '<tr><td colspan="4" style="text-align:center;padding:24px;color:var(--muted);">No logs recorded.</td></tr>'

    body = f"""
<h1>Usage Logs</h1>
<div class="card" style="padding:0;overflow:hidden;">
  <table>
    <thead>
      <tr>
        <th>Prompt &amp; Version</th>
        <th>Input</th>
        <th>Output</th>
        <th>Timestamp</th>
      </tr>
    </thead>
    <tbody>{log_rows}</tbody>
  </table>
</div>"""
    return _layout("Logs", body, protected, base, user)


def login_page(base: str = "", error: Optional[str] = None) -> str:
    err_div = f'<div class="error">{_escape(error)}</div>' if error else ""
    body = f"""
<div style="max-width:380px;margin:60px auto;">
  <div class="card">
    <h1 style="text-align:center;margin-bottom:12px;font-size:1.35rem;">Login to Prompt Manager</h1>
    <p class="text-muted" style="text-align:center;font-size:0.82rem;margin-bottom:20px;">Use your credentials or reach out to an Admin.</p>
    {err_div}
    <form method="post" action="{base}/login">
      <div class="form-group">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" required autofocus placeholder="Enter username">
      </div>
      <div class="form-group">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" required placeholder="Enter password">
      </div>
      <button type="submit" class="btn btn-primary" style="width:100%;margin-top:10px;">Login</button>
    </form>
  </div>
</div>"""
    return _layout("Login", body, False, base, None)


def users_page(
    users: list,
    current_user: Optional[dict] = None,
    base: str = "",
    error: Optional[str] = None,
    success: Optional[str] = None,
    csrf_token: Optional[str] = None,
) -> str:
    err_div = f'<div class="error">{_escape(error)}</div>' if error else ""
    succ_div = f'<div class="success">{_escape(success)}</div>' if success else ""
    csrf = _csrf_field(csrf_token)

    u_rows = ""
    for u in users:
        u_rows += f"""
        <tr>
          <td><strong>{_escape(u.get("username"))}</strong></td>
          <td>{_escape(u.get("email") or "-")}</td>
          <td><span class="badge badge-active">{_escape(u.get("role", "editor")).upper()}</span></td>
          <td class="text-muted" style="font-size:0.8rem;">{_escape(u.get("created_at") or "-")}</td>
        </tr>"""

    body = f"""
<h1>User Management</h1>
{err_div}
{succ_div}

<div class="card" style="margin-bottom:24px;">
  <h2>Add New User</h2>
  <form method="post" action="{base}/users" class="mt-16">
    {csrf}
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px;">
      <div>
        <label for="username">Username</label>
        <input type="text" id="username" name="username" required placeholder="username">
      </div>
      <div>
        <label for="email">Email</label>
        <input type="email" id="email" name="email" placeholder="user@domain.com">
      </div>
      <div>
        <label for="password">Password</label>
        <input type="password" id="password" name="password" required placeholder="password">
      </div>
      <div>
        <label for="role">Role</label>
        <select id="role" name="role">
          <option value="editor">Editor</option>
          <option value="admin">Admin</option>
          <option value="viewer">Viewer</option>
        </select>
      </div>
    </div>
    <button type="submit" class="btn btn-primary mt-16">Create User</button>
  </form>
</div>

<div class="card" style="padding:0;overflow:hidden;">
  <table>
    <thead>
      <tr>
        <th>Username</th>
        <th>Email</th>
        <th>Role</th>
        <th>Created At</th>
      </tr>
    </thead>
    <tbody>{u_rows}</tbody>
  </table>
</div>"""
    return _layout("User Management", body, False, base, current_user)
