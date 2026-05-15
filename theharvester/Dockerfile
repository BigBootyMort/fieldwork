FROM python:3.11-slim

# System deps theHarvester needs at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    dnsutils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    theHarvester \
    fastapi==0.115.0 \
    "uvicorn[standard]==0.30.6"

WORKDIR /srv
COPY server.py .

RUN useradd -m -u 1000 app && chown -R app:app /srv
USER app

EXPOSE 7001
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7001"]
