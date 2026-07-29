"""
Authentication, Password Security, Token Management, and RBAC for llmpivot.
Uses Python standard library (hashlib, hmac, base64, json) to guarantee zero extra dependencies.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional, Dict, Any
from fastapi import Request


ROLE_HIERARCHY = {"admin": 3, "editor": 2, "viewer": 1}


def hash_password(password: str, salt: Optional[str] = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"{salt}${key.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, key_hex = password_hash.split("$", 1)
        check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return hmac.compare_digest(check.hex(), key_hex)
    except Exception:
        return False


def create_token(payload: Dict[str, Any], secret_key: str, expires_in: int = 86400) -> str:
    data = payload.copy()
    data["exp"] = int(time.time()) + expires_in
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    b64_payload = base64.urlsafe_b64encode(json_bytes).decode("utf-8").rstrip("=")
    
    sig = hmac.new(secret_key.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256).digest()
    b64_sig = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")
    return f"{b64_payload}.{b64_sig}"


def verify_token(token: str, secret_key: str) -> Optional[Dict[str, Any]]:
    try:
        b64_payload, b64_sig = token.split(".", 1)
        expected_sig = hmac.new(secret_key.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256).digest()
        actual_sig = base64.urlsafe_b64decode(b64_sig + "==")
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload_bytes = base64.urlsafe_b64decode(b64_payload + "==")
        payload = json.loads(payload_bytes.decode("utf-8"))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def get_current_user_from_request(request: Request, secret_key: str) -> Optional[Dict[str, Any]]:
    token = request.cookies.get("llmpivot_session")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    if not token:
        return None
    return verify_token(token, secret_key)


def has_permission(user_role: str, min_required_role: str) -> bool:
    user_level = ROLE_HIERARCHY.get(user_role.lower(), 0)
    required_level = ROLE_HIERARCHY.get(min_required_role.lower(), 0)
    return user_level >= required_level
