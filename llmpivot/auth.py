"""
Authentication, Password Security, Token Management, and RBAC for llmpivot.

Password hashing strategy (priority order):
  1. argon2-cffi  — best, Argon2id (install: pip install argon2-cffi)
  2. bcrypt       — good, widely used (install: pip install bcrypt)
  3. PBKDF2-SHA256 via stdlib — always available, used as fallback

Token format: base64url(JSON payload) . HMAC-SHA256 signature
Uses Python standard library for HMAC/token logic (zero extra deps for auth core).
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional, Dict, Any
from fastapi import Request

logger = logging.getLogger("llmpivot.auth")

ROLE_HIERARCHY = {"admin": 3, "editor": 2, "viewer": 1}

# ---------------------------------------------------------------------------
# Password hashing — auto-selects best available backend
# ---------------------------------------------------------------------------

def _detect_hasher() -> str:
    """Return the best available password hasher: 'argon2', 'bcrypt', or 'pbkdf2'."""
    try:
        from argon2 import PasswordHasher  # noqa: F401
        return "argon2"
    except ImportError:
        pass
    try:
        import bcrypt  # noqa: F401
        return "bcrypt"
    except ImportError:
        pass
    return "pbkdf2"


_HASHER = _detect_hasher()

if _HASHER == "argon2":
    logger.debug("Password hasher: argon2id (argon2-cffi)")
elif _HASHER == "bcrypt":
    logger.debug("Password hasher: bcrypt")
else:
    logger.warning(
        "Password hasher: PBKDF2-SHA256 (stdlib fallback). "
        "For production, install argon2-cffi: pip install argon2-cffi"
    )


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """
    Hash a password using the best available backend.
    Prefix stored value with 'argon2$', 'bcrypt$', or 'pbkdf2$' so
    verify_password can dispatch to the right verifier regardless of
    which backend was active when the hash was created.
    """
    if _HASHER == "argon2":
        from argon2 import PasswordHasher
        ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
        raw = ph.hash(password)
        return f"argon2${raw}"
    elif _HASHER == "bcrypt":
        import bcrypt as _bcrypt
        hashed = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt(rounds=12))
        return f"bcrypt${hashed.decode('utf-8')}"
    else:
        # PBKDF2-SHA256 fallback
        if salt is None:
            salt = secrets.token_hex(16)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 260000)
        return f"pbkdf2${salt}${key.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a password against a stored hash.
    Handles all three formats (argon2$, bcrypt$, pbkdf2$) plus the legacy
    format '{salt}${key_hex}' created before the prefix scheme was introduced.
    """
    try:
        if password_hash.startswith("argon2$"):
            from argon2 import PasswordHasher
            from argon2.exceptions import VerifyMismatchError, VerifyInvalidError, InvalidHashError
            ph = PasswordHasher()
            raw_hash = password_hash[len("argon2$"):]
            try:
                return ph.verify(raw_hash, password)
            except (VerifyMismatchError, VerifyInvalidError, InvalidHashError):
                return False

        elif password_hash.startswith("bcrypt$"):
            import bcrypt as _bcrypt
            raw_hash = password_hash[len("bcrypt$"):]
            return _bcrypt.checkpw(password.encode("utf-8"), raw_hash.encode("utf-8"))

        elif password_hash.startswith("pbkdf2$"):
            # New PBKDF2 format: pbkdf2${salt}${key_hex}
            _, salt, key_hex = password_hash.split("$", 2)
            check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 260000)
            return hmac.compare_digest(check.hex(), key_hex)

        else:
            # Legacy format: {salt}${key_hex}  (original PBKDF2 at 100k iterations)
            salt, key_hex = password_hash.split("$", 1)
            check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
            return hmac.compare_digest(check.hex(), key_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HMAC session tokens
# ---------------------------------------------------------------------------

def create_token(payload: Dict[str, Any], secret_key: str, expires_in: int = 86400) -> str:
    """Create a signed token: base64url(json_payload).HMAC-SHA256(sig)."""
    data = payload.copy()
    data["exp"] = int(time.time()) + expires_in
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    b64_payload = base64.urlsafe_b64encode(json_bytes).decode("utf-8").rstrip("=")

    sig = hmac.new(secret_key.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256).digest()
    b64_sig = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")
    return f"{b64_payload}.{b64_sig}"


def verify_token(token: str, secret_key: str) -> Optional[Dict[str, Any]]:
    """Verify and decode a token. Returns payload dict or None if invalid/expired."""
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


# ---------------------------------------------------------------------------
# CSRF token helpers
# ---------------------------------------------------------------------------

_CSRF_SECRET_SUFFIX = ":csrf"


def generate_csrf_token(session_token: str, secret_key: str) -> str:
    """
    Generate a per-session CSRF token derived from the session token.
    Binding it to the session ensures it rotates with every login/logout.
    """
    raw = (session_token + _CSRF_SECRET_SUFFIX).encode("utf-8")
    sig = hmac.new(secret_key.encode("utf-8"), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")


def verify_csrf_token(submitted: Optional[str], session_token: str, secret_key: str) -> bool:
    """Constant-time comparison of submitted CSRF token against expected."""
    if not submitted:
        return False
    expected = generate_csrf_token(session_token, secret_key)
    try:
        return hmac.compare_digest(
            submitted.encode("utf-8"),
            expected.encode("utf-8"),
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def get_current_user_from_request(request: Request, secret_key: str) -> Optional[Dict[str, Any]]:
    """Extract and verify the session token from cookie or Authorization header."""
    token = request.cookies.get("llmpivot_session")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    if not token:
        return None

    user = verify_token(token, secret_key)
    if not isinstance(user, dict):
        return None
    return user


def get_session_token_from_request(request: Request) -> Optional[str]:
    """Return the raw session token string from cookie or header."""
    token = request.cookies.get("llmpivot_session")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    return token


def has_permission(user_role: str, min_required_role: str) -> bool:
    """Return True if user_role meets or exceeds min_required_role."""
    user_level = ROLE_HIERARCHY.get(user_role.lower(), 0)
    required_level = ROLE_HIERARCHY.get(min_required_role.lower(), 0)
    return user_level >= required_level
