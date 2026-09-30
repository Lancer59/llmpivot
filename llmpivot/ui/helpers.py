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
        f'<a href="{base}/users" class="nav-link">Users</a>'
        if role.lower() == "admin"
        else ""
    )
    initial = escape((user.get("username") or "U")[:1].upper())
    return f"""
        <div class="user-nav">
          <span class="user-avatar" aria-hidden="true">{initial}</span>
          <span class="user-name">{username}</span>
          <span class="badge badge-active">{role.upper()}</span>
          {admin_link}
          <a href="{base}/logout" class="nav-link">Logout</a>
        </div>"""
