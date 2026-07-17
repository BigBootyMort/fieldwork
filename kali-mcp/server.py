#!/usr/bin/env python3
"""Kali Linux MCP server.

Exposes a curated set of Kali Linux reconnaissance / scanning tools over the
Model Context Protocol (stdio transport), so an MCP client (e.g. Claude Code via
the Docker MCP Toolkit gateway) can drive them.

AUTHORIZED USE ONLY. This server is built for a single-user, localhost OSINT /
pentesting lab. Only run scans against systems you own or have explicit written
permission to test. All tools run inside the Kali container; nothing here reaches
the host filesystem.
"""
from __future__ import annotations

import os
import shlex
import subprocess

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "kali-mcp",
    instructions=(
        "Kali Linux security tooling exposed over MCP. Use ONLY against targets "
        "you are authorized to test. Prefer the typed tools (nmap_scan, "
        "dns_lookup, whois_lookup, whatweb_scan, nikto_scan, gobuster_dir); "
        "run_command is a generic escape hatch restricted to an allowlist of "
        "recon/scan binaries. Scans can take time — respect the per-tool timeouts."
    ),
)

DEFAULT_TIMEOUT = 120
MAX_OUTPUT = 20_000

# Binaries the generic run_command tool is allowed to invoke. Extend via the
# KALI_MCP_ALLOWLIST env var (comma-separated) without rebuilding the image.
_BASE_ALLOWLIST = {
    "nmap", "dig", "host", "nslookup", "whois", "whatweb", "nikto",
    "gobuster", "dirb", "sslscan", "sslyze", "wafw00f", "dnsrecon",
    "dnsenum", "sublist3r", "theharvester", "curl", "wget", "ping",
    "traceroute", "nc", "ncat", "netcat", "arp-scan", "masscan",
    "hping3", "fping", "amass", "ffuf", "feroxbuster",
}


def _allowlist() -> set[str]:
    extra = os.environ.get("KALI_MCP_ALLOWLIST", "")
    return _BASE_ALLOWLIST | {b.strip() for b in extra.split(",") if b.strip()}


def _run(cmd: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a command (no shell) and return combined, truncated output."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return f"error: tool not installed in the Kali image: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return f"error: timed out after {timeout}s: {' '.join(cmd)}"

    out = proc.stdout
    if proc.stderr:
        out += ("\n[stderr]\n" if out else "") + proc.stderr
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n...[truncated; {len(out)} bytes total]"
    return out.strip() or f"(no output; exit code {proc.returncode})"


@mcp.tool()
def nmap_scan(target: str, ports: str = "", extra_args: str = "-sV -T4") -> str:
    """Run an nmap scan against a host, hostname, or CIDR range.

    Args:
        target: Host/IP/CIDR to scan (e.g. "192.168.1.0/24", "example.com").
        ports: Optional port spec passed to -p (e.g. "22,80,443" or "1-1000").
        extra_args: Additional nmap flags (default "-sV -T4" for service/version
            detection at a moderate timing template). Avoid intrusive flags
            unless you have authorization.
    """
    cmd = ["nmap"]
    if ports:
        cmd += ["-p", ports]
    cmd += shlex.split(extra_args)
    cmd.append(target)
    return _run(cmd, timeout=300)


@mcp.tool()
def dns_lookup(name: str, record_type: str = "A") -> str:
    """Resolve DNS records for a name using dig.

    Args:
        name: Domain or hostname to look up.
        record_type: DNS record type (A, AAAA, MX, TXT, NS, CNAME, SOA, ANY).
    """
    return _run(["dig", "+nocmd", "+noall", "+answer", name, record_type])


@mcp.tool()
def whois_lookup(query: str) -> str:
    """Look up WHOIS registration data for a domain or IP address."""
    return _run(["whois", query])


@mcp.tool()
def whatweb_scan(url: str) -> str:
    """Fingerprint the technologies of a website with whatweb.

    Args:
        url: Target URL or host (e.g. "https://example.com").
    """
    return _run(["whatweb", "--color=never", "-a", "3", url], timeout=180)


@mcp.tool()
def nikto_scan(target: str, port: str = "") -> str:
    """Scan a web server for common vulnerabilities and misconfigurations.

    Args:
        target: Host or URL to scan.
        port: Optional port (defaults to nikto's own default of 80/443).
    """
    cmd = ["nikto", "-ask", "no", "-h", target]
    if port:
        cmd += ["-p", port]
    return _run(cmd, timeout=600)


@mcp.tool()
def gobuster_dir(url: str, wordlist: str = "", extensions: str = "") -> str:
    """Brute-force directories/files on a web server with gobuster.

    Args:
        url: Base URL (e.g. "http://example.com").
        wordlist: Path to a wordlist inside the container. Defaults to
            /usr/share/wordlists/dirb/common.txt.
        extensions: Optional comma-separated extensions (e.g. "php,txt,html").
    """
    wl = wordlist or "/usr/share/wordlists/dirb/common.txt"
    cmd = ["gobuster", "dir", "-q", "-u", url, "-w", wl]
    if extensions:
        cmd += ["-x", extensions]
    return _run(cmd, timeout=300)


@mcp.tool()
def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run an allowlisted Kali command-line tool with arbitrary arguments.

    The first token must be an allowed binary (see the server's allowlist;
    extend it with the KALI_MCP_ALLOWLIST env var). The command is parsed with
    shell-style tokenization but executed WITHOUT a shell, so pipes, redirects,
    and command chaining are not supported.

    Args:
        command: Full command line, e.g. "sslscan example.com:443".
        timeout: Max seconds to wait (capped at 900).
    """
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return f"error: could not parse command: {exc}"
    if not parts:
        return "error: empty command"

    binary = os.path.basename(parts[0])
    allowed = _allowlist()
    if binary not in allowed:
        return (
            f"error: '{binary}' is not in the allowlist. Allowed: "
            f"{', '.join(sorted(allowed))}. Extend via KALI_MCP_ALLOWLIST."
        )
    return _run(parts, timeout=min(max(int(timeout), 1), 900))


if __name__ == "__main__":
    mcp.run()
