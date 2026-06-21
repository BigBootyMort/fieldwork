"""Etherscan — raw Ethereum on-chain tracing (free API key).

Arkham gives *labeled* entities; this gives the raw money trail: an
address's ETH balance and recent transaction history (counterparties,
values, timestamps). Useful for following funds in crypto investigations.

Get a free key at https://etherscan.io/apis — set ETHERSCAN_API_KEY.
"""
import logging
import os
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("fieldwork.etherscan")

_BASE = "https://api.etherscan.io/api"
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_HEADERS = {"User-Agent": "Fieldwork OSINT", "Accept": "application/json"}


def _ts(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _eth(wei: str) -> float:
    try:
        return round(int(wei) / 1e18, 6)
    except Exception:
        return 0.0


async def trace_eth_address(address: str, limit: int = 15) -> dict:
    """Balance + recent transactions for an Ethereum address."""
    addr = address.strip()
    if not _ADDR_RE.match(addr):
        return {"address": addr, "found": False, "reason": "Not a valid 0x Ethereum address"}
    key = os.getenv("ETHERSCAN_API_KEY", "")
    if not key:
        return {"address": addr, "found": False, "reason": "ETHERSCAN_API_KEY not configured"}

    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            bal = await client.get(_BASE, params={
                "module": "account", "action": "balance",
                "address": addr, "tag": "latest", "apikey": key,
            })
            bal.raise_for_status()
            bj = bal.json()
            balance_eth = _eth(bj.get("result", "0")) if bj.get("status") == "1" else 0.0

            txr = await client.get(_BASE, params={
                "module": "account", "action": "txlist", "address": addr,
                "startblock": 0, "endblock": 99999999, "page": 1,
                "offset": limit, "sort": "desc", "apikey": key,
            })
            txr.raise_for_status()
            tj = txr.json()
            if tj.get("message", "").startswith("NOTOK") or (isinstance(tj.get("result"), str)):
                return {"address": addr, "found": False, "reason": tj.get("result", "Etherscan error")}
            raw = tj.get("result", []) if tj.get("status") == "1" else []
    except Exception as exc:
        log.warning("Etherscan trace failed for %s: %s", addr, exc)
        return {"address": addr, "found": False, "error": str(exc)}

    txs = []
    for t in raw[:limit]:
        direction = "out" if t.get("from", "").lower() == addr.lower() else "in"
        txs.append({
            "hash":         t.get("hash", ""),
            "direction":    direction,
            "counterparty": t.get("to", "") if direction == "out" else t.get("from", ""),
            "value_eth":    _eth(t.get("value", "0")),
            "timestamp":    _ts(t.get("timeStamp")),
            "is_error":     t.get("isError", "0") == "1",
            "url":          f"https://etherscan.io/tx/{t.get('hash','')}",
        })

    return {
        "address":     addr,
        "found":       True,
        "balance_eth": balance_eth,
        "tx_count":    len(raw),
        "transactions": txs,
        "url":         f"https://etherscan.io/address/{addr}",
    }
