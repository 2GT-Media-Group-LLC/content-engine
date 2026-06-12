"""Composio YouTubeProvider — legacy/fallback path.

Wraps the existing composio_client.get_client() machinery in the
YouTubeProvider Protocol so callers don't need to know which backend is
active. Behavior preserved exactly from the pre-migration code:

  - YOUTUBE_LIST_CHANNEL_VIDEOS via Composio's YouTube toolkit (OAuth)
  - VIDIQ_SCORE_TITLE via Composio's vidiq toolkit (if connected)
  - get_channel_analytics returns None — Composio v3 doesn't expose vidiq's
    analytics endpoint, so this provider can't build the perf snapshot.
    (Callers fall back to the manual data/<brand>_perf_30d.json.)

This file exists primarily so users who don't have a VidIQ subscription can
still set YOUTUBE_PROVIDER=composio and get peer/own channel uploads. Not
the recommended path post-May-2026 Composio breach — see docs/SECURITY.md.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..config import settings
from ..schemas import ChannelAnalytics, VideoRecord
from .composio_client import get_client

log = logging.getLogger("engine.composio_provider")


class ComposioProvider:
    """YouTubeProvider implementation against the Composio SDK."""
    name = "composio"

    def __init__(self):
        self._client = get_client()
        if type(self._client).__name__ == "NoopComposio":
            raise RuntimeError("Composio client returned NoopComposio — "
                                "COMPOSIO_API_KEY missing or invalid.")
        self._youtube_ok: bool | None = None  # cached auth probe

    # ─── Capability check ────────────────────────────────────────────────────
    def is_available(self) -> bool:
        if self._youtube_ok is None:
            self._youtube_ok = self._probe_youtube_auth()
        return self._youtube_ok

    # ─── List videos for a channel ───────────────────────────────────────────
    def list_channel_videos(
        self, channel: str, limit: int = 12, *, recent: bool = True
    ) -> list[VideoRecord]:
        # Composio's YouTube wrapper doesn't expose a "popular vs recent"
        # toggle — it always returns recent uploads in published-at order.
        # The `recent` arg is accepted for Protocol parity but ignored here.
        if not self.is_available():
            return []
        try:
            resp = self._client.execute(
                "YOUTUBE_LIST_CHANNEL_VIDEOS",
                arguments={"channelId": channel, "maxResults": limit},
            )
        except Exception as e:
            log.warning("YOUTUBE_LIST_CHANNEL_VIDEOS(%s) failed: %s", channel, e)
            return []
        if isinstance(resp, dict) and resp.get("_stub"):
            return []
        return _videos_from_youtube_payload(resp)

    # ─── Batch enrich (not supported via Composio) ───────────────────────────
    def enrich_videos(self, video_ids: list[str]) -> dict[str, dict]:
        """Composio's YouTube wrapper doesn't expose a batch metrics endpoint
        — only the per-channel video listing. Returns an empty mapping; the
        engine just won't have view/like data for peer videos on this path."""
        return {}

    # ─── CTR score for a title ───────────────────────────────────────────────
    def score_title(
        self, title: str, *, channel_id: str | None = None,
        video_id: str | None = None, kind: str = "long",
    ) -> float | None:
        args: dict[str, Any] = {"title": title, "type": kind}
        # Composio's vidiq tool doesn't accept channel_id/video_id hints.
        try:
            resp = self._client.execute("VIDIQ_SCORE_TITLE", arguments=args)
        except Exception as e:
            log.debug("VIDIQ_SCORE_TITLE rejected %r: %s", title[:60], e)
            return None
        if not isinstance(resp, dict) or resp.get("_stub"):
            return None
        try:
            score = resp.get("score")
            if score is None:
                score = (resp.get("data") or {}).get("score")
            return float(score) if score is not None else None
        except (TypeError, ValueError):
            return None

    # ─── Channel analytics (NOT supported via Composio) ──────────────────────
    def get_channel_analytics(
        self, channel_id: str, *, days: int = 30,
    ) -> ChannelAnalytics | None:
        log.warning("Composio provider does not support channel analytics. "
                    "Switch to YOUTUBE_PROVIDER=vidiq or maintain "
                    "data/<brand_short>_perf_30d.json manually.")
        return None

    # ─── Extended capabilities (vidiq-only; Composio path degrades) ──────────
    def get_comments(self, *, channel=None, video_id=None, limit=20):
        return []

    def get_transcript(self, video_id):
        return None

    def find_outliers(self, *, keyword=None, channels=None,
                      published_within="thisMonth", limit=20):
        return []

    def keyword_research(self, keyword):
        return None

    def generate_titles(self, *, title, description=None, previous_titles=None, n=5):
        return []

    def generate_thumbnail(self, *, title, description=None, direction=None,
                           transcript=None):
        return None

    # ─── Private helpers ─────────────────────────────────────────────────────
    def _probe_youtube_auth(self) -> bool:
        """Cheap 1-video call to verify the YouTube toolkit is connected."""
        own = settings.own_channel_id
        if not own:
            # No channel configured — can't probe. Assume ok and let real
            # calls fail loudly with whatever the real error is.
            return True
        try:
            resp = self._client.execute(
                "YOUTUBE_LIST_CHANNEL_VIDEOS",
                arguments={"channelId": own, "maxResults": 1},
            )
            if isinstance(resp, dict):
                if resp.get("_stub"):
                    return False
                if not resp.get("successful", True):
                    err = (resp.get("error") or "").lower()
                    if "connected_account" in err or "authentication" in err:
                        return False
            return True
        except Exception as e:
            msg = str(e).lower()
            if "connected_account" in msg or "authentication" in msg:
                return False
            return True  # other error — let caller see it


# ─── Module helpers ───────────────────────────────────────────────────────────
def _videos_from_youtube_payload(resp: Any) -> list[VideoRecord]:
    """Parse the Composio YouTube response envelope into VideoRecords."""
    if not isinstance(resp, dict):
        return []
    data = resp.get("data") or resp
    items = (
        data.get("items")
        or (data.get("data") or {}).get("items")
        or []
    )
    out: list[VideoRecord] = []
    for it in items:
        snip = it.get("snippet") or {}
        rid = snip.get("resourceId") or {}
        vid = (rid.get("videoId")
               or (it.get("id") or {}).get("videoId")
               or it.get("id"))
        if not vid or not isinstance(vid, str):
            continue
        out.append(VideoRecord(
            videoId=vid,
            title=snip.get("title", ""),
            publishedAt=snip.get("publishedAt"),
            # Composio's YouTube response doesn't include view/like counts.
            # Those stay None; the synthesizer handles missing metrics gracefully.
        ))
    return out


# Re-export so the factory doesn't need to import datetime directly
_ = datetime  # keep import for any future analytics support
