"""
Shodan InternetDB — completely free, no API key required.
https://internetdb.shodan.io/{ip}

Does NOT consume Shodan API credits.  Returns:
  - open_ports  : list of open TCP ports observed by Shodan scans
  - hostnames   : reverse-DNS hostnames
  - tags        : classification tags (vpn, tor, cloud, scanner, …)
  - cves        : CVE identifiers for known vulnerabilities on open services
  - cpes        : CPE identifiers for detected software

Persists data to the IP node in Neo4j.
"""
import hashlib
import logging
import httpx

log = logging.getLogger("fieldwork.internetdb")

_URL = "https://internetdb.shodan.io/{ip}"


async def enrich_ip_internetdb(graph_db, ip: str) -> dict:
    """
    Fetch open port / CVE / tag data for `ip` from Shodan InternetDB.
    No API key required.
    """
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            headers={"User-Agent": "Fieldwork OSINT"},
            follow_redirects=True,
        ) as client:
            r = await client.get(_URL.format(ip=ip))
            if r.status_code == 404:
                return {"ip": ip, "found": False,
                        "message": "IP not in Shodan InternetDB"}
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("InternetDB lookup failed for %s: %s", ip, exc)
        return {"ip": ip, "error": str(exc)}

    ports     = sorted(data.get("ports",     []))
    hostnames = data.get("hostnames", [])
    tags      = data.get("tags",      [])
    cves      = data.get("vulns",     [])   # list of CVE strings
    cpes      = data.get("cpes",      [])

    result = {
        "ip":        ip,
        "found":     True,
        "ports":     ports,
        "hostnames": hostnames,
        "tags":      tags,
        "cves":      cves,
        "cpes":      cpes,
    }

    # ── Persist to IP node ───────────────────────────────────────────────────
    node_id = f"ip_{hashlib.sha1(ip.encode()).hexdigest()[:12]}"
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MERGE (n:IP {id: $id})
            ON CREATE SET n.address = $ip, n.source = 'internetdb',
                          n.created_at = datetime()
            SET n.open_ports   = $ports,
                n.hostnames    = $hosts,
                n.shodan_tags  = $tags,
                n.cves         = $cves,
                n.updated_at   = datetime()
            """,
            id=node_id, ip=ip,
            ports=ports, hosts=hostnames, tags=tags, cves=cves,
        )

    log.info("InternetDB: %s  ports=%s  cves=%s  tags=%s",
             ip, ports[:6], cves[:3], tags)
    return result
