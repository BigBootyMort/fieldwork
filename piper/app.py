"""
Runi Piper TTS — local neural text-to-speech (no external calls at runtime).

Voice models are baked into the image at build time (Dockerfile ADDs them from
HuggingFace), so synthesis is fully offline. The shell backend proxies this at
/api/voice/tts; nothing is exposed to the browser directly except via that proxy
(plus a localhost audition port in compose).
"""
import io
import os
import wave

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
from piper import PiperVoice

VOICES_DIR = os.getenv("VOICES_DIR", "/voices")
DEFAULT = os.getenv("PIPER_VOICE", "en_GB-jenny_dioco-medium")
_cache: dict = {}


def _voice(name: str | None):
    name = name or DEFAULT
    if name not in _cache:
        path = os.path.join(VOICES_DIR, name + ".onnx")
        if not os.path.exists(path):
            raise FileNotFoundError(f"voice not installed: {name}")
        _cache[name] = PiperVoice.load(path)
    return _cache[name]


def _installed() -> list[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx"))
    except FileNotFoundError:
        return []


def _synth(text: str, voice: str | None, length_scale: float | None) -> bytes:
    v = _voice(voice)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        kw = {}
        if length_scale:
            kw["length_scale"] = length_scale
        v.synthesize(text, wf, **kw)
    return buf.getvalue()


app = FastAPI(title="Runi Piper TTS")


@app.get("/health")
def health():
    return {"ok": True, "default": DEFAULT, "voices": _installed()}


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    length_scale: float | None = None  # >1 slower, <1 faster


@app.post("/tts")
def tts_post(req: TTSRequest):
    return Response(_synth(req.text, req.voice, req.length_scale), media_type="audio/wav")


@app.get("/tts")
def tts_get(text: str, voice: str | None = None, length_scale: float | None = None):
    """Handy for auditioning in a browser: /tts?text=hello&voice=en_GB-alba-medium"""
    return Response(_synth(text, voice, length_scale), media_type="audio/wav")
