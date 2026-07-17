# Kali Linux MCP server

A Model Context Protocol (MCP) server that exposes a curated set of Kali Linux
reconnaissance / scanning tools to an MCP client (Claude Code, Claude Desktop,
Cursor, …) through the **Docker Desktop MCP Toolkit** gateway.

> **Authorized use only.** Built for this single-user, localhost OSINT / pentest
> lab. Only scan systems you own or have explicit written permission to test.
> Every tool runs inside the Kali container — nothing here touches the host FS.

## What it exposes

| Tool | Backing binary | Purpose |
|------|----------------|---------|
| `nmap_scan` | nmap | Port/service/version scanning of a host or CIDR |
| `dns_lookup` | dig | Resolve DNS records (A/AAAA/MX/TXT/NS/…) |
| `whois_lookup` | whois | Domain / IP registration data |
| `whatweb_scan` | whatweb | Web technology fingerprinting |
| `nikto_scan` | nikto | Web server vuln/misconfig scan |
| `gobuster_dir` | gobuster | Directory/file brute force |
| `run_command` | *allowlisted* | Generic escape hatch for other recon tools |

`run_command` only runs binaries on an allowlist (nmap, dig, whois, whatweb,
nikto, gobuster, sslscan, wafw00f, dnsrecon, curl, masscan, ffuf, amass, …).
Extend it at runtime with the `KALI_MCP_ALLOWLIST` env var (comma-separated),
no rebuild needed. Commands run **without a shell**, so pipes/redirects/chaining
are not supported — call one tool per invocation.

## Files

- `Dockerfile` — `kalilinux/kali-rolling` + curated tools + Python venv.
- `server.py` — FastMCP server (stdio transport).
- `requirements.txt` — the `mcp` Python SDK.

## Build

```bash
docker build -t kali-mcp:latest kali-mcp/
```

## Register with the Docker MCP Toolkit and connect Claude Code

The toolkit's newer CLI is profile-based. Create a profile that includes this
image and connect the `claude-code` client to it:

```bash
# 1. Create a profile that includes the locally-built image as a server
docker mcp profile create --name kali --server docker://kali-mcp:latest

# 2. Connect the Claude Code client to that profile (this repo's scope)
docker mcp client connect claude-code --profile kali

# verify
docker mcp profile show kali
docker mcp client ls
```

Reload / restart Claude Code afterwards so it picks up the new MCP server. The
tools appear namespaced under the gateway (e.g. `kali-mcp` → `nmap_scan`).

To remove:

```bash
docker mcp client disconnect claude-code
docker mcp profile remove kali
```

## Quick manual test (without the gateway)

The server speaks MCP over stdio, so you can smoke-test the container starts and
lists tools with a single JSON-RPC handshake:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | docker run --rm -i kali-mcp:latest
```

You should see an `initialize` result followed by a `tools/list` result naming
the six tools above.
