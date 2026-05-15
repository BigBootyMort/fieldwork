"""
Phase 13 — User management.

User nodes in Neo4j
-------------------
  id            user_<sha256[:16]>
  username      unique, case-insensitive
  email         unique, case-insensitive
  password_hash bcrypt via passlib
  role          "admin" | "investigator"
  created_at    ISO-8601 UTC
  active        bool

The very first user to register is automatically granted the "admin" role.
Subsequent users receive the "investigator" role and must be promoted by an admin.
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from auth import hash_password

log = logging.getLogger("fieldwork.users")

VALID_ROLES   = {"admin", "investigator"}
_USERNAME_RE  = re.compile(r"^[a-zA-Z0-9_.\-]{3,32}$")
_EMAIL_RE     = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── ID generation ─────────────────────────────────────────────────────────────

def _user_id(username: str) -> str:
    h = hashlib.sha256(username.lower().encode()).hexdigest()[:16]
    return f"user_{h}"


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def register_user(
    graph_db,
    username: str,
    email: str,
    password: str,
) -> dict:
    """
    Create a new user.  First user becomes admin automatically.
    Returns the public user dict (no password_hash).
    Raises ValueError on validation failures or duplicate username/email.
    """
    username = username.strip()
    email    = email.strip().lower()

    if not _USERNAME_RE.match(username):
        raise ValueError("Username must be 3–32 characters: letters, digits, _ . -")
    if not _EMAIL_RE.match(email):
        raise ValueError("Invalid email address")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")

    uid   = _user_id(username)
    now   = datetime.now(timezone.utc).isoformat()
    pw_h  = hash_password(password)

    async with graph_db.driver.session() as session:
        # Is this the first user?
        n_rec = await (await session.run("MATCH (u:User) RETURN count(u) AS n")).single()
        role  = "admin" if (n_rec["n"] if n_rec else 0) == 0 else "investigator"

        # Uniqueness check
        dup = await (await session.run(
            "MATCH (u:User) WHERE toLower(u.username) = toLower($un) OR u.email = $em "
            "RETURN u.id LIMIT 1",
            un=username, em=email,
        )).single()
        if dup:
            raise ValueError("Username or email is already registered")

        await session.run(
            """
            CREATE (u:User {
                id:            $id,
                username:      $username,
                email:         $email,
                password_hash: $pw_h,
                role:          $role,
                created_at:    $now,
                active:        true
            })
            """,
            id=uid, username=username, email=email,
            pw_h=pw_h, role=role, now=now,
        )

    log.info("Registered user %r (role=%s)", username, role)
    return {"id": uid, "username": username, "email": email, "role": role, "created_at": now}


async def get_user_by_username(graph_db, username: str) -> Optional[dict]:
    """Return the full user dict (including password_hash) or None."""
    async with graph_db.driver.session() as session:
        rec = await (await session.run(
            "MATCH (u:User) WHERE toLower(u.username) = toLower($un) AND u.active = true "
            "RETURN u {.*} AS u LIMIT 1",
            un=username,
        )).single()
    return dict(rec["u"]) if rec else None


async def get_user_by_id(graph_db, user_id: str) -> Optional[dict]:
    """Return user dict without password_hash, or None."""
    async with graph_db.driver.session() as session:
        rec = await (await session.run(
            "MATCH (u:User {id: $id}) RETURN u {.*} AS u LIMIT 1",
            id=user_id,
        )).single()
    if not rec:
        return None
    u = dict(rec["u"])
    u.pop("password_hash", None)
    return u


async def list_users(graph_db) -> list[dict]:
    """Return all users (no password_hash), ordered by creation date."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (u:User) "
            "RETURN u {.id, .username, .email, .role, .created_at, .active} AS u "
            "ORDER BY u.created_at"
        )
        return [dict(r["u"]) async for r in result]


async def update_user_role(graph_db, user_id: str, role: str) -> bool:
    if role not in VALID_ROLES:
        return False
    async with graph_db.driver.session() as session:
        rec = await (await session.run(
            "MATCH (u:User {id: $id}) SET u.role = $role RETURN count(u) AS n",
            id=user_id, role=role,
        )).single()
    return (rec["n"] if rec else 0) > 0


async def deactivate_user(graph_db, user_id: str) -> bool:
    async with graph_db.driver.session() as session:
        rec = await (await session.run(
            "MATCH (u:User {id: $id}) SET u.active = false RETURN count(u) AS n",
            id=user_id,
        )).single()
    return (rec["n"] if rec else 0) > 0
