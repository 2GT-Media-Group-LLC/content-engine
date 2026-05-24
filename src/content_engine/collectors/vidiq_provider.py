"""VidIQ YouTube data provider.

Talks to https://mcp.vidiq.com/mcp via the small MCPClient in
content_engine.clients.mcp_http. Replaces all Composio-mediated YouTube
calls with direct VidIQ MCP tools:

  - YOUTUBE_LIST_CHANNEL_VIDEOS  → vidiq_channel_videos
  - VIDIQ_SCORE_TITLE            → vidiq_score_title
  - (new!) auto perf snapshot    → vidiq_channel_analytics
                                   + vidiq_channel_stats
                                   + vidiq_channel_videos

Cost reference (per the tool descriptions VidIQ returns via tools/list):
  vidiq_user_channels      0 credits
  everything else above    5 credits / call

A typical weekly cycle is ~105 credits at default settings.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..clients.mcp_http import MCPClient, MCPError
from ..config import settings
from ..schemas import ChannelAnalytics, VideoRecord

log = logging.getLogger("engine.vidiq")

# Cache of {own_channel_id} for resilience against transient vidiq_user_channels
# outages. Same pattern as composio_client.py's connected-accounts cache.
_CACHE_PATH = Path.home() / ".cache" / "content-engine" / "vidiq_user_channels.json"


class VidIQProvider:
    """YouTubeProvider implementation against VidIQ MCP."""
    name = "vidiq"

    def __init__(self):
        api_key = os.getenv("VIDIQ_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("VIDIQ_API_KEY not set")
        self._client = MCPClient(settings.vidiq_mcp_endpoint, api_key)
        self._authorized_channels: set[str] | None = None  # lazy-loaded

    # ─── Capability check ────────────────────────────────────────────────────
    def is_available(self) -> bool:
        return True  # construction already validated the key

    # ─── List videos for a channel (peers + own) ─────────────────────────────
    def list_channel_videos(
        self, channel: str, limit: int = 12, *, recent: bool = True
    ) -> list[VideoRecord]:
        try:
            raw = self._client.call_tool(
                "vidiq_channel_videos",
                {
                    "channelId": channel,
                    "videoFormat": "long",
                    # recent=True → fresh uploads. recent=False → most-popular.
                    "popular": not recent,
                },
            )
        except MCPError as e:
            log.warning("vidiq_channel_videos(%s) failed: %s", channel, e)
            return []
        except Exception as e:
            log.warning("vidiq_channel_videos(%s) transport error: %s", channel, e)
            return []
        videos = _extract_video_list(raw)
        out: list[VideoRecord] = []
        for v in videos[:limit]:
            rec = _video_record_from_vidiq(v)
            if rec is not None:
                out.append(rec)
        log.debug("vidiq_channel_videos(%s): %d videos", channel, len(out))
        return out

    # ─── Batch enrich (view/like/comment + description) ─────────────────────
    def enrich_videos(self, video_ids: list[str]) -> dict[str, dict]:
        """Batch-fetch metrics + description for each video ID. Returns a
        {videoId → metrics_dict} mapping with keys:

            views, likes, comments      ← per-video counts
            description                  ← full body, fed into RawSignal.body
            duration_sec                 ← parsed from ISO-8601 duration
            tags, topic_categories       ← bonus context for clustering

        vidiq_get_videos_by_ids accepts up to 50 IDs per call at 5 credits
        per call, so we chunk and merge. Tolerant of missing IDs (silently
        absent from the result map)."""
        if not video_ids:
            return {}
        # De-dup while preserving order — saves credits if the caller
        # accidentally passes the same ID across batches.
        seen: set[str] = set()
        unique: list[str] = []
        for vid in video_ids:
            if isinstance(vid, str) and vid and vid not in seen:
                seen.add(vid)
                unique.append(vid)

        out: dict[str, dict] = {}
        for i in range(0, len(unique), 50):
            chunk = unique[i:i + 50]
            try:
                resp = self._client.call_tool(
                    "vidiq_get_videos_by_ids", {"videoIds": chunk},
                )
            except Exception as e:
                log.warning("vidiq_get_videos_by_ids(chunk of %d) failed: %s",
                            len(chunk), e)
                continue
            for v in _iter_video_list(resp):
                vid = v.get("id") or v.get("videoId")
                if not isinstance(vid, str):
                    continue
                out[vid] = {
                    "views": _coerce_int(v.get("viewCount") or v.get("views")),
                    "likes": _coerce_int(v.get("likeCount") or v.get("likes")),
                    "comments": _coerce_int(v.get("commentCount") or v.get("comments")),
                    "description": v.get("description") or "",
                    "duration_sec": _iso_duration_to_sec(v.get("duration")),
                    "tags": v.get("tags") or [],
                    "topic_categories": v.get("topicCategories") or [],
                    "thumbnail": v.get("thumbnail"),
                }
        log.debug("enriched %d/%d video ids via vidiq_get_videos_by_ids",
                  len(out), len(unique))
        return out

    # ─── CTR score for a title ───────────────────────────────────────────────
    def score_title(
        self, title: str, *, channel_id: str | None = None,
        video_id: str | None = None, kind: str = "long",
    ) -> float | None:
        args: dict[str, Any] = {"title": title, "type": kind}
        if channel_id:
            args["channelId"] = channel_id
        if video_id:
            args["videoId"] = video_id
        try:
            raw = self._client.call_tool("vidiq_score_title", args)
        except MCPError as e:
            # 400-class errors include things like "title too long" — log once,
            # let caller fall through to heuristic.
            log.debug("vidiq_score_title rejected %r: %s", title[:60], e)
            return None
        except Exception as e:
            log.debug("vidiq_score_title transport error for %r: %s", title[:60], e)
            return None
        score = _extract_score(raw)
        if score is None:
            log.debug("vidiq_score_title returned no parseable score for %r", title[:60])
        return score

    # ─── Channel analytics snapshot (replaces manual JSON) ───────────────────
    def get_channel_analytics(
        self, channel_id: str, *, days: int = 30,
    ) -> ChannelAnalytics | None:
        """Build the full perf snapshot the idea synthesizer reads.

        Combines three vidiq tools:
          - vidiq_channel_stats   → current_subs, channel_title, totals
          - vidiq_channel_analytics → window aggregates (subs_gained, views, etc.)
          - vidiq_channel_videos  → recent video list with per-video metrics

        Returns None if `channel_id` isn't authorized to this VidIQ account —
        analytics require channel authorization (one-time setup inside VidIQ)."""
        if not self._is_authorized(channel_id):
            log.warning("VidIQ analytics requires channel authorization. "
                        "Authorize %s at https://app.vidiq.com (Settings → "
                        "Channels) and re-run sync-performance.", channel_id)
            return None

        to_date = datetime.utcnow().date()
        from_date = to_date - timedelta(days=days)
        win = f"{days}d"

        # --- channel-level stats (subs, totals, title) ---
        # vidiq_channel_stats returns:
        #   { title, channelId, country, publishedAt,
        #     currentStats: { subscribers, views, videos },
        #     growth:       { subscribersGained, viewsGained, videosPublished },
        #     dailyStats:   [{ date, subscribers, views, videos }, ...] }
        channel_title = channel_id
        current_subs: int | None = None
        videos_published_30d: int | None = None
        growth_subs_gained: int | None = None
        try:
            stats = self._client.call_tool(
                "vidiq_channel_stats",
                {"channelId": channel_id,
                 "from": from_date.isoformat(),
                 "to": to_date.isoformat()},
            )
            if isinstance(stats, dict):
                channel_title = (stats.get("title") or stats.get("channelTitle")
                                  or settings.own_channel_title or channel_id)
                cur = stats.get("currentStats") or {}
                growth = stats.get("growth") or {}
                current_subs = _coerce_int(cur.get("subscribers")
                                            or stats.get("subscriberCount"))
                videos_published_30d = _coerce_int(growth.get("videosPublished"))
                growth_subs_gained = _coerce_int(growth.get("subscribersGained"))
        except Exception as e:
            log.warning("vidiq_channel_stats failed: %s", e)

        # --- aggregate analytics over the window ---
        # YouTube Analytics-grade subs_gained (counts subscriptions, not net delta).
        # Falls back to channel_stats.growth.subscribersGained if this call fails or
        # the channel isn't authorized for YT Analytics (vs. just public stats).
        analytics_subs_gained: int | None = None
        try:
            agg = self._client.call_tool(
                "vidiq_channel_analytics",
                {
                    "channelId": channel_id,
                    "startDate": from_date.isoformat(),
                    "endDate": to_date.isoformat(),
                    "metrics": ["views", "estimatedMinutesWatched",
                                 "subscribersGained", "likes", "comments"],
                },
            )
            analytics_subs_gained = _sum_metric(agg, "subscribersGained")
        except Exception as e:
            log.warning("vidiq_channel_analytics failed: %s", e)

        # Prefer the analytics figure (richer / matches YT Studio); fall back to
        # the simpler net-delta from channel_stats.
        subs_gained = (analytics_subs_gained
                        if analytics_subs_gained is not None
                        else growth_subs_gained)

        # --- recent videos (use popular=false to get fresh uploads in window) ---
        videos: list[VideoRecord] = self.list_channel_videos(
            channel_id, limit=20, recent=True,
        )
        # Tier and per-video win/lose tags are assigned by the synthesizer
        # consumer; we just supply raw metrics.

        snapshot = ChannelAnalytics(
            channel_id=channel_id,
            channel_title=channel_title,
            snapshot_at=datetime.utcnow().isoformat(),
            window=win,
            current_subs=current_subs,
            subs_gained_30d=subs_gained,
            # Prefer the explicit growth count from channel_stats; only fall back
            # to len(videos) if both calls failed (which would be all-None anyway).
            videos_published_30d=videos_published_30d if videos_published_30d is not None else len(videos),
            videos=videos,
            what_works=[],
            what_underperforms=[],
        )
        return snapshot

    # ─── Private helpers ─────────────────────────────────────────────────────
    def _is_authorized(self, channel_id: str) -> bool:
        """Check (with cache + fallback) whether the given channel is authorized
        to this VidIQ account. Required before vidiq_channel_analytics will work."""
        if self._authorized_channels is None:
            self._authorized_channels = self._load_authorized_channels()
        # Match either exact ID or handle alias. vidiq_user_channels returns
        # whatever IDs the user authorized; users may pass either form into
        # get_channel_analytics. Accept any case-insensitive match.
        target = channel_id.lower().lstrip("@")
        return any(
            target == (a or "").lower().lstrip("@") for a in self._authorized_channels
        )

    def _load_authorized_channels(self) -> set[str]:
        """Lazy-load + cache authorized channel list. Falls back to disk cache
        on transient failure (same pattern as composio_client.py)."""
        try:
            raw = self._client.call_tool("vidiq_user_channels", {})
        except Exception as e:
            log.warning("vidiq_user_channels failed: %s — falling back to cache", e)
            return _load_cached_authorized() or set()
        channels = _extract_channel_ids(raw)
        if channels:
            try:
                _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                _CACHE_PATH.write_text(json.dumps(sorted(channels), indent=2))
            except Exception as ce:
                log.debug("could not write vidiq channel cache: %s", ce)
        else:
            log.warning("vidiq_user_channels returned no authorized channels — "
                        "authorize your channel at https://app.vidiq.com")
        return channels


# ─── Module helpers (free functions for easier unit-testing) ──────────────────
def _extract_video_list(raw: Any) -> list[dict]:
    """vidiq_channel_videos may wrap items in {videos: [...]}, {data: {...}},
    or return a list directly. Handle all shapes."""
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []
    for key in ("videos", "items", "data", "results"):
        v = raw.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("videos", "items", "results"):
                if isinstance(v.get(k2), list):
                    return v[k2]
    return []


# Alias — _extract_video_list and _iter_video_list serve the same purpose;
# kept as a separate name in case enrichment ever needs a different shape.
_iter_video_list = _extract_video_list


_ISO_DURATION_RE = re.compile(
    r"^PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$"
)


def _iso_duration_to_sec(d: Any) -> int | None:
    """Parse YouTube's ISO-8601 duration ('PT19M30S' → 1170). Returns None
    on anything unparseable so callers don't have to special-case."""
    if not isinstance(d, str):
        return None
    m = _ISO_DURATION_RE.match(d.strip())
    if not m:
        return None
    parts = m.groupdict(default="0")
    return int(parts["h"]) * 3600 + int(parts["m"]) * 60 + int(parts["s"])


def _video_record_from_vidiq(v: dict) -> VideoRecord | None:
    """Map vidiq's per-video shape to our VideoRecord. Tolerant of missing fields."""
    vid = (v.get("videoId") or v.get("id")
           or (v.get("snippet") or {}).get("resourceId", {}).get("videoId"))
    if not vid:
        return None
    title = (v.get("title")
             or v.get("videoTitle")
             or (v.get("snippet") or {}).get("title") or "")
    published = (v.get("publishedAt") or v.get("publishDate") or v.get("published")
                  or (v.get("snippet") or {}).get("publishedAt"))
    if isinstance(published, (int, float)):
        try:
            published = datetime.utcfromtimestamp(published).isoformat() + "Z"
        except Exception:
            published = None
    return VideoRecord(
        videoId=vid,
        title=title,
        publishedAt=published,
        views=_coerce_int(v.get("viewCount") or v.get("views")),
        likes=_coerce_int(v.get("likeCount") or v.get("likes")),
        comments=_coerce_int(v.get("commentCount") or v.get("comments")),
        avg_view_pct=_coerce_float(
            v.get("averageViewPercentage") or v.get("avg_view_pct") or v.get("avgViewPct")
        ),
        subs_gained=_coerce_int(v.get("subscribersGained") or v.get("subs_gained")),
    )


def _extract_score(raw: Any) -> float | None:
    """Pull a 0-100 CTR score out of vidiq_score_title's response."""
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, dict):
        return None
    for key in ("score", "ctr_score", "ctrScore", "value"):
        v = raw.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    data = raw.get("data")
    if isinstance(data, dict):
        for key in ("score", "ctr_score", "ctrScore", "value"):
            v = data.get(key)
            if isinstance(v, (int, float)):
                return float(v)
    return None


def _sum_metric(agg_response: Any, metric_name: str) -> int | None:
    """vidiq_channel_analytics returns row-style results. Sum a named metric."""
    if not isinstance(agg_response, dict):
        return None
    rows = agg_response.get("rows") or agg_response.get("data") or []
    if not isinstance(rows, list):
        return None
    cols = agg_response.get("columnHeaders") or agg_response.get("columns") or []
    if not cols:
        # Try summing if rows are dicts keyed by metric name directly.
        total = 0
        any_found = False
        for r in rows:
            if isinstance(r, dict) and metric_name in r:
                try:
                    total += int(r[metric_name])
                    any_found = True
                except (TypeError, ValueError):
                    continue
        return total if any_found else None
    # Find column index by name.
    idx = None
    for i, c in enumerate(cols):
        name = c if isinstance(c, str) else (c.get("name") if isinstance(c, dict) else None)
        if name == metric_name:
            idx = i
            break
    if idx is None:
        return None
    total = 0
    any_found = False
    for r in rows:
        if not isinstance(r, list) or idx >= len(r):
            continue
        try:
            total += int(r[idx])
            any_found = True
        except (TypeError, ValueError):
            continue
    return total if any_found else None


def _extract_channel_ids(raw: Any) -> set[str]:
    """vidiq_user_channels response: list (or dict-wrapped list) of channel
    objects. Extract every id / handle we can match against."""
    candidates: list[dict] = []
    if isinstance(raw, list):
        candidates = [c for c in raw if isinstance(c, dict)]
    elif isinstance(raw, dict):
        for key in ("channels", "data", "items", "results"):
            v = raw.get(key)
            if isinstance(v, list):
                candidates = [c for c in v if isinstance(c, dict)]
                break
    out: set[str] = set()
    for c in candidates:
        for key in ("id", "channelId", "channel_id", "handle"):
            v = c.get(key)
            if isinstance(v, str) and v:
                out.add(v)
    return out


def _load_cached_authorized() -> set[str] | None:
    try:
        if _CACHE_PATH.exists():
            data = json.loads(_CACHE_PATH.read_text())
            if isinstance(data, list):
                return set(s for s in data if isinstance(s, str))
    except Exception as e:
        log.debug("could not read vidiq channel cache: %s", e)
    return None


def _coerce_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
