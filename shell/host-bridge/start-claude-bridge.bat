@echo off
REM Start the Claude Code bridge on the host so the Dockerized backend can use
REM your Claude Pro/Max subscription instead of an API key.
REM Keep this window open while you use AI features. Ctrl+C to stop.
cd /d "%~dp0"
echo Starting Claude Code bridge on http://localhost:8088 ...
python claude_bridge.py
pause
