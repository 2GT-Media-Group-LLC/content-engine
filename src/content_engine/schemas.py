"""Pydantic contracts for every agent input/output. Strict by design — a failed
parse is a signal to retry with a stronger model, not to paper over."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, conlist


# ─── Source / signal ──────────────────────────────────────────────────────────
class SourcePlatform(str, Enum):
    reddit = "reddit"
    youtube = "youtube"
    instagram = "instagram"
    linkedin = "linkedin"
    discord = "discord"
    gmail = "gmail"
    web = "web"
    google_docs = "google_docs"
    blog = "blog"            # vendor blogs + tech press via RSS/Atom
    hackernews = "hackernews"
    github = "github"        # GitHub releases
    youtube_comments = "youtube_comments"  # audience questions mined from comments


class RawSignal(BaseModel):
    """A unit of raw collected content (a Reddit post, YT video, etc.)."""
    platform: SourcePlatform
    external_id: str = Field(..., description="Stable id within source platform")
    url: str | None = None
    author: str | None = None
    title: str | None = None
    body: str | None = None
    posted_at: datetime | None = None
    metrics: dict = Field(default_factory=dict, description="upvotes/views/comments/etc")
    extra: dict = Field(default_factory=dict)
    collected_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Processor outputs ────────────────────────────────────────────────────────
class TopicTag(str, Enum):
    homelab = "homelab"
    selfhost = "self-hosting"
    ai_local = "ai-local"
    ai_cloud = "ai-cloud"
    virtualization = "virtualization"
    networking = "networking"
    storage = "storage"
    security = "security"
    hardware = "hardware"
    software = "software"
    proxmox = "proxmox"
    docker = "docker"
    kubernetes = "kubernetes"
    other = "other"


class Sentiment(str, Enum):
    very_negative = "very_negative"
    negative = "negative"
    neutral = "neutral"
    positive = "positive"
    very_positive = "very_positive"


class SignalSummary(BaseModel):
    """Output of the per-signal summarizer agent."""
    signal_id: int
    one_line: str = Field(..., max_length=200)
    key_points: conlist(str, min_length=1, max_length=5)  # type: ignore[valid-type]
    topics: conlist(TopicTag, min_length=1, max_length=4)  # type: ignore[valid-type]
    sentiment: Sentiment
    novelty: float = Field(..., ge=0, le=1, description="0=stale topic, 1=fresh")
    confidence: float = Field(..., ge=0, le=1)


# ─── Cluster / trend ──────────────────────────────────────────────────────────
class TrendCluster(BaseModel):
    """A cluster of related signals representing a trend."""
    cluster_id: int
    label: str
    signal_ids: list[int]
    dominant_topics: list[TopicTag]
    avg_sentiment: float = Field(..., ge=-1, le=1)
    heat_score: float = Field(..., ge=0, description="signal volume × recency × velocity")
    representative_quote: str | None = None


# ─── Idea + outline ───────────────────────────────────────────────────────────
class VideoFormat(str, Enum):
    short = "short"  # < 60s
    quick_win = "quick_win"  # 5-10 min tutorial
    long_form = "long_form"  # 15+ min deep dive
    journey = "journey"  # narrative / project log


class IdeaCandidate(BaseModel):
    idea_id: str = Field(..., description="stable hash, used for dedupe")
    angle: str = Field(..., max_length=300)
    why_now: str = Field(..., max_length=400)
    audience_fit: str
    format: VideoFormat
    suggested_titles: conlist(str, min_length=3, max_length=5)  # type: ignore[valid-type]
    thumbnail_concepts: conlist(str, min_length=2, max_length=4)  # type: ignore[valid-type]
    cross_post: dict[Literal["instagram", "linkedin", "discord"], str] = Field(default_factory=dict)
    risk_flags: list[str] = Field(default_factory=list,
                                   description="e.g. 'divisive: proxmox', 'sponsor conflict'")
    fatigue_score: float = Field(..., ge=0, le=1, description="1 = burnt out")
    confidence: float = Field(..., ge=0, le=1)
    source_cluster_ids: list[int] = Field(default_factory=list)
    source_signal_ids: list[int] = Field(default_factory=list)


class OutlineSection(BaseModel):
    """A single beat / section in the script outline."""
    title: str = Field(..., max_length=120)
    duration_sec: int = Field(..., ge=10, le=600,
                               description="estimated screen time for this section")
    beats: conlist(str, min_length=1, max_length=8)  # type: ignore[valid-type]
    b_roll_ideas: list[str] = Field(default_factory=list)


class ResearchSource(BaseModel):
    """A pointer to something the user should read/watch before producing this video."""
    url: str
    kind: Literal["reddit", "github", "docs", "blog", "youtube", "paper", "other"]
    title: str
    why_it_matters: str = Field(..., max_length=240)
    # Set by the citation verifier post-pass. True = URL was in source signals,
    # False = model invented or extrapolated this URL — DO NOT trust before checking.
    verified: bool = Field(default=False)


class ScriptOutline(BaseModel):
    idea_id: str
    hook: str = Field(..., max_length=600,
                       description="cold-open script — what's said in the first ~15 seconds")
    cold_open_seconds: int = Field(default=15, ge=5, le=60)
    # Relaxed min_length — too-tight constraints make Q4 models loop in JSON mode.
    sections: conlist(OutlineSection, min_length=2, max_length=12)  # type: ignore[valid-type]
    cta: str = Field(..., max_length=240)
    estimated_runtime_min: float = Field(..., gt=0)
    voice_notes: list[str] = Field(default_factory=list,
                                    description="cadence/tone reminders from voice guide")
    sources: conlist(ResearchSource, min_length=1, max_length=10)  # type: ignore[valid-type]
    open_questions: list[str] = Field(default_factory=list,
                                       description="things to verify before scripting")
    confidence: float = Field(..., ge=0, le=1)


# ─── Editorial critique ───────────────────────────────────────────────────────
class IdeaCritique(BaseModel):
    """Output of the critic pass — a quality gate between generation and the
    brief. Schema-constrained so the verdict is machine-actionable."""
    hook_strength: float = Field(..., ge=0, le=1,
                                  description="would the first 15s earn a click+stay")
    specificity: float = Field(..., ge=0, le=1,
                                description="concrete claim vs. generic listicle energy")
    freshness: float = Field(..., ge=0, le=1,
                              description="novel for this channel vs. fatigue territory")
    audience_fit: float = Field(..., ge=0, le=1)
    overall: float = Field(..., ge=0, le=1)
    verdict: Literal["pass", "revise"]
    feedback: str = Field(..., max_length=500,
                           description="actionable notes for one revision round")
    confidence: float = Field(..., ge=0, le=1)


# ─── Provider-agnostic YouTube shapes ─────────────────────────────────────────
# These are the shapes the engine consumes internally, regardless of which
# YouTube data provider (vidiq, composio, future) produced them. Providers
# normalize their wire formats into these models so collectors and the perf
# snapshot writer don't care where the data came from.
class VideoRecord(BaseModel):
    """One video, normalized across providers. Field names match what
    signals_from_youtube_videos() and _load_recent_videos() already consume."""
    videoId: str
    title: str
    publishedAt: str | None = None   # ISO 8601 string; keep as str for snapshot round-trip
    # Optional metrics — providers fill what they have. Missing = unknown.
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    avg_view_pct: float | None = None
    min_watched: int | None = None
    subs_gained: int | None = None
    tier: str | None = None  # "breakout" | "strong" | "evergreen" | ... — synthesizer-assigned


class ChannelAnalytics(BaseModel):
    """Channel-level snapshot for the idea synthesizer's performance context.
    Matches the existing data/<brand_short>_perf_30d.json schema consumed by
    synthesizers/ideas.py:_load_recent_videos()."""
    channel_id: str
    channel_title: str
    snapshot_at: str  # ISO timestamp
    window: str = "30d"
    current_subs: int | None = None
    subs_gained_30d: int | None = None
    videos_published_30d: int | None = None
    videos: list[VideoRecord] = Field(default_factory=list)
    # Optional narrative summaries — providers may compute or leave empty for the
    # synthesizer to fill from raw metrics.
    what_works: list[str] = Field(default_factory=list)
    what_underperforms: list[str] = Field(default_factory=list)


# ─── Agent envelope ───────────────────────────────────────────────────────────
class AgentResult(BaseModel):
    """Wraps every agent call. Lets the harness make routing decisions."""
    agent: str
    model_tier: str
    output: dict | None = None
    error: str | None = None
    confidence: float | None = None
    elapsed_ms: int
    raw_response: str | None = None
