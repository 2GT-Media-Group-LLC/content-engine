"""Comment-mining collector. Audience comments are people literally telling you
what video to make next — the highest-signal source the engine can read.

Pulls top comment threads for the own channel + each peer channel (channel
mode: one provider call per channel — 5 vidiq credits each, ~25/week total)
and ingests question-shaped / request-shaped comments as RawSignals on the
`youtube_comments` platform. The summarizer + clusterer treat them like any
other signal, so a cluster of "how do I back up Proxmox to B2?" comments
competes for heat directly against Reddit threads and HN posts.

Filtering is deliberate: most comments are praise/noise. We keep ones that
look like questions, requests, or pain points, with a like-count assist."""
from __future__ import annotations

import hashlib
import html as _html
import logging
import re
from datetime import datetime

from ..config import settings
from .base import ingest_signals
from ..schemas import RawSignal, SourcePlatform
from .yt_provider import get_provider

log = logging.getLogger("engine.comments")

# A comment is "signal" if it asks, requests, or reports pain — not if it
# just cheers. Heuristics tuned for technical-channel audiences.
_QUESTION = re.compile(r"\?")
_REQUEST = re.compile(
    r"\b(can you|could you|please (do|make|cover)|would love|i'?d love|"
    r"how (do|did|would|about)|what (about|if)|any chance|tutorial on|"
    r"video (on|about)|wish you|you should)\b", re.IGNORECASE)
_PAIN = re.compile(
    r"\b(struggling|stuck|can'?t get|doesn'?t work|broke|failing|"
    r"gave up|frustrat|confus|wish there was)\b", re.IGNORECASE)


_TAG = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """Unescape HTML entities and strip tags YouTube embeds in comment text."""
    return _TAG.sub("", _html.unescape(text)).strip()


def _is_signal(text: str, likes: int) -> bool:
    if len(text) < 15:
        return False
    score = 0
    if _QUESTION.search(text): score += 1
    if _REQUEST.search(text):  score += 2
    if _PAIN.search(text):     score += 2
    if likes >= 3:             score += 1
    # A substantive question is signal even with zero likes — most comments
    # are <2 days old when we mine them, so likes haven't accrued yet.
    if _QUESTION.search(text) and len(text) >= 80: score += 1
    return score >= 2


def collect(force_provider: str | None = None) -> dict:
    """Mine comments from own + peer channels. Skips cleanly when disabled,
    provider unavailable, or provider can't serve comments (composio/noop)."""
    if not settings.comments_enabled:
        return {"platform": "youtube_comments", "ingested": 0,
                "errors": [], "ran_at": datetime.utcnow().isoformat(),
                "skipped": "disabled in channel.yaml"}

    provider = get_provider(force=force_provider)
    total = 0
    kept = 0
    errors: list[str] = []

    channels: list[tuple[str, str, bool]] = []
    if settings.own_channel_id:
        channels.append((settings.own_channel_id, settings.own_channel_title, True))
    for p in settings.peer_channels_full:
        if p.get("handle"):
            channels.append((p["handle"], p.get("name", p["handle"]), False))

    for chan_ref, chan_name, is_owned in channels:
        try:
            threads = provider.get_comments(
                channel=chan_ref, limit=settings.comments_per_channel)
        except Exception as e:
            errors.append(f"{chan_name}: {str(e)[:200]}")
            continue
        if not threads:
            continue
        total += len(threads)
        signals: list[RawSignal] = []
        for c in threads:
            text = _clean(c.get("text", ""))
            likes = int(c.get("likes") or 0)
            if not _is_signal(text, likes):
                continue
            # Stable id: hash of channel + comment text head (comment IDs
            # aren't consistently exposed across response shapes).
            ext_id = "cm_" + hashlib.sha1(
                f"{chan_ref}|{text[:120]}".encode()).hexdigest()[:16]
            posted = None
            if c.get("published_at"):
                try:
                    posted = datetime.fromisoformat(
                        str(c["published_at"]).replace("Z", "+00:00"))
                except ValueError:
                    pass
            vid = c.get("video_id") or ""
            signals.append(RawSignal(
                platform=SourcePlatform.youtube_comments,
                external_id=ext_id,
                url=f"https://youtu.be/{vid}" if vid else None,
                author=c.get("author") or "(viewer)",
                title=f"[comment on {chan_name}] {text[:90]}",
                body=text,
                posted_at=posted,
                metrics={"likes": likes},
                extra={"channel": chan_name, "is_owned_channel": is_owned,
                        "video_id": vid},
            ))
        n = ingest_signals(signals)
        kept += n
        log.info("  → %s: %d/%d comments kept as signal", chan_name, n, len(threads))

    return {
        "platform": "youtube_comments",
        "provider": provider.name,
        "threads_seen": total,
        "ingested": kept,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
