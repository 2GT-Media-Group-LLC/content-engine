"""Outlier-scan collector. Finds breakout videos across the WHOLE niche, not
just the configured peer list — guards against peer-list myopia.

Uses the provider's find_outliers (vidiq_outliers: videos massively
overperforming their channel's baseline) per configured keyword. A video
blowing up on a 4k-sub channel is often a leading indicator of an
underserved topic; the breakout score is stored in metrics so the heat
formula's volume×novelty path picks it up via the summarizer."""
from __future__ import annotations

import logging
from datetime import datetime

from ..config import settings
from .base import ingest_signals
from ..schemas import RawSignal, SourcePlatform
from .yt_provider import get_provider

log = logging.getLogger("engine.outliers")


def collect(force_provider: str | None = None) -> dict:
    if not settings.outliers_enabled:
        return {"platform": "youtube", "collector": "outliers", "ingested": 0,
                "errors": [], "ran_at": datetime.utcnow().isoformat(),
                "skipped": "disabled in channel.yaml"}

    provider = get_provider(force=force_provider)
    total = 0
    errors: list[str] = []
    # Exclude own + peer channels — those are covered by the youtube collector.
    known = {settings.own_channel_title.lower()}
    known.update(p.get("name", "").lower() for p in settings.peer_channels_full)

    for kw in settings.outlier_keywords:
        try:
            vids = provider.find_outliers(
                keyword=kw, published_within="thisMonth",
                limit=settings.outliers_per_keyword)
        except Exception as e:
            errors.append(f"{kw}: {str(e)[:200]}")
            continue
        signals: list[RawSignal] = []
        for v in vids:
            if (v.get("channelTitle") or "").lower() in known:
                continue
            posted = None
            if v.get("publishedAt"):
                try:
                    posted = datetime.fromisoformat(
                        str(v["publishedAt"]).replace("Z", "+00:00"))
                except ValueError:
                    pass
            signals.append(RawSignal(
                platform=SourcePlatform.youtube,
                external_id=v["videoId"],
                url=f"https://youtu.be/{v['videoId']}",
                author=v.get("channelTitle") or "(unknown channel)",
                title=v.get("title") or "",
                body="",
                posted_at=posted,
                metrics={
                    "views": v.get("views"),
                    "vph": v.get("vph"),
                    "outlier_score": v.get("outlier_score"),
                },
                extra={
                    "channel_id": v.get("channelId"),
                    "channel_title": v.get("channelTitle"),
                    "is_owned": False,
                    "outlier": True,
                    "outlier_keyword": kw,
                },
            ))
        n = ingest_signals(signals)
        total += n
        log.info("  → outliers[%s]: %d breakout videos ingested", kw, n)

    return {
        "platform": "youtube",
        "collector": "outliers",
        "provider": provider.name,
        "ingested": total,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
