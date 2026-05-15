"""
Phase 13 — Audit log.

Every significant write action is recorded as an AuditEvent node.
Events are keyed by a SHA-256 hash of (user_id + action + timestamp)
so duplicates are naturally avoided.

Common action strings
---------------------
  user.register   user.role_change
  case.create     case.archive     case.share
  subject.add     subject.remove
  note.add        note.delete      note.pin
  task.add        task.delete      task.toggle
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("fieldwork.audit")


def _audit_id(user_id: str, action: str, ts: str) -> str:
    raw = f"{user_id}:{action}:{ts}"
    return "audit_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


async def log_event(
    graph_db,
    *,
    action: str,
    user_id: str,
    username: str,
    target_id: Optional[str] = None,
    target_type: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """
    Write an AuditEvent node.
    Errors are caught and logged rather than propagated — audit failures
    must never break the primary operation.
    """
    ts  = datetime.now(timezone.utc).isoformat()
    aid = _audit_id(user_id, action, ts)
    try:
        async with graph_db.driver.session() as session:
            await session.run(
                """
                CREATE (a:AuditEvent {
                    id:          $id,
                    action:      $action,
                    user_id:     $user_id,
                    username:    $username,
                    target_id:   $target_id,
                    target_type: $target_type,
                    detail:      $detail,
                    ts:          $ts
                })
                """,
                id=aid, action=action, user_id=user_id, username=username,
                target_id=target_id, target_type=target_type,
                detail=detail, ts=ts,
            )
    except Exception as exc:
        log.warning("audit.log_event failed (action=%s): %s", action, exc)


async def get_audit_log(
    graph_db,
    *,
    target_id: Optional[str] = None,
    user_id:   Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Return audit events in reverse-chronological order.
    Pass target_id to scope to a specific case/entity,
    or user_id to scope to a specific user's actions.
    """
    wheres: list[str] = []
    params: dict      = {"limit": limit}

    if target_id:
        wheres.append("a.target_id = $target_id")
        params["target_id"] = target_id
    if user_id:
        wheres.append("a.user_id = $user_id")
        params["user_id"] = user_id

    where_str = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    async with graph_db.driver.session() as session:
        result = await session.run(
            f"MATCH (a:AuditEvent) {where_str} "
            "RETURN a {.*} AS a ORDER BY a.ts DESC LIMIT $limit",
            **params,
        )
        return [dict(r["a"]) async for r in result]
