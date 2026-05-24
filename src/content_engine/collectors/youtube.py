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

    # Own channel
    own_id = settings.own_channel_id
    own_title = settings.own_channel_title
    if own_id:
        try:
            log.info("fetching %s recent videos via %s", own_title, provider.name)
            vids = provider.list_channel_videos(own_id, per_channel_limit, recent=True)
            n = ingest_signals(signals_from_youtube_videos(
                vids,
                {"channelId": own_id, "channelTitle": own_title, "is_owned": True},
            ))
            total += n
            log.info("  → %s: %d videos", own_title, n)
        except Exception as e:
            errors.append(f"{own_title}: {str(e)[:200]}")
            log.warning("%s failed: %s", own_title, e)
    else:
        log.warning("no own_channel_id configured in channel.yaml — skipping own-channel pull")

    # Peers — load from channel.yaml
    peers = [(p.get("handle"), p.get("name", p.get("handle", "?")))
             for p in settings.peer_channels_full if p.get("handle")]
    for handle, name in peers:
        try:
            log.info("fetching peer %s via %s", handle, provider.name)
            vids = provider.list_channel_videos(handle, per_channel_limit, recent=True)
            n = ingest_signals(signals_from_youtube_videos(
                vids,
                {"channelId": handle, "channelTitle": name, "is_owned": False},
            ))
            total += n
            log.info("  → %s: %d videos", handle, n)
        except Exception as e:
            errors.append(f"{handle}: {str(e)[:200]}")
            log.warning("%s failed: %s", handle, e)

    return {
        "platform": "youtube",
        "provider": provider.name,
        "channels_fetched": (1 if own_id else 0) + len(peers),
        "ingested": total,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
