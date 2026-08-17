# VoidAccess — dark-web threat-intel platform (KatrielMoses/voidaccess), CLI mode.
# https://github.com/KatrielMoses/voidaccess
#
# Flavor A (fieldwork-osint-tool skill, Layer 0): a thin FastAPI wrapper (server.py)
# shells out to the `voidaccess` CLI. We run its *lightweight CLI mode* (SQLite, no
# Postgres/Next.js) rather than nesting its full 4-service stack. Tor is bundled here
# (like the torbot container) so onion searches work on the internal Docker network.
# entrypoint.sh starts Tor, waits for bootstrap, then serves the wrapper on :7004.
# Internal-only: no ports published.
FROM python:3.11-slim

# tor = SOCKS5 proxy for onion sources; git/curl/build for pip wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl tor build-essential \
    && rm -rf /var/lib/apt/lists/*

# ── Local TLS-interception CA trust (before any pip/git/network step) ────────
COPY certs /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> /etc/ssl/certs/ca-certificates.crt && \
        echo "Trusted extra CA(s) for build"; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# CPU-only torch FIRST — voidaccess needs torch>=2.3, but the default PyPI wheel drags
# ~1.5GB of CUDA libs we can't use. The CPU wheel is a fraction of the size and satisfies
# the dependency, so the later `pip install voidaccess` won't pull the CUDA build.
RUN pip install --no-cache-dir "torch>=2.3" --index-url https://download.pytorch.org/whl/cpu

# VoidAccess + NLP extra (spaCy NER for entity extraction). Its own fastapi pin comes
# with it; we reuse that for the wrapper (no separate fastapi install → no version clash).
RUN pip install --no-cache-dir "voidaccess[nlp]"
RUN python -m spacy download en_core_web_sm || echo "spaCy model download skipped"
RUN pip install --no-cache-dir "uvicorn[standard]"

# certifi exists now — trust the extra CA at runtime too.
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

COPY server.py /srv/server.py
COPY entrypoint.sh /srv/entrypoint.sh
RUN chmod +x /srv/entrypoint.sh

# CLI SQLite mode: keep data under HOME (volume-mounted) so it persists.
ENV HOME=/data \
    TOR_PROXY_HOST=127.0.0.1 \
    TOR_PROXY_PORT=9050
WORKDIR /data

EXPOSE 7004
CMD ["/srv/entrypoint.sh"]
