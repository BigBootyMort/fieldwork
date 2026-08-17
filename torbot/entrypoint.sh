#!/bin/sh
# Start the bundled Tor daemon, wait for it to bootstrap, then launch the wrapper.
# Tor runs as root here (internal-only container) — it warns but works.
set -e

mkdir -p /tmp/tor
tor --SocksPort 9050 --DataDirectory /tmp/tor --Log "notice stdout" &

echo "torbot-entrypoint: waiting for Tor to bootstrap (up to ~90s)..."
for i in $(seq 1 45); do
    if curl -s --socks5-hostname 127.0.0.1:9050 -m 5 https://check.torproject.org/ >/dev/null 2>&1; then
        echo "torbot-entrypoint: Tor is ready (circuit established)."
        break
    fi
    sleep 2
done

# Start the API even if the probe never succeeded — /health reports Tor status,
# and clearnet crawls (--disable-socks5 path) don't need Tor.
cd /srv
exec uvicorn server:app --host 0.0.0.0 --port 7003
