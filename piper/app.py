"""
Runi Piper TTS.

- Clean voice (default): the official Piper binary (bundled espeak-ng-data →
  correct pronunciation).
- Accented voice (accent=ru): a custom phoneme-rewrite path — phonemize English
  with espeak-ng, apply Russian-accent substitution rules to the IPA, then run
  the voice's ONNX model directly. This gives English-with-a-Russian-accent that
  stays intelligible, entirely offline, and fully under our control.
"""
import io
import json
import os
import subprocess
import tempfile
import wave

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel

VOICES_DIR = os.getenv("VOICES_DIR", "/voices")
DEFAULT = os.getenv("PIPER_VOICE", "en_GB-alba-medium")
PIPER_BIN = "/opt/piper/piper"


def _installed() -> list[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx"))
    except FileNotFoundError:
        return []


# ── Clean voice: official binary ──────────────────────────────────────────────
def _synth_clean(text: str, voice: str, length_scale: float | None) -> bytes:
    model = os.path.join(VOICES_DIR, voice + ".onnx")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        out = tf.name
    try:
        cmd = [PIPER_BIN, "--model", model, "--output_file", out]
        if length_scale:
            cmd += ["--length_scale", str(length_scale)]
        proc = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise HTTPException(500, "piper: " + proc.stderr.decode(errors="ignore")[-300:])
        with open(out, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


# ── Accent path: espeak IPA → rewrite → ONNX ──────────────────────────────────
_sessions: dict = {}
_configs: dict = {}


def _load(voice: str):
    if voice not in _sessions:
        onnx = os.path.join(VOICES_DIR, voice + ".onnx")
        _sessions[voice] = ort.InferenceSession(onnx, providers=["CPUExecutionProvider"])
        _configs[voice] = json.load(open(onnx + ".json"))
    return _sessions[voice], _configs[voice]


def _phonemize(text: str, espeak_voice: str) -> str:
    """English → IPA via espeak-ng (matches the phoneme set Piper models expect)."""
    proc = subprocess.run(
        # --path points espeak-ng at the complete data bundled with the Piper binary
        # (the apt package's data dir isn't where its binary looks).
        ["espeak-ng", "--path=/opt/piper", "-q", "--ipa", "-v", espeak_voice, text],
        capture_output=True, text=True, timeout=30,
    )
    # espeak prints one line per sentence; join with a space separator.
    return " ".join(line.strip() for line in proc.stdout.splitlines() if line.strip())


# Russian-accent phoneme substitutions on English IPA.
_RU_SUB = {
    "w": "v",     # world → vorld
    "θ": "t",     # think → tink   (no θ in Russian)
    "ð": "d",     # this → dis
    "h": "x",     # hello → khello (Russian h is velar χ; espeak uses 'x')
    "ɹ": "r",     # English approximant r → trilled r
    "æ": "ɛ",     # cat → ket
    "ŋ": "n",     # -ing → -in
    "ɡ": "ɡ",
}
# Final devoicing (Russian devoices word-final voiced obstruents).
_DEVOICE = {"b": "p", "d": "t", "ɡ": "k", "v": "f", "z": "s", "ʒ": "ʃ", "dʒ": "tʃ"}
_VOWELS = set("aeiouɪʊɛɔæəɐɑɒʌɜɘɵiːuːɔːɑːɜː")


def _russify(ipa: str) -> str:
    out_words = []
    for word in ipa.split(" "):
        chars = [_RU_SUB.get(c, c) for c in word]
        # devoice the final consonant of the word
        for i in range(len(chars) - 1, -1, -1):
            c = chars[i]
            if c in "ˈˌːˑ .,!?;:":   # skip stress/length/punct markers
                continue
            if c in _DEVOICE:
                chars[i] = _DEVOICE[c]
            break
        out_words.append("".join(chars))
    return " ".join(out_words)


def _ids(ipa: str, pim: dict) -> list[int]:
    pad = pim["_"][0]
    ids = [pim["^"][0], pad]           # BOS, pad
    for ch in ipa:
        if ch in pim:
            ids.append(pim[ch][0])
            ids.append(pad)
    ids.append(pim["$"][0])            # EOS
    return ids


def _synth_accent(text: str, voice: str, accent: str, length_scale: float | None) -> bytes:
    sess, cfg = _load(voice)
    pim = cfg["phoneme_id_map"]
    ipa = _phonemize(text, cfg.get("espeak", {}).get("voice", "en-us"))
    if accent == "ru":
        ipa = _russify(ipa)
    ids = _ids(ipa, pim)

    inf = cfg.get("inference", {})
    scales = np.array(
        [inf.get("noise_scale", 0.667),
         length_scale or inf.get("length_scale", 1.0),
         inf.get("noise_w", 0.8)],
        dtype=np.float32,
    )
    inputs = {
        "input": np.array([ids], dtype=np.int64),
        "input_lengths": np.array([len(ids)], dtype=np.int64),
        "scales": scales,
    }
    # single-speaker models have no 'sid'; add it only if the graph wants it
    names = {i.name for i in sess.get_inputs()}
    if "sid" in names:
        inputs["sid"] = np.array([0], dtype=np.int64)

    audio = sess.run(None, inputs)[0].squeeze()
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(cfg["audio"]["sample_rate"])
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _synth(text: str, voice: str | None, accent: str | None, length_scale: float | None) -> bytes:
    voice = voice or DEFAULT
    if not os.path.exists(os.path.join(VOICES_DIR, voice + ".onnx")):
        raise HTTPException(400, f"voice not installed: {voice}")
    if accent and accent != "none":
        return _synth_accent(text, voice, accent, length_scale)
    return _synth_clean(text, voice, length_scale)


app = FastAPI(title="Runi Piper TTS")


@app.get("/health")
def health():
    return {"ok": True, "default": DEFAULT, "voices": _installed(), "accents": ["ru"]}


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    accent: str | None = None
    length_scale: float | None = None


@app.post("/tts")
def tts_post(req: TTSRequest):
    return Response(_synth(req.text, req.voice, req.accent, req.length_scale), media_type="audio/wav")


@app.get("/tts")
def tts_get(text: str, voice: str | None = None, accent: str | None = None, length_scale: float | None = None):
    return Response(_synth(text, voice, accent, length_scale), media_type="audio/wav")


@app.get("/audition", response_class=HTMLResponse)
def audition():
    voices = _installed()
    sample = "Runi online. Three breaches, one sanctions hit. I would pivot on the registrant email first."
    q = sample.replace(" ", "%20")
    blocks = []
    for v in voices:
        tag = " — default" if v == DEFAULT else ""
        blocks.append(
            f'<div style="margin:16px 0"><b>{v}</b>{tag}<br>'
            f'<div style="color:#5f6a97;font-size:.8rem">clean</div>'
            f'<audio controls preload="none" src="/tts?voice={v}&text={q}"></audio><br>'
            f'<div style="color:#ff2e97;font-size:.8rem;margin-top:4px">russian accent</div>'
            f'<audio controls preload="none" src="/tts?voice={v}&accent=ru&text={q}"></audio></div>'
        )
    return (
        '<body style="background:#04050a;color:#d7e3ff;font-family:monospace;padding:28px;max-width:680px">'
        '<h2 style="color:#18e0ff">RUNI // VOICE AUDITION</h2>'
        f'<p style="color:#5f6a97">Sample: “{sample}”</p>' + "".join(blocks) + "</body>"
    )
