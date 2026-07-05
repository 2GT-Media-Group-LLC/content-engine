"""Base helpers for collectors. Collectors only do I/O — schema validation, dedupe,
and DB insert. They don't call LLMs."""
from __future__ import annotations

import calendar
import html as _html
import re
from datetime import datetime
from typing import Iterable

from ..db import get_conn, upsert_signal
from ..schemas import RawSignal, SourcePlatform

_TAG_RE = re.compile(r"<[^>]+>")
_SUBMITTED_RE = re.compile(r"\bsubmitted by\b", re.IGNORECASE)


def ingest_signals(signals: Iterable[RawSignal | dict]) -> int:
    """Insert raw signals (validated). Returns count newly upserted."""
    n = 0
    with get_conn() as conn:
        for s in signals:
            if isinstance(s, dict):
                s = RawSignal.model_validate(s)
            upsert_signal(conn, s.model_dump(mode="python"))
            n += 1
    return n


def signals_from_youtube_videos(videos, channel: dict) -> list[RawSignal]:
    """Convert YouTube video records into RawSignals.

    Accepts dicts (Composio raw payloads, vidiq raw payloads) OR VideoRecord
    Pydantic instances. Field names checked in both their vidiq-native
    (camelCase: viewCount, likeCount) and provider-normalized (snake-case:
    views, likes) shapes so this stays stable across providers."""
    out: list[RawSignal] = []
    channel_handle = channel.get("title") or channel.get("channelTitle") or channel.get("channelId")
    for v in videos:
        if hasattr(v, "model_dump"):  # VideoRecord or any Pydantic model
            v = v.model_dump(exclude_none=False)
        try:
            posted_at = datetime.fromisoformat(v["publishedAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            posted_at = None
        out.append(RawSignal(
            platform=SourcePlatform.youtube,
            external_id=v["videoId"],
            url=f"https://youtu.be/{v['videoId']}",
            author=channel_handle,
            title=v.get("title") or v.get("videoTitle") or "",
            # Use the description if the enrichment pass populated it; transcripts
            # (longer + cleaner signal) would come from vidiq_video_transcript
            # later if/when we wire that in.
            body=(v.get("description") or v.get("body") or "")[:8000],
            posted_at=posted_at,
            metrics={
                "views": v.get("views") or v.get("viewCount"),
                "likes": v.get("likes") or v.get("likeCount"),
                "comments": v.get("comments") or v.get("commentCount"),
                "vph": v.get("vph"),
                "engagement_rate": v.get("engagementRate"),
                "avg_view_pct": v.get("avg_view_pct") or v.get("averageViewPercentage"),
            },
            extra={
                "channel_id": channel.get("channelId") or v.get("channelId"),
                "channel_title": channel_handle,
                "duration_sec": v.get("videoDuration"),
                "is_owned": channel.get("is_owned", False),
                "tags": v.get("tags") or [],
            },
        ))
    return out


def _clean_reddit_rss_body(html_content: str) -> str:
    """Reddit RSS entry content is HTML: the selftext (if any) followed by a
    'submitted by /u/… [link] [comments]' footer. Strip tags/entities and drop
    the footer so the summarizer sees just the post body."""
    if not html_content:
        return ""
    text = _html.unescape(_TAG_RE.sub(" ", html_content))
    text = _SUBMITTED_RE.split(text, maxsplit=1)[0]  # cut the boilerplate footer
    return re.sub(r"\s+", " ", text).strip()


def signals_from_reddit_rss(feed, *, subreddit: str) -> list[RawSignal]:
    """Convert a parsed Reddit RSS/Atom feed (feedparser result) into
    RawSignals. Reddit blocks the public .json endpoints (403) but still serves
    per-subreddit .rss feeds. RSS omits score/comment counts — fine, since heat
    is volume×novelty×recency and these are already top-of-week posts."""
    out: list[RawSignal] = []
    for e in getattr(feed, "entries", []):
        # Reddit's Atom id is the fullname "t3_<base36>"; strip the prefix so it
        # matches ids from the old JSON path and dedupes against them.
        rid = (e.get("id") or "").replace("t3_", "") or e.get("link") or ""
        if not rid:
            continue
        author = e.get("author") or ""
        if author.startswith("/u/"):
            author = author[3:]
        posted_at = None
        for key in ("published_parsed", "updated_parsed"):
            t = e.get(key)
            if t:
                posted_at = datetime.utcfromtimestamp(calendar.timegm(t))
                break
        content = ""
        if e.get("content"):
            content = e["content"][0].get("value", "")
        content = content or e.get("summary", "")
        tags = e.get("tags") or []
        flair = tags[0].get("term") if tags else None
        out.append(RawSignal(
            platform=SourcePlatform.reddit,
            external_id=rid,
            url=e.get("link"),
            author=author or None,
            title=e.get("title"),
            body=_clean_reddit_rss_body(content),
            posted_at=posted_at,
            metrics={},  # RSS carries no score/comment counts
            extra={"subreddit": subreddit, "flair": flair, "source": "rss"},
        ))
    return out


def signals_from_reddit_listing(listing: dict, *, default_subreddit: str | None = None
                                ) -> list[RawSignal]:
    """Convert a Reddit listing JSON (kind=Listing, data.children=[...])
    into RawSignal objects. Tolerant of Composio's slightly varied shapes."""
    out: list[RawSignal] = []
    children = (
        listing.get("data", {}).get("children")
        or listing.get("children")
        or []
    )
    for ch in children:
        d = ch.get("data", ch) if isinstance(ch, dict) else {}
        if not d:
            continue
        sub = d.get("subreddit") or default_subreddit or "unknown"
        created = d.get("created_utc")
        try:
            posted_at = datetime.utcfromtimestamp(float(created)) if created else None
        except (TypeError, ValueError):
            posted_at = None
        out.append(RawSignal(
            platform=SourcePlatform.reddit,
            external_id=d.get("id") or d.get("name") or d.get("url", ""),
            url=d.get("url") or (
                f"https://reddit.com{d['permalink']}" if d.get("permalink") else None
            ),
            author=d.get("author"),
            title=d.get("title"),
            body=d.get("selftext") or d.get("body") or "",
            posted_at=posted_at,
            metrics={
                "ups": d.get("ups") or d.get("score"),
                "num_comments": d.get("num_comments"),
                "upvote_ratio": d.get("upvote_ratio"),
            },
            extra={"subreddit": sub, "flair": d.get("link_flair_text")},
        ))
    return out
