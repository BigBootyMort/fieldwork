"""
AbuseIPDB — IP reputation lookup via the AbuseIPDB public API.

Free plan: 1 000 checks / day.  Sign up at https://www.abuseipdb.com/register
Set ABUSEIPDB_KEY in .env to enable.

Response data stored on the IP node:
  abuse_confidence  — 0-100 score (100 = definitely malicious)
  abuse_reports     — total number of reports in the past N days
  abuse_last_seen   — ISO timestamp of most recent report
  abuse_categories  — list of human-readable category names
  abuse_isp         — ISP from AbuseIPDB enrichment
  abuse_usage_type  — data-center / isp / residential / etc.
  abuse_domain      — reverse-DNS hostname

Risk scoring automatically picks up abuse_confidence as a signal.
"""
import os
import logging
import httpx

log = logging.getLogger("fieldwork.abuseipdb")

_API_KEY = os.getenv("ABUSEIPDB_KEY", "")
_BASE    = "https://api.abuseipdb.com/api/v2"

# AbuseIPDB category IDs → human-readable names
_CATEGORIES = {
    1:  "DNS Compromise",
    2:  "DNS Poisoning",
    3:  "Fraud Orders",
    4:  "DDoS Attack",
    5:  "FTP Brute-Force",
    6:  "Ping of Death",
    7:  "Phishing",
    8:  "Fraud VoIP",
    9:  "Open Proxy",
    10: "Web Spam",
    11: "Email Spam",
    12: "Blog Spam",
    13: "VPN IP",
    14: "Port Scan",
    15: "Hacking",
    16: "SQL Injection",
    17: "Spoofing",
    18: "Brute Force",
    19: "Bad Bot",
    20: "Exploited Host",
    21: "Web App Attack",
    22: "SSH Abuse",
    23: "IoT Targeted",
}


async def check_ip(graph_db, ip: str, max_age_days: int = 90) -> dict:
    """
    Check an IP address against AbuseIPDB.
    Requires ABUSEIPDB_KEY environment variable.
    Updates the IP node in Neo4j with abuse metadata.
    Returns the full enrichment dict.
    """
    if not _API_KEY:
        return {
            "ip":    ip,
            "error": "ABUSEIPDB_KEY not configured — add it to .env",
            "skipped": True,
        }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                f"{_BASE}/check",
                params={
                    "ipAddress":    ip,
                    "maxAgeInDays": max_age_days,
                    "verbose":      "",
                },
                headers={
                    "Key":    _API_KEY,
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:
            return {"ip": ip, "error": "IP not found or invalid format"}
        if e.response.status_code == 429:
            return {"ip": ip, "error": "AbuseIPDB rate limit reached (1 000/day on free plan)"}
        raise
    except Exception as exc:
        log.warning("AbuseIPDB lookup failed for %s: %s", ip, exc)
        return {"ip": ip, "error": str(exc)}

    confidence   = int(data.get("abuseConfidenceScore",  0))
    total_reports= int(data.get("totalReports",          0))
    last_seen    = data.get("lastReportedAt") or None
    isp          = data.get("isp")            or None
    usage_type   = data.get("usageType")      or None
    domain       = data.get("domain")         or None
    country_code = data.get("countryCode")    or None
    is_tor       = bool(data.get("isTor", False))
    is_public    = bool(data.get("isPublic", True))

    # Category IDs → names
    raw_cats = data.get("reports", [])
    seen_cats: set[int] = set()
    for rep in raw_cats:
        for cid in (rep.get("categories") or []):
            seen_cats.add(int(cid))
    category_names = [_CATEGORIES.get(c, f"Category {c}") for c in sorted(seen_cats)]

    result = {
        "ip":               ip,
        "confidence":       confidence,
        "total_reports":    total_reports,
        "last_seen":        last_seen,
        "categories":       category_names,
        "isp":              isp,
        "usage_type":       usage_type,
        "domain":           domain,
        "country_code":     country_code,
        "is_tor":           is_tor,
        "is_public":        is_public,
        "verdict":          _verdict(confidence),
    }

    # ── Persist to IP node ───────────────────────────────────────────────────
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MERGE (n:IP {id: $ip})
            ON CREATE SET n.address = $ip, n.source = 'abuseipdb', n.created_at = datetime()
            SET n.abuse_confidence  = $conf,
                n.abuse_reports     = $rpts,
                n.abuse_last_seen   = $last,
                n.abuse_categories  = $cats,
                n.abuse_isp         = $isp,
                n.abuse_usage_type  = $usage,
                n.abuse_domain      = $domain,
                n.is_tor            = $tor,
                n.updated_at        = datetime()
            """,
            ip=ip, conf=confidence, rpts=total_reports,
            last=last_seen or "", cats=category_names,
            isp=isp or "", usage=usage_type or "",
            domain=domain or "", tor=is_tor,
        )

        # Also update risk_score signals if confidence is meaningful
        if confidence >= 25:
            # Let risk_scoring pick this up naturally via abuse_confidence property
            # We don't call score_entity here to avoid circular imports
            log.info("AbuseIPDB: %s confidence=%d — flagged for risk rescoring", ip, confidence)

    return result


def _verdict(score: int) -> str:
    if score >= 75:  return "malicious"
    if score >= 25:  return "suspicious"
    if score > 0:    return "low_risk"
    return "clean"
