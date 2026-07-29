"""
Server-rendered HTML templates - pure Python strings, no template engine needed.
All link/form helpers take a `base` prefix (e.g. '/prompts') so paths are always absolute.
"""

import difflib
from typing import Optional, List, Dict, Any

APP_NAME = "Prompt Manager"


def _layout(title: str, body: str, protected: bool = False, base: str = "", user: Optional[dict] = None) -> str:
    badge = '<span class="nav-badge">PROTECTED</span>' if protected else ""
    
    user_nav = ""
    if user:
        username = user.get("username", "user")
        role = user.get("role", "editor")
        admin_link = f'<a href="{base}/users" class="nav-link" style="color:var(--accent);font-weight:700;">Users</a>' if role == 'admin' else ''
        user_nav = f"""
        <div style="display:flex;align-items:center;gap:8px;margin-left:12px;padding-left:12px;border-left:1px solid var(--line);">
          <span style="font-size:0.82rem;color:#e2e8f0;font-weight:600;">👤 {username}</span>
          <span class="badge badge-active" style="font-size:0.68rem;">{role.upper()}</span>
          {admin_link}
          <a href="{base}/logout" class="nav-link" style="font-size:0.82rem;color:#feb2b2;">Logout</a>
        </div>"""

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
    --bg: #0f1117;
    --surface: #191d27;
    --surface-strong: #202636;
    --line: #30364a;
    --line-soft: #242a3a;
    --text: #edf2f7;
    --muted: #98a2b3;
    --muted-strong: #c4ccd8;
    --accent: #8fb3ff;
    --accent-strong: #a7f3d0;
    --danger-bg: #742a2a;
    --danger-text: #feb2b2;
  }}
  body {{
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: linear-gradient(180deg, #151925 0%, var(--bg) 34%, #11131a 100%);
    color: var(--text);
    min-height: 100vh;
  }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .nav {{
    background: rgba(18, 22, 32, 0.92);
    border-bottom: 1px solid var(--line-soft);
    backdrop-filter: blur(14px);
    position: sticky;
    top: 0;
    z-index: 10;
  }}
  .nav-inner {{
    max-width: 1080px;
    margin: 0 auto;
    padding: 14px 24px;
    display: flex;
    align-items: center;
    gap: 18px;
  }}
  .nav-brand {{
    font-weight: 760;
    font-size: 1.04rem;
    color: #fff;
    white-space: nowrap;
  }}
  .nav-links {{ margin-left: auto; display: flex; align-items: center; gap: 6px; }}
  .nav-link {{
    color: var(--muted-strong);
    font-size: 0.9rem;
    font-weight: 600;
    padding: 7px 11px;
    border-radius: 7px;
  }}
  .nav-link:hover {{ background: var(--surface-strong); color: #fff; text-decoration: none; }}
  .nav-badge {{ font-size: 0.72rem; background: #4c2f12; color: #fbd38d; padding: 3px 8px; border-radius: 99px; font-weight: 700; }}
  .container {{ max-width: 1080px; margin: 0 auto; padding: 34px 24px; }}
  h1 {{ font-size: 1.55rem; font-weight: 750; margin-bottom: 24px; }}
  h2 {{ font-size: 1.02rem; font-weight: 700; margin-bottom: 12px; color: var(--muted-strong); }}
  .card {{ background: rgba(25, 29, 39, 0.96); border: 1px solid var(--line-soft); border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 18px 45px rgba(0, 0, 0, 0.18); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; padding: 11px 14px; color: var(--muted); border-bottom: 1px solid var(--line); font-weight: 650; }}
  td {{ padding: 11px 14px; border-bottom: 1px solid #202635; vertical-align: middle; }}
  tbody tr {{ transition: background .14s ease; }}
  tbody tr:hover {{ background: rgba(255, 255, 255, 0.025); }}
  tr:last-child td {{ border-bottom: none; }}
  .badge {{ display: inline-block; font-size: 0.7rem; padding: 2px 8px; border-radius: 99px; font-weight: 600; }}
  .badge-prod {{ background: #276749; color: #9ae6b4; }}
  .badge-staging {{ background: #744210; color: #fbd38d; }}
  .badge-experiment {{ background: #44337a; color: #d6bcfa; }}
  .badge-active {{ background: #2a4365; color: #90cdf4; }}
  .btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 34px; padding: 7px 15px; border-radius: 7px; font-size: 0.85rem; font-weight: 700; cursor: pointer; border: 1px solid transparent; transition: background .15s, border-color .15s, transform .15s; }}
  .btn:hover {{ transform: translateY(-1px); text-decoration: none; }}
  .btn-primary {{ background: var(--accent); color: #0f1117; }}
  .btn-primary:hover {{ background: #a3c2ff; }}
  .btn-ghost {{ background: transparent; color: var(--muted-strong); border-color: var(--line); }}
  .btn-ghost:hover {{ background: var(--surface-strong); color: #fff; border-color: var(--line-soft); }}
  .btn-danger {{ background: var(--danger-bg); color: var(--danger-text); border-color: #9b2c2c; }}
  .btn-danger:hover {{ background: #9b2c2c; }}
  .btn-sm {{ min-height: 28px; padding: 4px 10px; font-size: 0.78rem; }}
  .flex {{ display: flex; align-items: center; gap: 10px; }}
  .flex-between {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
  .mt-16 {{ margin-top: 16px; }}
  .error {{ background: var(--danger-bg); color: var(--danger-text); border: 1px solid #9b2c2c; padding: 10px 14px; border-radius: 7px; margin-bottom: 16px; font-size: 0.88rem; }}
  .success {{ background: #276749; color: #9ae6b4; border: 1px solid #2f855a; padding: 10px 14px; border-radius: 7px; margin-bottom: 16px; font-size: 0.88rem; }}
  .text-muted {{ color: var(--muted); }}
  textarea, input[type="text"], input[type="password"], input[type="email"], select {{ background: #11141f; border: 1px solid var(--line); color: var(--text); border-radius: 7px; padding: 9px 12px; font-size: 0.9rem; width: 100%; }}
  textarea:focus, input:focus, select:focus {{ outline: none; border-color: var(--accent); }}
  label {{ display: block; font-size: 0.82rem; font-weight: 650; color: var(--muted-strong); margin-bottom: 6px; }}
  .form-group {{ margin-bottom: 16px; }}
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
        name = p["name"]
        rows += f"""
        <tr>
          <td><a href="{base}/detail/{name}" style="font-weight:600;">{name}</a></td>
          <td>{v_str}</td>
          <td>{editor}</td>
          <td class="text-muted" style="font-size:0.82rem;">{updated}</td>
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


def prompt_detail(name: str, versions: list, protected: bool = False, base: str = "", user: Optional[dict] = None) -> str:
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
                  <input type="password" name="password" placeholder="Password" style="width:110px;padding:4px 8px;font-size:0.78rem;">
                  <button type="submit" class="btn btn-ghost btn-sm">Make Active</button>
                </form>"""
            else:
                act_btn = f"""
                <form method="post" action="{base}/activate/{name}/{v_id}">
                  <button type="submit" class="btn btn-ghost btn-sm">Make Active</button>
                </form>"""

        v_rows += f"""
        <tr>
          <td><strong>v{v_num}</strong> {act_badge} {tag_badge}</td>
          <td>{v.get("created_by") or "-"}</td>
          <td class="text-muted" style="font-size:0.82rem;">{v.get("created_at") or "-"}</td>
          <td><pre style="max-height:60px;overflow:hidden;font-size:0.8rem;color:var(--muted-strong);">{v.get("content", "")[:120]}</pre></td>
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
) -> str:
    err_div = f'<div class="error">{error}</div>' if error else ""

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

    llm_btn = f"""
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
<h1>{title_text}</h1>
{err_div}
<form method="post" action="{action}">
  {name_input}
  <div class="form-group">
    <div class="flex-between" style="margin-bottom:6px;">
      <label for="content" style="margin:0;">Prompt Content</label>
      {llm_btn}
    </div>
    <textarea id="content" name="content" rows="12">{content}</textarea>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:16px;">
    <div class="form-group">
      <label for="edited_by">Your Name / Team</label>
      <input type="text" id="edited_by" name="edited_by" value="{user.get('username', '') if user else ''}" placeholder="e.g. alex">
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


def diff_page(name: str, versions: list, v1: Optional[dict], v2: Optional[dict], protected: bool, base: str, user: Optional[dict] = None) -> str:
    diff_html = ""
    if v1 and v2:
        lines1 = (v1.get("content") or "").splitlines()
        lines2 = (v2.get("content") or "").splitlines()
        diff = list(difflib.unified_diff(lines1, lines2, lterm=""))
        diff_text = "\n".join(diff) or "No differences found."
        diff_html = f'<div class="card"><pre style="font-family:monospace;font-size:0.88rem;color:#e2e8f0;">{diff_text}</pre></div>'

    opts = "".join(f'<option value="{v["id"]}">v{v["version_number"]} ({v.get("created_at") or ""})</option>' for v in versions)

    body = f"""
<h1>Diff Versions: <span style="color:var(--accent);">{name}</span></h1>
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


def ab_test_page(name: str, versions: list, protected: bool, has_llm: bool, base: str, user: Optional[dict] = None) -> str:
    v_opts = "".join(f'<option value="{v["id"]}">v{v["version_number"]} ({v.get("tag") or "no tag"})</option>' for v in versions)

    llm_notice = "" if has_llm else '<div class="error" style="margin-bottom:16px;">LLM provider is not configured. Configure <code>llm_url</code> in PromptManager to enable live A/B test runs.</div>'

    body = f"""
<h1>A/B Test Prompt: <span style="color:var(--accent);">{name}</span></h1>
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


def logs_page(logs: list, protected: bool, base: str, prompt_filter: str = "", user: Optional[dict] = None) -> str:
    log_rows = ""
    for l in logs:
        log_rows += f"""
        <tr>
          <td><strong>{l.get("prompt_name")}</strong> (v{l.get("version_number", "-")})</td>
          <td><pre style="max-height:50px;overflow:hidden;font-size:0.78rem;">{l.get("input", "")[:100]}</pre></td>
          <td><pre style="max-height:50px;overflow:hidden;font-size:0.78rem;">{l.get("output", "")[:100]}</pre></td>
          <td class="text-muted" style="font-size:0.8rem;">{l.get("timestamp")}</td>
        </tr>"""

    if not log_rows:
        log_rows = '<tr><td colspan="4" style="text-align:center;padding:24px;color:var(--muted);">No logs recorded.</td></tr>'

    body = f"""
<h1>Usage Logs</h1>
<div class="card" style="padding:0;overflow:hidden;">
  <table>
    <thead>
      <tr>
        <th>Prompt & Version</th>
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
    err_div = f'<div class="error">{error}</div>' if error else ""
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


def users_page(users: list, current_user: Optional[dict] = None, base: str = "", error: Optional[str] = None, success: Optional[str] = None) -> str:
    err_div = f'<div class="error">{error}</div>' if error else ""
    succ_div = f'<div class="success">{success}</div>' if success else ""

    u_rows = ""
    for u in users:
        u_rows += f"""
        <tr>
          <td><strong>{u.get("username")}</strong></td>
          <td>{u.get("email") or "-"}</td>
          <td><span class="badge badge-active">{u.get("role", "editor").upper()}</span></td>
          <td class="text-muted" style="font-size:0.8rem;">{u.get("created_at") or "-"}</td>
        </tr>"""

    body = f"""
<h1>User Management</h1>
{err_div}
{succ_div}

<div class="card" style="margin-bottom:24px;">
  <h2>Add New User</h2>
  <form method="post" action="{base}/users" class="mt-16">
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
