"""YouTube collector — provider-agnostic.

Pulls recent uploads from peer channels + the configured own channel via
whichever YouTubeProvider is active (vidiq by default, composio as
fallback). The collector itself just sequences the work and feeds results
into the shared signal-ingest layer; provider-specific quirks (auth probes,
field-name differences, MCP transport) live in
collectors/yt_provider.py + collectors/{vidiq,composio}_provider.py.

The own-channel analytics snapshot (data/<brand_short>_perf_30d.json) is
written by `engine sync-performance` (see cli.py), not this collector.
"""
from __future__ import annotations

import logging
from datetime import datetime

from ..config import settings
from .base import ingest_signals, signals_from_youtube_videos
from .yt_provider import get_provider

log = logging.getLogger("engine.youtube")


def _merge_enrichment(video, metrics: dict | None):
    """Merge enrichment metrics (views/likes/comments/description) back into a
    VideoRecord. Returns a dict ready for signals_from_youtube_videos.

    Why a dict instead of a mutated VideoRecord: VideoRecord doesn't have a
    `body`/`description` field (it's the snapshot schema, not the signal
    schema). The signal ingester reads both shapes, so emitting a flat dict
    with everything keeps the schema clean."""
    base = video.model_dump(exclude_none=False) if hasattr(video, "model_dump") else dict(video)
    if metrics:
        # Only overwrite metric fields the enrichment actually filled in;
        # never clobber a non-None value with None.
        for k in ("views", "likes", "comments"):
            v = metrics.get(k)
            if v is not None:
                base[k] = v
        # Bonus fields the ingester will pick up (body, duration, tags).
        if metrics.get("description"):
            base["description"] = metrics["description"]
        if metrics.get("duration_sec") is not None:
            base["videoDuration"] = metrics["duration_sec"]
        if metrics.get("tags"):
            base["tags"] = metrics["tags"]
    return base


def collect(per_channel_limit: int = 12, *, force_provider: str | None = None) -> dict:
    """Pull recent videos from peer channels + own channel.
    Skips cleanly (with `_not_authorized` in errors) if the active provider
    has no credentials. `force_provider` overrides settings.youtube_provider
    for this call — used by `engine collect --provider <name>`."""
    provider = get_provider(force=force_provider)
    log.info("youtube collector using provider=%s", provider.name)

    total = 0
    errors: list[str] = []

    if not provider.is_available():
        log.warning("YouTube provider %s not available — skipping autonomous YT pull. "
                    "Check VIDIQ_API_KEY / COMPOSIO_API_KEY and YOUTUBE_PROVIDER in .env.",
                    provider.name)
        return {
            "platform": "youtube",
            "provider": provider.name,
            "channels_fetched": 0,
            "ingested": 0,
            "errors": [f"{provider.name}_not_authorized"],
            "ran_at": datetime.utcnow().isoformat(),
        }

    # Phase 1: gather bare video lists for every channel we care about.
    # For own channel we want RECENT uploads (the brief is about what the
    # channel just published / what's next). For peer channels we want their
    # MOST-POPULAR videos (proven trend signal: "what's working in this niche
    # historically") rather than just last-week's uploads.
    own_id = settings.own_channel_id
    own_title = settings.own_channel_title
    fetched: list[tuple[dict, list]] = []  # [(channel_meta, [VideoRecord, ...])]

    if own_id:
        try:
            log.info("fetching %s recent videos via %s", own_title, provider.name)
            vids = provider.list_channel_videos(own_id, per_channel_limit, recent=True)
            fetched.append(
                ({"channelId": own_id, "channelTitle": own_title, "is_owned": True}, vids)
            )
            log.info("  → %s: %d videos (recent uploads)", own_title, len(vids))
        except Exception as e:
            errors.append(f"{own_title}: {str(e)[:200]}")
            log.warning("%s failed: %s", own_title, e)
    else:
        log.warning("no own_channel_id configured in channel.yaml — skipping own-channel pull")

    # Peers — load from channel.yaml, pull most-popular videos as trend signal.
    peers = [(p.get("handle"), p.get("name", p.get("handle", "?")))
             for p in settings.peer_channels_full if p.get("handle")]
    for handle, name in peers:
        try:
            log.info("fetching peer %s (most-popular) via %s", handle, provider.name)
            vids = provider.list_channel_videos(handle, per_channel_limit, recent=False)
            fetched.append(
                ({"channelId": handle, "channelTitle": name, "is_owned": False}, vids)
            )
            log.info("  → %s: %d videos (most-popular)", handle, len(vids))
        except Exception as e:
            errors.append(f"{handle}: {str(e)[:200]}")
            log.warning("%s failed: %s", handle, e)

    # Phase 2: batch-enrich every video with views/likes/comments/description.
    # Costs 5 credits per 50 IDs (one MCP call per chunk); ~60 IDs in a typical
    # cycle = 10 credits total. Providers that can't enrich return {}; the
    # ingest path tolerates that and just leaves the metrics fields as None.
    all_ids: list[str] = [v.videoId for _, vlist in fetched for v in vlist if v.videoId]
    enrichment: dict[str, dict] = {}
    if all_ids:
        try:
            enrichment = provider.enrich_videos(all_ids)
            log.info("enriched %d/%d videos with views/likes/comments/description",
                     len(enrichment), len(all_ids))
        except Exception as e:
            log.warning("video enrichment failed (continuing without metrics): %s", e)

    # Phase 3: merge enrichment into each VideoRecord, then ingest as signals.
    for channel_meta, vids in fetched:
        try:
            merged = [_merge_enrichment(v, enrichment.get(v.videoId)) for v in vids]
            n = ingest_signals(signals_from_youtube_videos(merged, channel_meta))
            total += n
        except Exception as e:
            errors.append(f"{channel_meta.get('channelTitle','?')}: {str(e)[:200]}")
            log.warning("ingest failed for %s: %s", channel_meta.get("channelTitle"), e)

    return {
        "platform": "youtube",
        "provider": provider.name,
        "channels_fetched": (1 if own_id else 0) + len(peers),
        "ingested": total,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
