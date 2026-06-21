"""
Claude-vision image intelligence.

Upload a photo → extract EXIF (incl. GPS via ExifTool), then ask Claude's
vision model to geolocate it from visual cues: landmarks, signage, language,
architecture, vegetation, vehicles/plates, time/season — and cross-check its
estimate against any EXIF GPS.

Requires ANTHROPIC_API_KEY (image analysis goes through the Claude API; the
text-only subscription bridge can't process images). Without a key it returns
EXIF + a clear note so the feature degrades honestly.
"""
from __future__ import annotations

import base64
import logging
import mimetypes

import httpx

from llm_bridge import call_claude_api_vision, NoClaudeError, api_configured
from media import analyse_media

log = logging.getLogger("fieldwork.vision_intel")

_SYSTEM = ("You are an expert OSINT image-geolocation analyst. You reason "
           "carefully from visual evidence and never fabricate certainty.")

_PROMPT = """\
Analyse this image for OSINT. Use these markdown sections:

## Location estimate
Your best guess of where this was taken (country → region → specific place if
possible) and the visual evidence for it. Give a confidence (high/medium/low).

## Landmarks & signage
Identifiable buildings, monuments, signs, business names, languages/scripts.

## Text read
Any legible text, license plates, numbers (transcribe what you can).

## Notable objects & people
Vehicles, uniforms, flags, equipment — anything investigatively useful.

## Time & conditions
Season, time-of-day, weather cues.

## Caveats
What is uncertain or could mislead.

Be specific and grounded in what is actually visible."""

_OK_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


async def analyze_image(graph_db, path: str, filename: str,
                        http: httpx.AsyncClient | None = None) -> dict:
    """EXIF + Claude-vision geolocation analysis of an uploaded image."""
    # 1. EXIF (reuses ExifTool pipeline; persists a Location node on GPS)
    try:
        exif = await analyse_media(graph_db, path, filename)
    except Exception as exc:
        exif = {"found": False, "reason": str(exc)}

    lat = exif.get("gps_latitude")
    lon = exif.get("gps_longitude")
    gps = {"lat": lat, "lon": lon} if (lat is not None and lon is not None) else None

    exif_subset = {k: exif.get(k) for k in (
        "file_type", "make", "model", "software", "create_date",
        "gps_latitude", "gps_longitude", "gps_location") if exif.get(k)}

    # 2. Vision analysis (Claude API)
    ai, engine, note = None, None, None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as exc:
        return {"filename": filename, "exif": exif_subset, "gps": gps,
                "ai_analysis": None, "engine": None, "note": f"read failed: {exc}"}

    if len(data) > 5 * 1024 * 1024:
        note = "Image > 5 MB — Claude may reject it; consider resizing."

    if not api_configured():
        note = ("AI vision needs an ANTHROPIC_API_KEY (image analysis uses the "
                "Claude API; the text-only subscription bridge can't see images). "
                "EXIF is shown below.")
    else:
        b64 = base64.b64encode(data).decode()
        mt = mimetypes.guess_type(filename)[0] or "image/jpeg"
        if mt not in _OK_TYPES:
            mt = "image/jpeg"
        prompt = _PROMPT + (
            f"\n\nNOTE: the image's EXIF GPS is {gps['lat']}, {gps['lon']} — "
            "cross-check your visual estimate against this and flag agreement or conflict."
            if gps else
            "\n\nThere is no EXIF GPS — estimate purely from visual cues."
        )
        client = http or httpx.AsyncClient(timeout=120)
        try:
            ai = await call_claude_api_vision(
                image_b64=b64, media_type=mt, prompt=prompt,
                system=_SYSTEM, http=client, max_tokens=1500,
            )
            engine = "claude"
        except NoClaudeError as exc:
            note = str(exc)
        except Exception as exc:
            note = f"vision analysis failed: {exc}"
        finally:
            if not http:
                await client.aclose()

    return {"filename": filename, "exif": exif_subset, "gps": gps,
            "ai_analysis": ai, "engine": engine, "note": note}
