# Single-stage: slim Python + nmap + prebuilt ProjectDiscovery binaries.
# No Go compilation — binaries are downloaded directly from GitHub Releases.
# Build time: ~1-2 minutes (was 10+ with go install).
FROM python:3.11-slim

# Pin versions here — bump to upgrade a tool without rebuilding everything.
ARG SUBFINDER_VER=2.6.6
ARG NUCLEI_VER=3.2.9
ARG HTTPX_VER=1.6.9

RUN apt-get update && apt-get install -y --no-install-recommends \
        nmap \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# subfinder
RUN curl -fsSL \
    "https://github.com/projectdiscovery/subfinder/releases/download/v${SUBFINDER_VER}/subfinder_${SUBFINDER_VER}_linux_amd64.zip" \
    -o /tmp/sf.zip \
    && unzip -q /tmp/sf.zip subfinder -d /usr/local/bin \
    && chmod +x /usr/local/bin/subfinder \
    && rm /tmp/sf.zip

# nuclei
RUN curl -fsSL \
    "https://github.com/projectdiscovery/nuclei/releases/download/v${NUCLEI_VER}/nuclei_${NUCLEI_VER}_linux_amd64.zip" \
    -o /tmp/nu.zip \
    && unzip -q /tmp/nu.zip nuclei -d /usr/local/bin \
    && chmod +x /usr/local/bin/nuclei \
    && rm /tmp/nu.zip

# httpx
RUN curl -fsSL \
    "https://github.com/projectdiscovery/httpx/releases/download/v${HTTPX_VER}/httpx_${HTTPX_VER}_linux_amd64.zip" \
    -o /tmp/hx.zip \
    && unzip -q /tmp/hx.zip httpx -d /usr/local/bin \
    && chmod +x /usr/local/bin/httpx \
    && rm /tmp/hx.zip

# Pull nuclei templates at build time so first scan is instant
RUN nuclei -update-templates 2>/dev/null || true

# Python API wrapper
RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

WORKDIR /recon
COPY app.py .

EXPOSE 7002
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7002"]
