"""
Arkham Intelligence crypto enrichment — Phase 6.5

Arkham maps blockchain wallet addresses to real-world entities
(companies, exchanges, individuals, funds).

  enrich_wallet_arkham(graph_db, address)
      Looks up a wallet address via the Arkham API.
      Creates a Wallet node with entity metadata.
      Links to existing Person / Company nodes if entity name matches.

Requires ARKHAM_API_KEY — free tier available at arkhamintelligence.com.
"""
import logging
import os
import re

import httpx

log = logging.getLogger("crawler.arkham")

_ARKHAM_BASE = "https://api.arkhamintelligence.com"
_ARKHAM_KEY  = os.getenv("ARKHAM_API_KEY", "")

# Basic validation patterns for common chain address formats
_ADDR_PATTERNS = {
    "ethereum": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "bitcoin":  re.compile(r"^(1|3|bc1)[a-zA-HJ-NP-Z0-9]{25,62}$"),
    "solana":   re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
}


async def enrich_wallet_arkham(graph_db, address: str) -> dict:
    """
    Look up a wallet address via Arkham Intelligence.
    Creates/updates a Wallet node and attempts to link to known entities.
    """
    if not _ARKHAM_KEY:
        return {
            "found": False,
            "address": address,
            "reason": "ARKHAM_API_KEY not set. Register at arkhamintelligence.com.",
        }

    address = address.strip()
    chain   = _detect_chain(address)
    log.info("Arkham: looking up %s (chain: %s)", address, chain or "unknown")

    data = await _fetch_arkham(address)
    if data is None:
        return {"found": False, "address": address,
                "reason": "Arkham API unreachable or address not found"}

    entity      = data.get("arkhamEntity") or {}
    entity_name = entity.get("name") or entity.get("id") or ""
    entity_type = entity.get("type") or ""
    tags        = [t.get("name", "") for t in (data.get("arkhamLabels") or [])]

    # Token balances summary
    balances = []
    for token in (data.get("balances") or [])[:10]:
        bal = {
            "symbol":   token.get("symbol", ""),
            "name":     token.get("name", ""),
            "usd_value": token.get("usdValue", 0),
        }
        if bal["symbol"]:
            balances.append(bal)

    total_usd = sum(b["usd_value"] for b in balances)

    # Write Wallet node
    wallet_id = f"wallet:{address.lower()}"
    async with graph_db.driver.session() as session:
        await session.run(
            "MERGE (w:Wallet {id: $id}) "
            "ON CREATE SET "
            "  w.address     = $addr, "
            "  w.chain       = $chain, "
            "  w.entity_name = $entity, "
            "  w.entity_type = $etype, "
            "  w.tags        = $tags, "
            "  w.usd_value   = $usd, "
            "  w.source      = 'arkham', "
            "  w.first_seen  = datetime() "
            "ON MATCH SET "
            "  w.entity_name = $entity, "
            "  w.tags        = $tags, "
            "  w.usd_value   = $usd",
            id=wallet_id, addr=address,
            chain=chain or "unknown",
            entity=entity_name, etype=entity_type,
            tags=tags, usd=total_usd,
        )

        # Try to link to existing Person or Company by entity name
        if entity_name:
            await session.run(
                "MATCH (w:Wallet {id: $wid}) "
                "OPTIONAL MATCH (p:Person)  WHERE toLower(p.name)  = toLower($name) "
                "OPTIONAL MATCH (c:Company) WHERE toLower(c.name)  = toLower($name) "
                "WITH w, p, c "
                "FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END | "
                "  MERGE (p)-[:CONTROLS_WALLET]->(w)) "
                "FOREACH (_ IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END | "
                "  MERGE (c)-[:CONTROLS_WALLET]->(w))",
                wid=wallet_id, name=entity_name,
            )

    log.info("Arkham: %s → entity=%r tags=%s usd=%.0f",
             address, entity_name, tags, total_usd)
    return {
        "found":       True,
        "address":     address,
        "chain":       chain or "unknown",
        "entity_name": entity_name,
        "entity_type": entity_type,
        "tags":        tags,
        "balances":    balances,
        "total_usd":   total_usd,
        "wallet_id":   wallet_id,
    }


async def _fetch_arkham(address: str) -> dict | None:
    """Query the Arkham address endpoint."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{_ARKHAM_BASE}/address/{address}",
                headers={"API-Key": _ARKHAM_KEY},
            )
        if r.status_code == 404:
            return {}   # address exists but no entity mapping
        if r.status_code in (401, 403):
            log.warning("Arkham: invalid API key")
            return None
        if r.status_code != 200:
            log.warning("Arkham: HTTP %s", r.status_code)
            return None
        return r.json()
    except Exception as exc:
        log.warning("Arkham request failed: %s", exc)
        return None


def _detect_chain(address: str) -> str | None:
    """Guess blockchain from address format."""
    for chain, pattern in _ADDR_PATTERNS.items():
        if pattern.match(address):
            return chain
    return None
