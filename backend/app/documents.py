"""
Fieldwork Phase 17 — Document ingestion pipeline.

Upload any supported file to a case.  The pipeline:
  1. Hash the file  (MD5, SHA-1, SHA-256)
  2. Identify MIME type
  3. Extract text content and document metadata
  4. Extract EXIF + GPS for images
  5. Run spaCy NER on extracted text
  6. MERGE a Document node into Neo4j (same file → same node)
  7. Create (Case)-[:HAS_DOCUMENT]->(Document) relationship

Supported formats
-----------------
  PDF              — text + info dict (author, dates, title, page count)
  DOCX             — text + core properties
  TXT / MD / CSV   — raw text
  EML (email)      — subject + plain-text body
  JPEG / PNG / TIFF / WebP — EXIF metadata + GPS coordinates
"""
from __future__ import annotations

import email as _email_module
import hashlib
import json
import logging
import mimetypes
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_VT_API = "https://www.virustotal.com/api/v3"

# Max text stored in Neo4j (characters)
_TEXT_LIMIT = 50_000

# Supported MIME types
_MIME_TEXT_TYPES = {
    "text/plain", "text/csv", "text/markdown",
    "text/x-markdown", "text/x-rst",
}

# ── Magic-byte MIME detection ─────────────────────────────────────────────────

_MAGIC: list[tuple[bytes, str]] = [
    (b"%PDF",             "application/pdf"),
    (b"\xff\xd8\xff",     "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n","image/png"),
    (b"II*\x00",          "image/tiff"),
    (b"MM\x00*",          "image/tiff"),
    (b"RIFF",             "image/webp"),   # may also be WAV — checked below
    (b"PK\x03\x04",       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (b"\xd0\xcf\x11\xe0", "application/msword"),
    (b"From ",            "message/rfc822"),
    (b"Return-Path:",     "message/rfc822"),
    (b"MIME-Version:",    "message/rfc822"),
]


def _guess_mime(filename: str, data: bytes) -> str:
    mt, _ = mimetypes.guess_type(filename)
    if mt:
        return mt
    for sig, mime in _MAGIC:
        if data[:len(sig)] == sig:
            # RIFF could be WebP or WAV — check further
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return "application/octet-stream"


# ── Hashing ───────────────────────────────────────────────────────────────────

def _hash_file(data: bytes) -> dict[str, str]:
    return {
        "md5":    hashlib.md5(data).hexdigest(),
        "sha1":   hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


# ── PDF extraction ────────────────────────────────────────────────────────────

def _extract_pdf(data: bytes) -> dict:
    result = {"text": "", "page_count": 0, "metadata": {}, "error": None}
    try:
        from pdfminer.high_level import extract_text as _extract_text
        result["text"] = _extract_text(BytesIO(data)) or ""
    except Exception as exc:
        result["error"] = f"text: {exc}"

    try:
        from pdfminer.pdfparser  import PDFParser
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfpage    import PDFPage

        buf = BytesIO(data)
        parser = PDFParser(buf)
        pdfdoc  = PDFDocument(parser)

        if pdfdoc.info:
            for k, v in pdfdoc.info[0].items():
                try:
                    result["metadata"][k] = (
                        v.decode("utf-8", errors="replace") if isinstance(v, bytes) else str(v)
                    )
                except Exception:
                    pass

        buf.seek(0)
        result["page_count"] = sum(1 for _ in PDFPage.get_pages(BytesIO(data)))
    except Exception as exc:
        result["error"] = (result["error"] or "") + f" meta: {exc}"

    return result


# ── DOCX extraction ───────────────────────────────────────────────────────────

def _extract_docx(data: bytes) -> dict:
    try:
        from docx import Document as _DocxDoc
        doc = _DocxDoc(BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
        cp   = doc.core_properties
        meta = {
            k: str(getattr(cp, k, "") or "")
            for k in ("author", "title", "subject", "created", "modified")
        }
        return {"text": text, "metadata": meta, "error": None}
    except ImportError:
        return {"text": "", "metadata": {}, "error": "python-docx not installed"}
    except Exception as exc:
        return {"text": "", "metadata": {}, "error": str(exc)}


# ── Plain-text extraction ─────────────────────────────────────────────────────

def _extract_text_file(data: bytes) -> dict:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return {"text": data.decode(enc), "metadata": {}, "error": None}
        except UnicodeDecodeError:
            continue
    return {"text": data.decode("utf-8", errors="replace"), "metadata": {}, "error": None}


# ── EML extraction ────────────────────────────────────────────────────────────

def _extract_eml(data: bytes) -> dict:
    try:
        msg = _email_module.message_from_bytes(data)
        subject  = msg.get("Subject", "")
        from_hdr = msg.get("From",    "")
        date_hdr = msg.get("Date",    "")
        body     = ""

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    raw = part.get_payload(decode=True)
                    if raw:
                        body += raw.decode(errors="replace")
        else:
            raw = msg.get_payload(decode=True)
            if raw:
                body = raw.decode(errors="replace")

        text = f"Subject: {subject}\nFrom: {from_hdr}\nDate: {date_hdr}\n\n{body}"
        meta = {"subject": subject, "from": from_hdr, "date": date_hdr}
        return {"text": text, "metadata": meta, "error": None}
    except Exception as exc:
        return {"text": "", "metadata": {}, "error": str(exc)}


# ── EXIF extraction ───────────────────────────────────────────────────────────

def _dms_to_decimal(dms, ref: str) -> Optional[float]:
    """Convert EXIF GPS DMS tuple to decimal degrees."""
    try:
        d, m, s = float(dms[0]), float(dms[1]), float(dms[2])
        result  = d + m / 60 + s / 3600
        if str(ref).strip().upper() in ("S", "W"):
            result = -result
        return round(result, 7)
    except Exception:
        return None


def _extract_exif(data: bytes, mime: str) -> dict:
    if not mime.startswith("image/"):
        return {}
    try:
        from PIL import Image, ExifTags

        img  = Image.open(BytesIO(data))
        exif = img.getexif()
        if not exif:
            return {}

        named = {
            ExifTags.TAGS.get(tag_id, tag_id): val
            for tag_id, val in exif.items()
        }

        result: dict = {}

        # Camera make / model / datetime
        for field, key in (("Make", "camera_make"), ("Model", "camera_model"),
                           ("Software", "software")):
            if field in named:
                result[key] = str(named[field]).strip()

        raw_dt = named.get("DateTime") or named.get("DateTimeOriginal")
        if raw_dt:
            try:
                dt = datetime.strptime(str(raw_dt), "%Y:%m:%d %H:%M:%S")
                result["datetime"] = dt.isoformat()
            except ValueError:
                result["datetime"] = str(raw_dt)

        # GPS
        try:
            gps_ifd = exif.get_ifd(0x8825)   # GPSInfo IFD tag id
            if gps_ifd:
                lat_dms = gps_ifd.get(2)   # GPSLatitude
                lat_ref = gps_ifd.get(1)   # GPSLatitudeRef
                lng_dms = gps_ifd.get(4)   # GPSLongitude
                lng_ref = gps_ifd.get(3)   # GPSLongitudeRef

                lat = _dms_to_decimal(lat_dms, lat_ref) if lat_dms else None
                lng = _dms_to_decimal(lng_dms, lng_ref) if lng_dms else None

                if lat is not None and lng is not None:
                    result["gps_lat"] = lat
                    result["gps_lng"] = lng

                alt_raw = gps_ifd.get(6)   # GPSAltitude
                if alt_raw is not None:
                    try:
                        result["gps_alt"] = round(float(alt_raw), 1)
                    except Exception:
                        pass
        except Exception as exc:
            log.debug("GPS extraction error: %s", exc)

        return result

    except ImportError:
        log.warning("Pillow not installed — EXIF extraction disabled")
        return {}
    except Exception as exc:
        log.debug("EXIF extraction failed: %s", exc)
        return {}


# ── spaCy NER on extracted text ───────────────────────────────────────────────

def _run_ner(text: str) -> dict[str, list[str]]:
    """Run spaCy NER and return deduplicated entity lists."""
    if not text or not text.strip():
        return {"persons": [], "orgs": [], "locations": []}
    try:
        from ner_pipeline import extract_entities as _ner_extract
        result = _ner_extract(text[:80_000])
        return {
            "persons":   list(dict.fromkeys(result.get("persons",   []))),
            "orgs":      list(dict.fromkeys(result.get("orgs",      []))),
            "locations": list(dict.fromkeys(result.get("locations", []))),
        }
    except Exception as exc:
        log.warning("NER on document failed: %s", exc)
        return {"persons": [], "orgs": [], "locations": []}


# ── Dispatcher ────────────────────────────────────────────────────────────────

def _extract_content(filename: str, data: bytes, mime: str) -> dict:
    """Extract text, metadata, and EXIF from a file based on its MIME type."""
    extracted: dict = {
        "text":       "",
        "metadata":   {},
        "exif":       {},
        "page_count": 0,
        "error":      None,
    }

    if mime == "application/pdf":
        r = _extract_pdf(data)
        extracted.update(r)

    elif mime in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        r = _extract_docx(data)
        extracted.update(r)

    elif mime in _MIME_TEXT_TYPES or mime.startswith("text/"):
        r = _extract_text_file(data)
        extracted.update(r)

    elif mime == "message/rfc822":
        r = _extract_eml(data)
        extracted.update(r)

    elif mime.startswith("image/"):
        extracted["exif"] = _extract_exif(data, mime)

    else:
        extracted["error"] = f"Unsupported format: {mime}"

    return extracted


# ── Document ingestion (public API) ──────────────────────────────────────────

async def ingest_document(
    graph_db,
    case_id:  str,
    filename: str,
    data:     bytes,
    mime:     Optional[str] = None,
) -> dict:
    """
    Process a file upload and store a Document node linked to case_id.

    Returns the complete document dict.
    Raises ValueError on validation errors.
    """
    if len(data) > 50 * 1024 * 1024:
        raise ValueError("File too large (max 50 MB)")
    if not filename or len(filename) > 500:
        raise ValueError("Invalid filename")

    mime = mime or _guess_mime(filename, data)
    hashes   = _hash_file(data)
    doc_id   = f"doc_{hashes['sha256'][:20]}"
    content  = _extract_content(filename, data, mime)
    entities = _run_ner(content["text"])
    exif     = content.get("exif", {})

    text_full   = content.get("text", "") or ""
    text_stored = text_full[:_TEXT_LIMIT]

    now_iso = datetime.now(timezone.utc).isoformat()

    doc: dict = {
        "id":           doc_id,
        "filename":     filename,
        "mime_type":    mime,
        "size_bytes":   len(data),
        "sha256":       hashes["sha256"],
        "md5":          hashes["md5"],
        "sha1":         hashes["sha1"],
        "text":         text_stored,
        "text_length":  len(text_full),
        "page_count":   content.get("page_count") or 0,
        # Document metadata
        "doc_author":         content.get("metadata", {}).get("Author") or
                              content.get("metadata", {}).get("author") or "",
        "doc_title":          content.get("metadata", {}).get("Title") or
                              content.get("metadata", {}).get("title") or "",
        "doc_creator":        content.get("metadata", {}).get("Creator") or "",
        "doc_created_date":   content.get("metadata", {}).get("CreationDate") or
                              content.get("metadata", {}).get("created") or "",
        "doc_subject":        content.get("metadata", {}).get("Subject") or
                              content.get("metadata", {}).get("subject") or "",
        "email_from":         content.get("metadata", {}).get("from") or "",
        # EXIF
        "exif_datetime":     exif.get("datetime", ""),
        "exif_camera_make":  exif.get("camera_make", ""),
        "exif_camera_model": exif.get("camera_model", ""),
        "exif_lat":          exif.get("gps_lat"),
        "exif_lng":          exif.get("gps_lng"),
        "exif_alt":          exif.get("gps_alt"),
        # NER entities (stored as JSON for easy frontend consumption)
        "ner_persons":   entities["persons"][:50],
        "ner_orgs":      entities["orgs"][:50],
        "ner_locations": entities["locations"][:50],
        # VT (populated later via /document/{id}/vt)
        "vt_status":     "unchecked",
        "vt_detections": 0,
        "vt_total":      0,
        # Extraction status
        "extraction_error": content.get("error") or "",
        "uploaded_at":      now_iso,
    }

    # Persist to Neo4j — MERGE on doc_id so re-uploading the same file is safe
    async with graph_db.driver.session() as session:
        # 1. MERGE the Document node
        await session.run(
            """
            MERGE (d:Document {id: $id})
            ON CREATE SET
                d.filename          = $filename,
                d.mime_type         = $mime_type,
                d.size_bytes        = $size_bytes,
                d.sha256            = $sha256,
                d.md5               = $md5,
                d.sha1              = $sha1,
                d.text              = $text,
                d.text_length       = $text_length,
                d.page_count        = $page_count,
                d.doc_author        = $doc_author,
                d.doc_title         = $doc_title,
                d.doc_creator       = $doc_creator,
                d.doc_created_date  = $doc_created_date,
                d.doc_subject       = $doc_subject,
                d.email_from        = $email_from,
                d.exif_datetime     = $exif_datetime,
                d.exif_camera_make  = $exif_camera_make,
                d.exif_camera_model = $exif_camera_model,
                d.exif_lat          = $exif_lat,
                d.exif_lng          = $exif_lng,
                d.exif_alt          = $exif_alt,
                d.ner_persons       = $ner_persons,
                d.ner_orgs          = $ner_orgs,
                d.ner_locations     = $ner_locations,
                d.vt_status         = $vt_status,
                d.vt_detections     = $vt_detections,
                d.vt_total          = $vt_total,
                d.extraction_error  = $extraction_error,
                d.uploaded_at       = $uploaded_at
            """,
            **{k: v for k, v in doc.items() if k not in ("id",)},
            id=doc_id,
        )
        # 2. Link to case
        await session.run(
            """
            MATCH (c:Case {id: $cid}), (d:Document {id: $did})
            MERGE (c)-[:HAS_DOCUMENT]->(d)
            """,
            cid=case_id, did=doc_id,
        )

    return doc


async def list_documents(graph_db, case_id: str) -> list[dict]:
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (:Case {id: $cid})-[:HAS_DOCUMENT]->(d:Document)
            RETURN d {.*} AS d
            ORDER BY d.uploaded_at DESC
            """,
            cid=case_id,
        )
        return [dict(r["d"]) async for r in result]


async def get_document(graph_db, doc_id: str) -> Optional[dict]:
    async with graph_db.driver.session() as session:
        result = await session.run(
            "MATCH (d:Document {id: $id}) RETURN d {.*} AS d LIMIT 1",
            id=doc_id,
        )
        rec = await result.single()
        return dict(rec["d"]) if rec else None


async def remove_document(graph_db, case_id: str, doc_id: str) -> bool:
    """Remove the HAS_DOCUMENT link. The Document node is kept (may be shared)."""
    async with graph_db.driver.session() as session:
        result = await session.run(
            """
            MATCH (:Case {id: $cid})-[r:HAS_DOCUMENT]->(d:Document {id: $did})
            DELETE r
            RETURN count(r) AS n
            """,
            cid=case_id, did=doc_id,
        )
        rec = await result.single()
        return bool(rec and rec["n"] > 0)


# ── VirusTotal hash check ─────────────────────────────────────────────────────

async def check_vt_hash(graph_db, doc_id: str, sha256: str) -> dict:
    """Check a SHA-256 hash against VirusTotal and update the Document node."""
    api_key = os.getenv("VIRUSTOTAL_API_KEY", "")
    if not api_key:
        return {"status": "unchecked", "reason": "No VIRUSTOTAL_API_KEY configured"}

    result_data: dict = {"status": "unknown", "detections": 0, "total": 0}
    try:
        async with httpx.AsyncClient(
            timeout=20,
            headers={"x-apikey": api_key, "User-Agent": "fieldwork-osint/0.3"},
        ) as client:
            r = await client.get(f"{_VT_API}/files/{sha256}")

        if r.status_code == 404:
            result_data = {"status": "not_found", "detections": 0, "total": 0}
        elif r.status_code == 200:
            stats    = r.json().get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
            malicious  = stats.get("malicious", 0)
            suspicious = stats.get("suspicious", 0)
            total      = sum(stats.values())
            if malicious > 0:
                status = "malicious"
            elif suspicious > 0:
                status = "suspicious"
            else:
                status = "clean"
            result_data = {"status": status, "detections": malicious + suspicious, "total": total}
        else:
            result_data = {"status": "error", "reason": f"VT returned HTTP {r.status_code}"}
    except Exception as exc:
        result_data = {"status": "error", "reason": str(exc)}

    # Persist VT result back to the Document node
    checked_at = datetime.now(timezone.utc).isoformat()
    async with graph_db.driver.session() as session:
        await session.run(
            """
            MATCH (d:Document {id: $id})
            SET d.vt_status     = $status,
                d.vt_detections = $detections,
                d.vt_total      = $total,
                d.vt_checked_at = $checked_at
            """,
            id=doc_id,
            status=result_data["status"],
            detections=result_data.get("detections", 0),
            total=result_data.get("total", 0),
            checked_at=checked_at,
        )

    return result_data
