"""Performance insights: tier each video deterministically, then distill
what_works / what_underperforms narratives with a local model.

Tiers are pure math (no LLM) so they're stable run-to-run:
  breakout       ≥ 2.0× channel median views
  strong         ≥ 1.25×
  as_expected    0.75–1.25×
  underperformed < 0.75×
  evergreen      older than the window but still in the top half
Narratives are an LLM pass over the tiered table — patterns, not per-video
commentary. Runs inside `engine sync-performance` so the snapshot the idea
synthesizer reads always has the fields populated."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, conlist

from ..ollama_client import run_agent
from ..schemas import ChannelAnalytics

log = logging.getLogger("engine.perf_insights")


class PerfInsights(BaseModel):
    what_works: conlist(str, min_length=1, max_length=4)  # type: ignore[valid-type]
    what_underperforms: conlist(str, min_length=0, max_length=3)  # type: ignore[valid-type]
    confidence: float = Field(..., ge=0, le=1)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


def assign_tiers(snapshot: ChannelAnalytics, *, window_days: int = 30) -> None:
    """Mutate snapshot.videos in place with tier labels."""
    views = [v.views for v in snapshot.videos if isinstance(v.views, int)]
    med = _median([float(x) for x in views])
    if not med:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    for v in snapshot.videos:
        if not isinstance(v.views, int):
            v.tier = None
            continue
        ratio = v.views / med
        in_window = True
        if v.publishedAt:
            try:
                pub = datetime.fromisoformat(v.publishedAt.replace("Z", "+00:00"))
                in_window = pub >= cutoff
            except ValueError:
                pass
        if not in_window and ratio >= 1.0:
            v.tier = "evergreen"
        elif ratio >= 2.0:
            v.tier = "breakout"
        elif ratio >= 1.25:
            v.tier = "strong"
        elif ratio >= 0.75:
            v.tier = "as_expected"
        else:
            v.tier = "underperformed"


def fill_narratives(snapshot: ChannelAnalytics, *, cycle_id: str | None = None) -> None:
    """Mutate snapshot.what_works / what_underperforms via a local-model pass."""
    tiered = [v for v in snapshot.videos if v.tier and isinstance(v.views, int)]
    if len(tiered) < 4:
        log.info("only %d tiered videos — skipping narrative pass", len(tiered))
        return
    block = "\n".join(
        f"- [{v.tier}] \"{v.title}\" — {v.views:,} views"
        + (f", {v.likes} likes" if v.likes is not None else "")
        + (f", {v.comments} comments" if v.comments is not None else "")
        + (f" (published {v.publishedAt[:10]})" if v.publishedAt else "")
        for v in tiered
    )
    result, _ = run_agent(
        agent_name="perf_insights",
        prompt_name="perf_insights",
        schema=PerfInsights,
        prompt_vars={
            "video_block": block,
            "current_subs": snapshot.current_subs or "?",
            "subs_gained": snapshot.subs_gained_30d or "?",
        },
        starting_tier="heavy",
        cycle_id=cycle_id,
    )
    if result is None:
        log.warning("perf insights generation failed — leaving narratives empty")
        return
    snapshot.what_works = list(result.what_works)
    snapshot.what_underperforms = list(result.what_underperforms)
    log.info("perf narratives: %d what_works, %d what_underperforms "
             "(confidence %.2f)", len(snapshot.what_works),
             len(snapshot.what_underperforms), result.confidence)
