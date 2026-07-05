"""Reddit collector.

Reddit began 403-blocking unauthenticated *.json (and, increasingly, *.rss)
reads in 2026. Two strategies, chosen automatically:

  1. Authenticated (preferred, reliable). If REDDIT_CLIENT_ID +
     REDDIT_CLIENT_SECRET are set, use app-only OAuth (client_credentials) to
     hit oauth.reddit.com — a real 100 req/min quota, not blanket blocks.
     Create a free "script" app at https://www.reddit.com/prefs/apps.
  2. RSS fallback (best-effort, zero-config). Read the public per-subreddit
     RSS feeds. Works when Reddit isn't throttling the IP; degrades to 403/429
     under pressure. Backs off on 429.

Both paths produce identical RawSignals.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Iterable

import feedparser
import httpx

from ..config import settings
from .base import ingest_signals, signals_from_reddit_listing, signals_from_reddit_rss

log = logging.getLogger("engine.reddit")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_RSS_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/atom+xml,application/rss+xml,application/xml,text/xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Cached app-only OAuth token: (token, expires_epoch).
_token_cache: tuple[str, float] | None = None


# ─── Authenticated path (oauth.reddit.com) ────────────────────────────────────
def _get_app_token() -> str:
    """Fetch/reuse an app-only OAuth token via client_credentials. Cached until
    ~60s before expiry."""
    global _token_cache
    now = time.time()
    if _token_cache and _token_cache[1] - 60 > now:
        return _token_cache[0]
    r = httpx.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(settings.reddit_client_id, settings.reddit_client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": _UA},
        timeout=30.0,
    )
    r.raise_for_status()
    tok = r.json()
    token = tok["access_token"]
    _token_cache = (token, now + int(tok.get("expires_in", 3600)))
    log.info("reddit: acquired app-only OAuth token (expires in %ss)",
             tok.get("expires_in", 3600))
    return token


def _fetch_top_oauth(sub: str, time_window: str, limit: int) -> dict:
    token = _get_app_token()
    url = f"https://oauth.reddit.com/r/{sub}/top"
    headers = {"Authorization": f"bearer {token}", "User-Agent": _UA}
    with httpx.Client(timeout=30.0, headers=headers) as client:
        for attempt in range(3):
            r = client.get(url, params={"t": time_window, "limit": limit})
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                log.warning("r/%s 429 (oauth) — waiting %ds", sub, wait)
                time.sleep(wait)
                continue
            if r.status_code == 401:  # token expired mid-run — refresh once
                global _token_cache
                _token_cache = None
                headers["Authorization"] = f"bearer {_get_app_token()}"
                continue
            r.raise_for_status()
            return r.json()
        raise httpx.HTTPStatusError("429 after retries", request=r.request, response=r)


# ─── RSS fallback ─────────────────────────────────────────────────────────────
def _fetch_top_rss(sub: str, time_window: str, limit: int) -> feedparser.FeedParserDict:
    url = f"https://www.reddit.com/r/{sub}/top/.rss"
    params = {"t": time_window, "limit": limit}
    with httpx.Client(timeout=30.0, headers=_RSS_HEADERS, follow_redirects=True) as client:
        for attempt in range(3):
            r = client.get(url, params=params)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait = (int(retry_after) if retry_after and retry_after.isdigit()
                        else 8 * (attempt + 1))
                log.warning("r/%s 429 (rss) — waiting %ds (attempt %d/3)",
                            sub, wait, attempt + 1)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return feedparser.parse(r.text)
        raise httpx.HTTPStatusError("429 after retries", request=r.request, response=r)


def collect(time_window: str = "week",
            subs: Iterable[tuple[str, int]] | None = None,
            inter_request_sleep: float = 2.0) -> dict:
    """Fetch top posts for each subreddit via OAuth (if configured) or RSS."""
    subs = list(subs) if subs else settings.reddit_subreddits
    use_oauth = settings.reddit_auth_available
    mode = "oauth" if use_oauth else "rss"
    log.info("reddit collector using %s path", mode)
    if not use_oauth:
        log.warning("no REDDIT_CLIENT_ID/SECRET set — using best-effort RSS "
                    "(Reddit may 403/429). Create a free script app at "
                    "https://www.reddit.com/prefs/apps for reliable access.")

    total = 0
    errors: list[str] = []
    for sub_name, limit in subs:
        try:
            log.info("fetching r/%s top %d (%s) via %s", sub_name, limit, time_window, mode)
            if use_oauth:
                listing = _fetch_top_oauth(sub_name, time_window, limit)
                sigs = signals_from_reddit_listing(listing, default_subreddit=sub_name)
            else:
                feed = _fetch_top_rss(sub_name, time_window, limit)
                sigs = signals_from_reddit_rss(feed, subreddit=sub_name)
            n = ingest_signals(sigs)
            total += n
            log.info("  → ingested %d from r/%s", n, sub_name)
            time.sleep(inter_request_sleep)
        except Exception as e:
            errors.append(f"r/{sub_name}: {str(e)[:120]}")
            log.warning("r/%s failed: %s", sub_name, e)

    return {
        "platform": "reddit",
        "mode": mode,
        "subreddits_fetched": len(subs),
        "ingested": total,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
