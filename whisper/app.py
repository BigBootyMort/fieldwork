"""
Runi ears — local speech-to-text via faster-whisper (no external calls at runtime).

The model is baked into the image at build time, so transcription is fully offline.
The shell backend proxies this at /api/voice/stt; the browser sends a mic recording
(webm/opus from MediaRecorder), which PyAV decodes.
"""
import io
import os
import tempfile

from fastapi import FastAPI, UploadFile, File
from faster_whisper import WhisperModel

MODEL = os.getenv("WHISPER_MODEL", "base.en")
_model = WhisperModel(MODEL, download_root="/models", compute_type="int8")

app = FastAPI(title="Runi Whisper STT")


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL}


@app.post("/stt")
async def stt(file: UploadFile = File(...)):
    data = await file.read()
    # Write to a temp file — most robust across container audio formats (webm/ogg/wav).
    suffix = os.path.splitext(file.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(data)
        path = tf.name
    try:
        segments, info = _model.transcribe(path, vad_filter=True)
        text = " ".join(s.text for s in segments).strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"text": text, "language": getattr(info, "language", None)}
