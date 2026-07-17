# Kali Linux MCP server image.
# Curated recon/scan toolset + a Python MCP server that speaks stdio, so the
# Docker MCP Toolkit gateway can spawn it and expose the tools to MCP clients.
FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# Security tools + Python runtime. wordlists brings /usr/share/wordlists for
# gobuster's default. Keep this list curated so the image stays reasonable.
RUN apt-get update && apt-get install -y --no-install-recommends \
        nmap \
        dnsutils \
        whois \
        nikto \
        whatweb \
        gobuster \
        sslscan \
        wafw00f \
        dnsrecon \
        wordlists \
        curl \
        wget \
        iputils-ping \
        netcat-traditional \
        ca-certificates \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv so pip doesn't fight Debian's PEP 668 externally-managed marker.
RUN python3 -m venv /opt/mcp-venv
ENV PATH="/opt/mcp-venv/bin:$PATH"

COPY requirements.txt /app/requirements.txt
RUN /opt/mcp-venv/bin/pip install -r /app/requirements.txt

COPY server.py /app/server.py
WORKDIR /app

# The MCP gateway talks to this process over stdio.
ENTRYPOINT ["/opt/mcp-venv/bin/python", "/app/server.py"]
