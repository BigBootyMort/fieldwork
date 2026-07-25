"""Runi voice — local Piper TTS + Whisper STT (offline). Skips if the voice
containers aren't running (they're optional to the core stack)."""
import pytest

from conftest import SHELL_API, get_json


def test_voice_health(client):
    d = get_json(client, SHELL_API, "/api/voice/health")
    assert "piper" in d and "whisper" in d


def test_piper_tts_returns_wav(client):
    r = client.get(SHELL_API + "/api/voice/tts", params={"text": "system check"})
    if r.status_code == 503:
        pytest.skip("piper container not running")
    assert r.status_code == 200, r.text[:200]
    assert r.headers.get("content-type", "").startswith("audio/")
    assert r.content[:4] == b"RIFF", "not a WAV payload"


@pytest.mark.slow
def test_tts_stt_roundtrip(client):
    """Piper speaks a phrase; Whisper must transcribe it back."""
    phrase = "the registrant email is the strongest lead"
    r = client.get(SHELL_API + "/api/voice/tts", params={"text": phrase})
    if r.status_code != 200:
        pytest.skip("piper container not running")
    s = client.post(
        SHELL_API + "/api/voice/stt",
        files={"file": ("rt.wav", r.content, "audio/wav")},
        timeout=120,
    )
    if s.status_code != 200:
        pytest.skip("whisper container not running")
    text = s.json().get("text", "").lower()
    assert "registrant" in text and "email" in text, f"round-trip lost the phrase: {text!r}"
