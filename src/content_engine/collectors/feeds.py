"""RSS/Atom collector for vendor blogs + tech press.

Reads data/feeds.yaml as a registry so the user can edit the list without
touching code. Uses feedparser (tolerant of malformed feeds) and individual
per-feed try/except so one dead URL doesn't kill a cycle."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import feedparser  # type: ignore
import yaml

from ..config import settings
from ..schemas import RawSignal, SourcePlatform
from .base import ingest_signals

log = logging.getLogger("engine.feeds")

_REGISTRY = settings.db_path.parent / "feeds.yaml"


def _load_registry(path: Path | None = None) -> list[dict]:
    path = path or _REGISTRY
    if not path.exists():
        log.warning("no feeds registry at %s", path)
        return []
    return yaml.safe_load(path.read_text()) or []


def _to_datetime(struct_time) -> datetime | None:
    if not struct_time:
        return None
    try:
        # feedparser returns time.struct_time in UTC.
        return datetime(*struct_time[:6])
    except (TypeError, ValueError):
        return None


_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    return _HTML_TAG.sub("", s).strip()


def collect(max_per_feed: int = 10,
            window_days: int = 21,
            registry_path: Path | None = None) -> dict:
    """Fetch the latest entries from every registered feed."""
    registry = _load_registry(registry_path)
    if not registry:
        return {"platform": "blog", "feeds": 0, "ingested": 0, "errors": []}

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    total = 0
    errors: list[str] = []

    for entry in registry:
        name = entry.get("name", "?")
        url = entry.get("url")
        tags = entry.get("tags", [])
        if not url:
            continue
        try:
            log.info("fetching %s — %s", name, url)
            # feedparser handles network + parsing; no extra session needed.
            feed = feedparser.parse(url, request_headers={
                "User-Agent": f"{settings.brand_short}-content-engine/0.1",
            })
            if not feed.entries:
                # feedparser sets .version to '' when it didn't recognize a feed
                # format — almost always the vendor dropped RSS and the URL now
                # serves an HTML page. Surface that plainly, not as a cryptic
                # "not well-formed (invalid token)" XML error.
                if not feed.get("version"):
                    raise RuntimeError(
                        "URL did not return a valid RSS/Atom feed (got HTML?). "
                        "The vendor may have dropped RSS — update data/feeds.yaml.")
                if feed.bozo:
                    raise RuntimeError(str(feed.bozo_exception)[:200])

            signals: list[RawSignal] = []
            for item in (feed.entries or [])[:max_per_feed]:
                posted = _to_datetime(getattr(item, "published_parsed", None)) \
                         or _to_datetime(getattr(item, "updated_parsed", None))
                if posted and posted < cutoff:
                    continue
                external = (
                    getattr(item, "id", None)
                    or getattr(item, "guid", None)
                    or getattr(item, "link", None)
                )
                if not external:
                    continue
                body = _strip_html(
                    getattr(item, "summary", None)
                    or getattr(item, "description", None)
                    or ""
                )
                signals.append(RawSignal(
                    platform=SourcePlatform.blog,
                    external_id=str(external)[:300],
                    url=getattr(item, "link", None),
                    author=name,
                    title=_strip_html(getattr(item, "title", "")),
                    body=body[:5000],
                    posted_at=posted,
                    metrics={},
                    extra={"feed_tags": tags, "feed_url": url},
                ))
            n = ingest_signals(signals)
            total += n
            log.info("  → %d from %s", n, name)
            time.sleep(0.4)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:120]}")
            log.warning("%s failed: %s", name, str(e)[:160])

    return {
        "platform": "blog",
        "feeds": len(registry),
        "ingested": total,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
