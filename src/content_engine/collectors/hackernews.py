"""Hacker News collector via the Algolia HN search API (free, no auth).

Searches for stories matching a configured set of keywords from the past
N days. Stories then become RawSignals with platform=hackernews.

API docs: https://hn.algolia.com/api"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import httpx

from ..config import settings
from ..schemas import RawSignal, SourcePlatform
from .base import ingest_signals

log = logging.getLogger("engine.hn")

_API = "https://hn.algolia.com/api/v1/search_by_date"
_USER_AGENT = f"{settings.brand_short}-content-engine/0.1"


def collect(per_query_limit: int = 10,
            window_days: int | None = None,
            min_points: int | None = None,
            queries: list[str] | None = None) -> dict:
    """Search HN for each keyword in channel.yaml, keep stories above
    min_points within window. All defaults come from channel.yaml; pass
    args explicitly to override."""
    queries = queries if queries is not None else settings.hn_queries
    window_days = window_days if window_days is not None else settings.hn_window_days
    min_points = min_points if min_points is not None else settings.hn_min_points
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    cutoff_ts = int(cutoff.timestamp())
    total = 0
    errors: list[str] = []
    seen_ids: set[str] = set()

    with httpx.Client(timeout=30.0, headers={"User-Agent": _USER_AGENT}) as client:
        for q in queries:
            try:
                params = {
                    "query": q,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{cutoff_ts},points>={min_points}",
                    "hitsPerPage": per_query_limit,
                }
                r = client.get(_API, params=params)
                r.raise_for_status()
                hits = r.json().get("hits", [])
                signals: list[RawSignal] = []
                for h in hits:
                    obj_id = h.get("objectID")
                    if not obj_id or obj_id in seen_ids:
                        continue
                    seen_ids.add(obj_id)
                    title = h.get("title") or h.get("story_title") or ""
                    url = h.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
                    try:
                        posted_at = datetime.utcfromtimestamp(h.get("created_at_i", 0))
                    except (TypeError, ValueError):
                        posted_at = None
                    signals.append(RawSignal(
                        platform=SourcePlatform.hackernews,
                        external_id=obj_id,
                        url=url,
                        author=h.get("author"),
                        title=title,
                        body=(h.get("story_text") or "")[:3000],
                        posted_at=posted_at,
                        metrics={
                            "points": h.get("points"),
                            "num_comments": h.get("num_comments"),
                        },
                        extra={"matched_query": q, "hn_id": obj_id},
                    ))
                n = ingest_signals(signals)
                total += n
                log.info("  → %d HN stories for %r", n, q)
                time.sleep(0.3)
            except Exception as e:
                errors.append(f"hn[{q}]: {str(e)[:120]}")
                log.warning("HN search %r failed: %s", q, e)

    return {
        "platform": "hackernews",
        "queries": len(queries),
        "ingested": total,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
