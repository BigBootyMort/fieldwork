"""
Phase 11 — PDF Report Generation.

Renders a case bundle (subjects, notes, tasks) into a professional
PDF dossier using WeasyPrint.  The HTML is fully self-contained —
all CSS is embedded — so there are no external file dependencies.

Pipeline:
  bundle dict  →  build_pdf_html()  →  HTML string
                                     ↓
                              WeasyPrint.write_pdf()
                                     ↓
                              PDF bytes  →  HTTP response
"""

import datetime
import html as _html
import logging
from typing import Any

log = logging.getLogger("fieldwork.pdf_export")

# ── Markdown → HTML ───────────────────────────────────────────────────────────
try:
    import markdown as _md_lib

    def _md(text: str) -> str:
        return _md_lib.markdown(
            text,
            extensions=["tables", "fenced_code", "nl2br"],
        )

except ImportError:  # graceful degradation if package missing

    def _md(text: str) -> str:
        return _html.escape(text).replace("\n", "<br>")


# ── Tiny helpers ──────────────────────────────────────────────────────────────
def _e(s: Any) -> str:
    """HTML-escape any value."""
    return _html.escape(str(s) if s is not None else "")


def _dt(dt_str: str | None, short: bool = False) -> str:
    """Parse an ISO timestamp and return a readable string."""
    if not dt_str:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y") if short else dt.strftime("%d %b %Y  %H:%M UTC")
    except Exception:
        return str(dt_str)


_NOTE_ICONS = {
    "finding": "🔍", "lead": "💡",
    "hypothesis": "🧩", "caution": "⚠",
    "general": "📝",
}

_LABEL_ICONS = {
    "Person": "👤", "Company": "🏢", "Email": "📧",
    "Domain": "🌐", "IP": "🔌", "Username": "🆔",
    "Account": "📱", "Wallet": "💰", "Breach": "🔓",
    "Leak": "💧", "Aircraft": "✈", "Flight": "🛫",
    "TelegramChannel": "📡", "TelegramPost": "💬",
    "Article": "📰", "Location": "📍",
    "DarkWebMention": "🌑",
}


# ── Embedded CSS ──────────────────────────────────────────────────────────────
_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }

@page {
    size: A4;
    margin: 19mm 18mm 22mm 18mm;
    @top-right {
        content: "FIELDWORK  ·  CONFIDENTIAL";
        font-family: 'Segoe UI', Arial, sans-serif;
        font-size: 7.5pt;
        color: #aaa;
        letter-spacing: 0.08em;
    }
    @bottom-center {
        content: "Page " counter(page) " of " counter(pages);
        font-family: 'Segoe UI', Arial, sans-serif;
        font-size: 7.5pt;
        color: #bbb;
    }
}

body {
    font-family: 'Segoe UI', Arial, Helvetica, sans-serif;
    font-size: 10pt;
    line-height: 1.58;
    color: #111827;
    background: #fff;
}

/* ── Report header ── */
.rpt-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    padding-bottom: 11pt;
    border-bottom: 2.5pt solid #c1440e;
    margin-bottom: 16pt;
}
.rpt-logo {
    font-size: 22pt;
    font-weight: 900;
    letter-spacing: -0.03em;
    color: #c1440e;
    line-height: 1;
}
.rpt-logo span { color: #111827; }
.rpt-sub { font-size: 7.5pt; color: #888; margin-top: 3pt; letter-spacing: 0.06em; }
.rpt-meta { text-align: right; font-size: 8pt; color: #555; line-height: 1.75; }

/* ── Case title block ── */
.case-title { font-size: 17pt; font-weight: 800; color: #111827; line-height: 1.2; margin-bottom: 5pt; }
.case-desc  { font-size: 9.5pt; color: #4b5563; margin-bottom: 8pt; }

/* ── Badge ── */
.badge {
    display: inline-block;
    padding: 2pt 7pt;
    border-radius: 20pt;
    font-size: 7.5pt;
    font-weight: 700;
    letter-spacing: 0.04em;
    margin-right: 4pt;
    text-transform: uppercase;
}
.b-open     { background:#dbeafe; color:#1e40af }
.b-active   { background:#d1fae5; color:#065f46 }
.b-review   { background:#fef3c7; color:#92400e }
.b-closed   { background:#f3f4f6; color:#374151 }
.b-archived { background:#f3f4f6; color:#9ca3af }
.b-high     { background:#fee2e2; color:#991b1b }
.b-medium   { background:#fef3c7; color:#92400e }
.b-low      { background:#f3f4f6; color:#6b7280 }

/* ── Stats row ── */
.stats-row {
    display: flex;
    gap: 10pt;
    margin: 14pt 0 18pt;
    flex-wrap: wrap;
}
.stat-box {
    flex: 1;
    min-width: 64pt;
    background: #f9fafb;
    border: 1pt solid #e5e7eb;
    border-radius: 6pt;
    padding: 7pt 9pt;
    text-align: center;
}
.stat-n { font-size: 17pt; font-weight: 800; color: #c1440e; line-height: 1; }
.stat-l { font-size: 7pt; color: #9ca3af; margin-top: 2pt; text-transform: uppercase; letter-spacing: 0.06em; }

/* ── Section ── */
.section { margin-bottom: 18pt; }
.section-h {
    font-size: 11pt;
    font-weight: 800;
    color: #c1440e;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border-bottom: 1pt solid #e5e7eb;
    padding-bottom: 4pt;
    margin-bottom: 9pt;
}

/* ── Table ── */
table { width: 100%; border-collapse: collapse; font-size: 9pt; }
th {
    background: #f3f4f6;
    text-align: left;
    padding: 5pt 8pt;
    font-weight: 700;
    border: 1pt solid #e5e7eb;
    font-size: 7.5pt;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #374151;
}
td { padding: 5pt 8pt; border: 1pt solid #e5e7eb; vertical-align: top; word-break: break-word; }
tr:nth-child(even) td { background: #f9fafb; }

/* ── Note card ── */
.note {
    border: 1pt solid #e5e7eb;
    border-radius: 5pt;
    padding: 8pt 10pt;
    margin-bottom: 8pt;
    page-break-inside: avoid;
}
.note.pinned { border-left: 3pt solid #c1440e; }
.note-hd {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4pt;
}
.note-type { font-size: 7.5pt; font-weight: 700; color: #c1440e; text-transform: uppercase; letter-spacing: 0.04em; }
.note-time { font-size: 7pt; color: #9ca3af; }
.note-body { font-size: 9.5pt; color: #111827; }
.note-body p { margin-bottom: 3pt; }
.note-body code { background:#f3f4f6; padding:1pt 3pt; border-radius:3pt; font-size:8.5pt; }
.note-body pre  { background:#f3f4f6; padding:5pt 8pt; border-radius:4pt; margin:4pt 0; overflow:hidden; }
.note-body ul, .note-body ol { margin-left: 14pt; margin-bottom: 3pt; }
.note-body table { margin: 5pt 0; font-size: 8.5pt; }

/* ── Task list ── */
.task {
    display: flex;
    align-items: flex-start;
    gap: 7pt;
    padding: 5pt 0;
    border-bottom: 1pt solid #f3f4f6;
}
.task:last-child { border-bottom: none; }
.chk {
    width: 10pt; height: 10pt; min-width: 10pt;
    border: 1.5pt solid #9ca3af;
    border-radius: 2pt;
    margin-top: 2pt;
    display: flex; align-items: center; justify-content: center;
    font-size: 8pt;
}
.chk.done { background: #065f46; border-color: #065f46; color: #fff; }
.task-txt      { font-size: 9.5pt; flex: 1; }
.task-txt.done { text-decoration: line-through; color: #9ca3af; }

/* ── Footer ── */
.rpt-footer {
    margin-top: 22pt;
    padding-top: 7pt;
    border-top: 1pt solid #e5e7eb;
    font-size: 7.5pt;
    color: #bbb;
    text-align: center;
}
"""


# ── HTML builder ──────────────────────────────────────────────────────────────

def build_pdf_html(bundle: dict) -> str:
    """Convert a case bundle dict into a self-contained HTML string."""
    case     = bundle.get("case", {})
    subjects = bundle.get("subjects", [])
    notes    = sorted(
        bundle.get("notes", []),
        key=lambda n: (not n.get("pinned"), n.get("created_at", "")),
    )
    tasks = sorted(
        bundle.get("tasks", []),
        key=lambda t: (t.get("completed", False), t.get("created_at", "")),
    )

    now_str  = datetime.datetime.utcnow().strftime("%d %b %Y  %H:%M UTC")
    pending  = sum(1 for t in tasks if not t.get("completed"))
    cid      = _e(case.get("id", "—"))
    ctitle   = _e(case.get("title", "Untitled Case"))

    # ── Header ──────────────────────────────────────────────────────────────
    header = f"""
<div class="rpt-header">
  <div>
    <div class="rpt-logo">FIELD<span>WORK</span></div>
    <div class="rpt-sub">OSINT INVESTIGATIVE PLATFORM</div>
  </div>
  <div class="rpt-meta">
    Case ID: {cid}<br>
    Generated: {now_str}<br>
    <strong>CONFIDENTIAL — INVESTIGATIVE MATERIAL</strong>
  </div>
</div>"""

    # ── Title block ──────────────────────────────────────────────────────────
    status   = case.get("status", "open")
    priority = case.get("priority", "medium")
    desc_html = f'<p class="case-desc">{_e(case.get("description",""))}</p>' if case.get("description") else ""
    title_block = f"""
<div style="margin-bottom:14pt">
  <div class="case-title">{ctitle}</div>
  {desc_html}
  <span class="badge b-{_e(status)}">{_e(status)}</span>
  <span class="badge b-{_e(priority)}">{_e(priority)} priority</span>
</div>"""

    # ── Stats row ────────────────────────────────────────────────────────────
    opened = _dt(case.get("created_at"), short=True)
    stats = f"""
<div class="stats-row">
  <div class="stat-box"><div class="stat-n">{len(subjects)}</div><div class="stat-l">Subjects</div></div>
  <div class="stat-box"><div class="stat-n">{len(notes)}</div><div class="stat-l">Notes</div></div>
  <div class="stat-box"><div class="stat-n">{len(tasks)}</div><div class="stat-l">Tasks</div></div>
  <div class="stat-box"><div class="stat-n">{pending}</div><div class="stat-l">Pending</div></div>
  <div class="stat-box"><div class="stat-n" style="font-size:10pt;padding-top:3pt">{opened}</div><div class="stat-l">Opened</div></div>
</div>"""

    # ── Subjects ─────────────────────────────────────────────────────────────
    if subjects:
        rows = "".join(
            f"""<tr>
              <td>{_LABEL_ICONS.get(s.get('label',''), '●')} {_e(s.get('label',''))}</td>
              <td><strong>{_e(s.get('display_name') or s.get('entity_id',''))}</strong></td>
              <td>{_e(s.get('role',''))}</td>
            </tr>"""
            for s in subjects
        )
        subj_sec = f"""
<div class="section">
  <div class="section-h">Subjects ({len(subjects)})</div>
  <table>
    <thead><tr><th>Type</th><th>Identity</th><th>Role</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""
    else:
        subj_sec = ""

    # ── Notes ────────────────────────────────────────────────────────────────
    if notes:
        cards = ""
        for n in notes:
            pinned_cls = " pinned" if n.get("pinned") else ""
            ntype      = n.get("type", "general")
            icon       = _NOTE_ICONS.get(ntype, "📝")
            pin_flag   = "  📌" if n.get("pinned") else ""
            body_html  = _md(n.get("body", ""))
            cards += f"""
<div class="note{pinned_cls}">
  <div class="note-hd">
    <span class="note-type">{icon} {_e(ntype.title())}{pin_flag}</span>
    <span class="note-time">{_dt(n.get('created_at'))}</span>
  </div>
  <div class="note-body">{body_html}</div>
</div>"""
        notes_sec = f"""
<div class="section">
  <div class="section-h">Notes ({len(notes)})</div>
  {cards}
</div>"""
    else:
        notes_sec = ""

    # ── Tasks ────────────────────────────────────────────────────────────────
    if tasks:
        items = ""
        for t in tasks:
            done = t.get("completed", False)
            chk  = '<div class="chk done">✓</div>' if done else '<div class="chk"></div>'
            cls  = "task-txt done" if done else "task-txt"
            items += f"""
<div class="task">
  {chk}
  <span class="{cls}">{_e(t.get('text',''))}</span>
</div>"""
        tasks_sec = f"""
<div class="section">
  <div class="section-h">Tasks ({len(tasks)} total · {pending} pending)</div>
  {items}
</div>"""
    else:
        tasks_sec = ""

    # ── Footer ───────────────────────────────────────────────────────────────
    footer = f"""
<div class="rpt-footer">
  Generated by Fieldwork OSINT Platform &nbsp;·&nbsp; {now_str}
  &nbsp;·&nbsp; Handle with care — investigative material
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Fieldwork — {ctitle}</title>
<style>{_CSS}</style>
</head>
<body>
{header}
{title_block}
{stats}
{subj_sec}
{notes_sec}
{tasks_sec}
{footer}
</body>
</html>"""


# ── Public entrypoint ─────────────────────────────────────────────────────────

def export_case_pdf(bundle: dict) -> bytes:
    """
    Render a case bundle to PDF bytes.
    Raises RuntimeError if WeasyPrint is unavailable or rendering fails.
    """
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError(
            "WeasyPrint is not installed — rebuild the Docker image to enable PDF export"
        ) from exc

    try:
        html_str = build_pdf_html(bundle)
        return HTML(string=html_str).write_pdf()
    except Exception as exc:
        log.error("PDF generation failed: %s", exc, exc_info=True)
        raise RuntimeError(f"PDF generation error: {exc}") from exc
