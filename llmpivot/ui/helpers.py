"""Shared UI helpers for rendering consistent HTML fragments."""

from __future__ import annotations

import html
from typing import Any, Optional


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_user_badge(user: Optional[dict], base: str = "") -> str:
    if not user:
        return ""

    username = escape(user.get("username", "user"))
    role = escape(user.get("role", "editor"))
    admin_link = (
        f'<a href="{base}/users" class="nav-link" style="color:var(--accent);font-weight:700;">Users</a>'
        if role.lower() == "admin"
        else ""
    )
    return f"""
        <div style="display:flex;align-items:center;gap:8px;margin-left:12px;padding-left:12px;border-left:1px solid var(--line);">
          <span style="font-size:0.82rem;color:#e2e8f0;font-weight:600;">👤 {username}</span>
          <span class="badge badge-active" style="font-size:0.68rem;">{role.upper()}</span>
          {admin_link}
          <a href="{base}/logout" class="nav-link" style="font-size:0.82rem;color:#feb2b2;">Logout</a>
        </div>"""
