# MailAccess — email OSINT (2500+ platforms, breach detection, identity clustering).
# https://github.com/KatrielMoses/MailAccess
#
# Flavor B (see the fieldwork-osint-tool skill, Layer 0): the tool ships its own
# REST server, so there is no server.py wrapper — we just install it and run
# `mailaccess serve`. The Fieldwork backend reaches it at http://mailaccess:8000
# (MAILACCESS_URL) and drives it via POST /api/investigate → poll GET /api/report/{id}.
#
# Internal-only: no ports are published in docker-compose, matching maigret/theharvester.
FROM python:3.11-slim

# Build tools some of MailAccess's OSINT engine deps need (lxml, native wheels).
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl \
      gcc g++ pkg-config \
    && rm -rf /var/lib/apt/lists/*

# ── Local TLS-interception CA trust (before any pip/git/network step) ────────
# AVG/Avast-style HTTPS inspection re-signs traffic with a private root CA that
# breaks pip/git AT BUILD TIME. Trust the PEM in mailaccess/certs/ first.
# No-op if certs/ has no *.pem.
COPY certs /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> /etc/ssl/certs/ca-certificates.crt && \
        echo "Trusted extra CA(s) for build"; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# MailAccess + its own server deps. Omit the [ml] extra (spaCy name classification)
# for a lighter first build — add "mailaccess[ml]" if you want NER on discovered names.
RUN pip install --no-cache-dir mailaccess

# certifi exists now — trust the extra CA at runtime too (MailAccess queries thousands of sites).
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

# Runs as root on purpose: MailAccess writes bundle/cache files into its OWN
# site-packages dir at startup (e.g. .../site-packages/maltego for the Maltego
# transform bundle), which a non-root user cannot create -> PermissionError ->
# crash-loop. Acceptable here: the container is internal-only (no published ports)
# on a single-user localhost stack. Its SQLite cache lives under /root (volume-mounted).
WORKDIR /root

EXPOSE 8000
# `mailaccess serve` defaults to --host 127.0.0.1, which is unreachable from the
# backend container — bind 0.0.0.0 so http://mailaccess:8000 works across the
# Docker network. (Safe: no ports are published to the host.)
CMD ["mailaccess", "serve", "--host", "0.0.0.0", "--port", "8000"]
