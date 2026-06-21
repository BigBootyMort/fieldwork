#!/usr/bin/env python3
"""
Claude Code bridge — a tiny HOST-side HTTP shim that lets the Dockerized
backend use your Claude Pro/Max subscription instead of an API key.

It wraps the `claude` CLI (Claude Code) in headless print mode:

    claude -p  --output-format json  --max-turns 1  --append-system-prompt <sys>

The user prompt is piped over stdin so large article contexts aren't subject
to command-line length limits. The CLI authenticates with whatever you logged
in with — a Pro/Max subscription needs NO API key.

Run this ON THE HOST (not in Docker):

    python shell/host-bridge/claude_bridge.py
    # or use start-claude-bridge.bat

The backend reaches it at http://host.docker.internal:8088 by default.

Endpoints
    GET  /health    -> {"ok": true, "cli": "<path>", "version": "..."}
    POST /complete  -> {"system","prompt","max_tokens"?,"model"?}
                       returns {"text": "...", "engine": "claude-code"}

Notes / caveats
  * Claude Code's subscription is intended for interactive coding; driving it
    as an app backend is a grey area and rate-limited (Pro resets ~every 5h).
    Fine for single-user, low-volume use. The backend falls back to Ollama if
    this bridge is unreachable, rate-limited, or errors.
  * Requires the `claude` CLI on PATH and a completed `claude` login.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("CLAUDE_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLAUDE_BRIDGE_PORT", "8088"))
TIMEOUT = int(os.environ.get("CLAUDE_BRIDGE_TIMEOUT", "180"))

# Resolve the CLI once. On Windows it's usually claude.cmd / claude.exe.
_CLI = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")

# Run the CLI in a throwaway empty dir so it doesn't scan a real project
# (keeps it fast and avoids any incidental tool use on your files).
_WORKDIR = tempfile.mkdtemp(prefix="claude-bridge-")


def _cli_version() -> str:
    if not _CLI:
        return ""
    try:
        out = subprocess.run([_CLI, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr or "").strip()
    except Exception:
        return ""


def _logged_in() -> bool:
    """Cheap, no-cost auth probe via `claude auth status`."""
    if not _CLI:
        return False
    try:
        out = subprocess.run([_CLI, "auth", "status"], capture_output=True,
                             text=True, timeout=20)
        return bool(json.loads(out.stdout or "{}").get("loggedIn"))
    except Exception:
        return False


def _run_claude(system: str, prompt: str, model: str | None) -> str:
    """Invoke `claude -p` once and return the plain-text result."""
    if not _CLI:
        raise RuntimeError("claude CLI not found on PATH — install Claude Code and log in")

    cmd = [_CLI, "-p", "--output-format", "json", "--max-turns", "1"]
    if system:
        cmd += ["--append-system-prompt", system]
    if model:
        cmd += ["--model", model]

    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        cwd=_WORKDIR,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(f"claude exited {proc.returncode}: {err}")

    raw = (proc.stdout or "").strip()
    # --output-format json returns a single JSON object with a `result` field.
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            text = obj.get("result") or obj.get("text") or ""
            if obj.get("is_error"):
                raise RuntimeError(f"claude reported error: {str(text)[:300]}")
            return str(text).strip()
    except json.JSONDecodeError:
        pass
    # Fall back to raw stdout if it wasn't JSON for some reason.
    return raw


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # quiet
        pass

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            logged_in = _logged_in()
            self._send(200, {
                "ok": bool(_CLI) and logged_in,
                "cli": _CLI or "",
                "version": _cli_version(),
                "logged_in": logged_in,
                "hint": None if logged_in else
                        "Run `claude setup-token` (or `claude auth login`) once to authenticate "
                        "the CLI with your Claude subscription.",
            })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/complete":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            self._send(400, {"error": f"bad request: {exc}"})
            return

        prompt = (data.get("prompt") or "").strip()
        system = (data.get("system") or "").strip()
        model = data.get("model") or None
        if not prompt:
            self._send(400, {"error": "prompt required"})
            return

        try:
            text = _run_claude(system, prompt, model)
            self._send(200, {"text": text, "engine": "claude-code"})
        except subprocess.TimeoutExpired:
            self._send(504, {"error": f"claude timed out after {TIMEOUT}s"})
        except Exception as exc:
            self._send(503, {"error": str(exc)})


def main():
    if not _CLI:
        print("WARNING: `claude` CLI not found on PATH. Install Claude Code and run "
              "`claude` once to log in. The bridge will report unhealthy until then.",
              file=sys.stderr)
    else:
        print(f"claude CLI: {_CLI}  ({_cli_version()})")
        if _logged_in():
            print("auth: logged in [OK]  (using your Claude subscription)")
        else:
            print("auth: NOT logged in [X]  -- run this once, then restart the bridge:")
            print("        claude setup-token        (long-lived token, recommended)")
            print("     or claude auth login         (interactive browser sign-in)")
    print(f"Claude Code bridge listening on http://{HOST}:{PORT}  "
          f"(backend uses http://host.docker.internal:{PORT})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
