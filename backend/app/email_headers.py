"""
Email Header Analyzer — forensic parsing of raw email headers.

Extracts:
  - Received hop chain with timestamps and inter-hop delays
  - Originating IP (X-Originating-IP or first public hop)
  - SPF / DKIM / DMARC authentication results
  - Key envelope metadata (From, To, Subject, Date, Message-ID)
  - Suspicious anomalies (negative delays, auth failures, private IPs in wrong position)
  - All public IPs and relay hostnames for graph ingestion

On analyze_and_store() the public IPs and relay domains are MERGEd into Neo4j
as IP / Domain nodes so they appear in the graph immediately.
"""
import re
import logging
import ipaddress
from email.utils import parsedate_to_datetime
from datetime import datetime

log = logging.getLogger("fieldwork.email_headers")

# ── Regex constants ────────────────────────────────────────────────────────────

_IP4_RE  = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b')
_IP6_RE  = re.compile(r'\[([0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){2,7})\]')

# "from HOST (FQDN [IP])" — very permissive so we can match most MTAs
_FROM_RE = re.compile(r'from\s+(\S+)(?:\s+\(([^)]*)\))?', re.IGNORECASE)
_BY_RE   = re.compile(r'\bby\s+(\S+)',                      re.IGNORECASE)
_DATE_RE = re.compile(r';\s*(.+?)(?:\r?\n|$)',              re.IGNORECASE | re.DOTALL)

_SPF_RE  = re.compile(r'\bspf=(\S+)',  re.IGNORECASE)
_DKIM_RE = re.compile(r'\bdkim=(\S+)', re.IGNORECASE)
_DMARC_RE= re.compile(r'\bdmarc=(\S+)',re.IGNORECASE)

# Match a complete header value (may be folded across multiple lines)
def _header_value(raw: str, name: str) -> str | None:
    pat = re.compile(
        rf'^{re.escape(name)}:\s*(.+?)(?=\r?\n(?![ \t])|\Z)',
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    m = pat.search(raw)
    if not m:
        return None
    # Unfold: replace newline + leading whitespace with a single space
    return re.sub(r'\r?\n[ \t]+', ' ', m.group(1)).strip()


def _all_header_values(raw: str, name: str) -> list[str]:
    pat = re.compile(
        rf'^{re.escape(name)}:\s*(.+?)(?=\r?\n(?![ \t])|\Z)',
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    return [re.sub(r'\r?\n[ \t]+', ' ', m.group(1)).strip()
            for m in pat.finditer(raw)]


def _is_public(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return not (addr.is_private or addr.is_loopback or
                    addr.is_link_local or addr.is_reserved or
                    addr.is_multicast or addr.is_unspecified)
    except ValueError:
        return False


def _ips_from_text(text: str) -> list[str]:
    """Extract all IPv4 (and bracketed IPv6) addresses from a string."""
    hits: list[str] = []
    for m in _IP4_RE.finditer(text):
        hits.append(m.group(1))
    for m in _IP6_RE.finditer(text):
        hits.append(m.group(1))
    return hits


def _parse_hop(block: str, hop_number: int) -> dict:
    hop: dict = {
        "hop_number":    hop_number,
        "raw":           block,
        "from_host":     None,
        "from_ip":       None,
        "by_host":       None,
        "timestamp":     None,
        "delay_seconds": None,
        "is_public":     False,
    }

    # from HOST (FQDN [IP])
    m = _FROM_RE.search(block)
    if m:
        hop["from_host"] = m.group(1)
        paren = m.group(2) or ""
        ips = _ips_from_text(paren)
        if ips:
            hop["from_ip"] = ips[0]

    # If no IP from parenthetical, scan the whole block
    if not hop["from_ip"]:
        ips = _ips_from_text(block)
        if ips:
            hop["from_ip"] = ips[0]

    # by HOST
    m = _BY_RE.search(block)
    if m:
        hop["by_host"] = m.group(1)

    # date (after the final semicolon)
    m = _DATE_RE.search(block)
    if m:
        date_str = m.group(1).strip().split('\n')[0].strip()
        try:
            dt = parsedate_to_datetime(date_str)
            hop["timestamp"] = dt.isoformat()
        except Exception:
            hop["timestamp"] = date_str   # store raw if unparseable

    if hop["from_ip"]:
        hop["is_public"] = _is_public(hop["from_ip"])

    return hop


def parse_headers(raw: str) -> dict:
    """
    Full forensic parse of raw email headers.

    Returns
    -------
    dict with keys:
        meta         — From, To, Subject, Date, Message-ID, etc.
        auth         — spf, dkim, dmarc result strings
        hops         — list of hop dicts (chronological)
        originating_ip
        public_ips   — all unique public IPs across all hops
        domains      — relay hostnames worth investigating
        anomalies    — list of human-readable anomaly strings
    """
    result: dict = {
        "meta":           {},
        "auth":           {"spf": None, "dkim": None, "dmarc": None},
        "hops":           [],
        "originating_ip": None,
        "public_ips":     [],
        "domains":        [],
        "anomalies":      [],
    }

    # ── Envelope metadata ──────────────────────────────────────────────────────
    for field in ("From", "To", "Cc", "Subject", "Date", "Message-ID",
                  "Reply-To", "Return-Path", "X-Mailer", "User-Agent",
                  "X-Originating-IP", "MIME-Version"):
        val = _header_value(raw, field)
        if val:
            result["meta"][field] = val

    # ── X-Originating-IP ──────────────────────────────────────────────────────
    xoip_raw = result["meta"].get("X-Originating-IP", "")
    if xoip_raw:
        ips = _ips_from_text(xoip_raw)
        if ips:
            result["originating_ip"] = ips[0]

    # ── Authentication-Results ────────────────────────────────────────────────
    auth_blocks = _all_header_values(raw, "Authentication-Results")
    auth_combined = " ".join(auth_blocks)
    for name, pat, key in (
        ("SPF",  _SPF_RE,  "spf"),
        ("DKIM", _DKIM_RE, "dkim"),
        ("DMARC",_DMARC_RE,"dmarc"),
    ):
        m = pat.search(auth_combined)
        if m:
            result["auth"][key] = m.group(1).lower().rstrip(";,")

    # ── Received chain ────────────────────────────────────────────────────────
    # RFC 2822: each new Received: is prepended, so top = newest hop.
    # We reverse to get chronological (first delivery first).
    rcv_blocks_raw = _all_header_values(raw, "Received")
    rcv_blocks_raw.reverse()   # now chronological

    prev_ts: datetime | None = None
    for i, block in enumerate(rcv_blocks_raw):
        hop = _parse_hop(block, i + 1)

        if hop["timestamp"]:
            try:
                curr = datetime.fromisoformat(hop["timestamp"])
                if prev_ts is not None:
                    delay = (curr - prev_ts).total_seconds()
                    hop["delay_seconds"] = int(delay)
                    if delay < -30:
                        result["anomalies"].append(
                            f"Hop {i+1}: negative delay ({delay:.0f}s) — "
                            "possible timestamp forgery or severe clock skew"
                        )
                    elif delay > 7200:
                        result["anomalies"].append(
                            f"Hop {i+1}: unusually long delay "
                            f"({delay/3600:.1f} h) — greylisting or queuing"
                        )
                prev_ts = curr
            except Exception:
                pass

        result["hops"].append(hop)

    # ── Collect unique public IPs and domains ──────────────────────────────────
    seen_ips:     set[str] = set()
    seen_domains: set[str] = set()

    for hop in result["hops"]:
        ip = hop.get("from_ip")
        if ip and ip not in seen_ips:
            seen_ips.add(ip)
            if _is_public(ip):
                result["public_ips"].append(ip)

        for key in ("from_host", "by_host"):
            host = (hop.get(key) or "").lower().rstrip(".")
            if (host and host not in seen_domains and
                    "." in host and len(host) > 4 and
                    not _IP4_RE.fullmatch(host)):
                seen_domains.add(host)
                result["domains"].append(host)

    # Fallback: originating_ip from first public hop
    if not result["originating_ip"] and result["public_ips"]:
        result["originating_ip"] = result["public_ips"][0]

    # ── Anomalies from auth results ────────────────────────────────────────────
    auth = result["auth"]
    fail_words = {"fail", "hardfail", "softfail", "temperror", "permerror", "neutral"}
    if auth["spf"] and auth["spf"].lower() in fail_words:
        result["anomalies"].append(
            f"SPF {auth['spf'].upper()} — sender domain does not authorise this IP"
        )
    if auth["dkim"] and auth["dkim"].lower() not in ("pass", "none"):
        result["anomalies"].append(
            f"DKIM {auth['dkim'].upper()} — signature verification failed"
        )
    if auth["dmarc"] and auth["dmarc"].lower() not in ("pass", "none"):
        result["anomalies"].append(
            f"DMARC {auth['dmarc'].upper()} — message does not align with domain policy"
        )

    # ── Anomaly: private IP in a late (non-local) hop ─────────────────────────
    for hop in result["hops"][2:]:   # first two hops may legitimately be internal
        ip = hop.get("from_ip")
        if ip and not _is_public(ip):
            result["anomalies"].append(
                f"Hop {hop['hop_number']}: private/reserved IP {ip} "
                "at an unexpected position — may indicate header injection"
            )

    # ── Anomaly: Reply-To != From (common phishing indicator) ─────────────────
    from_val   = result["meta"].get("From",     "")
    replyto_val= result["meta"].get("Reply-To", "")
    if from_val and replyto_val:
        # Extract bare domains only
        from_domain    = re.search(r'@([\w.\-]+)', from_val)
        replyto_domain = re.search(r'@([\w.\-]+)', replyto_val)
        if (from_domain and replyto_domain and
                from_domain.group(1).lower() != replyto_domain.group(1).lower()):
            result["anomalies"].append(
                f"Reply-To domain ({replyto_domain.group(1)}) differs from "
                f"From domain ({from_domain.group(1)}) — common in phishing"
            )

    return result


async def analyze_and_store(graph_db, raw: str) -> dict:
    """Parse headers, create IP/Domain nodes in Neo4j, return parsed dict."""
    parsed = parse_headers(raw)

    async with graph_db.driver.session() as session:
        # Create IP nodes for all public IPs found
        for ip in parsed["public_ips"]:
            await session.run(
                "MERGE (n:IP {id: $id}) "
                "ON CREATE SET n.address = $ip, n.source = 'email_headers', "
                "              n.created_at = datetime()",
                id=ip, ip=ip,
            )

        # Create Domain nodes for relay hostnames (cap at 15)
        for domain in parsed["domains"][:15]:
            await session.run(
                "MERGE (n:Domain {id: $id}) "
                "ON CREATE SET n.name = $name, n.source = 'email_headers', "
                "              n.created_at = datetime()",
                id=domain, name=domain,
            )

    return parsed
