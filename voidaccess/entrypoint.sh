#!/bin/sh
# Start the bundled Tor daemon, wait for it to bootstrap, then launch the wrapper.
# Tor runs as root here (internal-only container) — it warns but works.
set -e

mkdir -p /tmp/tor /data
tor --SocksPort 9050 --DataDirectory /tmp/tor --Log "notice stdout" &

echo "voidaccess-entrypoint: waiting for Tor to bootstrap (up to ~90s)..."
for i in $(seq 1 45); do
    if curl -s --socks5-hostname 127.0.0.1:9050 -m 5 https://check.torproject.org/ >/dev/null 2>&1; then
        echo "voidaccess-entrypoint: Tor is ready (circuit established)."
        break
    fi
    sleep 2
done

# Serve even if Tor never confirmed — /health reports Tor status, and --no-tor
# investigations (clearnet sources) don't need it.
cd /srv
exec uvicorn server:app --host 0.0.0.0 --port 7004
