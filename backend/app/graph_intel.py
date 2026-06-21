"""
Investigation → Knowledge Graph persistence.

Takes the output of the AI Investigation Orchestrator and writes the discovered
entities + relationships into Neo4j with PROVENANCE — every node/edge records
which tool found it, when, and (where available) a confidence. This turns a
one-shot investigation into a cumulative, pivotable intelligence graph.

Schema (labels):
  Target node       : Person | Company | Domain | IPAddress | Email |
                      Username | CryptoAddress   {id, name, sources[], first_seen, last_seen}
  Derived entities  : Organization, Breach, Sanction, CourtCase, Subdomain,
                      Wallet, Subreddit, Location, ASN, Officer(:Person)
  Relationships carry {via: <tool>, confidence, found_at}

A whole investigation is also attached to an (:Investigation) node (and an
optional (:Case)) so you can list/timeline what a run discovered.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger("fieldwork.graph_intel")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:120] or "x"


_TARGET_LABEL = {
    "name": "Person", "company": "Company", "domain": "Domain",
    "ip": "IPAddress", "email": "Email", "username": "Username",
    "crypto_eth": "CryptoAddress",
}


async def _merge_node(session, label: str, node_id: str, name: str,
                      props: dict, tool: str) -> str:
    """MERGE a node by id, union the source tool, refresh timestamps + props."""
    props = {k: v for k, v in (props or {}).items() if v not in (None, "", [], {})}
    await session.run(
        f"""
        MERGE (n:{label} {{id: $id}})
        ON CREATE SET n.first_seen = datetime(), n.name = $name
        SET n.last_seen = datetime(),
            n.name = coalesce(n.name, $name),
            n.sources = CASE WHEN $tool IN coalesce(n.sources, [])
                             THEN n.sources ELSE coalesce(n.sources, []) + $tool END
        SET n += $props
        """,
        id=node_id, name=name or node_id, tool=tool, props=props,
    )
    return node_id


async def _merge_rel(session, a_label, a_id, rel, b_label, b_id, tool,
                     confidence: float = 0.8):
    await session.run(
        f"""
        MATCH (a:{a_label} {{id: $aid}}), (b:{b_label} {{id: $bid}})
        MERGE (a)-[r:{rel}]->(b)
        ON CREATE SET r.found_at = datetime()
        SET r.via = $tool, r.confidence = $conf, r.last_seen = datetime()
        """,
        aid=a_id, bid=b_id, tool=tool, conf=confidence,
    )


def _as_list(v):
    return v if isinstance(v, list) else ([v] if v else [])


async def persist_investigation(graph_db, result: dict,
                                case_id: str | None = None) -> dict:
    """
    Persist an orchestrator result into the graph. Returns a summary:
      {"target_id", "label", "nodes": n, "rels": m, "investigation_id"}
    Best-effort: a failure on one tool's findings never aborts the rest.
    """
    if not graph_db or not getattr(graph_db, "driver", None):
        return {"error": "graph unavailable"}

    target = result.get("target", "")
    ttype  = result.get("type", "name")
    label  = _TARGET_LABEL.get(ttype, "Entity")
    tid    = _slug(target)
    results = result.get("results", {}) or {}
    inv_id = f"inv-{tid}-{int(time.time())}"

    nodes = rels = 0
    async with graph_db.driver.session() as s:
        # Target + investigation provenance node
        await _merge_node(s, label, tid, target, {"value": target}, "orchestrator")
        nodes += 1
        await s.run(
            """
            MERGE (i:Investigation {id: $iid})
            SET i.target=$t, i.type=$ty, i.created_at=datetime(),
                i.engine=$eng, i.tools=$tools
            WITH i
            MATCH (n {id:$tid})
            MERGE (i)-[:INVESTIGATED]->(n)
            """,
            iid=inv_id, t=target, ty=ttype, eng=result.get("engine"),
            tools=list(results.keys()), tid=tid,
        )
        if case_id:
            await s.run(
                "MERGE (c:Case {id:$cid}) WITH c MATCH (i:Investigation {id:$iid}) "
                "MERGE (c)-[:HAS_INVESTIGATION]->(i)", cid=case_id, iid=inv_id,
            )

        async def link(lbl, nid, name, props, rel, tool, conf=0.8):
            nonlocal nodes, rels
            nid = _slug(nid) if lbl in ("Person", "Company", "Organization",
                                        "Subreddit", "ASN", "Location") else nid.lower()
            await _merge_node(s, lbl, nid, name, props, tool)
            await _merge_rel(s, label, tid, rel, lbl, nid, tool, conf)
            nodes += 1; rels += 1

        # ── Per-tool extraction ───────────────────────────────────────────
        # NOTE: do NOT skip on a bare `error` key — several tools (cert
        # transparency, urlscan) return partial data alongside an error field.
        for tool, res in results.items():
            if not isinstance(res, dict):
                continue
            try:
                t = tool.lower()

                if "ipinfo" in t or "asn" in t:
                    if res.get("org"):
                        await link("Organization", res["org"], res["org"],
                                   {"asn": res.get("asn")}, "ANNOUNCED_BY", tool)
                    loc = ", ".join(filter(None, [res.get("city"), res.get("country")]))
                    if loc:
                        await link("Location", loc, loc, {}, "LOCATED_IN", tool, 0.6)

                elif "otx" in t and res.get("found"):
                    if res.get("malicious"):
                        await s.run(f"MATCH (n:{label} {{id:$id}}) SET n.threat_flagged=true, "
                                    "n.pulse_count=$pc", id=tid, pc=res.get("pulse_count", 0))
                    for p in (res.get("pulses") or [])[:5]:
                        nm = p.get("name", "")[:120]
                        if nm:
                            await link("ThreatPulse", f"otx-{_slug(nm)}", nm,
                                       {"tags": p.get("tags")}, "REPORTED_IN", tool, 0.7)

                elif "rdap" in t or "whois" in t:
                    for em in (_as_list(res.get("emails")) + _as_list(res.get("registrant_email"))):
                        em = em.strip() if isinstance(em, str) else em
                        if em and "@" in str(em) and "redact" not in str(em).lower():
                            await link("Email", em, em, {}, "REGISTERED_BY", tool)
                    reg = res.get("registrar") or res.get("registrant_org") or res.get("registrant_name")
                    if reg and "redact" not in str(reg).lower():
                        await link("Organization", reg, reg, {}, "REGISTERED_BY", tool, 0.7)
                    for ns in (res.get("nameservers") or [])[:8]:
                        nsv = ns if isinstance(ns, str) else ns.get("ldhName", "")
                        if nsv:
                            await link("Nameserver", nsv.lower(), nsv, {}, "USES_NAMESERVER", tool, 0.85)

                elif "cert" in t:
                    for sub in (res.get("subdomains") or res.get("certs") or [])[:30]:
                        sd = sub if isinstance(sub, str) else (sub.get("name") or sub.get("common_name", ""))
                        if sd and sd != target:
                            await link("Subdomain", sd, sd, {}, "HAS_SUBDOMAIN", tool, 0.9)

                elif "passivedns" in t or "passive" in t:
                    for sub in (res.get("subdomains") or [])[:30]:
                        sd = sub if isinstance(sub, str) else sub.get("name", "")
                        if sd and sd != target:
                            await link("Subdomain", sd, sd, {}, "HAS_SUBDOMAIN", tool, 0.75)
                    for ip in (res.get("ip_history") or res.get("records") or [])[:25]:
                        ipv = ip if isinstance(ip, str) else (ip.get("ip") or ip.get("address", ""))
                        if ipv:
                            await link("IPAddress", ipv, ipv, {}, "RESOLVES_TO", tool, 0.8)

                elif "urlscan" in t:
                    for sc in (res.get("scans") or [])[:10]:
                        ipv = sc.get("ip") if isinstance(sc, dict) else ""
                        if ipv:
                            await link("IPAddress", ipv, ipv, {}, "RESOLVES_TO", tool, 0.6)

                elif "hunter" in t:
                    for em in (res.get("emails") or [])[:30]:
                        ev = em if isinstance(em, str) else em.get("value", "")
                        if ev:
                            await link("Email", ev, ev, {}, "HAS_EMAIL", tool, 0.7)

                elif "hibp" in t:
                    for br in (res.get("breaches") or [])[:30]:
                        bn = br if isinstance(br, str) else (br.get("Name") or br.get("name", ""))
                        if bn:
                            await link("Breach", f"breach-{_slug(bn)}", bn, {}, "EXPOSED_IN", tool, 0.95)

                elif "emailrep" in t and res.get("reputation"):
                    await s.run(f"MATCH (n:{label} {{id:$id}}) SET n.reputation=$r, "
                                "n.suspicious=$sus", id=tid,
                                r=res.get("reputation"), sus=bool(res.get("suspicious")))

                elif "sanction" in t or "ofac" in t:
                    for hit in (res.get("hits") or [])[:15]:
                        cap = hit.get("caption", "")
                        if cap:
                            await link("Sanction", f"sanction-{_slug(cap)}", cap,
                                       {"score": hit.get("score"), "lists": hit.get("datasets")},
                                       "SANCTIONS_MATCH", tool, hit.get("score", 0.6) or 0.6)

                elif "court" in t:
                    for c in (res.get("cases") or [])[:15]:
                        cn = c.get("case_name", "")
                        if cn:
                            await link("CourtCase", f"case-{_slug(cn)}", cn,
                                       {"court": c.get("court"), "date": c.get("date_filed"),
                                        "url": c.get("url")}, "PARTY_TO", tool, 0.7)

                elif "wikidata" in t and res.get("found"):
                    for f in (res.get("facts") or [])[:20]:
                        val = f.get("value", "")
                        prop = f.get("property", "related")
                        if val:
                            await link("Attribute", f"attr-{_slug(prop)}-{_slug(val)}", f"{prop}: {val}",
                                       {"property": prop}, "HAS_ATTRIBUTE", tool, 0.85)

                elif "companies house" in t:
                    for o in (res.get("officers") or res.get("companies") or [])[:25]:
                        nm = o.get("name") or o.get("company_name", "")
                        if nm:
                            await link("Person", nm, nm, {"role": o.get("role")},
                                       "OFFICER", tool, 0.8)
                    for b in (res.get("beneficial_owners") or [])[:25]:
                        if b.get("name"):
                            await link("Person", b["name"], b["name"],
                                       {"psc": True, "nature": b.get("nature")},
                                       "BENEFICIAL_OWNER", tool, 0.9)

                elif "reddit" in t and res.get("found"):
                    for sub in (res.get("active_subreddits") or [])[:10]:
                        sr = sub.get("subreddit") if isinstance(sub, dict) else sub
                        if sr:
                            await link("Subreddit", sr, f"r/{sr}", {}, "ACTIVE_IN", tool, 0.6)

                elif "etherscan" in t and res.get("found"):
                    await s.run(f"MATCH (n:{label} {{id:$id}}) SET n.eth_balance=$b",
                                id=tid, b=res.get("balance_eth"))
                    for tx in (res.get("transactions") or [])[:15]:
                        cp = tx.get("counterparty", "")
                        if cp:
                            await link("CryptoAddress", cp, cp, {},
                                       "TRANSACTED_WITH", tool, 0.7)
            except Exception as exc:
                log.debug("persist extractor %s failed: %s", tool, exc)

    return {"target_id": tid, "label": label, "nodes": nodes, "rels": rels,
            "investigation_id": inv_id, "case_id": case_id}


async def merge_entities(graph_db, keep_id: str, merge_id: str) -> dict:
    """
    Merge the `merge_id` node into `keep_id` (entity resolution): all of the
    duplicate's relationships are moved onto the kept node, source provenance is
    unioned, and the duplicate is deleted. Uses APOC (installed in this Neo4j).
    """
    if not graph_db or not getattr(graph_db, "driver", None):
        return {"error": "graph unavailable"}
    if keep_id == merge_id:
        return {"error": "keep_id and merge_id are the same"}
    try:
        async with graph_db.driver.session() as s:
            # Union the source lists onto the kept node first (mergeNodes
            # 'discard' keeps the first node's scalars, so do sources manually).
            await s.run(
                """
                MATCH (k {id:$keep}), (d {id:$dup})
                SET k.sources = CASE WHEN k.sources IS NULL THEN d.sources
                    ELSE k.sources + [x IN coalesce(d.sources, []) WHERE NOT x IN k.sources] END
                """, keep=keep_id, dup=merge_id,
            )
            r = await s.run(
                """
                MATCH (keep {id:$keep}), (dup {id:$dup})
                WHERE keep <> dup
                CALL apoc.refactor.mergeNodes([keep, dup],
                     {properties:'discard', mergeRels:true}) YIELD node
                RETURN node.id AS id, labels(node) AS labels, node.name AS name
                """, keep=keep_id, dup=merge_id,
            )
            rec = await r.single()
    except Exception as exc:
        log.warning("merge_entities failed: %s", exc)
        return {"error": str(exc)}
    if not rec:
        return {"error": "one or both nodes not found"}
    return {"merged": True, "kept_id": rec["id"], "removed_id": merge_id,
            "name": rec["name"], "labels": [l for l in (rec["labels"] or []) if l != "Entity"]}


async def get_investigation_subgraph(graph_db, target_id: str, depth: int = 1) -> dict:
    """Return nodes + edges around a target for graph rendering."""
    if not graph_db or not getattr(graph_db, "driver", None):
        return {"nodes": [], "edges": []}
    depth = max(1, min(int(depth), 3))
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    async with graph_db.driver.session() as s:
        r = await s.run(
            f"""
            MATCH (t {{id:$id}})-[rels*1..{depth}]-(m)
            WITH t, m, rels
            UNWIND rels AS rel
            WITH t, m, startNode(rel) AS a, endNode(rel) AS b, type(rel) AS rt,
                 rel.via AS via, rel.confidence AS conf
            RETURN DISTINCT id(a) AS aid, labels(a) AS al, a.id AS akey, a.name AS aname,
                            id(b) AS bid, labels(b) AS bl, b.id AS bkey, b.name AS bname,
                            rt, via, conf
            LIMIT 300
            """, id=target_id,
        )
        for rec in await r.fetch(300):
            for side in ("a", "b"):
                k = rec[f"{side}key"]
                if k and k not in nodes:
                    labs = [l for l in (rec[f"{side}l"] or []) if l != "Entity"]
                    nodes[k] = {"id": k, "label": (labs or ["Entity"])[0],
                                "name": rec[f"{side}name"] or k}
            edges.append({"source": rec["akey"], "target": rec["bkey"],
                          "type": rec["rt"], "via": rec["via"], "confidence": rec["conf"]})
    return {"nodes": list(nodes.values()), "edges": edges,
            "node_count": len(nodes), "edge_count": len(edges)}
