"""
Runi Piper TTS — local neural text-to-speech via the official Piper binary
(bundled espeak-ng-data → correct pronunciation), fully offline at runtime.

The shell backend proxies this at /api/voice/tts; compose also maps a localhost
audition port (5051). GET /audition serves a tiny A/B page for both voices.
"""
import os
import subprocess
import tempfile

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel

VOICES_DIR = os.getenv("VOICES_DIR", "/voices")
DEFAULT = os.getenv("PIPER_VOICE", "en_GB-jenny_dioco-medium")
PIPER_BIN = "/opt/piper/piper"


def _installed() -> list[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx"))
    except FileNotFoundError:
        return []


def _synth(text: str, voice: str | None, length_scale: float | None) -> bytes:
    model = os.path.join(VOICES_DIR, (voice or DEFAULT) + ".onnx")
    if not os.path.exists(model):
        raise HTTPException(400, f"voice not installed: {voice or DEFAULT}")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        out = tf.name
    try:
        cmd = [PIPER_BIN, "--model", model, "--output_file", out]
        if length_scale:
            cmd += ["--length_scale", str(length_scale)]
        proc = subprocess.run(cmd, input=text.encode("utf-8"),
                              capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise HTTPException(500, "piper: " + proc.stderr.decode(errors="ignore")[-300:])
        with open(out, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


app = FastAPI(title="Runi Piper TTS")


@app.get("/health")
def health():
    return {"ok": True, "default": DEFAULT, "voices": _installed()}


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    length_scale: float | None = None


@app.post("/tts")
def tts_post(req: TTSRequest):
    return Response(_synth(req.text, req.voice, req.length_scale), media_type="audio/wav")


@app.get("/tts")
def tts_get(text: str, voice: str | None = None, length_scale: float | None = None):
    return Response(_synth(text, voice, length_scale), media_type="audio/wav")


@app.get("/audition", response_class=HTMLResponse)
def audition():
    """A/B every installed voice from one page — no query-string fiddling."""
    voices = _installed()
    sample = "Runi online. Three breaches, one sanctions hit. I would pivot on the registrant email first."
    rows = "".join(
        f'<div style="margin:14px 0"><b>{v}</b>{" — default" if v==DEFAULT else ""}<br>'
        f'<audio controls preload="none" src="/tts?voice={v}&text={sample.replace(" ","%20")}"></audio></div>'
        for v in voices
    )
    return (
        '<body style="background:#04050a;color:#d7e3ff;font-family:monospace;padding:28px;max-width:640px">'
        '<h2 style="color:#18e0ff">RUNI // VOICE AUDITION</h2>'
        f'<p style="color:#5f6a97">Sample: “{sample}”</p>{rows}</body>'
    )
