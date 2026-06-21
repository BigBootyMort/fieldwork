"""Reddit OSINT — user profiles, comment history, and content search.

Reddit now hard-blocks unauthenticated JSON access for automated clients,
so this crawler uses OAuth (app-only / client-credentials) when a free
"script" app is configured:

  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET   (create at
  https://www.reddit.com/prefs/apps — choose "script", free)

With credentials it talks to oauth.reddit.com (reliable). Without them it
falls back to the public endpoint (best-effort; often 403s) and returns a
note telling you to configure the keys.
"""
import logging
import os
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("fieldwork.reddit")

_UA = ("Fieldwork-OSINT/1.0 (by /u/fieldwork-research)")
_PUBLIC_BASE = "https://www.reddit.com"
_OAUTH_BASE = "https://oauth.reddit.com"

# Simple module-level token cache: {"token": str, "exp": epoch}
_token_cache: dict = {}


def _ts(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except Exception:
        return ""


async def _get_token() -> str | None:
    """Fetch (and cache) an app-only OAuth token if client creds are set."""
    cid = os.getenv("REDDIT_CLIENT_ID", "")
    secret = os.getenv("REDDIT_CLIENT_SECRET", "")
    if not (cid and secret):
        return None
    if _token_cache.get("token") and _token_cache.get("exp", 0) > time.time() + 30:
        return _token_cache["token"]
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": _UA}) as client:
            r = await client.post(
                "https://www.reddit.com/api/v1/access_token",
                data={"grant_type": "client_credentials"},
                auth=(cid, secret),
            )
            r.raise_for_status()
            j = r.json()
            tok = j.get("access_token")
            if tok:
                _token_cache["token"] = tok
                _token_cache["exp"] = time.time() + j.get("expires_in", 3600)
                return tok
    except Exception as exc:
        log.warning("Reddit OAuth token fetch failed: %s", exc)
    return None


async def _client_and_base():
    """Return (base_url, headers) — OAuth if available, else public."""
    tok = await _get_token()
    if tok:
        return _OAUTH_BASE, {"User-Agent": _UA, "Authorization": f"Bearer {tok}"}
    return _PUBLIC_BASE, {"User-Agent": _UA, "Accept": "application/json"}


def _auth_note(found: bool) -> dict:
    if not (os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET")):
        return {"note": "Configure REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET (free 'script' app at "
                        "reddit.com/prefs/apps) for reliable access — public access is often blocked."}
    return {}


async def reddit_user(username: str, limit: int = 25) -> dict:
    """Profile + recent comments for a Reddit username, with subreddit activity map."""
    uname = username.lstrip("u/").strip()
    base, headers = await _client_and_base()
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
            about = await client.get(f"{base}/user/{uname}/about.json")
            if about.status_code == 404:
                return {"username": uname, "found": False, "reason": "User not found"}
            if about.status_code in (401, 403):
                return {"username": uname, "found": False,
                        "reason": "Blocked by Reddit", **_auth_note(False)}
            if about.status_code == 429:
                return {"username": uname, "found": False, "reason": "Rate limited by Reddit"}
            about.raise_for_status()
            a = about.json().get("data", {}) or {}

            comments = await client.get(f"{base}/user/{uname}/comments.json",
                                        params={"limit": limit, "sort": "new"})
            cdata = comments.json().get("data", {}).get("children", []) if comments.status_code == 200 else []
    except Exception as exc:
        log.warning("Reddit user lookup failed for %r: %s", uname, exc)
        return {"username": uname, "found": False, "error": str(exc)}

    sub_activity: dict = {}
    recent = []
    for c in cdata:
        d = c.get("data", {}) or {}
        sub = d.get("subreddit", "")
        sub_activity[sub] = sub_activity.get(sub, 0) + 1
        recent.append({
            "subreddit": sub,
            "body":      (d.get("body", "") or "")[:280],
            "score":     d.get("score", 0),
            "created":   _ts(d.get("created_utc")),
            "url":       f"{_PUBLIC_BASE}{d.get('permalink','')}",
        })

    top_subs = sorted(sub_activity.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "username":      uname,
        "found":         True,
        "created":       _ts(a.get("created_utc")),
        "comment_karma": a.get("comment_karma", 0),
        "link_karma":    a.get("link_karma", 0),
        "verified":      a.get("verified", False),
        "is_mod":        a.get("is_mod", False),
        "profile_url":   f"{_PUBLIC_BASE}/user/{uname}",
        "active_subreddits": [{"subreddit": s, "count": n} for s, n in top_subs[:15]],
        "recent_comments":   recent[:limit],
    }


async def reddit_search(query: str, limit: int = 20) -> dict:
    """Search Reddit posts mentioning a query term."""
    base, headers = await _client_and_base()
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
            r = await client.get(f"{base}/search.json",
                                 params={"q": query, "limit": limit, "sort": "relevance", "type": "link"})
            if r.status_code in (401, 403):
                return {"query": query, "found": False, "reason": "Blocked by Reddit", **_auth_note(False)}
            if r.status_code == 429:
                return {"query": query, "found": False, "reason": "Rate limited by Reddit"}
            r.raise_for_status()
            children = r.json().get("data", {}).get("children", [])
    except Exception as exc:
        log.warning("Reddit search failed for %r: %s", query, exc)
        return {"query": query, "found": False, "error": str(exc)}

    posts = []
    for c in children:
        d = c.get("data", {}) or {}
        posts.append({
            "title":     d.get("title", ""),
            "subreddit": d.get("subreddit", ""),
            "author":    d.get("author", ""),
            "score":     d.get("score", 0),
            "comments":  d.get("num_comments", 0),
            "created":   _ts(d.get("created_utc")),
            "url":       f"{_PUBLIC_BASE}{d.get('permalink','')}",
        })
    return {"query": query, "found": bool(posts), "posts": posts, "total": len(posts)}
