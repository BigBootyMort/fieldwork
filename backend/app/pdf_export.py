"""
Phase 11 — PDF Report Generation.
Phase 30 — Professional commercial-grade OSINT intelligence reports.

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
import json
import logging
from typing import Any
from uuid import uuid4

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


# ── Professional intelligence report ─────────────────────────────────────────

_SANCTIONS_TYPES = {"sanctions", "sanction", "pep"}
_COURT_TYPES = {"court", "case", "lawsuit", "legal"}
_ADVERSE_TYPES = {"adverse", "media", "news", "article"}

_PRO_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }

@page cover {
    size: A4;
    margin: 0;
}
@page body {
    size: A4;
    margin: 18mm 17mm 22mm 17mm;
    @top-right {
        content: "FIELDWORK INTELLIGENCE  ·  CONFIDENTIAL";
        font-family: Arial, sans-serif;
        font-size: 7pt;
        color: #999;
        letter-spacing: 0.08em;
    }
    @bottom-center {
        content: "Page " counter(page) " of " counter(pages);
        font-family: Arial, sans-serif;
        font-size: 7pt;
        color: #bbb;
    }
}

body { font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #111; background: #fff; }

.cover {
    page: cover;
    background: #0c0c1e;
    width: 210mm;
    height: 297mm;
    padding: 30mm 20mm;
    color: #fff;
    page-break-after: always;
}
.cover-logo {
    font-size: 14pt;
    font-weight: 900;
    letter-spacing: 0.15em;
    color: #f5e642;
    margin-bottom: 5mm;
}
.cover-divider { border: none; border-top: 1.5pt solid #f5e642; margin: 8mm 0; }
.cover-heading { font-size: 28pt; font-weight: 900; letter-spacing: 0.04em; color: #fff; line-height: 1.15; margin-bottom: 8mm; }
.cover-subject { font-size: 20pt; font-weight: 700; color: #f5e642; margin-bottom: 10mm; }
.cover-meta { font-size: 9pt; color: #ccc; line-height: 2.2; }
.cover-class { margin-top: 20mm; font-size: 8pt; color: #f5e642; letter-spacing: 0.12em; font-weight: 700; }

.body-page { page: body; }

.section { margin-bottom: 16pt; page-break-inside: avoid; }
.section-h {
    font-size: 10.5pt; font-weight: 800; color: #0c0c1e;
    text-transform: uppercase; letter-spacing: 0.08em;
    border-bottom: 2pt solid #f5e642;
    padding-bottom: 4pt; margin-bottom: 9pt;
}

table { width: 100%; border-collapse: collapse; font-size: 9pt; }
th { background: #0c0c1e; color: #f5e642; text-align: left; padding: 5pt 8pt; font-size: 8pt; text-transform: uppercase; letter-spacing: 0.05em; }
td { padding: 5pt 8pt; border: 1pt solid #e5e7eb; vertical-align: top; word-break: break-word; }
tr:nth-child(even) td { background: #f9fafb; }

.badge { display: inline-block; padding: 1.5pt 6pt; border-radius: 20pt; font-size: 7pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
.b-high { background: #fee2e2; color: #991b1b; }
.b-medium { background: #fef3c7; color: #92400e; }
.b-low { background: #f3f4f6; color: #6b7280; }
.b-critical { background: #7f1d1d; color: #fff; }

.risk-block { padding: 10pt 14pt; border-radius: 6pt; margin-bottom: 12pt; }
.risk-low { background: #d1fae5; border: 1.5pt solid #059669; }
.risk-medium { background: #fef3c7; border: 1.5pt solid #d97706; }
.risk-high { background: #fee2e2; border: 1.5pt solid #dc2626; }
.risk-critical { background: #7f1d1d; color: #fff; border: 1.5pt solid #7f1d1d; }

.none-msg { color: #9ca3af; font-style: italic; font-size: 9pt; }
.exec-summary { font-size: 10pt; line-height: 1.65; color: #1f2937; }

.disclaimer { font-size: 8pt; color: #6b7280; line-height: 1.6; border-top: 1pt solid #e5e7eb; padding-top: 8pt; margin-top: 12pt; }
"""


def _risk_calc(findings: list[dict]) -> tuple[str, int]:
    score = 0
    adverse_count = 0
    for f in findings:
        ftype = (f.get("type") or "").lower()
        if any(t in ftype for t in ("sanction", "pep")):
            score += 40
        elif any(t in ftype for t in ("court", "case")):
            score += 25
        elif any(t in ftype for t in ("adverse", "media", "article")):
            adverse_count += 1
            score += 20
    score = min(score, 100)
    if score >= 75 or (score >= 40 and adverse_count >= 3):
        level = "CRITICAL"
    elif score >= 40:
        level = "HIGH"
    elif score >= 20:
        level = "MEDIUM"
    else:
        level = "LOW"
    return level, score


def _findings_section(title: str, items: list[dict]) -> str:
    if not items:
        return f"""
<div class="section">
  <div class="section-h">{_e(title)}</div>
  <div class="none-msg">No matches found.</div>
</div>"""
    rows = ""
    for item in items:
        conf = (item.get("confidence") or "LOW").upper()
        conf_cls = {"HIGH": "b-high", "MEDIUM": "b-medium"}.get(conf, "b-low")
        rows += f"""<tr>
          <td><span class="badge {conf_cls}">{_e(conf)}</span></td>
          <td>{_e(item.get('value',''))}</td>
          <td>{_e(item.get('source',''))}</td>
          <td>{_e(item.get('timestamp',''))}</td>
        </tr>"""
    return f"""
<div class="section">
  <div class="section-h">{_e(title)}</div>
  <table>
    <thead><tr><th>Confidence</th><th>Value</th><th>Source</th><th>Timestamp</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


async def _ollama_summary(name: str, findings: list[dict], ollama_url: str, model: str) -> str:
    import httpx as _httpx
    prompt = (
        f"You are writing a professional intelligence report. "
        f"Summarise these findings about {name} in 3-4 sentences for an executive audience. "
        "Be factual, neutral, and highlight the most significant findings. "
        f"Findings: {json.dumps(findings[:20])}"
    )
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{ollama_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
        return resp.json().get("response", "").strip() or "No summary available."
    except Exception as exc:
        log.warning("Ollama summary failed: %s", exc)
        return "Summary unavailable — Ollama connection error."


async def generate_professional_report(
    person_data: dict,
    findings: list[dict],
    options: dict = None,
    ollama_url: str = "http://ollama:11434",
    model: str = "llama3.2",
) -> bytes:
    """Generate a professional commercial-grade OSINT intelligence report as PDF."""
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError("WeasyPrint not available") from exc

    options = options or {}
    name = (person_data.get("name") or person_data.get("display_name") or "Unknown Subject")
    case_ref = options.get("case_ref") or f"FW-{uuid4().hex[:8].upper()}"
    client_name = options.get("client_name") or "CONFIDENTIAL"
    today = datetime.date.today().strftime("%d %B %Y")
    redact_sources = bool(options.get("redact_sources"))

    risk_level, risk_score = _risk_calc(findings)
    risk_css = {"CRITICAL": "risk-critical", "HIGH": "risk-high", "MEDIUM": "risk-medium"}.get(risk_level, "risk-low")

    summary_text = await _ollama_summary(name, findings, ollama_url, model)

    def _filter(types_kw: list[str]) -> list[dict]:
        return [f for f in findings if any(k in (f.get("type") or "").lower() for k in types_kw)]

    identity_items = _filter(["email", "phone", "username", "account"])
    sanctions_items = _filter(["sanction", "pep"])
    adverse_items = _filter(["adverse", "article", "media", "news"])
    court_items = _filter(["court", "case", "legal", "lawsuit"])
    corporate_items = _filter(["company", "domain", "corporate"])
    network_items = _filter(["ip", "infrastructure", "host"])

    # Cover page
    cover = f"""
<div class="cover">
  <div class="cover-logo">FIELDWORK</div>
  <hr class="cover-divider">
  <div class="cover-heading">INTELLIGENCE<br>REPORT</div>
  <div class="cover-subject">{_e(name)}</div>
  <hr class="cover-divider">
  <div class="cover-meta">
    Case Reference: <strong>{_e(case_ref)}</strong><br>
    Client: <strong>{_e(client_name)}</strong><br>
    Date: <strong>{_e(today)}</strong><br>
    Classification: <strong>CONFIDENTIAL</strong>
  </div>
  <div class="cover-class">CONFIDENTIAL — FOR AUTHORISED RECIPIENTS ONLY</div>
</div>"""

    # Body sections
    sources_section = ""
    if not redact_sources:
        src_rows = "".join(
            f"<tr><td>{_e(f.get('source',''))}</td><td>{_e(f.get('type',''))}</td><td>{_e(f.get('timestamp',''))}</td></tr>"
            for f in findings
        )
        sources_section = f"""
<div class="section">
  <div class="section-h">Sources &amp; Methodology</div>
  <table>
    <thead><tr><th>Source</th><th>Data Type</th><th>Timestamp</th></tr></thead>
    <tbody>{src_rows}</tbody>
  </table>
</div>"""

    html_body = f"""
<div class="body-page">
  <div class="section">
    <div class="section-h">Executive Summary</div>
    <p class="exec-summary">{_e(summary_text)}</p>
  </div>

  <div class="section">
    <div class="section-h">Risk Assessment</div>
    <div class="risk-block {risk_css}">
      <strong>Overall Risk Level: {_e(risk_level)}</strong> &nbsp;·&nbsp; Risk Score: {risk_score}/100
    </div>
  </div>

  {_findings_section("Identity &amp; Contact", identity_items)}
  {_findings_section("Sanctions &amp; Compliance", sanctions_items)}
  {_findings_section("Adverse Media", adverse_items)}
  {_findings_section("Court Records", court_items)}
  {_findings_section("Corporate Intelligence", corporate_items)}
  {_findings_section("Network Intelligence", network_items)}

  {sources_section}

  <div class="section">
    <div class="section-h">Legal Disclaimer</div>
    <p class="disclaimer">
      This report is produced for informational purposes only. Fieldwork OSINT makes no warranty
      as to the accuracy or completeness of the information contained herein. This report does not
      constitute legal advice. All data is collected from publicly available sources. Recipients must
      ensure compliance with applicable data protection legislation including GDPR and the Data
      Protection Act 2018 before acting on this information.
    </p>
  </div>
</div>"""

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Fieldwork Intelligence Report — {_e(name)}</title>
<style>{_PRO_CSS}</style>
</head>
<body>
{cover}
{html_body}
</body>
</html>"""

    try:
        return HTML(string=full_html).write_pdf()
    except Exception as exc:
        log.error("Professional PDF generation failed: %s", exc, exc_info=True)
        raise RuntimeError(f"PDF generation error: {exc}") from exc
