FROM python:3.12-slim

# System deps required by theHarvester at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    dnsutils \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Local TLS-interception CA trust (before any pip/git/network step) ────────
# AVG/Avast-style HTTPS inspection re-signs traffic with a private root CA that
# breaks pip/git AT BUILD TIME. Trust the PEM in theharvester/certs/ first.
COPY certs /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> /etc/ssl/certs/ca-certificates.crt && \
        echo "Trusted extra CA(s) for build"; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# Install the real theHarvester from official GitHub source.
# The PyPI package "theHarvester" (v0.0.1) is a dead stub — it contains
# only the old discovery DNS library and ships NO CLI binary.
# theHarvester pulls its own FastAPI version; we do not pin it here
# to avoid dependency conflicts.
RUN pip install --no-cache-dir \
    "theHarvester @ git+https://github.com/laramies/theHarvester.git" \
    "uvicorn[standard]>=0.30.0"

# certifi exists now — trust the extra CA at runtime too (httpx/requests).
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

WORKDIR /srv
COPY server.py .

RUN useradd -m -u 1000 app && chown -R app:app /srv
USER app

EXPOSE 7001
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7001"]
