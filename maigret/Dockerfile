FROM python:3.11-slim

# Build tools needed by maigret's pycairo dependency (PDF report rendering)
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates \
      libcairo2-dev pkg-config meson ninja-build gcc \
    && rm -rf /var/lib/apt/lists/*

# ── Local TLS-interception CA trust (before any pip/git/network step) ────────
# AVG/Avast-style HTTPS inspection re-signs traffic with a private root CA that
# breaks pip/git AT BUILD TIME. Trust the PEM in maigret/certs/ first.
COPY certs /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> /etc/ssl/certs/ca-certificates.crt && \
        echo "Trusted extra CA(s) for build"; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# Install maigret from GitHub source (v0.6.0 is not on PyPI)
# fastapi + uvicorn — thin HTTP wrapper so the backend can call it
RUN pip install --no-cache-dir \
    "git+https://github.com/soxoj/maigret.git@v0.6.0" \
    fastapi==0.115.0 \
    "uvicorn[standard]==0.30.6"

# certifi exists now — trust the extra CA at runtime too (maigret queries 3000+ sites).
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

WORKDIR /srv
COPY server.py .

# Non-root user
RUN useradd -m -u 1000 app && chown -R app:app /srv
USER app

EXPOSE 7000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7000"]
