"""
abuse.ch threat intelligence feeds — completely free, no API key required.

Three services:
  URLhaus      — malware hosting / distribution URLs, domains, IPs
                 https://urlhaus-api.abuse.ch/v1/
  ThreatFox    — multi-type IOC database (IPs, domains, URLs, hashes)
                 https://threatfox-api.abuse.ch/api/v1/
  MalwareBazaar — file hash lookup (MD5, SHA1, SHA256)
                 https://mb-api.abuse.ch/api/v1/

All APIs accept POST and return JSON.  No authentication needed.
"""
import hashlib
import logging
import re
import httpx

log = logging.getLogger("fieldwork.abusedb")

_URLHAUS_BASE      = "https://urlhaus-api.abuse.ch/v1/"
_THREATFOX_BASE    = "https://threatfox-api.abuse.ch/api/v1/"
_MALWAREBAZAAR_BASE = "https://mb-api.abuse.ch/api/v1/"

_HEADERS = {"User-Agent": "Fieldwork OSINT"}


# ── URLhaus ───────────────────────────────────────────────────────────────────

def _is_ip(host: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host))


async def check_urlhaus(graph_db, host: str) -> dict:
    """
    Check if a domain or IP address appears in URLhaus as a malware host.
    Persists urlhaus_listed flag to the matching node in Neo4j.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client:
            r = await client.post(_URLHAUS_BASE + "host/", data={"host": host})
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("URLhaus lookup failed for %s: %s", host, exc)
        return {"host": host, "error": str(exc)}

    query_status = data.get("query_status", "")
    urls         = data.get("urls") or []

    result = {
        "host":         host,
        "found":        query_status not in ("no_results", "invalid_host"),
        "query_status": query_status,
        "url_count":    len(urls),
        "blacklists":   data.get("blacklists") or {},
        "urls": [
            {
                "url":        u.get("url", ""),
                "url_status": u.get("url_status", ""),
                "added":      (u.get("date_added") or "")[:10],
                "threat":     u.get("threat", ""),
                "tags":       u.get("tags") or [],
                "reporter":   u.get("reporter", ""),
                "urlhaus_ref":u.get("urlhaus_reference", ""),
            }
            for u in urls[:20]
        ],
    }

    # Persist listing flag to IP or Domain node
    if result["found"] and result["url_count"] > 0:
        ntype = "IP" if _is_ip(host) else "Domain"
        prop  = "address" if ntype == "IP" else "domain"
        nid   = f"{'ip' if ntype == 'IP' else 'domain'}_{hashlib.sha1(host.encode()).hexdigest()[:12]}"
        async with graph_db.driver.session() as session:
            await session.run(
                f"""
                MERGE (n:{ntype} {{id: $id}})
                ON CREATE SET n.{prop} = $host, n.source = 'urlhaus',
                              n.created_at = datetime()
                SET n.urlhaus_listed  = true,
                    n.urlhaus_urls    = $uc,
                    n.updated_at      = datetime()
                """,
                id=nid, host=host, uc=result["url_count"],
            )

    log.info("URLhaus: %s  found=%s  urls=%d", host, result["found"], result["url_count"])
    return result


# ── ThreatFox ─────────────────────────────────────────────────────────────────

async def search_threatfox(ioc: str) -> dict:
    """
    Search ThreatFox for any IOC type: IP:port, domain, URL, MD5, SHA256.
    Returns matched threat actor tags, malware families, and confidence scores.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client:
            r = await client.post(
                _THREATFOX_BASE,
                json={"query": "search_ioc", "search_term": ioc.strip()},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("ThreatFox search failed for %s: %s", ioc, exc)
        return {"ioc": ioc, "error": str(exc)}

    status = data.get("query_status", "")
    items  = data.get("data") or []

    return {
        "ioc":   ioc,
        "found": status == "ok" and bool(items),
        "count": len(items),
        "results": [
            {
                "ioc_type":    x.get("ioc_type", ""),
                "ioc_value":   x.get("ioc", ""),
                "threat_type": x.get("threat_type", ""),
                "malware":     x.get("malware_printable", ""),
                "confidence":  x.get("confidence_level", 0),
                "first_seen":  (x.get("first_seen") or "")[:10],
                "last_seen":   (x.get("last_seen")  or "")[:10],
                "tags":        x.get("tags") or [],
                "reporter":    x.get("reporter", ""),
                "reference":   x.get("reference", ""),
                "threatfox_link": f"https://threatfox.abuse.ch/ioc/{x.get('id','')}",
            }
            for x in items[:20]
        ],
    }


# ── MalwareBazaar ─────────────────────────────────────────────────────────────

async def check_malwarebazaar(file_hash: str) -> dict:
    """
    Look up a file hash (MD5 / SHA1 / SHA256) in MalwareBazaar.
    Returns malware family, file metadata, AV vendor hits, and tags.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS) as client:
            r = await client.post(
                _MALWAREBAZAAR_BASE,
                data={"query": "get_info", "hash": file_hash.strip()},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("MalwareBazaar lookup failed for %s: %s", file_hash, exc)
        return {"hash": file_hash, "error": str(exc)}

    status  = data.get("query_status", "")
    samples = data.get("data") or []

    if not samples:
        return {"hash": file_hash, "found": False, "query_status": status}

    s = samples[0]
    vendor_intel = list((s.get("vendor_intel") or {}).keys())

    return {
        "hash":           file_hash,
        "found":          True,
        "sha256":         s.get("sha256_hash",   ""),
        "sha1":           s.get("sha1_hash",     ""),
        "md5":            s.get("md5_hash",      ""),
        "file_name":      s.get("file_name",     ""),
        "file_type":      s.get("file_type",     ""),
        "file_size":      s.get("file_size"),
        "mime_type":      s.get("mime_type",     ""),
        "signature":      s.get("signature",     ""),   # malware family name
        "first_seen":     (s.get("first_seen") or "")[:10],
        "last_seen":      (s.get("last_seen")  or "")[:10],
        "tags":           s.get("tags") or [],
        "delivery_method":s.get("delivery_method",  ""),
        "origin_country": s.get("origin_country",   ""),
        "vendor_hits":    vendor_intel[:15],
        "bazaar_url":     f"https://bazaar.abuse.ch/sample/{s.get('sha256_hash', '')}",
    }
