"""
Server-rendered HTML templates — pure Python strings, no template engine needed.
All link/form helpers take a `base` prefix (e.g. '/prompts') so paths are always absolute.
"""

import difflib
from typing import Optional


# ---------------------------------------------------------------------------
# Shared layout
# ---------------------------------------------------------------------------

def _layout(title: str, body: str, protected: bool = False, base: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — llmpivot</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; }}
  a {{ color: #7c9ef8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .nav {{ background: #1a1d27; border-bottom: 1px solid #2d3148; padding: 12px 24px; display: flex; align-items: center; gap: 16px; }}
  .nav-brand {{ font-weight: 700; font-size: 1.1rem; color: #fff; }}
  .nav-badge {{ font-size: 0.7rem; background: #7c9ef8; color: #0f1117; padding: 2px 7px; border-radius: 99px; font-weight: 600; }}
  .container {{ max-width: 960px; margin: 0 auto; padding: 32px 24px; }}
  h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 24px; }}
  h2 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 12px; color: #a0aec0; }}
  .card {{ background: #1a1d27; border: 1px solid #2d3148; border-radius: 10px; padding: 20px; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; padding: 8px 12px; color: #718096; border-bottom: 1px solid #2d3148; font-weight: 500; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #1e2235; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  .badge {{ display: inline-block; font-size: 0.7rem; padding: 2px 8px; border-radius: 99px; font-weight: 600; }}
  .badge-prod {{ background: #276749; color: #9ae6b4; }}
  .badge-staging {{ background: #744210; color: #fbd38d; }}
  .badge-experiment {{ background: #44337a; color: #d6bcfa; }}
  .badge-active {{ background: #2a4365; color: #90cdf4; }}
  .btn {{ display: inline-block; padding: 7px 16px; border-radius: 6px; font-size: 0.85rem; font-weight: 500; cursor: pointer; border: none; transition: opacity .15s; }}
  .btn:hover {{ opacity: 0.85; }}
  .btn-primary {{ background: #7c9ef8; color: #0f1117; }}
  .btn-ghost {{ background: transparent; border: 1px solid #2d3148; color: #a0aec0; }}
  .btn-danger {{ background: #742a2a; color: #feb2b2; }}
  .btn-sm {{ padding: 4px 10px; font-size: 0.78rem; }}
  textarea {{ width: 100%; background: #0f1117; border: 1px solid #2d3148; border-radius: 6px; color: #e2e8f0; padding: 12px; font-family: 'Courier New', monospace; font-size: 0.9rem; resize: vertical; min-height: 160px; }}
  textarea:focus {{ outline: none; border-color: #7c9ef8; }}
  input[type=text], input[type=password], select {{ background: #0f1117; border: 1px solid #2d3148; border-radius: 6px; color: #e2e8f0; padding: 8px 12px; font-size: 0.9rem; width: 100%; }}
  input:focus, select:focus {{ outline: none; border-color: #7c9ef8; }}
  .form-group {{ margin-bottom: 16px; }}
  label {{ display: block; font-size: 0.85rem; color: #a0aec0; margin-bottom: 6px; }}
  .error {{ background: #742a2a; color: #feb2b2; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }}
  .success {{ background: #276749; color: #9ae6b4; padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }}
  .diff-add {{ background: #1a3a2a; color: #9ae6b4; display: block; padding: 1px 8px; }}
  .diff-remove {{ background: #3a1a1a; color: #feb2b2; display: block; padding: 1px 8px; }}
  .diff-same {{ display: block; padding: 1px 8px; color: #718096; }}
  .diff-block {{ font-family: 'Courier New', monospace; font-size: 0.85rem; border: 1px solid #2d3148; border-radius: 6px; overflow: auto; max-height: 400px; }}
  .side-by-side {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .suggest-box {{ background: #1a2744; border: 1px solid #3a5298; border-radius: 8px; padding: 16px; margin-top: 12px; display: none; }}
  .suggest-content {{ font-family: 'Courier New', monospace; font-size: 0.85rem; white-space: pre-wrap; color: #e2e8f0; margin-bottom: 12px; }}
  .spinner {{ display: inline-block; width: 14px; height: 14px; border: 2px solid #7c9ef8; border-top-color: transparent; border-radius: 50%; animation: spin .6s linear infinite; vertical-align: middle; margin-right: 6px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .mono {{ font-family: 'Courier New', monospace; font-size: 0.85rem; }}
  .text-muted {{ color: #718096; font-size: 0.85rem; }}
  .flex {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
  .mt-8 {{ margin-top: 8px; }}
  .mt-16 {{ margin-top: 16px; }}
</style>
</head>
<body>
<nav class="nav">
  <span class="nav-brand">⚡ llmpivot</span>
  <span class="nav-badge">v1</span>
  {'<span class="nav-badge" style="background:#744210;color:#fbd38d;">🔒 protected</span>' if protected else ''}
  <a href="{base}/list" style="margin-left:auto;font-size:0.9rem;">Prompts</a>
  <a href="{base}/logs" style="font-size:0.9rem;">Logs</a>
</nav>
<div class="container">
{body}
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Prompt list
# ---------------------------------------------------------------------------

def prompt_list(prompts: list[dict], protected: bool, base: str) -> str:
    rows = ""
    for p in prompts:
        rows += f"""<tr>
          <td><a href="{base}/detail/{p['name']}">{p['name']}</a></td>
          <td class="mono">v{p['active_version'] or '—'}</td>
          <td class="text-muted">{p['last_edited_by'] or '—'}</td>
          <td class="text-muted">{(p['last_updated'] or '')[:16]}</td>
          <td><a href="{base}/edit/{p['name']}" class="btn btn-ghost btn-sm">Edit</a></td>
        </tr>"""

    if not rows:
        rows = f'<tr><td colspan="5" style="color:#718096;text-align:center;padding:32px;">No prompts yet. <a href="{base}/edit/__new__">Create one</a></td></tr>'

    body = f"""
<div class="flex" style="margin-bottom:20px;">
  <h1 style="margin:0;">Prompts</h1>
  <div style="margin-left:auto;" class="flex">
    <a href="{base}/import" class="btn btn-ghost">⬆ Import JSON</a>
    <a href="{base}/export" class="btn btn-ghost">⬇ Export JSON</a>
    <a href="{base}/edit/__new__" class="btn btn-primary">+ New Prompt</a>
  </div>
</div>
<div class="card" style="padding:0;overflow:hidden;">
<table>
  <thead><tr><th>Name</th><th>Active Version</th><th>Last Edited By</th><th>Last Updated</th><th></th></tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>"""
    return _layout("Prompts", body, protected, base)


# ---------------------------------------------------------------------------
# Prompt detail
# ---------------------------------------------------------------------------

def prompt_detail(name: str, versions: list[dict], protected: bool, base: str) -> str:
    active = next((v for v in versions if v["is_active"]), None)

    active_block = ""
    if active:
        tag_html = _tag_badge(active.get("tag"))
        active_block = f"""
<div class="card">
  <h2>Active Version — v{active['version_number']} {tag_html}</h2>
  <pre class="mono" style="white-space:pre-wrap;color:#e2e8f0;">{_esc(active['content'])}</pre>
  <p class="text-muted mt-8">by {_esc(active['created_by'] or '—')} · {str(active['created_at'])[:16]}</p>
</div>"""

    version_rows = ""
    for v in versions:
        active_badge = '<span class="badge badge-active">active</span>' if v["is_active"] else ""
        tag_html = _tag_badge(v.get("tag"))
        activate_form = ""
        if not v["is_active"]:
            activate_form = f"""<form method="post" action="{base}/activate/{name}/{v['id']}" style="display:inline">
              {_pw_field(protected)}
              <button class="btn btn-ghost btn-sm" type="submit">Make Active</button>
            </form>"""
        version_rows += f"""<tr>
          <td class="mono">v{v['version_number']}</td>
          <td>{active_badge} {tag_html}</td>
          <td class="text-muted">{_esc(v['created_by'] or '—')}</td>
          <td class="text-muted">{str(v['created_at'])[:16]}</td>
          <td>
            <div class="flex">
              {activate_form}
              <a href="{base}/diff/{name}?v1={v['id']}" class="btn btn-ghost btn-sm">Diff</a>
            </div>
          </td>
        </tr>"""

    body = f"""
<div class="flex" style="margin-bottom:20px;">
  <h1 style="margin:0;">{_esc(name)}</h1>
  <div style="margin-left:auto;" class="flex">
    <a href="{base}/test/{name}" class="btn btn-ghost">A/B Test</a>
    <a href="{base}/edit/{name}" class="btn btn-primary">Edit / New Version</a>
  </div>
</div>
{active_block}
<h2>Version History</h2>
<div class="card" style="padding:0;overflow:hidden;">
<table>
  <thead><tr><th>Version</th><th>Tags</th><th>Created By</th><th>Date</th><th></th></tr></thead>
  <tbody>{version_rows}</tbody>
</table>
</div>
<div class="mt-16"><a href="{base}/list" class="btn btn-ghost">← All Prompts</a></div>"""
    return _layout(name, body, protected, base)


# ---------------------------------------------------------------------------
# Edit / create
# ---------------------------------------------------------------------------

def edit_page(
    name: str,
    current_content: str,
    protected: bool,
    has_llm: bool,
    base: str,
    error: str = "",
    is_new: bool = False,
) -> str:
    title = "New Prompt" if is_new else f"Edit — {name}"
    form_action = f"{base}/edit/__new__" if is_new else f"{base}/edit/{name}"
    back_href = f"{base}/list" if is_new else f"{base}/detail/{name}"

    name_field = ""
    if is_new:
        name_field = """<div class="form-group">
          <label for="prompt_name">Prompt Name</label>
          <input type="text" id="prompt_name" name="prompt_name" placeholder="e.g. summary" required>
        </div>"""

    suggest_btn = ""
    suggest_section = ""
    if has_llm:
        suggest_btn = """<button type="button" class="btn btn-ghost btn-sm mt-8" onclick="getSuggestion()">
          ✨ Get AI Suggestion
        </button>"""
        suggest_section = f"""
<div class="suggest-box" id="suggestBox">
  <h2 style="margin-bottom:8px;">AI Suggestion</h2>
  <div class="suggest-content" id="suggestContent"></div>
  <div class="flex">
    <button type="button" class="btn btn-primary btn-sm" onclick="acceptSuggestion()">Accept</button>
    <button type="button" class="btn btn-ghost btn-sm" onclick="dismissSuggestion()">Dismiss</button>
  </div>
</div>
<script>
async function getSuggestion() {{
  const content = document.getElementById('content').value.trim();
  if (!content) {{ alert('Write some prompt content first.'); return; }}
  const btn = event.target;
  btn.innerHTML = '<span class="spinner"></span>Thinking...';
  btn.disabled = true;
  try {{
    const res = await fetch('{base}/api/suggest', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{content}})
    }});
    const data = await res.json();
    if (!res.ok) {{ alert(data.detail || 'LLM error'); return; }}
    document.getElementById('suggestContent').textContent = data.suggestion;
    document.getElementById('suggestBox').style.display = 'block';
  }} catch(e) {{
    alert('Request failed: ' + e.message);
  }} finally {{
    btn.innerHTML = '✨ Get AI Suggestion';
    btn.disabled = false;
  }}
}}
function acceptSuggestion() {{
  document.getElementById('content').value = document.getElementById('suggestContent').textContent;
  document.getElementById('suggestBox').style.display = 'none';
}}
function dismissSuggestion() {{
  document.getElementById('suggestBox').style.display = 'none';
}}
</script>"""

    error_html = f'<div class="error">{_esc(error)}</div>' if error else ""

    body = f"""
<h1>{title}</h1>
{error_html}
<div class="card">
<form method="post" action="{form_action}">
  {name_field}
  <div class="form-group">
    <label for="content">Prompt Content</label>
    <textarea id="content" name="content" rows="10" placeholder="Enter your prompt...">{_esc(current_content)}</textarea>
    {suggest_btn}
    {suggest_section}
  </div>
  <div class="form-group">
    <label for="edited_by">Edited By</label>
    <input type="text" id="edited_by" name="edited_by" placeholder="your name or team">
  </div>
  <div class="form-group">
    <label for="tag">Tag (optional)</label>
    <select id="tag" name="tag">
      <option value="">— none —</option>
      <option value="prod">prod</option>
      <option value="staging">staging</option>
      <option value="experiment">experiment</option>
    </select>
  </div>
  {'<div class="form-group"><label for="password">Admin Password</label><input type="password" id="password" name="password" placeholder="required to set active or prod tag"></div>' if protected else ''}
  <div class="flex">
    <button type="submit" name="set_active" value="1" class="btn btn-primary">Save &amp; Set Active</button>
    <button type="submit" name="set_active" value="0" class="btn btn-ghost">Save as Draft</button>
    <a href="{back_href}" class="btn btn-ghost">Cancel</a>
  </div>
</form>
</div>"""
    return _layout(title, body, protected, base)


# ---------------------------------------------------------------------------
# Diff view
# ---------------------------------------------------------------------------

def diff_page(name: str, versions: list[dict], v1: Optional[dict], v2: Optional[dict], protected: bool, base: str) -> str:
    options = "".join(
        f'<option value="{v["id"]}" {"selected" if v1 and v["id"] == v1["id"] else ""}>v{v["version_number"]} {v.get("tag") or ""}</option>'
        for v in versions
    )
    options2 = "".join(
        f'<option value="{v["id"]}" {"selected" if v2 and v["id"] == v2["id"] else ""}>v{v["version_number"]} {v.get("tag") or ""}</option>'
        for v in versions
    )

    diff_html = ""
    if v1 and v2:
        diff_html = _render_diff(v1["content"], v2["content"])

    body = f"""
<h1>Diff — {_esc(name)}</h1>
<div class="card">
  <form method="get" action="{base}/diff/{name}" class="flex">
    <div style="flex:1">
      <label class="text-muted">Version A</label>
      <select name="v1">{options}</select>
    </div>
    <div style="flex:1">
      <label class="text-muted">Version B</label>
      <select name="v2">{options2}</select>
    </div>
    <button type="submit" class="btn btn-primary" style="align-self:flex-end;">Compare</button>
  </form>
</div>
{diff_html}
<div class="mt-16"><a href="{base}/detail/{name}" class="btn btn-ghost">← Back</a></div>"""
    return _layout(f"Diff — {name}", body, protected, base)


def _render_diff(a: str, b: str) -> str:
    lines_a = a.splitlines(keepends=True)
    lines_b = b.splitlines(keepends=True)
    diff = list(difflib.unified_diff(lines_a, lines_b, lineterm=""))
    if not diff:
        return '<div class="card"><p class="text-muted">No differences.</p></div>'

    html_lines = []
    for line in diff[2:]:  # skip the --- +++ header lines
        if line.startswith("+"):
            html_lines.append(f'<span class="diff-add">+ {_esc(line[1:].rstrip())}</span>')
        elif line.startswith("-"):
            html_lines.append(f'<span class="diff-remove">- {_esc(line[1:].rstrip())}</span>')
        else:
            html_lines.append(f'<span class="diff-same">  {_esc(line.rstrip())}</span>')

    return f'<div class="diff-block">{"".join(html_lines)}</div>'


# ---------------------------------------------------------------------------
# A/B test
# ---------------------------------------------------------------------------

def ab_test_page(name: str, versions: list[dict], protected: bool, has_llm: bool, base: str) -> str:
    if not has_llm:
        body = f"""<h1>A/B Test — {_esc(name)}</h1>
<div class="card"><p class="text-muted">LLM not configured. Add <code>llm_url</code> to PromptManager to enable this feature.</p></div>
<a href="{base}/detail/{name}" class="btn btn-ghost">← Back</a>"""
        return _layout(f"A/B Test — {name}", body, protected, base)

    options = "".join(
        f'<option value="{v["id"]}">v{v["version_number"]} {v.get("tag") or ""}</option>'
        for v in versions
    )

    body = f"""
<h1>A/B Test — {_esc(name)}</h1>
<div class="card">
  <div class="form-group">
    <label>Test Input</label>
    <textarea id="ab_input" rows="4" placeholder="Enter your test input..."></textarea>
  </div>
  <div class="side-by-side">
    <div>
      <label class="text-muted">Version A</label>
      <select id="ver_a">{options}</select>
    </div>
    <div>
      <label class="text-muted">Version B</label>
      <select id="ver_b">{options}</select>
    </div>
  </div>
  <button class="btn btn-primary mt-16" onclick="runAB()">Run Test</button>
</div>
<div class="side-by-side mt-16" id="ab_results" style="display:none;">
  <div class="card">
    <h2 id="label_a">Version A</h2>
    <pre class="mono" id="out_a" style="white-space:pre-wrap;min-height:80px;"></pre>
  </div>
  <div class="card">
    <h2 id="label_b">Version B</h2>
    <pre class="mono" id="out_b" style="white-space:pre-wrap;min-height:80px;"></pre>
  </div>
</div>
<div class="mt-16"><a href="{base}/detail/{name}" class="btn btn-ghost">← Back</a></div>
<script>
async function runAB() {{
  const input = document.getElementById('ab_input').value.trim();
  const v_a = document.getElementById('ver_a').value;
  const v_b = document.getElementById('ver_b').value;
  if (!input) {{ alert('Enter test input first.'); return; }}
  document.getElementById('ab_results').style.display = 'grid';
  document.getElementById('out_a').textContent = 'Running...';
  document.getElementById('out_b').textContent = 'Running...';
  document.getElementById('label_a').textContent = 'Version ' + document.getElementById('ver_a').options[document.getElementById('ver_a').selectedIndex].text;
  document.getElementById('label_b').textContent = 'Version ' + document.getElementById('ver_b').options[document.getElementById('ver_b').selectedIndex].text;
  const [ra, rb] = await Promise.all([
    fetch('{base}/api/run', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{version_id: parseInt(v_a), input}})}}).then(r=>r.json()),
    fetch('{base}/api/run', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{version_id: parseInt(v_b), input}})}}).then(r=>r.json()),
  ]);
  document.getElementById('out_a').textContent = ra.output || ra.detail || 'Error';
  document.getElementById('out_b').textContent = rb.output || rb.detail || 'Error';
}}
</script>"""
    return _layout(f"A/B Test — {name}", body, protected, base)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    if not s:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _tag_badge(tag: Optional[str]) -> str:
    if not tag:
        return ""
    cls = {"prod": "badge-prod", "staging": "badge-staging", "experiment": "badge-experiment"}.get(tag, "")
    return f'<span class="badge {cls}">{tag}</span>'


def _pw_field(protected: bool) -> str:
    if not protected:
        return ""
    return '<input type="password" name="password" placeholder="admin password" style="width:140px;margin-right:4px;">'


# ---------------------------------------------------------------------------
# Logs page
# ---------------------------------------------------------------------------

def logs_page(logs: list[dict], protected: bool, base: str, prompt_filter: str = "") -> str:
    rows = ""
    for log in logs:
        rows += f"""<tr>
          <td class="text-muted">{str(log['timestamp'])[:19]}</td>
          <td><a href="{base}/detail/{log['prompt_name']}">{_esc(log['prompt_name'])}</a></td>
          <td class="mono" style="color:#a0aec0;">v{log['version_number']}</td>
          <td class="mono" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{_esc(log['input'] or '—')}</td>
          <td class="mono" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{_esc(log['output'] or '—')}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="5" style="color:#718096;text-align:center;padding:32px;">No logs yet.</td></tr>'

    body = f"""
<div class="flex" style="margin-bottom:20px;">
  <h1 style="margin:0;">Usage Logs</h1>
  <form method="get" action="{base}/logs" class="flex" style="margin-left:auto;">
    <input type="text" name="prompt" value="{_esc(prompt_filter)}" placeholder="Filter by prompt name" style="width:200px;">
    <button type="submit" class="btn btn-ghost">Filter</button>
    {'<a href="' + base + '/logs" class="btn btn-ghost">Clear</a>' if prompt_filter else ''}
  </form>
</div>
<div class="card" style="padding:0;overflow:hidden;">
<table>
  <thead><tr><th>Timestamp</th><th>Prompt</th><th>Version</th><th>Input</th><th>Output</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>
<p class="text-muted mt-8">Showing last 100 entries.</p>"""
    return _layout("Logs", body, protected, base)
