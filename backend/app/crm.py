"""
Phase 9 — Investigation CRM.

Provides Case management for journalists:
  - Cases:    named investigation containers with status and priority
  - Subjects: graph entities (Person, Company, Email…) linked to a case
  - Notes:    typed findings/leads/hypotheses/cautions attached to a case
  - Tasks:    verification checklist items per case
  - Export:   full dossier as JSON or Markdown

Neo4j schema used:
  (Case)-[:HAS_NOTE]->   (Note)
  (Case)-[:HAS_TASK]->   (Task)
  (Case)-[:HAS_SUBJECT]->(any entity node)
"""
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("fieldwork.crm")

# ── Allowed enum values ───────────────────────────────────────────────────────
CASE_STATUSES   = {"open", "active", "review", "closed", "archived"}
CASE_PRIORITIES = {"high", "medium", "low"}
NOTE_TYPES      = {"finding", "lead", "hypothesis", "caution", "general"}
SUBJECT_ROLES   = {"primary", "secondary", "associate", "witness", "unknown"}

# Entity labels that may be added as case subjects
SUBJECT_LABELS = frozenset([
    "Person", "Company", "Email", "Domain", "IP", "Username", "Account",
    "Wallet", "Aircraft", "TelegramChannel", "Breach", "DarkWebMention",
])

# ── ID helpers ────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower().strip()).strip("_")[:50]


def _frag(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_entity_label(label: str) -> str:
    if label in SUBJECT_LABELS:
        return label
    if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", label):
        return label
    raise ValueError(f"Invalid entity label: {label!r}")


# ── Case CRUD ─────────────────────────────────────────────────────────────────

async def create_case(
    graph_db,
    title: str,
    description: str = "",
    status: str = "open",
    priority: str = "medium",
    owner_id: Optional[str] = None,
) -> dict:
    slug  = _slug(title)
    ts    = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    cid   = f"case_{slug}_{ts}"
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            CREATE (c:Case {
                id:          $id,
                title:       $title,
                description: $description,
                status:      $status,
                priority:    $priority,
                created_at:  datetime(),
                updated_at:  datetime()
            })
            RETURN c { .* } AS c
            """,
            id=cid, title=title, description=description,
            status=status, priority=priority,
        )
        record = await result.single()
        case = dict(record["c"])
        if owner_id:
            await session.run(
                "MATCH (c:Case {id: $cid}), (u:User {id: $uid}) MERGE (c)-[:OWNED_BY]->(u)",
                cid=cid, uid=owner_id,
            )
        return case


async def list_cases(
    graph_db,
    include_archived: bool = False,
    user_id: Optional[str] = None,
) -> list[dict]:
    wheres: list[str] = []
    params: dict      = {}

    if not include_archived:
        wheres.append("c.status <> 'archived'")

    # If a user_id is given, show only their own cases + shared cases +
    # any legacy cases that have no owner (created before auth was added).
    if user_id:
        wheres.append(
            "(NOT EXISTS { (c)-[:OWNED_BY]->() }"
            " OR EXISTS { (c)-[:OWNED_BY]->(:User {id: $uid}) }"
            " OR EXISTS { (c)-[:SHARED_WITH]->(:User {id: $uid}) })"
        )
        params["uid"] = user_id

    where_str = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    async with graph_db.driver.session() as session:
        result = await session.run(
            f"""
            MATCH (c:Case) {where_str}
            OPTIONAL MATCH (c)-[:HAS_NOTE]->(n:Note)
            OPTIONAL MATCH (c)-[:HAS_TASK]->(t:Task)
            OPTIONAL MATCH (c)-[:HAS_SUBJECT]->(sub)
            OPTIONAL MATCH (c)-[:OWNED_BY]->(owner:User)
            WITH c,
                 count(DISTINCT n)   AS note_count,
                 count(DISTINCT sub) AS subject_count,
                 collect(t)          AS all_tasks,
                 owner.username      AS owner_username
            RETURN
                c {{ .* }}                                                         AS c,
                note_count, subject_count,
                size(all_tasks)                                                    AS task_total,
                size([x IN all_tasks WHERE x IS NOT NULL AND x.completed = false]) AS tasks_pending,
                owner_username
            ORDER BY
                CASE c.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                c.updated_at DESC
            """,
            **params,
        )
        rows: list[dict] = []
        async for r in result:
            c = dict(r["c"])
            c["note_count"]     = r["note_count"]
            c["subject_count"]  = r["subject_count"]
            c["task_total"]     = r["task_total"]
            c["tasks_pending"]  = r["tasks_pending"]
            c["owner_username"] = r["owner_username"]
            rows.append(c)
        return rows


async def get_case(graph_db, case_id: str) -> Optional[dict]:
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (c:Case {id: $id}) RETURN c { .* } AS c LIMIT 1",
            id=case_id,
        )
        record = await result.single()
        return dict(record["c"]) if record else None


async def update_case(
    graph_db,
    case_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
) -> Optional[dict]:
    sets: list[str] = ["c.updated_at = datetime()"]
    params: dict    = {"id": case_id}
    if title       is not None: sets.append("c.title = $title");       params["title"]       = title
    if description is not None: sets.append("c.description = $desc");  params["desc"]        = description
    if status      is not None: sets.append("c.status = $status");     params["status"]      = status
    if priority    is not None: sets.append("c.priority = $priority"); params["priority"]    = priority

    async with graph_db.driver.session() as session:
        result = await session.run(
            f"MATCH (c:Case {{id: $id}}) SET {', '.join(sets)} RETURN c {{ .* }} AS c",
            **params,
        )
        record = await result.single()
        return dict(record["c"]) if record else None


async def archive_case(graph_db, case_id: str) -> bool:
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (c:Case {id: $id}) SET c.status = 'archived', c.updated_at = datetime() RETURN c.id AS id",
            id=case_id,
        )
        record = await result.single()
        return record is not None


# ── Subjects ──────────────────────────────────────────────────────────────────

async def add_subject(
    graph_db, case_id: str, entity_id: str, entity_label: str, role: str = "unknown"
) -> dict:
    lbl = _safe_entity_label(entity_label)
    async with graph_db.driver.session() as session:
        # Ensure the entity node exists
        check = await session.run(
            f"MATCH (e:{lbl} {{id: $eid}}) RETURN e.id AS id LIMIT 1",
            eid=entity_id,
        )
        if not await check.single():
            raise ValueError(f"{lbl} node '{entity_id}' not found in graph")

        result = await session.run(
            f"""
            MATCH (c:Case {{id: $cid}})
            MATCH (e:{lbl} {{id: $eid}})
            MERGE (c)-[r:HAS_SUBJECT]->(e)
            ON CREATE SET
                r.role         = $role,
                r.entity_label = $lbl,
                r.added_at     = datetime()
            ON MATCH SET
                r.role = $role
            RETURN
                e.id          AS entity_id,
                labels(e)[0]  AS entity_label,
                r.role        AS role,
                r.added_at    AS added_at,
                e {{ .* }}   AS props
            """,
            cid=case_id, eid=entity_id, role=role, lbl=lbl,
        )
        record = await result.single()
        if not record:
            raise ValueError(f"Case '{case_id}' not found")
        props = dict(record["props"])
        display = (
            props.get("name") or props.get("address") or
            props.get("handle") or props.get("title") or entity_id
        )
        return {
            "entity_id":    record["entity_id"],
            "entity_label": record["entity_label"],
            "display_name": display,
            "role":         record["role"],
            "props":        props,
        }


async def remove_subject(graph_db, case_id: str, entity_id: str) -> bool:
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (c:Case {id: $cid})-[r:HAS_SUBJECT]->(e {id: $eid}) DELETE r RETURN count(r) AS n",
            cid=case_id, eid=entity_id,
        )
        record = await result.single()
        return bool(record and record["n"] > 0)


async def get_subjects(graph_db, case_id: str) -> list[dict]:
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Case {id: $id})-[r:HAS_SUBJECT]->(sub)
            RETURN
                sub.id        AS entity_id,
                labels(sub)[0] AS entity_label,
                r.role        AS role,
                sub { .* }   AS props
            ORDER BY r.added_at
            """,
            id=case_id,
        )
        rows: list[dict] = []
        async for r in result:
            props = dict(r["props"])
            display = (
                props.get("name") or props.get("address") or
                props.get("handle") or props.get("title") or r["entity_id"]
            )
            rows.append({
                "entity_id":    r["entity_id"],
                "entity_label": r["entity_label"],
                "display_name": display,
                "role":         r["role"],
                "props":        props,
            })
        return rows


# ── Notes ─────────────────────────────────────────────────────────────────────

async def add_note(
    graph_db, case_id: str, content: str,
    note_type: str = "general", pinned: bool = False,
) -> dict:
    nid = f"note_{_frag(case_id, content, _now_iso())}"
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Case {id: $cid})
            CREATE (n:Note {
                id:         $id,
                case_id:    $cid,
                content:    $content,
                type:       $type,
                pinned:     $pinned,
                created_at: datetime()
            })
            CREATE (c)-[:HAS_NOTE]->(n)
            SET c.updated_at = datetime()
            RETURN n { .* } AS n
            """,
            cid=case_id, id=nid, content=content,
            type=note_type, pinned=pinned,
        )
        record = await result.single()
        if not record:
            raise ValueError(f"Case '{case_id}' not found")
        return dict(record["n"])


async def pin_note(graph_db, case_id: str, note_id: str, pinned: bool) -> bool:
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (:Case {id: $cid})-[:HAS_NOTE]->(n:Note {id: $nid}) "
            "SET n.pinned = $pinned RETURN n.id AS id",
            cid=case_id, nid=note_id, pinned=pinned,
        )
        return (await result.single()) is not None


async def delete_note(graph_db, case_id: str, note_id: str) -> bool:
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (:Case {id: $cid})-[:HAS_NOTE]->(n:Note {id: $nid}) DETACH DELETE n RETURN 1 AS ok",
            cid=case_id, nid=note_id,
        )
        return (await result.single()) is not None


async def get_notes(graph_db, case_id: str) -> list[dict]:
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (:Case {id: $id})-[:HAS_NOTE]->(n:Note)
            RETURN n { .* } AS n
            ORDER BY n.pinned DESC, n.created_at DESC
            """,
            id=case_id,
        )
        return [dict(r["n"]) async for r in result]


# ── Tasks ─────────────────────────────────────────────────────────────────────

async def add_task(graph_db, case_id: str, text: str) -> dict:
    tid = f"task_{_frag(case_id, text, _now_iso())}"
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (c:Case {id: $cid})
            CREATE (t:Task {
                id:         $id,
                case_id:    $cid,
                text:       $text,
                completed:  false,
                created_at: datetime()
            })
            CREATE (c)-[:HAS_TASK]->(t)
            SET c.updated_at = datetime()
            RETURN t { .* } AS t
            """,
            cid=case_id, id=tid, text=text,
        )
        record = await result.single()
        if not record:
            raise ValueError(f"Case '{case_id}' not found")
        return dict(record["t"])


async def toggle_task(graph_db, case_id: str, task_id: str, completed: bool) -> bool:
    async with graph_db.driver.session() as session:
        cypher = (
            "MATCH (:Case {id: $cid})-[:HAS_TASK]->(t:Task {id: $tid}) "
            "SET t.completed = $done"
        )
        if completed:
            cypher += ", t.completed_at = datetime()"
        cypher += " RETURN t.id AS id"
        result = await session.run(cypher, cid=case_id, tid=task_id, done=completed)
        return (await result.single()) is not None


async def delete_task(graph_db, case_id: str, task_id: str) -> bool:
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (:Case {id: $cid})-[:HAS_TASK]->(t:Task {id: $tid}) DETACH DELETE t RETURN 1 AS ok",
            cid=case_id, tid=task_id,
        )
        return (await result.single()) is not None


async def get_tasks(graph_db, case_id: str) -> list[dict]:
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (:Case {id: $id})-[:HAS_TASK]->(t:Task)
            RETURN t { .* } AS t
            ORDER BY t.completed ASC, t.created_at ASC
            """,
            id=case_id,
        )
        return [dict(r["t"]) async for r in result]


# ── Full case bundle ──────────────────────────────────────────────────────────

async def get_case_full(graph_db, case_id: str) -> Optional[dict]:
    """Return case + subjects + notes + tasks in one call (avoids 4 round-trips)."""
    case = await get_case(graph_db, case_id)
    if not case:
        return None
    subjects, notes, tasks = await __import__("asyncio").gather(
        get_subjects(graph_db, case_id),
        get_notes(graph_db, case_id),
        get_tasks(graph_db, case_id),
    )
    return {
        "case":     case,
        "subjects": subjects,
        "notes":    notes,
        "tasks":    tasks,
        "stats": {
            "subject_count": len(subjects),
            "note_count":    len(notes),
            "task_total":    len(tasks),
            "tasks_pending": sum(1 for t in tasks if not t.get("completed")),
        },
    }


# ── Export ────────────────────────────────────────────────────────────────────

_NOTE_TYPE_ICONS = {
    "finding":    "🔍 FINDING",
    "lead":       "💡 LEAD",
    "hypothesis": "🧩 HYPOTHESIS",
    "caution":    "⚠ CAUTION",
    "general":    "📝 NOTE",
}

_LABEL_ICONS = {
    "Person": "👤", "Company": "🏢", "Email": "📧", "Domain": "🌐",
    "IP": "🔌", "Wallet": "💰", "Aircraft": "✈", "Breach": "🔓",
    "DarkWebMention": "🌑", "TelegramChannel": "📡",
}


def export_case_markdown(bundle: dict) -> str:
    case     = bundle["case"]
    subjects = bundle["subjects"]
    notes    = bundle["notes"]
    tasks    = bundle["tasks"]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = [
        f"# {case['title']}",
        "",
        f"**Status:** {case.get('status','—').title()} &nbsp;|&nbsp; "
        f"**Priority:** {case.get('priority','—').title()} &nbsp;|&nbsp; "
        f"**Created:** {str(case.get('created_at',''))[:10]}",
        "",
    ]

    if case.get("description"):
        lines += ["## Description", "", case["description"], ""]

    if subjects:
        lines.append("## Subjects")
        lines.append("")
        for s in subjects:
            icon  = _LABEL_ICONS.get(s["entity_label"], "•")
            role  = s.get("role", "unknown").title()
            lines.append(f"- {icon} **{s['display_name']}** ({s['entity_label']}, {role})")
        lines.append("")

    pinned = [n for n in notes if n.get("pinned")]
    regular = [n for n in notes if not n.get("pinned")]

    if notes:
        lines.append("## Notes")
        lines.append("")
        for n in pinned + regular:
            type_label = _NOTE_TYPE_ICONS.get(n.get("type","general"), "📝")
            ts = str(n.get("created_at",""))[:16].replace("T"," ")
            pin_mark = " 📌" if n.get("pinned") else ""
            lines.append(f"### {type_label}{pin_mark} — {ts}")
            lines.append("")
            lines.append(n.get("content",""))
            lines.append("")

    if tasks:
        lines.append("## Verification Tasks")
        lines.append("")
        for t in tasks:
            check = "x" if t.get("completed") else " "
            lines.append(f"- [{check}] {t['text']}")
        lines.append("")

    lines += [
        "---",
        f"*Exported from Fieldwork OSINT — {now}*",
    ]
    return "\n".join(lines)


# ── Phase 13: Case collaboration ──────────────────────────────────────────────

async def share_case(graph_db, case_id: str, share_with_username: str) -> dict:
    """
    Create a SHARED_WITH relationship from a Case to a User by username.
    Returns {"shared_with": username} or {"error": "..."}.
    """
    async with graph_db.driver.session() as session:
        rec = await (await session.run(
            "MATCH (u:User) WHERE toLower(u.username) = toLower($un) AND u.active = true "
            "RETURN u.id AS uid, u.username AS un LIMIT 1",
            un=share_with_username,
        )).single()
        if not rec:
            return {"error": f"User '{share_with_username}' not found"}
        await session.run(
            "MATCH (c:Case {id: $cid}), (u:User {id: $uid}) MERGE (c)-[:SHARED_WITH]->(u)",
            cid=case_id, uid=rec["uid"],
        )
        return {"shared_with": rec["un"]}


async def unshare_case(graph_db, case_id: str, user_id: str) -> bool:
    """Remove a SHARED_WITH relationship."""
    async with graph_db.driver.session() as session:
        rec = await (await session.run(
            "MATCH (c:Case {id: $cid})-[r:SHARED_WITH]->(u:User {id: $uid}) DELETE r RETURN count(r) AS n",
            cid=case_id, uid=user_id,
        )).single()
    return (rec["n"] if rec else 0) > 0


async def get_case_collaborators(graph_db, case_id: str) -> dict:
    """Return owner and shared-with users for a case."""
    async with graph_db.driver.session() as session:
        rec = await (await session.run(
            """
            MATCH (c:Case {id: $cid})
            OPTIONAL MATCH (c)-[:OWNED_BY]->(owner:User)
            OPTIONAL MATCH (c)-[:SHARED_WITH]->(shared:User)
            RETURN
                owner  {.id, .username, .role} AS owner,
                collect(shared {.id, .username, .role}) AS shared
            LIMIT 1
            """,
            cid=case_id,
        )).single()
    if not rec:
        return {"owner": None, "shared": []}
    return {
        "owner":  dict(rec["owner"]) if rec["owner"] else None,
        "shared": [dict(s) for s in (rec["shared"] or []) if s.get("id")],
    }
