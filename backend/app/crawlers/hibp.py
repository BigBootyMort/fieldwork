"""
HaveIBeenPwned breach check — Phase 6.4

Two modes:

  check_email_hibp(graph_db, email)
      Requires HIBP_API_KEY ($3.50/month at haveibeenpwned.com/API/Key).
      Returns breaches for a specific email address.
      Creates Breach nodes + links to the Email node.

  check_domain_hibp(graph_db, domain)
      FREE — no key required.
      Scans HIBP's full breach list for entries whose Domain field matches.
      Tells you if the domain has ever appeared in a known data breach.
      Does NOT return individual email addresses (HIBP protects those).

Both are on-demand enrichment endpoints; not run in auto-crawl.
"""
import logging
import os

import httpx

log = logging.getLogger("crawler.hibp")

_HIBP_BASE   = "https://haveibeenpwned.com/api/v3"
_HIBP_KEY    = os.getenv("HIBP_API_KEY", "")
_UA          = "Fieldwork-OSINT/0.4"  # HIBP requires a user-agent


# ── Email check (requires paid key) ──────────────────────────────────────────

async def check_email_hibp(graph_db, email: str) -> dict:
    """
    Check a specific email against HIBP's breached-account database.
    Stores Breach nodes and links them to the Email node in Neo4j.
    Requires HIBP_API_KEY.
    """
    if not _HIBP_KEY:
        return {
            "found": False,
            "email": email,
            "reason": "HIBP_API_KEY not set. Get a key at haveibeenpwned.com/API/Key (~$3.50/month).",
        }

    log.info("HIBP email check: %s", email)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{_HIBP_BASE}/breachedaccount/{email}",
                headers={"hibp-api-key": _HIBP_KEY, "User-Agent": _UA},
                params={"truncateResponse": "false"},
            )

        if r.status_code == 404:
            return {"found": True, "email": email, "pwned": False, "breaches": []}
        if r.status_code == 401:
            return {"found": False, "email": email, "reason": "HIBP API key invalid or expired"}
        if r.status_code == 429:
            return {"found": False, "email": email, "reason": "HIBP rate limit hit — wait 1.5 s between requests"}
        if r.status_code != 200:
            return {"found": False, "email": email, "reason": f"HIBP HTTP {r.status_code}"}

        breaches = r.json()
    except Exception as exc:
        log.warning("HIBP email check failed: %s", exc)
        return {"found": False, "email": email, "reason": str(exc)}

    # Write Breach nodes + link to Email
    stored = 0
    async with graph_db.driver.session() as session:
        # Ensure Email node exists
        await session.run(
            "MERGE (e:Email {id: $id}) "
            "ON CREATE SET e.address = $id, e.first_seen = datetime()",
            id=email.lower(),
        )
        for b in breaches:
            bid = f"breach:{b['Name'].lower()}"
            await session.run(
                "MERGE (br:Breach {id: $id}) "
                "ON CREATE SET "
                "  br.name        = $name, "
                "  br.domain      = $domain, "
                "  br.breach_date = $date, "
                "  br.data_classes = $classes, "
                "  br.pwn_count   = $count, "
                "  br.source      = 'hibp', "
                "  br.first_seen  = datetime() "
                "WITH br "
                "MATCH (e:Email {id: $email}) "
                "MERGE (e)-[:APPEARED_IN]->(br)",
                id=bid,
                name=b.get("Name", ""),
                domain=b.get("Domain", ""),
                date=b.get("BreachDate", ""),
                classes=b.get("DataClasses", []),
                count=b.get("PwnCount", 0),
                email=email.lower(),
            )
            stored += 1

    log.info("HIBP: %s — %d breaches found", email, len(breaches))
    return {
        "found":    True,
        "email":    email,
        "pwned":    len(breaches) > 0,
        "count":    len(breaches),
        "stored":   stored,
        "breaches": [_summarise_breach(b) for b in breaches],
    }


# ── Domain check (free, no key) ───────────────────────────────────────────────

async def check_domain_hibp(graph_db, domain: str) -> dict:
    """
    Check whether a domain appears in any HIBP breach record.
    Uses the public /api/v3/breaches endpoint (no key required).
    Does NOT reveal individual email addresses.
    """
    log.info("HIBP domain check: %s", domain)
    domain_lower = domain.lower().strip()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{_HIBP_BASE}/breaches",
                headers={"User-Agent": _UA},
            )
        if r.status_code != 200:
            return {"found": False, "domain": domain, "reason": f"HIBP HTTP {r.status_code}"}

        all_breaches = r.json()
    except Exception as exc:
        log.warning("HIBP domain fetch failed: %s", exc)
        return {"found": False, "domain": domain, "reason": str(exc)}

    # Filter to breaches whose Domain field matches
    matching = [
        b for b in all_breaches
        if (b.get("Domain") or "").lower() == domain_lower
    ]

    # Also surface breaches with this domain in their name (looser match)
    related = [
        b for b in all_breaches
        if domain_lower in (b.get("Domain") or "").lower()
        and b not in matching
    ]

    # Write matching Breach nodes and link to Domain
    if matching or related:
        async with graph_db.driver.session() as session:
            # Ensure Domain node exists
            await session.run(
                "MERGE (d:Domain {id: $id}) "
                "ON CREATE SET d.name = $id, d.first_seen = datetime()",
                id=domain_lower,
            )
            for b in (matching + related)[:20]:
                bid = f"breach:{b['Name'].lower()}"
                await session.run(
                    "MERGE (br:Breach {id: $id}) "
                    "ON CREATE SET "
                    "  br.name        = $name, "
                    "  br.domain      = $domain, "
                    "  br.breach_date = $date, "
                    "  br.data_classes = $classes, "
                    "  br.pwn_count   = $count, "
                    "  br.source      = 'hibp', "
                    "  br.first_seen  = datetime() "
                    "WITH br "
                    "MATCH (d:Domain {id: $did}) "
                    "MERGE (d)-[:APPEARED_IN]->(br)",
                    id=bid,
                    name=b.get("Name", ""),
                    domain=b.get("Domain", ""),
                    date=b.get("BreachDate", ""),
                    classes=b.get("DataClasses", []),
                    count=b.get("PwnCount", 0),
                    did=domain_lower,
                )

    log.info("HIBP domain %s: %d exact matches, %d related", domain, len(matching), len(related))
    return {
        "found":         True,
        "domain":        domain,
        "exact_matches": len(matching),
        "related":       len(related),
        "breaches":      [_summarise_breach(b) for b in matching],
        "related_breaches": [_summarise_breach(b) for b in related[:5]],
    }


def _summarise_breach(b: dict) -> dict:
    return {
        "name":         b.get("Name", ""),
        "domain":       b.get("Domain", ""),
        "breach_date":  b.get("BreachDate", ""),
        "pwn_count":    b.get("PwnCount", 0),
        "data_classes": b.get("DataClasses", []),
        "description":  _strip_html(b.get("Description", ""))[:300],
        "is_verified":  b.get("IsVerified", False),
        "is_sensitive": b.get("IsSensitive", False),
    }


def _strip_html(text: str) -> str:
    """Minimal HTML tag stripper for breach descriptions."""
    import re
    return re.sub(r"<[^>]+>", "", text or "").strip()
