"""
Fieldwork Phase 14 — Timeline engine.

Produces a normalised, reverse-chronological event stream from two sources:

  Global timeline  → AuditEvent nodes across the whole graph
  Case timeline    → AuditEvent nodes for one case
                    + Note creation events
                    + Task-completion events
                    (supplementing audit for pre-Phase-13 data and
                     adding content that isn't captured in audit detail)

Every returned event shares this shape:
  {
    id:          str           unique stable identifier
    ts_ms:       int           milliseconds since Unix epoch (for JS sorting)
    ts_iso:      str           ISO-8601 UTC string (for display)
    category:    str           case | note | task | entity | user | other
    action:      str           raw action string or synthetic type
    actor:       str           username or "—"
    title:       str           short human-readable headline
    detail:      str           body / extra text (may be empty)
    target_id:   str | None
    target_type: str | None
    case_id:     str | None
    case_title:  str | None
  }
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ── Timestamp normalisation ───────────────────────────────────────────────────

def _ts_to_ms(ts) -> int:
    """Convert any timestamp representation to milliseconds since Unix epoch."""
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        # Neo4j timestamp() is already in ms; Python floats are seconds.
        return int(ts) if ts > 2e12 else int(ts * 1000)

    # neo4j.time.DateTime — use .to_native() if available
    if hasattr(ts, "to_native"):
        try:
            native = ts.to_native()
            if hasattr(native, "timestamp"):
                return int(native.timestamp() * 1000)
        except Exception:
            pass

    s = str(ts).strip()
    if not s:
        return 0
    try:
        s = s.replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _ms_to_iso(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# ── Human-readable action titles ──────────────────────────────────────────────

_ACTION_TITLES: dict[str, str] = {
    "case.create":      "Case created",
    "case.archive":     "Case archived",
    "case.share":       "Case shared",
    "subject.add":      "Subject added",
    "subject.remove":   "Subject removed",
    "note.add":         "Note added",
    "note.delete":      "Note deleted",
    "note.pin":         "Note pinned",
    "task.add":         "Task added",
    "task.delete":      "Task deleted",
    "task.toggle":      "Task completed",
    "user.register":    "User registered",
    "user.role_change": "Role changed",
    "entity.create":    "Entity added to graph",
}


def _action_category(action: str) -> str:
    prefix = (action or "").split(".")[0]
    return {
        "case":    "case",
        "note":    "note",
        "task":    "task",
        "subject": "entity",
        "entity":  "entity",
        "user":    "user",
        "auth":    "user",
    }.get(prefix, "other")


def _normalise_audit(raw: dict, case_title: Optional[str] = None) -> dict:
    action = raw.get("action") or ""
    ts_ms  = _ts_to_ms(raw.get("ts"))
    return {
        "id":          raw.get("id", ""),
        "ts_ms":       ts_ms,
        "ts_iso":      _ms_to_iso(ts_ms),
        "category":    _action_category(action),
        "action":      action,
        "actor":       raw.get("username") or raw.get("user_id") or "—",
        "title":       _ACTION_TITLES.get(action, action or "Event"),
        "detail":      raw.get("detail") or "",
        "target_id":   raw.get("target_id"),
        "target_type": raw.get("target_type"),
        "case_id":     raw.get("target_id") if raw.get("target_type") == "Case" else None,
        "case_title":  case_title,
    }


# ── Global timeline ───────────────────────────────────────────────────────────

async def get_global_timeline(
    graph_db,
    limit:     int            = 200,
    since_iso: Optional[str] = None,
    category:  Optional[str] = None,
) -> list[dict]:
    """
    Most recent audit events across the whole system.
    Optionally filtered by `since_iso` (ISO timestamp) and `category`.
    """
    wheres: list[str] = []
    params: dict = {"limit": limit}

    if since_iso:
        wheres.append("a.ts > $since")
        params["since"] = since_iso

    where_str = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    async with graph_db.driver.session() as session:
        result = await session.run(
            f"""
            MATCH (a:AuditEvent) {where_str}
            OPTIONAL MATCH (c:Case {{id: a.target_id}})
            RETURN a {{.*}} AS a, c.title AS case_title
            ORDER BY a.ts DESC
            LIMIT $limit
            """,
            **params,
        )
        events: list[dict] = []
        async for r in result:
            ev = _normalise_audit(dict(r["a"]), case_title=r["case_title"])
            events.append(ev)

    if category:
        events = [e for e in events if e["category"] == category]

    return events


# ── Case timeline ─────────────────────────────────────────────────────────────

async def get_case_timeline(
    graph_db,
    case_id: str,
    limit:   int = 200,
) -> list[dict]:
    """
    Chronological event stream for one case, newest first.

    Three sources are merged:
      1. AuditEvent nodes whose target_id is this case  (with actor info)
      2. Note nodes by created_at                       (with note content)
      3. Completed Task nodes by completed_at           (with task text)
      4. The Case node itself                           (creation timestamp)

    Sources 2–4 supplement the audit log and are always included so that
    pre-Phase-13 investigations (which have no audit events) still produce
    a useful timeline.  Audit events are de-duplicated against note/task
    direct events by tracking covered note/task IDs.
    """
    events: list[dict] = []
    audited_note_ids: set[str] = set()
    audited_task_ids: set[str] = set()

    async with graph_db.driver.session() as session:

        # ── 1. Audit events ───────────────────────────────────────────────
        result = await session.run(
            """
            MATCH (a:AuditEvent {target_id: $cid})
            RETURN a {.*} AS a
            ORDER BY a.ts DESC
            LIMIT $limit
            """,
            cid=case_id, limit=limit,
        )
        async for r in result:
            raw = dict(r["a"])
            ev  = _normalise_audit(raw, case_title=None)
            ev["case_id"] = case_id
            events.append(ev)
            # Track IDs that the audit log already covers
            detail = raw.get("detail") or ""
            if "note_id=" in detail:
                audited_note_ids.add(detail.split("note_id=")[-1].split(",")[0].strip())
            if "task_id=" in detail:
                audited_task_ids.add(detail.split("task_id=")[-1].split(",")[0].strip())

        # ── 2. Note creation events ───────────────────────────────────────
        result = await session.run(
            """
            MATCH (:Case {id: $cid})-[:HAS_NOTE]->(n:Note)
            RETURN n.id AS id, n.content AS content, n.type AS ntype,
                   toString(n.created_at) AS created_at
            ORDER BY n.created_at DESC
            LIMIT $limit
            """,
            cid=case_id, limit=limit,
        )
        async for r in result:
            note_id = r["id"]
            if note_id in audited_note_ids:
                continue            # Already covered by audit event
            ts_ms = _ts_to_ms(r["created_at"])
            body  = (r["content"] or "")[:200]
            events.append({
                "id":          note_id + "_created",
                "ts_ms":       ts_ms,
                "ts_iso":      _ms_to_iso(ts_ms),
                "category":    "note",
                "action":      "note.add",
                "actor":       "—",
                "title":       f"Note added ({r['ntype'] or 'general'})",
                "detail":      body,
                "target_id":   note_id,
                "target_type": "Note",
                "case_id":     case_id,
                "case_title":  None,
            })

        # ── 3. Task completion events ─────────────────────────────────────
        result = await session.run(
            """
            MATCH (:Case {id: $cid})-[:HAS_TASK]->(t:Task)
            WHERE t.completed = true AND t.completed_at IS NOT NULL
            RETURN t.id AS id, t.text AS text,
                   toString(t.completed_at) AS completed_at
            ORDER BY t.completed_at DESC
            LIMIT $limit
            """,
            cid=case_id, limit=limit,
        )
        async for r in result:
            task_id = r["id"]
            if task_id in audited_task_ids:
                continue
            ts_ms = _ts_to_ms(r["completed_at"])
            events.append({
                "id":          task_id + "_done",
                "ts_ms":       ts_ms,
                "ts_iso":      _ms_to_iso(ts_ms),
                "category":    "task",
                "action":      "task.toggle",
                "actor":       "—",
                "title":       "Task completed",
                "detail":      r["text"] or "",
                "target_id":   task_id,
                "target_type": "Task",
                "case_id":     case_id,
                "case_title":  None,
            })

        # ── 4. Case creation fallback ─────────────────────────────────────
        # Only add if no audit event already covers case.create for this case.
        has_create = any(e["action"] == "case.create" for e in events)
        if not has_create:
            result = await session.run(
                """
                MATCH (c:Case {id: $cid})
                RETURN c.title AS title, toString(c.created_at) AS created_at
                LIMIT 1
                """,
                cid=case_id,
            )
            rec = await result.single()
            if rec and rec["created_at"]:
                ts_ms = _ts_to_ms(rec["created_at"])
                events.append({
                    "id":          case_id + "_created",
                    "ts_ms":       ts_ms,
                    "ts_iso":      _ms_to_iso(ts_ms),
                    "category":    "case",
                    "action":      "case.create",
                    "actor":       "—",
                    "title":       "Case created",
                    "detail":      rec["title"] or "",
                    "target_id":   case_id,
                    "target_type": "Case",
                    "case_id":     case_id,
                    "case_title":  rec["title"],
                })

    events.sort(key=lambda e: e["ts_ms"], reverse=True)
    return events[:limit]
