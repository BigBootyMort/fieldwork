FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Local TLS-interception CA trust (before any pip/git/network step) ────────
# AVG/Avast-style HTTPS inspection re-signs traffic with a private root CA that
# breaks git clone + pip AT BUILD TIME. Trust the PEM in spiderfoot/certs/ first.
COPY certs /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> /etc/ssl/certs/ca-certificates.crt && \
        echo "Trusted extra CA(s) for build"; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

WORKDIR /spiderfoot

# Clone SpiderFoot source (no PyPI package exists)
RUN git clone --depth 1 https://github.com/smicallef/spiderfoot.git . \
    && pip install --no-cache-dir -r requirements.txt

# certifi exists now — trust the extra CA at runtime too (SpiderFoot fetches over HTTPS).
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

RUN useradd -m -u 1000 -s /bin/sh spiderfoot \
    && chown -R spiderfoot:spiderfoot /spiderfoot

USER spiderfoot

EXPOSE 5001
CMD ["python", "sf.py", "-l", "0.0.0.0:5001"]
