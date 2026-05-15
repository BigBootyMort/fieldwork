"""
Phase 13 — Authentication core.

Provides:
  hash_password / verify_password   bcrypt via passlib
  create_access_token / decode_token HS256 JWT via python-jose
  get_current_user                   FastAPI dependency (no DB hit)
  require_admin                      FastAPI dependency, role check
  optional_user                      Like get_current_user but non-raising
"""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import bcrypt as _bcrypt

log = logging.getLogger("fieldwork.auth")

# ── Signing key ───────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET", "")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    log.warning(
        "JWT_SECRET env var not set — using a random key. "
        "All tokens will be invalidated on container restart."
    )

ALGORITHM            = "HS256"
ACCESS_EXPIRE_HOURS  = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", "8"))

# ── Password hashing (bcrypt direct — bypasses passlib's bcrypt 5.x issues) ──
def _to_bytes(plain: str) -> bytes:
    """Encode and truncate to 72 bytes — bcrypt's hard limit."""
    return plain.encode("utf-8")[:72]


def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(_to_bytes(plain), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(_to_bytes(plain), hashed.encode("utf-8"))
    except Exception:
        return False


# ── Token handling ────────────────────────────────────────────────────────────
def create_access_token(payload: dict) -> str:
    """Sign a JWT with the given payload dict + an exp claim."""
    data = payload.copy()
    data["exp"] = datetime.now(timezone.utc) + timedelta(hours=ACCESS_EXPIRE_HOURS)
    return jwt.encode(data, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Return decoded payload or None if invalid/expired."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ── FastAPI dependencies ──────────────────────────────────────────────────────
_oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

_401 = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated — please sign in",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(token: str = Depends(_oauth2)) -> dict:
    """
    Validate the Bearer token and return its payload.
    Payload keys: sub (user_id), username, role.
    Raises 401 if missing or invalid.
    """
    if not token:
        raise _401
    payload = decode_token(token)
    if not payload or "sub" not in payload:
        raise _401
    return payload


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Raises 403 if the authenticated user is not an admin."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


async def optional_user(token: str = Depends(_oauth2)) -> Optional[dict]:
    """
    Like get_current_user but returns None instead of raising if no/bad token.
    Use for endpoints that work both authenticated and unauthenticated.
    """
    if not token:
        return None
    return decode_token(token)   # may be None if expired
