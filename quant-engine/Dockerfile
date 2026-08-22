# Quant engine — NautilusTrader backtest engine for the Runi Shell "Quant" module.
# Track 4 (re-scoped, rev B). Replaces the earlier VibeTrading container, whose backtester
# booked 0 trades. NautilusTrader is a production-grade backtester that actually books trades.
#
# Research mode only: the wrapper runs Nautilus's BacktestEngine against a SIM venue — no live
# venue/adapter/exchange key is ever wired in (the wall). Claude fills a fixed strategy scaffold;
# the wrapper owns all engine/venue/instrument/data boilerplate. Internal-only: no ports published.
#
# nautilus_trader 1.231+ requires Python 3.12+.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# ── Local TLS-interception CA trust (before any pip/network step) ────────────
COPY certs /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> /etc/ssl/certs/ca-certificates.crt && \
        echo "Trusted extra CA(s) for build"; \
    fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

# NautilusTrader (backtest engine) + ccxt (historical OHLCV) + the wrapper's web deps.
RUN pip install --no-cache-dir nautilus_trader
# Nautilus's Cython BarDataWrangler needs writable numpy buffers; pandas 3's copy-on-write
# makes to_numpy() read-only -> "buffer source array is read-only". Pin pandas <3 (Nautilus
# allows >=2.3.3,<4). Without this, backtests crash before booking a single trade.
RUN pip install --no-cache-dir "pandas>=2.3.3,<3"
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" ccxt httpx

# certifi exists now — trust the extra CA at runtime too.
RUN if ls /usr/local/share/ca-certificates/extra/*.pem >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/extra/*.pem >> "$(python -c 'import certifi;print(certifi.where())')" && \
        echo "Appended extra CA(s) to certifi"; \
    fi

COPY server.py /srv/server.py
COPY gate.py /srv/gate.py
WORKDIR /srv
ENV HOME=/data

EXPOSE 7005
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7005"]
