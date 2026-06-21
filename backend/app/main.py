"""
Fieldwork OSINT — connection engine API.

Single-user, localhost-only. Hardened CORS, uses a shared GraphDB driver
across requests and crawlers (no per-request reconnects).
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Query, Request
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
from crawlers.passive_dns import passive_dns_domain, reverse_ip_lookup
from crawlers.asn import enrich_ip_asn
from crawlers.crtsh import domain_cert_transparency
from crawlers.urlscan    import search_domain as urlscan_search_domain
from crawlers.internetdb import enrich_ip_internetdb
from crawlers.greynoise  import enrich_ip_greynoise
from crawlers.abusedb    import check_urlhaus, search_threatfox, check_malwarebazaar
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
from crawlers.ipinfo import enrich_ip_ipinfo
from crawlers.hunter import hunt_domain_emails
from crawlers.emailrep import check_email_rep
from crawlers.google_dorks import run_dork
from crawlers.sanctions import check_sanctions
from crawlers.adverse_media import search_adverse_media
from crawlers.court_records import search_court_records
from crawlers.whois_history import get_whois_history
from crawlers.companies_house import search_companies_house, companies_house_company
from crawlers.otx import enrich_otx
from crawlers.reddit import reddit_user, reddit_search
from crawlers.wikidata import lookup_wikidata
from crawlers.etherscan import trace_eth_address
from crawlers.maritime import track_vessel
from crawlers.geocode import geocode, reverse_geocode
from orchestrator import (investigate as orchestrate_investigation,
                          detect_type as orch_detect_type,
                          deep_investigate as orch_deep_investigate)
from graph_intel import persist_investigation, get_investigation_subgraph
from link_analysis import analyze as analyze_links
from vision_intel import analyze_image as vision_analyze_image
import inv_monitor

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
    recon_url = os.getenv("RECON_URL", "http://recon:7002")
    db_ok, maigret_ok, harvester_ok, recon_ok = await asyncio.gather(
        graph_db.ping(),
        _ping(f"{_MAIGRET_URL}/health"),
        _ping(f"{_HARVESTER_URL}/health"),
        _ping(f"{recon_url}/health"),
    )
    return {
        "ok": db_ok,
        "neo4j":        db_ok,
        "maigret":      maigret_ok,
        "theharvester": harvester_ok,
        "recon":        recon_ok,
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
    d   = _validate_domain(domain)
    res = await enrich_domain(graph_db, d)
    _audit("RDAP", d, detail=str(res.get("registrant_org") or res.get("registrar") or ""))
    return res


@app.get("/enrich/domain/{domain}/vt")
async def enrich_domain_virustotal(domain: str):
    """VirusTotal reputation + passive DNS → IP nodes."""
    d   = _validate_domain(domain)
    res = await enrich_domain_vt(graph_db, d)
    _audit("VirusTotal/domain", d, detail=f"rep={res.get('reputation', '?')}")
    return res


@app.get("/enrich/domain/{domain}/wayback")
async def enrich_domain_wayback_endpoint(domain: str):
    """Wayback Machine history → first/last archived dates + interesting paths."""
    d   = _validate_domain(domain)
    res = await enrich_domain_wayback(graph_db, d)
    _audit("Wayback", d, detail=f"snapshots={res.get('total_snapshots', '?')}")
    return res


@app.get("/enrich/ip/{ip}")
async def enrich_ip_shodan_endpoint(ip: str):
    """Shodan host data → ports, org, location."""
    i   = _validate_ip(ip)
    res = await enrich_ip_shodan(graph_db, i)
    _audit("Shodan", i, detail=f"ports={res.get('open_ports', '?')}")
    return res


@app.get("/enrich/ip/{ip}/vt")
async def enrich_ip_virustotal(ip: str):
    """VirusTotal IP reputation + reverse DNS → Domain nodes."""
    i   = _validate_ip(ip)
    res = await enrich_ip_vt(graph_db, i)
    _audit("VirusTotal/ip", i, detail=f"rep={res.get('reputation', '?')}")
    return res


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


@app.post("/analyze/image/intel")
async def analyze_image_intel_endpoint(file: UploadFile = File(...)):
    """
    Claude-vision OSINT analysis of an uploaded photo: geolocation from visual
    cues + landmarks/signage/text/objects, EXIF extraction, and GPS cross-check.
    Falls back to EXIF-only (with a note) when no ANTHROPIC_API_KEY is set.
    """
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "img").suffix,
                                     delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        result = await vision_analyze_image(graph_db, tmp_path, file.filename or "image", None)
    finally:
        os.unlink(tmp_path)
    _audit("VisionIntel", file.filename or "image",
           detail=f"engine={result.get('engine')} gps={bool(result.get('gps'))}")
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


@app.get("/auth/config")
async def auth_config():
    """Public config the frontend uses to decide whether to show the login UI."""
    from auth import AUTH_DISABLED, _DEFAULT_USER
    return {
        "auth_disabled": AUTH_DISABLED,
        "default_user":  _DEFAULT_USER if AUTH_DISABLED else None,
    }


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


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 31 — Analyst Confidence Tagging                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝

_TAG_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,32}$")
_PRESET_TAGS = {
    "confirmed", "unverified", "discredited", "priority",
    "false-positive", "reviewed", "sensitive", "lead",
}
_TAG_ENTITY_LABELS = [
    "Person", "Company", "Email", "Phone", "Username", "IP", "Domain",
    "URL", "Wallet", "Location", "Aircraft",
]


class TagRequest(BaseModel):
    tag: str = Field(..., min_length=1, max_length=32)


@app.post("/entity/{entity_id:path}/tag")
async def add_entity_tag(
    entity_id: str,
    req:       TagRequest,
    _user:     dict = Depends(get_current_user),
):
    """Add an analyst confidence tag to any entity node."""
    tag = req.tag.strip().lower()
    if not _TAG_RE.match(tag):
        raise HTTPException(400, "Tag: letters, digits, hyphens, underscores only (max 32)")
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (n) WHERE n.id = $id
            SET n.analyst_tags = CASE
                WHEN $tag IN coalesce(n.analyst_tags, [])
                THEN n.analyst_tags
                ELSE coalesce(n.analyst_tags, []) + [$tag]
            END
            RETURN n.analyst_tags AS tags
        """, id=entity_id, tag=tag)
        row = await result.single()
        if not row:
            raise HTTPException(404, "Entity not found")
        return {"id": entity_id, "analyst_tags": list(row["tags"] or [])}


@app.delete("/entity/{entity_id:path}/tag")
async def remove_entity_tag(
    entity_id: str,
    req:       TagRequest,
    _user:     dict = Depends(get_current_user),
):
    """Remove an analyst confidence tag from an entity node."""
    tag = req.tag.strip().lower()
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (n) WHERE n.id = $id
            SET n.analyst_tags = [t IN coalesce(n.analyst_tags, []) WHERE t <> $tag]
            RETURN n.analyst_tags AS tags
        """, id=entity_id, tag=tag)
        row = await result.single()
        if not row:
            raise HTTPException(404, "Entity not found")
        return {"id": entity_id, "analyst_tags": list(row["tags"] or [])}


@app.get("/graph/by-tag/{tag}")
async def entities_by_tag(
    tag:   str,
    limit: int  = 100,
    _user: dict = Depends(get_current_user),
):
    """Return all entities carrying a given analyst tag."""
    tag = tag.strip().lower()
    if not _TAG_RE.match(tag):
        raise HTTPException(400, "Invalid tag format")
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (n) WHERE $tag IN coalesce(n.analyst_tags, [])
              AND any(l IN labels(n) WHERE l IN $lbls)
            RETURN n.id AS id, labels(n) AS labels,
                   n.analyst_tags AS tags,
                   coalesce(n.name, n.email, n.ip, n.domain,
                            n.handle, n.address, n.id) AS display,
                   n.risk_score AS risk_score, n.risk_level AS risk_level
            ORDER BY n.risk_score DESC NULLS LAST
            LIMIT $lim
        """, tag=tag, lbls=_TAG_ENTITY_LABELS, lim=min(limit, 500))
        rows = await result.data()

    def _lbl(lbls):
        return next((l for l in lbls if l in _TAG_ENTITY_LABELS), lbls[0] if lbls else "Entity")

    return {
        "tag":      tag,
        "count":    len(rows),
        "entities": [
            {
                "id":           r["id"],
                "label":        _lbl(r["labels"] or []),
                "display":      r["display"] or r["id"],
                "analyst_tags": list(r["tags"] or []),
                "risk_score":   r.get("risk_score"),
                "risk_level":   r.get("risk_level"),
            }
            for r in rows
        ],
    }


@app.get("/graph/tags")
async def list_used_tags(_user: dict = Depends(get_current_user)):
    """Return every analyst tag in use with entity counts."""
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (n) WHERE n.analyst_tags IS NOT NULL AND size(n.analyst_tags) > 0
            UNWIND n.analyst_tags AS tag
            RETURN tag, count(*) AS cnt
            ORDER BY cnt DESC
        """)
        rows = await result.data()
    return {"tags": [{"tag": r["tag"], "count": r["cnt"]} for r in rows]}


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 32 — Passive DNS + ASN Enrichment                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@app.get("/enrich/domain/{domain}/passive-dns")
async def domain_passive_dns(
    domain: str,
    _user:  dict = Depends(get_current_user),
):
    """
    Enumerate subdomains and historical IP resolutions for a domain.
    Sources: HackerTarget (free) + VirusTotal passive DNS (if key set).
    """
    domain = domain.strip().lower().lstrip("www.").lstrip(".")
    if not domain or len(domain) > 253:
        raise HTTPException(400, "Invalid domain")
    res = await passive_dns_domain(graph_db, domain)
    _audit("PassiveDNS", domain, detail=f"subdomains={res.get('subdomain_count', 0)}")
    return res


@app.get("/enrich/ip/{ip}/reverse-dns")
async def ip_reverse_dns(
    ip:    str,
    _user: dict = Depends(get_current_user),
):
    """
    Find domains co-hosted on an IP (reverse IP lookup).
    Sources: HackerTarget (free) + VirusTotal (if key set).
    """
    _validate_ip(ip)
    res = await reverse_ip_lookup(graph_db, ip)
    _audit("ReverseDNS", ip, detail=f"hosts={len(res.get('hosts', []))}")
    return res


@app.get("/enrich/ip/{ip}/asn")
async def ip_asn_lookup(
    ip:    str,
    _user: dict = Depends(get_current_user),
):
    """
    BGP/ASN ownership for an IP address.
    Sources: BGPView (free) with ip-api.com fallback.
    Creates an ASN node linked to the IP via BELONGS_TO_ASN.
    """
    _validate_ip(ip)
    result = await enrich_ip_asn(graph_db, ip)
    if "error" in result:
        raise HTTPException(502, result["error"])
    _audit("ASN", ip, detail=f"asn={result.get('asn', '?')} org={result.get('org', '')[:40]}")
    return result


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  Phase 33 — Hypothesis Board                                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝

import uuid as _uuid_mod
import time as _htime

_HYP_STATUSES   = {"open", "investigating", "confirmed", "rejected"}
_HYP_CONFIDENCE = {"low", "medium", "high"}


class HypothesisCreateRequest(BaseModel):
    claim:            str       = Field(..., min_length=3, max_length=2000)
    status:           str       = Field("open")
    confidence:       str       = Field("low")
    evidence_for:     list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)


class HypothesisPatchRequest(BaseModel):
    claim:            Optional[str]       = None
    status:           Optional[str]       = None
    confidence:       Optional[str]       = None
    evidence_for:     Optional[list[str]] = None
    evidence_against: Optional[list[str]] = None


def _val_hyp(status: Optional[str], confidence: Optional[str]):
    if status and status not in _HYP_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(_HYP_STATUSES))}")
    if confidence and confidence not in _HYP_CONFIDENCE:
        raise HTTPException(400, f"confidence must be one of: {', '.join(sorted(_HYP_CONFIDENCE))}")


def _parse_ev(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, list):
        return v
    import json as _jj
    try:
        return _jj.loads(v)
    except Exception:
        return [v]


@app.post("/case/{case_id}/hypothesis", status_code=201)
async def create_hypothesis(
    case_id: str,
    req:     HypothesisCreateRequest,
    _user:   dict = Depends(get_current_user),
):
    """Add an investigative hypothesis to a case."""
    cid = _val_case_id(case_id)
    _val_hyp(req.status, req.confidence)
    import json as _jj
    hyp_id = str(_uuid_mod.uuid4())
    now    = _htime.strftime("%Y-%m-%dT%H:%M:%SZ", _htime.gmtime())

    async with graph_db.driver.session() as session:
        r = await session.run("MATCH (c:Case {id:$id}) RETURN c.id", id=cid)
        if not await r.single():
            raise HTTPException(404, "Case not found")
        await session.run("""
            MATCH (c:Case {id: $cid})
            CREATE (h:Hypothesis {
                id: $id, case_id: $cid,
                claim: $claim, status: $status, confidence: $confidence,
                evidence_for: $ef, evidence_against: $ea,
                created_at: $now, updated_at: $now, created_by: $user
            })
            CREATE (c)-[:HAS_HYPOTHESIS]->(h)
        """,
            cid=cid, id=hyp_id,
            claim=req.claim.strip(), status=req.status, confidence=req.confidence,
            ef=_jj.dumps(req.evidence_for), ea=_jj.dumps(req.evidence_against),
            now=now, user=_user.get("username", ""),
        )

    return {"hypothesis": {
        "id": hyp_id, "case_id": cid,
        "claim": req.claim.strip(), "status": req.status,
        "confidence": req.confidence,
        "evidence_for": req.evidence_for, "evidence_against": req.evidence_against,
        "created_at": now, "updated_at": now,
        "created_by": _user.get("username", ""),
    }}


@app.get("/case/{case_id}/hypothesis")
async def list_hypotheses(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """List all hypotheses for a case ordered by status then creation time."""
    cid = _val_case_id(case_id)
    _STATUS_ORD = {"open": 0, "investigating": 1, "confirmed": 2, "rejected": 3}

    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (c:Case {id: $cid})-[:HAS_HYPOTHESIS]->(h:Hypothesis)
            RETURN h.id AS id, h.claim AS claim, h.status AS status,
                   h.confidence AS confidence,
                   h.evidence_for AS ef, h.evidence_against AS ea,
                   h.created_at AS created_at, h.updated_at AS updated_at,
                   h.created_by AS created_by
            ORDER BY h.created_at ASC
        """, cid=cid)
        rows = await result.data()

    hyps = [
        {
            "id": r["id"], "case_id": cid,
            "claim": r["claim"], "status": r["status"] or "open",
            "confidence": r["confidence"] or "low",
            "evidence_for":     _parse_ev(r["ef"]),
            "evidence_against": _parse_ev(r["ea"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "created_by": r["created_by"] or "",
        }
        for r in rows
    ]
    hyps.sort(key=lambda h: (_STATUS_ORD.get(h["status"], 9), h["created_at"] or ""))
    return {"hypotheses": hyps, "count": len(hyps)}


@app.patch("/case/{case_id}/hypothesis/{hyp_id}")
async def update_hypothesis(
    case_id: str,
    hyp_id:  str,
    req:     HypothesisPatchRequest,
    _user:   dict = Depends(get_current_user),
):
    """Update status, confidence, claim, or evidence lists on a hypothesis."""
    cid = _val_case_id(case_id)
    _val_hyp(req.status, req.confidence)
    import json as _jj
    now = _htime.strftime("%Y-%m-%dT%H:%M:%SZ", _htime.gmtime())

    sets:   list[str] = ["h.updated_at = $now"]
    params: dict      = {"cid": cid, "hid": hyp_id, "now": now}

    if req.claim       is not None: sets.append("h.claim = $claim");           params["claim"] = req.claim.strip()
    if req.status      is not None: sets.append("h.status = $status");         params["status"] = req.status
    if req.confidence  is not None: sets.append("h.confidence = $confidence"); params["confidence"] = req.confidence
    if req.evidence_for     is not None: sets.append("h.evidence_for = $ef");  params["ef"] = _jj.dumps(req.evidence_for)
    if req.evidence_against is not None: sets.append("h.evidence_against=$ea");params["ea"] = _jj.dumps(req.evidence_against)

    async with graph_db.driver.session() as session:
        result = await session.run(
            f"MATCH (c:Case {{id:$cid}})-[:HAS_HYPOTHESIS]->(h:Hypothesis {{id:$hid}}) "
            f"SET {', '.join(sets)} RETURN h.status AS status",
            **params,
        )
        row = await result.single()
        if not row:
            raise HTTPException(404, "Hypothesis not found")
    return {"updated": True, "id": hyp_id, "status": row["status"]}


@app.delete("/case/{case_id}/hypothesis/{hyp_id}")
async def delete_hypothesis(
    case_id: str,
    hyp_id:  str,
    _user:   dict = Depends(get_current_user),
):
    """Permanently delete a hypothesis."""
    cid = _val_case_id(case_id)
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (c:Case {id:$cid})-[:HAS_HYPOTHESIS]->(h:Hypothesis {id:$hid})
            DETACH DELETE h RETURN count(h) AS n
        """, cid=cid, hid=hyp_id)
        row = await result.single()
        if not row or not row["n"]:
            raise HTTPException(404, "Hypothesis not found")
    return {"deleted": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Ph34 — Case Investigation Timeline
# ═══════════════════════════════════════════════════════════════════════════════

_TL_CATEGORIES = {"discovery", "incident", "registration", "financial",
                  "communication", "travel", "legal", "other"}

class TimelineEventCreate(BaseModel):
    title:       str = Field(..., min_length=1, max_length=200)
    event_date:  str = Field(..., description="ISO date or datetime, e.g. 2024-03-15")
    category:    str = Field("other")
    description: str = Field("", max_length=2000)
    entity_ids:  list[str] = Field(default_factory=list)

class TimelineEventPatch(BaseModel):
    title:       Optional[str] = None
    event_date:  Optional[str] = None
    category:    Optional[str] = None
    description: Optional[str] = None
    entity_ids:  Optional[list[str]] = None


@app.post("/case/{case_id}/timeline", status_code=201)
async def create_timeline_event(
    case_id: str,
    req:     TimelineEventCreate,
    _user:   dict = Depends(get_current_user),
):
    """Add a dated event to the case timeline."""
    cid = _val_case_id(case_id)
    category = req.category if req.category in _TL_CATEGORIES else "other"
    eid = str(_uuid_mod.uuid4())

    async with graph_db.driver.session() as session:
        # Verify case exists
        r = await session.run("MATCH (c:Case {id:$id}) RETURN c.id", id=cid)
        if not await r.single():
            raise HTTPException(404, "Case not found")

        await session.run("""
            MATCH (c:Case {id:$cid})
            CREATE (e:TimelineEvent {
                id:          $eid,
                case_id:     $cid,
                title:       $title,
                event_date:  $event_date,
                category:    $category,
                description: $description,
                entity_ids:  $entity_ids,
                created_at:  datetime()
            })
            CREATE (c)-[:HAS_TIMELINE_EVENT]->(e)
        """,
            cid=cid, eid=eid,
            title=req.title,
            event_date=req.event_date,
            category=category,
            description=req.description,
            entity_ids=req.entity_ids[:20],
        )

    return {
        "id":          eid,
        "case_id":     cid,
        "title":       req.title,
        "event_date":  req.event_date,
        "category":    category,
        "description": req.description,
        "entity_ids":  req.entity_ids[:20],
    }


@app.get("/case/{case_id}/timeline")
async def list_timeline_events(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """Return all timeline events for a case, sorted by event_date ascending."""
    cid = _val_case_id(case_id)
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (c:Case {id:$cid})-[:HAS_TIMELINE_EVENT]->(e:TimelineEvent)
            RETURN e.id          AS id,
                   e.title       AS title,
                   e.event_date  AS event_date,
                   e.category    AS category,
                   e.description AS description,
                   e.entity_ids  AS entity_ids,
                   e.created_at  AS created_at
            ORDER BY e.event_date ASC
        """, cid=cid)
        rows = await result.data()

    events = [
        {
            "id":          r["id"],
            "title":       r["title"],
            "event_date":  r["event_date"],
            "category":    r["category"],
            "description": r["description"] or "",
            "entity_ids":  list(r["entity_ids"] or []),
        }
        for r in rows
    ]
    return {"case_id": cid, "events": events, "count": len(events)}


@app.patch("/case/{case_id}/timeline/{event_id}")
async def patch_timeline_event(
    case_id:  str,
    event_id: str,
    req:      TimelineEventPatch,
    _user:    dict = Depends(get_current_user),
):
    """Partially update a timeline event."""
    cid = _val_case_id(case_id)
    updates: list[str] = []
    params: dict = {"cid": cid, "eid": event_id}

    if req.title is not None:
        updates.append("e.title = $title"); params["title"] = req.title[:200]
    if req.event_date is not None:
        updates.append("e.event_date = $event_date"); params["event_date"] = req.event_date
    if req.category is not None:
        cat = req.category if req.category in _TL_CATEGORIES else "other"
        updates.append("e.category = $category"); params["category"] = cat
    if req.description is not None:
        updates.append("e.description = $description"); params["description"] = req.description[:2000]
    if req.entity_ids is not None:
        updates.append("e.entity_ids = $entity_ids"); params["entity_ids"] = req.entity_ids[:20]

    if not updates:
        raise HTTPException(400, "Nothing to update")

    set_clause = ", ".join(updates)
    async with graph_db.driver.session() as session:
        result = await session.run(f"""
            MATCH (c:Case {{id:$cid}})-[:HAS_TIMELINE_EVENT]->(e:TimelineEvent {{id:$eid}})
            SET {set_clause}
            RETURN e.id AS id, e.title AS title, e.event_date AS event_date,
                   e.category AS category, e.description AS description,
                   e.entity_ids AS entity_ids
        """, **params)
        row = await result.single()
        if not row:
            raise HTTPException(404, "Timeline event not found")

    return dict(row)


@app.delete("/case/{case_id}/timeline/{event_id}")
async def delete_timeline_event(
    case_id:  str,
    event_id: str,
    _user:    dict = Depends(get_current_user),
):
    """Delete a timeline event."""
    cid = _val_case_id(case_id)
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (c:Case {id:$cid})-[:HAS_TIMELINE_EVENT]->(e:TimelineEvent {id:$eid})
            DETACH DELETE e RETURN count(e) AS n
        """, cid=cid, eid=event_id)
        row = await result.single()
        if not row or not row["n"]:
            raise HTTPException(404, "Timeline event not found")
    return {"deleted": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Ph35 — Bulk Case Enrichment
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/case/{case_id}/enrich-all")
async def bulk_enrich_case(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """
    Enrich every IP and Domain subject in a case automatically.

    For each Domain subject: runs passive DNS (HackerTarget + VT).
    For each IP subject:     runs ASN lookup (BGPView + ip-api).

    Runs concurrently (up to 5 at a time to respect free-tier rate limits).
    Returns a summary of what was enriched and any errors.
    """
    import asyncio
    cid = _val_case_id(case_id)

    # Fetch case subjects
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (c:Case {id:$cid})-[:HAS_SUBJECT]->(n)
            RETURN n.id AS id, labels(n) AS lbls,
                   coalesce(n.domain, n.ip, n.email, n.name, n.id) AS value
        """, cid=cid)
        subjects = await result.data()

    domains = [s for s in subjects if "Domain" in (s["lbls"] or [])]
    ips     = [s for s in subjects if "IP"     in (s["lbls"] or [])]

    semaphore = asyncio.Semaphore(5)

    async def _safe_pdns(s):
        async with semaphore:
            try:
                r = await passive_dns_domain(graph_db, s["value"])
                return {"entity": s["value"], "type": "domain", "ok": True,
                        "subdomains": len(r.get("subdomains", [])),
                        "ips": len(r.get("ip_history", []))}
            except Exception as exc:
                return {"entity": s["value"], "type": "domain", "ok": False, "error": str(exc)}

    async def _safe_asn(s):
        async with semaphore:
            try:
                r = await enrich_ip_asn(graph_db, s["value"])
                return {"entity": s["value"], "type": "ip", "ok": True,
                        "asn": r.get("asn"), "asn_name": r.get("asn_name")}
            except Exception as exc:
                return {"entity": s["value"], "type": "ip", "ok": False, "error": str(exc)}

    tasks = [_safe_pdns(s) for s in domains[:20]] + [_safe_asn(s) for s in ips[:20]]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if r.get("ok")]
    failures  = [r for r in results if not r.get("ok")]

    return {
        "case_id":    cid,
        "enriched":   len(successes),
        "failed":     len(failures),
        "domains":    len(domains),
        "ips":        len(ips),
        "results":    results,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Ph36 — Certificate Transparency (crt.sh)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/enrich/domain/{domain}/certs")
async def domain_certs(
    domain: str,
    _user:  dict = Depends(get_current_user),
):
    """
    Query crt.sh for all TLS certificates ever issued for *.{domain}.
    Returns unique subdomains with issuer and validity dates.
    Free, no API key required.
    """
    if not domain or len(domain) > 253:
        raise HTTPException(400, "Invalid domain")
    d   = domain.lower().strip()
    res = await domain_cert_transparency(graph_db, d)
    _audit("CertTransparency", d, detail=f"subdomains={res.get('unique_count', 0)}")
    return res


# ── Ph52 — URLScan.io domain scan history ────────────────────────────────────

@app.get("/enrich/domain/{domain}/urlscan")
async def domain_urlscan(
    domain: str,
    limit:  int  = 10,
    _user:  dict = Depends(get_current_user),
):
    """
    Retrieve recent URLScan.io scans for a domain.
    Returns page titles, IPs seen, detected technologies, screenshots,
    and malicious verdicts.  Free, no API key required for search.
    Persists observed IPs as IP nodes linked via RESOLVES_TO.
    """
    d = _validate_domain(domain)
    n = min(max(1, limit), 50)
    res = await urlscan_search_domain(graph_db, d, limit=n)
    _audit("URLScan", d, detail=f"scans={res.get('scan_count', 0)}")
    return res


# ═══════════════════════════════════════════════════════════════════════════════
# Ph37 — Cross-case entity overlap
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/case/{case_id}/related-cases")
async def related_cases(
    case_id: str,
    limit:   int = 10,
    _user:   dict = Depends(get_current_user),
):
    """
    Find other cases that share at least one subject entity with this case.

    Returns cases ranked by number of shared entities descending, along with
    the list of shared entity IDs and display names.
    """
    cid = _val_case_id(case_id)
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (c:Case {id:$cid})-[:HAS_SUBJECT]->(shared)<-[:HAS_SUBJECT]-(other:Case)
            WHERE other.id <> $cid
            WITH other,
                 collect(DISTINCT {
                     id:      coalesce(shared.id, ''),
                     display: coalesce(shared.name, shared.domain, shared.ip,
                                       shared.email, shared.handle, shared.id, ''),
                     label:   head(labels(shared))
                 }) AS entities
            ORDER BY size(entities) DESC
            LIMIT $limit
            RETURN other.id        AS case_id,
                   other.title     AS title,
                   other.status    AS status,
                   other.created_at AS created_at,
                   entities,
                   size(entities)  AS shared_count
        """, cid=cid, limit=limit)
        rows = await result.data()

    related = [
        {
            "case_id":      r["case_id"],
            "title":        r["title"],
            "status":       r["status"],
            "shared_count": r["shared_count"],
            "shared":       list(r["entities"]),
        }
        for r in rows
    ]
    return {"case_id": cid, "related": related, "count": len(related)}


# ═══════════════════════════════════════════════════════════════════════════════
# Ph40 — Entity Notes (node-level annotations)
# ═══════════════════════════════════════════════════════════════════════════════

_ENTITY_NOTE_TYPES = {"observation", "source", "caution", "discrepancy", "general"}

class EntityNoteCreate(BaseModel):
    content:   str = Field(..., min_length=1, max_length=2000)
    note_type: str = Field("observation")


@app.post("/entity/{entity_id:path}/note", status_code=201)
async def add_entity_note(
    entity_id: str,
    req:       EntityNoteCreate,
    _user:     dict = Depends(get_current_user),
):
    """Attach a freeform analyst note directly to an entity node."""
    ntype = req.note_type if req.note_type in _ENTITY_NOTE_TYPES else "observation"
    nid   = str(_uuid_mod.uuid4())
    author = _user.get("sub", "analyst")

    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (e) WHERE e.id = $eid
            CREATE (n:EntityNote {
                id:         $nid,
                content:    $content,
                note_type:  $ntype,
                author:     $author,
                created_at: datetime()
            })
            CREATE (e)-[:HAS_ENTITY_NOTE]->(n)
            RETURN n.id AS id, n.content AS content, n.note_type AS note_type,
                   n.author AS author, toString(n.created_at) AS created_at
        """, eid=entity_id, nid=nid,
             content=req.content, ntype=ntype, author=author)
        row = await result.single()
        if not row:
            raise HTTPException(404, "Entity not found")

    return dict(row)


@app.get("/entity/{entity_id:path}/note")
async def list_entity_notes(
    entity_id: str,
    _user:     dict = Depends(get_current_user),
):
    """Return all analyst notes attached to this entity, newest first."""
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (e)-[:HAS_ENTITY_NOTE]->(n:EntityNote)
            WHERE e.id = $eid
            RETURN n.id AS id, n.content AS content, n.note_type AS note_type,
                   n.author AS author, toString(n.created_at) AS created_at
            ORDER BY n.created_at DESC
        """, eid=entity_id)
        rows = await result.data()

    return {"entity_id": entity_id, "notes": [dict(r) for r in rows]}


@app.delete("/entity/{entity_id:path}/note/{note_id}")
async def delete_entity_note(
    entity_id: str,
    note_id:   str,
    _user:     dict = Depends(get_current_user),
):
    """Delete an entity note."""
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (e)-[:HAS_ENTITY_NOTE]->(n:EntityNote {id:$nid})
            WHERE e.id = $eid
            DETACH DELETE n RETURN count(n) AS deleted
        """, eid=entity_id, nid=note_id)
        row = await result.single()
        if not row or not row["deleted"]:
            raise HTTPException(404, "Note not found")
    return {"deleted": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Ph41 — Graph Export (GEXF / CSV)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/graph/export")
async def export_graph(
    format:    str = "gexf",          # gexf | node_csv | edge_csv
    entity_id: Optional[str] = None,  # seed node; None = full sample
    depth:     int = 2,
    _user:     dict = Depends(get_current_user),
):
    """
    Export the graph (or subgraph) in GEXF, node-CSV, or edge-CSV format.

    GEXF can be opened directly in Gephi for advanced layout/analysis.
    CSV formats are compatible with any spreadsheet or graph tool.
    """
    import csv, io, xml.etree.ElementTree as ET

    # ── Fetch nodes + edges ───────────────────────────────────────────────────
    async with graph_db.driver.session() as session:
        if entity_id:
            # Subgraph around seed
            result = await session.run("""
                MATCH path = (seed)-[*0..{depth}]-(n)
                WHERE seed.id = $eid
                WITH collect(DISTINCT n) AS nodes,
                     collect(DISTINCT relationships(path)) AS rel_lists
                UNWIND nodes AS node
                WITH collect(DISTINCT {
                    id:      node.id,
                    label:   head(labels(node)),
                    display: coalesce(node.name, node.domain, node.ip,
                                      node.email, node.handle, node.id)
                }) AS node_list, rel_lists
                UNWIND rel_lists AS rels
                UNWIND rels AS rel
                WITH node_list,
                     collect(DISTINCT {
                         src:  startNode(rel).id,
                         dst:  endNode(rel).id,
                         type: type(rel)
                     }) AS edge_list
                RETURN node_list AS nodes, edge_list AS edges
            """.replace("{depth}", str(min(depth, 4))), eid=entity_id)
        else:
            result = await session.run("""
                MATCH (n)
                WHERE n.id IS NOT NULL
                WITH collect(DISTINCT {
                    id:      n.id,
                    label:   head(labels(n)),
                    display: coalesce(n.name, n.domain, n.ip,
                                      n.email, n.handle, n.id)
                })[..500] AS node_list
                OPTIONAL MATCH (a)-[r]->(b)
                WHERE a.id IS NOT NULL AND b.id IS NOT NULL
                WITH node_list,
                     collect(DISTINCT {src: a.id, dst: b.id, type: type(r)})[..2000] AS edge_list
                RETURN node_list AS nodes, edge_list AS edges
            """)

        row = await result.single()

    nodes = list(row["nodes"] or []) if row else []
    edges = list(row["edges"] or []) if row else []

    # ── GEXF ─────────────────────────────────────────────────────────────────
    if format == "gexf":
        root = ET.Element("gexf", {
            "xmlns": "http://gexf.net/1.3",
            "version": "1.3",
        })
        graph_el = ET.SubElement(root, "graph", {
            "defaultedgetype": "directed",
        })
        attrs_el = ET.SubElement(graph_el, "attributes", {"class": "node"})
        ET.SubElement(attrs_el, "attribute", {
            "id": "0", "title": "type", "type": "string"
        })

        nodes_el = ET.SubElement(graph_el, "nodes")
        for n in nodes:
            if not n.get("id"):
                continue
            ne = ET.SubElement(nodes_el, "node", {
                "id":    str(n["id"]),
                "label": str(n.get("display") or n["id"])[:60],
            })
            avs = ET.SubElement(ne, "attvalues")
            ET.SubElement(avs, "attvalue", {"for": "0", "value": str(n.get("label", ""))})

        edges_el = ET.SubElement(graph_el, "edges")
        seen_edges: set[str] = set()
        for i, e in enumerate(edges):
            if not e.get("src") or not e.get("dst"):
                continue
            key = f"{e['src']}__{e['dst']}__{e.get('type','')}"
            if key in seen_edges:
                continue
            seen_edges.add(key)
            ET.SubElement(edges_el, "edge", {
                "id":     str(i),
                "source": str(e["src"]),
                "target": str(e["dst"]),
                "label":  str(e.get("type", "")),
            })

        xml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=False)
        xml_str   = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes
        return Response(
            content=xml_str,
            media_type="application/xml",
            headers={"Content-Disposition": "attachment; filename=fieldwork-graph.gexf"},
        )

    # ── Node CSV ──────────────────────────────────────────────────────────────
    if format == "node_csv":
        buf = io.StringIO()
        w   = csv.writer(buf)
        w.writerow(["id", "label", "display_name"])
        for n in nodes:
            w.writerow([n.get("id",""), n.get("label",""), n.get("display","")])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=fieldwork-nodes.csv"},
        )

    # ── Edge CSV ──────────────────────────────────────────────────────────────
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["source", "target", "type"])
    seen_edges2: set[str] = set()
    for e in edges:
        key = f"{e.get('src')}__{e.get('dst')}__{e.get('type','')}"
        if key in seen_edges2 or not e.get("src") or not e.get("dst"):
            continue
        seen_edges2.add(key)
        w.writerow([e.get("src",""), e.get("dst",""), e.get("type","")])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fieldwork-edges.csv"},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Ph43 — Entity Merge / Deduplication
# ═══════════════════════════════════════════════════════════════════════════════

class MergeEntitiesRequest(BaseModel):
    source_id: str = Field(..., description="Entity to absorb (will be deleted)")
    target_id: str = Field(..., description="Entity to keep (accumulates all edges)")
    copy_props: bool = Field(True, description="Copy non-null properties from source to target if target's value is null/empty")


@app.post("/entity/merge-nodes")
async def merge_entity_nodes(
    req:   MergeEntitiesRequest,
    _user: dict = Depends(get_current_user),
):
    """
    Merge two entity nodes: all relationships on `source` are re-pointed to
    `target`, then `source` is deleted.  Optionally fills missing properties
    on `target` from `source`.

    This is a destructive operation — `source` is permanently removed.
    """
    src, tgt = req.source_id.strip(), req.target_id.strip()
    if not src or not tgt:
        raise HTTPException(400, "Both source_id and target_id are required")
    if src == tgt:
        raise HTTPException(400, "source_id and target_id must be different")

    async with graph_db.driver.session() as session:
        # Verify both exist
        check = await session.run("""
            MATCH (s) WHERE s.id = $src
            MATCH (t) WHERE t.id = $tgt
            RETURN s.id AS sid, t.id AS tid,
                   head(labels(s)) AS slbl, head(labels(t)) AS tlbl
        """, src=src, tgt=tgt)
        row = await check.single()
        if not row:
            raise HTTPException(404, "One or both entities not found")

        # Re-home outgoing relationships: (source)-[r]->(x)  →  (target)-[r]->(x)
        # (excluding self-loops and existing duplicates)
        await session.run("""
            MATCH (s) WHERE s.id = $src
            MATCH (t) WHERE t.id = $tgt
            MATCH (s)-[r]->(x) WHERE x.id <> $tgt
            WITH t, x, type(r) AS rtype, properties(r) AS rprops
            CALL apoc.create.relationship(t, rtype, rprops, x) YIELD rel
            RETURN count(rel)
        """, src=src, tgt=tgt)

        # Re-home incoming relationships: (x)-[r]->(source)  →  (x)-[r]->(target)
        await session.run("""
            MATCH (s) WHERE s.id = $src
            MATCH (t) WHERE t.id = $tgt
            MATCH (x)-[r]->(s) WHERE x.id <> $tgt
            WITH t, x, type(r) AS rtype, properties(r) AS rprops
            CALL apoc.create.relationship(x, rtype, rprops, t) YIELD rel
            RETURN count(rel)
        """, src=src, tgt=tgt)

        # Copy missing properties if requested
        if req.copy_props:
            await session.run("""
                MATCH (s) WHERE s.id = $src
                MATCH (t) WHERE t.id = $tgt
                WITH s, t, properties(s) AS sprops
                UNWIND keys(sprops) AS k
                WITH t, k, sprops[k] AS v
                WHERE k <> 'id' AND (t[k] IS NULL OR t[k] = '')
                  AND v IS NOT NULL AND v <> ''
                SET t[k] = v
            """, src=src, tgt=tgt)

        # Delete source
        result = await session.run("""
            MATCH (s) WHERE s.id = $src
            DETACH DELETE s RETURN count(s) AS n
        """, src=src)
        row2 = await result.single()

    return {
        "merged":       True,
        "source_id":    src,
        "target_id":    tgt,
        "source_label": row["slbl"],
        "target_label": row["tlbl"],
        "deleted":      bool(row2 and row2["n"]),
    }


@app.get("/entity/merge-preview")
async def merge_preview(
    source_id: str,
    target_id: str,
    _user:     dict = Depends(get_current_user),
):
    """Preview what a merge would do without actually merging."""
    src, tgt = source_id.strip(), target_id.strip()
    async with graph_db.driver.session() as session:
        result = await session.run("""
            MATCH (s) WHERE s.id = $src
            MATCH (t) WHERE t.id = $tgt
            OPTIONAL MATCH (s)-[ro]->()
            OPTIONAL MATCH ()-[ri]->(s)
            RETURN head(labels(s)) AS src_label,
                   coalesce(s.name, s.domain, s.ip, s.email, s.id) AS src_display,
                   head(labels(t)) AS tgt_label,
                   coalesce(t.name, t.domain, t.ip, t.email, t.id) AS tgt_display,
                   count(DISTINCT ro) AS outgoing,
                   count(DISTINCT ri) AS incoming
        """, src=src, tgt=tgt)
        row = await result.single()

    if not row:
        raise HTTPException(404, "One or both entities not found")

    return {
        "source": {"id": src, "label": row["src_label"], "display": row["src_display"]},
        "target": {"id": tgt, "label": row["tgt_label"], "display": row["tgt_display"]},
        "relationships_to_move": row["outgoing"] + row["incoming"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Ph44 — Case Health Score
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/case/{case_id}/health")
async def case_health_score(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """
    Compute a multi-dimensional health score for an investigation case.

    Dimensions (each 0–100):
      • tasks_done       — % of tasks marked complete
      • hypotheses       — % of hypotheses with a decision (confirmed/rejected)
      • enrichment       — % of subjects with enriched data (risk_score or ASN or domain props)
      • documentation    — notes + timeline events (saturates at 10)
      • coverage         — % of subjects with a display name + label

    Returns overall (weighted average) and per-dimension scores.
    """
    import asyncio
    cid = _val_case_id(case_id)

    async def _tasks():
        async with graph_db.driver.session() as s:
            r = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_TASK]->(t:Task)
                RETURN count(t) AS total,
                       sum(CASE WHEN t.done THEN 1 ELSE 0 END) AS done
            """, cid=cid)
            return await r.single()

    async def _hyps():
        async with graph_db.driver.session() as s:
            r = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_HYPOTHESIS]->(h:Hypothesis)
                RETURN count(h) AS total,
                       sum(CASE WHEN h.status IN ['confirmed','rejected'] THEN 1 ELSE 0 END) AS decided
            """, cid=cid)
            return await r.single()

    async def _enrichment():
        async with graph_db.driver.session() as s:
            r = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_SUBJECT]->(n)
                RETURN count(n) AS total,
                       sum(CASE WHEN n.risk_score IS NOT NULL OR n.asn IS NOT NULL
                                  OR n.registrant_org IS NOT NULL OR n.isp IS NOT NULL
                            THEN 1 ELSE 0 END) AS enriched
            """, cid=cid)
            return await r.single()

    async def _docs():
        async with graph_db.driver.session() as s:
            r = await s.run("""
                MATCH (c:Case {id:$cid})
                OPTIONAL MATCH (c)-[:HAS_NOTE]->(n:Note)
                OPTIONAL MATCH (c)-[:HAS_TIMELINE_EVENT]->(e:TimelineEvent)
                RETURN count(DISTINCT n) AS notes, count(DISTINCT e) AS events
            """, cid=cid)
            return await r.single()

    async def _coverage():
        async with graph_db.driver.session() as s:
            r = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_SUBJECT]->(n)
                RETURN count(n) AS total,
                       sum(CASE WHEN (n.name IS NOT NULL OR n.domain IS NOT NULL
                                   OR n.ip IS NOT NULL OR n.email IS NOT NULL)
                                  AND size(labels(n)) > 0
                            THEN 1 ELSE 0 END) AS covered
            """, cid=cid)
            return await r.single()

    t, h, e, d, cv = await asyncio.gather(_tasks(), _hyps(), _enrichment(), _docs(), _coverage())

    def _pct(num, den): return round(100 * num / den) if den else None
    def _doc_score(notes, events): return min(100, round(10 * (notes + events)))

    scores = {
        "tasks_done":  _pct(t["done"],    t["total"])    if t and t["total"] else None,
        "hypotheses":  _pct(h["decided"], h["total"])    if h and h["total"] else None,
        "enrichment":  _pct(e["enriched"],e["total"])    if e and e["total"] else None,
        "documentation": _doc_score(d["notes"], d["events"]) if d else 0,
        "coverage":    _pct(cv["covered"],cv["total"])   if cv and cv["total"] else None,
    }

    # Weighted overall — skip None dimensions
    weights = {"tasks_done": 25, "hypotheses": 20, "enrichment": 30,
               "documentation": 10, "coverage": 15}
    total_w  = sum(w for k, w in weights.items() if scores[k] is not None)
    overall  = round(sum(scores[k] * weights[k] for k in weights if scores[k] is not None) / total_w) \
               if total_w else 0

    def _level(s):
        if s is None: return "n/a"
        if s >= 80:   return "good"
        if s >= 50:   return "fair"
        return "poor"

    return {
        "case_id": cid,
        "overall": overall,
        "level":   _level(overall),
        "dimensions": {k: {"score": v, "level": _level(v)} for k, v in scores.items()},
        "raw": {
            "tasks_total":   t["total"]    if t else 0,
            "tasks_done":    t["done"]     if t else 0,
            "hyp_total":     h["total"]    if h else 0,
            "hyp_decided":   h["decided"]  if h else 0,
            "subjects_total":e["total"]    if e else 0,
            "subjects_enriched": e["enriched"] if e else 0,
            "notes":         d["notes"]    if d else 0,
            "timeline_events": d["events"] if d else 0,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Ph45 — Graph Snapshots
# Save named snapshots of a visible graph state; restore or diff them later.
# Snapshots store the set of node IDs present at save time and a label.
# Restoring re-fetches those nodes from the live graph.
# ═══════════════════════════════════════════════════════════════════════════════

import json as _json
# _uuid_mod already imported by Ph33 above


class SnapshotRequest(BaseModel):
    name:        str        = Field(..., max_length=120)
    node_ids:    List[str]  = Field(default_factory=list)
    description: str        = Field("", max_length=500)


@app.post("/graph/snapshot")
async def save_graph_snapshot(
    req:   SnapshotRequest,
    _user: dict = Depends(get_current_user),
):
    """Persist a named snapshot of the current graph canvas (set of node IDs)."""
    snap_id       = f"snap_{_uuid_mod.uuid4().hex[:14]}"
    node_ids_json = _json.dumps(list(dict.fromkeys(req.node_ids))[:500])
    nc            = len(req.node_ids)
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MERGE (s:GraphSnapshot {id: $id})
            SET s.name        = $name,
                s.description = $desc,
                s.node_ids    = $nids,
                s.node_count  = $nc,
                s.created_at  = datetime()
            """,
            id=snap_id, name=req.name.strip(), desc=req.description.strip(),
            nids=node_ids_json, nc=nc,
        )
    return {"id": snap_id, "name": req.name.strip(), "node_count": nc}


@app.get("/graph/snapshots")
async def list_graph_snapshots(_user: dict = Depends(get_current_user)):
    """Return all saved graph snapshots ordered newest-first."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (s:GraphSnapshot)
            RETURN s.id AS id, s.name AS name, s.description AS description,
                   s.node_count AS node_count,
                   toString(s.created_at) AS created_at
            ORDER BY s.created_at DESC
            LIMIT 50
            """
        )
        rows = await result.data()
    return {"snapshots": [dict(r) for r in rows]}


@app.get("/graph/snapshot/{snap_id}")
async def get_graph_snapshot(snap_id: str, _user: dict = Depends(get_current_user)):
    """Restore a snapshot: returns full node/edge data for the saved node set."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (s:GraphSnapshot {id: $id})
            RETURN s.id AS id, s.name AS name, s.description AS description,
                   s.node_ids AS node_ids, s.node_count AS node_count,
                   toString(s.created_at) AS created_at
            """,
            id=snap_id,
        )
        row = await result.single()
    if not row:
        raise HTTPException(404, "Snapshot not found")

    meta     = dict(row)
    node_ids = _json.loads(meta.pop("node_ids") or "[]")

    nodes: list = []
    edges: list = []
    if node_ids:
        async with graph_db.driver.session() as session:
            nr = await session.run(
                """
                MATCH (n) WHERE n.id IN $ids
                RETURN n.id AS id, head(labels(n)) AS label,
                       coalesce(n.name, n.domain, n.ip, n.email,
                                n.username, n.id) AS display,
                       n{.*} AS props
                """,
                ids=node_ids,
            )
            for rec in await nr.data():
                nodes.append({
                    "data": {
                        "id":      rec["id"],
                        "label":   rec["label"],
                        "display": rec["display"],
                        "props":   rec["props"],
                    }
                })

            er = await session.run(
                """
                MATCH (a)-[r]->(b)
                WHERE a.id IN $ids AND b.id IN $ids
                RETURN a.id AS src, b.id AS dst, type(r) AS type
                """,
                ids=node_ids,
            )
            for rec in await er.data():
                edges.append({
                    "data": {
                        "source": rec["src"],
                        "target": rec["dst"],
                        "type":   rec["type"],
                        "label":  rec["type"],
                    }
                })

    return {**meta, "node_ids": node_ids, "nodes": nodes, "edges": edges}


@app.delete("/graph/snapshot/{snap_id}")
async def delete_graph_snapshot(snap_id: str, _user: dict = Depends(get_current_user)):
    async with graph_db.driver.session() as session:
        r  = await session.run(
            "MATCH (s:GraphSnapshot {id: $id}) DELETE s RETURN count(s) AS n",
            id=snap_id,
        )
        row = await r.single()
    return {"deleted": bool(row and row["n"])}


# ═══════════════════════════════════════════════════════════════════════════════
# Ph46 — Enrichment Audit / Activity Log
# In-memory ring buffer (max 200) — resets on restart, no persistence needed.
# Enrichment endpoints call _audit() after completing work.
# ═══════════════════════════════════════════════════════════════════════════════

from collections import deque as _deque
from datetime import datetime as _dt, timezone as _tz

_audit_log: _deque = _deque(maxlen=200)


def _audit(action: str, subject: str, detail: str = "", ok: bool = True) -> None:
    _audit_log.appendleft({
        "ts":      _dt.now(_tz.utc).isoformat(timespec="seconds"),
        "action":  action,
        "subject": subject,
        "detail":  detail[:200],
        "ok":      ok,
    })


@app.get("/audit-log")
async def get_enrichment_audit_log(
    limit: int  = 50,
    _user: dict = Depends(get_current_user),
):
    """Return recent enrichment / crawl actions from the in-memory audit log."""
    n     = min(max(1, limit), 200)
    items = list(_audit_log)[:n]
    return {"entries": items, "total": len(_audit_log)}


# ═══════════════════════════════════════════════════════════════════════════════
# Ph47 — Entity Watchlist / Pinboard
# A single shared watchlist (Watchlist{id:'global'} node in Neo4j) that holds
# WATCHES relationships to pinned entities.  Single-user tool, no auth scoping.
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/watchlist")
async def get_watchlist(_user: dict = Depends(get_current_user)):
    """Return all entities currently pinned to the watchlist."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (w:Watchlist {id:'global'})-[r:WATCHES]->(n)
            RETURN n.id AS id, head(labels(n)) AS label,
                   coalesce(n.name, n.domain, n.ip, n.email, n.username, n.id) AS display,
                   toString(r.added_at) AS pinned_at
            ORDER BY r.added_at DESC
            """
        )
        rows = await result.data()
    return {"watchlist": [dict(r) for r in rows]}


@app.post("/watchlist/{entity_id:path}")
async def add_to_watchlist(entity_id: str, _user: dict = Depends(get_current_user)):
    """Pin an entity to the watchlist."""
    eid = entity_id.strip()
    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (n) WHERE n.id = $eid
            MERGE (w:Watchlist {id:'global'})
            MERGE (w)-[r:WATCHES]->(n)
            ON CREATE SET r.added_at = datetime()
            RETURN n.id AS found
            """,
            eid=eid,
        )
        row = await r.single()
    if not row:
        raise HTTPException(404, "Entity not found")
    return {"pinned": True, "entity_id": eid}


@app.delete("/watchlist/{entity_id:path}")
async def remove_from_watchlist(entity_id: str, _user: dict = Depends(get_current_user)):
    """Remove an entity from the watchlist."""
    eid = entity_id.strip()
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MATCH (w:Watchlist {id:'global'})-[r:WATCHES]->(n {id: $eid})
            DELETE r
            """,
            eid=eid,
        )
    return {"unpinned": True, "entity_id": eid}


# ═══════════════════════════════════════════════════════════════════════════════
# Ph49 — Edge Annotations
# Add a freetext analyst note to any graph relationship, keyed by
# (source_id, target_id, rel_type).  Stored as a `note` property on the rel.
# ═══════════════════════════════════════════════════════════════════════════════

class EdgeAnnotationRequest(BaseModel):
    source_id: str = Field(..., description="Source node ID")
    target_id: str = Field(..., description="Target node ID")
    rel_type:  str = Field(..., description="Relationship type (e.g. OWNS_DOMAIN)")
    note:      str = Field("", max_length=1000, description="Analyst note; empty string clears it")


@app.patch("/graph/edge-annotation")
async def annotate_edge(
    req:   EdgeAnnotationRequest,
    _user: dict = Depends(get_current_user),
):
    """
    Attach (or overwrite) an analyst note on a graph relationship.
    The relationship is identified by source ID, target ID, and type.
    Passing an empty string for `note` clears the annotation.
    """
    src, tgt, rtype = req.source_id.strip(), req.target_id.strip(), req.rel_type.strip()
    if not src or not tgt or not rtype:
        raise HTTPException(400, "source_id, target_id and rel_type are required")

    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (a {id: $src})-[r]->(b {id: $tgt})
            WHERE type(r) = $rtype
            SET r.note = $note, r.note_updated = datetime()
            RETURN count(r) AS n
            """,
            src=src, tgt=tgt, rtype=rtype, note=req.note.strip(),
        )
        row = await result.single()

    if not row or not row["n"]:
        raise HTTPException(404, "Relationship not found")

    _audit("EdgeAnnotation", f"{src}→{tgt}", detail=f"type={rtype}")
    return {"annotated": True, "source_id": src, "target_id": tgt, "rel_type": rtype}


@app.get("/graph/edge-annotation")
async def get_edge_annotation(
    source_id: str,
    target_id: str,
    rel_type:  str,
    _user:     dict = Depends(get_current_user),
):
    """Fetch the existing analyst note for a relationship."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (a {id: $src})-[r]->(b {id: $tgt})
            WHERE type(r) = $rtype
            RETURN r.note AS note, toString(r.note_updated) AS updated_at
            LIMIT 1
            """,
            src=source_id.strip(), tgt=target_id.strip(), rtype=rel_type.strip(),
        )
        row = await result.single()
    if not row:
        raise HTTPException(404, "Relationship not found")
    return {"note": row["note"] or "", "updated_at": row["updated_at"]}


# ═══════════════════════════════════════════════════════════════════════════════
# Ph50 — Case Dossier v2 (enhanced markdown)
# Richer export that pulls timeline, hypotheses, entity notes, health score,
# and enrichment data into a comprehensive investigative document.
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/case/{case_id}/dossier")
async def case_dossier_v2(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """
    Generate an enhanced Markdown dossier for a case.
    Includes: metadata, health score, subjects + enrichment, timeline,
    hypotheses, tasks, entity notes, and analyst tags.
    Returns text/markdown as a downloadable attachment.
    """
    cid    = _val_case_id(case_id)
    import asyncio as _asyncio

    # ── Gather all data in parallel ──────────────────────────────────────────
    async def _base():
        return await get_case_full(graph_db, cid)

    async def _timeline():
        async with graph_db.driver.session() as s:
            r = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_TIMELINE_EVENT]->(e:TimelineEvent)
                RETURN e.id AS id, e.title AS title, e.event_date AS event_date,
                       e.category AS category, e.description AS description,
                       e.source AS source
                ORDER BY e.event_date ASC
            """, cid=cid)
            return await r.data()

    async def _hypotheses():
        async with graph_db.driver.session() as s:
            r = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_HYPOTHESIS]->(h:Hypothesis)
                RETURN h.id AS id, h.title AS title, h.description AS description,
                       h.status AS status, h.confidence AS confidence,
                       toString(h.created_at) AS created_at
                ORDER BY h.created_at ASC
            """, cid=cid)
            return await r.data()

    async def _entity_notes():
        async with graph_db.driver.session() as s:
            r = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_SUBJECT]->(n)
                MATCH (n)-[:HAS_ENTITY_NOTE]->(note:EntityNote)
                RETURN coalesce(n.name,n.domain,n.ip,n.email,n.id) AS entity_name,
                       head(labels(n)) AS entity_label,
                       note.content AS content, note.note_type AS note_type,
                       toString(note.created_at) AS created_at
                ORDER BY note.created_at ASC
            """, cid=cid)
            return await r.data()

    async def _tags():
        async with graph_db.driver.session() as s:
            r = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_SUBJECT]->(n)
                WHERE size(coalesce(n.analyst_tags,[])) > 0
                RETURN coalesce(n.name,n.domain,n.ip,n.email,n.id) AS entity_name,
                       head(labels(n)) AS entity_label,
                       n.analyst_tags AS tags
            """, cid=cid)
            return await r.data()

    async def _health():
        try:
            from fastapi.testclient import TestClient
        except Exception:
            pass
        async with graph_db.driver.session() as s:
            # Inline simplified health calc
            r1 = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_TASK]->(t:Task)
                RETURN count(t) AS total,
                       sum(CASE WHEN t.done THEN 1 ELSE 0 END) AS done
            """, cid=cid)
            t = await r1.single()
            r2 = await s.run("""
                MATCH (c:Case {id:$cid})-[:HAS_SUBJECT]->(n)
                RETURN count(n) AS total,
                       sum(CASE WHEN n.risk_score IS NOT NULL OR n.asn IS NOT NULL
                                  OR n.registrant_org IS NOT NULL THEN 1 ELSE 0 END) AS enriched
            """, cid=cid)
            e = await r2.single()
            return {"tasks": t, "enrichment": e}

    bundle, timeline, hyps, enotes, tags, health_raw = await _asyncio.gather(
        _base(), _timeline(), _hypotheses(), _entity_notes(), _tags(), _health()
    )
    if not bundle:
        raise HTTPException(404, "Case not found")

    # ── Build Markdown ───────────────────────────────────────────────────────
    now     = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
    case    = bundle.get("case", {})
    subjs   = bundle.get("subjects", [])
    notes   = bundle.get("notes", [])
    tasks   = bundle.get("tasks", [])

    status_icon = {"open":"🟡","active":"🟢","closed":"⚫","archived":"🗄"}.get(case.get("status",""), "❓")
    priority_icon = {"critical":"🔴","high":"🟠","medium":"🟡","low":"🟢"}.get(case.get("priority",""), "⬜")

    md = []
    md.append(f"# 🔍 Case Dossier — {case.get('title','Untitled')}")
    md.append(f"\n> Generated: {now}  |  Fieldwork OSINT")
    md.append("\n---\n")

    # Metadata
    md.append("## 📋 Case Overview\n")
    md.append(f"| Field | Value |")
    md.append(f"|-------|-------|")
    md.append(f"| **Status** | {status_icon} {case.get('status','—')} |")
    md.append(f"| **Priority** | {priority_icon} {case.get('priority','—')} |")
    md.append(f"| **Created** | {(case.get('created_at') or '')[:10]} |")
    md.append(f"| **Updated** | {(case.get('updated_at') or '')[:10]} |")
    md.append(f"| **Subjects** | {len(subjs)} |")
    md.append(f"| **Notes** | {len(notes)} |")
    tasks_done = sum(1 for t in tasks if t.get("done"))
    md.append(f"| **Tasks** | {tasks_done}/{len(tasks)} complete |")
    if case.get("description"):
        md.append(f"\n**Description:** {case['description']}\n")

    # Health
    t_h = health_raw["tasks"]; e_h = health_raw["enrichment"]
    if t_h and t_h["total"]:
        pct_done = round(100 * t_h["done"] / t_h["total"])
        md.append(f"\n**Task completion:** {pct_done}%  "
                  f"**Enrichment coverage:** "
                  f"{round(100*e_h['enriched']/e_h['total']) if e_h and e_h['total'] else 'n/a'}%\n")

    # Subjects
    if subjs:
        md.append("\n---\n## 👥 Subjects\n")
        for s in subjs:
            props  = s.get("properties", {})
            label  = s.get("label", "Entity")
            name   = s.get("display_name") or s.get("id","?")
            icon   = {"Person":"👤","Domain":"🌐","IP":"📡","Email":"📧",
                      "Company":"🏢","Wallet":"💰","Phone":"📞"}.get(label,"🔵")
            md.append(f"### {icon} {name} `[{label}]`\n")

            # Key properties
            show_props = {k: v for k, v in props.items()
                         if k not in ("id","analyst_tags","created_at","updated_at","source")
                         and v and str(v).strip()}
            if show_props:
                md.append("| Property | Value |")
                md.append("|----------|-------|")
                for k, v in list(show_props.items())[:15]:
                    md.append(f"| {k.replace('_',' ').title()} | {str(v)[:120]} |")
                md.append("")

            # Tags
            etags = props.get("analyst_tags", [])
            if etags:
                tag_str = " ".join(f"`{t}`" for t in etags)
                md.append(f"**Tags:** {tag_str}\n")

    # Timeline
    if timeline:
        md.append("\n---\n## 📅 Investigation Timeline\n")
        for ev in timeline:
            date = (ev.get("event_date") or "")[:10]
            cat  = ev.get("category") or ""
            cat_icon = {"intelligence":"🔍","operation":"⚡","lead":"💡",
                        "development":"📈","finding":"✅","admin":"📋"}.get(cat,"📌")
            md.append(f"**{date}** {cat_icon} **{ev.get('title','Event')}**")
            if ev.get("description"):
                md.append(f"\n> {ev['description']}")
            if ev.get("source"):
                md.append(f"\n> *Source: {ev['source']}*")
            md.append("")

    # Hypotheses
    if hyps:
        md.append("\n---\n## 🧩 Hypothesis Board\n")
        status_icons = {"open":"🔵","investigating":"🟡",
                        "confirmed":"✅","rejected":"❌"}
        conf_icons   = {"low":"⬇","medium":"➡","high":"⬆"}
        for h in hyps:
            si = status_icons.get(h.get("status","open"),"❓")
            ci = conf_icons.get(h.get("confidence","medium"),"➡")
            md.append(f"- {si} **{h.get('title','Hypothesis')}** "
                      f"[{h.get('status','')} · confidence {ci} {h.get('confidence','')}]")
            if h.get("description"):
                md.append(f"  > {h['description']}")
        md.append("")

    # Tasks
    if tasks:
        md.append("\n---\n## ✅ Tasks\n")
        for t in tasks:
            chk  = "x" if t.get("done") else " "
            prio = {"critical":"🔴","high":"🟠","medium":"🟡","low":"🟢"}.get(t.get("priority",""), "")
            md.append(f"- [{chk}] {prio} {t.get('text','Task')}")
        md.append("")

    # Case notes
    if notes:
        md.append("\n---\n## 📝 Investigation Notes\n")
        pinned = [n for n in notes if n.get("pinned")]
        rest   = [n for n in notes if not n.get("pinned")]
        for n in (pinned + rest):
            pin = "📌 " if n.get("pinned") else ""
            ts  = (n.get("created_at") or "")[:10]
            md.append(f"**{pin}{n.get('author','Analyst')}** — {ts}")
            md.append(f"\n{n.get('content','')}\n")

    # Entity notes
    if enotes:
        md.append("\n---\n## 🗒 Entity Observations\n")
        by_entity: dict = {}
        for en in enotes:
            key = en.get("entity_name","?")
            by_entity.setdefault(key, []).append(en)
        for entity_name, notes_list in by_entity.items():
            md.append(f"**{entity_name}**")
            for en in notes_list:
                icon = {"observation":"👁","source":"📌","caution":"⚠",
                        "discrepancy":"❗","general":"📝"}.get(en.get("note_type",""),"📝")
                md.append(f"  - {icon} {en.get('content','')}")
            md.append("")

    # Tagged entities index
    if tags:
        md.append("\n---\n## 🏷 Analyst Tags Index\n")
        tag_index: dict = {}
        for row in tags:
            for tg in (row.get("tags") or []):
                tag_index.setdefault(tg, []).append(
                    f"{row.get('entity_label','?')} · {row.get('entity_name','?')}"
                )
        for tag, entities in sorted(tag_index.items()):
            md.append(f"**`{tag}`** — {', '.join(entities)}")
        md.append("")

    md.append("\n---\n*Generated by Fieldwork OSINT · fieldwork-osint/0.2*\n")

    content_str = "\n".join(md)
    safe_id     = case_id.replace("/", "_")
    _audit("DossierExport", cid, detail=f"subjects={len(subjs)}")
    return PlainTextResponse(
        content=content_str,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="fieldwork-dossier-{safe_id}.md"'
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Ph54 — API Key Manager
# Browse-based .env editor: see which keys are set, paste new values, save.
# Writes to the project-root .env and hot-reloads into the running process.
# ═══════════════════════════════════════════════════════════════════════════════

import os     as _os_mod
import re     as _re_mod
from pathlib import Path as _PathLib

# Project-root .env is two levels above main.py  (fieldwork/.env)
_DOT_ENV_PATH = (_PathLib(__file__).parent.parent / ".env").resolve()


def _write_env_key(key: str, value: str) -> None:
    """
    Write or update a single KEY=VALUE line in the project .env file.
    Creates the file if it doesn't exist.  Does NOT call os.environ —
    callers must do that themselves if hot-reload is needed.
    """
    existing: list[str] = []
    if _DOT_ENV_PATH.exists():
        existing = _DOT_ENV_PATH.read_text(encoding="utf-8").splitlines()

    updated = False
    new_lines: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            new_lines.append(f'{key}="{value}"')
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f'{key}="{value}"')

    _DOT_ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ── Runtime API-key persistence ──────────────────────────────────────────────
# Stored in /app/ which is volume-mounted to ./backend/app/ on the host,
# so values survive container restarts and uvicorn --reload resets.
_RUNTIME_KEYS_PATH = Path(__file__).parent / "runtime_api_keys.json"


def _load_runtime_keys() -> dict:
    try:
        if _RUNTIME_KEYS_PATH.exists():
            import json as _jrk
            return _jrk.loads(_RUNTIME_KEYS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_runtime_keys(updates: dict) -> None:
    """Merge updates into the persisted runtime key store."""
    data = _load_runtime_keys()
    for k, v in updates.items():
        if v:
            data[k] = v
        else:
            data.pop(k, None)
    try:
        import json as _jrk
        _RUNTIME_KEYS_PATH.write_text(_jrk.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# Restore runtime keys into os.environ at import time (runs on every reload).
# docker-compose injects empty strings for optional keys (SHODAN_API_KEY=""),
# so we check the VALUE, not just key presence, before overwriting.
for _rk, _rv in _load_runtime_keys().items():
    if _rv and not _os_mod.environ.get(_rk, "").strip():
        _os_mod.environ[_rk] = _rv

# ── Key verification ─────────────────────────────────────────────────────────
_VERIFY_PATH = Path(__file__).parent / "key_verifications.json"


def _load_verifications() -> dict:
    try:
        if _VERIFY_PATH.exists():
            import json as _jv
            return _jv.loads(_VERIFY_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_verifications(data: dict) -> None:
    try:
        import json as _jv
        _VERIFY_PATH.write_text(_jv.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


async def _test_api_key(name: str, value: str) -> tuple:
    """
    Returns (ok, message) where ok is True/False/None.
    None = verification not supported for this service.
    """
    import os as _osv
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
            if name == "SHODAN_API_KEY":
                r = await c.get("https://api.shodan.io/api-info", params={"key": value})
                if r.status_code == 200:
                    data = r.json()
                    plan = data.get("plan", "unknown")
                    return True, f"Valid — plan: {plan}"
                return False, "Invalid key"

            elif name == "VIRUSTOTAL_API_KEY":
                # account_quotas returns 403 on free keys — use ip lookup instead
                r = await c.get(
                    "https://www.virustotal.com/api/v3/ip_addresses/8.8.8.8",
                    headers={"x-apikey": value},
                )
                if r.status_code == 200: return True, "Valid"
                if r.status_code == 401: return False, "Invalid key"
                if r.status_code == 429: return False, "Rate limit — try again in a minute"
                return False, f"HTTP {r.status_code}"

            elif name == "ABUSEIPDB_KEY":
                r = await c.get("https://api.abuseipdb.com/api/v2/check",
                                params={"ipAddress": "1.1.1.1", "maxAgeInDays": "90"},
                                headers={"Key": value, "Accept": "application/json"})
                return (True, "Valid") if r.status_code == 200 else (False, "Invalid key")

            elif name == "HIBP_API_KEY":
                r = await c.get("https://haveibeenpwned.com/api/v3/breaches",
                                headers={"hibp-api-key": value,
                                         "User-Agent": "Fieldwork-OSINT-Checker/1.0"})
                return (True, "Valid") if r.status_code == 200 else (False, f"HTTP {r.status_code}")

            elif name == "NUMVERIFY_KEY":
                r = await c.get("https://apilayer.net/api/validate",
                                params={"access_key": value, "number": "14158586273",
                                        "country_code": "US", "format": "1"})
                if r.status_code == 200:
                    d = r.json()
                    if d.get("error"):
                        return False, d["error"].get("info", "Invalid key")
                    return True, "Valid"
                return False, f"HTTP {r.status_code}"

            elif name == "CENSYS_API_ID":
                secret = _osv.environ.get("CENSYS_API_SECRET", "")
                if not secret:
                    return None, "Save CENSYS_API_SECRET too — both are required"
                r = await c.get("https://search.censys.io/api/v2/account", auth=(value, secret))
                return (True, "Valid") if r.status_code == 200 else (False, "Invalid credentials")

            elif name == "CENSYS_API_SECRET":
                api_id = _osv.environ.get("CENSYS_API_ID", "")
                if not api_id:
                    return None, "Save CENSYS_API_ID too — both are required"
                r = await c.get("https://search.censys.io/api/v2/account", auth=(api_id, value))
                return (True, "Valid") if r.status_code == 200 else (False, "Invalid credentials")

            elif name == "DEHASHED_KEY":
                email = _osv.environ.get("DEHASHED_EMAIL", "")
                if not email:
                    return None, "Save DEHASHED_EMAIL too — both are required"
                r = await c.get(
                    "https://api.dehashed.com/search",
                    params={"query": "email:test@example.com", "size": "1"},
                    auth=(email, value),
                    headers={"Accept": "application/json"},
                )
                return (True, "Valid") if r.status_code == 200 else (False, f"HTTP {r.status_code}")

            elif name == "DEHASHED_EMAIL":
                key = _osv.environ.get("DEHASHED_KEY", "")
                if not key:
                    return None, "Save DEHASHED_KEY too — both are required"
                r = await c.get(
                    "https://api.dehashed.com/search",
                    params={"query": "email:test@example.com", "size": "1"},
                    auth=(value, key),
                    headers={"Accept": "application/json"},
                )
                return (True, "Valid") if r.status_code == 200 else (False, f"HTTP {r.status_code}")

            elif name == "OPENCORPORATES_TOKEN":
                r = await c.get("https://api.opencorporates.com/v0.4/account_status",
                                params={"api_token": value})
                return (True, "Valid") if r.status_code == 200 else (False, "Invalid token")

            elif name == "GITHUB_TOKEN":
                r = await c.get("https://api.github.com/user",
                                headers={"Authorization": f"Bearer {value}",
                                         "Accept": "application/vnd.github.v3+json"})
                if r.status_code == 200:
                    login = r.json().get("login", "?")
                    return True, f"Valid — @{login}"
                return False, "Invalid token"

            elif name == "NEWS_API_KEY":
                r = await c.get("https://newsapi.org/v2/top-headlines",
                                params={"country": "us", "pageSize": "1", "apiKey": value})
                if r.status_code == 200 and r.json().get("status") == "ok":
                    return True, "Valid"
                msg = r.json().get("message", "Invalid key") if r.status_code != 200 else "Invalid key"
                return False, msg

            elif name == "URLSCAN_API_KEY":
                r = await c.get("https://urlscan.io/user/", headers={"API-Key": value})
                return (True, "Valid") if r.status_code == 200 else (False, "Invalid key")

            elif name == "SAUCENAO_KEY":
                r = await c.get("https://saucenao.com/search.php",
                                params={"db": "999", "output_type": "2", "numres": "1",
                                        "url": "https://www.gstatic.com/webp/gallery/1.jpg",
                                        "api_key": value})
                return (True, "Valid") if r.status_code == 200 else (False, f"HTTP {r.status_code}")

            elif name == "ALEPH_API_KEY":
                r = await c.get("https://aleph.occrp.org/api/2/accounts/me",
                                headers={"Authorization": f"ApiKey {value}"})
                return (True, "Valid") if r.status_code == 200 else (False, "Invalid key")

            elif name == "OPENSKY_USERNAME":
                pwd = _osv.environ.get("OPENSKY_PASSWORD", "")
                if not pwd:
                    return None, "Save OPENSKY_PASSWORD too — both are required"
                r = await c.get("https://opensky-network.org/api/states/own",
                                auth=(value, pwd))
                return (True, "Valid") if r.status_code == 200 else (False, "Invalid credentials")

            elif name == "OPENSKY_PASSWORD":
                user = _osv.environ.get("OPENSKY_USERNAME", "")
                if not user:
                    return None, "Save OPENSKY_USERNAME too — both are required"
                r = await c.get("https://opensky-network.org/api/states/own",
                                auth=(user, value))
                return (True, "Valid") if r.status_code == 200 else (False, "Invalid credentials")

            elif name == "GREYNOISE_API_KEY":
                r = await c.get("https://api.greynoise.io/ping",
                                headers={"key": value})
                return (True, "Valid") if r.status_code == 200 else (False, "Invalid key")

            elif name == "OTX_API_KEY":
                r = await c.get("https://otx.alienvault.com/api/v1/user/me",
                                headers={"X-OTX-API-KEY": value})
                if r.status_code == 200:
                    username = r.json().get("username", "?")
                    return True, f"Valid — @{username}"
                return False, "Invalid key"

            elif name in ("OLLAMA_MODEL", "OLLAMA_EMBED_MODEL"):
                ollama_url = _osv.environ.get("OLLAMA_URL", "http://ollama:11434")
                r = await c.get(f"{ollama_url}/api/tags", timeout=5.0)
                if r.status_code == 200:
                    models = [m["name"] for m in r.json().get("models", [])]
                    if any(value in m for m in models):
                        return True, f"Available"
                    short = ", ".join(models[:4]) or "none pulled"
                    return False, f"Model not found (have: {short})"
                return False, "Ollama unreachable"

            elif name == "ARKHAM_API_KEY":
                r = await c.get(
                    "https://api.arkhamintelligence.com/intelligence/address/"
                    "0x742d35Cc6634C0532925a3b8D4C04D23E1D3e0bD/entities",
                    headers={"API-Key": value},
                )
                return (True, "Valid") if r.status_code in (200, 404) else (False, "Invalid key")

            else:
                return None, "Verification not supported"
    except httpx.TimeoutException:
        return False, "Request timed out"
    except Exception as exc:
        return False, str(exc)[:120]


# Every API key Fieldwork supports, with free-tier signup links
_API_KEY_REGISTRY = [
    {"key": "SHODAN_API_KEY",       "service": "Shodan",             "free": True,  "note": "100 credits/month",         "url": "https://account.shodan.io/register"},
    {"key": "VIRUSTOTAL_API_KEY",   "service": "VirusTotal",         "free": True,  "note": "500 req/day",               "url": "https://www.virustotal.com/gui/join-us"},
    {"key": "ABUSEIPDB_KEY",        "service": "AbuseIPDB",          "free": True,  "note": "1 000 checks/day",          "url": "https://www.abuseipdb.com/register"},
    {"key": "HIBP_API_KEY",         "service": "HaveIBeenPwned",     "free": False, "note": "From $3.50/month",          "url": "https://haveibeenpwned.com/API/Key"},
    {"key": "NUMVERIFY_KEY",        "service": "NumVerify",          "free": True,  "note": "100 req/month",             "url": "https://numverify.com/product"},
    {"key": "CENSYS_API_ID",        "service": "Censys ID",          "free": True,  "note": "250 req/month",             "url": "https://accounts.censys.io/register"},
    {"key": "CENSYS_API_SECRET",    "service": "Censys Secret",      "free": True,  "note": "250 req/month",             "url": "https://accounts.censys.io/register"},
    {"key": "DEHASHED_KEY",         "service": "DeHashed",           "free": False, "note": "$5.49/month",               "url": "https://www.dehashed.com/register"},
    {"key": "DEHASHED_EMAIL",       "service": "DeHashed (email)",   "free": False, "note": "Account email",             "url": "https://www.dehashed.com/register"},
    {"key": "OPENCORPORATES_TOKEN", "service": "OpenCorporates",     "free": True,  "note": "Free for non-commercial",   "url": "https://opencorporates.com/users/account_requests/new"},
    {"key": "GITHUB_TOKEN",         "service": "GitHub PAT",         "free": True,  "note": "Free with account",         "url": "https://github.com/settings/tokens"},
    {"key": "NEWS_API_KEY",         "service": "NewsAPI",            "free": True,  "note": "100 req/day developer plan","url": "https://newsapi.org/register"},
    {"key": "URLSCAN_API_KEY",      "service": "URLScan.io",         "free": True,  "note": "Free — enables submissions","url": "https://urlscan.io/user/signup"},
    {"key": "ARKHAM_API_KEY",       "service": "Arkham Intel",       "free": False, "note": "Requires application",      "url": "https://platform.arkhamintelligence.com"},
    {"key": "SAUCENAO_KEY",         "service": "SauceNAO",           "free": True,  "note": "150 searches/day",          "url": "https://saucenao.com/user.php?page=register"},
    {"key": "ALEPH_API_KEY",        "service": "OCCRP Aleph",        "free": True,  "note": "Free with account",         "url": "https://aleph.occrp.org/"},
    {"key": "OPENSKY_USERNAME",     "service": "OpenSky (user)",     "free": True,  "note": "Free with account",         "url": "https://opensky-network.org/index.php?option=com_users&view=registration"},
    {"key": "OPENSKY_PASSWORD",     "service": "OpenSky (pass)",     "free": True,  "note": "Free with account",         "url": "https://opensky-network.org/index.php?option=com_users&view=registration"},
    {"key": "GREYNOISE_API_KEY",    "service": "GreyNoise",          "free": True,  "note": "Works keyless; key = higher limits", "url": "https://viz.greynoise.io/account/signup"},
    {"key": "OTX_API_KEY",          "service": "AlienVault OTX",     "free": True,  "note": "Free with account",         "url": "https://otx.alienvault.com/signup"},
    {"key": "ANTHROPIC_API_KEY",    "service": "Claude (Anthropic)", "free": False, "note": "Optional — powers Claude AI for summaries/chat/hypotheses. Or use the free Claude Code bridge (subscription) instead.", "url": "https://console.anthropic.com/settings/keys"},
    {"key": "COMPANIES_HOUSE_KEY",  "service": "UK Companies House", "free": True,  "note": "Free — officers + beneficial owners (PSC)", "url": "https://developer.company-information.service.gov.uk/"},
    {"key": "ETHERSCAN_API_KEY",    "service": "Etherscan",          "free": True,  "note": "Free — 5 req/s ETH on-chain tracing", "url": "https://etherscan.io/apis"},
    {"key": "REDDIT_CLIENT_ID",     "service": "Reddit app ID",      "free": True,  "note": "Free 'script' app — required since Reddit blocked public JSON", "url": "https://www.reddit.com/prefs/apps"},
    {"key": "REDDIT_CLIENT_SECRET", "service": "Reddit app secret",  "free": True,  "note": "Free 'script' app secret",  "url": "https://www.reddit.com/prefs/apps"},
    {"key": "OLLAMA_MODEL",         "service": "Ollama model name",  "free": True,  "note": "e.g. llama3, mistral",      "url": "https://ollama.com/library"},
    {"key": "OLLAMA_EMBED_MODEL",   "service": "Ollama embed model", "free": True,  "note": "e.g. nomic-embed-text",     "url": "https://ollama.com/library"},
]

_SENSITIVE_KEYS = {"JWT_SECRET", "ACCESS_TOKEN_EXPIRE_HOURS"}


@app.get("/settings/api-keys")
async def get_api_key_status(_user: dict = Depends(get_current_user)):
    """
    Return the status of all supported API keys.
    Values are masked (first 4 chars + bullets); never returned in full.
    Includes persisted verification results (verified/invalid/untested).
    """
    verifications = _load_verifications()
    out = []
    for entry in _API_KEY_REGISTRY:
        raw  = _os_mod.environ.get(entry["key"], "").strip()
        conf = bool(raw)
        masked = (raw[:4] + "•" * min(len(raw) - 4, 16)) if conf else ""
        v = verifications.get(entry["key"], {})
        out.append({
            **entry,
            "configured":  conf,
            "masked":      masked,
            "verified":    v.get("ok"),       # True / False / None
            "verify_msg":  v.get("msg", ""),
        })
    return {"keys": out, "env_file": str(_DOT_ENV_PATH)}


@app.post("/settings/api-keys/verify")
async def verify_api_key(request: Request, _user: dict = Depends(get_current_user)):
    """
    Test a single API key against its service.
    Body: {"key": "SHODAN_API_KEY"}
    Returns: {"ok": true/false/null, "msg": "..."}
    """
    import json as _jvr
    try:
        body = await request.body()
        data = _jvr.loads(body)
        key_name = data.get("key", "")
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    if not key_name:
        raise HTTPException(400, "key is required")

    value = _os_mod.environ.get(key_name, "").strip()
    if not value:
        raise HTTPException(400, f"{key_name} is not configured")

    ok, msg = await _test_api_key(key_name, value)

    # Persist result
    verifications = _load_verifications()
    import time as _time
    verifications[key_name] = {"ok": ok, "msg": msg, "ts": int(_time.time())}
    _save_verifications(verifications)

    return {"ok": ok, "msg": msg}


@app.post("/settings/api-keys")
async def update_api_keys(
    request: Request,
    _user:   dict = Depends(get_current_user),
):
    """
    Write API key updates to the project .env file and hot-reload into this
    process so changes take effect without a restart.
    Ignores keys not in the registry; rejects reserved keys (JWT_SECRET etc.).
    """
    import json as _json_apikeys
    try:
        body = await request.body()
        data = _json_apikeys.loads(body)
        updates_raw = data.get("updates", {})
        if not isinstance(updates_raw, dict):
            raise ValueError("updates must be a dict")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}")

    valid_keys = {e["key"] for e in _API_KEY_REGISTRY}
    filtered   = {
        k: v.strip()
        for k, v in updates_raw.items()
        if k in valid_keys and k not in _SENSITIVE_KEYS
    }
    if not filtered:
        raise HTTPException(400, "No valid/permitted API key names provided")

    # ── Read existing .env preserving comments + unrelated keys ─────────────
    existing_lines: list[str] = []
    if _DOT_ENV_PATH.exists():
        existing_lines = _DOT_ENV_PATH.read_text(encoding="utf-8").splitlines()

    written_keys: set[str] = set()
    new_lines: list[str]   = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" in stripped:
            key_part = stripped.split("=", 1)[0].strip()
            if key_part in filtered:
                val = filtered[key_part]
                new_lines.append(f'{key_part}="{val}"' if val else f"# {key_part}=")
                written_keys.add(key_part)
                continue
        new_lines.append(line)

    # Append keys not already in the file
    for k, v in filtered.items():
        if k not in written_keys:
            if v:
                new_lines.append(f'{k}="{v}"')
            # If v is empty and key didn't exist, nothing to do

    _DOT_ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # ── Hot-reload into running process + persist for next startup ──────────
    for k, v in filtered.items():
        if v:
            _os_mod.environ[k] = v
        else:
            _os_mod.environ.pop(k, None)

    _save_runtime_keys(filtered)   # survives uvicorn --reload and container restart

    return {"updated": sorted(filtered.keys()), "env_file": str(_DOT_ENV_PATH)}


# ═══════════════════════════════════════════════════════════════════════════════
# Ph55 — Shodan InternetDB (keyless) + GreyNoise Community (keyless)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/enrich/ip/{ip}/internetdb")
async def ip_internetdb(ip: str, _user: dict = Depends(get_current_user)):
    """
    Shodan InternetDB — open ports, CVEs, hostnames, tags.
    Completely free; no API key or Shodan account required.
    """
    i   = _validate_ip(ip)
    res = await enrich_ip_internetdb(graph_db, i)
    _audit("InternetDB", i,
           detail=f"ports={len(res.get('ports',[]))} cves={len(res.get('cves',[]))}")
    return res


@app.get("/enrich/ip/{ip}/greynoise")
async def ip_greynoise(ip: str, _user: dict = Depends(get_current_user)):
    """
    GreyNoise Community — classify IP as scanner / benign / malicious.
    Works without an API key; add GREYNOISE_API_KEY for higher rate limits.
    """
    i   = _validate_ip(ip)
    res = await enrich_ip_greynoise(graph_db, i)
    _audit("GreyNoise", i, detail=f"class={res.get('classification','?')} noise={res.get('noise')}")
    return res


# ═══════════════════════════════════════════════════════════════════════════════
# Ph56 — abuse.ch Feeds (URLhaus / ThreatFox / MalwareBazaar)
# All completely free, no API key required.
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/enrich/ip/{ip}/urlhaus")
async def ip_urlhaus(ip: str, _user: dict = Depends(get_current_user)):
    """Check an IP against URLhaus (malware hosting database)."""
    i   = _validate_ip(ip)
    res = await check_urlhaus(graph_db, i)
    _audit("URLhaus", i, detail=f"urls={res.get('url_count',0)}")
    return res


@app.get("/enrich/domain/{domain}/urlhaus")
async def domain_urlhaus(domain: str, _user: dict = Depends(get_current_user)):
    """Check a domain against URLhaus (malware hosting database)."""
    d   = _validate_domain(domain)
    res = await check_urlhaus(graph_db, d)
    _audit("URLhaus", d, detail=f"urls={res.get('url_count',0)}")
    return res


@app.get("/enrich/threatfox")
async def threatfox_search(
    ioc:   str,
    _user: dict = Depends(get_current_user),
):
    """
    Search ThreatFox for any IOC type: IP:port, domain, URL, MD5, SHA256.
    Returns threat actor tags, malware families, confidence scores.
    """
    if not ioc or len(ioc) > 500:
        raise HTTPException(400, "ioc parameter required (max 500 chars)")
    res = await search_threatfox(ioc.strip())
    _audit("ThreatFox", ioc[:80], detail=f"hits={res.get('count',0)}")
    return res


@app.get("/enrich/malwarebazaar")
async def malwarebazaar_lookup(
    hash:  str,
    _user: dict = Depends(get_current_user),
):
    """
    Look up a file hash (MD5 / SHA1 / SHA256) in MalwareBazaar.
    Returns malware family name, file metadata, and AV vendor hits.
    """
    h = hash.strip()
    if not h or not _re_mod.match(r"^[0-9a-fA-F]{32,64}$", h):
        raise HTTPException(400, "Provide a valid MD5 (32), SHA1 (40), or SHA256 (64) hex hash")
    res = await check_malwarebazaar(h)
    _audit("MalwareBazaar", h[:16] + "…", detail=f"found={res.get('found')} sig={res.get('signature','')}")
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Ph57 — Bulk IOC Import
# ─────────────────────────────────────────────────────────────────────────────
import json as _json_mod
import uuid as _uuid57

_IOC_TYPE_PATTERNS = [
    # SHA256 hash (64 hex)
    (_re_mod.compile(r"^[0-9a-fA-F]{64}$"),         "Hash",    "sha256"),
    # SHA1 hash (40 hex)
    (_re_mod.compile(r"^[0-9a-fA-F]{40}$"),          "Hash",    "sha1"),
    # MD5 hash (32 hex)
    (_re_mod.compile(r"^[0-9a-fA-F]{32}$"),          "Hash",    "md5"),
    # IPv4 address
    (_re_mod.compile(r"^\d{1,3}(\.\d{1,3}){3}$"),   "IP",      "ipv4"),
    # IPv4:port
    (_re_mod.compile(r"^\d{1,3}(\.\d{1,3}){3}:\d+$"), "IP",    "ipv4_port"),
    # URL (http/https/ftp)
    (_re_mod.compile(r"^https?://|^ftp://", _re_mod.I), "URL",  "url"),
    # Email address
    (_re_mod.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"), "Email", "email"),
    # Domain — must contain at least one dot, no spaces
    (_re_mod.compile(r"^(?:[a-zA-Z0-9_\-]+\.)+[a-zA-Z]{2,}$"), "Domain", "domain"),
]

def _classify_ioc(raw: str):
    """Return (ioc_type_label, subtype) for a raw IOC string, or ('Unknown','unknown')."""
    v = raw.strip()
    for pat, label, sub in _IOC_TYPE_PATTERNS:
        if pat.search(v):
            return label, sub
    return "Unknown", "unknown"


class BulkIOCRequest(BaseModel):
    text:         str  = Field(..., description="Raw text — one IOC per line, or comma/space separated")
    case_id:      str  = Field("", description="Optional: add all created entities to this case")
    auto_enrich:  bool = Field(False, description="Run free keyless enrichments after import")
    tags:         List[str] = Field(default_factory=list)


@app.post("/ioc/bulk-import")
async def bulk_ioc_import(
    req:           BulkIOCRequest,
    background:    BackgroundTasks,
    _user:         dict = Depends(get_current_user),
):
    """
    Parse a blob of text, classify each token as IP/Domain/URL/Hash/Email,
    create graph nodes for the ones that are new, optionally add them to a case
    and schedule background enrichment.

    Returns a summary: {total, created, skipped, entities:[...]}
    """
    # ── Parse tokens ─────────────────────────────────────────────────────────
    import re as _re2
    # split on whitespace, commas, semicolons, pipes
    raw_tokens = _re2.split(r"[\s,;|]+", req.text.strip())
    tokens     = [t.strip() for t in raw_tokens if len(t.strip()) >= 4]

    results   = []
    created   = 0
    skipped   = 0
    seen      = set()

    async with graph_db.driver.session() as session:
        for tok in tokens:
            if tok in seen:
                continue
            seen.add(tok)

            label, subtype = _classify_ioc(tok)
            if label == "Unknown":
                skipped += 1
                continue

            # Build node properties
            if label == "IP":
                # strip port if present
                ip_val = tok.split(":")[0]
                nid    = f"ip_{hashlib.sha1(ip_val.encode()).hexdigest()[:12]}"
                props  = {"address": ip_val, "source": "bulk_import"}
                merge_key = "address"
                merge_val = ip_val
            elif label == "Domain":
                nid    = f"domain_{hashlib.sha1(tok.encode()).hexdigest()[:12]}"
                props  = {"domain": tok, "source": "bulk_import"}
                merge_key = "domain"
                merge_val = tok
            elif label == "Hash":
                nid    = f"hash_{hashlib.sha1(tok.encode()).hexdigest()[:12]}"
                props  = {"value": tok, "hash_type": subtype, "source": "bulk_import"}
                merge_key = "value"
                merge_val = tok
            elif label == "URL":
                nid    = f"url_{hashlib.sha1(tok.encode()).hexdigest()[:12]}"
                props  = {"url": tok, "source": "bulk_import"}
                merge_key = "url"
                merge_val = tok
            elif label == "Email":
                nid    = f"email_{hashlib.sha1(tok.encode()).hexdigest()[:12]}"
                props  = {"email": tok, "source": "bulk_import"}
                merge_key = "email"
                merge_val = tok
            else:
                skipped += 1
                continue

            if req.tags:
                props["tags"] = req.tags

            # MERGE node
            set_clause = ", ".join(
                f"n.{k} = ${k}" for k in props if k not in ("source",)
            )
            await session.run(
                f"""
                MERGE (n:{label} {{id: $id}})
                ON CREATE SET n.{merge_key} = $val,
                              n.source      = $src,
                              n.created_at  = datetime()
                SET n.updated_at = datetime()
                {", n." + set_clause if set_clause else ""}
                """,
                id=nid, val=merge_val, src="bulk_import",
                **{k: v for k, v in props.items() if k not in (merge_key, "source")},
            )

            entity = {
                "id":      nid,
                "label":   label,
                "subtype": subtype,
                "value":   merge_val,
            }
            results.append(entity)
            created += 1

            # Add to case if requested
            if req.case_id:
                cid = req.case_id.strip()
                try:
                    rel_id = str(_uuid57.uuid4())
                    await session.run(
                        """
                        MATCH  (c:Case {id: $cid})
                        MATCH  (e {id: $eid})
                        MERGE  (c)-[r:HAS_ENTITY]->(e)
                        ON CREATE SET r.id=$rid, r.role='ioc', r.added_at=datetime()
                        """,
                        cid=cid, eid=nid, rid=rel_id,
                    )
                except Exception:
                    pass

    _audit("BulkImport", f"{created} IOCs", detail=f"total_tokens={len(tokens)} skipped={skipped}")

    # ── Optional background enrichment ────────────────────────────────────────
    if req.auto_enrich and results:
        async def _run_enrichment():
            for ent in results:
                try:
                    if ent["label"] == "IP":
                        await enrich_ip_internetdb(graph_db, ent["value"])
                        await enrich_ip_greynoise(graph_db,  ent["value"])
                        await check_urlhaus(graph_db,        ent["value"])
                    elif ent["label"] == "Domain":
                        await check_urlhaus(graph_db,        ent["value"])
                    elif ent["label"] == "Hash":
                        await check_malwarebazaar(ent["value"])
                except Exception as exc:
                    log.warning("Auto-enrich failed for %s: %s", ent["value"], exc)

        background.add_task(_run_enrichment)

    return {
        "total":    len(tokens),
        "created":  created,
        "skipped":  skipped,
        "entities": results,
    }


@app.get("/ioc/parse-preview")
async def ioc_parse_preview(
    text:  str,
    _user: dict = Depends(get_current_user),
):
    """
    Dry-run: parse and classify IOCs without writing anything.
    Returns the classification breakdown so the UI can show a preview.
    """
    import re as _re3
    raw_tokens = _re3.split(r"[\s,;|]+", text.strip())
    tokens     = [t.strip() for t in raw_tokens if len(t.strip()) >= 4]
    seen       = set()
    parsed     = []
    counts: dict = {}
    for tok in tokens:
        if tok in seen:
            continue
        seen.add(tok)
        label, subtype = _classify_ioc(tok)
        parsed.append({"value": tok, "label": label, "subtype": subtype})
        counts[label] = counts.get(label, 0) + 1

    return {"total": len(parsed), "counts": counts, "items": parsed[:200]}


# ─────────────────────────────────────────────────────────────────────────────
# Ph58 — Auto-Enrichment Pipeline
# ─────────────────────────────────────────────────────────────────────────────
import json as _json58

_AUTO_ENRICH_CONFIG_KEY = "AUTO_ENRICH_CONFIG"

# Default config stored in .env as JSON; falls back to this dict
_AUTO_ENRICH_DEFAULTS: dict = {
    "enabled":  False,
    "on_node_types": ["IP", "Domain", "Hash"],
    "sources": {
        "IP":     ["internetdb", "greynoise", "urlhaus"],
        "Domain": ["urlhaus"],
        "Hash":   ["malwarebazaar"],
    },
}

def _load_auto_enrich_cfg() -> dict:
    raw = os.getenv(_AUTO_ENRICH_CONFIG_KEY, "")
    if raw:
        try:
            return _json58.loads(raw)
        except Exception:
            pass
    return dict(_AUTO_ENRICH_DEFAULTS)


class AutoEnrichConfig(BaseModel):
    enabled:       bool              = False
    on_node_types: List[str]         = Field(default_factory=lambda: ["IP","Domain","Hash"])
    sources:       dict              = Field(default_factory=dict)


@app.get("/settings/auto-enrich")
async def get_auto_enrich_config(_user: dict = Depends(get_current_user)):
    """Return current auto-enrichment pipeline configuration."""
    return _load_auto_enrich_cfg()


@app.post("/settings/auto-enrich")
async def save_auto_enrich_config(
    cfg:   AutoEnrichConfig,
    _user: dict = Depends(get_current_user),
):
    """Persist auto-enrichment config to .env (hot-reload)."""
    data = cfg.dict()
    val  = _json58.dumps(data)
    # Write to .env
    _write_env_key(_AUTO_ENRICH_CONFIG_KEY, val)
    os.environ[_AUTO_ENRICH_CONFIG_KEY] = val
    _audit("AutoEnrich", "config saved", detail=f"enabled={cfg.enabled}")
    return {"ok": True, "config": data}


async def _maybe_auto_enrich(label: str, value: str) -> None:
    """Called after a node is created; runs configured enrichments if enabled."""
    cfg = _load_auto_enrich_cfg()
    if not cfg.get("enabled"):
        return
    node_types = cfg.get("on_node_types", [])
    if label not in node_types:
        return
    sources = cfg.get("sources", {}).get(label, [])
    for src in sources:
        try:
            if label == "IP":
                if src == "internetdb":
                    await enrich_ip_internetdb(graph_db, value)
                elif src == "greynoise":
                    await enrich_ip_greynoise(graph_db, value)
                elif src == "urlhaus":
                    await check_urlhaus(graph_db, value)
            elif label == "Domain":
                if src == "urlhaus":
                    await check_urlhaus(graph_db, value)
            elif label == "Hash":
                if src == "malwarebazaar":
                    await check_malwarebazaar(value)
        except Exception as exc:
            log.warning("auto-enrich %s %s via %s: %s", label, value, src, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Ph59 — STIX 2.1 Bundle Export
# ─────────────────────────────────────────────────────────────────────────────
import datetime as _dt59
import uuid     as _uuid59

_STIX_SPEC_VERSION = "2.1"
_STIX_FIELDWORK_ID = "identity--fieldwork-osint-platform"


def _stix_ts() -> str:
    return _dt59.datetime.now(_dt59.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _neo4j_node_to_stix(node: dict) -> dict | None:
    """
    Convert a Neo4j property dict (with .label / .id keys added by the query)
    to a STIX 2.1 object.  Returns None for unmapped node types.
    """
    label   = node.get("_label", "")
    ts      = _stix_ts()
    cid     = f"identity--{_uuid59.uuid4()}"
    oid     = f"identity--{_uuid59.uuid4()}"  # reused per branch

    if label == "IP":
        ip = node.get("address", "")
        if not ip:
            return None
        return {
            "type":             "ipv4-addr",
            "spec_version":     _STIX_SPEC_VERSION,
            "id":               f"ipv4-addr--{_uuid59.uuid5(_uuid59.NAMESPACE_URL, ip)}",
            "value":            ip,
            "x_fieldwork_id":   node.get("id", ""),
            "x_open_ports":     node.get("open_ports", []),
            "x_cves":           node.get("cves", []),
            "x_gn_verdict":     node.get("gn_classification", ""),
            "x_urlhaus":        bool(node.get("urlhaus_listed", False)),
        }
    elif label == "Domain":
        domain = node.get("domain", "")
        if not domain:
            return None
        return {
            "type":           "domain-name",
            "spec_version":   _STIX_SPEC_VERSION,
            "id":             f"domain-name--{_uuid59.uuid5(_uuid59.NAMESPACE_URL, domain)}",
            "value":          domain,
            "x_fieldwork_id": node.get("id", ""),
            "x_urlhaus":      bool(node.get("urlhaus_listed", False)),
        }
    elif label == "URL":
        url = node.get("url", "")
        if not url:
            return None
        return {
            "type":           "url",
            "spec_version":   _STIX_SPEC_VERSION,
            "id":             f"url--{_uuid59.uuid5(_uuid59.NAMESPACE_URL, url)}",
            "value":          url,
            "x_fieldwork_id": node.get("id", ""),
        }
    elif label == "Email":
        email = node.get("email", "")
        if not email:
            return None
        return {
            "type":           "email-addr",
            "spec_version":   _STIX_SPEC_VERSION,
            "id":             f"email-addr--{_uuid59.uuid5(_uuid59.NAMESPACE_URL, email)}",
            "value":          email,
            "x_fieldwork_id": node.get("id", ""),
        }
    elif label == "Hash":
        val   = node.get("value", "")
        htype = node.get("hash_type", "sha256").lower()
        if not val:
            return None
        hmap = {"md5": "MD5", "sha1": "SHA-1", "sha256": "SHA-256"}
        return {
            "type":         "file",
            "spec_version": _STIX_SPEC_VERSION,
            "id":           f"file--{_uuid59.uuid5(_uuid59.NAMESPACE_URL, val)}",
            "hashes":       {hmap.get(htype, "SHA-256"): val},
            "x_fieldwork_id": node.get("id", ""),
            "x_signature":  node.get("mb_signature", ""),
        }
    elif label in ("Person", "Company", "Organisation"):
        name = node.get("name", node.get("display_name", ""))
        if not name:
            return None
        return {
            "type":           "identity",
            "spec_version":   _STIX_SPEC_VERSION,
            "id":             f"identity--{_uuid59.uuid5(_uuid59.NAMESPACE_URL, name + label)}",
            "name":           name,
            "identity_class": "individual" if label == "Person" else "organization",
            "x_fieldwork_id": node.get("id", ""),
            "created":        ts,
            "modified":       ts,
        }
    return None


@app.get("/case/{case_id}/stix")
async def export_case_stix(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """
    Export all entities linked to a case as a STIX 2.1 Bundle (JSON).
    Includes Indicator objects for IP/Domain/Hash/URL nodes with
    enrichment properties as STIX extensions, and Relationship objects
    for graph edges between STIX-mappable nodes.

    Returns application/json; client should save as .json.
    """
    import json as _jsonstix
    cid = case_id.strip()
    if not cid:
        raise HTTPException(400, "case_id required")

    ts = _stix_ts()

    async with graph_db.driver.session() as session:
        # Fetch case metadata
        meta_r = await session.run(
            "MATCH (c:Case {id:$id}) RETURN c", id=cid
        )
        meta_rec = await meta_r.single()
        if not meta_rec:
            raise HTTPException(404, "Case not found")
        case_node = dict(meta_rec["c"])

        # Fetch all entities linked to the case
        ents_r = await session.run(
            """
            MATCH (c:Case {id:$id})-[:HAS_ENTITY]->(e)
            RETURN e, labels(e) AS lbs
            """,
            id=cid,
        )
        ent_records = await ents_r.fetch(500)

        # Fetch edges between those entities
        edges_r = await session.run(
            """
            MATCH (c:Case {id:$id})-[:HAS_ENTITY]->(a)
            MATCH (c)-[:HAS_ENTITY]->(b)
            MATCH (a)-[r]->(b)
            WHERE type(r) <> 'HAS_ENTITY'
            RETURN a.id AS src_id, b.id AS tgt_id, type(r) AS rel_type
            LIMIT 500
            """,
            id=cid,
        )
        edge_records = await edges_r.fetch(500)

    # ── Build STIX objects ────────────────────────────────────────────────────
    stix_objects: list = []
    fieldwork_identity = {
        "type":           "identity",
        "spec_version":   _STIX_SPEC_VERSION,
        "id":             _STIX_FIELDWORK_ID,
        "name":           "Fieldwork OSINT Platform",
        "identity_class": "system",
        "created":        ts,
        "modified":       ts,
    }
    stix_objects.append(fieldwork_identity)

    # Case as a Report object
    report_obj = {
        "type":             "report",
        "spec_version":     _STIX_SPEC_VERSION,
        "id":               f"report--{_uuid59.uuid5(_uuid59.NAMESPACE_URL, cid)}",
        "name":             case_node.get("title", case_node.get("id", cid)),
        "description":      case_node.get("description", ""),
        "published":        ts,
        "created":          ts,
        "modified":         ts,
        "created_by_ref":   _STIX_FIELDWORK_ID,
        "object_refs":      [],   # filled below
        "x_fieldwork_status": case_node.get("status", ""),
        "x_fieldwork_priority": case_node.get("priority", ""),
    }

    # Map fieldwork node id → STIX id for relationship building
    fw_to_stix: dict = {}

    for rec in ent_records:
        node  = dict(rec["e"])
        lbs   = rec["lbs"] or []
        node["_label"] = lbs[0] if lbs else "Unknown"
        stix_obj = _neo4j_node_to_stix(node)
        if stix_obj:
            stix_objects.append(stix_obj)
            fw_to_stix[node.get("id", "")] = stix_obj["id"]
            report_obj["object_refs"].append(stix_obj["id"])

    # Relationships between STIX-mapped nodes
    for rec in edge_records:
        src_stix = fw_to_stix.get(rec["src_id"])
        tgt_stix = fw_to_stix.get(rec["tgt_id"])
        if not src_stix or not tgt_stix:
            continue
        rel_type = rec["rel_type"].lower().replace("_", "-")
        rel_obj  = {
            "type":               "relationship",
            "spec_version":       _STIX_SPEC_VERSION,
            "id":                 f"relationship--{_uuid59.uuid4()}",
            "relationship_type":  rel_type,
            "source_ref":         src_stix,
            "target_ref":         tgt_stix,
            "created":            ts,
            "modified":           ts,
            "created_by_ref":     _STIX_FIELDWORK_ID,
        }
        stix_objects.append(rel_obj)
        report_obj["object_refs"].append(rel_obj["id"])

    stix_objects.append(report_obj)

    bundle = {
        "type":    "bundle",
        "id":      f"bundle--{_uuid59.uuid4()}",
        "objects": stix_objects,
    }

    _audit("STIX Export", cid, detail=f"objects={len(stix_objects)}")

    return Response(
        content=_jsonstix.dumps(bundle, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition":
                f'attachment; filename="fieldwork-stix-{cid[:8]}.json"'
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ph60 — Entity Risk Scoring
# ─────────────────────────────────────────────────────────────────────────────

def _compute_risk_score(props: dict) -> dict:
    """
    Synthesise all enrichment signals stored on a Neo4j node into a 0–100 score.
    Returns {"score": int, "level": str, "signals": [{"label": str, "points": int}]}
    """
    score   = 0
    signals = []

    def _add(label: str, pts: int) -> None:
        nonlocal score
        score += pts
        signals.append({"label": label, "points": pts})

    # ── abuse.ch URLhaus ──────────────────────────────────────────────────────
    if props.get("urlhaus_listed"):
        uc = int(props.get("urlhaus_urls", 1) or 1)
        pts = min(40 + (uc - 1) * 2, 55)
        _add(f"URLhaus — {uc} malware URL(s)", pts)

    # ── GreyNoise ─────────────────────────────────────────────────────────────
    gn_cls = (props.get("gn_classification") or "").lower()
    gn_noise = bool(props.get("gn_noise"))
    gn_riot  = bool(props.get("gn_riot"))
    if gn_riot:
        _add("GreyNoise RIOT (known benign)", -10)     # reduces score
    elif gn_cls == "malicious":
        _add("GreyNoise — classified malicious", 38)
    elif gn_noise and gn_cls == "unknown":
        _add("GreyNoise — active internet scanner", 15)

    # ── Shodan CVEs ───────────────────────────────────────────────────────────
    cves = props.get("cves") or []
    if isinstance(cves, (list, tuple)) and cves:
        pts = min(len(cves) * 6, 30)
        _add(f"Shodan — {len(cves)} CVE(s) on open services", pts)

    # ── VirusTotal detections ─────────────────────────────────────────────────
    vt_mal = int(props.get("vt_malicious", 0) or 0)
    if vt_mal > 0:
        pts = min(10 + vt_mal * 4, 45)
        _add(f"VirusTotal — {vt_mal} vendor detection(s)", pts)

    # ── AbuseIPDB confidence score ────────────────────────────────────────────
    adb_score = float(props.get("abuseipdb_score", 0) or 0)
    if adb_score > 0:
        pts = round(adb_score * 0.30)
        _add(f"AbuseIPDB — confidence {int(adb_score)}%", pts)

    # ── MalwareBazaar signature ───────────────────────────────────────────────
    mb_sig = (props.get("mb_signature") or props.get("signature") or "").strip()
    if mb_sig:
        _add(f"MalwareBazaar — {mb_sig}", 40)

    # ── Shodan scanner tags ───────────────────────────────────────────────────
    shodan_tags = props.get("shodan_tags") or []
    bad_tags    = {"tor", "vpn", "scanner", "malware", "honeypot", "botnet"}
    hit_tags    = [t for t in shodan_tags if t.lower() in bad_tags]
    if hit_tags:
        _add(f"Shodan tags: {', '.join(hit_tags)}", len(hit_tags) * 5)

    # Clamp 0–100
    final = max(0, min(100, score))

    level = (
        "critical" if final >= 75 else
        "high"     if final >= 50 else
        "medium"   if final >= 25 else
        "low"      if final > 0  else
        "none"
    )

    return {
        "score":   final,
        "level":   level,
        "signals": [s for s in signals if s["points"] != 0],
    }


@app.get("/entity/{entity_id}/risk-score")
async def get_entity_risk_score(
    entity_id: str,
    _user:     dict = Depends(get_current_user),
):
    """Compute a 0–100 risk score from all enrichment properties on the entity."""
    async with graph_db.driver.session() as session:
        r = await session.run(
            "MATCH (n {id: $id}) RETURN properties(n) AS props",
            id=entity_id,
        )
        rec = await r.single()
    if not rec:
        raise HTTPException(404, "Entity not found")
    result = _compute_risk_score(dict(rec["props"]))
    result["entity_id"] = entity_id
    return result


@app.post("/graph/risk-overlay")
async def graph_risk_overlay(
    payload: dict = Body(...),
    _user:   dict = Depends(get_current_user),
):
    """
    Given a list of entity IDs, return risk scores for all of them at once.
    Body: {"ids": ["id1", "id2", ...]}
    """
    ids = payload.get("ids") or []
    if not ids:
        return {"scores": {}}
    async with graph_db.driver.session() as session:
        r = await session.run(
            "UNWIND $ids AS eid MATCH (n {id: eid}) RETURN n.id AS id, properties(n) AS props",
            ids=ids[:500],
        )
        records = await r.fetch(500)
    scores = {}
    for rec in records:
        rs = _compute_risk_score(dict(rec["props"]))
        scores[rec["id"]] = rs
    return {"scores": scores}


# ─────────────────────────────────────────────────────────────────────────────
# Ph61 — Scheduled Watchlist Re-enrichment
# ─────────────────────────────────────────────────────────────────────────────
import json as _json61

_WATCHLIST_SCHED_KEY = "WATCHLIST_ENRICH_SCHEDULE"
_WATCHLIST_SCHED_DEFAULTS = {
    "enabled":          False,
    "interval_hours":   24,
    "alert_on_change":  True,
    "min_score_delta":  10,      # only alert if score changes by this much
}


def _load_watchlist_sched() -> dict:
    raw = os.getenv(_WATCHLIST_SCHED_KEY, "")
    if raw:
        try:
            return _json61.loads(raw)
        except Exception:
            pass
    return dict(_WATCHLIST_SCHED_DEFAULTS)


class WatchlistScheduleConfig(BaseModel):
    enabled:         bool  = False
    interval_hours:  int   = Field(24, ge=1, le=168)
    alert_on_change: bool  = True
    min_score_delta: int   = Field(10, ge=1, le=50)


@app.get("/settings/watchlist-schedule")
async def get_watchlist_schedule(_user: dict = Depends(get_current_user)):
    return _load_watchlist_sched()


@app.post("/settings/watchlist-schedule")
async def save_watchlist_schedule(
    cfg:   WatchlistScheduleConfig,
    _user: dict = Depends(get_current_user),
):
    data = cfg.dict()
    val  = _json61.dumps(data)
    _write_env_key(_WATCHLIST_SCHED_KEY, val)
    os.environ[_WATCHLIST_SCHED_KEY] = val
    _audit("WatchlistSchedule", "config saved", detail=f"enabled={cfg.enabled} interval={cfg.interval_hours}h")
    return {"ok": True, "config": data}


@app.post("/watchlist/enrich-now")
async def watchlist_enrich_now(
    background: BackgroundTasks,
    _user:      dict = Depends(get_current_user),
):
    """
    Trigger immediate re-enrichment of all watchlisted entities.
    Runs in background; returns immediately with the entity count.
    """
    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (w:Watchlist {id:'global'})-[:WATCHES]->(e)
            RETURN e.id AS eid, labels(e) AS lbs, properties(e) AS props
            """
        )
        records = await r.fetch(200)

    entities = [
        {
            "id":    rec["eid"],
            "label": (rec["lbs"] or ["Unknown"])[0],
            "props": dict(rec["props"]),
        }
        for rec in records
    ]

    cfg = _load_watchlist_sched()

    async def _run():
        for ent in entities:
            label = ent["label"]
            props = ent["props"]
            old_score = _compute_risk_score(props)["score"]
            value = (
                props.get("address") or props.get("domain") or
                props.get("value")   or props.get("email")  or ""
            )
            if not value:
                continue
            try:
                if label == "IP":
                    await enrich_ip_internetdb(graph_db, value)
                    await enrich_ip_greynoise(graph_db,  value)
                    await check_urlhaus(graph_db,        value)
                elif label == "Domain":
                    await check_urlhaus(graph_db, value)
                elif label == "Hash":
                    await check_malwarebazaar(value)
            except Exception as exc:
                log.warning("watchlist enrich %s %s: %s", label, value, exc)

            # Re-fetch updated props and check score delta
            if cfg.get("alert_on_change"):
                try:
                    async with graph_db.driver.session() as s2:
                        r2 = await s2.run(
                            "MATCH (n {id:$id}) RETURN properties(n) AS props",
                            id=ent["id"],
                        )
                        rec2 = await r2.single()
                        if rec2:
                            new_score = _compute_risk_score(dict(rec2["props"]))["score"]
                            delta = abs(new_score - old_score)
                            if delta >= cfg.get("min_score_delta", 10):
                                async with graph_db.driver.session() as s3:
                                    await s3.run(
                                        """
                                        CREATE (a:Alert {
                                            id:          $aid,
                                            type:        'risk_score_change',
                                            entity_id:   $eid,
                                            entity_name: $name,
                                            message:     $msg,
                                            severity:    $sev,
                                            created_at:  datetime(),
                                            acknowledged: false
                                        })
                                        """,
                                        aid=str(_uuid59.uuid4()),
                                        eid=ent["id"],
                                        name=value,
                                        msg=f"Risk score changed {old_score}→{new_score} (Δ{delta:+d})",
                                        sev="high" if new_score >= 75 else "medium",
                                    )
                except Exception as exc:
                    log.warning("watchlist score-delta check failed: %s", exc)

    background.add_task(_run)
    _audit("WatchlistEnrich", "manual trigger", detail=f"entities={len(entities)}")
    return {"ok": True, "entity_count": len(entities), "status": "running in background"}


# ─────────────────────────────────────────────────────────────────────────────
# Ph62 — Case Evidence Locker
# ─────────────────────────────────────────────────────────────────────────────
import uuid as _uuid62

class EvidenceItem(BaseModel):
    url:            str  = Field("",  max_length=2000)
    title:          str  = Field("",  max_length=300)
    notes:          str  = Field("",  max_length=2000)
    source:         str  = Field("manual", max_length=100)   # manual | urlscan | screenshot
    screenshot_url: str  = Field("",  max_length=2000)
    entity_id:      str  = Field("",  max_length=200)        # optional linked entity


@app.post("/case/{case_id}/evidence")
async def add_case_evidence(
    case_id: str,
    item:    EvidenceItem,
    _user:   dict = Depends(get_current_user),
):
    """Add an evidence item (URL, screenshot, note) to a case."""
    cid = case_id.strip()
    eid = str(_uuid62.uuid4())
    async with graph_db.driver.session() as session:
        # Verify case exists
        r = await session.run("MATCH (c:Case {id:$id}) RETURN c.id", id=cid)
        if not await r.single():
            raise HTTPException(404, "Case not found")
        await session.run(
            """
            MATCH (c:Case {id:$cid})
            CREATE (e:Evidence {
                id:             $eid,
                url:            $url,
                title:          $title,
                notes:          $notes,
                source:         $src,
                screenshot_url: $ss,
                entity_id:      $entid,
                added_by:       $user,
                created_at:     datetime()
            })
            CREATE (c)-[:HAS_EVIDENCE]->(e)
            """,
            cid=cid, eid=eid,
            url=item.url.strip(),
            title=item.title.strip(),
            notes=item.notes.strip(),
            src=item.source,
            ss=item.screenshot_url.strip(),
            entid=item.entity_id.strip(),
            user=_user.get("username", ""),
        )
    _audit("Evidence", cid, detail=f"title={item.title[:60]!r} src={item.source}")
    return {"id": eid, "ok": True}


@app.get("/case/{case_id}/evidence")
async def list_case_evidence(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """List all evidence items attached to a case."""
    cid = case_id.strip()
    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (c:Case {id:$cid})-[:HAS_EVIDENCE]->(e:Evidence)
            RETURN e ORDER BY e.created_at DESC
            """,
            cid=cid,
        )
        records = await r.fetch(200)
    items = [dict(rec["e"]) for rec in records]
    return {"evidence": items, "count": len(items)}


@app.delete("/case/{case_id}/evidence/{evidence_id}")
async def delete_case_evidence(
    case_id:     str,
    evidence_id: str,
    _user:       dict = Depends(get_current_user),
):
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MATCH (c:Case {id:$cid})-[:HAS_EVIDENCE]->(e:Evidence {id:$eid})
            DETACH DELETE e
            """,
            cid=case_id.strip(), eid=evidence_id.strip(),
        )
    return {"ok": True}


@app.post("/entity/{entity_id}/capture-evidence")
async def capture_entity_evidence(
    entity_id: str,
    payload:   dict = Body(...),
    _user:     dict = Depends(get_current_user),
):
    """
    Fetch a URLScan screenshot / scan result for a domain or IP entity
    and store it as evidence on the specified case.
    Body: {"case_id": "...", "domain": "..."}
    """
    cid    = (payload.get("case_id") or "").strip()
    domain = (payload.get("domain")  or "").strip()
    if not cid or not domain:
        raise HTTPException(400, "case_id and domain required")

    # Hit URLScan search to find the most recent scan with a screenshot
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            headers={"User-Agent": "Fieldwork OSINT", "Accept": "application/json"},
        ) as client:
            r = await client.get(
                f"https://urlscan.io/api/v1/search/",
                params={"q": f"domain:{domain}", "size": "5"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        raise HTTPException(502, f"URLScan lookup failed: {exc}")

    results   = data.get("results") or []
    scan_hit  = next((s for s in results if s.get("screenshot")), None)

    ss_url    = ""
    page_url  = ""
    scan_ref  = ""
    if scan_hit:
        ss_url   = scan_hit.get("screenshot", "")
        page_url = scan_hit.get("page", {}).get("url", f"https://{domain}")
        scan_ref = f"https://urlscan.io/result/{scan_hit.get('_id', '')}/"

    # Store as evidence
    eid = str(_uuid62.uuid4())
    async with graph_db.driver.session() as session:
        r2 = await session.run("MATCH (c:Case {id:$id}) RETURN c.id", id=cid)
        if not await r2.single():
            raise HTTPException(404, "Case not found")
        await session.run(
            """
            MATCH (c:Case {id:$cid})
            CREATE (e:Evidence {
                id:             $eid,
                url:            $url,
                title:          $title,
                notes:          $notes,
                source:         'urlscan',
                screenshot_url: $ss,
                entity_id:      $entid,
                added_by:       $user,
                created_at:     datetime()
            })
            CREATE (c)-[:HAS_EVIDENCE]->(e)
            """,
            cid=cid, eid=eid,
            url=page_url or f"https://{domain}",
            title=f"URLScan capture — {domain}",
            notes=f"Automated capture via URLScan.io{chr(10)}{scan_ref}".strip(),
            ss=ss_url,
            entid=entity_id,
            user=_user.get("username", ""),
        )

    _audit("Evidence", cid, detail=f"URLScan capture domain={domain}")
    return {
        "id":             eid,
        "screenshot_url": ss_url,
        "page_url":       page_url,
        "scan_ref":       scan_ref,
        "found":          bool(scan_hit),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph63 — Graph Cluster Suggestions
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/graph/cluster-suggestions")
async def graph_cluster_suggestions(
    payload: dict = Body(...),
    _user:   dict = Depends(get_current_user),
):
    """
    Given a list of node IDs currently on screen, return grouping suggestions.
    Groups by: node label type, shared ASN, shared registrar, shared country.
    Body: {"ids": [...]}
    Returns: {"groups": [{"key": str, "label": str, "ids": [...], "color": str}]}
    """
    ids = payload.get("ids") or []
    if not ids:
        return {"groups": []}

    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            UNWIND $ids AS eid
            MATCH (n {id: eid})
            RETURN n.id AS id, labels(n) AS lbs,
                   n.asn            AS asn,
                   n.country        AS country,
                   n.registrar      AS registrar,
                   n.gn_name        AS gn_name
            """,
            ids=ids[:500],
        )
        records = await r.fetch(500)

    # Group by node type
    type_groups: dict = {}
    for rec in records:
        nid   = rec["id"]
        lbs   = rec["lbs"] or ["Unknown"]
        label = lbs[0]
        type_groups.setdefault(label, []).append(nid)

    _TYPE_COLORS = {
        "Person":     "#7b5ea7", "Company":   "#1a6e9e", "Domain": "#0f6e56",
        "IP":         "#a32d2d", "Email":     "#ba7517", "Hash":   "#4a4a8a",
        "URL":        "#2a6e8a", "Location":  "#2a7d4f", "Phone":  "#6e4a2a",
        "Unknown":    "#3a3f4b",
    }

    groups = []
    for label, group_ids in type_groups.items():
        if len(group_ids) < 2:
            continue
        groups.append({
            "key":   f"type_{label}",
            "label": label,
            "ids":   group_ids,
            "color": _TYPE_COLORS.get(label, "#3a3f4b"),
        })

    # Group by ASN (if at least 2 nodes share one)
    asn_groups: dict = {}
    for rec in records:
        asn = (rec["asn"] or "").strip()
        if asn:
            asn_groups.setdefault(asn, []).append(rec["id"])
    for asn, group_ids in asn_groups.items():
        if len(group_ids) >= 2:
            groups.append({
                "key":   f"asn_{asn}",
                "label": f"ASN {asn}",
                "ids":   group_ids,
                "color": "#1a3a6e",
            })

    # Group by country
    country_groups: dict = {}
    for rec in records:
        c = (rec["country"] or "").strip()
        if c:
            country_groups.setdefault(c, []).append(rec["id"])
    for country, group_ids in country_groups.items():
        if len(group_ids) >= 2:
            groups.append({
                "key":   f"country_{country}",
                "label": f"📍 {country}",
                "ids":   group_ids,
                "color": "#2a4a2a",
            })

    return {"groups": groups}


# ─────────────────────────────────────────────────────────────────────────────
# Ph64 — Shodan Full API Integration
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/enrich/ip/{ip}/shodan-full")
async def shodan_full_lookup(
    ip:    str,
    _user: dict = Depends(get_current_user),
):
    """
    Full Shodan host lookup using SHODAN_API_KEY.
    Returns open services with banners, CVEs, org, ISP, ASN, geo, hostnames.
    Persists enriched data to the IP node.
    """
    api_key = os.getenv("SHODAN_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            402,
            "SHODAN_API_KEY not set — add it via the API Key Manager (🔑) or Admin panel"
        )

    ip_clean = ip.strip()
    # Validate IP
    try:
        ipaddress.ip_address(ip_clean)
    except ValueError:
        raise HTTPException(400, "Invalid IP address")

    try:
        async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "Fieldwork OSINT"}) as client:
            r = await client.get(
                f"https://api.shodan.io/shodan/host/{ip_clean}",
                params={"key": api_key},
            )
            if r.status_code == 404:
                return {"ip": ip_clean, "found": False, "message": "IP not in Shodan"}
            if r.status_code == 401:
                raise HTTPException(401, "Invalid SHODAN_API_KEY")
            if r.status_code == 429:
                raise HTTPException(429, "Shodan rate limit — check your plan")
            r.raise_for_status()
            data = r.json()
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Shodan full lookup failed for %s: %s", ip_clean, exc)
        raise HTTPException(502, f"Shodan error: {exc}")

    # Extract services / ports
    services = []
    for item in (data.get("data") or []):
        svc = {
            "port":      item.get("port"),
            "transport": item.get("transport", "tcp"),
            "product":   item.get("product",   ""),
            "version":   item.get("version",   ""),
            "module":    item.get("_shodan", {}).get("module", ""),
            "banner":    (item.get("data") or "")[:400].strip(),
            "cpe":       item.get("cpe",  []),
            "timestamp": (item.get("timestamp") or "")[:10],
        }
        # HTTP-specific extras
        http = item.get("http") or {}
        if http:
            svc["http_title"] = http.get("title",  "")
            svc["http_server"]= http.get("server", "")
        # SSL extras
        ssl = item.get("ssl") or {}
        if ssl:
            cert = ssl.get("cert") or {}
            sub  = cert.get("subject") or {}
            svc["ssl_cn"]      = sub.get("CN", "")
            svc["ssl_issuer"]  = (cert.get("issuer") or {}).get("O", "")
            svc["ssl_expires"] = (cert.get("expires") or "")[:10]
        services.append(svc)

    cves = list((data.get("vulns") or {}).keys())

    result = {
        "ip":          ip_clean,
        "found":       True,
        "org":         data.get("org",          ""),
        "isp":         data.get("isp",          ""),
        "asn":         data.get("asn",          ""),
        "country":     data.get("country_name", ""),
        "country_code":data.get("country_code", ""),
        "city":        data.get("city",         ""),
        "region":      data.get("region_code",  ""),
        "latitude":    data.get("latitude"),
        "longitude":   data.get("longitude"),
        "hostnames":   data.get("hostnames",    []),
        "domains":     data.get("domains",      []),
        "tags":        data.get("tags",         []),
        "ports":       sorted(data.get("ports", [])),
        "cves":        cves,
        "os":          data.get("os",           ""),
        "last_update": (data.get("last_update") or "")[:10],
        "services":    services[:40],
        "service_count": len(services),
    }

    # Persist enriched data to IP node
    node_id = f"ip_{hashlib.sha1(ip_clean.encode()).hexdigest()[:12]}"
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MERGE (n:IP {id: $id})
            ON CREATE SET n.address = $ip, n.source = 'shodan_full',
                          n.created_at = datetime()
            SET n.org          = $org,
                n.isp          = $isp,
                n.asn          = $asn,
                n.country      = $country,
                n.city         = $city,
                n.hostnames    = $hosts,
                n.domains      = $domains,
                n.open_ports   = $ports,
                n.cves         = $cves,
                n.shodan_tags  = $tags,
                n.os           = $os,
                n.updated_at   = datetime()
            """,
            id=node_id, ip=ip_clean,
            org=result["org"],   isp=result["isp"],
            asn=result["asn"],   country=result["country"],
            city=result["city"], hosts=result["hostnames"],
            domains=result["domains"], ports=result["ports"],
            cves=result["cves"], tags=result["tags"],
            os=result["os"],
        )

    _audit("ShodanFull", ip_clean, detail=f"ports={len(result['ports'])} cves={len(cves)} svcs={len(services)}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Ph65 — HTML Investigation Report Builder
# ─────────────────────────────────────────────────────────────────────────────
import html as _html_mod
import datetime as _dt65

_REPORT_CSS = """
<style>
  :root{--accent:#c1440e;--dark:#0d1117;--surface:#161b22;--border:#30363d;
        --text:#e6edf3;--dim:#7d8590;--success:#238636;--danger:#da3633;
        --warning:#9e6a03;--badge-bg:#21262d}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--dark);
       color:var(--text);line-height:1.6;padding:2rem}
  .report-header{border-bottom:3px solid var(--accent);padding-bottom:1.25rem;margin-bottom:2rem}
  .report-title{font-size:2rem;font-weight:800;letter-spacing:-0.02em}
  .report-meta{color:var(--dim);font-size:0.85rem;margin-top:0.4rem}
  .section{margin-bottom:2.5rem}
  .section-title{font-size:1.15rem;font-weight:700;border-bottom:1px solid var(--border);
                 padding-bottom:0.4rem;margin-bottom:1rem;color:var(--accent)}
  .entity-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;
               padding:0.85rem 1rem;margin-bottom:0.75rem}
  .entity-name{font-weight:700;font-size:1rem;margin-bottom:0.25rem}
  .entity-type{display:inline-block;background:var(--badge-bg);border-radius:4px;
               font-size:0.68rem;padding:1px 7px;color:var(--dim);margin-right:0.4rem}
  .risk-badge{display:inline-block;border-radius:4px;font-size:0.72rem;
              font-weight:700;padding:2px 9px}
  .risk-none    {background:rgba(48,54,61,0.7);color:var(--dim)}
  .risk-low     {background:rgba(35,134,54,0.25);color:#3fb950}
  .risk-medium  {background:rgba(158,106,3,0.25);color:#d29922}
  .risk-high    {background:rgba(218,54,51,0.25);color:#f85149}
  .risk-critical{background:rgba(218,0,0,0.35);color:#ff4444}
  .prop-table{width:100%;border-collapse:collapse;font-size:0.82rem;margin-top:0.5rem}
  .prop-table td{padding:3px 8px;border-bottom:1px solid var(--border)}
  .prop-table td:first-child{color:var(--dim);width:160px;white-space:nowrap}
  .tag{display:inline-block;background:var(--badge-bg);border-radius:4px;
       font-size:0.7rem;padding:1px 6px;margin:1px}
  .evidence-card{background:var(--surface);border:1px solid var(--border);
                 border-radius:8px;padding:0.75rem;margin-bottom:0.6rem}
  .evidence-ss{width:100%;max-height:220px;object-fit:cover;border-radius:5px;
               margin-bottom:0.5rem;border:1px solid var(--border)}
  .hyp-card{padding:0.6rem 0.85rem;margin-bottom:0.5rem;border-radius:6px;
             border-left:3px solid var(--border)}
  .hyp-open{border-color:#9e6a03;background:rgba(158,106,3,0.07)}
  .hyp-confirmed{border-color:#238636;background:rgba(35,134,54,0.07)}
  .hyp-rejected{border-color:#da3633;background:rgba(218,54,51,0.07)}
  .task-row{display:flex;align-items:center;gap:0.5rem;padding:0.35rem 0;
            font-size:0.85rem;border-bottom:1px solid var(--border)}
  .task-check{width:16px;height:16px;accent-color:var(--success)}
  .timeline-item{display:flex;gap:0.75rem;padding:0.5rem 0;border-bottom:1px solid var(--border)}
  .timeline-date{color:var(--dim);font-size:0.8rem;width:90px;flex-shrink:0;padding-top:2px}
  .footer{margin-top:3rem;border-top:1px solid var(--border);padding-top:1rem;
          color:var(--dim);font-size:0.75rem;text-align:center}
  a{color:#58a6ff}
  @media print{body{background:#fff;color:#111}
    .report-header{border-color:#c1440e}
    .entity-card,.evidence-card{border-color:#ccc;background:#f9f9f9}
    a{color:#0969da}}
</style>"""


class ReportRequest(BaseModel):
    sections: List[str] = Field(
        default=["summary","subjects","risk","evidence","hypotheses","tasks","timeline","notes"],
        description="Ordered list of sections to include"
    )
    title_override: str = Field("", max_length=200)


@app.post("/case/{case_id}/report/html")
async def build_html_report(
    case_id: str,
    req:     ReportRequest,
    _user:   dict = Depends(get_current_user),
):
    """
    Generate a standalone dark-themed HTML investigation report.
    Includes chosen sections; all CSS is inlined so the file is self-contained.
    """
    cid = case_id.strip()
    if not cid:
        raise HTTPException(400, "case_id required")

    ts_now = _dt65.datetime.now(_dt65.timezone.utc)
    ts_str = ts_now.strftime("%Y-%m-%d %H:%M UTC")

    def _e(s):
        return _html_mod.escape(str(s or ""), quote=True)

    async with graph_db.driver.session() as session:
        # Case metadata
        cr = await session.run("MATCH (c:Case {id:$id}) RETURN c", id=cid)
        crec = await cr.single()
        if not crec:
            raise HTTPException(404, "Case not found")
        case = dict(crec["c"])

        # Subjects / entities
        er = await session.run(
            "MATCH (c:Case {id:$id})-[:HAS_ENTITY]->(e) "
            "RETURN e, labels(e) AS lbs ORDER BY e.display_name",
            id=cid,
        )
        ent_records = await er.fetch(300)
        entities = [{"props": dict(r["e"]), "label": (r["lbs"] or ["Unknown"])[0]}
                    for r in ent_records]

        # Evidence
        ev_r = await session.run(
            "MATCH (c:Case {id:$id})-[:HAS_EVIDENCE]->(e:Evidence) "
            "RETURN e ORDER BY e.created_at",
            id=cid,
        )
        evidence = [dict(r["e"]) for r in await ev_r.fetch(100)]

        # Hypotheses
        hy_r = await session.run(
            "MATCH (c:Case {id:$id})-[:HAS_HYPOTHESIS]->(h:Hypothesis) "
            "RETURN h ORDER BY h.created_at",
            id=cid,
        )
        hypotheses = [dict(r["h"]) for r in await hy_r.fetch(50)]

        # Tasks
        ta_r = await session.run(
            "MATCH (c:Case {id:$id})-[:HAS_TASK]->(t:Task) "
            "RETURN t ORDER BY t.created_at",
            id=cid,
        )
        tasks = [dict(r["t"]) for r in await ta_r.fetch(100)]

        # Timeline events
        tl_r = await session.run(
            "MATCH (c:Case {id:$id})-[:HAS_EVENT]->(e:TimelineEvent) "
            "RETURN e ORDER BY e.date",
            id=cid,
        )
        timeline = [dict(r["e"]) for r in await tl_r.fetch(100)]

        # Notes
        no_r = await session.run(
            "MATCH (c:Case {id:$id})-[:HAS_NOTE]->(n:Note) "
            "RETURN n ORDER BY n.created_at DESC",
            id=cid,
        )
        notes_list = [dict(r["n"]) for r in await no_r.fetch(50)]

    title = req.title_override.strip() or case.get("title", cid)
    sections = req.sections or ["summary", "subjects", "risk", "evidence",
                                 "hypotheses", "tasks", "timeline", "notes"]

    # ── Build HTML ────────────────────────────────────────────────────────────
    parts: list[str] = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} — Fieldwork Investigation Report</title>
{_REPORT_CSS}
</head>
<body>
<div class="report-header">
  <div class="report-title">🔍 {_e(title)}</div>
  <div class="report-meta">
    Case ID: <code>{_e(cid)}</code> &nbsp;·&nbsp;
    Status: <strong>{_e(case.get('status',''))}</strong> &nbsp;·&nbsp;
    Priority: <strong>{_e(case.get('priority',''))}</strong> &nbsp;·&nbsp;
    Generated: {_e(ts_str)} by Fieldwork OSINT
  </div>
</div>
""")

    for sec in sections:

        if sec == "summary":
            desc = case.get("description", "")
            parts.append(f"""<div class="section">
<div class="section-title">📋 Executive Summary</div>
<p style="margin-bottom:0.75rem">{_e(desc) if desc else '<em style="color:var(--dim)">No description recorded.</em>'}</p>
<table class="prop-table">
  <tr><td>Subjects</td><td>{len(entities)}</td></tr>
  <tr><td>Evidence items</td><td>{len(evidence)}</td></tr>
  <tr><td>Hypotheses</td><td>{len(hypotheses)}</td></tr>
  <tr><td>Tasks</td><td>{len(tasks)} ({sum(1 for t in tasks if t.get('completed'))} done)</td></tr>
  <tr><td>Timeline events</td><td>{len(timeline)}</td></tr>
</table>
</div>""")

        elif sec == "subjects":
            rows = ""
            for ent in entities:
                p    = ent["props"]
                lbl  = ent["label"]
                name = p.get("display_name") or p.get("name") or p.get("domain") or \
                       p.get("address") or p.get("email") or p.get("value") or p.get("id","")
                props_html = ""
                skip = {"id","display_name","name","source","created_at","updated_at","label"}
                for k, v in p.items():
                    if k in skip or v is None or v == "" or v == []:
                        continue
                    props_html += f"<tr><td>{_e(k)}</td><td>{_e(v if not isinstance(v,list) else ', '.join(str(x) for x in v))}</td></tr>"
                rows += f"""<div class="entity-card">
  <div class="entity-name"><span class="entity-type">{_e(lbl)}</span>{_e(name)}</div>
  {f'<table class="prop-table">{props_html}</table>' if props_html else ''}
</div>"""
            _empty_subj = '<p style="color:var(--dim)">No entities linked.</p>'
            parts.append(f'<div class="section"><div class="section-title">🧩 Subjects ({len(entities)})</div>{rows or _empty_subj}</div>')

        elif sec == "risk":
            rows = ""
            for ent in entities:
                p    = ent["props"]
                rs   = _compute_risk_score(p)
                name = p.get("display_name") or p.get("name") or p.get("domain") or \
                       p.get("address") or p.get("email") or p.get("value") or p.get("id","")
                lbl  = ent["label"]
                sig  = "; ".join(s["label"] for s in rs["signals"]) or "—"
                badge = f'<span class="risk-badge risk-{rs["level"]}">{rs["score"]}/100 {rs["level"].upper()}</span>'
                rows += f"""<div style="display:flex;align-items:center;gap:0.75rem;padding:0.45rem 0;
                     border-bottom:1px solid var(--border);font-size:0.85rem">
  <span class="entity-type">{_e(lbl)}</span>
  <span style="flex:1;font-weight:600">{_e(name)}</span>
  {badge}
  <span style="color:var(--dim);font-size:0.75rem;max-width:350px">{_e(sig)}</span>
</div>"""
            scored = sorted(entities, key=lambda x: _compute_risk_score(x["props"])["score"], reverse=True)
            rows = ""
            for ent in scored:
                p    = ent["props"]
                rs   = _compute_risk_score(p)
                name = p.get("display_name") or p.get("name") or p.get("domain") or \
                       p.get("address") or p.get("email") or p.get("value") or p.get("id","")
                lbl  = ent["label"]
                sig  = "; ".join(s["label"] for s in rs["signals"]) or "No threat signals"
                badge = f'<span class="risk-badge risk-{rs["level"]}">{rs["score"]}/100 {rs["level"].upper()}</span>'
                rows += f"""<div style="display:flex;align-items:center;gap:0.75rem;padding:0.45rem 0;
                     border-bottom:1px solid var(--border);font-size:0.85rem;flex-wrap:wrap">
  <span class="entity-type">{_e(lbl)}</span>
  <span style="flex:1;font-weight:600;min-width:120px">{_e(name)}</span>
  {badge}
  <span style="color:var(--dim);font-size:0.75rem">{_e(sig)}</span>
</div>"""
            _empty_risk = '<p style="color:var(--dim)">No entities to score.</p>'
            parts.append(f'<div class="section"><div class="section-title">⚠ Risk Assessment</div>{rows or _empty_risk}</div>')

        elif sec == "evidence":
            ev_html = ""
            for ev in evidence:
                ss = ev.get("screenshot_url","")
                ss_tag = f'<img class="evidence-ss" src="{_e(ss)}" alt="screenshot">' if ss else ""
                url    = ev.get("url","")
                url_tag = f'<a href="{_e(url)}">{_e(url)}</a>' if url else ""
                ev_html += f"""<div class="evidence-card">
  {ss_tag}
  <strong>{_e(ev.get('title','(untitled)'))}</strong>
  {f'<div style="font-size:0.8rem;margin-top:0.25rem">{url_tag}</div>' if url else ""}
  {f'<div style="color:var(--dim);font-size:0.78rem;margin-top:0.25rem">{_e(ev.get("notes",""))}</div>' if ev.get("notes") else ""}
  <div style="color:var(--dim);font-size:0.7rem;margin-top:0.3rem">
    Source: {_e(ev.get('source','manual'))} &nbsp;·&nbsp; {_e(str(ev.get('created_at',''))[:10])}
  </div>
</div>"""
            _empty_ev = '<p style="color:var(--dim)">No evidence attached.</p>'
            parts.append(f'<div class="section"><div class="section-title">🗃 Evidence ({len(evidence)})</div>{ev_html or _empty_ev}</div>')

        elif sec == "hypotheses":
            hy_html = ""
            for h in hypotheses:
                status = h.get("status","open")
                cls    = {"confirmed":"hyp-confirmed","rejected":"hyp-rejected"}.get(status,"hyp-open")
                icons  = {"confirmed":"✅","rejected":"❌","open":"🔍","investigating":"🔬"}
                hy_html += f"""<div class="hyp-card {cls}">
  <strong>{icons.get(status,'🔍')} {_e(h.get('title',''))}</strong>
  <div style="font-size:0.78rem;color:var(--dim);margin-top:0.2rem">
    Status: {_e(status)} &nbsp;·&nbsp; Confidence: {_e(h.get('confidence',''))}
    {f'<br>{_e(h.get("description",""))}' if h.get("description") else ""}
  </div>
</div>"""
            _empty_hyp = '<p style="color:var(--dim)">None recorded.</p>'
            parts.append(f'<div class="section"><div class="section-title">🧩 Hypotheses ({len(hypotheses)})</div>{hy_html or _empty_hyp}</div>')

        elif sec == "tasks":
            ta_html = ""
            done    = sum(1 for t in tasks if t.get("completed"))
            for t in tasks:
                checked = "checked" if t.get("completed") else ""
                pri_color = {"high":"#da3633","medium":"#9e6a03","low":"#238636"}.get(t.get("priority",""),"var(--dim)")
                ta_html += f"""<div class="task-row">
  <input type="checkbox" class="task-check" {checked} disabled>
  <span style="flex:1{';text-decoration:line-through;color:var(--dim)' if t.get('completed') else ''}">{_e(t.get('text',''))}</span>
  <span style="font-size:0.7rem;color:{pri_color}">{_e(t.get('priority',''))}</span>
</div>"""
            _empty_tasks = '<p style="color:var(--dim)">No tasks.</p>'
            parts.append(f'<div class="section"><div class="section-title">✅ Tasks — {done}/{len(tasks)} complete</div>{ta_html or _empty_tasks}</div>')

        elif sec == "timeline":
            tl_html = ""
            for ev in timeline:
                tl_html += f"""<div class="timeline-item">
  <div class="timeline-date">{_e(str(ev.get('date',''))[:10])}</div>
  <div>
    <strong>{_e(ev.get('title',''))}</strong>
    {f'<div style="font-size:0.8rem;color:var(--dim)">{_e(ev.get("description",""))}</div>' if ev.get("description") else ""}
  </div>
</div>"""
            _empty_tl = '<p style="color:var(--dim)">No events recorded.</p>'
            parts.append(f'<div class="section"><div class="section-title">📅 Timeline ({len(timeline)} events)</div>{tl_html or _empty_tl}</div>')

        elif sec == "notes":
            no_html = ""
            for n in notes_list:
                no_html += f"""<div style="background:var(--surface);border:1px solid var(--border);
  border-radius:6px;padding:0.65rem;margin-bottom:0.5rem">
  <div style="white-space:pre-wrap;font-size:0.85rem">{_e(n.get('content',''))}</div>
  <div style="color:var(--dim);font-size:0.7rem;margin-top:0.3rem">
    {_e(str(n.get('created_at',''))[:10])} — {_e(n.get('author',''))}
  </div>
</div>"""
            _empty_notes = '<p style="color:var(--dim)">No notes.</p>'
            parts.append(f'<div class="section"><div class="section-title">📝 Notes ({len(notes_list)})</div>{no_html or _empty_notes}</div>')

    parts.append(f"""<div class="footer">
  Generated by <strong>Fieldwork OSINT</strong> &nbsp;·&nbsp; {_e(ts_str)}<br>
  This document is sensitive — handle according to your organisation's data classification policy.
</div>
</body></html>""")

    html_out = "\n".join(parts)
    _audit("ReportHTML", cid, detail=f"sections={sections} entities={len(entities)}")

    return Response(
        content=html_out.encode("utf-8"),
        media_type="text/html",
        headers={
            "Content-Disposition":
                f'attachment; filename="fieldwork-report-{cid[:8]}.html"'
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ph66 — Cross-Case Entity Correlation
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/correlation/shared-entities")
async def correlation_shared_entities(
    min_cases: int = 2,
    limit:     int = 50,
    _user:     dict = Depends(get_current_user),
):
    """
    Find entities that appear in two or more cases.
    Returns each entity with the list of cases it belongs to.
    """
    n = max(2, min(min_cases, 10))
    l = min(max(1, limit), 200)
    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (c:Case)-[:HAS_ENTITY]->(e)
            WITH e, collect(DISTINCT {id: c.id, title: c.title, status: c.status}) AS cases
            WHERE size(cases) >= $n
            RETURN e, labels(e) AS lbs, cases
            ORDER BY size(cases) DESC
            LIMIT $l
            """,
            n=n, l=l,
        )
        records = await r.fetch(200)

    results = []
    for rec in records:
        props = dict(rec["e"])
        lbs   = rec["lbs"] or ["Unknown"]
        label = lbs[0]
        name  = (props.get("display_name") or props.get("name") or
                 props.get("domain")       or props.get("address") or
                 props.get("email")        or props.get("value")   or
                 props.get("id", ""))
        rs    = _compute_risk_score(props)
        results.append({
            "id":         props.get("id", ""),
            "label":      label,
            "name":       name,
            "case_count": len(rec["cases"]),
            "cases":      list(rec["cases"]),
            "risk_score": rs["score"],
            "risk_level": rs["level"],
        })
    return {"entities": results, "total": len(results)}


@app.get("/correlation/infrastructure-clusters")
async def correlation_infrastructure_clusters(
    _user: dict = Depends(get_current_user),
):
    """
    Find groups of entities sharing the same infrastructure attribute:
    ASN, registrar, country, or Shodan tag.
    Useful for spotting shared hosting / bullet-proof hosters.
    """
    async with graph_db.driver.session() as session:
        # Shared ASN
        asn_r = await session.run(
            """
            MATCH (n) WHERE n.asn IS NOT NULL AND n.asn <> ''
            WITH n.asn AS asn, collect({id:n.id, label:labels(n)[0],
                 name: coalesce(n.address, n.domain, n.display_name, n.id)}) AS nodes
            WHERE size(nodes) >= 2
            RETURN 'asn' AS cluster_type, asn AS key, nodes
            ORDER BY size(nodes) DESC LIMIT 20
            """
        )
        asn_records = await asn_r.fetch(20)

        # Shared registrar
        reg_r = await session.run(
            """
            MATCH (n:Domain) WHERE n.registrar IS NOT NULL AND n.registrar <> ''
            WITH n.registrar AS reg, collect({id:n.id, label:'Domain',
                 name: coalesce(n.domain, n.id)}) AS nodes
            WHERE size(nodes) >= 2
            RETURN 'registrar' AS cluster_type, reg AS key, nodes
            ORDER BY size(nodes) DESC LIMIT 15
            """
        )
        reg_records = await reg_r.fetch(15)

        # Shared Shodan tags (scanner, tor, vpn, etc.)
        tag_r = await session.run(
            """
            MATCH (n:IP) WHERE n.shodan_tags IS NOT NULL
            UNWIND n.shodan_tags AS tag
            WITH tag, collect({id:n.id, label:'IP',
                 name: coalesce(n.address, n.id)}) AS nodes
            WHERE size(nodes) >= 2 AND tag IN ['tor','vpn','scanner','malware','botnet','honeypot']
            RETURN 'tag' AS cluster_type, tag AS key, nodes
            ORDER BY size(nodes) DESC LIMIT 10
            """
        )
        tag_records = await tag_r.fetch(10)

    def _fmt(records, type_label):
        return [
            {
                "cluster_type": r["cluster_type"],
                "key":          r["key"],
                "type_label":   type_label,
                "node_count":   len(r["nodes"]),
                "nodes":        list(r["nodes"])[:30],
            }
            for r in records
        ]

    clusters = (
        _fmt(asn_records,  "Shared ASN")      +
        _fmt(reg_records,  "Shared registrar") +
        _fmt(tag_records,  "Shared tag")
    )
    clusters.sort(key=lambda x: x["node_count"], reverse=True)
    return {"clusters": clusters, "total": len(clusters)}


@app.post("/correlation/find-paths")
async def correlation_find_paths(
    payload: dict = Body(...),
    _user:   dict = Depends(get_current_user),
):
    """
    Find all shortest paths between two entities in the graph (up to 4 hops).
    Body: {"from_id": "...", "to_id": "..."}
    """
    from_id = (payload.get("from_id") or "").strip()
    to_id   = (payload.get("to_id")   or "").strip()
    if not from_id or not to_id:
        raise HTTPException(400, "from_id and to_id required")

    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (a {id:$fid}), (b {id:$tid})
            MATCH path = allShortestPaths((a)-[*1..4]-(b))
            RETURN [n IN nodes(path) | {id: n.id,
                    label: labels(n)[0],
                    name: coalesce(n.display_name, n.name, n.domain,
                                   n.address, n.email, n.value, n.id)}] AS node_chain,
                   [r IN relationships(path) | type(r)] AS rel_chain
            LIMIT 10
            """,
            fid=from_id, tid=to_id,
        )
        records = await r.fetch(10)

    paths = [
        {"nodes": rec["node_chain"], "rels": rec["rel_chain"]}
        for rec in records
    ]
    return {"paths": paths, "count": len(paths), "from_id": from_id, "to_id": to_id}


# ─────────────────────────────────────────────────────────────────────────────
# Ph67 — One-Click Domain Full Profile
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/enrich/domain/{domain}/full-profile")
async def domain_full_profile(
    domain: str,
    _user:  dict = Depends(get_current_user),
):
    """
    Run all free domain enrichments in parallel and return a combined profile:
    RDAP · Passive DNS · Cert Transparency · URLScan · URLhaus · Risk score.
    """
    d = _validate_domain(domain)

    # Import crawlers needed
    from crawlers.rdap       import enrich_domain          as _rdap
    from crawlers.passive_dns import passive_dns_domain    as _pdns
    from crawlers.crtsh      import domain_cert_transparency as _crtsh
    from crawlers.urlscan    import search_domain          as _urlscan_search

    async def _safe(coro, label):
        try:
            return label, await coro
        except Exception as exc:
            log.warning("full-profile %s %s: %s", label, d, exc)
            return label, {"error": str(exc)}

    results = await asyncio.gather(
        _safe(_rdap(graph_db, d),               "rdap"),
        _safe(_pdns(graph_db, d),               "passive_dns"),
        _safe(_crtsh(graph_db, d),              "certs"),
        _safe(_urlscan_search(graph_db, d, 10), "urlscan"),
        _safe(check_urlhaus(graph_db, d),       "urlhaus"),
    )
    combined = {label: data for label, data in results}

    # Compute risk score from what's now stored in the graph
    async with graph_db.driver.session() as session:
        r = await session.run(
            "MATCH (n:Domain {domain:$d}) RETURN properties(n) AS props", d=d
        )
        rec = await r.single()
        props = dict(rec["props"]) if rec else {}

    combined["risk"] = _compute_risk_score(props)
    combined["domain"] = d

    # Summary highlights
    rdap_data = combined.get("rdap") or {}
    combined["summary"] = {
        "registrar":    rdap_data.get("registrar", ""),
        "created":      rdap_data.get("created",   ""),
        "expires":      rdap_data.get("expires",   ""),
        "registrant":   rdap_data.get("registrant_name", ""),
        "nameservers":  rdap_data.get("nameservers", []),
        "cert_count":   len((combined.get("certs") or {}).get("certificates", [])),
        "scan_count":   (combined.get("urlscan") or {}).get("scan_count", 0),
        "urlhaus_hit":  (combined.get("urlhaus") or {}).get("found", False),
        "pdns_ips":     [
            e.get("ip","") for e in
            ((combined.get("passive_dns") or {}).get("results") or [])[:5]
        ],
    }

    _audit("DomainFullProfile", d, detail=f"risk={combined['risk']['score']}")
    return combined


# ─────────────────────────────────────────────────────────────────────────────
# Ph68 — Smart Paste / Text-to-Graph
# ─────────────────────────────────────────────────────────────────────────────

class SmartPasteRequest(BaseModel):
    text:    str      = Field(..., max_length=50_000)
    case_id: str      = Field("", max_length=200)
    run_ner: bool     = Field(True)
    tags:    List[str]= Field(default_factory=list)


@app.post("/smart-paste")
async def smart_paste(
    req:        SmartPasteRequest,
    background: BackgroundTasks,
    _user:      dict = Depends(get_current_user),
):
    """
    Extract all entities from free-form text:
    1. IOC regex classification (IPs, domains, hashes, URLs, emails)
    2. NER pipeline (persons, organisations, locations) if run_ner=True

    Returns a deduplicated entity list classified by type.
    Optionally creates graph nodes + links them to a case (if case_id given).
    """
    import re as _re68

    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text is required")

    found: dict[str, dict] = {}   # dedup key → {value, label, subtype, source}

    # ── IOC regex pass ────────────────────────────────────────────────────────
    # Extract URLs first (greedy), then remaining tokens
    url_pat  = _re68.compile(r'https?://[^\s<>"\']+', _re68.I)
    for m in url_pat.finditer(text):
        v = m.group().rstrip('.,;)')
        found[v.lower()] = {"value": v, "label": "URL", "subtype": "url", "source": "regex"}

    # Tokenise remaining (replace URLs with placeholder to avoid re-matching)
    clean = url_pat.sub(' ', text)
    for tok in _re68.split(r'[\s,;|<>\[\]()"\']', clean):
        tok = tok.strip().rstrip('.,;:!?')
        if len(tok) < 4:
            continue
        label, subtype = _classify_ioc(tok)
        if label not in ("Unknown",):
            key = tok.lower()
            if key not in found:
                found[key] = {"value": tok, "label": label, "subtype": subtype, "source": "regex"}

    # ── NER pass ─────────────────────────────────────────────────────────────
    if req.run_ner:
        try:
            ner_result = await asyncio.get_event_loop().run_in_executor(
                None, process_text, text
            )
            for ent in (ner_result.get("entities") or []):
                v   = ent.get("text", "").strip()
                typ = ent.get("type", "").upper()
                if not v or len(v) < 3:
                    continue
                mapping = {
                    "PERSON":  ("Person",   "person"),
                    "ORG":     ("Company",  "org"),
                    "GPE":     ("Location", "gpe"),
                    "LOC":     ("Location", "location"),
                    "EMAIL":   ("Email",    "email"),
                    "URL":     ("URL",      "url"),
                }
                if typ in mapping:
                    lbl, sub = mapping[typ]
                    key = v.lower()
                    if key not in found:
                        found[key] = {"value": v, "label": lbl, "subtype": sub, "source": "ner"}
        except Exception as exc:
            log.warning("smart-paste NER failed: %s", exc)

    entities = list(found.values())

    # ── Optional graph ingestion ──────────────────────────────────────────────
    created = 0
    if req.case_id.strip() and entities:
        bulk_req = BulkIOCRequest(
            text="\n".join(e["value"] for e in entities
                           if e["label"] in ("IP","Domain","URL","Hash","Email")),
            case_id=req.case_id,
            auto_enrich=False,
            tags=req.tags,
        )
        # Re-use bulk import for IOC types
        async with graph_db.driver.session() as session:
            for ent in entities:
                lbl = ent["label"]
                val = ent["value"]
                if lbl in ("Person", "Company", "Location"):
                    nid = f"{lbl.lower()}_{hashlib.sha1(val.encode()).hexdigest()[:12]}"
                    prop_key = {"Person":"name","Company":"name","Location":"name"}[lbl]
                    try:
                        await session.run(
                            f"""
                            MERGE (n:{lbl} {{id: $id}})
                            ON CREATE SET n.{prop_key} = $val, n.display_name = $val,
                                          n.source = 'smart_paste', n.created_at = datetime()
                            SET n.updated_at = datetime()
                            """,
                            id=nid, val=val,
                        )
                        if req.case_id:
                            await session.run(
                                """
                                MATCH (c:Case {id:$cid}) MATCH (e {id:$eid})
                                MERGE (c)-[r:HAS_ENTITY]->(e)
                                ON CREATE SET r.role='extracted', r.added_at=datetime()
                                """,
                                cid=req.case_id, eid=nid,
                            )
                        created += 1
                    except Exception:
                        pass

    # Count by label
    counts: dict[str, int] = {}
    for e in entities:
        counts[e["label"]] = counts.get(e["label"], 0) + 1

    _audit("SmartPaste", f"{len(entities)} entities", detail=f"ner={req.run_ner} case={req.case_id[:20]}")
    return {
        "total":    len(entities),
        "counts":   counts,
        "entities": entities,
        "created":  created,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph69 — Passive DNS Historical Timeline
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/enrich/domain/{domain}/pdns-timeline")
async def pdns_timeline(
    domain: str,
    _user:  dict = Depends(get_current_user),
):
    """
    Return stored pDNS resolution events for a domain, plus cert issuance
    timestamps and subdomain discoveries, ordered by time.
    The frontend renders these as an SVG swimlane timeline.
    """
    d = _validate_domain(domain)

    async with graph_db.driver.session() as session:

        # DNS resolutions stored on RESOLVES_TO edges
        pdns_r = await session.run(
            """
            MATCH (dom:Domain {domain:$d})-[r:RESOLVES_TO]->(ip:IP)
            RETURN ip.address    AS ip,
                   r.first_seen  AS first_seen,
                   r.last_seen   AS last_seen,
                   coalesce(r.source, 'passive_dns') AS source
            ORDER BY coalesce(r.first_seen, '') ASC
            """,
            d=d,
        )
        pdns_records = await pdns_r.fetch(200)

        # Certificate issuance events
        cert_r = await session.run(
            """
            MATCH (dom:Domain {domain:$d})-[:HAS_CERT]->(c:Certificate)
            RETURN c.id AS cert_id,
                   coalesce(c.issuer, c.issuer_cn, '') AS issuer,
                   c.not_before AS not_before,
                   c.not_after  AS not_after
            ORDER BY coalesce(c.not_before, '') ASC
            """,
            d=d,
        )
        cert_records = await cert_r.fetch(100)

        # Subdomains found in SAN / passive DNS
        sub_r = await session.run(
            """
            MATCH (dom:Domain {domain:$d})
            OPTIONAL MATCH (dom)-[:HAS_CERT]->(c:Certificate)-[:COVERS]->(sub:Domain)
                WHERE sub.domain <> $d
            OPTIONAL MATCH (dom)-[:HAS_SUBDOMAIN]->(sub2:Domain)
                WHERE sub2.domain <> $d
            WITH coalesce(sub.domain, sub2.domain) AS subdomain,
                 coalesce(c.not_before, '') AS first_seen
            WHERE subdomain IS NOT NULL
            RETURN DISTINCT subdomain, first_seen
            ORDER BY first_seen ASC
            LIMIT 80
            """,
            d=d,
        )
        sub_records = await sub_r.fetch(80)

    events: list[dict] = []

    for rec in pdns_records:
        events.append({
            "type":       "dns_resolution",
            "target":     rec["ip"] or "",
            "first_seen": rec["first_seen"] or "",
            "last_seen":  rec["last_seen"]  or "",
            "source":     rec["source"]     or "passive_dns",
        })

    for rec in cert_records:
        events.append({
            "type":       "certificate",
            "target":     rec["issuer"]     or "",
            "cert_id":    rec["cert_id"]    or "",
            "first_seen": rec["not_before"] or "",
            "last_seen":  rec["not_after"]  or "",
        })

    for rec in sub_records:
        events.append({
            "type":       "subdomain",
            "target":     rec["subdomain"]  or "",
            "first_seen": rec["first_seen"] or "",
            "last_seen":  "",
            "source":     "crt.sh",
        })

    # Deduplicate IPs for swimlane assignment
    ips = list(dict.fromkeys(
        e["target"] for e in events
        if e["type"] == "dns_resolution" and e["target"]
    ))

    # Determine time bounds
    all_times = [
        e["first_seen"] for e in events if e.get("first_seen")
    ] + [
        e["last_seen"]  for e in events if e.get("last_seen")
    ]
    t_min = min(all_times) if all_times else ""
    t_max = max(all_times) if all_times else ""

    _audit("PDNSTimeline", d, detail=f"events={len(events)}")
    return {
        "domain": d,
        "events": events,
        "ips":    ips,
        "t_min":  t_min,
        "t_max":  t_max,
        "total":  len(events),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph70 — Entity Merge & Deduplication Wizard
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/entity/duplicates")
async def find_duplicate_entities(
    label:  str   = Query(None),
    limit:  int   = Query(80, ge=1, le=200),
    _user:  dict  = Depends(get_current_user),
):
    """
    Find entity nodes with identical canonical values (case-insensitive) that
    are stored as separate Neo4j nodes.  Returns groups of pairs ready to merge.
    """
    label_filter = f":{label}" if label and label.isalpha() else ""

    async with graph_db.driver.session() as session:
        r = await session.run(
            f"""
            MATCH (a{label_filter}), (b{label_filter})
            WHERE id(a) < id(b)
              AND a.id <> b.id
              AND (
                (a.address   IS NOT NULL AND toLower(a.address)      = toLower(b.address))
             OR (a.domain    IS NOT NULL AND toLower(a.domain)       = toLower(b.domain))
             OR (a.hash_value IS NOT NULL AND toLower(a.hash_value)  = toLower(b.hash_value))
             OR (a.url       IS NOT NULL AND toLower(a.url)          = toLower(b.url))
             OR (a.email     IS NOT NULL AND toLower(a.email)        = toLower(b.email))
              )
            WITH a, b, labels(a)[0] AS lbl
            RETURN a.id AS id_a, b.id AS id_b, lbl,
                   properties(a) AS props_a, properties(b) AS props_b,
                   coalesce(a.address, a.domain, a.hash_value,
                            a.url, a.email, a.display_name, a.id) AS canonical
            LIMIT $lim
            """,
            lim=limit,
        )
        records = await r.fetch(limit)

    groups = []
    for rec in records:
        pa = dict(rec["props_a"])
        pb = dict(rec["props_b"])

        all_keys = sorted(set(pa) | set(pb))
        diffs:  list[dict] = []
        merged: dict       = {}

        for k in all_keys:
            va = pa.get(k)
            vb = pb.get(k)
            if va is None:
                merged[k] = vb
            elif vb is None:
                merged[k] = va
            elif isinstance(va, str) and isinstance(vb, str):
                merged[k] = va if len(va) >= len(vb) else vb
                if va.lower() != vb.lower():
                    diffs.append({"key": k, "a": va[:120], "b": vb[:120]})
            else:
                merged[k] = va
                if va != vb:
                    diffs.append({"key": k, "a": str(va)[:80], "b": str(vb)[:80]})

        groups.append({
            "label":       rec["lbl"],
            "canonical":   rec["canonical"],
            "id_a":        rec["id_a"],
            "id_b":        rec["id_b"],
            "diffs":       diffs[:12],
            "diff_count":  len(diffs),
            "merged_props": {
                k: v for k, v in merged.items()
                if k not in ("id",) and v is not None
            },
        })

    return {"groups": groups, "total": len(groups)}


class EntityMergeRequest(BaseModel):
    keep_id:      str  = Field(..., min_length=1)
    remove_id:    str  = Field(..., min_length=1)
    merged_props: dict = Field(default_factory=dict)


@app.post("/entity/merge")
async def merge_entities(
    req:   EntityMergeRequest,
    _user: dict = Depends(get_current_user),
):
    """
    Merge two entity nodes.
    - Re-links key relationship types from remove→keep
    - Applies merged_props onto the kept node
    - Deletes the removed node
    Does NOT require APOC — uses explicit relationship-type queries for the
    relationship types defined in the Fieldwork schema.
    """
    if req.keep_id == req.remove_id:
        raise HTTPException(400, "keep_id and remove_id must be different")

    # Relationship types to re-link (both directions)
    REL_TYPES = [
        "HAS_ENTITY", "LINKED_TO", "RESOLVES_TO", "COMMUNICATES_WITH",
        "HOSTED_ON",  "BELONGS_TO", "REGISTERED_BY", "MENTIONS",
        "HAS_EVIDENCE", "HAS_CERT", "HAS_SUBDOMAIN", "PART_OF",
    ]

    async with graph_db.driver.session() as session:
        # Verify both nodes exist
        chk = await session.run(
            "MATCH (a {id:$ka}), (b {id:$rb}) RETURN a.id AS aid",
            ka=req.keep_id, rb=req.remove_id,
        )
        if not await chk.single():
            raise HTTPException(404, "One or both nodes not found")

        for rt in REL_TYPES:
            # Outgoing edges from the node to remove
            await session.run(
                f"""
                MATCH (rem {{id:$rid}})-[r:{rt}]->(other)
                WHERE other.id <> $kid
                MATCH (keep {{id:$kid}})
                MERGE (keep)-[:{rt}]->(other)
                DELETE r
                """,
                rid=req.remove_id, kid=req.keep_id,
            )
            # Incoming edges to the node to remove
            await session.run(
                f"""
                MATCH (other)-[r:{rt}]->(rem {{id:$rid}})
                WHERE other.id <> $kid
                MATCH (keep {{id:$kid}})
                MERGE (other)-[:{rt}]->(keep)
                DELETE r
                """,
                rid=req.remove_id, kid=req.keep_id,
            )

        # Apply merged properties
        if req.merged_props:
            safe = {
                k: v for k, v in req.merged_props.items()
                if isinstance(k, str) and k not in ("id",)
            }
            if safe:
                await session.run(
                    "MATCH (n {id:$kid}) SET n += $props",
                    kid=req.keep_id, props=safe,
                )

        # Delete the now-detached removed node
        await session.run(
            "MATCH (n {id:$rid}) DETACH DELETE n",
            rid=req.remove_id,
        )

    _audit("MergeEntities", req.keep_id, detail=f"removed={req.remove_id}")
    return {"status": "merged", "kept": req.keep_id, "removed": req.remove_id}


# ─────────────────────────────────────────────────────────────────────────────
# Ph71 — Network Infrastructure Pivot
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/enrich/ip/{ip}/network-pivot")
async def network_pivot(
    ip:    str,
    _user: dict = Depends(get_current_user),
):
    """
    Given an IP, return:
    - ASN siblings already in the graph (same ASN, different IP)
    - /24 subnet peers already in the graph
    - Co-hosted domains resolving into the same /24
    - Shodan /24 census if SHODAN_API_KEY is set
    """
    ip_clean = _validate_ip(ip)
    parts    = ip_clean.split(".")
    if len(parts) != 4:
        raise HTTPException(400, "Invalid IPv4 address")
    prefix24 = ".".join(parts[:3]) + "."

    async with graph_db.driver.session() as session:

        # Own node's ASN / org / country
        meta_r = await session.run(
            """
            MATCH (n:IP {address:$ip})
            RETURN n.asn AS asn, n.country AS country,
                   coalesce(n.org, n.isp, '') AS org
            """,
            ip=ip_clean,
        )
        meta = await meta_r.single()
        asn     = meta["asn"]     if meta else None
        country = meta["country"] if meta else None
        org     = meta["org"]     if meta else None

        # ASN siblings in graph
        asn_siblings: list[dict] = []
        if asn:
            sib_r = await session.run(
                """
                MATCH (n:IP)
                WHERE n.asn = $asn AND n.address <> $ip
                RETURN n.address AS address, n.id AS id,
                       coalesce(n.gn_classification,'') AS gn_class,
                       coalesce(n.urlhaus_listed, false) AS urlhaus,
                       coalesce(n.country,'') AS country
                LIMIT 30
                """,
                asn=asn, ip=ip_clean,
            )
            asn_siblings = [dict(r) for r in await sib_r.fetch(30)]

        # /24 peers in graph
        subnet_r = await session.run(
            """
            MATCH (n:IP)
            WHERE n.address STARTS WITH $prefix AND n.address <> $ip
            RETURN n.address AS address, n.id AS id,
                   coalesce(n.gn_classification,'') AS gn_class,
                   coalesce(n.urlhaus_listed, false) AS urlhaus
            LIMIT 30
            """,
            prefix=prefix24, ip=ip_clean,
        )
        subnet_peers = [dict(r) for r in await subnet_r.fetch(30)]

        # Co-hosted domains resolving into this /24
        cohost_r = await session.run(
            """
            MATCH (ip:IP)<-[:RESOLVES_TO]-(dom:Domain)
            WHERE ip.address STARTS WITH $prefix
            RETURN DISTINCT dom.domain AS domain,
                   dom.id AS id,
                   ip.address AS resolved_ip
            LIMIT 40
            """,
            prefix=prefix24,
        )
        cohosted = [dict(r) for r in await cohost_r.fetch(40)]

    # Shodan /24 census (optional — requires key)
    shodan_key        = os.getenv("SHODAN_API_KEY", "")
    shodan_total: int | None = None
    shodan_hosts: list[dict] = []

    if shodan_key:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.shodan.io/shodan/host/search",
                    params={
                        "key":    shodan_key,
                        "query":  f"net:{'.'.join(parts[:3])}.0/24",
                        "minify": "true",
                        "page":   "1",
                    },
                )
                if resp.status_code == 200:
                    sdata        = resp.json()
                    shodan_total = sdata.get("total", 0)
                    for m in (sdata.get("matches") or [])[:20]:
                        shodan_hosts.append({
                            "ip":        m.get("ip_str", ""),
                            "port":      m.get("port"),
                            "org":       m.get("org", ""),
                            "hostnames": m.get("hostnames", [])[:4],
                        })
        except Exception as exc:
            log.warning("network-pivot shodan /24: %s", exc)

    _audit("NetworkPivot", ip_clean, detail=f"asn={asn} /24_peers={len(subnet_peers)}")
    return {
        "ip":                 ip_clean,
        "subnet24":           f"{'.'.join(parts[:3])}.0/24",
        "asn":                asn,
        "country":            country,
        "org":                org,
        "asn_siblings":       asn_siblings,
        "subnet_peers":       subnet_peers,
        "cohosted_domains":   cohosted,
        "shodan_subnet_total": shodan_total,
        "shodan_subnet_hosts": shodan_hosts,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph72 — IOC Feed Subscription
# ─────────────────────────────────────────────────────────────────────────────

# Built-in feed registry (always available, no config needed for free ones)
_FEED_REGISTRY: dict[str, dict] = {
    "feodo": {
        "name":        "Feodo Tracker",
        "description": "abuse.ch C2 IP blocklist — Emotet, QakBot, Dridex etc.",
        "url":         "https://feodotracker.abuse.ch/downloads/ipblocklist.json",
        "free":        True,
        "ioc_type":    "IP",
        "key_env":     None,
    },
    "urlhaus_recent": {
        "name":        "URLhaus Recent",
        "description": "abuse.ch recent malware-hosting URLs",
        "url":         "https://urlhaus-api.abuse.ch/v1/urls/recent/limit/200/",
        "free":        True,
        "ioc_type":    "URL",
        "key_env":     None,
    },
    "threatfox_recent": {
        "name":        "ThreatFox 24h",
        "description": "abuse.ch ThreatFox IOCs from the last 24 hours",
        "url":         "https://threatfox-api.abuse.ch/api/v1/",
        "free":        True,
        "ioc_type":    "mixed",
        "key_env":     None,
    },
    "mb_recent": {
        "name":        "MalwareBazaar Recent",
        "description": "abuse.ch recent malware sample hashes",
        "url":         "https://mb-api.abuse.ch/api/v1/",
        "free":        True,
        "ioc_type":    "Hash",
        "key_env":     None,
    },
    "otx": {
        "name":        "OTX AlienVault",
        "description": "AlienVault OTX subscribed pulse IOCs",
        "url":         "https://otx.alienvault.com/api/v1/pulses/subscribed",
        "free":        False,
        "ioc_type":    "mixed",
        "key_env":     "OTX_API_KEY",
    },
}

# In-memory sync status (survives the process lifetime)
_feed_status: dict[str, dict] = {}


def _feed_enabled(name: str) -> bool:
    """Check whether a feed is enabled via env FEED_{NAME}_ENABLED=true."""
    return os.getenv(f"FEED_{name.upper()}_ENABLED", "false").lower() == "true"


async def _sync_feodo(db) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(_FEED_REGISTRY["feodo"]["url"])
        r.raise_for_status()
        items = r.json()
    created = 0
    async with db.driver.session() as session:
        for item in items[:500]:
            ip = (item.get("ip_address") or "").strip()
            if not ip:
                continue
            nid = f"ip:{ip}"
            await session.run(
                """
                MERGE (n:IP {id:$id})
                ON CREATE SET n.address=$ip, n.created_at=datetime(),
                              n.feed_source='feodo', n.feed_malware=true
                ON MATCH  SET n.feed_source='feodo', n.feed_malware=true,
                              n.feed_last_seen=datetime()
                """,
                id=nid, ip=ip,
            )
            created += 1
    return {"imported": created, "total": len(items)}


async def _sync_urlhaus_recent(db) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(_FEED_REGISTRY["urlhaus_recent"]["url"])
        r.raise_for_status()
        data = r.json()
    urls = data.get("urls", [])
    created = 0
    async with db.driver.session() as session:
        for item in urls[:300]:
            url_val = (item.get("url") or "").strip()
            if not url_val:
                continue
            nid = f"url:{url_val[:200]}"
            await session.run(
                """
                MERGE (n:URL {id:$id})
                ON CREATE SET n.url=$url, n.created_at=datetime(),
                              n.urlhaus_status=$status, n.feed_source='urlhaus'
                ON MATCH  SET n.urlhaus_status=$status, n.feed_source='urlhaus',
                              n.feed_last_seen=datetime()
                """,
                id=nid, url=url_val[:500],
                status=(item.get("url_status") or ""),
            )
            created += 1
    return {"imported": created, "total": len(urls)}


async def _sync_threatfox_recent(db) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            _FEED_REGISTRY["threatfox_recent"]["url"],
            json={"query": "get_iocs", "days": 1},
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    iocs = data.get("data") or []
    created = 0
    async with db.driver.session() as session:
        for item in iocs[:500]:
            ioc_val   = (item.get("ioc") or "").strip()
            ioc_type  = (item.get("ioc_type") or "").lower()
            malware   = (item.get("malware") or "")
            if not ioc_val:
                continue
            if ioc_type in ("ip:port", "ip"):
                label, nid = "IP", f"ip:{ioc_val.split(':')[0]}"
                addr = ioc_val.split(":")[0]
                await session.run(
                    """
                    MERGE (n:IP {id:$id})
                    ON CREATE SET n.address=$addr, n.created_at=datetime(),
                                  n.feed_source='threatfox', n.feed_malware=$mal
                    ON MATCH  SET n.feed_source='threatfox', n.feed_malware=$mal
                    """,
                    id=nid, addr=addr, mal=malware,
                )
            elif ioc_type == "domain":
                nid = f"domain:{ioc_val}"
                await session.run(
                    """
                    MERGE (n:Domain {id:$id})
                    ON CREATE SET n.domain=$d, n.created_at=datetime(),
                                  n.feed_source='threatfox', n.feed_malware=$mal
                    ON MATCH  SET n.feed_source='threatfox', n.feed_malware=$mal
                    """,
                    id=nid, d=ioc_val, mal=malware,
                )
            elif ioc_type in ("sha256_hash", "md5_hash", "sha1_hash"):
                nid = f"hash:{ioc_val}"
                await session.run(
                    """
                    MERGE (n:Hash {id:$id})
                    ON CREATE SET n.hash_value=$h, n.created_at=datetime(),
                                  n.feed_source='threatfox', n.feed_malware=$mal
                    ON MATCH  SET n.feed_source='threatfox', n.feed_malware=$mal
                    """,
                    id=nid, h=ioc_val, mal=malware,
                )
            elif ioc_type == "url":
                nid = f"url:{ioc_val[:200]}"
                await session.run(
                    """
                    MERGE (n:URL {id:$id})
                    ON CREATE SET n.url=$u, n.created_at=datetime(),
                                  n.feed_source='threatfox', n.feed_malware=$mal
                    ON MATCH  SET n.feed_source='threatfox', n.feed_malware=$mal
                    """,
                    id=nid, u=ioc_val[:500], mal=malware,
                )
            else:
                continue
            created += 1
    return {"imported": created, "total": len(iocs)}


async def _sync_mb_recent(db) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            _FEED_REGISTRY["mb_recent"]["url"],
            data={"query": "get_recent", "selector": "time"},
        )
        r.raise_for_status()
        data = r.json()
    samples = data.get("data") or []
    created = 0
    async with db.driver.session() as session:
        for item in samples[:200]:
            h = (item.get("sha256_hash") or "").strip()
            if not h:
                continue
            nid = f"hash:{h}"
            await session.run(
                """
                MERGE (n:Hash {id:$id})
                ON CREATE SET n.hash_value=$h, n.hash_type='sha256',
                              n.created_at=datetime(), n.feed_source='malwarebazaar',
                              n.mb_signature=$sig, n.mb_file_type=$ft
                ON MATCH  SET n.feed_source='malwarebazaar', n.mb_signature=$sig,
                              n.mb_file_type=$ft, n.feed_last_seen=datetime()
                """,
                id=nid, h=h,
                sig=(item.get("signature") or ""),
                ft=(item.get("file_type") or ""),
            )
            created += 1
    return {"imported": created, "total": len(samples)}


async def _sync_otx(db) -> dict:
    key = os.getenv("OTX_API_KEY", "")
    if not key:
        raise ValueError("OTX_API_KEY not set")
    created = 0
    total   = 0
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            _FEED_REGISTRY["otx"]["url"],
            headers={"X-OTX-API-KEY": key},
            params={"limit": 20, "modified_since": ""},
        )
        r.raise_for_status()
        data = r.json()
    pulses = data.get("results") or []
    async with db.driver.session() as session:
        for pulse in pulses:
            for indicator in (pulse.get("indicators") or [])[:100]:
                itype = (indicator.get("type") or "").lower()
                val   = (indicator.get("indicator") or "").strip()
                if not val:
                    continue
                total += 1
                mal = pulse.get("name", "OTX")
                if itype == "IPv4":
                    nid = f"ip:{val}"
                    await session.run(
                        "MERGE (n:IP {id:$id}) ON CREATE SET n.address=$a, n.created_at=datetime(), n.feed_source='otx', n.feed_malware=$m ON MATCH SET n.feed_source='otx'",
                        id=nid, a=val, m=mal,
                    )
                elif itype == "domain":
                    nid = f"domain:{val}"
                    await session.run(
                        "MERGE (n:Domain {id:$id}) ON CREATE SET n.domain=$d, n.created_at=datetime(), n.feed_source='otx', n.feed_malware=$m ON MATCH SET n.feed_source='otx'",
                        id=nid, d=val, m=mal,
                    )
                elif itype in ("FileHash-SHA256", "FileHash-MD5", "FileHash-SHA1"):
                    nid = f"hash:{val}"
                    await session.run(
                        "MERGE (n:Hash {id:$id}) ON CREATE SET n.hash_value=$h, n.created_at=datetime(), n.feed_source='otx', n.feed_malware=$m ON MATCH SET n.feed_source='otx'",
                        id=nid, h=val, m=mal,
                    )
                else:
                    continue
                created += 1
    return {"imported": created, "total": total}


_FEED_SYNC_FNS: dict[str, any] = {
    "feodo":           _sync_feodo,
    "urlhaus_recent":  _sync_urlhaus_recent,
    "threatfox_recent":_sync_threatfox_recent,
    "mb_recent":       _sync_mb_recent,
    "otx":             _sync_otx,
}


@app.get("/feeds")
async def list_feeds(_user: dict = Depends(get_current_user)):
    """List all known feed subscriptions with their status."""
    feeds = []
    for name, meta in _FEED_REGISTRY.items():
        has_key = True
        if meta.get("key_env"):
            has_key = bool(os.getenv(meta["key_env"], ""))
        status = _feed_status.get(name, {})
        feeds.append({
            "id":          name,
            "name":        meta["name"],
            "description": meta["description"],
            "free":        meta["free"],
            "ioc_type":    meta["ioc_type"],
            "key_env":     meta.get("key_env"),
            "has_key":     has_key,
            "enabled":     _feed_enabled(name),
            "last_sync":   status.get("last_sync"),
            "last_count":  status.get("last_count", 0),
            "last_error":  status.get("last_error"),
        })
    return {"feeds": feeds}


@app.post("/feeds/{feed_name}/sync")
async def sync_feed(
    feed_name: str,
    _user:     dict = Depends(get_current_user),
):
    """Manually trigger a sync of a single feed."""
    if feed_name not in _FEED_REGISTRY:
        raise HTTPException(404, f"Unknown feed: {feed_name}")
    meta = _FEED_REGISTRY[feed_name]
    if meta.get("key_env") and not os.getenv(meta["key_env"], ""):
        raise HTTPException(402, f"{meta['key_env']} not set — add it in Admin → API Keys")
    fn = _FEED_SYNC_FNS.get(feed_name)
    if not fn:
        raise HTTPException(501, "Sync not implemented for this feed")
    try:
        result = await fn(graph_db)
        _feed_status[feed_name] = {
            "last_sync":  datetime.utcnow().isoformat() + "Z",
            "last_count": result.get("imported", 0),
            "last_error": None,
        }
        _audit("FeedSync", feed_name, detail=f"imported={result.get('imported', 0)}")
        return {"feed": feed_name, **result, "status": "ok"}
    except Exception as exc:
        err = str(exc)[:200]
        _feed_status[feed_name] = {
            "last_sync":  datetime.utcnow().isoformat() + "Z",
            "last_count": 0,
            "last_error": err,
        }
        raise HTTPException(500, f"Feed sync failed: {err}")


@app.post("/feeds/sync-all")
async def sync_all_feeds(
    background: BackgroundTasks,
    _user:      dict = Depends(get_current_user),
):
    """Sync all enabled feeds in the background."""
    enabled = [n for n in _FEED_REGISTRY if _feed_enabled(n)]
    if not enabled:
        return {"message": "No feeds enabled — toggle feeds on in Admin first", "queued": 0}

    async def _run_all():
        for name in enabled:
            fn = _FEED_SYNC_FNS.get(name)
            if not fn:
                continue
            try:
                result = await fn(graph_db)
                _feed_status[name] = {
                    "last_sync":  datetime.utcnow().isoformat() + "Z",
                    "last_count": result.get("imported", 0),
                    "last_error": None,
                }
            except Exception as exc:
                _feed_status[name] = {
                    "last_sync":  datetime.utcnow().isoformat() + "Z",
                    "last_count": 0,
                    "last_error": str(exc)[:200],
                }

    background.add_task(_run_all)
    return {"message": "Sync started in background", "queued": len(enabled), "feeds": enabled}


@app.post("/feeds/{feed_name}/toggle")
async def toggle_feed(
    feed_name: str,
    payload:   dict = Body(...),
    _user:     dict = Depends(get_current_user),
):
    """Enable or disable a feed (writes to .env)."""
    if feed_name not in _FEED_REGISTRY:
        raise HTTPException(404, "Unknown feed")
    enabled = bool(payload.get("enabled", False))
    _write_env_key(f"FEED_{feed_name.upper()}_ENABLED", "true" if enabled else "false")
    return {"feed": feed_name, "enabled": enabled}


# ─────────────────────────────────────────────────────────────────────────────
# Ph74 — Entity Coverage Report
# ─────────────────────────────────────────────────────────────────────────────

# Per-label enrichment coverage checks: each value is (display_name, predicate)
_COVERAGE_CHECKS: dict[str, list[tuple[str, any]]] = {
    "IP": [
        ("GreyNoise",  lambda p: bool(p.get("gn_classification"))),
        ("Shodan",     lambda p: bool(p.get("cves") or p.get("shodan_ports") or p.get("shodan_data"))),
        ("VirusTotal", lambda p: p.get("vt_malicious") is not None),
        ("AbuseIPDB",  lambda p: p.get("abuseipdb_score") is not None),
        ("URLhaus",    lambda p: p.get("urlhaus_listed") is not None),
        ("ASN",        lambda p: bool(p.get("asn"))),
        ("Geo",        lambda p: bool(p.get("country") or p.get("city"))),
    ],
    "Domain": [
        ("RDAP",        lambda p: bool(p.get("registrar") or p.get("created"))),
        ("Passive DNS", lambda p: bool(p.get("pdns_ips") or p.get("pdns_count"))),
        ("Certs",       lambda p: bool(p.get("cert_count") or p.get("has_certs"))),
        ("URLScan",     lambda p: bool(p.get("urlscan_count") or p.get("urlscan_scan_count"))),
        ("URLhaus",     lambda p: p.get("urlhaus_listed") is not None),
        ("VirusTotal",  lambda p: p.get("vt_malicious") is not None),
        ("Wayback",     lambda p: bool(p.get("wayback_url") or p.get("wayback_snapshot"))),
    ],
    "Hash": [
        ("MalwareBazaar", lambda p: bool(p.get("mb_signature") or p.get("signature"))),
        ("VirusTotal",    lambda p: p.get("vt_malicious") is not None),
        ("ThreatFox",     lambda p: bool(p.get("feed_source") == "threatfox" or p.get("tf_malware"))),
    ],
    "URL": [
        ("URLhaus",    lambda p: p.get("urlhaus_listed") is not None),
        ("URLScan",    lambda p: bool(p.get("urlscan_count") or p.get("urlscan_scan_count"))),
        ("VirusTotal", lambda p: p.get("vt_malicious") is not None),
    ],
    "Email": [
        ("HIBP",  lambda p: p.get("hibp_breached") is not None),
        ("Haveibeenpwned", lambda p: bool(p.get("hibp_breaches"))),
    ],
}


@app.get("/case/{case_id}/coverage")
async def case_coverage(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """
    For each entity linked to the case, return which enrichment sources have
    data and which are still blank.  Used to surface investigative blind-spots.
    """
    cid = _val_case_id(case_id)
    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (c:Case {id:$cid})-[:HAS_ENTITY]->(e)
            RETURN e.id AS id, labels(e)[0] AS label,
                   coalesce(e.display_name, e.address, e.domain,
                            e.hash_value, e.url, e.email, e.id) AS name,
                   properties(e) AS props
            ORDER BY label, name
            LIMIT 200
            """,
            cid=cid,
        )
        records = await r.fetch(200)

    entities = []
    all_checks: set[str] = set()

    for rec in records:
        label  = rec["label"] or "Unknown"
        props  = dict(rec["props"])
        checks = _COVERAGE_CHECKS.get(label, [])
        cov    = {}
        for check_name, pred in checks:
            all_checks.add(check_name)
            try:
                cov[check_name] = bool(pred(props))
            except Exception:
                cov[check_name] = False
        total  = len(checks)
        done   = sum(1 for v in cov.values() if v)
        entities.append({
            "id":       rec["id"],
            "label":    label,
            "name":     rec["name"] or rec["id"],
            "coverage": cov,
            "done":     done,
            "total":    total,
            "pct":      round(done / total * 100) if total else 0,
        })

    # Summary by label
    by_label: dict[str, dict] = {}
    for e in entities:
        lb = e["label"]
        if lb not in by_label:
            by_label[lb] = {"count": 0, "total_pct": 0}
        by_label[lb]["count"] += 1
        by_label[lb]["total_pct"] += e["pct"]
    for lb, d in by_label.items():
        d["avg_pct"] = round(d["total_pct"] / d["count"]) if d["count"] else 0

    return {
        "case_id":  cid,
        "entities": entities,
        "by_label": by_label,
        "total":    len(entities),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph75 — Saved Searches / Alert Rules
# ─────────────────────────────────────────────────────────────────────────────

# Built-in preset queries — run as Cypher, no user-supplied input
_SEARCH_PRESETS: dict[str, dict] = {
    "high_risk_ips": {
        "name":        "High-risk IPs",
        "description": "IPs with GreyNoise malicious or URLhaus hits added in the last 7 days",
        "cypher": """
            MATCH (n:IP)
            WHERE (n.gn_classification = 'malicious' OR n.urlhaus_listed = true)
              AND n.created_at >= datetime() - duration('P7D')
            RETURN n.id AS id, labels(n)[0] AS label,
                   coalesce(n.address, n.id) AS name,
                   n.created_at AS ts
            ORDER BY n.created_at DESC LIMIT 100
        """,
    },
    "new_feed_iocs": {
        "name":        "New feed IOCs",
        "description": "All nodes ingested from subscribed feeds in the last 24 hours",
        "cypher": """
            MATCH (n)
            WHERE n.feed_source IS NOT NULL
              AND n.created_at >= datetime() - duration('P1D')
            RETURN n.id AS id, labels(n)[0] AS label,
                   coalesce(n.address, n.domain, n.hash_value, n.url, n.id) AS name,
                   n.feed_source AS ts
            ORDER BY n.created_at DESC LIMIT 200
        """,
    },
    "orphan_nodes": {
        "name":        "Orphan IOCs",
        "description": "Nodes not linked to any case and not enriched",
        "cypher": """
            MATCH (n)
            WHERE NOT (n:Case) AND NOT (n:AuditLog) AND NOT (n:User)
              AND NOT ()-[:HAS_ENTITY]->(n)
              AND NOT (n)-[:HAS_ENTITY]->()
              AND NOT (n)<-[:RESOLVES_TO]-()
              AND NOT (n)-[:RESOLVES_TO]->()
            RETURN n.id AS id, labels(n)[0] AS label,
                   coalesce(n.address, n.domain, n.hash_value, n.url, n.id) AS name,
                   n.created_at AS ts
            ORDER BY n.created_at DESC LIMIT 100
        """,
    },
    "expiring_certs": {
        "name":        "Expiring certificates",
        "description": "Domains whose stored certs expire within 30 days",
        "cypher": """
            MATCH (dom:Domain)-[:HAS_CERT]->(c:Certificate)
            WHERE c.not_after IS NOT NULL
              AND c.not_after <= datetime() + duration('P30D')
              AND c.not_after >= datetime()
            RETURN dom.id AS id, 'Domain' AS label,
                   coalesce(dom.domain, dom.id) AS name,
                   c.not_after AS ts
            ORDER BY c.not_after ASC LIMIT 50
        """,
    },
    "malware_hashes": {
        "name":        "Malware hashes",
        "description": "Hash nodes with MalwareBazaar or VirusTotal detections",
        "cypher": """
            MATCH (n:Hash)
            WHERE n.mb_signature IS NOT NULL OR n.vt_malicious > 0
            RETURN n.id AS id, 'Hash' AS label,
                   coalesce(n.hash_value, n.id) AS name,
                   coalesce(n.mb_signature, toString(n.vt_malicious) + ' VT detections') AS ts
            ORDER BY n.created_at DESC LIMIT 100
        """,
    },
}

# In-memory store for custom saved searches (keyed by id)
_saved_searches: dict[str, dict] = {}


class SavedSearchCreate(BaseModel):
    name:        str  = Field(..., min_length=1, max_length=120)
    description: str  = Field("",  max_length=400)
    cypher:      str  = Field(..., min_length=10, max_length=2000)


# Very conservative allowlist — no WRITE operations
_CYPHER_DENY = re.compile(
    r'\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|CALL\s+db\.|LOAD\s+CSV)\b',
    re.IGNORECASE,
)


@app.get("/saved-searches")
async def list_saved_searches(_user: dict = Depends(get_current_user)):
    """Return presets + user-created saved searches."""
    presets = [
        {"id": k, "preset": True, **{kk: vv for kk, vv in v.items() if kk != "cypher"}}
        for k, v in _SEARCH_PRESETS.items()
    ]
    custom = list(_saved_searches.values())
    return {"presets": presets, "custom": custom, "total": len(presets) + len(custom)}


@app.post("/saved-searches")
async def create_saved_search(
    req:   SavedSearchCreate,
    _user: dict = Depends(get_current_user),
):
    """Create a custom saved search (read-only Cypher only)."""
    if _CYPHER_DENY.search(req.cypher):
        raise HTTPException(400, "Custom queries must be read-only (no CREATE/MERGE/SET/DELETE)")
    sid  = f"custom_{uuid.uuid4().hex[:10]}"
    entry = {
        "id":          sid,
        "preset":      False,
        "name":        req.name,
        "description": req.description,
        "cypher":      req.cypher,
        "created_at":  datetime.utcnow().isoformat() + "Z",
        "last_run":    None,
        "last_count":  0,
    }
    _saved_searches[sid] = entry
    return entry


@app.delete("/saved-searches/{search_id}")
async def delete_saved_search(
    search_id: str,
    _user:     dict = Depends(get_current_user),
):
    if search_id not in _saved_searches:
        raise HTTPException(404, "Search not found or is a built-in preset (cannot delete)")
    del _saved_searches[search_id]
    return {"deleted": search_id}


@app.post("/saved-searches/{search_id}/run")
async def run_saved_search(
    search_id: str,
    _user:     dict = Depends(get_current_user),
):
    """Run a preset or custom saved search and return matching entities."""
    if search_id in _SEARCH_PRESETS:
        cypher = _SEARCH_PRESETS[search_id]["cypher"]
        meta   = _SEARCH_PRESETS[search_id]
    elif search_id in _saved_searches:
        cypher = _saved_searches[search_id]["cypher"]
        meta   = _saved_searches[search_id]
        if _CYPHER_DENY.search(cypher):
            raise HTTPException(400, "Stored query contains disallowed operations")
    else:
        raise HTTPException(404, "Search not found")

    async with graph_db.driver.session() as session:
        try:
            r = await session.run(cypher)
            records = await r.fetch(200)
        except Exception as exc:
            raise HTTPException(400, f"Query error: {exc}")

    results = [
        {
            "id":    rec.get("id", ""),
            "label": rec.get("label", ""),
            "name":  rec.get("name", "") or rec.get("id", ""),
            "ts":    str(rec.get("ts", "")) if rec.get("ts") else "",
        }
        for rec in records
    ]

    if search_id in _saved_searches:
        _saved_searches[search_id]["last_run"]   = datetime.utcnow().isoformat() + "Z"
        _saved_searches[search_id]["last_count"] = len(results)

    _audit("SavedSearch.run", search_id, detail=f"results={len(results)}")
    return {
        "search_id":   search_id,
        "name":        meta.get("name", search_id),
        "results":     results,
        "count":       len(results),
        "ran_at":      datetime.utcnow().isoformat() + "Z",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph76 — IOC Defang / Refang Utility
# ─────────────────────────────────────────────────────────────────────────────

import re as _re76

# Defang transformation patterns — order matters
_DEFANG_RULES: list[tuple] = [
    # scheme
    (_re76.compile(r'\bhttp://',  _re76.I), 'hXXp://'),
    (_re76.compile(r'\bhttps://', _re76.I), 'hXXps://'),
    (_re76.compile(r'\bftp://',   _re76.I), 'fXXp://'),
    # dot in domain/IP
    (_re76.compile(r'(?<=[\w\-])\.([\w\-])'), r'[.]\1'),
    # @ in email
    (_re76.compile(r'(?<=[\w\-])@(?=[\w\-])'), '[@]'),
]

# Refang: reverse the most common defang variants
_REFANG_RULES: list[tuple] = [
    (_re76.compile(r'hXXps?://',   _re76.I), lambda m: m.group().replace('XX','tt').replace('Xp','tp')),
    (_re76.compile(r'h\[tt\]ps?://', _re76.I), lambda m: m.group().replace('[tt]','tt')),
    (_re76.compile(r'fXXp://',     _re76.I), lambda m: 'ftp://'),
    (_re76.compile(r'\[[\.\-]\]'), lambda m: m.group()[1]),  # [.] → .   [-] → -
    (_re76.compile(r'\(\.\)'),     lambda m: '.'),            # (.) → .
    (_re76.compile(r'\[at\]',  _re76.I), lambda m: '@'),     # [at] → @
    (_re76.compile(r'\[@\]'),      lambda m: '@'),            # [@] → @
    (_re76.compile(r'\\\.'),       lambda m: '.'),            # \. → .
    (_re76.compile(r'dot',         _re76.I), '.'),            # simple "dot" replacement – only applied to tokenised values
]

# Re-use Ph68 classifier
def _defang_text(text: str) -> str:
    result = text
    for pat, repl in _DEFANG_RULES:
        result = pat.sub(repl, result)
    return result


def _refang_text(text: str) -> str:
    result = text
    for pat, repl in _REFANG_RULES:
        if callable(repl):
            result = pat.sub(repl, result)
        else:
            result = pat.sub(repl, result)
    return result


class DefangRequest(BaseModel):
    text: str = Field(..., max_length=50_000)
    mode: str = Field("defang", pattern="^(defang|refang)$")


@app.post("/util/defang")
async def util_defang(req: DefangRequest, _user: dict = Depends(get_current_user)):
    """
    Defang or refang IOC text.
    defang: evil.com → evil[.]com, http:// → hXXp://
    refang: evil[.]com → evil.com, hXXp:// → http://
    Also returns extracted IOC classifications for refanged text.
    """
    if req.mode == "defang":
        output = _defang_text(req.text)
        return {"mode": "defang", "output": output, "iocs": []}

    # refang mode — also classify the cleaned IOCs
    refanged = _refang_text(req.text)
    iocs = []
    for tok in _re76.split(r'[\s,;\n\r|<>\[\]()"\']', refanged):
        tok = tok.strip().rstrip('.,;:!?')
        if len(tok) < 4:
            continue
        label, subtype = _classify_ioc(tok)
        if label not in ("Unknown",):
            iocs.append({"value": tok, "label": label, "subtype": subtype})
    # deduplicate by value
    seen: set[str] = set()
    deduped = []
    for i in iocs:
        k = i["value"].lower()
        if k not in seen:
            seen.add(k)
            deduped.append(i)

    return {"mode": "refang", "output": refanged, "iocs": deduped}


# ─────────────────────────────────────────────────────────────────────────────
# Ph77 — Outbound Webhook Notifications
# ─────────────────────────────────────────────────────────────────────────────

# In-memory webhook registry
_webhooks: dict[str, dict] = {}

# Load from env on startup if WEBHOOK_URL_1 / WEBHOOK_URL_2 etc. are set
def _load_webhooks_from_env() -> None:
    for i in range(1, 6):
        url  = os.getenv(f"WEBHOOK_URL_{i}", "")
        name = os.getenv(f"WEBHOOK_NAME_{i}", f"Webhook {i}")
        events = os.getenv(f"WEBHOOK_EVENTS_{i}", "alert,feed_sync")
        if url:
            wid = f"env_wh_{i}"
            _webhooks[wid] = {
                "id": wid, "name": name, "url": url,
                "events": [e.strip() for e in events.split(",")],
                "enabled": True, "source": "env",
                "last_fired": None, "last_status": None,
            }

_load_webhooks_from_env()


async def _fire_webhook(wid: str, event_type: str, payload: dict) -> None:
    """Fire a webhook in the background — does not raise."""
    wh = _webhooks.get(wid)
    if not wh or not wh.get("enabled"):
        return
    if event_type not in (wh.get("events") or []):
        return
    body = {
        "event":     event_type,
        "source":    "fieldwork-osint",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        **payload,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(wh["url"], json=body)
            _webhooks[wid]["last_fired"]  = datetime.utcnow().isoformat() + "Z"
            _webhooks[wid]["last_status"] = resp.status_code
    except Exception as exc:
        _webhooks[wid]["last_fired"]  = datetime.utcnow().isoformat() + "Z"
        _webhooks[wid]["last_status"] = f"error: {exc}"
        log.warning("webhook %s failed: %s", wid, exc)


async def _broadcast_event(event_type: str, payload: dict) -> None:
    """Fire matching webhooks for the given event type."""
    for wid in list(_webhooks):
        await _fire_webhook(wid, event_type, payload)


class WebhookCreate(BaseModel):
    name:   str       = Field(..., min_length=1, max_length=80)
    url:    str       = Field(..., min_length=10, max_length=500)
    events: List[str] = Field(default_factory=lambda: ["alert", "feed_sync", "risk_change"])


_ALLOWED_EVENTS = {"alert", "feed_sync", "risk_change", "case.create", "entity.add", "test"}


@app.get("/settings/webhooks")
async def list_webhooks(_user: dict = Depends(get_current_user)):
    """List all configured webhooks."""
    return {"webhooks": list(_webhooks.values()), "allowed_events": sorted(_ALLOWED_EVENTS)}


@app.post("/settings/webhooks")
async def create_webhook(
    req:   WebhookCreate,
    _user: dict = Depends(get_current_user),
):
    """Register a new outbound webhook."""
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")
    filtered_events = [e for e in req.events if e in _ALLOWED_EVENTS]
    if not filtered_events:
        raise HTTPException(400, f"No valid events. Allowed: {sorted(_ALLOWED_EVENTS)}")
    wid = f"wh_{uuid.uuid4().hex[:10]}"
    entry = {
        "id":          wid,
        "name":        req.name,
        "url":         req.url,
        "events":      filtered_events,
        "enabled":     True,
        "source":      "ui",
        "last_fired":  None,
        "last_status": None,
    }
    _webhooks[wid] = entry
    _write_env_key(f"WEBHOOK_URL_{len(_webhooks)}", req.url)
    _write_env_key(f"WEBHOOK_NAME_{len(_webhooks)}", req.name)
    _write_env_key(f"WEBHOOK_EVENTS_{len(_webhooks)}", ",".join(filtered_events))
    return entry


@app.delete("/settings/webhooks/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    _user:      dict = Depends(get_current_user),
):
    if webhook_id not in _webhooks:
        raise HTTPException(404, "Webhook not found")
    del _webhooks[webhook_id]
    return {"deleted": webhook_id}


@app.patch("/settings/webhooks/{webhook_id}")
async def toggle_webhook(
    webhook_id: str,
    payload:    dict = Body(...),
    _user:      dict = Depends(get_current_user),
):
    if webhook_id not in _webhooks:
        raise HTTPException(404, "Webhook not found")
    if "enabled" in payload:
        _webhooks[webhook_id]["enabled"] = bool(payload["enabled"])
    return _webhooks[webhook_id]


@app.post("/settings/webhooks/{webhook_id}/test")
async def test_webhook(
    webhook_id: str,
    _user:      dict = Depends(get_current_user),
):
    """Fire a test payload to the webhook immediately."""
    if webhook_id not in _webhooks:
        raise HTTPException(404, "Webhook not found")
    wh = _webhooks[webhook_id]
    test_body = {
        "event":     "test",
        "source":    "fieldwork-osint",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "message":   "This is a test notification from Fieldwork OSINT.",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(wh["url"], json=test_body)
            _webhooks[webhook_id]["last_fired"]  = datetime.utcnow().isoformat() + "Z"
            _webhooks[webhook_id]["last_status"] = resp.status_code
            return {"status": resp.status_code, "ok": resp.status_code < 400}
    except Exception as exc:
        _webhooks[webhook_id]["last_status"] = f"error: {exc}"
        raise HTTPException(502, f"Webhook delivery failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Ph78 — GraphML / JSON Export (extends existing GEXF/CSV export)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/graph/export-extended")
async def export_graph_extended(
    format:    str  = "graphml",          # graphml | json | jsonld
    entity_id: Optional[str] = None,
    depth:     int  = 2,
    case_id:   Optional[str] = None,
    _user:     dict = Depends(get_current_user),
):
    """
    Extended graph export — GraphML (yEd/Gephi/Cytoscape Desktop), full JSON
    (with all node properties), or JSON-LD (semantic web compatible).
    Optionally scoped to a specific case via case_id.
    """
    import io, json, xml.etree.ElementTree as ET

    async with graph_db.driver.session() as session:
        if case_id:
            cid = _val_case_id(case_id)
            result = await session.run(
                """
                MATCH (c:Case {id:$cid})-[:HAS_ENTITY]->(n)
                WITH collect(DISTINCT n) AS case_nodes
                UNWIND case_nodes AS n
                OPTIONAL MATCH (n)-[r]-(m)
                WHERE m IN case_nodes
                WITH collect(DISTINCT {
                    id: n.id, label: head(labels(n)),
                    display: coalesce(n.display_name, n.address, n.domain,
                                      n.hash_value, n.url, n.email, n.id),
                    props: properties(n)
                }) AS node_list,
                collect(DISTINCT {
                    src: startNode(r).id, dst: endNode(r).id, type: type(r),
                    props: properties(r)
                }) AS edge_list
                RETURN node_list AS nodes, edge_list AS edges
                """, cid=cid,
            )
        elif entity_id:
            result = await session.run(
                f"""
                MATCH path = (seed)-[*0..{min(depth, 4)}]-(n)
                WHERE seed.id = $eid
                WITH collect(DISTINCT n) AS nodes_set,
                     collect(DISTINCT relationships(path)) AS rel_lists
                UNWIND nodes_set AS node
                WITH collect(DISTINCT {{
                    id: node.id, label: head(labels(node)),
                    display: coalesce(node.display_name, node.address, node.domain,
                                      node.hash_value, node.url, node.email, node.id),
                    props: properties(node)
                }}) AS node_list, rel_lists
                UNWIND rel_lists AS rels
                UNWIND rels AS rel
                WITH node_list, collect(DISTINCT {{
                    src: startNode(rel).id, dst: endNode(rel).id, type: type(rel),
                    props: properties(rel)
                }}) AS edge_list
                RETURN node_list AS nodes, edge_list AS edges
                """, eid=entity_id,
            )
        else:
            result = await session.run(
                """
                MATCH (n) WHERE n.id IS NOT NULL
                WITH collect(DISTINCT {
                    id: n.id, label: head(labels(n)),
                    display: coalesce(n.display_name, n.address, n.domain,
                                      n.hash_value, n.url, n.email, n.id),
                    props: properties(n)
                })[..1000] AS node_list
                OPTIONAL MATCH (a)-[r]->(b)
                WHERE a.id IS NOT NULL AND b.id IS NOT NULL
                WITH node_list,
                     collect(DISTINCT {
                         src: a.id, dst: b.id, type: type(r),
                         props: properties(r)
                     })[..3000] AS edge_list
                RETURN node_list AS nodes, edge_list AS edges
                """
            )
        row = await result.single()

    nodes = list(row["nodes"] or []) if row else []
    edges = list(row["edges"] or []) if row else []

    # ── GraphML ──────────────────────────────────────────────────────────────
    if format == "graphml":
        ns = "http://graphml.graphdrawing.org/xmlns"
        root = ET.Element("graphml", {
            "xmlns":               ns,
            "xmlns:xsi":           "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation":  f"{ns} http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd",
        })

        # Declare keys
        keys = [
            ("d0", "node", "label",   "string"),
            ("d1", "node", "type",    "string"),
            ("d2", "node", "risk",    "int"),
            ("d3", "edge", "type",    "string"),
        ]
        for kid, kfor, kname, ktype in keys:
            ET.SubElement(root, "key", {
                "id": kid, "for": kfor,
                "attr.name": kname, "attr.type": ktype,
            })

        graph_el = ET.SubElement(root, "graph", {
            "id": "G", "edgedefault": "directed",
        })

        for n in nodes:
            if not n.get("id"):
                continue
            ne = ET.SubElement(graph_el, "node", {"id": str(n["id"])})
            d0 = ET.SubElement(ne, "data", {"key": "d0"})
            d0.text = str(n.get("display") or n["id"])[:80]
            d1 = ET.SubElement(ne, "data", {"key": "d1"})
            d1.text = str(n.get("label", ""))
            risk_props = dict(n.get("props") or {})
            risk = _compute_risk_score(risk_props).get("score", 0)
            d2 = ET.SubElement(ne, "data", {"key": "d2"})
            d2.text = str(risk)

        seen_edges: set[str] = set()
        for i, e in enumerate(edges):
            src, dst = e.get("src"), e.get("dst")
            if not src or not dst:
                continue
            etype = e.get("type", "")
            key = f"{src}__{dst}__{etype}"
            if key in seen_edges:
                continue
            seen_edges.add(key)
            ee = ET.SubElement(graph_el, "edge", {
                "id": f"e{i}", "source": str(src), "target": str(dst),
            })
            d3 = ET.SubElement(ee, "data", {"key": "d3"})
            d3.text = etype

        xml_bytes = ET.tostring(root, encoding="unicode")
        xml_str   = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes
        return Response(
            content=xml_str,
            media_type="application/xml",
            headers={"Content-Disposition": "attachment; filename=fieldwork-graph.graphml"},
        )

    # ── Full JSON ────────────────────────────────────────────────────────────
    if format == "json":
        # Strip Neo4j DateTime objects (not JSON-serialisable)
        def _safe_props(p):
            out = {}
            for k, v in (p or {}).items():
                try:
                    json.dumps(v)
                    out[k] = v
                except (TypeError, ValueError):
                    out[k] = str(v)
            return out

        payload = {
            "format":      "fieldwork-graph-v1",
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "node_count":  len(nodes),
            "edge_count":  len(edges),
            "nodes": [
                {
                    "id":           n.get("id"),
                    "label":        n.get("label"),
                    "display_name": n.get("display"),
                    "properties":   _safe_props(n.get("props")),
                }
                for n in nodes if n.get("id")
            ],
            "edges": [
                {
                    "source":     e.get("src"),
                    "target":     e.get("dst"),
                    "type":       e.get("type"),
                    "properties": _safe_props(e.get("props")),
                }
                for e in edges if e.get("src") and e.get("dst")
            ],
        }
        return Response(
            content=json.dumps(payload, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=fieldwork-graph.json"},
        )

    # ── JSON-LD (semantic web friendly) ──────────────────────────────────────
    if format == "jsonld":
        ctx = {
            "@vocab":     "https://fieldwork.local/ontology#",
            "id":         "@id",
            "type":       "@type",
            "label":      "rdfs:label",
            "rdfs":       "http://www.w3.org/2000/01/rdf-schema#",
        }
        graph_jsonld = []
        for n in nodes:
            if not n.get("id"):
                continue
            graph_jsonld.append({
                "id":    f"fw:{n['id']}",
                "type":  n.get("label") or "Entity",
                "label": n.get("display") or n["id"],
            })
        for e in edges:
            if not e.get("src") or not e.get("dst"):
                continue
            graph_jsonld.append({
                "id":     f"fw:edge:{e.get('src')}:{e.get('dst')}:{e.get('type','')}",
                "type":   e.get("type", "RelatedTo"),
                "source": f"fw:{e['src']}",
                "target": f"fw:{e['dst']}",
            })
        payload = {"@context": ctx, "@graph": graph_jsonld}
        return Response(
            content=json.dumps(payload, indent=2, default=str),
            media_type="application/ld+json",
            headers={"Content-Disposition": "attachment; filename=fieldwork-graph.jsonld"},
        )

    raise HTTPException(400, f"Unsupported format: {format} (use graphml | json | jsonld)")


# ─────────────────────────────────────────────────────────────────────────────
# Ph79 — MITRE ATT&CK Technique Tagging
# ─────────────────────────────────────────────────────────────────────────────

# Curated subset of common ATT&CK techniques relevant to OSINT investigations
_MITRE_TECHNIQUES: dict[str, dict] = {
    # Initial Access
    "T1566":    {"name": "Phishing",                       "tactic": "Initial Access"},
    "T1566.001":{"name": "Spearphishing Attachment",       "tactic": "Initial Access"},
    "T1566.002":{"name": "Spearphishing Link",             "tactic": "Initial Access"},
    "T1190":    {"name": "Exploit Public-Facing App",      "tactic": "Initial Access"},
    "T1133":    {"name": "External Remote Services",       "tactic": "Initial Access"},
    "T1078":    {"name": "Valid Accounts",                 "tactic": "Initial Access"},
    "T1195":    {"name": "Supply Chain Compromise",        "tactic": "Initial Access"},
    # Execution
    "T1059":    {"name": "Command & Scripting Interpreter","tactic": "Execution"},
    "T1204":    {"name": "User Execution",                 "tactic": "Execution"},
    # Persistence
    "T1098":    {"name": "Account Manipulation",           "tactic": "Persistence"},
    "T1136":    {"name": "Create Account",                 "tactic": "Persistence"},
    # Defense Evasion
    "T1027":    {"name": "Obfuscated Files or Information","tactic": "Defense Evasion"},
    "T1140":    {"name": "Deobfuscate/Decode Files",       "tactic": "Defense Evasion"},
    "T1562":    {"name": "Impair Defenses",                "tactic": "Defense Evasion"},
    # Credential Access
    "T1110":    {"name": "Brute Force",                    "tactic": "Credential Access"},
    "T1555":    {"name": "Credentials from Password Stores","tactic": "Credential Access"},
    "T1003":    {"name": "OS Credential Dumping",          "tactic": "Credential Access"},
    # Discovery
    "T1018":    {"name": "Remote System Discovery",        "tactic": "Discovery"},
    "T1046":    {"name": "Network Service Discovery",      "tactic": "Discovery"},
    "T1083":    {"name": "File and Directory Discovery",   "tactic": "Discovery"},
    "T1595":    {"name": "Active Scanning",                "tactic": "Reconnaissance"},
    "T1592":    {"name": "Gather Victim Host Information", "tactic": "Reconnaissance"},
    "T1589":    {"name": "Gather Victim Identity Info",    "tactic": "Reconnaissance"},
    "T1590":    {"name": "Gather Victim Network Info",     "tactic": "Reconnaissance"},
    "T1593":    {"name": "Search Open Websites/Domains",   "tactic": "Reconnaissance"},
    "T1596":    {"name": "Search Open Technical DBs",      "tactic": "Reconnaissance"},
    # Lateral Movement
    "T1021":    {"name": "Remote Services",                "tactic": "Lateral Movement"},
    "T1570":    {"name": "Lateral Tool Transfer",          "tactic": "Lateral Movement"},
    # Collection
    "T1119":    {"name": "Automated Collection",           "tactic": "Collection"},
    "T1213":    {"name": "Data from Information Repos",    "tactic": "Collection"},
    # Command & Control
    "T1071":    {"name": "Application Layer Protocol",     "tactic": "Command and Control"},
    "T1071.001":{"name": "Web Protocols (C2)",             "tactic": "Command and Control"},
    "T1090":    {"name": "Proxy",                          "tactic": "Command and Control"},
    "T1102":    {"name": "Web Service (C2)",               "tactic": "Command and Control"},
    "T1568":    {"name": "Dynamic Resolution (DGA/DDNS)",  "tactic": "Command and Control"},
    "T1573":    {"name": "Encrypted Channel",              "tactic": "Command and Control"},
    "T1095":    {"name": "Non-Application Layer Protocol", "tactic": "Command and Control"},
    # Exfiltration
    "T1041":    {"name": "Exfil Over C2 Channel",          "tactic": "Exfiltration"},
    "T1567":    {"name": "Exfil Over Web Service",         "tactic": "Exfiltration"},
    # Impact
    "T1486":    {"name": "Data Encrypted for Impact",      "tactic": "Impact"},
    "T1485":    {"name": "Data Destruction",               "tactic": "Impact"},
    "T1490":    {"name": "Inhibit System Recovery",        "tactic": "Impact"},
    "T1498":    {"name": "Network Denial of Service",      "tactic": "Impact"},
    # Resource Development (often relevant to infrastructure investigations)
    "T1583":    {"name": "Acquire Infrastructure",         "tactic": "Resource Development"},
    "T1583.001":{"name": "Acquire Domains",                "tactic": "Resource Development"},
    "T1583.002":{"name": "Acquire DNS Server",             "tactic": "Resource Development"},
    "T1583.003":{"name": "Acquire VPS",                    "tactic": "Resource Development"},
    "T1584":    {"name": "Compromise Infrastructure",      "tactic": "Resource Development"},
    "T1585":    {"name": "Establish Accounts",             "tactic": "Resource Development"},
    "T1587":    {"name": "Develop Capabilities (Malware)", "tactic": "Resource Development"},
    "T1588":    {"name": "Obtain Capabilities",            "tactic": "Resource Development"},
}


@app.get("/mitre/techniques")
async def list_mitre_techniques(_user: dict = Depends(get_current_user)):
    """Return the curated MITRE ATT&CK technique catalogue."""
    by_tactic: dict[str, list] = {}
    for tid, meta in _MITRE_TECHNIQUES.items():
        tac = meta["tactic"]
        by_tactic.setdefault(tac, []).append({
            "id":     tid,
            "name":   meta["name"],
            "tactic": tac,
            "url":    f"https://attack.mitre.org/techniques/{tid.replace('.','/' )}/",
        })
    for tac in by_tactic:
        by_tactic[tac].sort(key=lambda t: t["id"])
    return {"by_tactic": by_tactic, "total": len(_MITRE_TECHNIQUES)}


class MitreTagRequest(BaseModel):
    technique_id: str   = Field(..., min_length=4, max_length=20)
    note:         str   = Field("", max_length=400)


@app.post("/entity/{entity_id}/mitre")
async def tag_entity_mitre(
    entity_id: str,
    req:       MitreTagRequest,
    _user:     dict = Depends(get_current_user),
):
    """Attach a MITRE ATT&CK technique to an entity."""
    tid = req.technique_id.strip().upper()
    if tid not in _MITRE_TECHNIQUES:
        raise HTTPException(400, f"Unknown MITRE technique: {tid}")
    meta = _MITRE_TECHNIQUES[tid]
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MATCH (e {id:$eid})
            MERGE (t:MitreTechnique {id:$tid})
            ON CREATE SET t.name=$name, t.tactic=$tactic, t.created_at=datetime()
            MERGE (e)-[r:USES_TECHNIQUE]->(t)
            ON CREATE SET r.note=$note, r.tagged_at=datetime(), r.tagged_by=$uid
            ON MATCH  SET r.note=$note, r.updated_at=datetime()
            """,
            eid=entity_id, tid=tid, name=meta["name"], tactic=meta["tactic"],
            note=req.note, uid=_user.get("sub", "unknown"),
        )
    _audit("MitreTag", entity_id, detail=f"{tid} ({meta['name']})")
    return {"entity_id": entity_id, "technique_id": tid, "name": meta["name"]}


@app.get("/entity/{entity_id}/mitre")
async def list_entity_mitre(
    entity_id: str,
    _user:     dict = Depends(get_current_user),
):
    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (e {id:$eid})-[r:USES_TECHNIQUE]->(t:MitreTechnique)
            RETURN t.id AS id, t.name AS name, t.tactic AS tactic,
                   r.note AS note, r.tagged_at AS tagged_at
            ORDER BY t.tactic, t.id
            """, eid=entity_id,
        )
        recs = await r.fetch(100)
    return {"entity_id": entity_id, "techniques": [dict(r) for r in recs]}


@app.delete("/entity/{entity_id}/mitre/{technique_id}")
async def untag_entity_mitre(
    entity_id:    str,
    technique_id: str,
    _user:        dict = Depends(get_current_user),
):
    tid = technique_id.strip().upper()
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MATCH (e {id:$eid})-[r:USES_TECHNIQUE]->(t:MitreTechnique {id:$tid})
            DELETE r
            """, eid=entity_id, tid=tid,
        )
    return {"removed": True}


@app.get("/case/{case_id}/mitre-coverage")
async def case_mitre_coverage(
    case_id: str,
    _user:   dict = Depends(get_current_user),
):
    """
    Return a tactic→technique coverage matrix for the case.
    Shows which ATT&CK techniques are observed across entities in the case.
    """
    cid = _val_case_id(case_id)
    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (c:Case {id:$cid})-[:HAS_ENTITY]->(e)-[:USES_TECHNIQUE]->(t:MitreTechnique)
            RETURN t.id AS tid, t.name AS name, t.tactic AS tactic,
                   count(DISTINCT e) AS entity_count,
                   collect(DISTINCT {id: e.id, label: labels(e)[0],
                                     name: coalesce(e.display_name, e.address,
                                                    e.domain, e.id)})[..10] AS entities
            ORDER BY tactic, tid
            """, cid=cid,
        )
        records = await r.fetch(200)

    by_tactic: dict[str, list] = {}
    for rec in records:
        tac = rec["tactic"] or "Unknown"
        by_tactic.setdefault(tac, []).append({
            "id":           rec["tid"],
            "name":         rec["name"],
            "entity_count": rec["entity_count"],
            "entities":     list(rec["entities"]),
        })
    return {
        "case_id":           cid,
        "by_tactic":         by_tactic,
        "tactics_covered":   len(by_tactic),
        "techniques_seen":   len(records),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph80 — Indicator Lifecycle / Aging Decay
# ─────────────────────────────────────────────────────────────────────────────

def _compute_freshness(props: dict) -> dict:
    """
    Compute an indicator freshness score (0-100) for a node.
    100 = brand new / recently enriched
      0 = very stale, no recent activity, never confirmed

    Decay model:
    - Base age decay: nodes lose 1 point per week since creation (max -52)
    - Last-enriched bonus: -1 point per week since last_enriched (max -26)
    - Confirmation bonus: +20 if node is part of a case
    - Hot signal bonus: +15 if recent feed_source or urlhaus_listed
    - High-VT bonus: +10 if vt_malicious > 5
    """
    score = 100
    now   = datetime.utcnow()
    signals: list[dict] = []

    def _add(label: str, pts: int):
        nonlocal score
        score += pts
        signals.append({"label": label, "points": pts})

    # Parse created_at
    created = props.get("created_at")
    if created:
        try:
            if hasattr(created, "to_native"):     # Neo4j DateTime
                created_dt = created.to_native()
                if hasattr(created_dt, "tzinfo") and created_dt.tzinfo is not None:
                    created_dt = created_dt.replace(tzinfo=None)
            elif isinstance(created, str):
                created_dt = datetime.fromisoformat(created.rstrip("Z"))
            else:
                created_dt = None
            if created_dt:
                weeks_old = max(0, (now - created_dt).days // 7)
                if weeks_old > 0:
                    _add(f"Age decay — {weeks_old} weeks old", -min(weeks_old, 52))
        except Exception:
            pass

    # Last enriched / fed
    last = props.get("last_enriched") or props.get("feed_last_seen")
    if last:
        try:
            if hasattr(last, "to_native"):
                last_dt = last.to_native()
                if hasattr(last_dt, "tzinfo") and last_dt.tzinfo is not None:
                    last_dt = last_dt.replace(tzinfo=None)
            elif isinstance(last, str):
                last_dt = datetime.fromisoformat(last.rstrip("Z"))
            else:
                last_dt = None
            if last_dt:
                weeks_since = max(0, (now - last_dt).days // 7)
                if weeks_since > 1:
                    _add(f"Stale enrichment — {weeks_since}w since last refresh", -min(weeks_since, 26))
        except Exception:
            pass

    # Hot signals
    if props.get("urlhaus_listed") or props.get("feed_source"):
        _add("Active threat-feed signal", 15)
    if int(props.get("vt_malicious", 0) or 0) > 5:
        _add("Strong VT detections", 10)
    if (props.get("gn_classification") or "").lower() == "malicious":
        _add("GreyNoise — currently malicious", 10)

    final = max(0, min(100, score))
    level = (
        "fresh"  if final >= 75 else
        "active" if final >= 50 else
        "aging"  if final >= 25 else
        "stale"
    )
    return {"freshness": final, "level": level, "signals": signals}


@app.get("/entity/{entity_id}/freshness")
async def entity_freshness(
    entity_id: str,
    _user:     dict = Depends(get_current_user),
):
    async with graph_db.driver.session() as session:
        r = await session.run(
            "MATCH (n {id:$eid}) RETURN properties(n) AS props, labels(n)[0] AS label",
            eid=entity_id,
        )
        rec = await r.single()
        if not rec:
            raise HTTPException(404, "Entity not found")
    return {
        "entity_id": entity_id,
        "label":     rec["label"],
        **_compute_freshness(dict(rec["props"])),
    }


@app.post("/lifecycle/decay-scan")
async def lifecycle_decay_scan(
    background: BackgroundTasks,
    threshold:  int  = Query(25, ge=0, le=100, description="Freshness ≤ this becomes 'stale'"),
    _user:      dict = Depends(get_current_user),
):
    """
    Scan all enrichable nodes (IP / Domain / Hash / URL), compute freshness,
    and mark nodes with freshness ≤ threshold as `stale=true` so the UI can
    visually de-emphasise them.  Runs as a background task on large graphs.
    """
    async def _run():
        scanned = 0
        marked  = 0
        async with graph_db.driver.session() as session:
            r = await session.run(
                """
                MATCH (n) WHERE n:IP OR n:Domain OR n:Hash OR n:URL
                RETURN n.id AS id, properties(n) AS props
                LIMIT 5000
                """
            )
            records = await r.fetch(5000)
            for rec in records:
                scanned += 1
                f = _compute_freshness(dict(rec["props"]))
                if f["freshness"] <= threshold:
                    marked += 1
                    await session.run(
                        """
                        MATCH (n {id:$id})
                        SET n.stale = true,
                            n.freshness = $f,
                            n.lifecycle_level = $lvl,
                            n.lifecycle_scanned_at = datetime()
                        """,
                        id=rec["id"], f=f["freshness"], lvl=f["level"],
                    )
                else:
                    await session.run(
                        """
                        MATCH (n {id:$id})
                        SET n.stale = false,
                            n.freshness = $f,
                            n.lifecycle_level = $lvl,
                            n.lifecycle_scanned_at = datetime()
                        """,
                        id=rec["id"], f=f["freshness"], lvl=f["level"],
                    )
            _audit("LifecycleDecayScan", "system", detail=f"scanned={scanned} marked_stale={marked}")

    background.add_task(_run)
    return {"status": "scan started in background", "threshold": threshold}


@app.get("/lifecycle/stats")
async def lifecycle_stats(_user: dict = Depends(get_current_user)):
    """Aggregate freshness distribution across all enrichable nodes."""
    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (n) WHERE n:IP OR n:Domain OR n:Hash OR n:URL
            RETURN labels(n)[0] AS label,
                   coalesce(n.lifecycle_level, 'unscanned') AS level,
                   count(*) AS count
            """
        )
        records = await r.fetch(100)

        scan_r = await session.run(
            "MATCH (n) WHERE n.lifecycle_scanned_at IS NOT NULL "
            "RETURN max(n.lifecycle_scanned_at) AS last_scan"
        )
        scan_rec = await scan_r.single()
        last_scan = str(scan_rec["last_scan"]) if scan_rec and scan_rec["last_scan"] else None

    by_label: dict[str, dict] = {}
    totals = {"fresh": 0, "active": 0, "aging": 0, "stale": 0, "unscanned": 0}
    for rec in records:
        lb  = rec["label"] or "Unknown"
        lvl = rec["level"]
        cnt = rec["count"]
        by_label.setdefault(lb, {"fresh":0,"active":0,"aging":0,"stale":0,"unscanned":0})
        by_label[lb][lvl] = cnt
        if lvl in totals:
            totals[lvl] += cnt
    return {
        "totals":    totals,
        "by_label":  by_label,
        "last_scan": last_scan,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph81 — Investigation Templates
# ─────────────────────────────────────────────────────────────────────────────

_INVESTIGATION_TEMPLATES: dict[str, dict] = {
    "phishing": {
        "id":          "phishing",
        "icon":        "🎣",
        "name":        "Phishing Campaign",
        "title_pfx":   "[Phishing] ",
        "description":
            "Investigation template for a suspected phishing campaign. "
            "Pre-populates tasks for header analysis, link unpacking, infrastructure pivoting, and victimology.",
        "tasks": [
            "Capture raw email headers + EML and import to graph",
            "Defang and tokenise all URLs / IPs / domains from the body",
            "Resolve all linked domains → IPs; RDAP + crt.sh on each domain",
            "URLScan + URLhaus on each landing URL",
            "GreyNoise + AbuseIPDB on each linked IP",
            "Identify shared infrastructure (ASN, /24, registrar, SSL cert SAN)",
            "Cross-reference sender display name / domain with prior cases",
            "Document targeted victim group (industry, geography, role)",
        ],
        "hypotheses": [
            "Campaign is sourced from a single threat actor / kit",
            "Infrastructure overlaps with a previously-tracked campaign",
        ],
        "mitre":       ["T1566", "T1566.001", "T1566.002", "T1583.001", "T1071.001"],
        "tag":         "phishing-investigation",
    },
    "ransomware": {
        "id":          "ransomware",
        "icon":        "🔒",
        "name":        "Ransomware Response",
        "title_pfx":   "[Ransomware] ",
        "description":
            "Response template for a ransomware incident. Covers IOC triage, "
            "ransom note attribution, infrastructure mapping, and timeline reconstruction.",
        "tasks": [
            "Collect ransom note + filenames; identify ransomware family",
            "Hash + sandbox any retrievable executables (MalwareBazaar)",
            "Map C2 infrastructure — Feodo Tracker, ThreatFox lookups",
            "Extract Tor / clearnet payment URLs; check for prior victim listings",
            "Identify initial access vector (RDP, VPN, phishing, supply chain)",
            "Build the kill-chain timeline from logs to data exfil",
            "Map MITRE ATT&CK techniques against observed TTPs",
            "Determine data-exfiltration status (double extortion?)",
        ],
        "hypotheses": [
            "Initial access was via an unpatched edge device",
            "Operator is an affiliate of a known RaaS programme",
        ],
        "mitre":       ["T1190", "T1133", "T1486", "T1490", "T1041", "T1567"],
        "tag":         "ransomware-response",
    },
    "domain_compromise": {
        "id":          "domain_compromise",
        "icon":        "🌐",
        "name":        "Domain Compromise / Defacement",
        "title_pfx":   "[Domain] ",
        "description":
            "Template for investigating compromise or defacement of a specific domain — "
            "DNS hijack, account takeover, sub-domain takeover, or registrar compromise.",
        "tasks": [
            "Snapshot current WHOIS / RDAP for the affected domain",
            "Pull passive DNS history to spot unexpected resolution changes",
            "Crt.sh — look for surprise SSL cert issuances in last 90 days",
            "Wayback Machine snapshot to compare prior vs current content",
            "Check all sub-domains for stale CNAME records (takeover vectors)",
            "Validate registrar account integrity (2FA, transfer-lock, EPP)",
            "Identify any nameservers introduced recently",
        ],
        "hypotheses": [
            "Registrar account credentials were compromised",
            "Sub-domain takeover via dangling DNS record",
        ],
        "mitre":       ["T1584", "T1583.001", "T1098"],
        "tag":         "domain-compromise",
    },
    "threat_actor": {
        "id":          "threat_actor",
        "icon":        "👤",
        "name":        "Threat Actor Profile",
        "title_pfx":   "[Actor] ",
        "description":
            "Build a structured profile for a threat actor or persona — alias mapping, "
            "infrastructure history, TTPs, and victimology.",
        "tasks": [
            "Enumerate all known aliases / handles / personas",
            "Username harvest across social/dev/dark-web platforms",
            "Map historical infrastructure (domains, ASNs, hosting providers)",
            "Catalogue known TTPs and map to MITRE ATT&CK",
            "Document attribution evidence and confidence assessment",
            "List victim industries / geographies / sectors",
            "Identify any links to known groups / RaaS programmes",
            "Maintain timeline of activity bursts",
        ],
        "hypotheses": [
            "Actor is a sub-cluster of a larger known group",
            "Operator uses a consistent OpSec pattern (timezone, language)",
        ],
        "mitre":       ["T1583", "T1585", "T1587", "T1588", "T1589"],
        "tag":         "threat-actor-profile",
    },
    "insider": {
        "id":          "insider",
        "icon":        "🕵",
        "name":        "Insider Threat",
        "title_pfx":   "[Insider] ",
        "description":
            "Investigation template for a suspected malicious or negligent insider — "
            "focus on data-exfil, lateral movement, and behavioural anomalies.",
        "tasks": [
            "Build the subject's normal access baseline",
            "Pull all anomalous outbound transfers (size, destination, time)",
            "Check for use of personal email, cloud storage, removable media",
            "Map any newly-created accounts attributed to subject",
            "Review any communications with external entities",
            "Search dark-web / paste sites for leaked credentials or data",
            "Document chain-of-custody for all evidence collected",
        ],
        "hypotheses": [
            "Subject is exfiltrating data prior to a planned departure",
            "Subject has been recruited by an external actor",
        ],
        "mitre":       ["T1078", "T1213", "T1567", "T1119", "T1098"],
        "tag":         "insider-threat",
    },
}


@app.get("/templates")
async def list_templates(_user: dict = Depends(get_current_user)):
    """List all available investigation templates."""
    return {
        "templates": [
            {
                "id":          t["id"],
                "icon":        t["icon"],
                "name":        t["name"],
                "description": t["description"],
                "task_count":  len(t["tasks"]),
                "hyp_count":   len(t["hypotheses"]),
                "mitre_count": len(t["mitre"]),
            }
            for t in _INVESTIGATION_TEMPLATES.values()
        ]
    }


class TemplateApplyRequest(BaseModel):
    title:    Optional[str] = Field(None, max_length=200)
    case_id:  Optional[str] = Field(None, max_length=200)


@app.post("/templates/{template_id}/apply")
async def apply_template(
    template_id: str,
    req:         TemplateApplyRequest,
    user:        dict = Depends(get_current_user),
):
    """
    Apply a template either to an existing case (case_id provided) or by
    creating a brand-new case (title provided).
    Adds: tasks, hypothesis stubs, and the template's tag to the case.
    """
    if template_id not in _INVESTIGATION_TEMPLATES:
        raise HTTPException(404, f"Unknown template: {template_id}")
    tmpl = _INVESTIGATION_TEMPLATES[template_id]

    # Resolve target case
    if req.case_id:
        cid = _val_case_id(req.case_id)
        async with graph_db.driver.session() as session:
            r = await session.run("MATCH (c:Case {id:$id}) RETURN c.title AS t", id=cid)
            rec = await r.single()
            if not rec:
                raise HTTPException(404, "Case not found")
            case_title = rec["t"]
        created_case = None
    else:
        title = (req.title or tmpl["title_pfx"] + "New investigation").strip()
        new_case = await create_case(
            graph_db, title, tmpl["description"],
            "active", "medium", owner_id=user["sub"],
        )
        cid = new_case["id"]
        case_title = new_case["title"]
        created_case = new_case

    # Add template tasks
    tasks_added = 0
    for task_text in tmpl["tasks"]:
        try:
            await add_task(graph_db, cid, task_text)
            tasks_added += 1
        except Exception as exc:
            log.warning("template task add failed: %s", exc)

    # Add hypothesis stubs
    hyps_added = 0
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    async with graph_db.driver.session() as session:
        for claim in tmpl["hypotheses"]:
            try:
                hyp_id = str(uuid.uuid4())
                await session.run(
                    """
                    MATCH (c:Case {id: $cid})
                    CREATE (h:Hypothesis {
                        id: $id, case_id: $cid,
                        claim: $claim, status: 'open', confidence: 'medium',
                        evidence_for: '[]', evidence_against: '[]',
                        created_at: $now, updated_at: $now, created_by: $user
                    })
                    CREATE (c)-[:HAS_HYPOTHESIS]->(h)
                    """,
                    cid=cid, id=hyp_id, claim=claim, now=now,
                    user=user.get("username", "template"),
                )
                hyps_added += 1
            except Exception as exc:
                log.warning("template hypothesis add failed: %s", exc)

        # Attach the template tag + MITRE technique stubs at case level
        await session.run(
            """
            MATCH (c:Case {id: $cid})
            SET c.template = $tmpl, c.tags = coalesce(c.tags, []) + [$tag]
            """,
            cid=cid, tmpl=tmpl["id"], tag=tmpl["tag"],
        )

    _audit("TemplateApply", cid,
           detail=f"template={template_id} tasks={tasks_added} hyps={hyps_added}")
    return {
        "case_id":       cid,
        "case_title":    case_title,
        "template":      template_id,
        "tasks_added":   tasks_added,
        "hypotheses_added": hyps_added,
        "mitre_suggestions": tmpl["mitre"],
        "created_case":  created_case,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph82 — Database Backup & Restore
# ─────────────────────────────────────────────────────────────────────────────

import pathlib as _pl

_BACKUP_DIR = _pl.Path(os.getenv("FW_BACKUP_DIR", "/data/backups"))
try:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
except Exception as exc:
    log.warning("Could not create backup dir %s: %s", _BACKUP_DIR, exc)


def _safe_backup_filename(name: str) -> str:
    """Restrict filename to safe characters and force .json suffix."""
    cleaned = re.sub(r'[^A-Za-z0-9_\-.]', '_', name)
    if not cleaned.endswith(".json"):
        cleaned += ".json"
    return cleaned


@app.post("/admin/backup")
async def create_backup(
    name:  Optional[str] = Query(None, max_length=80),
    _user: dict = Depends(get_current_user),
):
    """
    Dump the full graph (nodes + edges with all properties) to a JSON file
    in the backup directory. Skips Neo4j-system internals.
    """
    import json as _json

    fname = _safe_backup_filename(
        name or f"fieldwork-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    )
    path = _BACKUP_DIR / fname

    async with graph_db.driver.session() as session:
        # Nodes
        n_result = await session.run(
            """
            MATCH (n)
            WHERE n.id IS NOT NULL AND NOT n:AuditLog
            RETURN n.id AS id, labels(n) AS labels, properties(n) AS props
            """
        )
        node_records = await n_result.fetch(50_000)

        # Edges
        e_result = await session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE a.id IS NOT NULL AND b.id IS NOT NULL
            RETURN a.id AS src, b.id AS dst, type(r) AS type, properties(r) AS props
            """
        )
        edge_records = await e_result.fetch(200_000)

    def _serialize(v):
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        if isinstance(v, (list, tuple)):
            return [_serialize(x) for x in v]
        if isinstance(v, dict):
            return {k: _serialize(vv) for k, vv in v.items()}
        return str(v)

    nodes = [
        {"id": r["id"], "labels": list(r["labels"]), "props": _serialize(dict(r["props"]))}
        for r in node_records
    ]
    edges = [
        {"src": r["src"], "dst": r["dst"], "type": r["type"],
         "props": _serialize(dict(r["props"]))}
        for r in edge_records
    ]

    payload = {
        "format":      "fieldwork-backup-v1",
        "created_at":  datetime.utcnow().isoformat() + "Z",
        "node_count":  len(nodes),
        "edge_count":  len(edges),
        "nodes":       nodes,
        "edges":       edges,
    }

    try:
        path.write_text(_json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        raise HTTPException(500, f"Backup write failed: {exc}")

    size_kb = path.stat().st_size // 1024
    _audit("BackupCreate", fname, detail=f"nodes={len(nodes)} edges={len(edges)} size={size_kb}KB")
    return {
        "filename":   fname,
        "path":       str(path),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "size_kb":    size_kb,
        "created_at": payload["created_at"],
    }


@app.get("/admin/backups")
async def list_backups(_user: dict = Depends(get_current_user)):
    """List backup files in the backup directory."""
    if not _BACKUP_DIR.exists():
        return {"backups": [], "dir": str(_BACKUP_DIR)}
    files = []
    for p in sorted(_BACKUP_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
            files.append({
                "filename":   p.name,
                "size_kb":    st.st_size // 1024,
                "modified":   datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z",
            })
        except Exception:
            continue
    return {"backups": files[:50], "dir": str(_BACKUP_DIR)}


@app.delete("/admin/backups/{filename}")
async def delete_backup(
    filename: str,
    _user:    dict = Depends(get_current_user),
):
    fname = _safe_backup_filename(filename)
    path  = _BACKUP_DIR / fname
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Backup file not found")
    try:
        path.unlink()
    except Exception as exc:
        raise HTTPException(500, f"Delete failed: {exc}")
    return {"deleted": fname}


@app.get("/admin/backups/{filename}/download")
async def download_backup(
    filename: str,
    _user:    dict = Depends(get_current_user),
):
    fname = _safe_backup_filename(filename)
    path  = _BACKUP_DIR / fname
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Backup file not found")
    return Response(
        content=path.read_bytes(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


class RestoreRequest(BaseModel):
    filename:           str  = Field(..., max_length=120)
    merge_mode:         bool = Field(True, description="If False, wipes graph first")
    confirm_wipe_token: str  = Field("",   description="Required if merge_mode=False")


@app.post("/admin/restore")
async def restore_backup(
    req:        RestoreRequest,
    background: BackgroundTasks,
    _user:      dict = Depends(get_current_user),
):
    """
    Restore a backup file.
    merge_mode=True (default): upserts every node/edge via MERGE (safe — no data loss).
    merge_mode=False: WIPES the existing graph first. Requires confirm_wipe_token='WIPE'.
    Runs in background for large files.
    """
    import json as _json

    fname = _safe_backup_filename(req.filename)
    path  = _BACKUP_DIR / fname
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Backup file not found")

    if not req.merge_mode and req.confirm_wipe_token != "WIPE":
        raise HTTPException(400, "Wipe restore requires confirm_wipe_token='WIPE'")

    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(400, f"Backup file is not valid JSON: {exc}")

    if payload.get("format") != "fieldwork-backup-v1":
        raise HTTPException(400, "Backup format unrecognised")

    nodes = payload.get("nodes", [])
    edges = payload.get("edges", [])

    async def _do_restore():
        async with graph_db.driver.session() as session:
            if not req.merge_mode:
                # Wipe non-system nodes (preserve AuditLog and User as safety)
                await session.run(
                    "MATCH (n) WHERE NOT n:AuditLog AND NOT n:User DETACH DELETE n"
                )
            # Restore nodes
            for node in nodes:
                nid    = node.get("id")
                labels = [lb for lb in (node.get("labels") or []) if lb.replace("_", "").isalnum()]
                props  = node.get("props", {}) or {}
                if not nid or not labels:
                    continue
                lbl_str = ":".join(labels)
                try:
                    await session.run(
                        f"MERGE (n:{lbl_str} {{id: $id}}) SET n += $props",
                        id=nid, props=props,
                    )
                except Exception as exc:
                    log.warning("restore node %s failed: %s", nid, exc)
            # Restore edges
            for edge in edges:
                src   = edge.get("src")
                dst   = edge.get("dst")
                etype = edge.get("type")
                props = edge.get("props", {}) or {}
                if not src or not dst or not etype:
                    continue
                if not etype.replace("_", "").isalnum():
                    continue
                try:
                    await session.run(
                        f"""
                        MATCH (a {{id:$src}}), (b {{id:$dst}})
                        MERGE (a)-[r:{etype}]->(b)
                        SET r += $props
                        """,
                        src=src, dst=dst, props=props,
                    )
                except Exception as exc:
                    log.warning("restore edge %s→%s failed: %s", src, dst, exc)
            log.info("Backup restore complete: %d nodes, %d edges", len(nodes), len(edges))

    background.add_task(_do_restore)
    _audit("BackupRestore", fname, detail=f"mode={'merge' if req.merge_mode else 'wipe'}")
    return {
        "status":     "restore queued",
        "filename":   fname,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "merge_mode": req.merge_mode,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph83 — Investigation Workflow Playbooks
# ─────────────────────────────────────────────────────────────────────────────

_PLAYBOOKS: dict[str, dict] = {
    "domain_triage": {
        "id":      "domain_triage",
        "icon":    "🌐",
        "name":    "Initial Domain Triage",
        "applies_to": ["Domain"],
        "description": "Standard first-look workflow for a suspicious domain.",
        "steps": [
            {"key": "rdap",       "name": "RDAP / WHOIS lookup",
             "action": "enrichDomain('rdap')", "hint": "Registrar, age, registrant"},
            {"key": "pdns",       "name": "Passive DNS history",
             "action": "enrichDomainPassiveDNS()", "hint": "Sub-domains, IP history"},
            {"key": "certs",      "name": "Certificate transparency (crt.sh)",
             "action": "enrichDomainCerts()", "hint": "Sister domains via SAN"},
            {"key": "urlhaus",    "name": "URLhaus malware check",
             "action": "enrichDomainURLhaus()", "hint": "Known-bad listing"},
            {"key": "vt",         "name": "VirusTotal verdict",
             "action": "enrichDomain('vt')", "hint": "Vendor detection ratio"},
            {"key": "urlscan",    "name": "URLScan.io recent scans",
             "action": "enrichDomainURLScan()", "hint": "Page content & tech"},
            {"key": "timeline",   "name": "Build pDNS timeline",
             "action": "showPDNSTimeline()", "hint": "Visualise resolution history"},
            {"key": "pivot",      "name": "Network pivot from primary IP",
             "action": "runNetworkPivot()", "hint": "ASN siblings, co-hosted"},
        ],
    },
    "ip_triage": {
        "id":      "ip_triage",
        "icon":    "🔌",
        "name":    "Suspicious IP Investigation",
        "applies_to": ["IP"],
        "description": "Comprehensive IP investigation workflow.",
        "steps": [
            {"key": "asn",        "name": "ASN / BGP lookup",
             "action": "runASN()", "hint": "ISP + AS number"},
            {"key": "greynoise",  "name": "GreyNoise classification",
             "action": "runGreyNoise()", "hint": "Scanner / benign / malicious"},
            {"key": "internetdb", "name": "Shodan InternetDB (free)",
             "action": "runInternetDB()", "hint": "Open ports + CVEs"},
            {"key": "shodan",     "name": "Shodan full host detail",
             "action": "runShodanFull()", "hint": "Banners, SSL, services"},
            {"key": "vt",         "name": "VirusTotal verdict",
             "action": "runVT()", "hint": "Detection count"},
            {"key": "abuseipdb",  "name": "AbuseIPDB abuse score",
             "action": "runAbuseIPDB()", "hint": "Reported abuses"},
            {"key": "urlhaus",    "name": "URLhaus malware-hosting check",
             "action": "runURLhaus()", "hint": "Active malware hosting"},
            {"key": "rdns",       "name": "Reverse DNS / co-hosted",
             "action": "runRDNS()", "hint": "Other hostnames"},
            {"key": "pivot",      "name": "Network pivot (/24 + ASN)",
             "action": "runNetworkPivot()", "hint": "Subnet neighbours"},
        ],
    },
    "hash_triage": {
        "id":      "hash_triage",
        "icon":    "🔢",
        "name":    "Malware Hash Analysis",
        "applies_to": ["Hash"],
        "description": "Workflow for triaging an unknown file hash.",
        "steps": [
            {"key": "mb",       "name": "MalwareBazaar lookup",
             "action": "lookupMalwareBazaar()", "hint": "Family, file type, tags"},
            {"key": "vt",       "name": "VirusTotal scan",
             "action": "lookupVTHash()",   "hint": "Vendor detection summary"},
            {"key": "tf",       "name": "ThreatFox indicator search",
             "action": "lookupThreatFox()", "hint": "Linked C2 / actor"},
            {"key": "yara",     "name": "Run any in-house YARA rules",
             "action": "",             "hint": "Manual — out-of-band"},
            {"key": "sandbox",  "name": "Detonate in sandbox",
             "action": "",             "hint": "Manual — Cuckoo / ANY.RUN / Joe"},
        ],
    },
    "phishing_triage": {
        "id":      "phishing_triage",
        "icon":    "📧",
        "name":    "Phishing Email Triage",
        "applies_to": ["Email", "URL", "Domain"],
        "description": "Quick triage workflow for a single phishing email.",
        "steps": [
            {"key": "headers",   "name": "Parse email headers",
             "action": "switchToolsTab('email-headers')", "hint": "SPF / DKIM / DMARC"},
            {"key": "extract",   "name": "Extract IOCs (Smart Paste)",
             "action": "switchToolsTab('smart-paste')", "hint": "Auto IOC extraction"},
            {"key": "defang",    "name": "Defang for safe sharing",
             "action": "switchToolsTab('defang')", "hint": "Safe IOC formatting"},
            {"key": "url_check", "name": "URLhaus + URLScan on every link",
             "action": "", "hint": "Per-URL enrichment"},
            {"key": "dom_check", "name": "RDAP on every sender / link domain",
             "action": "", "hint": "Domain age + registrar"},
            {"key": "hibp",      "name": "HIBP on sender + target addresses",
             "action": "", "hint": "Known breaches"},
        ],
    },
}


@app.get("/playbooks")
async def list_playbooks(
    label: Optional[str] = Query(None, description="Filter to playbooks applicable to this entity label"),
    _user: dict = Depends(get_current_user),
):
    """List available investigation playbooks."""
    out = []
    for pb in _PLAYBOOKS.values():
        if label and label not in pb["applies_to"]:
            continue
        out.append({
            "id":          pb["id"],
            "icon":        pb["icon"],
            "name":        pb["name"],
            "description": pb["description"],
            "applies_to":  pb["applies_to"],
            "step_count":  len(pb["steps"]),
        })
    return {"playbooks": out}


@app.get("/playbooks/{playbook_id}")
async def get_playbook(
    playbook_id: str,
    entity_id:   Optional[str] = Query(None),
    _user:       dict = Depends(get_current_user),
):
    """
    Get a playbook's full step list, and (if entity_id given) per-step
    completion status by checking the entity's stored playbook progress.
    """
    if playbook_id not in _PLAYBOOKS:
        raise HTTPException(404, "Playbook not found")
    pb = _PLAYBOOKS[playbook_id]

    progress: dict[str, bool] = {}
    if entity_id:
        async with graph_db.driver.session() as session:
            r = await session.run(
                "MATCH (n {id:$eid}) RETURN n.playbook_progress AS p",
                eid=entity_id,
            )
            rec = await r.single()
            if rec and rec["p"]:
                import json as _j
                try:
                    progress = _j.loads(rec["p"])
                except Exception:
                    progress = {}

    steps_out = []
    for step in pb["steps"]:
        steps_out.append({**step, "done": bool(progress.get(f"{playbook_id}:{step['key']}", False))})
    done = sum(1 for s in steps_out if s["done"])
    return {
        "id":          pb["id"],
        "icon":        pb["icon"],
        "name":        pb["name"],
        "description": pb["description"],
        "applies_to":  pb["applies_to"],
        "steps":       steps_out,
        "done":        done,
        "total":       len(steps_out),
        "pct":         round(done / len(steps_out) * 100) if steps_out else 0,
    }


@app.get("/entity/{entity_id}/playbook-summary")
async def entity_playbook_summary(
    entity_id: str,
    _user:     dict = Depends(get_current_user),
):
    """
    Single-call summary: returns the playbook with the most progress for
    this entity, or null if no playbook has been started.
    """
    import json as _j
    async with graph_db.driver.session() as session:
        r = await session.run(
            "MATCH (n {id:$eid}) RETURN n.playbook_progress AS p",
            eid=entity_id,
        )
        rec = await r.single()
        if not rec or not rec["p"]:
            return {"active": None}
        try:
            prog = _j.loads(rec["p"])
        except Exception:
            return {"active": None}

    # Count completed steps per playbook_id
    counts: dict[str, int] = {}
    for key, done in prog.items():
        if not done:
            continue
        pb_id = key.split(":", 1)[0]
        counts[pb_id] = counts.get(pb_id, 0) + 1
    if not counts:
        return {"active": None}

    best_id = max(counts, key=counts.get)
    if best_id not in _PLAYBOOKS:
        return {"active": None}
    pb = _PLAYBOOKS[best_id]
    total = len(pb["steps"])
    done  = counts[best_id]
    return {
        "active": {
            "id":    pb["id"],
            "icon":  pb["icon"],
            "name":  pb["name"],
            "done":  done,
            "total": total,
            "pct":   round(done / total * 100) if total else 0,
        }
    }


class PlaybookStepToggle(BaseModel):
    entity_id:   str  = Field(..., min_length=1)
    playbook_id: str  = Field(..., min_length=1)
    step_key:    str  = Field(..., min_length=1)
    done:        bool


@app.post("/playbooks/step-toggle")
async def toggle_playbook_step(
    req:   PlaybookStepToggle,
    _user: dict = Depends(get_current_user),
):
    """Mark a playbook step as done / not-done for a specific entity."""
    if req.playbook_id not in _PLAYBOOKS:
        raise HTTPException(404, "Unknown playbook")
    import json as _j

    async with graph_db.driver.session() as session:
        r = await session.run(
            "MATCH (n {id:$eid}) RETURN n.playbook_progress AS p",
            eid=req.entity_id,
        )
        rec = await r.single()
        if not rec:
            raise HTTPException(404, "Entity not found")
        try:
            prog = _j.loads(rec["p"]) if rec["p"] else {}
        except Exception:
            prog = {}
        prog[f"{req.playbook_id}:{req.step_key}"] = req.done
        await session.run(
            "MATCH (n {id:$eid}) SET n.playbook_progress = $p",
            eid=req.entity_id, p=_j.dumps(prog),
        )
    return {"status": "ok", "progress_keys": len(prog)}


# ─────────────────────────────────────────────────────────────────────────────
# Ph84 — Document IOC Extractor
# ─────────────────────────────────────────────────────────────────────────────

def _extract_text_from_pdf(raw: bytes) -> str:
    """Extract text from PDF bytes — tries pypdf, then pdfminer.six, else fails."""
    try:
        from pypdf import PdfReader
        import io as _io
        reader = PdfReader(_io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        pass
    try:
        from pdfminer.high_level import extract_text
        import io as _io
        return extract_text(_io.BytesIO(raw))
    except ImportError:
        pass
    raise HTTPException(
        501,
        "PDF parsing not available — install pypdf or pdfminer.six in the backend container"
    )


def _extract_text_from_docx(raw: bytes) -> str:
    """Extract text from .docx bytes — tries python-docx."""
    try:
        from docx import Document
        import io as _io
        doc = Document(_io.BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs if p.text)
    except ImportError:
        raise HTTPException(501, "DOCX parsing not available — install python-docx in the backend container")


def _extract_text_from_eml(raw: bytes) -> str:
    """Extract text from .eml bytes — stdlib only."""
    import email as _email
    from email import policy as _epolicy
    try:
        msg = _email.message_from_bytes(raw, policy=_epolicy.default)
        parts: list[str] = []
        # Headers worth keeping
        for hdr in ("From", "To", "Subject", "Date", "Reply-To", "Return-Path",
                    "Received-SPF", "Authentication-Results"):
            val = msg.get(hdr)
            if val:
                parts.append(f"{hdr}: {val}")
        # Body
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    try:
                        parts.append(part.get_content())
                    except Exception:
                        try:
                            parts.append(part.get_payload(decode=True).decode("utf-8", errors="replace"))
                        except Exception:
                            pass
        else:
            try:
                parts.append(msg.get_content())
            except Exception:
                parts.append(str(msg.get_payload()))
        return "\n\n".join(parts)
    except Exception as exc:
        raise HTTPException(400, f"EML parse failed: {exc}")


@app.post("/util/extract-iocs-from-file")
async def extract_iocs_from_file(
    file:  UploadFile = File(...),
    _user: dict = Depends(get_current_user),
):
    """
    Upload a document (.pdf, .docx, .eml, .txt, .csv, .md, .json) and get
    classified IOCs back. Reuses the same _classify_ioc() pipeline as
    /smart-paste so behaviour is consistent.
    """
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large — 10 MB max")

    name = (file.filename or "upload").lower()
    ext  = name.rsplit(".", 1)[-1] if "." in name else ""

    # Extract text per type
    if ext == "pdf" or (file.content_type or "").endswith("pdf"):
        text = _extract_text_from_pdf(raw)
    elif ext == "docx":
        text = _extract_text_from_docx(raw)
    elif ext == "eml":
        text = _extract_text_from_eml(raw)
    elif ext in ("txt", "csv", "md", "json", "log", "yaml", "yml"):
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
    else:
        # Best-effort: try utf-8 decode
        try:
            text = raw.decode("utf-8")
        except Exception:
            raise HTTPException(415, f"Unsupported file type: .{ext}")

    if not text or not text.strip():
        return {
            "filename": file.filename,
            "extracted_chars": 0,
            "iocs": [],
            "counts": {},
        }

    # IOC extraction — same approach as /smart-paste
    import re as _re84
    found: dict[str, dict] = {}

    # URLs first (greedy)
    url_pat = _re84.compile(r'https?://[^\s<>"\'\)]+', _re84.I)
    for m in url_pat.finditer(text):
        v = m.group().rstrip('.,;)')
        found[v.lower()] = {"value": v, "label": "URL", "subtype": "url"}
    clean = url_pat.sub(' ', text)

    for tok in _re84.split(r'[\s,;|<>\[\]()"\'\t\n\r]', clean):
        tok = tok.strip().rstrip('.,;:!?>')
        if len(tok) < 4:
            continue
        # Strip "defang" wrappers like [.]  inside the token before classifying
        candidate = tok.replace("[.]", ".").replace("[.", ".").replace(".]", ".")
        label, subtype = _classify_ioc(candidate)
        if label == "Unknown":
            continue
        key = candidate.lower()
        if key not in found:
            found[key] = {"value": candidate, "label": label, "subtype": subtype}

    iocs = list(found.values())
    counts: dict[str, int] = {}
    for i in iocs:
        counts[i["label"]] = counts.get(i["label"], 0) + 1

    _audit("DocExtract", file.filename or "upload",
           detail=f"chars={len(text)} iocs={len(iocs)}")
    return {
        "filename":        file.filename,
        "file_type":       ext,
        "extracted_chars": len(text),
        "iocs":            iocs,
        "counts":          counts,
        "preview":         text[:1500],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph85 — Username Variation Generator
# ─────────────────────────────────────────────────────────────────────────────

class UsernameVariationsRequest(BaseModel):
    name:        str       = Field(..., min_length=2, max_length=120)
    birth_year:  Optional[int] = Field(None, ge=1900, le=2100)
    extras:      List[str] = Field(default_factory=list, description="Custom snippets to append")
    leetspeak:   bool      = Field(True, description="Include l33t-speak substitutions")
    include_nums: bool     = Field(True, description="Append common number suffixes")


# Common number suffixes used in real-world usernames
_COMMON_SUFFIXES = ["1", "2", "7", "21", "23", "42", "69", "99", "100", "123", "777", "1234"]
# Common separators used between name parts
_SEPARATORS      = ["", ".", "_", "-"]
# Leet substitutions
_LEET_MAP = {
    "a": "4", "b": "8", "e": "3", "g": "9",
    "i": "1", "l": "1", "o": "0", "s": "5",
    "t": "7", "z": "2",
}


def _leetify(s: str) -> list[str]:
    """Produce a few common leet variants of a string (not the combinatorial explosion)."""
    out = {s}
    # full substitution
    full = "".join(_LEET_MAP.get(c, c) for c in s.lower())
    if full != s.lower():
        out.add(full)
    # single-substitution variants
    for orig, repl in _LEET_MAP.items():
        if orig in s.lower():
            out.add(s.lower().replace(orig, repl, 1))
    return list(out)


@app.post("/util/username-variations")
async def username_variations(
    req:   UsernameVariationsRequest,
    _user: dict = Depends(get_current_user),
):
    """
    Generate likely username variants from a real name. Useful for
    cross-platform username harvesting (Sherlock, Maigret) and sock-puppet hunting.

    Strategy:
    - Split name into parts (first / middle / last)
    - Combine with separators: first.last, first_last, firstlast, flast, firstl, lastfirst, etc.
    - Suffix with birth year, common numbers, and analyst-supplied extras
    - Optionally apply l33t-speak substitutions
    """
    raw = req.name.strip()
    if not raw:
        raise HTTPException(400, "name required")

    # Lowercase + ASCII-fold; strip non-word
    import unicodedata as _ud
    normalised = _ud.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    parts = [p for p in re.split(r"[\s\-']+", normalised.lower()) if p and p.isalnum()]
    if not parts:
        raise HTTPException(400, "no usable name parts after normalisation")

    first = parts[0]
    last  = parts[-1] if len(parts) > 1 else ""
    middle = parts[1:-1] if len(parts) > 2 else []

    bases: set[str] = set()

    # Single-name forms
    bases.update({first, last, first[0]+last if last else first})

    # First+last combos with separators
    if last:
        for sep in _SEPARATORS:
            bases.add(f"{first}{sep}{last}")
            bases.add(f"{last}{sep}{first}")
            bases.add(f"{first[0]}{sep}{last}")     # j.doe
            bases.add(f"{first}{sep}{last[0]}")     # jane.d
            bases.add(f"{last}{sep}{first[0]}")     # doe.j
            bases.add(f"{last[0]}{sep}{first}")     # d.jane

    # Middle-name forms
    if last and middle:
        m = middle[0]
        for sep in _SEPARATORS:
            bases.add(f"{first}{sep}{m[0]}{sep}{last}")  # jane.r.doe / janerdoe
            bases.add(f"{first}{m[0]}{last}")            # janerdoe (no sep)

    # Filter empty / too-short
    bases = {b for b in bases if 3 <= len(b) <= 30}

    # Number suffixes
    variants: set[str] = set(bases)
    if req.include_nums:
        for b in list(bases):
            for sfx in _COMMON_SUFFIXES:
                variants.add(f"{b}{sfx}")
        if req.birth_year:
            yr  = str(req.birth_year)
            yr2 = yr[-2:]
            for b in list(bases):
                variants.add(f"{b}{yr}")
                variants.add(f"{b}{yr2}")
                variants.add(f"{b}_{yr}")

    # Extras
    for extra in (req.extras or []):
        extra_clean = re.sub(r"[^A-Za-z0-9_\-.]", "", extra).lower()
        if not extra_clean:
            continue
        for b in list(bases):
            variants.add(f"{b}{extra_clean}")
            variants.add(f"{b}_{extra_clean}")
            variants.add(f"{extra_clean}{b}")

    # Leetspeak
    if req.leetspeak:
        leet_out: set[str] = set()
        for v in list(variants)[:50]:   # cap leet expansion source
            leet_out.update(_leetify(v))
        variants |= leet_out

    # Sort: short + alphabetical first
    sorted_variants = sorted(variants, key=lambda v: (len(v), v))

    return {
        "input_name":     raw,
        "parsed":         {"first": first, "middle": middle, "last": last},
        "variant_count":  len(sorted_variants),
        "variants":       sorted_variants[:300],
        "harvest_url":    "/api/harvest/username",  # frontend hint
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph86 — Rich Webhook Formatters (Slack / Discord / Teams)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_webhook_format(url: str) -> str:
    """Auto-detect the webhook target by hostname."""
    u = (url or "").lower()
    if "hooks.slack.com" in u:
        return "slack"
    if "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
        return "discord"
    if ".webhook.office.com" in u or "outlook.office.com/webhook" in u:
        return "teams"
    return "raw"


def _format_webhook_payload(event_type: str, payload: dict, fmt: str) -> dict:
    """Translate a generic event payload to the appropriate webhook schema."""
    title    = payload.get("title")   or f"Fieldwork OSINT — {event_type}"
    message  = payload.get("message") or payload.get("detail") or ""
    url      = payload.get("url")     or ""
    severity = (payload.get("severity") or "info").lower()
    icon     = {
        "alert":       "🚨",
        "feed_sync":   "📡",
        "risk_change": "⚠️",
        "case.create": "📋",
        "entity.add":  "➕",
        "test":        "🔔",
    }.get(event_type, "ℹ️")

    if fmt == "slack":
        # Slack Block Kit
        colour = {
            "critical": "#c43a3a", "high": "#c43a3a",
            "warning":  "#ba7517", "medium": "#ba7517",
            "info":     "#1a6e9e", "low":    "#1a6e9e",
        }.get(severity, "#1a6e9e")
        blocks = [
            {"type": "header",
             "text": {"type": "plain_text", "text": f"{icon} {title}", "emoji": True}},
        ]
        if message:
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": message[:2900]}})
        fields = []
        for k in ("entity", "entity_id", "score", "delta", "feed", "count"):
            if k in payload and payload[k] is not None:
                fields.append({"type": "mrkdwn",
                               "text": f"*{k}:*\n{str(payload[k])[:80]}"})
        if fields:
            blocks.append({"type": "section", "fields": fields[:10]})
        if url:
            blocks.append({"type": "actions", "elements": [
                {"type": "button",
                 "text": {"type": "plain_text", "text": "Open in Fieldwork"},
                 "url":  url,
                 "style": "primary"}
            ]})
        return {
            "text":        f"{icon} {title}",
            "attachments": [{"color": colour, "blocks": blocks}],
        }

    if fmt == "discord":
        colour_int = {
            "critical": 0xc43a3a, "high": 0xc43a3a,
            "warning":  0xba7517, "medium": 0xba7517,
            "info":     0x1a6e9e, "low":    0x1a6e9e,
        }.get(severity, 0x1a6e9e)
        embed = {
            "title":       f"{icon} {title}"[:256],
            "description": message[:4000],
            "color":       colour_int,
            "timestamp":   payload.get("timestamp") or datetime.utcnow().isoformat(),
            "footer":      {"text": "Fieldwork OSINT"},
            "fields":      [],
        }
        for k in ("entity", "entity_id", "score", "delta", "feed", "count"):
            if k in payload and payload[k] is not None:
                embed["fields"].append({
                    "name":   k,
                    "value":  str(payload[k])[:1024],
                    "inline": True,
                })
        if url:
            embed["url"] = url
        return {"embeds": [embed][:10]}

    if fmt == "teams":
        # Microsoft Teams MessageCard schema
        theme = {
            "critical": "C43A3A", "high": "C43A3A",
            "warning":  "BA7517", "medium": "BA7517",
            "info":     "1A6E9E", "low":    "1A6E9E",
        }.get(severity, "1A6E9E")
        facts = []
        for k in ("entity", "entity_id", "score", "delta", "feed", "count"):
            if k in payload and payload[k] is not None:
                facts.append({"name": k, "value": str(payload[k])[:200]})
        card = {
            "@type":       "MessageCard",
            "@context":    "https://schema.org/extensions",
            "themeColor":  theme,
            "summary":     title[:200],
            "title":       f"{icon} {title}",
            "sections":    [{
                "activityTitle":    event_type,
                "activitySubtitle": message[:500] if message else "",
                "facts":            facts,
                "markdown":         True,
            }],
        }
        if url:
            card["potentialAction"] = [{
                "@type":   "OpenUri",
                "name":    "Open in Fieldwork",
                "targets": [{"os": "default", "uri": url}],
            }]
        return card

    # raw — pass-through
    return {
        "event":     event_type,
        "source":    "fieldwork-osint",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        **payload,
    }


# Replace the simpler _fire_webhook + test_webhook with format-aware versions.
# We're not deleting the old ones — just shadowing the behaviour in the new endpoints.
async def _fire_webhook_formatted(wid: str, event_type: str, payload: dict) -> None:
    """Fire a webhook in the background — uses the format declared on the webhook record (or auto-detects)."""
    wh = _webhooks.get(wid)
    if not wh or not wh.get("enabled"):
        return
    if event_type not in (wh.get("events") or []):
        return
    fmt = (wh.get("format") or "auto").lower()
    if fmt == "auto":
        fmt = _detect_webhook_format(wh["url"])
    body = _format_webhook_payload(event_type, payload, fmt)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(wh["url"], json=body)
            _webhooks[wid]["last_fired"]  = datetime.utcnow().isoformat() + "Z"
            _webhooks[wid]["last_status"] = resp.status_code
            _webhooks[wid]["last_format"] = fmt
    except Exception as exc:
        _webhooks[wid]["last_fired"]  = datetime.utcnow().isoformat() + "Z"
        _webhooks[wid]["last_status"] = f"error: {exc}"
        log.warning("webhook %s failed: %s", wid, exc)


async def _broadcast_event(event_type: str, payload: dict) -> None:   # noqa: F811
    """Fan-out event to all enabled webhooks (format-aware, supersedes Ph77)."""
    for wid in list(_webhooks):
        await _fire_webhook_formatted(wid, event_type, payload)


@app.patch("/settings/webhooks/{webhook_id}/format")
async def set_webhook_format(
    webhook_id: str,
    payload:    dict = Body(...),
    _user:      dict = Depends(get_current_user),
):
    """Set the rich-message format for a webhook: auto | slack | discord | teams | raw."""
    if webhook_id not in _webhooks:
        raise HTTPException(404, "Webhook not found")
    fmt = (payload.get("format") or "auto").lower()
    if fmt not in ("auto", "slack", "discord", "teams", "raw"):
        raise HTTPException(400, "format must be one of: auto, slack, discord, teams, raw")
    _webhooks[webhook_id]["format"] = fmt
    return {"id": webhook_id, "format": fmt}


@app.post("/settings/webhooks/{webhook_id}/test-rich")
async def test_webhook_rich(
    webhook_id: str,
    _user:      dict = Depends(get_current_user),
):
    """Fire a richly-formatted test alert to the webhook using the configured format."""
    if webhook_id not in _webhooks:
        raise HTTPException(404, "Webhook not found")
    test_payload = {
        "title":     "Test alert — high-risk indicator detected",
        "message":   "This is a test from Fieldwork OSINT.\nIf you see this in your channel, the webhook is wired correctly.",
        "entity":    "evil.example.com",
        "entity_id": "domain:evil.example.com",
        "score":     85,
        "severity":  "warning",
        "url":       "https://fieldwork.local/",
    }
    await _fire_webhook_formatted(webhook_id, "test", test_payload)
    wh = _webhooks[webhook_id]
    return {
        "fired":  True,
        "format": wh.get("format") or _detect_webhook_format(wh["url"]),
        "status": wh.get("last_status"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph87 — AI-Generated Case Summary (Ollama)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/case/{case_id}/ai-summary")
async def ai_case_summary(
    case_id: str,
    user:    dict = Depends(get_current_user),
):
    """
    Generate an executive summary of a case using the local LLM.
    Pulls entities, hypotheses, notes, and recent activity, then asks the model
    to produce a structured threat-intel briefing.
    """
    cid = _val_case_id(case_id)

    async with graph_db.driver.session() as session:
        # Case metadata
        meta_r = await session.run(
            """
            MATCH (c:Case {id:$cid})
            RETURN c.title AS title, c.description AS description,
                   c.status AS status, c.priority AS priority,
                   c.created_at AS created
            """, cid=cid,
        )
        meta = await meta_r.single()
        if not meta:
            raise HTTPException(404, "Case not found")

        # Entities (capped)
        ent_r = await session.run(
            """
            MATCH (c:Case {id:$cid})-[:HAS_ENTITY]->(e)
            RETURN labels(e)[0] AS label,
                   coalesce(e.display_name, e.address, e.domain,
                            e.hash_value, e.url, e.email, e.id) AS name,
                   coalesce(e.gn_classification, '') AS gn,
                   coalesce(e.urlhaus_listed, false) AS urlhaus,
                   coalesce(e.vt_malicious, 0) AS vt,
                   coalesce(e.country, '') AS country
            LIMIT 80
            """, cid=cid,
        )
        ent_records = await ent_r.fetch(80)

        # Hypotheses
        hyp_r = await session.run(
            """
            MATCH (c:Case {id:$cid})-[:HAS_HYPOTHESIS]->(h:Hypothesis)
            RETURN h.claim AS claim, h.status AS status, h.confidence AS conf
            ORDER BY h.created_at DESC LIMIT 20
            """, cid=cid,
        )
        hyp_records = await hyp_r.fetch(20)

        # Notes
        notes_r = await session.run(
            """
            MATCH (c:Case {id:$cid})-[:HAS_NOTE]->(n:CaseNote)
            RETURN coalesce(n.kind, n.note_type, 'general') AS kind,
                   coalesce(n.content, n.body, '') AS body,
                   n.created_at AS ts
            ORDER BY n.created_at DESC LIMIT 30
            """, cid=cid,
        )
        notes_records = await notes_r.fetch(30)

    # Build a structured context
    entities_by_label: dict[str, list] = {}
    for r in ent_records:
        lb = r["label"] or "Unknown"
        entities_by_label.setdefault(lb, []).append({
            "name":    r["name"],
            "gn":      r["gn"],
            "urlhaus": r["urlhaus"],
            "vt":      r["vt"],
            "country": r["country"],
        })

    ctx_lines = [
        f"# CASE: {meta['title']}",
        f"Status: {meta['status']} | Priority: {meta['priority']}",
        f"Description: {(meta['description'] or '').strip()[:600]}",
        "",
        "## ENTITIES",
    ]
    for lb, items in entities_by_label.items():
        ctx_lines.append(f"### {lb} ({len(items)})")
        for it in items[:15]:
            risk_bits = []
            if it["gn"]:           risk_bits.append(f"GreyNoise:{it['gn']}")
            if it["urlhaus"]:      risk_bits.append("URLhaus:listed")
            if it["vt"] and it["vt"] > 0:
                risk_bits.append(f"VT:{it['vt']}")
            if it["country"]:      risk_bits.append(it["country"])
            tag = f" [{', '.join(risk_bits)}]" if risk_bits else ""
            ctx_lines.append(f"  - {it['name']}{tag}")

    if hyp_records:
        ctx_lines.append("\n## HYPOTHESES")
        for r in hyp_records:
            ctx_lines.append(f"  - ({r['status']}, {r['conf']}) {r['claim']}")

    if notes_records:
        ctx_lines.append("\n## RECENT NOTES")
        for r in notes_records[:15]:
            body = (r["body"] or "").strip().replace("\n", " ")[:240]
            ctx_lines.append(f"  - [{r['kind']}] {body}")

    context = "\n".join(ctx_lines)[:8000]

    prompt = (
        "You are a senior threat-intelligence analyst writing an internal briefing.\n"
        "Produce a concise structured summary of the investigation context below.\n"
        "Use the following sections, each as a markdown heading:\n"
        "  1. ## Executive Summary  (3-4 sentences)\n"
        "  2. ## Key Findings        (bullet list, evidence-backed)\n"
        "  3. ## Threat Actor Hypothesis  (single best assessment with confidence)\n"
        "  4. ## Recommended Next Steps  (numbered, prioritised)\n"
        "  5. ## Open Questions      (gaps in evidence)\n\n"
        "Do NOT invent facts that are not in the context. If something is unknown, say so.\n"
        "Be precise about technical details. Avoid filler.\n"
    )

    try:
        response = await llm_chat(
            message=prompt + "\n\n## CONTEXT\n" + context,
            context="",
            history=[],
            model=None,   # default Ollama model
        )
    except Exception as exc:
        raise HTTPException(503, f"LLM unavailable: {exc}")

    _audit("CaseAISummary", cid,
           detail=f"entities={len(ent_records)} hyps={len(hyp_records)} notes={len(notes_records)}")
    return {
        "case_id":     cid,
        "summary":     response,
        "context_chars": len(context),
        "input_counts": {
            "entities":   len(ent_records),
            "hypotheses": len(hyp_records),
            "notes":      len(notes_records),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph88 — Geographic IP Heat Map
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/map/ip-heatmap")
async def ip_heatmap(
    case_id: Optional[str] = Query(None),
    _user:   dict = Depends(get_current_user),
):
    """
    Return aggregated IP coordinates suitable for a Leaflet heat-map overlay.
    Each entry is [lat, lng, intensity] where intensity grows with the number
    of IPs at that location and is amplified for high-risk indicators.
    """
    where_clause = ""
    params: dict = {}
    if case_id:
        cid = _val_case_id(case_id)
        where_clause = "MATCH (c:Case {id:$cid})-[:HAS_ENTITY]->(n:IP) "
        params["cid"] = cid
    else:
        where_clause = "MATCH (n:IP) "

    async with graph_db.driver.session() as session:
        r = await session.run(
            where_clause +
            """
            WHERE n.latitude IS NOT NULL AND n.longitude IS NOT NULL
            RETURN n.address AS ip,
                   toFloat(n.latitude)  AS lat,
                   toFloat(n.longitude) AS lng,
                   coalesce(n.country, '') AS country,
                   coalesce(n.city,    '') AS city,
                   coalesce(n.org,     '') AS org,
                   coalesce(n.gn_classification, '') AS gn_class,
                   coalesce(n.urlhaus_listed, false) AS urlhaus,
                   coalesce(n.vt_malicious, 0) AS vt
            LIMIT 2000
            """,
            **params,
        )
        records = await r.fetch(2000)

    # Bucket by rounded coordinate to merge nearby points
    buckets: dict[tuple, dict] = {}
    for rec in records:
        if rec["lat"] is None or rec["lng"] is None:
            continue
        key = (round(rec["lat"], 2), round(rec["lng"], 2))
        b = buckets.setdefault(key, {
            "lat":     key[0],
            "lng":     key[1],
            "count":   0,
            "bad":     0,
            "sample":  [],
            "country": rec["country"],
            "city":    rec["city"],
        })
        b["count"] += 1
        if rec["urlhaus"] or rec["gn_class"] == "malicious" or rec["vt"] > 0:
            b["bad"] += 1
        if len(b["sample"]) < 4:
            b["sample"].append(rec["ip"])

    points = []
    by_country: dict[str, int] = {}
    for b in buckets.values():
        # intensity: log scale on count + 2× boost for malicious
        intensity = min(1.0, (b["count"] + b["bad"] * 2) / 10.0)
        points.append({
            "lat":       b["lat"],
            "lng":       b["lng"],
            "intensity": round(intensity, 3),
            "count":     b["count"],
            "bad":       b["bad"],
            "sample":    b["sample"],
            "country":   b["country"],
            "city":      b["city"],
        })
        if b["country"]:
            by_country[b["country"]] = by_country.get(b["country"], 0) + b["count"]

    top_countries = sorted(by_country.items(), key=lambda x: -x[1])[:15]

    return {
        "points":        points,
        "total_ips":     sum(b["count"] for b in buckets.values()),
        "total_bad":     sum(b["bad"]   for b in buckets.values()),
        "bucket_count":  len(buckets),
        "top_countries": [{"country": c, "count": n} for c, n in top_countries],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph89 — Custom Entity Properties
# ─────────────────────────────────────────────────────────────────────────────

# Custom props are stored as a JSON blob on the node under `custom_props`
# to avoid colliding with built-in schema fields.

class CustomPropRequest(BaseModel):
    key:   str = Field(..., min_length=1, max_length=60)
    value: str = Field(..., max_length=2000)


_CUSTOM_KEY_RE = re.compile(r"^[A-Za-z0-9_\-. ]+$")


@app.get("/entity/{entity_id}/custom-props")
async def get_custom_props(
    entity_id: str,
    _user:     dict = Depends(get_current_user),
):
    """Read the custom-properties JSON blob for an entity."""
    import json as _j
    async with graph_db.driver.session() as session:
        r = await session.run(
            "MATCH (n {id:$eid}) RETURN n.custom_props AS p",
            eid=entity_id,
        )
        rec = await r.single()
        if not rec:
            raise HTTPException(404, "Entity not found")
        try:
            props = _j.loads(rec["p"]) if rec["p"] else {}
        except Exception:
            props = {}
    return {"entity_id": entity_id, "custom_props": props, "count": len(props)}


@app.post("/entity/{entity_id}/custom-prop")
async def set_custom_prop(
    entity_id: str,
    req:       CustomPropRequest,
    _user:     dict = Depends(get_current_user),
):
    """Add or update one custom property on an entity."""
    if not _CUSTOM_KEY_RE.match(req.key):
        raise HTTPException(400, "Key must be alphanumeric (with - _ . space) only")
    import json as _j
    async with graph_db.driver.session() as session:
        r = await session.run(
            "MATCH (n {id:$eid}) RETURN n.custom_props AS p",
            eid=entity_id,
        )
        rec = await r.single()
        if not rec:
            raise HTTPException(404, "Entity not found")
        try:
            props = _j.loads(rec["p"]) if rec["p"] else {}
        except Exception:
            props = {}
        props[req.key] = req.value
        await session.run(
            "MATCH (n {id:$eid}) SET n.custom_props = $p",
            eid=entity_id, p=_j.dumps(props),
        )
    return {"entity_id": entity_id, "key": req.key, "value": req.value, "total": len(props)}


@app.delete("/entity/{entity_id}/custom-prop/{key}")
async def delete_custom_prop(
    entity_id: str,
    key:       str,
    _user:     dict = Depends(get_current_user),
):
    import json as _j
    async with graph_db.driver.session() as session:
        r = await session.run(
            "MATCH (n {id:$eid}) RETURN n.custom_props AS p",
            eid=entity_id,
        )
        rec = await r.single()
        if not rec:
            raise HTTPException(404, "Entity not found")
        try:
            props = _j.loads(rec["p"]) if rec["p"] else {}
        except Exception:
            props = {}
        if key in props:
            del props[key]
            await session.run(
                "MATCH (n {id:$eid}) SET n.custom_props = $p",
                eid=entity_id, p=_j.dumps(props),
            )
    return {"entity_id": entity_id, "removed": key}


# ─────────────────────────────────────────────────────────────────────────────
# Ph90 — Subdomain Brute-Force Enumeration
# ─────────────────────────────────────────────────────────────────────────────

# Curated wordlist — top common subdomain names
_SUBDOMAIN_WORDLIST: list[str] = [
    "www","mail","ftp","webmail","smtp","pop","ns1","ns2","ns3","ns4",
    "admin","administrator","ad","blog","shop","store","app","apps","api","apis",
    "dev","staging","stage","stg","test","testing","qa","uat","prod","production",
    "beta","alpha","demo","sandbox","preview","internal","intranet","extranet",
    "vpn","remote","ssh","rdp","portal","login","sso","auth","oauth","idp",
    "cdn","static","assets","media","img","images","video","videos","files","download",
    "downloads","upload","uploads","secure","vault","crm","erp","hr","finance",
    "support","help","helpdesk","ticket","tickets","desk","kb","docs","wiki","status",
    "monitor","monitoring","grafana","kibana","prometheus","jenkins","gitlab","github",
    "git","svn","jira","confluence","bitbucket","artifactory","nexus","npm","pypi",
    "registry","docker","k8s","kube","kubernetes","cluster","cloud","aws","azure","gcp",
    "s3","backup","backups","db","database","sql","mysql","postgres","oracle","mongo",
    "redis","cache","elastic","es","search","mx","mx1","mx2","email","exchange",
    "owa","outlook","calendar","chat","slack","teams","zoom","meet","video","conf",
    "voip","sip","pbx","fax","sms","gateway","gw","router","switch","firewall",
    "edge","lb","loadbalancer","proxy","squid","nginx","apache","tomcat","weblogic",
    "iis","node","node1","node2","srv","srv01","srv02","host","host1","host2",
    "server","server1","server2","web","web1","web2","www1","www2","www3","www4",
    "old","new","tmp","temp","archive","legacy","v1","v2","v3","v4","api-v1","api-v2",
    "m","mobile","wap","i","my","portal-prod","portal-staging","public","private",
    "secret","intern","corp","corporate","partners","customer","customers","client",
    "clients","users","user","ldap","kerberos","dns","dns1","dns2","time","ntp",
    "pacific","atlantic","eu","us","asia","apac","emea","americas","north","south",
    "east","west","central","gateway1","gateway2","metrics","logs","syslog","sentry",
    "errors","alert","alerts","report","reports","analytics","track","tracking",
    "tracker","ads","banner","banners","pixel","webhook","webhooks","oauth2","sso2",
    "alpha2","beta2","release","releases","update","updates","cdn1","cdn2","cdn3",
    "static1","static2","origin","origin1","origin2","cache1","cache2","mirror","mirror1",
]


@app.post("/enrich/domain/{domain}/brute-subdomains")
async def brute_subdomains(
    domain:    str,
    limit:     int  = Query(200, ge=10, le=500, description="How many wordlist entries to try"),
    timeout_s: float = Query(2.0, ge=0.5, le=10.0, description="DNS timeout per query"),
    _user:     dict = Depends(get_current_user),
):
    """
    Brute-force common subdomains for a target domain using the built-in wordlist.
    Creates Domain → IP RESOLVES_TO edges for every successful resolution.

    Pure DNS — no third-party API key needed.
    """
    d = _validate_domain(domain)
    import asyncio as _aio
    import socket  as _sock

    words = _SUBDOMAIN_WORDLIST[:limit]
    sem   = _aio.Semaphore(20)

    async def _resolve(fqdn: str) -> tuple[str, list[str]]:
        """Resolve fqdn → list of IPs (empty list on failure)."""
        async with sem:
            loop = _aio.get_running_loop()
            try:
                infos = await _aio.wait_for(
                    loop.getaddrinfo(fqdn, None, family=_sock.AF_INET),
                    timeout=timeout_s,
                )
                ips = list({info[4][0] for info in infos if info[4]})
                return fqdn, ips
            except Exception:
                return fqdn, []

    coros   = [_resolve(f"{w}.{d}") for w in words]
    results = await _aio.gather(*coros)
    hits    = [(fqdn, ips) for fqdn, ips in results if ips]

    # Persist hits
    persisted = 0
    async with graph_db.driver.session() as session:
        # Ensure parent domain exists
        await session.run(
            """
            MERGE (parent:Domain {id:$pid})
            ON CREATE SET parent.domain=$d, parent.created_at=datetime()
            """,
            pid=f"domain:{d}", d=d,
        )
        for fqdn, ips in hits:
            sub_id = f"domain:{fqdn}"
            await session.run(
                """
                MERGE (sub:Domain {id:$sid})
                ON CREATE SET sub.domain=$f, sub.created_at=datetime(),
                              sub.source='brute_subdomain'
                ON MATCH SET sub.last_seen_brute=datetime()
                WITH sub
                MATCH (parent:Domain {id:$pid})
                MERGE (parent)-[:HAS_SUBDOMAIN]->(sub)
                """,
                sid=sub_id, f=fqdn, pid=f"domain:{d}",
            )
            for ip in ips:
                await session.run(
                    """
                    MERGE (ip:IP {id:$ipid})
                    ON CREATE SET ip.address=$addr, ip.created_at=datetime(),
                                  ip.source='brute_subdomain'
                    WITH ip
                    MATCH (sub:Domain {id:$sid})
                    MERGE (sub)-[r:RESOLVES_TO]->(ip)
                    ON CREATE SET r.first_seen=datetime(), r.source='brute_subdomain'
                    """,
                    ipid=f"ip:{ip}", addr=ip, sid=sub_id,
                )
            persisted += 1

    _audit("BruteSubdomains", d, detail=f"tried={len(words)} hits={len(hits)}")
    return {
        "domain":    d,
        "wordlist_size": len(words),
        "hits":      [{"fqdn": f, "ips": ips} for f, ips in hits],
        "hit_count": len(hits),
        "persisted": persisted,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ph91 — IOC Confidence Voting
# ─────────────────────────────────────────────────────────────────────────────

_VALID_VERDICTS = {"malicious", "benign", "unknown"}


class VoteRequest(BaseModel):
    verdict: str = Field(..., description="malicious | benign | unknown")
    comment: str = Field("", max_length=400)


@app.post("/entity/{entity_id}/vote")
async def submit_vote(
    entity_id: str,
    req:       VoteRequest,
    user:      dict = Depends(get_current_user),
):
    """
    Cast (or update) the current user's verdict on an entity.
    Each user has exactly one vote per entity — recasting overwrites.
    """
    verdict = req.verdict.lower().strip()
    if verdict not in _VALID_VERDICTS:
        raise HTTPException(400, f"verdict must be one of: {sorted(_VALID_VERDICTS)}")
    uid  = user.get("sub") or user.get("username") or "anonymous"
    now  = datetime.utcnow().isoformat() + "Z"
    async with graph_db.driver.session() as session:
        # Confirm entity exists
        e = await session.run("MATCH (e {id:$eid}) RETURN e.id", eid=entity_id)
        if not await e.single():
            raise HTTPException(404, "Entity not found")
        # Upsert vote (one per voter per entity)
        await session.run(
            """
            MATCH (e {id:$eid})
            MERGE (v:Vote {id: $eid + ':vote:' + $uid})
            ON CREATE SET v.entity_id=$eid, v.voter_id=$uid,
                          v.verdict=$verdict, v.comment=$comment, v.created_at=$now
            ON MATCH  SET v.verdict=$verdict, v.comment=$comment, v.updated_at=$now
            MERGE (e)-[:HAS_VOTE]->(v)
            """,
            eid=entity_id, uid=uid, verdict=verdict, comment=req.comment, now=now,
        )
    return {"entity_id": entity_id, "verdict": verdict, "voter": uid}


@app.get("/entity/{entity_id}/votes")
async def get_votes(
    entity_id: str,
    user:      dict = Depends(get_current_user),
):
    """Get aggregated vote tally for an entity plus the calling user's own vote."""
    uid = user.get("sub") or user.get("username") or "anonymous"
    async with graph_db.driver.session() as session:
        r = await session.run(
            """
            MATCH (e {id:$eid})-[:HAS_VOTE]->(v:Vote)
            RETURN v.verdict AS verdict, v.voter_id AS voter, v.comment AS comment,
                   coalesce(v.updated_at, v.created_at) AS ts
            ORDER BY ts DESC
            """, eid=entity_id,
        )
        records = await r.fetch(200)

    tally = {"malicious": 0, "benign": 0, "unknown": 0}
    own_vote: Optional[dict] = None
    voters: list[dict] = []
    for rec in records:
        v = rec["verdict"]
        if v in tally:
            tally[v] += 1
        voters.append({
            "voter":   rec["voter"],
            "verdict": v,
            "comment": rec["comment"],
            "ts":      str(rec["ts"]) if rec["ts"] else "",
        })
        if rec["voter"] == uid:
            own_vote = voters[-1]

    total = sum(tally.values())
    # Confidence = ratio of dominant verdict
    if total > 0:
        leading = max(tally, key=tally.get)
        confidence = round(tally[leading] / total * 100)
    else:
        leading = None
        confidence = 0

    return {
        "entity_id":  entity_id,
        "tally":      tally,
        "total":      total,
        "leading":    leading,
        "confidence": confidence,
        "voters":     voters[:50],
        "own_vote":   own_vote,
    }


@app.delete("/entity/{entity_id}/vote")
async def retract_vote(
    entity_id: str,
    user:      dict = Depends(get_current_user),
):
    """Retract the calling user's vote on an entity."""
    uid = user.get("sub") or user.get("username") or "anonymous"
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MATCH (v:Vote {id: $eid + ':vote:' + $uid})
            DETACH DELETE v
            """, eid=entity_id, uid=uid,
        )
    return {"retracted": True, "voter": uid}


# ============================================================
# Recon — background jobs: nmap, subfinder, nuclei, httpx
# ============================================================
import uuid as _uuid
from datetime import datetime as _dt

_RECON_URL  = os.getenv("RECON_URL", "http://recon:7002")
_recon_jobs: dict[str, dict] = {}          # in-memory job store (single-user)


def _new_recon_job(tool: str, target: str) -> dict:
    jid = _uuid.uuid4().hex[:10]
    job: dict = {
        "id":       jid,
        "tool":     tool,
        "target":   target,
        "status":   "running",
        "result":   None,
        "error":    None,
        "started":  _dt.utcnow().isoformat(),
        "finished": None,
    }
    _recon_jobs[jid] = job
    return job


async def _store_nmap(target: str, data: dict) -> None:
    """Promote open nmap ports to Neo4j Port nodes linked to the scanned host."""
    async with graph_db.driver.session() as s:
        for p in data.get("open_ports", []):
            # Use the actual resolved IP from nmap output when available
            host_ip = p.get("host") or target
            await s.run(
                """
                MERGE (i:IP {id: $ip})
                  ON CREATE SET i.address = $ip, i.first_seen = datetime()
                WITH i
                MERGE (port:Port {id: $pid})
                  ON CREATE SET port.number     = $num,
                                port.protocol   = $proto,
                                port.service    = $svc,
                                port.product    = $prod,
                                port.version    = $ver,
                                port.source     = 'nmap',
                                port.first_seen = datetime()
                  ON MATCH SET  port.last_seen  = datetime(),
                                port.service    = $svc,
                                port.product    = $prod,
                                port.version    = $ver
                MERGE (i)-[:HAS_PORT]->(port)
                """,
                ip=host_ip,
                pid=f"{host_ip}:{p['port']}/{p.get('protocol','tcp')}",
                num=p["port"],
                proto=p.get("protocol", "tcp"),
                svc=p.get("service", ""),
                prod=p.get("product", ""),
                ver=p.get("version", ""),
            )


async def _store_subfinder(domain: str, data: dict) -> None:
    """Promote subdomains to Domain nodes, link with HAS_SUBDOMAIN."""
    async with graph_db.driver.session() as s:
        for sd in data.get("subdomains", []):
            host = (sd.get("host") or "").strip()
            if not host:
                continue
            await s.run(
                """
                MERGE (child:Domain {id: $child})
                  ON CREATE SET child.name = $child,
                                child.source = 'subfinder',
                                child.first_seen = datetime()
                  ON MATCH SET  child.last_seen = datetime()
                WITH child
                MERGE (parent:Domain {id: $parent})
                  ON CREATE SET parent.name = $parent,
                                parent.first_seen = datetime()
                MERGE (parent)-[:HAS_SUBDOMAIN]->(child)
                """,
                child=host, parent=domain,
            )


async def _store_nuclei(target: str, data: dict) -> None:
    """Promote nuclei findings to Vulnerability nodes linked to target (IP or Domain)."""
    # Determine whether to create an IP or Domain node
    try:
        ipaddress.ip_address(target)
        node_label, node_prop = "IP", "address"
    except ValueError:
        node_label, node_prop = "Domain", "name"

    async with graph_db.driver.session() as s:
        for f in data.get("findings", []):
            tid = f.get("template_id", "unknown")
            await s.run(
                f"""
                MERGE (v:Vulnerability {{id: $vid}})
                  ON CREATE SET v.name        = $name,
                                v.severity    = $sev,
                                v.template_id = $tid,
                                v.matched_at  = $matched,
                                v.source      = 'nuclei',
                                v.first_seen  = datetime()
                  ON MATCH SET  v.last_seen   = datetime()
                WITH v
                MERGE (t:{node_label} {{id: $target}})
                  ON CREATE SET t.{node_prop} = $target, t.first_seen = datetime()
                MERGE (t)-[:HAS_VULNERABILITY]->(v)
                """,
                vid=f"{target}:{tid}",
                name=f.get("name", tid),
                sev=f.get("severity", "unknown"),
                tid=tid,
                matched=f.get("matched_at", target),
                target=target,
            )


async def _store_httpx(target: str, data: dict) -> None:
    """Update Domain node with HTTP metadata from httpx probe."""
    async with graph_db.driver.session() as s:
        for r in data.get("results", []):
            await s.run(
                """
                MERGE (d:Domain {id: $id})
                  ON CREATE SET d.name = $id, d.first_seen = datetime()
                SET d.http_status = $status,
                    d.http_title  = $title,
                    d.http_tech   = $tech,
                    d.webserver   = $server,
                    d.last_seen   = datetime()
                """,
                id=target,
                status=r.get("status_code", 0),
                title=r.get("title", ""),
                tech=r.get("technologies", []),
                server=r.get("webserver", ""),
            )


_RECON_STORE: dict[str, object] = {
    "nmap":      _store_nmap,
    "subfinder": _store_subfinder,
    "nuclei":    _store_nuclei,
    "httpx":     _store_httpx,
}


async def _run_recon_job(job_id: str, tool: str, payload: dict) -> None:
    job = _recon_jobs[job_id]
    try:
        async with httpx.AsyncClient(timeout=400.0) as client:
            resp = await client.post(f"{_RECON_URL}/{tool}", json=payload)
            resp.raise_for_status()
            data = resp.json()
        job["status"] = "done"
        job["result"] = data
        # Persist findings to Neo4j
        store_fn = _RECON_STORE.get(tool)
        if store_fn:
            target = payload.get("target") or payload.get("domain", "")
            await store_fn(target, data)
    except httpx.ConnectError:
        job["status"] = "error"
        job["error"]  = "Recon service unavailable — run: docker compose up -d recon"
    except httpx.HTTPStatusError as exc:
        job["status"] = "error"
        job["error"]  = f"Recon service error {exc.response.status_code}: {exc.response.text[:300]}"
    except Exception as exc:
        job["status"] = "error"
        job["error"]  = str(exc)
    finally:
        job["finished"] = _dt.utcnow().isoformat()


# ── Pydantic model ────────────────────────────────────────────────────────────

class ReconStartRequest(BaseModel):
    target:  str  = Field(..., min_length=1, max_length=500)
    options: dict = Field(default_factory=dict)


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/recon/jobs")
async def list_recon_jobs():
    """Return all recon jobs (most recent first)."""
    jobs = sorted(_recon_jobs.values(), key=lambda j: j["started"], reverse=True)
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/recon/jobs/{job_id}")
async def get_recon_job(job_id: str):
    """Poll a single recon job by ID."""
    job = _recon_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.delete("/recon/jobs")
async def clear_recon_jobs():
    """Remove all finished recon jobs (keep running ones)."""
    for jid in list(_recon_jobs.keys()):
        if _recon_jobs[jid]["status"] != "running":
            del _recon_jobs[jid]
    return {"remaining": len(_recon_jobs)}


@app.post("/recon/nmap")
async def start_nmap(req: ReconStartRequest, background_tasks: BackgroundTasks):
    """Start an nmap port/service scan in the background."""
    job = _new_recon_job("nmap", req.target)
    payload = {"target": req.target, **req.options}
    background_tasks.add_task(_run_recon_job, job["id"], "nmap", payload)
    return {"job_id": job["id"], "status": "running"}


@app.post("/recon/subfinder")
async def start_subfinder(req: ReconStartRequest, background_tasks: BackgroundTasks):
    """Start a subfinder passive subdomain enumeration in the background."""
    job = _new_recon_job("subfinder", req.target)
    payload = {"domain": req.target, **req.options}
    background_tasks.add_task(_run_recon_job, job["id"], "subfinder", payload)
    return {"job_id": job["id"], "status": "running"}


@app.post("/recon/nuclei")
async def start_nuclei(req: ReconStartRequest, background_tasks: BackgroundTasks):
    """Start a nuclei vulnerability template scan in the background."""
    job = _new_recon_job("nuclei", req.target)
    payload = {"target": req.target, **req.options}
    background_tasks.add_task(_run_recon_job, job["id"], "nuclei", payload)
    return {"job_id": job["id"], "status": "running"}


@app.post("/recon/httpx")
async def start_httpx(req: ReconStartRequest, background_tasks: BackgroundTasks):
    """Start an httpx HTTP probe in the background."""
    job = _new_recon_job("httpx", req.target)
    payload = {"target": req.target, **req.options}
    background_tasks.add_task(_run_recon_job, job["id"], "httpx", payload)
    return {"job_id": job["id"], "status": "running"}


# ═══════════════════════════════════════════════════════════════════════════════
# New enrichment endpoints — IPInfo / Hunter / EmailRep / Google Dorks
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/enrich/ip/{ip}/ipinfo")
async def ip_ipinfo(ip: str):
    """
    IPinfo.io IP enrichment — geolocation, ASN, hostname, timezone.
    Works without a key; set IPINFO_TOKEN for higher rate limits.
    """
    i = _validate_ip(ip)
    res = await enrich_ip_ipinfo(i)
    _audit("IPInfo", i, detail=f"city={res.get('city','')} org={res.get('org','')[:40]}")
    return res


@app.get("/enrich/domain/{domain}/hunter")
@app.post("/enrich/domain/{domain}/hunter")
async def domain_hunter(domain: str):
    """
    Hunter.io — discover email addresses for a company domain.
    Requires HUNTER_API_KEY.
    """
    d = _validate_domain(domain)
    res = await hunt_domain_emails(d)
    _audit("Hunter", d, detail=f"emails={len(res.get('emails', []))}")
    return res


@app.get("/enrich/email/{email}/rep")
@app.post("/enrich/email/{email}/rep")
async def email_rep(email: str):
    """
    EmailRep.io — check email reputation and suspicious flags.
    Free tier works without a key; set EMAILREP_KEY for higher limits.
    """
    res = await check_email_rep(email)
    _audit("EmailRep", email, detail=f"rep={res.get('reputation','?')} susp={res.get('suspicious')}")
    return res


class DorkRequest(BaseModel):
    query: str
    num:   int = 10


@app.post("/search/dork")
async def google_dork(req: DorkRequest):
    """
    Google Custom Search Engine dork — run advanced Google search operators.
    Requires GOOGLE_CSE_KEY and GOOGLE_CSE_CX.
    """
    res = await run_dork(req.query, req.num)
    _audit("GoogleDork", req.query[:80], detail=f"results={len(res.get('results', []))}")
    return res


# ============================================================
# Sanctions, Adverse Media, Court Records, WHOIS History
# Professional Report — Phase 30
# ============================================================

@app.get("/enrich/name/sanctions")
async def sanctions_by_name(q: str):
    """Screen a name against OFAC, EU, UN, UK HMT sanctions lists and PEP databases."""
    if not q or len(q) > 300:
        raise HTTPException(400, "q is required (max 300 chars)")
    return await check_sanctions(q)


@app.get("/enrich/name/adverse")
async def adverse_by_name(q: str):
    """Search adverse media (negative news) for a name using DuckDuckGo + LLM classification."""
    if not q or len(q) > 300:
        raise HTTPException(400, "q is required (max 300 chars)")
    return await search_adverse_media(
        q,
        os.getenv("OLLAMA_URL", "http://ollama:11434"),
        os.getenv("OLLAMA_MODEL", "llama3.2"),
    )


@app.get("/enrich/name/courts")
async def courts_by_name(q: str):
    """Search US federal and state court records via CourtListener."""
    if not q or len(q) > 300:
        raise HTTPException(400, "q is required (max 300 chars)")
    return await search_court_records(q)


@app.get("/enrich/person/{person_id}/sanctions")
async def enrich_person_sanctions(person_id: str):
    person = await graph_db.get_person_by_id(person_id)
    name = (person or {}).get("name", person_id)
    return await check_sanctions(name)


@app.get("/enrich/person/{person_id}/adverse")
async def enrich_person_adverse(person_id: str):
    person = await graph_db.get_person_by_id(person_id)
    name = (person or {}).get("name", person_id)
    return await search_adverse_media(
        name,
        os.getenv("OLLAMA_URL", "http://ollama:11434"),
        os.getenv("OLLAMA_MODEL", "llama3.2"),
    )


@app.get("/enrich/person/{person_id}/courts")
async def enrich_person_courts(person_id: str):
    person = await graph_db.get_person_by_id(person_id)
    name = (person or {}).get("name", person_id)
    return await search_court_records(name)


@app.get("/enrich/domain/{domain}/whois-history")
async def enrich_domain_whois_history(domain: str):
    return await get_whois_history(domain)


# ═══════════════════════════════════════════════════════════════════════════════
# New crawlers — Companies House, OTX, Reddit, Wikidata, Etherscan, Maritime, Geocode
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/enrich/company/uk/search")
async def companies_house_search_ep(q: str):
    """UK Companies House — search the official register by company name."""
    if not q or len(q) > 300:
        raise HTTPException(400, "q is required (max 300 chars)")
    res = await search_companies_house(q)
    _audit("CompaniesHouse", q[:80], detail=f"results={len(res.get('companies', []))}")
    return res


@app.get("/enrich/company/uk/{number}")
async def companies_house_company_ep(number: str):
    """UK Companies House — full profile, officers, and beneficial owners (PSC)."""
    if not re.match(r"^[A-Za-z0-9]{6,10}$", number):
        raise HTTPException(400, "invalid company number")
    res = await companies_house_company(number)
    _audit("CompaniesHouse", number, detail=f"owners={len(res.get('beneficial_owners', []))}")
    return res


@app.get("/enrich/ioc/otx")
async def otx_ep(indicator: str):
    """AlienVault OTX — threat-intel context for an IP, domain, hostname, or file hash."""
    if not indicator or len(indicator) > 256:
        raise HTTPException(400, "indicator is required (max 256 chars)")
    res = await enrich_otx(indicator.strip())
    _audit("OTX", indicator[:80], detail=f"pulses={res.get('pulse_count', 0)}")
    return res


@app.get("/enrich/reddit/user/{username}")
async def reddit_user_ep(username: str):
    """Reddit — profile, karma, active subreddits, and recent comment history."""
    res = await reddit_user(username)
    _audit("Reddit", username, detail=f"karma={res.get('comment_karma', 0)}")
    return res


@app.get("/enrich/reddit/search")
async def reddit_search_ep(q: str):
    """Reddit — search posts mentioning a query term."""
    if not q or len(q) > 300:
        raise HTTPException(400, "q is required (max 300 chars)")
    res = await reddit_search(q)
    _audit("Reddit", q[:80], detail=f"posts={len(res.get('posts', []))}")
    return res


@app.get("/enrich/entity/wikidata")
async def wikidata_ep(q: str):
    """Wikidata + Wikipedia — structured entity facts and summary for research briefs."""
    if not q or len(q) > 300:
        raise HTTPException(400, "q is required (max 300 chars)")
    res = await lookup_wikidata(q)
    _audit("Wikidata", q[:80], detail=f"id={res.get('id', '')}")
    return res


@app.get("/enrich/crypto/eth/{address}")
async def etherscan_ep(address: str):
    """Etherscan — ETH balance and recent transaction trace for an address."""
    res = await trace_eth_address(address)
    _audit("Etherscan", address[:64], detail=f"bal={res.get('balance_eth', 0)} txs={res.get('tx_count', 0)}")
    return res


@app.get("/enrich/vessel")
async def maritime_ep(q: str):
    """Maritime — resolve a vessel by MMSI/IMO/name → live position + tracking links."""
    if not q or len(q) > 120:
        raise HTTPException(400, "q is required (max 120 chars)")
    res = await track_vessel(q)
    _audit("Maritime", q[:80], detail=f"found={res.get('found')}")
    return res


@app.get("/enrich/geocode")
async def geocode_ep(q: str):
    """Nominatim — forward geocode an address/place to coordinates."""
    if not q or len(q) > 300:
        raise HTTPException(400, "q is required (max 300 chars)")
    res = await geocode(q)
    _audit("Geocode", q[:80], detail=f"results={len(res.get('results', []))}")
    return res


@app.get("/enrich/geocode/reverse")
async def reverse_geocode_ep(lat: float, lon: float):
    """Nominatim — reverse geocode coordinates to the nearest address."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "lat/lon out of range")
    res = await reverse_geocode(lat, lon)
    _audit("Geocode", f"{lat},{lon}", detail=res.get("display_name", "")[:60])
    return res


# ═══════════════════════════════════════════════════════════════════════════════
# AI Investigation Orchestrator — one target → multi-tool fan-out + Claude brief
# ═══════════════════════════════════════════════════════════════════════════════

class OrchestrateRequest(BaseModel):
    target:  str
    type:    Optional[str] = "auto"   # auto | name | email | domain | company | ip | username | crypto_eth
    persist: bool = True              # write findings into the knowledge graph
    case_id: Optional[str] = None     # attach the investigation to a case


@app.get("/investigate/detect")
async def investigate_detect(target: str):
    """Preview the auto-detected target type without running anything."""
    if not target or len(target) > 300:
        raise HTTPException(400, "target is required (max 300 chars)")
    return {"target": target, "type": orch_detect_type(target.strip())}


@app.post("/investigate/orchestrate")
async def investigate_orchestrate(req: OrchestrateRequest):
    """
    AI Investigation Orchestrator: auto-detect the target type, fan out across
    every relevant crawler concurrently, then have Claude (subscription bridge,
    Ollama fallback) synthesise a structured intelligence brief with citations.
    """
    if not req.target or len(req.target) > 300:
        raise HTTPException(400, "target is required (max 300 chars)")
    res = await orchestrate_investigation(req.target.strip(), req.type or "auto", graph_db=graph_db)
    if req.persist and not res.get("error"):
        try:
            res["graph"] = await persist_investigation(graph_db, res, case_id=req.case_id)
        except Exception as exc:
            log.warning("graph persist failed: %s", exc)
            res["graph"] = {"error": str(exc)}
    _audit("Orchestrate", req.target[:80],
           detail=f"type={res.get('type')} tools={len(res.get('tools_run', []))} "
                  f"engine={res.get('engine')} graph={res.get('graph', {}).get('nodes', 0)}n")
    return res


class DeepInvestigateRequest(BaseModel):
    target:     str
    type:       Optional[str] = "auto"
    max_hops:   int = 1        # 1 = seed + direct pivots
    max_branch: int = 4        # pivots followed per node


@app.post("/investigate/deep")
async def investigate_deep(req: DeepInvestigateRequest):
    """
    Recursive deep investigation: BFS auto-pivot from a seed (email→domain→
    people→…), expanding the knowledge graph, with one synthesized brief over
    the whole expansion. Bounded by max_hops / max_branch (global cap 8 nodes).
    """
    if not req.target or len(req.target) > 300:
        raise HTTPException(400, "target required")
    res = await orch_deep_investigate(
        req.target.strip(), req.type or "auto", graph_db=graph_db,
        max_hops=max(1, min(req.max_hops, 2)),
        max_branch=max(1, min(req.max_branch, 6)),
    )
    _audit("DeepInvestigate", req.target[:80],
           detail=f"hops={req.max_hops} nodes={res.get('node_count')} engine={res.get('engine')}")
    return res


@app.get("/investigate/graph")
async def investigate_graph(target_id: str, depth: int = 1):
    """Return the knowledge subgraph around an investigated target (for rendering)."""
    if not target_id or len(target_id) > 200:
        raise HTTPException(400, "target_id required")
    return await get_investigation_subgraph(graph_db, target_id.strip(), depth)


class MonitorRequest(BaseModel):
    target:     str
    type:       Optional[str] = "auto"
    interval_h: int = 24


@app.get("/monitor/investigations")
async def monitor_list():
    """List investigation monitors."""
    return {"monitors": inv_monitor.list_monitors()}


@app.post("/monitor/investigations")
async def monitor_add(req: MonitorRequest):
    """Register a target for scheduled re-investigation + change alerts."""
    if not req.target or len(req.target) > 300:
        raise HTTPException(400, "target required")
    return inv_monitor.add_monitor(req.target.strip(), req.type or "auto", req.interval_h)


@app.delete("/monitor/investigations/{mon_id}")
async def monitor_delete(mon_id: str):
    return {"removed": inv_monitor.remove_monitor(mon_id)}


@app.post("/monitor/investigations/{mon_id}/run")
async def monitor_run_now(mon_id: str):
    """Run one monitor immediately (establishes baseline on first run)."""
    mons = [m for m in inv_monitor.list_monitors() if m["id"] == mon_id]
    if not mons:
        raise HTTPException(404, "monitor not found")
    return await inv_monitor.run_monitor(graph_db, mons[0])


@app.get("/monitor/alerts")
async def monitor_alerts(limit: int = 100):
    """Change alerts raised by investigation monitors."""
    return {"alerts": inv_monitor.list_alerts(limit)}


@app.post("/investigate/link-analysis")
async def investigate_link_analysis():
    """Claude link analysis across the whole investigation graph: shared
    entities (hidden connections), duplicate-entity merge suggestions, top pivots."""
    res = await analyze_links(graph_db)
    _audit("LinkAnalysis", "graph",
           detail=f"shared={len(res.get('shared_entities', []))} "
                  f"dupes={len(res.get('duplicates', []))} engine={res.get('engine')}")
    return res


class ProfessionalReportRequest(BaseModel):
    person_id: str
    client_name: Optional[str] = None
    case_ref: Optional[str] = None
    redact_sources: bool = False


@app.post("/report/professional")
async def create_professional_report(req: ProfessionalReportRequest):
    """Generate a professional commercial-grade OSINT PDF intelligence report."""
    from fastapi.responses import Response
    from pdf_export import generate_professional_report

    person = await graph_db.get_person_by_id(req.person_id) or {"name": req.person_id}
    try:
        pivot_data = await pivot_from(graph_db, req.person_id, depth=2, max_per_hop=200)
    except Exception:
        pivot_data = {"hops": []}

    findings = []
    for hop in pivot_data.get("hops", []):
        for node in hop.get("nodes", []):
            findings.append({
                "type": node.get("label", "unknown"),
                "value": node.get("display_name", ""),
                "source": (node.get("props") or {}).get("source", "fieldwork"),
                "confidence": "MEDIUM",
                "timestamp": str((node.get("props") or {}).get("first_seen", "")),
            })

    options = {
        "client_name": req.client_name,
        "case_ref": req.case_ref,
        "redact_sources": req.redact_sources,
    }
    pdf_bytes = await generate_professional_report(
        person, findings, options,
        os.getenv("OLLAMA_URL", "http://ollama:11434"),
        os.getenv("OLLAMA_MODEL", "llama3.2"),
    )
    name_slug = (person.get("name") or req.person_id).replace(" ", "_").lower()
    _audit("ProfessionalReport", req.person_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report_{name_slug}.pdf"'},
    )
