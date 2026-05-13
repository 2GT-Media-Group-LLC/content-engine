"""YouTube collector via Composio's YOUTUBE_LIST_CHANNEL_VIDEOS (OAuth, free).
Pulls recent uploads from peer channels + the 2GT channel and ingests them as
RawSignals.

The 2GT analytics snapshot (data/2gt_perf_30d.json) is NOT refreshed here —
that needs vidiq's analytics API which isn't exposed in Composio v3. Use
`engine sync-performance` (manual MCP path) until vidiq lands in Composio."""
from __future__ import annotations

import logging
from datetime import datetime

from ..config import settings
from .base import ingest_signals, signals_from_youtube_videos
from .composio_client import get_client

log = logging.getLogger("engine.youtube")


def _list_videos(client, channel_ref: str, max_results: int = 12) -> list[dict]:
    """Returns a list of {videoId, title, publishedAt} dicts (normalized for
    signals_from_youtube_videos). Tolerant of YouTube API response shape
    differences across Composio versions."""
    resp = client.execute(
        "YOUTUBE_LIST_CHANNEL_VIDEOS",
        arguments={"channelId": channel_ref, "maxResults": max_results},
    )
    if resp.get("_stub"):
        return []
    # The Composio wrapper returns the full envelope; data is under .data.
    data = resp.get("data") or resp
    items = (
        data.get("items")
        or data.get("data", {}).get("items")
        or []
    )
    out: list[dict] = []
    for it in items:
        snip = it.get("snippet") or {}
        rid = snip.get("resourceId") or {}
        vid = rid.get("videoId") or it.get("id", {}).get("videoId") or it.get("id")
        if not vid:
            continue
        out.append({
            "videoId": vid,
            "title": snip.get("title", ""),
            "publishedAt": snip.get("publishedAt", ""),
        })
    return out


def _has_youtube_auth(client) -> bool:
    """Cheap probe: try one channel, see if it returns the auth-required error."""
    try:
        resp = client.execute(
            "YOUTUBE_LIST_CHANNEL_VIDEOS",
            arguments={"channelId": settings.own_channel_id, "maxResults": 1},
        )
        if resp.get("_stub"):
            return False
        # Composio returns successful=False + an error message when auth missing.
        if isinstance(resp, dict) and not resp.get("successful", True):
            err = (resp.get("error") or "").lower()
            if "connected_account" in err or "authentication" in err:
                return False
        return True
    except Exception as e:
        if "connected_account" in str(e) or "authentication" in str(e):
            return False
        return True  # other error — let caller see it


def collect(per_channel_limit: int = 12) -> dict:
    """Pull recent videos from peer channels + own channel.
    Skips cleanly if YouTube isn't OAuth'd in this Composio account."""
    client = get_client()
    total = 0
    errors: list[str] = []

    if not _has_youtube_auth(client):
        log.warning(
            "YouTube not connected on this Composio account. "
            "Connect via app.composio.dev → Apps → YouTube → Connect, "
            "or keep using the MCP path manually. Skipping autonomous YT pull."
        )
        return {
            "platform": "youtube",
            "channels_fetched": 0,
            "ingested": 0,
            "errors": ["youtube_not_authorized"],
            "ran_at": datetime.utcnow().isoformat(),
        }

    # Own channel
    own_id = settings.own_channel_id
    own_title = settings.own_channel_title
    if own_id:
        try:
            log.info("fetching %s recent videos", own_title)
            vids = _list_videos(client, own_id, per_channel_limit)
            n = ingest_signals(signals_from_youtube_videos(
                vids, {"channelId": own_id, "channelTitle": own_title, "is_owned": True}))
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
            log.info("fetching peer %s", handle)
            vids = _list_videos(client, handle, per_channel_limit)
            n = ingest_signals(signals_from_youtube_videos(
                vids, {"channelId": handle, "channelTitle": name, "is_owned": False}))
            total += n
            log.info("  → %s: %d videos", handle, n)
        except Exception as e:
            errors.append(f"{handle}: {str(e)[:200]}")
            log.warning("%s failed: %s", handle, e)

    return {
        "platform": "youtube",
        "channels_fetched": (1 if own_id else 0) + len(peers),
        "ingested": total,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
