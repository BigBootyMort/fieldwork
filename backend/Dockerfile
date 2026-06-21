FROM python:3.11-slim

# System tools used by media analysis endpoints
RUN apt-get update && apt-get install -y --no-install-recommends \
    libimage-exiftool-perl \
    ffmpeg \
    # Phase 11 — WeasyPrint PDF rendering dependencies
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# ── Local TLS-interception CA trust (MUST come before any pip/network step) ──
# TLS-inspecting antivirus/proxies (e.g. AVG/Avast Web Shield) re-sign HTTPS
# with a private root CA. The host trusts it; the container does not — which
# breaks `pip install` AT BUILD TIME and every outbound HTTPS call at runtime
# (CERTIFICATE_VERIFY_FAILED). Drop the root CA PEM into backend/certs/ and we
# trust it before installing anything. No-op if the dir has no *.pem.
COPY ./certs /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> /etc/ssl/certs/ca-certificates.crt && \
        echo "Trusted $(ls /usr/local/share/ca-certificates/extra/*.pem | wc -l) extra CA(s) for build"; \
    fi
# Make pip + stdlib ssl (nltk/textblob/spaCy whl download) use the system
# bundle (now incl. the extra CA). httpx uses certifi, patched after install.
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Phase 4: download spaCy NLP model and TextBlob corpora at build time
# so the first request doesn't pay the download cost at runtime.
# en_core_web_sm is ~12 MB — fast, good enough for NER on news text.
# Swap for en_core_web_md/lg for higher accuracy at the cost of size.
RUN pip install --no-cache-dir \
    https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl
RUN python -m textblob.download_corpora

# Now that certifi is installed, append the extra CA to it too so runtime
# httpx (which uses certifi, not the system bundle) trusts the same root.
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

COPY ./app /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
