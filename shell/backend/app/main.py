"""
Shell backend — the Runi-style chrome around Fieldwork + future modules.

Exposes:
  GET  /api/shell/health               — liveness
  GET  /api/shell/modules              — registered module list (for the nav)
  GET  /api/shell/config               — public settings the UI needs (URLs etc.)

Module-specific routes are mounted under each module's `prefix`
(e.g. /api/news/feeds, /api/calendar/events) when those modules ship.
For v1 the only module is Fieldwork which keeps its existing backend
at RUNI_API and is loaded into the shell via iframe.
"""
from __future__ import annotations

import logging
import os
from contextlib  import asynccontextmanager
from fastapi     import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from deps        import build_deps, Settings
from registry    import ModuleRegistry
from modules.fieldwork import manifest as fieldwork_manifest
from modules.news      import manifest as news_manifest
from modules.reports   import manifest as reports_manifest
from modules.markets   import manifest as markets_manifest
from modules.agent     import manifest as agent_manifest
from modules.gigs      import manifest as gigs_manifest
from modules.presence  import manifest as presence_manifest
from modules.identity  import manifest as identity_manifest
from modules.quant     import manifest as quant_manifest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("shell")


# ── Registry + deps wired at import time ────────────────────────────────────
# Add future modules here — order matters for dependency resolution.
registry = ModuleRegistry()
registry.register(fieldwork_manifest)
registry.register(news_manifest)
registry.register(reports_manifest)
registry.register(markets_manifest)
registry.register(agent_manifest)
registry.register(gigs_manifest)
registry.register(presence_manifest)
registry.register(identity_manifest)
registry.register(quant_manifest)
# Future modules:
#   from modules.calendar import manifest as cal_manifest
#   registry.register(cal_manifest)
#   from modules.alerts import manifest as alerts_manifest
#   registry.register(alerts_manifest)

deps: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build shared deps on startup, close them on shutdown."""
    global deps
    deps = build_deps()
    log.info("shell deps built — %d modules registered", len(registry.all()))
    registry.bootstrap(app, deps)
    yield
    # Cleanup
    drv = deps.get("graph_db")
    if drv:
        await drv.close()
    http = deps.get("http")
    if http:
        await http.aclose()


app = FastAPI(title="Runi Shell", version="0.1.0", lifespan=lifespan)

# Permissive CORS — both frontends are localhost-bound by docker-compose,
# but the legacy Fieldwork frontend (port 3000) might fetch the shell's
# /api/shell/* endpoints during the iframe-bridge handshake.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Shell-level endpoints ───────────────────────────────────────────────────
@app.get("/api/shell/health")
async def health():
    return {"status": "ok", "modules": len(registry.all())}


@app.get("/api/shell/modules")
async def list_modules():
    """Return module manifests the frontend uses to build the nav."""
    settings: Settings = deps["settings"]
    out = []
    for m in registry.list_json():
        # Inject iframe URL for legacy modules that defer to env config
        if m["kind"] == "iframe" and m["url"] is None and m["id"] == "fieldwork":
            m["url"] = settings.RUNI_FRONT
        out.append(m)
    return {"modules": out, "count": len(out)}


@app.get("/api/shell/config")
async def public_config():
    """Public config the UI may need (no secrets)."""
    s: Settings = deps["settings"]
    return {
        "runi_api":   s.RUNI_API,
        "runi_front": s.RUNI_FRONT,
        "ollama_url":      s.OLLAMA_URL,
        "ollama_model":    s.OLLAMA_MODEL,
    }


# ── Runi voice — local TTS (Piper) + STT (Whisper), proxied for the frontend ──
# Infrastructure, not a nav module: app-level routes (no manifest → no phantom tab).
# The containers are reached by service name on the compose network; nothing here
# calls out to the internet.
PIPER_URL   = os.getenv("PIPER_URL",   "http://piper:5000")
WHISPER_URL = os.getenv("WHISPER_URL", "http://whisper:5000")


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    accent: str | None = None           # e.g. "ru" for a Russian accent
    strength: str | None = None         # accent intensity: light | medium | heavy
    length_scale: float | None = None   # >1 slower, <1 faster


@app.get("/api/voice/health")
async def voice_health():
    http = deps["http"]
    out = {}
    for name, url in (("piper", PIPER_URL), ("whisper", WHISPER_URL)):
        try:
            r = await http.get(f"{url}/health", timeout=5)
            out[name] = r.json() if r.status_code == 200 else {"ok": False, "status": r.status_code}
        except Exception as exc:
            out[name] = {"ok": False, "error": str(exc)}
    return out


@app.get("/api/voice/voices")
async def voice_voices():
    http = deps["http"]
    try:
        r = await http.get(f"{PIPER_URL}/health", timeout=5)
        d = r.json()
        return {"voices": d.get("voices", []), "default": d.get("default")}
    except Exception as exc:
        raise HTTPException(503, f"piper unavailable: {exc}")


async def _piper_tts(payload: dict) -> Response:
    http = deps["http"]
    try:
        r = await http.post(f"{PIPER_URL}/tts", json=payload, timeout=60)
    except Exception as exc:
        raise HTTPException(503, f"piper unavailable: {exc}")
    if r.status_code != 200:
        raise HTTPException(502, f"piper error {r.status_code}")
    return Response(content=r.content, media_type="audio/wav")


@app.post("/api/voice/tts")
async def voice_tts_post(req: TTSRequest):
    return await _piper_tts({k: v for k, v in req.dict().items() if v is not None})


@app.get("/api/voice/tts")
async def voice_tts_get(text: str, voice: str | None = None, accent: str | None = None,
                        strength: str | None = None, length_scale: float | None = None):
    payload: dict = {"text": text}
    if voice:
        payload["voice"] = voice
    if accent:
        payload["accent"] = accent
    if strength:
        payload["strength"] = strength
    if length_scale:
        payload["length_scale"] = length_scale
    return await _piper_tts(payload)


@app.post("/api/voice/stt")
async def voice_stt(file: UploadFile = File(...)):
    http = deps["http"]
    data = await file.read()
    try:
        r = await http.post(
            f"{WHISPER_URL}/stt",
            files={"file": (file.filename or "audio.webm", data, file.content_type or "audio/webm")},
            timeout=120,
        )
    except Exception as exc:
        raise HTTPException(503, f"whisper unavailable: {exc}")
    if r.status_code != 200:
        raise HTTPException(502, f"whisper error {r.status_code}")
    return r.json()
