"""Reddit collector. Uses Reddit's free public JSON API directly — no auth,
no API credits. Composio v3's Reddit toolkit doesn't expose a listing endpoint,
and OAuth'd top-of-subreddit reads aren't needed for public data anyway."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Iterable

import httpx

from ..config import settings
from .base import ingest_signals, signals_from_reddit_listing

log = logging.getLogger("engine.reddit")

# Reddit will 429 if we hammer; their guideline is 1 req/sec for the public
# JSON endpoint. We're well under that.
_USER_AGENT = f"{settings.brand_short}-content-engine/0.1"
_BASE = "https://www.reddit.com"


def _fetch_top(sub: str, time_window: str, limit: int) -> dict:
    url = f"{_BASE}/r/{sub}/top.json"
    with httpx.Client(timeout=30.0, headers={"User-Agent": _USER_AGENT}) as client:
        r = client.get(url, params={"t": time_window, "limit": limit})
        if r.status_code == 429:
            log.warning("rate limited on r/%s, sleeping 5s", sub)
            time.sleep(5)
            r = client.get(url, params={"t": time_window, "limit": limit})
        r.raise_for_status()
        return r.json()


def collect(time_window: str = "week",
            subs: Iterable[tuple[str, int]] | None = None,
            inter_request_sleep: float = 1.1) -> dict:
    """Fetch top posts for each subreddit. Returns counts.
    Pulls subreddit list from channel.yaml unless `subs` is passed explicitly."""
    subs = list(subs) if subs else settings.reddit_subreddits
    total = 0
    errors: list[str] = []

    for sub_name, limit in subs:
        try:
            log.info("fetching r/%s top %d (%s)", sub_name, limit, time_window)
            listing = _fetch_top(sub_name, time_window, limit)
            sigs = signals_from_reddit_listing(listing, default_subreddit=sub_name)
            n = ingest_signals(sigs)
            total += n
            log.info("  → ingested %d from r/%s", n, sub_name)
            time.sleep(inter_request_sleep)
        except Exception as e:
            errors.append(f"r/{sub_name}: {str(e)[:120]}")
            log.warning("r/%s failed: %s", sub_name, e)

    return {
        "platform": "reddit",
        "subreddits_fetched": len(subs),
        "ingested": total,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
