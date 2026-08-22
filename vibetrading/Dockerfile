# VibeTrading — NL→strategy→backtest engine (VibeTradingLabs/vibetrading), research mode.
# https://github.com/VibeTradingLabs/vibetrading
#
# Track 4 / Phase A. A thin FastAPI wrapper (server.py) calls VibeTrading's Python API
# in-process: strategy.generate (litellm→Claude), backtest.run, template.generate.
#
# THE WALL (design decision, enforced here at the dependency level): we install ONLY the
# core `vibetrading` package — NOT the [hyperliquid,paradex,lighter,aster] live-exchange
# extras. Without those adapters installed the container physically cannot place a live
# trade; it can only generate strategies, download historical data (ccxt/clearnet), and
# backtest/paper-trade. No Tor needed. Internal-only: no ports published.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl gcc g++ \
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

# Core only — deliberately no live-exchange extras (see THE WALL above).
RUN pip install --no-cache-dir vibetrading
RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

# certifi exists now — trust the extra CA at runtime too (ccxt fetches over HTTPS).
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

COPY server.py /srv/server.py
WORKDIR /srv
# Downloaded OHLCV data + any cache live here (volume-mounted).
ENV HOME=/data

EXPOSE 7005
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7005"]
