---
name: fieldwork-graph
description: >-
  Work with the shared Neo4j graph that is the single datastore for the whole platform —
  legacy Fieldwork entities, SpiderFoot-promoted nodes, investigation findings, news,
  reports, and audit logs all live in one instance. Use this skill whenever a task involves
  reading or writing the graph: writing a Cypher query, adding a node label or relationship
  type, persisting crawler/investigation findings with provenance, entity resolution /
  dedup / merge, the schema in neo4j/init.cypher, or debugging why data isn't showing up in
  the graph/map/connections views. Triggers include "Cypher", "Neo4j", "graph query",
  "MERGE a node", "relationship type", "provenance", "persist to the graph", "dedup /
  merge entities", "graph_db", "GraphDB", "init.cypher", or any change to how entities are
  stored or linked. Applies to both backends (they share the instance).
---

# Fieldwork shared graph (Neo4j)

**One Neo4j instance backs everything** — legacy Fieldwork, the shell modules, news,
reports, audit. Container `runi-neo4j` (bolt `7687`, browser `7474`). There is no
second database, so a label or relationship you add is visible to every service. ~114 files
touch it; treat the schema as a shared contract.

## Two access styles, one database

- **Legacy backend** (`backend/app`): the `GraphDB` class (`graph.py`) wraps the async
  driver and exposes typed helpers — `find_or_create_person`, `get_person_by_id`,
  `search_person`, `get_connections`, `find_paths`, `find_weak_links`, `add_company`,
  `add_relationship`, … Prefer an existing helper; drop to `async with self.driver.session()
  as s: await s.run(cypher, **params)` only for ad-hoc queries. The driver is created once at
  app lifespan and shared across requests/crawlers — never open your own.
- **Shell backend** (`shell/backend/app`): modules get the **raw** async driver as
  `deps["graph_db"]` (built in `deps.py`). Use it directly:
  `async with driver.session() as session: res = await session.run(cypher, **params)`.

Both are `neo4j` Python async driver sessions — same Cypher, same params, same instance.

## The id-keyed MERGE + provenance convention (follow this when writing)

Every node has a **unique `id`** (constraints in `init.cypher` enforce it per label). Write
idempotently by MERGEing on `id`, and **record where each fact came from** — the graph's
value is its provenance. The canonical pattern (see `graph_intel.py`):

```cypher
// node: merge by id, union the contributing source, refresh timestamps
MERGE (n:Domain {id: $id})
ON CREATE SET n.first_seen = datetime()
SET n.name = $name, n.source = $source, n.last_seen = datetime()
```

```cypher
// edge: stamp provenance on the relationship
MERGE (a)-[r:RESOLVES_TO]->(b)
ON CREATE SET r.found_at = datetime()
SET r.via = $tool, r.confidence = $conf, r.last_seen = datetime()
```

Provenance fields you'll see and should keep populating: `source` / `via` (which
tool/crawler), `confidence` (0–1), `first_seen` / `found_at` / `last_seen` (datetimes).
Investigation persistence links an `Investigation` node `-[:INVESTIGATED]->` each derived
node, optionally under a `Case` (`-[:HAS_INVESTIGATION]->`); mirror that if you extend it.

**Never build Cypher with f-string *values*.** Parameterise values (`$id`, `$name`) to avoid
injection and to let Neo4j cache the plan. Interpolating a **label or relationship type** is
sometimes unavoidable (Cypher can't parameterise those) — only do it from a fixed internal
allowlist, never from user input.

## Schema (`neo4j/init.cypher`)

Runs **once** at first startup via the `neo4j-init` compose service — it only creates
constraints and indexes (`IF NOT EXISTS`), it does not seed data. Node labels in use:
`Person`, `Company`, `Location` (core); `Email`, `Phone`, `Username`, `Account`, `IP`,
`Domain`, `Breach`, `Leak`, `Wallet` (SpiderFoot-promoted); `Article`/`NewsArticle`,
`NewsCountry`, `ReportDoc`, `AuditLog`, `Case`, `Investigation`, `Aircraft`, `Flight`,
`DarkWebMention`, `TelegramChannel`/`TelegramPost`, plus orchestrator-derived
`Subdomain`, `Nameserver`, `Organization`, `Sanction`, `CourtCase`, `ThreatPulse`,
`Subreddit`, `Attribute`, `CryptoAddress`.

Adding a new label that needs uniqueness/lookup? Add a `CREATE CONSTRAINT … IF NOT EXISTS`
(and any `CREATE INDEX`) here. Because `init.cypher` only runs on a fresh volume, apply the
same statement to the running DB too (via `cypher-shell` / the browser at `:7474`) so you
don't have to wipe the volume — see the `fieldwork-stack-ops` skill for how to exec into
neo4j.

**`neo4j/init.cypher` must stay LF-only.** The repo sets `core.autocrlf input`; a CRLF here
breaks the init container. Edit with an LF-preserving tool and don't let an editor rewrite
line endings.

## Entity resolution / dedup

Duplicate handling uses `POSSIBLE_DUPLICATE` / `CONFIRMED_DISTINCT` relationships (indexed
on `confidence`) and APOC `mergeNodes` for the actual merge (`graph_intel.merge_entities`,
exposed as `POST /investigate/merge {keep_id, merge_id}`; legacy `dedup.py` drives the
scanner). When merging, keep the surviving node's `id` stable — everything references nodes
by `id`.

## Debugging "my data isn't in the graph"

- Confirm the write path ran and used the shared driver (not a swallowed exception — many
  crawlers catch and return `{found:false}` without writing).
- Check the label/`id` match the read query (a typo'd label creates a silent parallel node).
- Inspect directly: open `http://localhost:7474` or exec `cypher-shell` and run
  `MATCH (n {id:$id}) RETURN n` — the graph is the source of truth, faster than reasoning
  from code.
- Relationship direction matters: `get_connections`/viz queries assume the documented
  direction; a reversed `MERGE (a)<-[r]-(b)` won't show up where expected.

## After changing the schema or storage shape

Update `docs/kb/architecture.md` (Data model section) per the `CLAUDE.md` maintenance
protocol — the node/edge inventory there is what the next session trusts.
