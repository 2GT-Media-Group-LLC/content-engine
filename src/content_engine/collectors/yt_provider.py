"""YouTube data provider abstraction.

The engine needs YouTube data for three things:
  1. List recent uploads from peer + own channels (ingested as RawSignal).
  2. Score candidate video titles for CTR.
  3. Build the own-channel performance snapshot the idea synthesizer reads.

Historically all three flowed through Composio. After the May 2026 Composio
breach, we moved to VidIQ MCP as the default — but kept Composio as an
optional fallback for users who don't have a VidIQ subscription. This file
defines the contract both providers implement and the factory that picks
between them based on env.

Selection order (in `get_provider()`):
  1. settings.youtube_provider (from YOUTUBE_PROVIDER env, default "vidiq")
  2. If that provider's credentials are missing, fall back to NoopProvider
     so the engine still runs end-to-end without the source.

Callers should treat None returns as "skip / no data" and degrade gracefully.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from ..config import settings
from ..schemas import ChannelAnalytics, VideoRecord

log = logging.getLogger("engine.yt_provider")


# ─── Protocol ─────────────────────────────────────────────────────────────────
@runtime_checkable
class YouTubeProvider(Protocol):
    """Contract for any YouTube data backend."""

    name: str  # "vidiq" | "composio" | "noop"

    def is_available(self) -> bool:
        """True if this provider has the credentials it needs to actually fetch."""
        ...

    def list_channel_videos(
        self, channel: str, limit: int = 12, *, recent: bool = True
    ) -> list[VideoRecord]:
        """Return up to `limit` videos for `channel` (UC… id or @handle).
        recent=True → newest uploads; recent=False → most-popular.
        Empty list on auth failure or transient error (logged)."""
        ...

    def enrich_videos(self, video_ids: list[str]) -> dict[str, dict]:
        """Batch-fetch view/like/comment/description data for the given video
        IDs. Returns a {videoId → metrics_dict} map. Providers that can't
        enrich should return {} so callers degrade gracefully.

        VidIQ's vidiq_get_videos_by_ids is batched (up to 50 IDs per call at
        5 credits per call), so enriching a full collect cycle is cheap."""
        ...

    def score_title(
        self, title: str, *, channel_id: str | None = None,
        video_id: str | None = None, kind: str = "long",
    ) -> float | None:
        """Return CTR score 0-100 or None if the provider can't score."""
        ...

    def get_channel_analytics(
        self, channel_id: str, *, days: int = 30,
    ) -> ChannelAnalytics | None:
        """Return own-channel 30-day analytics, or None if unavailable
        (e.g. Composio v3 doesn't expose vidiq's analytics endpoint)."""
        ...

    def get_comments(self, *, channel: str | None = None,
                     video_id: str | None = None, limit: int = 20) -> list[dict]:
        """Top comment threads for a channel OR a single video. Each dict:
        {text, author, likes, published_at, video_id}. [] when unsupported."""
        ...

    def get_transcript(self, video_id: str) -> str | None:
        """Full transcript text for a video, or None when unavailable."""
        ...

    def find_outliers(self, *, keyword: str | None = None,
                      channels: list[str] | None = None,
                      published_within: str = "thisMonth",
                      limit: int = 20) -> list[dict]:
        """Overperforming videos in the niche (beyond the fixed peer list).
        Each dict: {videoId, title, channelTitle, views, vph, outlier_score,
        publishedAt}. [] when unsupported."""
        ...

    def keyword_research(self, keyword: str) -> dict | None:
        """Search volume / competition / opportunity for a keyword.
        {keyword, volume, competition, score, monthly_searches, related}.
        None when unsupported."""
        ...

    def generate_titles(self, *, title: str, description: str | None = None,
                        previous_titles: list[str] | None = None,
                        n: int = 5) -> list[dict]:
        """Provider-generated title candidates [{title, score}]. [] when
        unsupported — callers keep the locally-generated titles."""
        ...

    def generate_thumbnail(self, *, title: str, description: str | None = None,
                           direction: str | None = None,
                           transcript: str | None = None) -> dict | None:
        """Generate a thumbnail. Returns {url, score, feedback} or None."""
        ...


# ─── Noop fallback ────────────────────────────────────────────────────────────
class NoopProvider:
    """Used when no credentials are configured. Logs once on init, then
    silently no-ops every call. Keeps the engine pipeline running."""
    name = "noop"

    def __init__(self, reason: str = "no provider configured"):
        log.warning("YouTube provider unavailable (%s). Peer/own-channel "
                    "collection and title scoring will be skipped — set "
                    "YOUTUBE_PROVIDER + the matching API key in .env to enable.",
                    reason)

    def is_available(self) -> bool:
        return False

    def list_channel_videos(self, channel, limit=12, *, recent=True):
        return []

    def enrich_videos(self, video_ids):
        return {}

    def score_title(self, title, *, channel_id=None, video_id=None, kind="long"):
        return None

    def get_channel_analytics(self, channel_id, *, days=30):
        return None

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


# ─── Factory ──────────────────────────────────────────────────────────────────
_CACHED_PROVIDER: YouTubeProvider | None = None
_CACHED_NAME: str | None = None


def get_provider(force: str | None = None) -> YouTubeProvider:
    """Return the active YouTube provider, instantiating lazily and caching.

    `force` overrides settings.youtube_provider for one call — use this for
    `--provider` CLI flags or A/B testing. Each force value gets its own
    cached instance so flipping back and forth in tests doesn't pay the
    init cost more than once per name."""
    global _CACHED_PROVIDER, _CACHED_NAME
    target = (force or settings.youtube_provider or "vidiq").strip().lower()
    if _CACHED_PROVIDER is not None and _CACHED_NAME == target:
        return _CACHED_PROVIDER

    provider = _instantiate(target)
    _CACHED_PROVIDER = provider
    _CACHED_NAME = target
    return provider


def reset_provider_cache() -> None:
    """Force the next get_provider() call to re-instantiate. Useful in tests
    or after rotating credentials at runtime."""
    global _CACHED_PROVIDER, _CACHED_NAME
    _CACHED_PROVIDER = None
    _CACHED_NAME = None


def _instantiate(target: str) -> YouTubeProvider:
    if target == "vidiq":
        if not os.getenv("VIDIQ_API_KEY", "").strip():
            return NoopProvider("YOUTUBE_PROVIDER=vidiq but VIDIQ_API_KEY not set")
        from .vidiq_provider import VidIQProvider
        try:
            return VidIQProvider()
        except Exception as e:
            log.error("VidIQProvider init failed: %s", e)
            return NoopProvider(f"vidiq init failed: {e}")

    if target == "composio":
        if not os.getenv("COMPOSIO_API_KEY", "").strip():
            return NoopProvider("YOUTUBE_PROVIDER=composio but COMPOSIO_API_KEY not set")
        from .composio_provider import ComposioProvider
        try:
            return ComposioProvider()
        except Exception as e:
            log.error("ComposioProvider init failed: %s", e)
            return NoopProvider(f"composio init failed: {e}")

    if target == "noop":
        return NoopProvider("YOUTUBE_PROVIDER=noop")

    log.warning("Unknown YOUTUBE_PROVIDER=%r; falling back to noop", target)
    return NoopProvider(f"unknown provider {target!r}")
