"""
GreyNoise Community API — free without an API key; higher rate limits with one.

Community endpoint: https://api.greynoise.io/v3/community/{ip}

Classifies IPs as:
  noise        — actively scanning the internet (suspicious by default)
  riot         — known benign services (Cloudflare, Google, AWS, etc.)
  classification: "malicious" | "benign" | "unknown"

Requires no account.  If GREYNOISE_API_KEY is set in .env the key is sent
as the `key` header, which raises rate limits significantly.

Persists classification to the IP node in Neo4j.
"""
import hashlib
import logging
import os
import httpx

log = logging.getLogger("fieldwork.greynoise")

_COMMUNITY_URL = "https://api.greynoise.io/v3/community/{ip}"
_API_KEY        = os.getenv("GREYNOISE_API_KEY", "")


async def enrich_ip_greynoise(graph_db, ip: str) -> dict:
    """
    Look up an IP against GreyNoise Community.
    Works without an API key; set GREYNOISE_API_KEY for higher quotas.
    """
    headers = {"User-Agent": "Fieldwork OSINT", "Accept": "application/json"}
    if _API_KEY:
        headers["key"] = _API_KEY

    try:
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
            r = await client.get(_COMMUNITY_URL.format(ip=ip))
            if r.status_code == 404:
                return {
                    "ip":      ip,
                    "found":   False,
                    "message": "IP not observed by GreyNoise",
                }
            if r.status_code == 429:
                return {
                    "ip":    ip,
                    "error": "GreyNoise rate limit reached — "
                             "add GREYNOISE_API_KEY to .env for higher limits",
                }
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("GreyNoise lookup failed for %s: %s", ip, exc)
        return {"ip": ip, "error": str(exc)}

    result = {
        "ip":             ip,
        "found":          True,
        "noise":          bool(data.get("noise",          False)),
        "riot":           bool(data.get("riot",           False)),
        "classification": data.get("classification",     "unknown"),
        "name":           data.get("name",               ""),
        "link":           data.get("link",               ""),
        "last_seen":      (data.get("last_seen") or "")[:10],
        "message":        data.get("message",            ""),
    }

    # Derive a simple verdict string
    if result["riot"]:
        result["verdict"] = "benign_service"
    elif result["classification"] == "malicious":
        result["verdict"] = "malicious"
    elif result["noise"] and result["classification"] == "unknown":
        result["verdict"] = "scanner"
    else:
        result["verdict"] = result["classification"] or "unknown"

    # ── Persist to IP node ───────────────────────────────────────────────────
    node_id = f"ip_{hashlib.sha1(ip.encode()).hexdigest()[:12]}"
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MERGE (n:IP {id: $id})
            ON CREATE SET n.address = $ip, n.source = 'greynoise',
                          n.created_at = datetime()
            SET n.gn_noise          = $noise,
                n.gn_riot           = $riot,
                n.gn_classification = $cls,
                n.gn_name           = $name,
                n.gn_last_seen      = $last,
                n.updated_at        = datetime()
            """,
            id=node_id, ip=ip,
            noise=result["noise"], riot=result["riot"],
            cls=result["classification"], name=result["name"],
            last=result["last_seen"],
        )

    log.info("GreyNoise: %s  class=%s  noise=%s  riot=%s",
             ip, result["classification"], result["noise"], result["riot"])
    return result
