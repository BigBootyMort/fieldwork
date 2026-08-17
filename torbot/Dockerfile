# TorBot — dark-web .onion OSINT crawler (DedSecInside/TorBot).
# https://github.com/DedSecInside/TorBot
#
# Flavor A (fieldwork-osint-tool skill, Layer 0): a thin FastAPI wrapper (server.py)
# shells out to TorBot's CLI. Unlike maigret, TorBot crawls .onion sites, so this
# container ALSO bundles its own Tor daemon (SOCKS5 on 127.0.0.1:9050) — TorBot's own
# Dockerfile assumes an external host Tor, which we don't have on the Docker network.
# entrypoint.sh starts Tor, waits for bootstrap, then launches the wrapper on :7003.
# Internal-only: no ports published (matches maigret/theharvester/recon).
FROM python:3.11-slim

# tor = the SOCKS5 proxy TorBot crawls through; git to clone TorBot; curl for the
# Tor readiness probe; gcc/g++ for any native wheels in TorBot's requirements.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl tor \
      gcc g++ pkg-config \
    && rm -rf /var/lib/apt/lists/*

# ── Local TLS-interception CA trust (before any pip/git/network step) ────────
# AVG/Avast-style HTTPS inspection re-signs traffic with a private root CA that
# breaks pip/git AT BUILD TIME. Trust the PEM in torbot/certs/ first (no-op if none).
COPY certs /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> /etc/ssl/certs/ca-certificates.crt && \
        echo "Trusted extra CA(s) for build"; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# Clone TorBot (dev branch — the maintained one) and install its runtime deps.
# We run it via `python main.py` with PYTHONPATH=/torbot/src (its package layout),
# avoiding the poetry editable-install dance.
RUN git clone --depth 1 --branch dev https://github.com/DedSecInside/TorBot.git /torbot
RUN pip install --no-cache-dir -r /torbot/requirements.txt
ENV PYTHONPATH=/torbot/src

# Our thin HTTP wrapper's own deps.
RUN pip install --no-cache-dir fastapi==0.115.0 "uvicorn[standard]==0.30.6"

# certifi exists now — trust the extra CA at runtime too.
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

COPY server.py /srv/server.py
COPY entrypoint.sh /srv/entrypoint.sh
RUN chmod +x /srv/entrypoint.sh

EXPOSE 7003
CMD ["/srv/entrypoint.sh"]
