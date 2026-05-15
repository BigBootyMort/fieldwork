"""
Media analysis: ExifTool metadata extraction, reverse image search,
video keyframe extraction, PDF text extraction, and NER.

Entry points used by the API:

  analyse_media(graph_db, path, filename)
      Run ExifTool on any uploaded file. GPS → Location node.
      For PDFs/Office docs: extract full text, run NER + sentiment.
      Returns structured metadata dict.

  reverse_image_links(image_url)
      Construct browser-openable search URLs for Yandex / Google /
      TinEye / Bing Visual Search.
      Optionally calls SauceNAO if SAUCENAO_KEY is set (150/day free).

  extract_frames_and_search(path, max_frames)
      Use ffmpeg to pull keyframes, then run reverse_image_links on each.
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx

from scraper import extract_pdf_text
from ner_pipeline import process_text

log = logging.getLogger("media")

# File extensions that may contain extractable text (beyond images/video)
_TEXT_EXTRACTABLE = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf"}

# SauceNAO API — free tier: 100 searches/day (no key), 150/day (free account key)
# Get a free key at: https://saucenao.com/user.php?page=register
SAUCENAO_URL = "https://sausei.sauce.nao.io/api.php"
SAUCENAO_KEY = os.getenv("SAUCENAO_KEY", "")


# ── ExifTool ──────────────────────────────────────────────────────────────────

async def analyse_media(graph_db, path: str, filename: str) -> dict:
    """
    Run ExifTool on *path*, extract useful fields, and persist any
    GPS coordinates as a Location node in Neo4j.
    """
    raw = await _run_exiftool(path)
    if not raw:
        return {"filename": filename, "found": False, "reason": "ExifTool produced no output"}

    meta = raw[0] if isinstance(raw, list) else raw

    result = {
        "filename": filename,
        "found": True,
        "file_type": meta.get("FileType", ""),
        "file_size": meta.get("FileSize", ""),
        "create_date": meta.get("CreateDate") or meta.get("DateTimeOriginal") or meta.get("MediaCreateDate", ""),
        "modify_date": meta.get("ModifyDate") or meta.get("FileModifyDate", ""),
        "make": meta.get("Make", ""),
        "model": meta.get("Model", ""),
        "software": meta.get("Software", ""),
        "author": meta.get("Author") or meta.get("Creator") or meta.get("Artist", ""),
        "copyright": meta.get("Copyright", ""),
        "description": meta.get("Description") or meta.get("ImageDescription") or meta.get("Comment", ""),
        "gps_latitude":  meta.get("GPSLatitude"),
        "gps_longitude": meta.get("GPSLongitude"),
        "gps_altitude":  meta.get("GPSAltitude"),
        "gps_location":  None,
        "location_node_created": False,
    }

    # Convert DMS strings to decimal if needed
    lat = _to_decimal(meta.get("GPSLatitude"),  meta.get("GPSLatitudeRef",  "N"))
    lon = _to_decimal(meta.get("GPSLongitude"), meta.get("GPSLongitudeRef", "E"))

    if lat is not None and lon is not None:
        loc_name = f"{lat:.5f}, {lon:.5f}"
        result["gps_location"] = loc_name
        result["gps_latitude"]  = lat
        result["gps_longitude"] = lon

        city    = meta.get("City", "")
        country = meta.get("Country") or meta.get("CountryCode", "")
        if city and country:
            loc_name = f"{city}, {country}"
        elif city:
            loc_name = city
        elif country:
            loc_name = country

        async with graph_db.driver.session() as session:
            from graph import slugify
            await session.run(
                "MERGE (l:Location {id: $id}) "
                "ON CREATE SET l.name = $name, l.lat = $lat, l.lon = $lon, "
                "              l.first_seen = datetime() "
                "ON MATCH  SET l.lat = $lat, l.lon = $lon",
                id=slugify(loc_name), name=loc_name, lat=lat, lon=lon,
            )
        result["location_node_created"] = True
        log.info("ExifTool GPS: %s → Location node %r", loc_name, loc_name)

    # ── Phase 4.2 / 4.3: PDF/document text extraction + NER ──────────────────
    suffix = Path(filename).suffix.lower()
    if suffix in _TEXT_EXTRACTABLE:
        doc_text = await extract_pdf_text(path)
        if doc_text:
            nlp = await process_text(doc_text, translate=False)
            result["document_text_preview"] = doc_text[:500]
            result["language"]        = nlp["language"]
            result["sentiment_score"] = nlp["sentiment_score"]
            result["sentiment_label"] = nlp["sentiment_label"]
            result["entities"]        = nlp["entities"]
            log.info(
                "Document NLP: lang=%s sentiment=%s persons=%d orgs=%d",
                nlp["language"],
                nlp["sentiment_label"],
                len(nlp["entities"].get("persons", [])),
                len(nlp["entities"].get("orgs", [])),
            )
        else:
            result["document_text_preview"] = None

    return result


async def _run_exiftool(path: str) -> Optional[list]:
    """Run exiftool -j on *path*, return parsed JSON."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "exiftool", "-j", "-n", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return json.loads(stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        log.error("exiftool binary not found in PATH")
        return None
    except asyncio.TimeoutError:
        log.warning("exiftool timed out on %s", path)
        return None
    except Exception as e:
        log.warning("exiftool error: %s", e)
        return None


def _to_decimal(value, ref: str = "") -> Optional[float]:
    """Convert ExifTool GPS value to a signed decimal float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        d = float(value)
    elif isinstance(value, str):
        # Could be "51 deg 30' 26.40\" N" or "51.507333"
        m = re.match(r"([\d.]+)\s*deg\s*([\d.]+)'\s*([\d.]+)\"", value)
        if m:
            d = float(m.group(1)) + float(m.group(2)) / 60 + float(m.group(3)) / 3600
        else:
            try:
                d = float(re.sub(r"[^\d.\-]", "", value))
            except ValueError:
                return None
    else:
        return None

    if ref.upper() in ("S", "W"):
        d = -d
    return round(d, 7)


# ── Reverse image search ──────────────────────────────────────────────────────

def reverse_image_links(image_url: str) -> dict:
    """
    Return a dict of named browser-openable URLs for reverse image search.
    All services accept image URLs directly via query string.
    """
    encoded = urllib.parse.quote(image_url, safe="")
    return {
        "yandex":   f"https://yandex.com/images/search?url={encoded}&rpt=imageview",
        "google":   f"https://lens.google.com/uploadbyurl?url={encoded}",
        "tineye":   f"https://tineye.com/search?url={encoded}",
        "bing":     f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{encoded}",
    }


async def saucenao_search(image_url: str) -> list[dict]:
    """
    Call SauceNAO API for a URL-based reverse image search.
    Returns a list of matches with similarity % and source URL.
    Falls back to [] on any error (not a hard dependency).
    """
    params: dict = {
        "db": "999",
        "output_type": "2",
        "numres": "6",
        "url": image_url,
    }
    if SAUCENAO_KEY:
        params["api_key"] = SAUCENAO_KEY

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(SAUCENAO_URL, params=params)
        if resp.status_code != 200:
            log.warning("SauceNAO: HTTP %s", resp.status_code)
            return []
        data = resp.json()
        results = []
        for r in data.get("results", []):
            header = r.get("header", {})
            similarity = float(header.get("similarity", 0))
            if similarity < 60.0:
                continue
            results.append({
                "similarity": similarity,
                "index_name": header.get("index_name", ""),
                "thumbnail":  header.get("thumbnail", ""),
                "urls": list(r.get("data", {}).values())[:3],
            })
        return sorted(results, key=lambda x: -x["similarity"])
    except Exception as e:
        log.warning("SauceNAO error: %s", e)
        return []


# ── Video frame extraction ────────────────────────────────────────────────────

async def extract_frames_and_search(video_path: str, max_frames: int = 8) -> list[dict]:
    """
    Extract up to *max_frames* keyframes from a video with ffmpeg,
    save them as temp JPEGs, run reverse_image_links on each.

    Returns a list of frame result dicts (exif metadata + search links).
    Because the frames are temp files we can't provide stable URLs for
    SauceNAO — we return browser links instead.
    """
    if not await _ffmpeg_available():
        return [{"error": "ffmpeg not found in PATH"}]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Extract one frame every N seconds, up to max_frames
        # Use scene-change detection to pick meaningful frames
        cmd = [
            "ffmpeg", "-i", video_path,
            "-vf", f"select='not(mod(n\\,{_frame_interval(video_path, max_frames)}))',scale=640:-1",
            "-vsync", "vfr",
            "-frames:v", str(max_frames),
            os.path.join(tmpdir, "frame_%03d.jpg"),
            "-y", "-loglevel", "error",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            return [{"error": "ffmpeg timed out"}]

        frame_files = sorted(Path(tmpdir).glob("frame_*.jpg"))
        results = []
        for frame_path in frame_files[:max_frames]:
            meta = await _run_exiftool(str(frame_path)) or [{}]
            results.append({
                "frame": frame_path.name,
                "note": "Upload this frame manually to any reverse-image service below",
                "search_links": {
                    "yandex": "https://yandex.com/images/search",
                    "google": "https://lens.google.com/",
                    "tineye": "https://tineye.com/",
                },
            })
        return results


def _frame_interval(video_path: str, max_frames: int) -> int:
    """Estimate a frame-skip interval so we spread max_frames across the video."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=10,
        )
        total = int(result.stdout.strip())
        return max(1, total // max_frames)
    except Exception:
        return 30  # fallback: every 30 frames


async def _ffmpeg_available() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except FileNotFoundError:
        return False
