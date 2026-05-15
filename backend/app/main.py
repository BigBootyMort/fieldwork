"""
Fieldwork OSINT — connection engine API.

Single-user, localhost-only. Hardened CORS, uses a shared GraphDB driver
across requests and crawlers (no per-request reconnects).
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
import asyncio
import ipaddress
import logging
import os
import re
from pathlib import Path

import httpx

from graph import GraphDB
from crawlers.opencorporates import OpenCorporatesCrawler
from crawlers.github import GitHubCrawler
from crawlers.news import NewsCrawler
from crawlers.sec import SECCrawler
from crawlers.crtsh import CrtShCrawler
from crawlers.aleph import AlephCrawler, search_aleph
from crawlers.rdap import enrich_domain
from crawlers.shodan_client import enrich_ip_shodan
from crawlers.virustotal import enrich_domain_vt, enrich_ip_vt
from crawlers.wayback import enrich_domain_wayback
from media import analyse_media, reverse_image_links, saucenao_search, extract_frames_and_search
from ner_pipeline import process_text, warm_nlp
from dedup import (
    run_duplicate_detection, get_duplicate_candidates,
    merge_persons, dismiss_duplicate, get_graph_health,
)
from crawlers.opensky import enrich_aircraft_faa, enrich_aircraft_flights
from crawlers.ahmia import search_ahmia
from crawlers.telegram import scrape_telegram_channel
from crawlers.hibp import check_email_hibp, check_domain_hibp
from crawlers.arkham import enrich_wallet_arkham
from crawlers.censys import search_hosts as censys_search_hosts, get_host as censys_get_host
from crawlers.dehashed import search as dehashed_search
from spiderfoot_client import SpiderFootClient, promote_scan_to_graph
from monitor import (
    add_watch, remove_watch, list_watches, get_watch,
    get_alerts, mark_alerts_seen, get_unseen_count,
    check_watch,
)
from scheduler import start_scheduler, stop_scheduler, run_all_active
from pivot import search_all_entities, entity_summary, pivot_from, pivot_suggestions
from graph_viz import subgraph_around, full_graph_sample, expand_node as gv_expand_node
from map_view import get_locations, geocode_location, geolocate_ip, add_manual_location
from crm import (
    create_case, list_cases, get_case, get_case_full, update_case, archive_case,
    add_subject, remove_subject, get_subjects,
    add_note, pin_note, delete_note, get_notes,
    add_task, toggle_task, delete_task, get_tasks,
    export_case_markdown,
    share_case, unshare_case, get_case_collaborators,
    CASE_STATUSES, CASE_PRIORITIES, NOTE_TYPES, SUBJECT_ROLES, SUBJECT_LABELS,
)
from auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, require_admin, optional_user,
)
from users import (
    register_user, get_user_by_username, get_user_by_id,
    list_users as list_all_users, update_user_role, deactivate_user,
    VALID_ROLES,
)
from audit import log_event as audit_log, get_audit_log
from fastapi.responses import PlainTextResponse, Response
from fastapi import BackgroundTasks, Body
import hashlib
from llm import (
    extract_entities   as llm_extract_entities,
    summarize_case     as llm_summarize_case,
    suggest_hypotheses as llm_suggest_hypotheses,
    chat               as llm_chat,
    ollama_status,
    pull_model         as llm_pull_model,
    VALID_ENTITY_TYPES,
)
from timeline import get_global_timeline, get_case_timeline
from documents import (
    ingest_document, list_documents, get_document,
    remove_document, check_vt_hash,
)
from search import full_text_search, ensure_fulltext_index, related_entities
from semantic_search import (
    ensure_vector_index, pull_embed_model, semantic_search,
    index_entity as sem_index_entity, batch_index_all,
)
from federated_search import federated_search, SOURCE_ORDER
from bulk_import import (
    parse_file as bi_parse_file,
    normalise_rows as bi_normalise,
    bulk_import_entities,
    SUPPORTED_LABELS as BI_LABELS,
    detect_type as bi_detect_type,
)
from dashboard import (
    get_dashboard_stats,
    get_recent_activity,
    get_top_connected,
    get_entity_timeline,
)
from risk_scoring import (
    score_entity,
    score_all_entities,
    get_high_risk_entities,
)
from email_headers import analyze_and_store as analyze_email_headers
from crawlers.phone_intel import enrich_phone
from crawlers.abuseipdb import check_ip as abuseipdb_check_ip

from fastapi import UploadFile, File
import tempfile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fieldwork")

# ---- Lifespan: single shared driver for the whole app ----
graph_db = GraphDB()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await graph_db.connect()
    log.info("Neo4j driver connected")
    # Warm spaCy model at startup — pays the 1-2 s load cost once
    # so the first /crawl/person request isn't penalised.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, warm_nlp)
    # Phase 18: ensure fulltext index exists (idempotent)
    await ensure_fulltext_index(graph_db)
    # Semantic search: ensure vector index + pull embedding model in background
    await ensure_vector_index(graph_db)
    asyncio.create_task(pull_embed_model())
    # Start background monitoring scheduler (Phase 7)
    scheduler = start_scheduler(graph_db)
    yield
    stop_scheduler()
    await graph_db.close()
    log.info("Neo4j driver closed")


app = FastAPI(
    title="Fieldwork OSINT",
    description="Connection engine for journalists",
    version="0.3.0",
    lifespan=lifespan,
)

# CORS — single-user localhost: allow loopback and file:// (origin null)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost",
        "http://127.0.0.1",
        "null",  # file:// pages
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ---- Request models ----
class SearchRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    company: Optional[str] = Field(None, max_length=200)
    location: Optional[str] = Field(None, max_length=200)


class PathRequest(BaseModel):
    source_id: str = Field(..., min_length=1, max_length=200)
    target_id: str = Field(..., min_length=1, max_length=200)
    max_depth: int = Field(3, ge=1, le=6)


# ---- Routes ----
@app.get("/")
async def root():
    return {"message": "Fieldwork OSINT API", "version": "0.3.0", "status": "running"}


async def _ping(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(url)
            return r.status_code == 200
    except Exception:
        return False


@app.get("/health")
async def health():
    db_ok, maigret_ok, harvester_ok = await asyncio.gather(
        graph_db.ping(),
        _ping(f"{_MAIGRET_URL}/health"),
        _ping(f"{_HARVESTER_URL}/health"),
    )
    return {
        "ok": db_ok,
        "neo4j":        db_ok,
        "maigret":      maigret_ok,
        "theharvester": harvester_ok,
        "shodan":       bool(os.getenv("SHODAN_API_KEY")),
        "virustotal":   bool(os.getenv("VIRUSTOTAL_API_KEY")),
        "aleph":        bool(os.getenv("ALEPH_API_KEY")),
        "saucenao":     bool(os.getenv("SAUCENAO_KEY")),
    }


@app.post("/search/person")
async def search_person(req: SearchRequest):
    results = await graph_db.search_person(req.name, req.company, req.location)
    return {"results": results}


_CRAWLER_TIMEOUT = 90.0  # seconds per crawler before we give up and continue


async def _run_crawler(crawler, person: dict, company: Optional[str]) -> Optional[dict]:
    """Run one crawler with a timeout. Returns an error dict or None on success."""
    try:
        await asyncio.wait_for(crawler.crawl(person, company), timeout=_CRAWLER_TIMEOUT)
        return None
    except asyncio.TimeoutError:
        log.warning("Crawler %s timed out after %.0fs", crawler.name, _CRAWLER_TIMEOUT)
        return {"crawler": crawler.name, "error": f"timed out after {_CRAWLER_TIMEOUT:.0f}s"}
    except Exception as e:
        log.exception("Crawler %s failed", crawler.name)
        return {"crawler": crawler.name, "error": str(e)}


@app.post("/crawl/person")
async def crawl_person(req: SearchRequest):
    """Find or create a person, then run all crawlers concurrently."""
    person = await graph_db.find_or_create_person(req.name)

    crawlers = [
        OpenCorporatesCrawler(graph_db),
        GitHubCrawler(graph_db),
        NewsCrawler(graph_db),
        SECCrawler(graph_db),
        CrtShCrawler(graph_db),
        AlephCrawler(graph_db),
    ]

    results = await asyncio.gather(*[
        _run_crawler(c, person, req.company) for c in crawlers
    ])
    errors = [r for r in results if r is not None]

    connections = await graph_db.get_connections(person["id"], depth=2)
    return {"person": person, "connections": connections, "errors": errors}


@app.post("/paths")
async def find_paths(req: PathRequest):
    paths = await graph_db.find_paths(req.source_id, req.target_id, req.max_depth)
    return {"paths": paths}


@app.get("/person/{person_id}/sources")
async def find_potential_sources(person_id: str):
    # Validate id format to prevent injection-y values
    if not re.match(r"^[a-z0-9_]+$", person_id) or len(person_id) > 200:
        raise HTTPException(400, "Invalid person_id")
    sources = await graph_db.find_weak_links(person_id)
    return {"sources": sources}


@app.get("/person/{person_id}")
async def get_person(person_id: str):
    if not re.match(r"^[a-z0-9_]+$", person_id) or len(person_id) > 200:
        raise HTTPException(400, "Invalid person_id")
    person = await graph_db.get_person_by_id(person_id)
    if not person:
        raise HTTPException(404, "Person not found")
    return {"person": person}


# ============================================================
# Maigret client (username OSINT across 3000+ sites)
# ============================================================
_MAIGRET_URL = os.getenv("MAIGRET_URL", "http://maigret:7000")


class MaigretClient:
    def __init__(self, base_url: str = _MAIGRET_URL, timeout: float = 190.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def search(self, username: str, top_sites: int = 100) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(
                f"{self.base_url}/search",
                json={"username": username, "top_sites": top_sites, "timeout": 15},
            )
            r.raise_for_status()
            return r.json()


# ============================================================
# Enrichment endpoints — on-demand, not part of auto-crawl
# ============================================================
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def _validate_domain(domain: str) -> str:
    domain = domain.lower().strip()
    if not _DOMAIN_RE.match(domain):
        raise HTTPException(400, "Invalid domain name")
    return domain


def _validate_ip(ip: str) -> str:
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(400, "Invalid IP address")
    return ip


@app.get("/enrich/domain/{domain}")
async def enrich_domain_rdap(domain: str):
    """RDAP registrant data → enrich Domain node."""
    return await enrich_domain(graph_db, _validate_domain(domain))


@app.get("/enrich/domain/{domain}/vt")
async def enrich_domain_virustotal(domain: str):
    """VirusTotal reputation + passive DNS → IP nodes."""
    return await enrich_domain_vt(graph_db, _validate_domain(domain))


@app.get("/enrich/domain/{domain}/wayback")
async def enrich_domain_wayback_endpoint(domain: str):
    """Wayback Machine history → first/last archived dates + interesting paths."""
    return await enrich_domain_wayback(graph_db, _validate_domain(domain))


@app.get("/enrich/ip/{ip}")
async def enrich_ip_shodan_endpoint(ip: str):
    """Shodan host data → ports, org, location."""
    return await enrich_ip_shodan(graph_db, _validate_ip(ip))


@app.get("/enrich/ip/{ip}/vt")
async def enrich_ip_virustotal(ip: str):
    """VirusTotal IP reputation + reverse DNS → Domain nodes."""
    return await enrich_ip_vt(graph_db, _validate_ip(ip))


# ============================================================
# OCCRP Aleph — standalone search endpoint
# ============================================================

@app.get("/search/aleph")
async def aleph_search(q: str, schema: str = ""):
    """Search OCCRP Aleph for entities matching a free-text query."""
    if not q or len(q) > 200:
        raise HTTPException(400, "q must be 1-200 characters")
    results = await search_aleph(q.strip(), schema_filter=schema)
    return {"query": q, "results": results, "count": len(results)}


# ============================================================
# theHarvester — domain email/subdomain recon
# ============================================================
_HARVESTER_URL = os.getenv("HARVESTER_URL", "http://theharvester:7001")


class HarvestRequest(BaseModel):
    domain: str = Field(..., min_length=1, max_length=253)
    sources: str = Field("crtsh,dnsdumpster,hackertarget,otx,rapiddns,sitedossier,urlscan",
                         max_length=500)
    limit: int = Field(100, ge=10, le=500)


@app.post("/harvest/domain")
async def harvest_domain(req: HarvestRequest):
    """
    Run theHarvester against a domain. Promotes found emails, hosts and
    IPs to Neo4j as Email / Domain / IP nodes.
    """
    domain = _validate_domain(req.domain)

    try:
        async with httpx.AsyncClient(timeout=130.0) as client:
            resp = await client.post(
                f"{_HARVESTER_URL}/harvest",
                json={"domain": domain, "sources": req.sources, "limit": req.limit},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, "theHarvester service unavailable — is it running?")
    except httpx.HTTPStatusError as e:
        raise HTTPException(500, f"theHarvester error: {e}")

    emails  = data.get("emails",  [])
    hosts   = data.get("hosts",   [])
    ips     = data.get("ips",     [])
    promoted = {"emails": 0, "domains": 0, "ips": 0}

    async with graph_db.driver.session() as session:
        for email in emails[:200]:
            email = email.strip().lower()
            if "@" not in email:
                continue
            await session.run(
                "MERGE (e:Email {id: $id}) "
                "ON CREATE SET e.address = $id, e.source = 'theharvester', e.first_seen = datetime()",
                id=email,
            )
            promoted["emails"] += 1

        for host in hosts[:200]:
            host = host.strip().lower()
            if not host:
                continue
            await session.run(
                "MERGE (d:Domain {id: $id}) "
                "ON CREATE SET d.name = $id, d.source = 'theharvester', d.first_seen = datetime() "
                "ON MATCH SET d.last_seen = datetime()",
                id=host,
            )
            promoted["domains"] += 1

        for ip in ips[:100]:
            ip = ip.strip()
            if not ip:
                continue
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                continue
            await session.run(
                "MERGE (i:IP {id: $id}) "
                "ON CREATE SET i.address = $id, i.source = 'theharvester', i.first_seen = datetime()",
                id=ip,
            )
            promoted["ips"] += 1

    return {**data, "promoted": promoted}


# ============================================================
# Media analysis — ExifTool / reverse image / video frames
# ============================================================

@app.post("/analyze/media")
async def analyze_media_endpoint(file: UploadFile = File(...)):
    """
    Upload any file (image, video, PDF, Office doc).
    ExifTool extracts metadata; GPS coordinates create a Location node.
    """
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "upload").suffix,
                                     delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        result = await analyse_media(graph_db, tmp_path, file.filename or "upload")
    finally:
        os.unlink(tmp_path)

    return result


class ImageSearchRequest(BaseModel):
    image_url: str = Field(..., min_length=5, max_length=2000)


@app.post("/analyze/image/reverse")
async def reverse_image_endpoint(req: ImageSearchRequest):
    """
    Given a public image URL, return browser-openable reverse-image search
    links (Yandex, Google, TinEye, Bing) plus SauceNAO results if configured.
    """
    links   = reverse_image_links(req.image_url)
    saucenao = await saucenao_search(req.image_url)
    return {"image_url": req.image_url, "search_links": links, "saucenao": saucenao}


@app.post("/analyze/video/frames")
async def video_frames_endpoint(file: UploadFile = File(...),
                                 max_frames: int = 8):
    """
    Upload a video. ffmpeg extracts up to *max_frames* keyframes and
    returns reverse-image search links for each frame.
    """
    if max_frames < 1 or max_frames > 20:
        raise HTTPException(400, "max_frames must be 1-20")

    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        frames = await extract_frames_and_search(tmp_path, max_frames=max_frames)
    finally:
        os.unlink(tmp_path)

    return {"filename": file.filename, "frames": frames}


# ============================================================
# Username scan (Maigret)
# ============================================================
class UsernameScanRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    person_id: Optional[str] = Field(None, max_length=200)
    top_sites: int = Field(100, ge=10, le=500)


_HANDLE_RE = re.compile(r"^[a-zA-Z0-9._\-]{1,50}$")


@app.post("/scan/username")
async def scan_username(req: UsernameScanRequest):
    """
    Search a username across 3000+ sites via Maigret.
    If person_id is supplied, promotes found Account nodes to the graph.
    """
    if not _HANDLE_RE.match(req.username):
        raise HTTPException(400, "Username contains invalid characters")

    if req.person_id and (
        not re.match(r"^[a-z0-9_]+$", req.person_id) or len(req.person_id) > 200
    ):
        raise HTTPException(400, "Invalid person_id")

    client = MaigretClient()
    try:
        results = await client.search(req.username, top_sites=req.top_sites)
    except httpx.ConnectError:
        raise HTTPException(503, "Maigret service unavailable — is it running?")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 504:
            raise HTTPException(504, "Maigret scan timed out — try fewer top_sites")
        raise HTTPException(500, f"Maigret error: {e}")
    except Exception as e:
        log.exception("Maigret scan failed")
        raise HTTPException(500, f"Scan failed: {e}")

    promoted = 0
    if req.person_id:
        async with graph_db.driver.session() as session:
            for account in results.get("accounts", []):
                url = (account.get("url") or "").strip()
                site = account.get("site", "")
                if not url:
                    continue
                await session.run(
                    "MERGE (a:Account {id: $id}) "
                    "ON CREATE SET a.url = $url, a.platform = $platform, "
                    "              a.username = $handle, a.first_seen = datetime() "
                    "WITH a MATCH (p:Person {id: $pid}) "
                    "MERGE (p)-[r:HAS_ACCOUNT]->(a) "
                    "ON CREATE SET r.source = 'maigret', r.first_seen = datetime()",
                    id=url, url=url, platform=site, handle=req.username, pid=req.person_id,
                )
                promoted += 1

    return {**results, "promoted": promoted}


# ============================================================
# Graph quality — duplicate detection + merge (Phase 5)
# ============================================================

class MergeRequest(BaseModel):
    keep_id:   str = Field(..., min_length=1, max_length=200)
    delete_id: str = Field(..., min_length=1, max_length=200)


class DismissRequest(BaseModel):
    id_a: str = Field(..., min_length=1, max_length=200)
    id_b: str = Field(..., min_length=1, max_length=200)


def _valid_node_id(v: str) -> str:
    if not re.match(r"^[a-z0-9_:]+$", v) or len(v) > 200:
        raise HTTPException(400, "Invalid node id")
    return v


@app.get("/graph/health")
async def graph_health():
    """Node/relationship counts, pending duplicates, orphan count."""
    return await get_graph_health(graph_db)


@app.post("/graph/dedup/run")
async def dedup_run():
    """
    Scan the Person graph for likely duplicates.
    Creates POSSIBLE_DUPLICATE relationships with confidence scores.
    Safe to call repeatedly — skips already-known pairs.
    """
    result = await run_duplicate_detection(graph_db)
    return result


@app.get("/graph/dedup/candidates")
async def dedup_candidates():
    """Return open duplicate candidate pairs, ordered by confidence."""
    candidates = await get_duplicate_candidates(graph_db)
    return {"candidates": candidates, "count": len(candidates)}


@app.post("/graph/merge")
async def graph_merge(req: MergeRequest):
    """
    Merge two Person nodes. All relationships from *delete_id* are
    re-pointed to *keep_id* via APOC. The deleted node is removed.
    """
    keep   = _valid_node_id(req.keep_id)
    delete = _valid_node_id(req.delete_id)
    if keep == delete:
        raise HTTPException(400, "keep_id and delete_id must differ")
    try:
        node = await merge_persons(graph_db, keep, delete)
        return {"merged": True, "surviving_node": node}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        log.exception("Merge failed")
        raise HTTPException(500, f"Merge failed: {e}")


@app.post("/graph/dedup/dismiss")
async def dedup_dismiss(req: DismissRequest):
    """Mark a pair as confirmed distinct — removes from candidate list permanently."""
    await dismiss_duplicate(graph_db, _valid_node_id(req.id_a), _valid_node_id(req.id_b))
    return {"dismissed": True}


# ============================================================
# NLP — manual text analysis endpoint
# ============================================================

class TextAnalysisRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=100_000)
    translate: bool = Field(False)


@app.post("/analyze/text")
async def analyze_text(req: TextAnalysisRequest):
    """
    Run the full NLP pipeline (language detection, NER, sentiment,
    optional translation) on arbitrary text.
    Useful for pasting article bodies, document extracts, or social posts.
    """
    result = await process_text(req.text, translate=req.translate)
    return result


# ============================================================
# Phase 6 — New data source endpoints
# ============================================================

# ── Aircraft / Flight (OpenSky + FAA) ─────────────────────────────
@app.get("/enrich/aircraft/faa/{registration}")
async def enrich_faa(registration: str):
    """FAA N-Number registry lookup → Aircraft node."""
    if not re.match(r"^N?[0-9]{1,5}[A-Z]{0,2}$", registration.upper()):
        raise HTTPException(400, "Invalid FAA registration format (e.g. N12345 or N123AB)")
    return await enrich_aircraft_faa(graph_db, registration)


@app.get("/enrich/aircraft/flights/{icao24}")
async def enrich_flights(icao24: str, days: int = 7):
    """OpenSky flight history for an ICAO24 hex address → Flight nodes."""
    if not re.match(r"^[0-9a-fA-F]{6}$", icao24):
        raise HTTPException(400, "ICAO24 must be exactly 6 hex characters (e.g. a835af)")
    if not 1 <= days <= 30:
        raise HTTPException(400, "days must be 1-30")
    return await enrich_aircraft_flights(graph_db, icao24.lower(), days)


# ── Dark web search (Ahmia.fi) ─────────────────────────────────────
class AhmiaRequest(BaseModel):
    query:       str = Field(..., min_length=1, max_length=200)
    max_results: int = Field(20, ge=1, le=50)


@app.post("/search/darkweb")
async def darkweb_search(req: AhmiaRequest):
    """Search Ahmia.fi dark web index. No Tor required."""
    return await search_ahmia(graph_db, req.query.strip(), req.max_results)


# ── Telegram public channel scraper ───────────────────────────────
class TelegramRequest(BaseModel):
    channel:        str = Field(..., min_length=1, max_length=100)
    pages:          int = Field(3, ge=1, le=10)
    keyword_filter: Optional[str] = Field(None, max_length=100)


@app.post("/harvest/telegram")
async def harvest_telegram(req: TelegramRequest):
    """Scrape a public Telegram channel (no login). Runs NER on posts."""
    channel = re.sub(r"[^a-zA-Z0-9_]", "", req.channel.lstrip("@"))
    if not channel:
        raise HTTPException(400, "Invalid channel name")
    return await scrape_telegram_channel(
        graph_db, channel, req.pages, req.keyword_filter
    )


# ── HIBP breach check ─────────────────────────────────────────────
@app.get("/enrich/email/{email}/hibp")
async def hibp_email(email: str):
    """HaveIBeenPwned check for a specific email. Requires HIBP_API_KEY."""
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 254:
        raise HTTPException(400, "Invalid email address")
    return await check_email_hibp(graph_db, email.lower())


@app.get("/enrich/domain/{domain}/hibp")
async def hibp_domain(domain: str):
    """Check if a domain appears in any HIBP breach. Free, no key required."""
    return await check_domain_hibp(graph_db, _validate_domain(domain))


# ── Arkham crypto wallet enrichment ───────────────────────────────
@app.get("/enrich/wallet/{address}/arkham")
async def arkham_wallet(address: str):
    """Arkham Intelligence wallet lookup → entity mapping. Requires ARKHAM_API_KEY."""
    # Accept ETH (0x...), Bitcoin, Solana addresses
    address = address.strip()
    if len(address) < 25 or len(address) > 100:
        raise HTTPException(400, "Invalid wallet address length")
    return await enrich_wallet_arkham(graph_db, address)


# ============================================================
# SpiderFoot integration
# ============================================================
class SpiderFootScanRequest(BaseModel):
    scanname: str = Field(..., min_length=1, max_length=200)
    target: str = Field(..., min_length=1, max_length=500)
    target_type: str = Field("DOMAIN_NAME", max_length=50)
    usecase: str = Field("passive")


class SpiderFootPromoteRequest(BaseModel):
    scan_id: str = Field(..., min_length=8, max_length=100)
    case_id: Optional[str] = Field(None, max_length=100)
    target_node_id: Optional[str] = Field(None, max_length=200)


@app.post("/spiderfoot/startscan")
async def spiderfoot_start_scan(req: SpiderFootScanRequest):
    client = SpiderFootClient()
    try:
        scan_id = await client.start_scan(req.scanname, req.target, req.target_type, req.usecase)
        return {"scan_id": scan_id, "name": req.scanname}
    except Exception as e:
        log.exception("SpiderFoot start failed")
        raise HTTPException(500, f"SpiderFoot start failed: {e}")


@app.get("/spiderfoot/scan/{scan_id}/status")
async def spiderfoot_scan_status(scan_id: str):
    if not re.match(r"^[A-Za-z0-9_-]+$", scan_id) or len(scan_id) > 100:
        raise HTTPException(400, "Invalid scan_id")
    client = SpiderFootClient()
    try:
        return await client.scan_status(scan_id)
    except Exception as e:
        raise HTTPException(500, f"Status fetch failed: {e}")


@app.get("/spiderfoot/scan/{scan_id}/results")
async def spiderfoot_scan_results(scan_id: str):
    if not re.match(r"^[A-Za-z0-9_-]+$", scan_id) or len(scan_id) > 100:
        raise HTTPException(400, "Invalid scan_id")
    client = SpiderFootClient()
    try:
        events = await client.scan_results(scan_id)
        return {"scan_id": scan_id, "events": events, "count": len(events)}
    except Exception as e:
        raise HTTPException(500, f"Results fetch failed: {e}")


@app.get("/spiderfoot/scans")
async def spiderfoot_list_scans():
    client = SpiderFootClient()
    try:
        scans = await client.list_scans()
        return {"scans": scans}
    except Exception as e:
        raise HTTPException(500, f"Scan list failed: {e}")


@app.post("/spiderfoot/promote")
async def spiderfoot_promote(req: SpiderFootPromoteRequest):
    """Promote SpiderFoot scan findings into the Neo4j graph."""
    try:
        result = await promote_scan_to_graph(graph_db, req.scan_id, req.target_node_id)
        return result
    except Exception as e:
        log.exception("Promote failed")
        raise HTTPException(500, f"Promote failed: {e}")


# ============================================================
# Phase 7 — Continuous monitoring
# ============================================================

class WatchRequest(BaseModel):
    name:           str = Field(..., min_length=1, max_length=200)
    interval_hours: int = Field(24, ge=1, le=168)   # 1 h – 1 week


class SeenRequest(BaseModel):
    alert_ids: List[str] = Field(default_factory=list)


_WATCH_ID_RE = re.compile(r"^watch_[a-z0-9_]+$")


def _validate_watch_id(wid: str) -> str:
    if not _WATCH_ID_RE.match(wid) or len(wid) > 200:
        raise HTTPException(400, "Invalid watch_id")
    return wid


@app.post("/monitor/watch")
async def monitor_add_watch(req: WatchRequest):
    """
    Start monitoring a person by name.
    Creates a WatchedSubject node and takes an initial connection snapshot.
    Alerts will fire on the next scheduled check when new connections appear.
    """
    watch = await add_watch(graph_db, req.name.strip(), req.interval_hours)
    return {"watch": watch}


@app.delete("/monitor/watch/{watch_id}")
async def monitor_remove_watch(watch_id: str):
    """Deactivate a WatchedSubject (soft delete — history is preserved)."""
    wid = _validate_watch_id(watch_id)
    removed = await remove_watch(graph_db, wid)
    if not removed:
        raise HTTPException(404, "Watch not found")
    return {"removed": True, "watch_id": wid}


@app.get("/monitor/watches")
async def monitor_list_watches():
    """List all WatchedSubject nodes (active and inactive)."""
    watches = await list_watches(graph_db)
    return {"watches": watches, "count": len(watches)}


@app.post("/monitor/run")
async def monitor_run_now():
    """
    Manually trigger the monitoring cycle for ALL active watches immediately,
    regardless of their scheduled interval. Useful for testing or catching up.
    Re-crawls each watched subject and creates Alert nodes for new connections.
    """
    try:
        result = await run_all_active(graph_db)
        return result
    except Exception as e:
        log.exception("Manual monitor run failed")
        raise HTTPException(500, f"Monitor run failed: {e}")


@app.get("/monitor/alerts")
async def monitor_get_alerts(limit: int = 50):
    """Return the most recent Alert nodes, newest first."""
    if not 1 <= limit <= 200:
        raise HTTPException(400, "limit must be 1-200")
    alerts = await get_alerts(graph_db, limit=limit)
    return {"alerts": alerts, "count": len(alerts)}


@app.get("/monitor/alerts/count")
async def monitor_alert_count():
    """Return the count of unseen Alert nodes (for the header badge)."""
    n = await get_unseen_count(graph_db)
    return {"unseen": n}


@app.post("/monitor/alerts/seen")
async def monitor_mark_seen(req: SeenRequest):
    """
    Mark alerts as seen. Pass specific IDs in alert_ids to mark those;
    pass an empty list to mark ALL unseen alerts as seen.
    """
    # Validate IDs if provided
    for aid in req.alert_ids:
        if not re.match(r"^alert_[a-f0-9]+$", aid) or len(aid) > 80:
            raise HTTPException(400, f"Invalid alert_id: {aid!r}")
    count = await mark_alerts_seen(graph_db, req.alert_ids)
    return {"marked_seen": count}


# ============================================================
# Phase 8 — Entity Pivot Engine
# ============================================================

@app.get("/entity/search")
async def entity_search_endpoint(q: str, limit: int = 30):
    """
    Universal full-text search across ALL node types (Person, Company, Email,
    Domain, IP, Wallet, Breach, Aircraft, etc.).
    Results are ranked by investigative weight then display name.
    Minimum query length: 2 characters.
    """
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(400, "q must be at least 2 characters")
    if not 1 <= limit <= 100:
        raise HTTPException(400, "limit must be 1-100")
    results = await search_all_entities(graph_db, q, limit=limit)
    return {"query": q, "results": results, "count": len(results)}


@app.get("/entity/summary")
async def entity_summary_endpoint(id: str, label: str = ""):
    """
    Fetch a node's properties, label, and connection degree.
    Pass ?label=Person to disambiguate when the same id exists across types.
    If label is omitted, all labels are probed in priority order.
    """
    if not id or len(id) > 300:
        raise HTTPException(400, "id is required (max 300 chars)")
    result = await entity_summary(graph_db, id, label=label or None)
    if not result:
        raise HTTPException(404, "Entity not found in graph")
    return result


@app.get("/entity/pivot")
async def entity_pivot_endpoint(
    id: str,
    label: str = "",
    depth: int = 1,
    max_per_hop: int = 50,
):
    """
    BFS pivot from any node. Returns neighbors at each hop, sorted by
    investigative weight × degree. Depth 1 = direct connections only;
    depth 2 = two hops (slower but reveals indirect links).

    - max 3 hops; max 100 nodes per hop.
    """
    if not id or len(id) > 300:
        raise HTTPException(400, "id is required (max 300 chars)")
    if not 1 <= depth <= 3:
        raise HTTPException(400, "depth must be 1-3")
    if not 1 <= max_per_hop <= 100:
        raise HTTPException(400, "max_per_hop must be 1-100")
    result = await pivot_from(
        graph_db, id,
        label=label or None,
        depth=depth,
        max_per_hop=max_per_hop,
    )
    if not result["source"]:
        raise HTTPException(404, "Entity not found in graph")
    return result


@app.get("/entity/suggestions")
async def entity_suggestions_endpoint(id: str, label: str = ""):
    """
    Return ranked next-step recommendations for a pivot point.
    Rules fire on the presence or absence of particular neighbor labels —
    e.g. "Email present but no Breach → suggest HIBP check".
    """
    if not id or len(id) > 300:
        raise HTTPException(400, "id is required (max 300 chars)")
    suggestions = await pivot_suggestions(graph_db, id, label=label or None)
    return {"id": id, "suggestions": suggestions, "count": len(suggestions)}


# ============================================================
# Phase 10 — Graph Visualization
# ============================================================

@app.get("/graph/viz")
async def graph_viz_endpoint(
    center: str = "",
    label: str = "",
    depth: int = 2,
    limit: int = 150,
):
    """
    Return a Cytoscape-compatible subgraph payload.

    - With *center*: BFS from that entity up to *depth* hops.
    - Without *center*: random sample of the entire graph.
    """
    if center:
        if len(center) > 300:
            raise HTTPException(400, "center id too long")
        return await subgraph_around(
            graph_db, center,
            label=label or None,
            depth=max(1, min(depth, 3)),
            limit=max(10, min(limit, 300)),
        )
    return await full_graph_sample(graph_db, limit=max(20, min(limit, 500)))


@app.get("/graph/expand")
async def graph_expand_endpoint(entity_id: str, label: str = ""):
    """
    1-hop expansion from *entity_id*.
    Returns nodes + edges for merging into an existing canvas.
    """
    if not entity_id or len(entity_id) > 300:
        raise HTTPException(400, "entity_id is required (max 300 chars)")
    return await gv_expand_node(graph_db, entity_id, label=label or None)


# ============================================================
# Phase 12 — Geospatial Map View
# ============================================================

@app.get("/map/locations")
async def map_locations_endpoint(limit: int = 500):
    """
    Return all Location nodes split into geolocated (lat/lng present)
    and ungeolocated (address-only) lists, each with connected-entity summaries.
    """
    return await get_locations(graph_db, limit=max(10, min(limit, 1000)))


@app.post("/map/geocode/{location_id}")
async def map_geocode_endpoint(location_id: str):
    """
    Attempt Nominatim (OpenStreetMap) geocoding for a single Location node.
    On success the node's lat/lng properties are updated in the graph and
    returned in the response so the frontend can move the marker immediately.
    """
    if not location_id or len(location_id) > 300:
        raise HTTPException(400, "location_id required (max 300 chars)")
    result = await geocode_location(graph_db, location_id)
    if result is None:
        raise HTTPException(404, "Location not found in graph")
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result


class IPGeoRequest(BaseModel):
    ip: str = Field(..., min_length=3, max_length=45)

@app.post("/map/geolocate-ip")
async def map_geolocate_ip(req: IPGeoRequest, user: dict = Depends(get_current_user)):
    """
    Geolocate an IP address via ip-api.com (free, no key needed).
    Creates a Location node and links it to the IP entity if one exists.
    """
    result = await geolocate_ip(graph_db, req.ip)
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result


class ManualLocationRequest(BaseModel):
    name:    str            = Field("", max_length=200)
    address: str            = Field("", max_length=400)
    lat:     Optional[float] = None
    lng:     Optional[float] = None

@app.post("/map/location/add")
async def map_add_location(req: ManualLocationRequest, user: dict = Depends(get_current_user)):
    """
    Manually add a Location node.
    Supply lat+lng for an instant pin, or just name/address to auto-geocode via Nominatim.
    """
    result = await add_manual_location(
        graph_db,
        name=req.name,
        address=req.address,
        lat=req.lat,
        lng=req.lng,
    )
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result


# ============================================================
# Phase 13 — Multi-user Auth
# ============================================================

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    email:    str = Field(..., max_length=200)
    password: str = Field(..., min_length=8)

class LoginRequest(BaseModel):
    username: str
    password: str

class RoleUpdateRequest(BaseModel):
    role: str

class ShareRequest(BaseModel):
    username: str


@app.post("/auth/register", status_code=201)
async def auth_register(req: RegisterRequest):
    """Register a new user. The first user automatically becomes admin."""
    try:
        user = await register_user(graph_db, req.username, req.email, req.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    token = create_access_token({"sub": user["id"], "username": user["username"], "role": user["role"]})
    await audit_log(graph_db, action="user.register", user_id=user["id"],
                    username=user["username"], detail=f"role={user['role']}")
    return {"access_token": token, "token_type": "bearer",
            "user": {k: v for k, v in user.items() if k != "password_hash"}}


@app.post("/auth/login")
async def auth_login(req: LoginRequest):
    """Authenticate and receive a Bearer token."""
    user = await get_user_by_username(graph_db, req.username)
    if not user or not verify_password(req.password, user.get("password_hash", "")):
        raise HTTPException(401, "Invalid username or password")
    if not user.get("active", True):
        raise HTTPException(403, "Account is deactivated")
    token = create_access_token({"sub": user["id"], "username": user["username"], "role": user["role"]})
    return {"access_token": token, "token_type": "bearer",
            "username": user["username"], "role": user["role"]}


@app.get("/auth/me")
async def auth_me(user: dict = Depends(get_current_user)):
    """Return the current user's profile."""
    return {"id": user["sub"], "username": user["username"], "role": user["role"]}


@app.get("/auth/users")
async def auth_list_users(_admin: dict = Depends(require_admin)):
    """Admin: list all registered users."""
    return {"users": await list_all_users(graph_db)}


@app.patch("/auth/users/{user_id}/role")
async def auth_update_role(user_id: str, req: RoleUpdateRequest,
                           _admin: dict = Depends(require_admin)):
    """Admin: promote or demote a user."""
    if req.role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of: {', '.join(sorted(VALID_ROLES))}")
    ok = await update_user_role(graph_db, user_id, req.role)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"updated": True}


@app.delete("/auth/users/{user_id}")
async def auth_deactivate_user(user_id: str, admin: dict = Depends(require_admin)):
    """Admin: deactivate a user (soft delete — data is preserved)."""
    if user_id == admin["sub"]:
        raise HTTPException(400, "Cannot deactivate your own account")
    ok = await deactivate_user(graph_db, user_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"deactivated": True}


@app.get("/audit/case/{case_id}")
async def audit_case_log(case_id: str, limit: int = 50,
                         _user: dict = Depends(get_current_user)):
    """Return the audit log for a specific case."""
    return {"events": await get_audit_log(graph_db, target_id=_val_case_id(case_id), limit=limit)}


@app.get("/audit/me")
async def audit_my_log(limit: int = 50, user: dict = Depends(get_current_user)):
    """Return the current user's action history."""
    return {"events": await get_audit_log(graph_db, user_id=user["sub"], limit=limit)}


# ============================================================
# Phase 9 — Investigation CRM
# ============================================================

_CASE_ID_RE = re.compile(r"^case_[a-z0-9_]+_\d{14}$")
_NOTE_ID_RE = re.compile(r"^note_[a-f0-9]{16}$")
_TASK_ID_RE = re.compile(r"^task_[a-f0-9]{16}$")


def _val_case_id(cid: str) -> str:
    if not _CASE_ID_RE.match(cid):
        raise HTTPException(400, "Invalid case_id format")
    return cid


def _val_note_id(nid: str) -> str:
    if not _NOTE_ID_RE.match(nid):
        raise HTTPException(400, "Invalid note_id format")
    return nid


def _val_task_id(tid: str) -> str:
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "Invalid task_id format")
    return tid


# ── Case request models ──────────────────────────────────────────────────────

class CaseCreateRequest(BaseModel):
    title:       str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=5000)
    status:      str = Field("open")
    priority:    str = Field("medium")


class CaseUpdateRequest(BaseModel):
    title:       Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=5000)
    status:      Optional[str] = None
    priority:    Optional[str] = None


class SubjectRequest(BaseModel):
    entity_id:    str = Field(..., min_length=1, max_length=300)
    entity_label: str = Field(..., min_length=1, max_length=50)
    role:         str = Field("unknown")


class NoteRequest(BaseModel):
    content:   str  = Field(..., min_length=1, max_length=10_000)
    note_type: str  = Field("general")
    pinned:    bool = Field(False)


class NotePinRequest(BaseModel):
    pinned: bool


class TaskRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


class TaskToggleRequest(BaseModel):
    completed: bool


# ── Case CRUD endpoints ──────────────────────────────────────────────────────

@app.post("/case")
async def case_create(req: CaseCreateRequest, user: Optional[dict] = Depends(optional_user)):
    """Create a new investigation case. Authenticated users are set as owner."""
    if req.status not in CASE_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(CASE_STATUSES))}")
    if req.priority not in CASE_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {', '.join(sorted(CASE_PRIORITIES))}")
    owner_id = user["sub"] if user else None
    case = await create_case(
        graph_db, req.title.strip(), req.description.strip(),
        req.status, req.priority, owner_id=owner_id,
    )
    if user:
        await audit_log(graph_db, action="case.create", user_id=user["sub"],
                        username=user["username"], target_id=case["id"],
                        target_type="Case", detail=req.title.strip())
    return {"case": case}


@app.get("/case")
async def case_list(include_archived: bool = False, user: Optional[dict] = Depends(optional_user)):
    """List cases. Authenticated users see only their own and shared cases."""
    user_id = user["sub"] if user else None
    cases = await list_cases(graph_db, include_archived=include_archived, user_id=user_id)
    return {"cases": cases, "count": len(cases)}


@app.get("/case/{case_id}")
async def case_get(case_id: str):
    """Get a full case bundle: case metadata + subjects + notes + tasks."""
    bundle = await get_case_full(graph_db, _val_case_id(case_id))
    if not bundle:
        raise HTTPException(404, "Case not found")
    return bundle


@app.patch("/case/{case_id}")
async def case_update(case_id: str, req: CaseUpdateRequest):
    """Update case title, description, status, or priority."""
    if req.status is not None and req.status not in CASE_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(CASE_STATUSES))}")
    if req.priority is not None and req.priority not in CASE_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {', '.join(sorted(CASE_PRIORITIES))}")
    case = await update_case(
        graph_db, _val_case_id(case_id),
        title=req.title, description=req.description,
        status=req.status, priority=req.priority,
    )
    if not case:
        raise HTTPException(404, "Case not found")
    return {"case": case}


@app.delete("/case/{case_id}")
async def case_archive(case_id: str):
    """Archive a case (soft delete — data is preserved in graph)."""
    ok = await archive_case(graph_db, _val_case_id(case_id))
    if not ok:
        raise HTTPException(404, "Case not found")
    return {"archived": True}


# ── Subject endpoints ────────────────────────────────────────────────────────

@app.post("/case/{case_id}/subject")
async def case_add_subject(case_id: str, req: SubjectRequest):
    """Link an existing graph entity to a case as a subject."""
    if req.role not in SUBJECT_ROLES:
        raise HTTPException(400, f"role must be one of: {', '.join(sorted(SUBJECT_ROLES))}")
    if req.entity_label not in SUBJECT_LABELS:
        raise HTTPException(400, f"entity_label must be one of: {', '.join(sorted(SUBJECT_LABELS))}")
    try:
        subject = await add_subject(
            graph_db, _val_case_id(case_id),
            req.entity_id, req.entity_label, req.role,
        )
        return {"subject": subject}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/case/{case_id}/subject/{entity_id:path}")
async def case_remove_subject(case_id: str, entity_id: str):
    """Remove a subject from a case (relationship only — the entity node stays)."""
    ok = await remove_subject(graph_db, _val_case_id(case_id), entity_id)
    if not ok:
        raise HTTPException(404, "Subject relationship not found")
    return {"removed": True}


# ── Note endpoints ───────────────────────────────────────────────────────────

@app.post("/case/{case_id}/note")
async def case_add_note(case_id: str, req: NoteRequest):
    """Add a typed note (finding/lead/hypothesis/caution/general) to a case."""
    if req.note_type not in NOTE_TYPES:
        raise HTTPException(400, f"note_type must be one of: {', '.join(sorted(NOTE_TYPES))}")
    try:
        note = await add_note(
            graph_db, _val_case_id(case_id),
            req.content.strip(), req.note_type, req.pinned,
        )
        return {"note": note}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.patch("/case/{case_id}/note/{note_id}/pin")
async def case_pin_note(case_id: str, note_id: str, req: NotePinRequest):
    """Pin or unpin a note (pinned notes float to the top)."""
    ok = await pin_note(graph_db, _val_case_id(case_id), _val_note_id(note_id), req.pinned)
    if not ok:
        raise HTTPException(404, "Note not found")
    return {"pinned": req.pinned}


@app.delete("/case/{case_id}/note/{note_id}")
async def case_delete_note(case_id: str, note_id: str):
    """Permanently delete a note from a case."""
    ok = await delete_note(graph_db, _val_case_id(case_id), _val_note_id(note_id))
    if not ok:
        raise HTTPException(404, "Note not found")
    return {"deleted": True}


# ── Task endpoints ───────────────────────────────────────────────────────────

@app.post("/case/{case_id}/task")
async def case_add_task(case_id: str, req: TaskRequest):
    """Add a verification task to a case checklist."""
    try:
        task = await add_task(graph_db, _val_case_id(case_id), req.text.strip())
        return {"task": task}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.patch("/case/{case_id}/task/{task_id}")
async def case_toggle_task(case_id: str, task_id: str, req: TaskToggleRequest):
    """Mark a task as completed or reopen it."""
    ok = await toggle_task(
        graph_db, _val_case_id(case_id), _val_task_id(task_id), req.completed
    )
    if not ok:
        raise HTTPException(404, "Task not found")
    return {"completed": req.completed}


@app.delete("/case/{case_id}/task/{task_id}")
async def case_delete_task(case_id: str, task_id: str):
    """Delete a task from a case."""
    ok = await delete_task(graph_db, _val_case_id(case_id), _val_task_id(task_id))
    if not ok:
        raise HTTPException(404, "Task not found")
    return {"deleted": True}


# ── Collaboration endpoints ───────────────────────────────────────────────────

@app.get("/case/{case_id}/collaborators")
async def case_get_collaborators(case_id: str):
    """Return the owner and shared-with users for a case."""
    return await get_case_collaborators(graph_db, _val_case_id(case_id))


@app.post("/case/{case_id}/share")
async def case_share(case_id: str, req: ShareRequest, user: dict = Depends(get_current_user)):
    """Share a case with another user by username."""
    result = await share_case(graph_db, _val_case_id(case_id), req.username)
    if "error" in result:
        raise HTTPException(404, result["error"])
    await audit_log(graph_db, action="case.share", user_id=user["sub"],
                    username=user["username"], target_id=case_id,
                    target_type="Case", detail=f"shared_with={req.username}")
    return result


@app.delete("/case/{case_id}/share/{user_id}")
async def case_unshare(case_id: str, user_id: str, _user: dict = Depends(get_current_user)):
    """Remove a shared-with relationship."""
    ok = await unshare_case(graph_db, _val_case_id(case_id), user_id)
    if not ok:
        raise HTTPException(404, "Share relationship not found")
    return {"removed": True}


# ── Export endpoints ─────────────────────────────────────────────────────────

@app.get("/case/{case_id}/export")
async def case_export(case_id: str, format: str = "json"):
    """
    Export a full case dossier.

    - **format=json** (default): structured JSON with all case data.
    - **format=markdown**: human-readable Markdown report for archiving.
    - **format=pdf**: professional PDF dossier rendered via WeasyPrint.
    """
    bundle = await get_case_full(graph_db, _val_case_id(case_id))
    if not bundle:
        raise HTTPException(404, "Case not found")

    safe_id = case_id.replace("/", "_")

    if format == "markdown":
        md = export_case_markdown(bundle)
        return PlainTextResponse(
            content=md,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="fieldwork-case-{safe_id}.md"'
            },
        )

    if format == "pdf":
        from pdf_export import export_case_pdf
        try:
            pdf_bytes = export_case_pdf(bundle)
        except RuntimeError as exc:
            raise HTTPException(500, str(exc))
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="fieldwork-case-{safe_id}.pdf"'
            },
        )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 16 — Local LLM (Ollama)                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── Entity labels and primary properties ─────────────────────────────────────

_ENTITY_PRIMARY_PROP: dict[str, str] = {
    "Person":        "name",
    "Organization":  "name",
    "Email":         "address",
    "Domain":        "name",
    "IP":            "address",
    "Phone":         "number",
    "Location":      "name",
    "CryptoAddress": "address",
    "Username":      "username",
    "URL":           "url",
}


# ── Request models ────────────────────────────────────────────────────────────

class LLMExtractRequest(BaseModel):
    text:  str  = Field(..., min_length=1, max_length=32_000)
    model: Optional[str] = None


class LLMCaseRequest(BaseModel):
    model: Optional[str] = None


class LLMPullRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=200)


class EntityCreateRequest(BaseModel):
    label:   str = Field(..., min_length=1, max_length=50)
    value:   str = Field(..., min_length=1, max_length=2000)
    context: Optional[str] = Field(None, max_length=500)


# ── Status & model management ─────────────────────────────────────────────────

@app.get("/llm/status")
async def llm_status(_user: dict = Depends(get_current_user)):
    """Return Ollama availability, pulled models, and the configured default."""
    return await ollama_status()


@app.post("/llm/pull")
async def llm_pull(
    req:              LLMPullRequest,
    background_tasks: BackgroundTasks,
    user:             dict = Depends(require_admin),
):
    """Admin-only: pull a model from the Ollama registry in the background."""
    background_tasks.add_task(llm_pull_model, req.model)
    return {
        "pulling": True,
        "model":   req.model,
        "message": "Pull started in background. Poll /llm/status to check progress.",
    }


# ── Entity extraction ─────────────────────────────────────────────────────────

@app.post("/llm/extract")
async def llm_extract(
    req:  LLMExtractRequest,
    user: dict = Depends(get_current_user),
):
    """Extract OSINT entities from free-form text using the local LLM."""
    try:
        entities = await llm_extract_entities(req.text, req.model)
    except Exception as exc:
        log.exception("LLM extract failed")
        raise HTTPException(502, f"Ollama error: {exc}")
    return {"entities": entities, "count": len(entities)}


# ── Case summarisation & hypothesis generation ────────────────────────────────

@app.post("/llm/summarize/{case_id}")
async def llm_summarize(
    case_id: str,
    req:     LLMCaseRequest = Body(default=LLMCaseRequest()),
    user:    dict = Depends(get_current_user),
):
    """Generate a narrative investigation summary for a case."""
    bundle = await get_case_full(graph_db, _val_case_id(case_id))
    if not bundle:
        raise HTTPException(404, "Case not found")
    try:
        summary = await llm_summarize_case(bundle, req.model)
    except Exception as exc:
        log.exception("LLM summarize failed")
        raise HTTPException(502, f"Ollama error: {exc}")
    return {"summary": summary}


@app.post("/llm/hypothesize/{case_id}")
async def llm_hypothesize(
    case_id: str,
    req:     LLMCaseRequest = Body(default=LLMCaseRequest()),
    user:    dict = Depends(get_current_user),
):
    """Generate structured investigation leads and hypotheses for a case."""
    bundle = await get_case_full(graph_db, _val_case_id(case_id))
    if not bundle:
        raise HTTPException(404, "Case not found")
    try:
        hypotheses = await llm_suggest_hypotheses(bundle, req.model)
    except Exception as exc:
        log.exception("LLM hypothesize failed")
        raise HTTPException(502, f"Ollama error: {exc}")
    return {"hypotheses": hypotheses}


# ── Entity creation (used by LLM extract "Add to graph" flow) ────────────────

@app.post("/entity/create")
async def entity_create(
    req:  EntityCreateRequest,
    user: dict = Depends(get_current_user),
):
    """
    Merge a single entity node into the graph.

    Uses MERGE so re-adding an existing entity is a no-op.
    The entity ID is a deterministic sha-256 of (label, lowercased value)
    so the same real-world entity always maps to the same node.
    """
    if req.label not in VALID_ENTITY_TYPES:
        raise HTTPException(400, f"Invalid label. Must be one of: {', '.join(sorted(VALID_ENTITY_TYPES))}")

    entity_id = f"{req.label.lower()}_{hashlib.sha256(req.value.lower().encode()).hexdigest()[:16]}"
    prop      = _ENTITY_PRIMARY_PROP.get(req.label, "name")

    async with graph_db.driver.session() as session:
        await session.run(
            f"MERGE (e:{req.label} {{id: $id}}) "
            f"ON CREATE SET e.{prop} = $value, e.source = 'llm_extract', "
            f"              e.created = timestamp() "
            f"ON MATCH  SET e.updated = timestamp()",
            id=entity_id,
            value=req.value,
        )

    await audit_log(
        graph_db,
        action="entity.create",
        user_id=user["sub"],
        username=user["username"],
        target_id=entity_id,
        target_type=req.label,
        detail=f"value={req.value[:80]}, source=llm_extract",
    )

    return {"id": entity_id, "label": req.label, "value": req.value, "created": True}


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 14 — Timeline                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@app.get("/timeline")
async def global_timeline(
    limit:    int            = 200,
    since:    Optional[str]  = None,
    category: Optional[str]  = None,
    _user:    dict           = Depends(get_current_user),
):
    """
    Global reverse-chronological event feed across all cases and users.

    Query params
    ------------
    limit     Max events to return (default 200, max 500).
    since     Only events after this ISO-8601 timestamp.
    category  Filter to one category: case | note | task | entity | user
    """
    limit = min(limit, 500)
    events = await get_global_timeline(graph_db, limit=limit, since_iso=since, category=category)
    return {"events": events, "count": len(events)}


@app.get("/timeline/case/{case_id}")
async def case_timeline(
    case_id: str,
    limit:   int  = 200,
    _user:   dict = Depends(get_current_user),
):
    """
    Reverse-chronological event stream for a single case.
    Includes audit events, note creation events, and task completions.
    """
    limit  = min(limit, 500)
    events = await get_case_timeline(graph_db, _val_case_id(case_id), limit=limit)
    return {"events": events, "count": len(events)}


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 17 — Document ingestion                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

_DOC_ID_RE = re.compile(r"^doc_[a-f0-9]{20}$")

def _val_doc_id(did: str) -> str:
    if not _DOC_ID_RE.match(did):
        raise HTTPException(400, "Invalid document id")
    return did


@app.post("/case/{case_id}/document")
async def document_upload(
    case_id: str,
    file:    UploadFile = File(...),
    user:    dict       = Depends(get_current_user),
):
    """
    Upload a file and attach it to a case.

    Supported: PDF, DOCX, TXT, MD, CSV, JPG, PNG, TIFF, WebP, EML.
    Max size: 50 MB.

    The response includes extracted text, metadata, EXIF, and spaCy NER entities.
    """
    data = await file.read()
    try:
        doc = await ingest_document(
            graph_db,
            case_id  = _val_case_id(case_id),
            filename = file.filename or "upload",
            data     = data,
            mime     = file.content_type or None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.exception("Document ingestion failed")
        raise HTTPException(500, f"Ingestion error: {exc}")

    await audit_log(
        graph_db,
        action      = "document.upload",
        user_id     = user["sub"],
        username    = user["username"],
        target_id   = case_id,
        target_type = "Case",
        detail      = f"file={file.filename}, size={len(data)}, doc_id={doc['id']}",
    )
    return doc


@app.get("/case/{case_id}/documents")
async def document_list(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """List all documents attached to a case."""
    docs = await list_documents(graph_db, _val_case_id(case_id))
    return {"documents": docs, "count": len(docs)}


@app.get("/document/{doc_id}")
async def document_get(
    doc_id: str,
    _user:  dict = Depends(get_current_user),
):
    """Fetch full document details including extracted text and entities."""
    doc = await get_document(graph_db, _val_doc_id(doc_id))
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc


@app.delete("/case/{case_id}/document/{doc_id}")
async def document_remove(
    case_id: str,
    doc_id:  str,
    user:    dict = Depends(get_current_user),
):
    """Detach a document from a case (the Document node is preserved)."""
    ok = await remove_document(graph_db, _val_case_id(case_id), _val_doc_id(doc_id))
    if not ok:
        raise HTTPException(404, "Document not linked to this case")
    await audit_log(
        graph_db,
        action="document.remove", user_id=user["sub"], username=user["username"],
        target_id=case_id, target_type="Case",
        detail=f"doc_id={doc_id}",
    )
    return {"removed": True}


@app.post("/document/{doc_id}/vt")
async def document_vt_check(
    doc_id: str,
    _user:  dict = Depends(get_current_user),
):
    """Check the document's SHA-256 against VirusTotal and persist the result."""
    doc = await get_document(graph_db, _val_doc_id(doc_id))
    if not doc:
        raise HTTPException(404, "Document not found")
    result = await check_vt_hash(graph_db, doc_id, doc["sha256"])
    return result


# ── Phase 18 — Global full-text search ────────────────────────────────────────

_VALID_CATEGORIES = {"case", "entity", "location", "document", "article"}
_SAFE_QUERY_RE    = re.compile(r'^[\w\s\-@.+":\'*?]{1,300}$')


@app.get("/search")
async def global_search(
    q:        str,
    limit:    int = 40,
    category: Optional[str] = None,
    _user:    dict = Depends(get_current_user),
):
    """Full-text search across all node types in the graph.

    Parameters
    ----------
    q        : search string (1–300 chars, alphanumeric + common punctuation)
    limit    : max results (1–200, default 40)
    category : optional filter — one of case/entity/location/document/article
    """
    q = q.strip()
    if not q:
        raise HTTPException(400, "Query string 'q' is required")
    if not _SAFE_QUERY_RE.match(q):
        raise HTTPException(400, "Query contains disallowed characters")
    if category and category not in _VALID_CATEGORIES:
        raise HTTPException(400, f"category must be one of: {', '.join(sorted(_VALID_CATEGORIES))}")
    limit = max(1, min(limit, 200))

    fuzzy = False  # reserved for future query param
    results = await full_text_search(graph_db, q, limit=limit, category=category or None, fuzzy=fuzzy)
    return {"query": q, "count": len(results), "results": results}


@app.get("/search/fuzzy")
async def fuzzy_search(
    q:        str,
    limit:    int = 40,
    category: Optional[str] = None,
    _user:    dict = Depends(get_current_user),
):
    """Full-text search with fuzzy (edit-distance) matching — finds near-matches and typos."""
    q = q.strip()
    if not q:
        raise HTTPException(400, "Query string 'q' is required")
    limit = max(1, min(limit, 200))
    results = await full_text_search(graph_db, q, limit=limit, category=category or None, fuzzy=True)
    return {"query": q, "count": len(results), "results": results, "mode": "fuzzy"}


@app.get("/search/semantic")
async def semantic_search_endpoint(
    q:     str,
    limit: int = 20,
    _user: dict = Depends(get_current_user),
):
    """AI-powered semantic search using Ollama embeddings + Neo4j vector index."""
    q = q.strip()
    if not q:
        raise HTTPException(400, "Query string 'q' is required")
    limit = max(1, min(limit, 100))
    results = await semantic_search(graph_db, q, limit=limit)
    return {"query": q, "count": len(results), "results": results, "mode": "semantic"}


@app.get("/search/related/{entity_id}")
async def related_search(
    entity_id: str,
    depth:     int = 2,
    limit:     int = 50,
    _user:     dict = Depends(get_current_user),
):
    """Return all entities reachable from entity_id within depth hops."""
    if not entity_id or len(entity_id) > 300:
        raise HTTPException(400, "Invalid entity_id")
    results = await related_entities(graph_db, entity_id, depth=depth, limit=limit)
    return {"entity_id": entity_id, "depth": depth, "count": len(results), "results": results}


@app.post("/search/federated")
async def federated_search_endpoint(
    q:       str,
    sources: Optional[str] = None,   # comma-separated list, or all
    _user:   dict = Depends(get_current_user),
):
    """
    Fire `q` against multiple OSINT sources in parallel and return merged results.
    sources: comma-separated list (shodan,virustotal,hibp,github,news,aleph,ahmia,censys,dehashed)
             defaults to all available sources.
    """
    q = q.strip()
    if not q:
        raise HTTPException(400, "Query string 'q' is required")
    src_list = [s.strip() for s in sources.split(",")] if sources else None
    return await federated_search(graph_db, q, sources=src_list)


@app.post("/search/index")
async def trigger_index(user: dict = Depends(require_admin)):
    """Admin: trigger a background batch-index of all entities for semantic search."""
    asyncio.create_task(batch_index_all(graph_db))
    return {"status": "indexing started in background"}


@app.get("/search/censys")
async def censys_search(
    q:        str,
    per_page: int = 25,
    _user:    dict = Depends(get_current_user),
):
    """Search Censys internet-wide scan data. Requires CENSYS_API_ID / CENSYS_API_SECRET."""
    return await censys_search_hosts(q.strip(), per_page=per_page)


@app.get("/search/dehashed")
async def dehashed_search_endpoint(
    q:     str,
    field: str = "auto",
    size:  int = 20,
    _user: dict = Depends(get_current_user),
):
    """Search Dehashed credential breach database. Requires DEHASHED_EMAIL / DEHASHED_KEY."""
    return await dehashed_search(q.strip(), field=field, size=size)


# ── Phase 19 — Bulk entity import ─────────────────────────────────────────────

_BI_SIZE_LIMIT = 5 * 1024 * 1024  # 5 MB


@app.post("/import/preview")
async def import_preview(
    file:  UploadFile = File(...),
    _user: dict = Depends(get_current_user),
):
    """Parse an uploaded CSV / JSON / TXT file and return up to 100 rows
    for the user to review before committing to the graph."""
    data = await file.read()
    if len(data) > _BI_SIZE_LIMIT:
        raise HTTPException(413, "File too large (max 5 MB)")

    raw, parse_warns = bi_parse_file(file.filename or "", data)
    valid, norm_errs = bi_normalise(raw)

    return {
        "filename":      file.filename,
        "parsed":        len(raw),
        "valid":         len(valid),
        "warnings":      parse_warns + norm_errs,
        "preview":       valid[:100],
        "supported_types": sorted(BI_LABELS),
    }


@app.post("/case/{case_id}/import")
async def case_bulk_import(
    case_id: str,
    file:    UploadFile = File(...),
    link:    bool = True,
    user:    dict = Depends(get_current_user),
):
    """Upload a CSV / JSON / TXT file and batch-MERGE entity nodes into the graph.

    Parameters
    ----------
    link : if true (default) each entity is also added as a subject of this case
    """
    data = await file.read()
    if len(data) > _BI_SIZE_LIMIT:
        raise HTTPException(413, "File too large (max 5 MB)")

    cid = _val_case_id(case_id)

    raw, parse_warns = bi_parse_file(file.filename or "", data)
    if not raw:
        raise HTTPException(
            400,
            f"No parseable rows found. {' '.join(parse_warns) or 'Check file format.'}",
        )

    valid, norm_errs = bi_normalise(raw)
    if not valid:
        raise HTTPException(
            400,
            f"No valid rows after type resolution. Errors: {'; '.join(norm_errs[:5])}",
        )

    result = await bulk_import_entities(
        graph_db, valid,
        case_id=cid if link else None,
        source="bulk_import",
    )

    await audit_log(
        graph_db,
        action="import.bulk",
        user_id=user["sub"], username=user["username"],
        target_id=cid, target_type="Case",
        detail=(
            f"file={file.filename}, parsed={len(raw)}, valid={len(valid)}, "
            f"created={result['created']}, updated={result['updated']}, "
            f"linked={result['linked']}"
        ),
    )

    return {
        "filename": file.filename,
        "parsed":   len(raw),
        "valid":    len(valid),
        "created":  result["created"],
        "updated":  result["updated"],
        "linked":   result["linked"],
        "warnings": parse_warns + norm_errs + result["errors"],
        "nodes":    result["nodes"],
    }


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 20 — Dashboard Analytics                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@app.get("/dashboard")
async def dashboard_stats(_user: dict = Depends(get_current_user)):
    """
    Full dashboard payload in one request: totals, entity breakdown,
    case stats, risk summary, recent activity, top-connected nodes.
    All queries run in parallel — target < 200 ms on a typical database.
    """
    return await get_dashboard_stats(graph_db)


@app.get("/dashboard/recent")
async def dashboard_recent(
    limit: int = 20,
    _user: dict = Depends(get_current_user),
):
    """Return the most recently created/updated entity nodes."""
    limit = max(1, min(limit, 100))
    return {"recent": await get_recent_activity(graph_db, limit=limit)}


@app.get("/dashboard/top-connected")
async def dashboard_top_connected(
    limit: int = 10,
    _user: dict = Depends(get_current_user),
):
    """Return the N most-connected entity nodes by relationship degree."""
    limit = max(1, min(limit, 50))
    return {"top_connected": await get_top_connected(graph_db, limit=limit)}


@app.get("/dashboard/timeline")
async def dashboard_timeline(
    days: int = 30,
    _user: dict = Depends(get_current_user),
):
    """Return daily entity-creation counts for the last N days (for the sparkline chart)."""
    days = max(7, min(days, 365))
    return {"timeline": await get_entity_timeline(graph_db, days=days)}


# ── Risk scoring ──────────────────────────────────────────────────────────────

@app.post("/risk/score/{entity_id:path}")
async def risk_score_entity(
    entity_id: str,
    _user: dict = Depends(get_current_user),
):
    """
    Compute and persist a risk score (0-100) for a single entity.
    Reads existing graph signals — no external API calls.
    """
    if not entity_id or len(entity_id) > 300:
        raise HTTPException(400, "entity_id required (max 300 chars)")
    result = await score_entity(graph_db, entity_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@app.post("/risk/score-all")
async def risk_score_all(user: dict = Depends(require_admin)):
    """
    Admin: bulk-score every entity node in the graph.
    Runs in the background — returns immediately with a job summary
    once all entities have been scored.
    """
    result = await score_all_entities(graph_db)
    return {"status": "complete", **result}


@app.get("/risk/high")
async def risk_high_entities(
    limit: int = 20,
    _user: dict = Depends(get_current_user),
):
    """Return the N highest-risk entities, sorted by risk_score DESC."""
    limit = max(1, min(limit, 100))
    entities = await get_high_risk_entities(graph_db, limit=limit)
    return {"entities": entities, "count": len(entities)}


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 21 — Email Header Analyzer                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class EmailHeaderRequest(BaseModel):
    raw: str = Field(..., min_length=10, max_length=200_000,
                     description="Raw email headers pasted verbatim")


@app.post("/analyze/email-headers")
async def analyze_email_headers_endpoint(
    req:  EmailHeaderRequest,
    _user: dict = Depends(get_current_user),
):
    """
    Forensic analysis of raw email headers.

    Pass the full raw header block (everything above the body).
    Returns:
      - Received hop chain with inter-hop delays
      - SPF / DKIM / DMARC authentication results
      - Originating IP (X-Originating-IP or first public hop)
      - Detected anomalies (spoofing indicators, timestamp forgery, etc.)
      - All public IPs and relay domains — MERGEd into Neo4j as nodes
    """
    try:
        result = await analyze_email_headers(graph_db, req.raw)
        return result
    except Exception as exc:
        log.exception("Email header analysis failed")
        raise HTTPException(500, f"Analysis failed: {exc}")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 22 — Manual Relationship Builder                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Whitelist of allowed manual relationship types (no Cypher injection)
_MANUAL_REL_TYPES = {
    "CONNECTED_TO", "WORKS_WITH", "OWNS", "CONTROLS", "RELATED_TO",
    "COMMUNICATES_WITH", "FUNDED_BY", "ASSOCIATED_WITH", "PART_OF",
    "ALIAS_OF", "LINKED_TO", "COLLABORATES_WITH", "REPORTED_BY",
    "MENTIONED_WITH", "POSSIBLY_SAME_AS",
}


class ManualRelRequest(BaseModel):
    source_id:  str = Field(..., min_length=1, max_length=300)
    target_id:  str = Field(..., min_length=1, max_length=300)
    rel_type:   str = Field("CONNECTED_TO", min_length=1, max_length=50)
    note:       str = Field("", max_length=500)
    directed:   bool = Field(True, description="If false, create relationship in both directions")


@app.post("/graph/relationship")
async def create_manual_relationship(
    req:  ManualRelRequest,
    user: dict = Depends(get_current_user),
):
    """
    Manually draw a relationship between two existing graph entities.
    The relationship is tagged `manual: true` so it can be styled differently
    in the graph view and removed without affecting auto-crawled data.
    """
    rel = req.rel_type.upper().replace(" ", "_")
    if rel not in _MANUAL_REL_TYPES:
        raise HTTPException(
            400,
            f"rel_type must be one of: {', '.join(sorted(_MANUAL_REL_TYPES))}"
        )

    async with graph_db.driver.session() as session:
        # Verify both nodes exist
        check = await session.run(
            "MATCH (a {id: $src}), (b {id: $tgt}) RETURN count(a) + count(b) AS n",
            src=req.source_id, tgt=req.target_id,
        )
        row = await check.single()
        if not row or row["n"] < 2:
            raise HTTPException(404, "One or both entities not found in graph")

        # Create directed relationship
        await session.run(
            f"MATCH (a {{id: $src}}), (b {{id: $tgt}}) "
            f"MERGE (a)-[r:{rel} {{manual: true}}]->(b) "
            f"ON CREATE SET r.note = $note, r.created_by = $user, r.created_at = datetime() "
            f"ON MATCH  SET r.note = $note",
            src=req.source_id, tgt=req.target_id,
            note=req.note, user=user["username"],
        )

        # If undirected, also create the reverse edge
        if not req.directed:
            await session.run(
                f"MATCH (a {{id: $src}}), (b {{id: $tgt}}) "
                f"MERGE (b)-[r:{rel} {{manual: true}}]->(a) "
                f"ON CREATE SET r.note = $note, r.created_by = $user, r.created_at = datetime()",
                src=req.source_id, tgt=req.target_id,
                note=req.note, user=user["username"],
            )

    await audit_log(
        graph_db,
        action="graph.relationship.create",
        user_id=user["sub"], username=user["username"],
        target_id=req.source_id,
        detail=f"{req.source_id} --[{rel}]--> {req.target_id} note={req.note[:40]}",
    )

    return {
        "created": True,
        "source_id": req.source_id,
        "target_id": req.target_id,
        "rel_type":  rel,
        "directed":  req.directed,
    }


@app.delete("/graph/relationship")
async def delete_manual_relationship(
    source_id: str,
    target_id: str,
    rel_type:  str  = "CONNECTED_TO",
    user:      dict = Depends(get_current_user),
):
    """Delete a manually-created relationship between two nodes."""
    rel = rel_type.upper().replace(" ", "_")
    if rel not in _MANUAL_REL_TYPES:
        raise HTTPException(400, f"Invalid rel_type: {rel}")

    async with graph_db.driver.session() as session:
        result = await session.run(
            f"MATCH (a {{id: $src}})-[r:{rel} {{manual: true}}]->(b {{id: $tgt}}) "
            f"DELETE r RETURN count(r) AS deleted",
            src=source_id, tgt=target_id,
        )
        row = await result.single()
        deleted = row["deleted"] if row else 0

    if not deleted:
        raise HTTPException(404, "Manual relationship not found")

    await audit_log(
        graph_db,
        action="graph.relationship.delete",
        user_id=user["sub"], username=user["username"],
        target_id=source_id,
        detail=f"{source_id} --[{rel}]--> {target_id}",
    )
    return {"deleted": True, "count": deleted}


@app.get("/graph/relationship-types")
async def list_rel_types(_user: dict = Depends(get_current_user)):
    """Return the list of valid manual relationship types."""
    return {"rel_types": sorted(_MANUAL_REL_TYPES)}


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 23 — Phone Number Intelligence                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Accepts E.164, national, or local format — the library handles normalisation
_PHONE_RAW_RE = re.compile(r'^[\+\d\s\(\)\-\.]{4,25}$')


@app.get("/enrich/phone/{number:path}")
async def enrich_phone_endpoint(
    number: str,
    _user:  dict = Depends(get_current_user),
):
    """
    Parse and enrich a phone number.

    Accepts most formats: +1 555 123 4567 · +44 7911 123456 · 07911123456

    Returns:
      - E.164 canonical form, national format
      - Country, region, carrier (offline library — portable numbers may differ)
      - Line type: MOBILE / FIXED_LINE / VOIP / TOLL_FREE / etc.
      - Timezones associated with the number prefix
      - Investigation resource links (Truecaller, Spy Dialer, etc.)
      - NumVerify live lookup if NUMVERIFY_KEY is set
      - Phone node created/updated in Neo4j
    """
    number = number.strip()
    if not _PHONE_RAW_RE.match(number):
        raise HTTPException(400, "Invalid phone number format")

    result = await enrich_phone(graph_db, number)

    if "error" in result and result["error"] and not result.get("valid"):
        raise HTTPException(422, result["error"])

    return result


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 24 — AbuseIPDB IP Reputation                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 25 — Universal Quick Extract                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class QuickExtractRequest(BaseModel):
    text:    str  = Field(..., min_length=1, max_length=50_000)
    use_llm: bool = Field(True, description="Also run LLM extraction (slower, smarter)")


# spaCy label → investigative entity type mapping
_NER_TYPE_MAP = {
    "persons":   "Person",
    "orgs":      "Company",
    "locations": "Location",
}

# Regex extractors for structured indicators not covered by spaCy
import re as _re

_INDICATOR_RE: list[tuple[str, str]] = [
    ("Email",    _re.compile(r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b')),
    ("IP",       _re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')),
    ("Domain",   _re.compile(r'\b(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+(?:com|org|net|io|gov|edu|co|uk|de|ru|cn|info|biz|me|tv|onion)\b', _re.IGNORECASE)),
    ("URL",      _re.compile(r'https?://[^\s<>"\']{4,200}')),
    ("Phone",    _re.compile(r'(?<!\d)(?:\+\d{1,3}[\s\-]?)?(?:\(?\d{2,4}\)?[\s\-]?)?\d{3,4}[\s\-]?\d{3,5}(?!\d)')),
    ("Wallet",   _re.compile(r'\b(?:0x[a-fA-F0-9]{40}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{6,87})\b')),
    ("Username", _re.compile(r'@([a-zA-Z0-9_]{3,30})\b')),
]


@app.post("/analyze/quick-extract")
async def quick_extract_endpoint(
    req:  QuickExtractRequest,
    _user: dict = Depends(get_current_user),
):
    """
    Combined NLP + regex + (optional) LLM entity extraction in one call.

    Runs three extraction passes and deduplicates:
      1. spaCy NER  — persons, organizations, locations
      2. Regex      — emails, IPs, domains, URLs, phone numbers, crypto wallets, @usernames
      3. LLM        — everything spaCy misses, with context (if use_llm=true and Ollama up)

    Returns a flat list of {type, value, source, context} dicts ready
    for one-click graph addition.
    """
    from ner_pipeline import extract_entities as ner_extract
    text = req.text
    seen: dict[tuple, dict] = {}   # (type_lower, value_lower) → entity dict

    def _add(type_: str, value: str, source: str, context: str = ""):
        value = value.strip()
        if not value or len(value) > 500:
            return
        key = (type_.lower(), value.lower())
        if key in seen:
            existing = seen[key]
            if source not in existing["source"]:
                existing["source"] += "+" + source
        else:
            seen[key] = {"type": type_, "value": value, "source": source, "context": context}

    # Pass 1 — spaCy NER
    try:
        loop = asyncio.get_event_loop()
        ner_result = await loop.run_in_executor(None, ner_extract, text)
        for category, items in ner_result.items():
            ent_type = _NER_TYPE_MAP.get(category)
            if ent_type:
                for item in items:
                    _add(ent_type, item, "nlp")
    except Exception as exc:
        log.debug("NER pass failed: %s", exc)

    # Pass 2 — regex indicators
    for type_, pattern in _INDICATOR_RE:
        for m in pattern.finditer(text):
            val = m.group(1) if m.lastindex else m.group(0)
            _add(type_, val, "regex")

    # Pass 3 — LLM (optional)
    if req.use_llm:
        try:
            llm_ents = await llm_extract_entities(text[:12_000])
            for ent in llm_ents:
                _add(
                    ent.get("type", "Unknown"),
                    ent.get("value", ""),
                    "llm",
                    ent.get("context", ""),
                )
        except Exception as exc:
            log.debug("LLM extraction skipped: %s", exc)

    entities = list(seen.values())
    # Sort: multi-source first, then by type priority
    _type_priority = ["Email","IP","Domain","Person","Company","Phone","Wallet","URL","Username","Location"]
    def _sort_key(e):
        src_score = 0 if "+" in e["source"] else 1
        tp = _type_priority.index(e["type"]) if e["type"] in _type_priority else 99
        return (src_score, tp, e["value"].lower())

    entities.sort(key=_sort_key)
    return {"entities": entities, "count": len(entities)}


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 26 — Graph Shortest Path Finder                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@app.get("/paths")
async def find_graph_paths(
    source_id: str,
    target_id:  str,
    max_depth:  int  = 5,
    _user:      dict = Depends(get_current_user),
):
    """
    Find the shortest path(s) between two entities in the graph.

    Uses Neo4j allShortestPaths with a configurable max hop depth (2–8).
    Returns up to 5 distinct paths, each as an ordered list of nodes and
    edges so the frontend can highlight the chain in Cytoscape.

    Response shape:
      {
        "source_id": "...",
        "target_id": "...",
        "path_count": N,
        "paths": [
          {
            "length": 3,
            "nodes": [ {id, label, display}, ... ],
            "edges": [ {source, target, type}, ... ]
          }
        ]
      }
    """
    if not source_id or not target_id:
        raise HTTPException(400, "source_id and target_id are required")
    if source_id == target_id:
        raise HTTPException(400, "source_id and target_id must be different")
    max_depth = max(2, min(8, max_depth))

    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (src), (tgt)
            WHERE src.id = $src AND tgt.id = $tgt
            MATCH p = allShortestPaths((src)-[*1..""" + str(max_depth) + """]-(tgt))
            RETURN p
            LIMIT 5
            """,
            src=source_id,
            tgt=target_id,
        )
        rows = await result.data()

    if not rows:
        return {
            "source_id":  source_id,
            "target_id":  target_id,
            "path_count": 0,
            "paths":      [],
            "message":    f"No path found within {max_depth} hops",
        }

    def _display(node) -> str:
        for k in ("name", "title", "email", "handle", "username", "address", "ip", "domain"):
            v = node.get(k)
            if v:
                return str(v)
        return node.get("id", "?")

    paths_out = []
    for row in rows:
        path = row["p"]
        # neo4j-driver Path object: .nodes, .relationships
        nodes_out = []
        for n in path.nodes:
            nprops = dict(n.items())
            lbls   = list(n.labels)
            entity_label = next(
                (l for l in lbls if l not in ("Entity",)), lbls[0] if lbls else "Entity"
            )
            nodes_out.append({
                "id":      nprops.get("id", str(n.element_id)),
                "label":   entity_label,
                "display": _display(nprops),
            })

        edges_out = []
        for r in path.relationships:
            rprops  = dict(r.items())
            src_nid = dict(r.start_node.items()).get("id", str(r.start_node.element_id))
            tgt_nid = dict(r.end_node.items()).get("id",   str(r.end_node.element_id))
            edges_out.append({
                "source": src_nid,
                "target": tgt_nid,
                "type":   r.type,
            })

        paths_out.append({
            "length": len(edges_out),
            "nodes":  nodes_out,
            "edges":  edges_out,
        })

    return {
        "source_id":  source_id,
        "target_id":  target_id,
        "path_count": len(paths_out),
        "paths":      paths_out,
    }


@app.get("/enrich/ip/{ip}/abuse")
async def enrich_ip_abuseipdb(
    ip:           str,
    max_age_days: int  = 90,
    _user:        dict = Depends(get_current_user),
):
    """
    Check an IP address against AbuseIPDB.

    Returns:
      - confidence score (0-100)
      - abuse categories (Port Scan, SSH Abuse, Phishing, etc.)
      - ISP, usage type, last report timestamp
      - Tor exit node flag

    Requires ABUSEIPDB_KEY in .env (free: 1 000 checks/day).
    Updates the IP node with abuse metadata used by the risk scorer.
    """
    _validate_ip(ip)
    if not 1 <= max_age_days <= 365:
        raise HTTPException(400, "max_age_days must be 1-365")

    result = await abuseipdb_check_ip(graph_db, ip, max_age_days=max_age_days)

    if "error" in result and not result.get("skipped"):
        raise HTTPException(502, result["error"])

    return result


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 27 — Bulk Entity Import (CSV / JSON)                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class BulkImportRequest(BaseModel):
    format:       str = Field("auto",    description="'csv', 'json', or 'auto'")
    data:         str = Field(...,       min_length=1, max_length=500_000)
    default_type: str = Field("Unknown", description="Fallback entity type when not specified in data")


@app.post("/import/bulk")
async def paste_bulk_import(
    req:   BulkImportRequest,
    _user: dict = Depends(get_current_user),
):
    """
    Import entities from pasted text — CSV, JSON, or plain-text list.
    Delegates parsing and merging to the existing bulk_import module.

    CSV (with or without headers):
      type,value,note
      IP,1.2.3.4,suspicious

    JSON (array of objects or strings):
      [{"type":"Domain","value":"evil.com"}]

    Plain text — one indicator per line, type auto-detected.

    Returns: {imported, skipped, errors, entity_ids}
    """
    raw = req.data.strip()
    fmt = req.format.lower()
    if fmt == "auto":
        fmt = "json" if raw.startswith(("[", "{")) else "csv"

    # Reuse existing parser from bulk_import.py
    # bi_parse_file expects (filename, bytes) and infers format from extension
    ext  = ".json" if fmt == "json" else ".csv"
    data_bytes = raw.encode("utf-8")
    try:
        parsed_rows, parse_warns = bi_parse_file(f"paste{ext}", data_bytes)
    except Exception as exc:
        raise HTTPException(400, f"Parse error: {exc}")

    if not parsed_rows:
        raise HTTPException(400, "No rows parsed — check your data format")
    if len(parsed_rows) > 5000:
        raise HTTPException(400, "Limit is 5 000 rows per import")

    # Fill in default type for rows that auto-detect returned None
    default_type = req.default_type.capitalize()
    if default_type not in BI_LABELS:
        default_type = "Person"   # safest fallback that bi_normalise accepts
    for row in parsed_rows:
        if not row.get("type"):
            row["type"] = default_type

    valid_rows, norm_errs = bi_normalise(parsed_rows)
    if not valid_rows:
        raise HTTPException(400, f"No valid rows after normalisation. {'; '.join(norm_errs[:3])}")

    result = await bulk_import_entities(graph_db, valid_rows, case_id=None, source="paste_import")

    nodes = result.get("nodes", [])
    return {
        "imported":   result.get("created", 0) + result.get("updated", 0),
        "skipped":    len(parse_warns) + len(norm_errs),
        "errors":     (parse_warns + norm_errs + result.get("errors", []))[:20],
        "entity_ids": [n["id"] for n in nodes],
    }


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 28 — Case Report Generator                                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@app.get("/case/{case_id}/report", response_class=Response)
async def export_case_report(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """
    Generate a self-contained HTML investigation report for a case.

    Includes: case metadata, KPI summary, subject table with risk scores,
    all notes (pinned first), and task checklist.

    Returns Content-Type: text/html so the browser opens it directly or
    it can be File → Saved for sharing / printing.
    """
    bundle = await get_case_full(graph_db, _val_case_id(case_id))
    if not bundle:
        raise HTTPException(404, "Case not found")

    case     = bundle.get("case", {})
    subjects = bundle.get("subjects", [])
    notes    = bundle.get("notes", [])
    tasks    = bundle.get("tasks", [])

    def _esc(s) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # ── KPI counts ────────────────────────────────────────────────────────
    total_tasks = len(tasks)
    done_tasks  = sum(1 for t in tasks if t.get("done"))
    pinned      = [n for n in notes if n.get("pinned")]
    unpinned    = [n for n in notes if not n.get("pinned")]
    sorted_notes = pinned + unpinned

    risk_counts = {"high": 0, "medium": 0, "low": 0}
    for s in subjects:
        rl = (s.get("risk_level") or "").lower()
        if rl in risk_counts:
            risk_counts[rl] += 1

    # ── Subject rows ──────────────────────────────────────────────────────
    subject_rows = ""
    for s in subjects:
        rs  = s.get("risk_score", "")
        rl  = s.get("risk_level", "")
        rf  = s.get("risk_factors") or []
        if isinstance(rf, str):
            import json as _jj
            try:    rf = _jj.loads(rf)
            except: rf = [rf]
        badge = ""
        if rl == "high":
            badge = f'<span style="background:#a32d2d22;color:#e05555;border:1px solid #a32d2d44;border-radius:4px;padding:1px 6px;font-size:0.7rem">{rs} HIGH</span>'
        elif rl == "medium":
            badge = f'<span style="background:#ba751722;color:#e09940;border:1px solid #ba751744;border-radius:4px;padding:1px 6px;font-size:0.7rem">{rs} MED</span>'
        elif rl == "low":
            badge = f'<span style="background:#2a9d6e22;color:#3dba86;border:1px solid #2a9d6e44;border-radius:4px;padding:1px 6px;font-size:0.7rem">{rs} LOW</span>'
        factors_html = '<ul style="margin:0.2rem 0 0 1rem;padding:0;font-size:0.72rem;color:#8b92a5">' + \
                       "".join(f"<li>{_esc(f)}</li>" for f in rf[:5]) + "</ul>" if rf else ""
        label   = _esc(s.get("entity_label") or s.get("label", ""))
        display = _esc(s.get("display") or s.get("name") or s.get("id", ""))
        role    = _esc(s.get("role", ""))
        subject_rows += f"""<tr>
            <td><span style="background:#1a1d2422;border:1px solid #2d323d;border-radius:4px;padding:1px 5px;font-size:0.7rem">{label}</span></td>
            <td style="font-weight:600">{display}</td>
            <td>{badge}</td>
            <td style="color:#8b92a5;font-size:0.78rem">{role}</td>
            <td>{factors_html}</td>
        </tr>"""

    # ── Note blocks ───────────────────────────────────────────────────────
    note_icons = {"finding":"🔍","lead":"💡","hypothesis":"🧩","caution":"⚠️","general":"📝"}
    notes_html = ""
    for n in sorted_notes:
        icon    = note_icons.get(n.get("type", "general"), "📝")
        pinmark = "📌 " if n.get("pinned") else ""
        created = (n.get("created_at") or "")[:10]
        author  = _esc(n.get("author", ""))
        content = _esc(n.get("content", "")).replace("\n", "<br>")
        ntype   = _esc((n.get("type") or "general").capitalize())
        notes_html += f"""<div style="background:#1a1d24;border:1px solid #2d323d;border-radius:8px;padding:0.9rem;margin-bottom:0.6rem">
            <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.4rem">
                <span>{icon}</span>
                <span style="font-weight:600;font-size:0.83rem">{pinmark}{ntype}</span>
                <span style="margin-left:auto;font-size:0.72rem;color:#8b92a5">{created}{(' · ' + author) if author else ''}</span>
            </div>
            <div style="font-size:0.86rem;line-height:1.65">{content}</div>
        </div>"""

    # ── Tasks ─────────────────────────────────────────────────────────────
    tasks_html = ""
    for t in sorted(tasks, key=lambda x: not x.get("done")):
        done  = t.get("done", False)
        text  = _esc(t.get("text", ""))
        check = "☑" if done else "☐"
        style = "text-decoration:line-through;color:#8b92a5" if done else ""
        tasks_html += f'<div style="padding:0.28rem 0;font-size:0.85rem"><span style="margin-right:0.5rem">{check}</span><span style="{style}">{text}</span></div>'

    # ── Metadata ──────────────────────────────────────────────────────────
    import time as _time
    generated_at = _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime())
    title    = _esc(case.get("title", "Untitled Case"))
    status   = _esc((case.get("status")   or "").capitalize())
    priority = _esc((case.get("priority") or "").capitalize())
    desc     = _esc(case.get("description") or "")
    created  = (case.get("created_at") or "")[:10]
    updated  = (case.get("updated_at") or "")[:10]

    sc = {"Open":"#1a6e9e","Active":"#0f6e56","Review":"#ba7517","Closed":"#8b92a5","Archived":"#555"}.get(status,"#8b92a5")
    pc = {"High":"#a32d2d","Medium":"#ba7517","Low":"#2a9d6e"}.get(priority,"#8b92a5")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fieldwork Report — {title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0c10;color:#e4e6eb;line-height:1.6;padding:2rem 1.5rem}}
.wrap{{max-width:900px;margin:0 auto}}
h2{{font-size:0.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#8b92a5;margin:2rem 0 .75rem;padding-bottom:.4rem;border-bottom:1px solid #2d323d}}
.badge{{display:inline-block;border-radius:5px;padding:2px 8px;font-size:.75rem;font-weight:600}}
.kpi-row{{display:flex;gap:.75rem;flex-wrap:wrap;margin:1rem 0}}
.kpi{{background:#1a1d24;border:1px solid #2d323d;border-radius:8px;padding:.7rem 1rem;flex:1;min-width:90px;text-align:center}}
.kpi-val{{font-size:1.5rem;font-weight:800;line-height:1.1}}
.kpi-lbl{{font-size:.65rem;color:#8b92a5;text-transform:uppercase;letter-spacing:.05em;margin-top:.15rem}}
table{{width:100%;border-collapse:collapse;font-size:.84rem}}
th{{text-align:left;padding:.45rem .7rem;color:#8b92a5;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #2d323d}}
td{{padding:.5rem .7rem;border-bottom:1px solid #141618;vertical-align:top}}
.footer{{margin-top:2.5rem;font-size:.7rem;color:#555;border-top:1px solid #1a1d24;padding-top:.8rem}}
@media print{{
  body{{background:#fff;color:#111;padding:1rem}}
  .kpi{{background:#f5f5f5;border-color:#ddd}}
  td,th{{border-color:#ddd}}
  .footer{{color:#888}}
}}
</style>
</head>
<body>
<div class="wrap">
  <div style="display:flex;align-items:flex-start;gap:1rem;flex-wrap:wrap;margin-bottom:.2rem">
    <div style="flex:1">
      <div style="font-size:.72rem;color:#8b92a5;margin-bottom:.2rem">🔍 Fieldwork OSINT — Investigation Report</div>
      <h1 style="font-size:1.55rem;font-weight:800;letter-spacing:-.02em">{title}</h1>
      {f'<div style="color:#8b92a5;font-size:.86rem;margin-top:.3rem">{desc}</div>' if desc else ''}
    </div>
    <div style="display:flex;flex-direction:column;gap:.3rem;align-items:flex-end;flex-shrink:0">
      <span class="badge" style="background:{sc}22;color:{sc};border:1px solid {sc}44">{status}</span>
      <span class="badge" style="background:{pc}22;color:{pc};border:1px solid {pc}44">{priority} priority</span>
    </div>
  </div>
  <div style="font-size:.77rem;color:#8b92a5;margin-bottom:.5rem">Created {created} · Updated {updated} · Generated {generated_at}</div>

  <div class="kpi-row">
    <div class="kpi"><div class="kpi-val">{len(subjects)}</div><div class="kpi-lbl">Subjects</div></div>
    <div class="kpi"><div class="kpi-val">{len(notes)}</div><div class="kpi-lbl">Notes</div></div>
    <div class="kpi"><div class="kpi-val">{done_tasks}/{total_tasks}</div><div class="kpi-lbl">Tasks</div></div>
    <div class="kpi"><div class="kpi-val" style="color:#e05555">{risk_counts['high']}</div><div class="kpi-lbl">High risk</div></div>
    <div class="kpi"><div class="kpi-val" style="color:#e09940">{risk_counts['medium']}</div><div class="kpi-lbl">Medium risk</div></div>
    <div class="kpi"><div class="kpi-val" style="color:#3dba86">{risk_counts['low']}</div><div class="kpi-lbl">Low risk</div></div>
  </div>

  {('<h2>Subjects</h2><table><thead><tr><th>Type</th><th>Identity</th><th>Risk</th><th>Role</th><th>Risk factors</th></tr></thead><tbody>' + subject_rows + '</tbody></table>') if subjects else ''}
  {('<h2>Notes &amp; Findings</h2>' + notes_html) if sorted_notes else ''}
  {('<h2>Tasks</h2><div style="background:#1a1d24;border:1px solid #2d323d;border-radius:8px;padding:.9rem 1rem">' + tasks_html + '</div>') if tasks else ''}

  <div class="footer">Generated by <strong>Fieldwork OSINT</strong> · {generated_at} · Confidential — Handle per applicable data-protection laws</div>
</div>
</body>
</html>"""

    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Content-Disposition": f'inline; filename="fieldwork-{_val_case_id(case_id)}.html"',
        },
    )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 29 — Graph Analytics (centrality, bridges, structural stats)      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

_ANALYTICS_ENTITY_LABELS = [
    "Person", "Company", "Email", "Phone", "Username", "IP", "Domain",
    "URL", "Wallet", "Location", "Aircraft", "Breach", "Leak",
    "DarkWebMention", "TelegramChannel",
]


async def _run_analytics_query(session, cypher: str, **params):
    result = await session.run(cypher, **params)
    return await result.data()


@app.get("/graph/analytics")
async def graph_analytics(
    limit: int  = 15,
    _user: dict = Depends(get_current_user),
):
    """
    Compute structural analytics for the current investigation graph.

    Returns five sections:
      • stats    — node / edge counts, average degree, density
      • hubs     — top-N nodes by total relationship count
      • bridges  — nodes connected to the widest variety of entity label types
      • isolated — count of nodes with zero relationships
      • risk_dist — count of nodes at each risk level

    All computations are pure Cypher — no GDS plugin required.
    """
    limit = max(5, min(50, limit))

    async with graph_db.driver.session() as session:
        stats_r, hubs_r, bridges_r, isolated_r, risk_r = await asyncio.gather(
            # 1. Structural stats
            _run_analytics_query(session, """
                MATCH (n) WHERE n.id IS NOT NULL
                WITH count(n) AS nodes
                OPTIONAL MATCH ()-[r]->()
                RETURN nodes, count(r) AS edges
            """),
            # 2. Hub nodes — ranked by degree
            _run_analytics_query(session, """
                MATCH (n)
                WHERE n.id IS NOT NULL
                  AND any(l IN labels(n) WHERE l IN $lbls)
                WITH n, labels(n) AS lbls, size((n)--()) AS deg
                WHERE deg > 0
                RETURN n.id AS id,
                       lbls AS labels,
                       coalesce(n.name, n.title, n.email, n.handle,
                                n.address, n.ip, n.domain, n.id) AS display,
                       n.risk_level  AS risk_level,
                       n.risk_score  AS risk_score,
                       deg           AS degree
                ORDER BY deg DESC
                LIMIT $lim
            """, lbls=_ANALYTICS_ENTITY_LABELS, lim=limit),
            # 3. Type bridges — nodes linked to the most distinct neighbour label types
            _run_analytics_query(session, """
                MATCH (n)--(m)
                WHERE n.id IS NOT NULL
                  AND any(l IN labels(n) WHERE l IN $lbls)
                WITH n, labels(n) AS nlbls,
                     collect(DISTINCT [l IN labels(m) WHERE l <> 'Entity'][0]) AS neighbour_types,
                     size((n)--()) AS deg
                WHERE size(neighbour_types) >= 2
                RETURN n.id AS id,
                       nlbls AS labels,
                       coalesce(n.name, n.email, n.handle,
                                n.address, n.ip, n.domain, n.id) AS display,
                       [t IN neighbour_types WHERE t IS NOT NULL] AS neighbour_types,
                       deg AS degree,
                       n.risk_level AS risk_level
                ORDER BY size(neighbour_types) DESC, deg DESC
                LIMIT $lim
            """, lbls=_ANALYTICS_ENTITY_LABELS, lim=limit),
            # 4. Isolated nodes
            _run_analytics_query(session, """
                MATCH (n) WHERE n.id IS NOT NULL
                  AND any(l IN labels(n) WHERE l IN $lbls)
                  AND NOT (n)--()
                RETURN count(n) AS cnt
            """, lbls=_ANALYTICS_ENTITY_LABELS),
            # 5. Risk distribution
            _run_analytics_query(session, """
                MATCH (n) WHERE n.id IS NOT NULL
                  AND n.risk_level IS NOT NULL
                  AND any(l IN labels(n) WHERE l IN $lbls)
                RETURN n.risk_level AS level, count(n) AS cnt
                ORDER BY cnt DESC
            """, lbls=_ANALYTICS_ENTITY_LABELS),
        )

    # ── Post-process ──────────────────────────────────────────────────────
    node_count = stats_r[0]["nodes"] if stats_r else 0
    edge_count = stats_r[0]["edges"] if stats_r else 0
    avg_degree = round(2 * edge_count / node_count, 2) if node_count else 0
    density    = round(edge_count / (node_count * (node_count - 1)), 4) if node_count > 1 else 0

    def _label(lbls, known):
        return next((l for l in lbls if l in known), lbls[0] if lbls else "Entity")

    hubs = [
        {
            "id":         r["id"],
            "label":      _label(r["labels"] or [], _ANALYTICS_ENTITY_LABELS),
            "display":    r["display"] or r["id"],
            "degree":     r["degree"],
            "risk_level": r.get("risk_level"),
            "risk_score": int(r["risk_score"]) if r.get("risk_score") else None,
        }
        for r in hubs_r
    ]

    bridges = [
        {
            "id":              r["id"],
            "label":           _label(r["labels"] or [], _ANALYTICS_ENTITY_LABELS),
            "display":         r["display"] or r["id"],
            "degree":          r["degree"],
            "neighbour_types": [t for t in (r["neighbour_types"] or []) if t],
            "risk_level":      r.get("risk_level"),
        }
        for r in bridges_r
    ]

    risk_dist = {r["level"]: r["cnt"] for r in risk_r}

    return {
        "stats": {
            "nodes":     node_count,
            "edges":     edge_count,
            "avg_degree": avg_degree,
            "density":   density,
            "isolated":  isolated_r[0]["cnt"] if isolated_r else 0,
        },
        "hubs":     hubs,
        "bridges":  bridges,
        "risk_dist": risk_dist,
    }


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 30 — Investigation AI Chat                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class AIChatRequest(BaseModel):
    message:    str             = Field(..., min_length=1, max_length=8000)
    case_id:    Optional[str]   = None
    entity_ids: Optional[list[str]] = None   # up to 10 entity IDs for context
    history:    Optional[list[dict]] = None  # [{role, content}] — last N turns
    model:      Optional[str]   = None


@app.post("/ai/chat")
async def ai_chat(
    req:   AIChatRequest,
    _user: dict = Depends(get_current_user),
):
    """
    Multi-turn OSINT investigation chat powered by a local Ollama LLM.

    Context is automatically built from:
      - The specified case (title, status, subjects, notes, tasks)
      - Up to 10 specific entity nodes (properties + risk data)
      - Conversation history (last N exchanges)

    The LLM is instructed to stay grounded in the provided data and
    clearly flag when it is speculating beyond available evidence.

    Returns: {response: str, context_tokens: int}
    """
    context_parts: list[str] = []

    # ── Case context ──────────────────────────────────────────────────────
    if req.case_id:
        try:
            bundle = await get_case_full(graph_db, _val_case_id(req.case_id))
            if bundle:
                case     = bundle.get("case", {})
                subjects = bundle.get("subjects", [])[:12]
                notes    = bundle.get("notes", [])[:15]
                tasks    = bundle.get("tasks", [])

                subj_txt = "\n".join(
                    f"  [{s.get('entity_label','?')}] {s.get('display') or s.get('entity_id','')} "
                    f"(role: {s.get('role','')}"
                    + (f", risk: {s.get('risk_score')} {s.get('risk_level','')}" if s.get('risk_score') else "")
                    + ")"
                    for s in subjects
                ) or "  None."

                note_txt = "\n".join(
                    f"  [{n.get('type','note')}] {n.get('content', n.get('body',''))[:300]}"
                    for n in notes
                ) or "  None."

                task_txt = "\n".join(
                    f"  [{'✓' if t.get('done') else '○'}] {t.get('text','')}"
                    for t in tasks
                ) or "  None."

                context_parts.append(
                    f"CASE: {case.get('title','Untitled')}\n"
                    f"Status: {case.get('status','?')}  Priority: {case.get('priority','?')}\n"
                    f"Description: {case.get('description','—')}\n\n"
                    f"SUBJECTS:\n{subj_txt}\n\n"
                    f"NOTES:\n{note_txt}\n\n"
                    f"TASKS:\n{task_txt}"
                )
        except Exception as exc:
            log.debug("Chat: failed to fetch case context: %s", exc)

    # ── Entity context ────────────────────────────────────────────────────
    if req.entity_ids:
        entity_ids = req.entity_ids[:10]
        try:
            async with graph_db.driver.session() as session:
                result = await session.run("""
                    UNWIND $ids AS eid
                    MATCH (n) WHERE n.id = eid
                    RETURN n.id AS id, labels(n) AS lbls, properties(n) AS props
                """, ids=entity_ids)
                ent_rows = await result.data()

            ent_lines = []
            for r in ent_rows:
                props   = {k: v for k, v in (r["props"] or {}).items()
                           if k not in ("id",) and v}
                lbls    = r["lbls"] or []
                label   = next((l for l in lbls if l in _ANALYTICS_ENTITY_LABELS), lbls[0] if lbls else "Entity")
                display = (props.get("name") or props.get("email") or props.get("ip")
                           or props.get("domain") or props.get("handle") or r["id"])
                risk_str = ""
                if props.get("risk_score"):
                    risk_str = f" | risk={props['risk_score']} {props.get('risk_level','')}"
                ent_lines.append(f"  [{label}] {display}{risk_str}")

            if ent_lines:
                context_parts.append("ENTITIES IN FOCUS:\n" + "\n".join(ent_lines))
        except Exception as exc:
            log.debug("Chat: failed to fetch entity context: %s", exc)

    context = "\n\n".join(context_parts)

    # ── Cap history ───────────────────────────────────────────────────────
    history = (req.history or [])[-10:]  # last 5 turns (10 messages)

    try:
        response = await llm_chat(
            message=req.message,
            context=context,
            history=history,
            model=req.model,
        )
    except Exception as exc:
        raise HTTPException(503, f"LLM unavailable: {exc}")

    return {
        "response":       response,
        "context_length": len(context),
    }
