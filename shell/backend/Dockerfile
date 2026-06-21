FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal — shell backend itself stays lean
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Local TLS-interception CA trust (MUST come before pip/network steps) ─────
# TLS-inspecting antivirus/proxies (e.g. AVG/Avast Web Shield) re-sign HTTPS
# with a private root CA the container doesn't trust — which breaks `pip
# install` AT BUILD TIME plus RSS polling and the Claude bridge call at runtime
# (CERTIFICATE_VERIFY_FAILED). Drop the root CA PEM into shell/backend/certs/
# and we trust it before installing anything. No-op if the dir has no *.pem.
COPY certs /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> /etc/ssl/certs/ca-certificates.crt && \
        echo "Trusted extra CA(s) for build"; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# certifi exists now — append the extra CA so runtime httpx trusts it too.
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

COPY app /app

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
