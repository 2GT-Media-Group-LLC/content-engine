"""Post-mortem calibration: compare what the engine PREDICTED about produced
ideas against what ACTUALLY happened on YouTube.

For every idea with fate=produced and a linked video ID:
  1. fetch current stats via the provider (batched — 5 credits per 50 videos)
  2. upsert a video_performance row (feeds the GUI /performance page)
  3. grade the prediction: confidence + best title score vs. how the video
     performed relative to the channel's median views

The rolling summary lands in data/calibration.md, which the retro agent reads
when distilling the editorial guide — closing the loop: predictions → reality
→ adjusted taste → better predictions."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from ..collectors.yt_provider import get_provider
from ..config import settings
from ..db import get_conn

log = logging.getLogger("engine.postmortem")

CALIBRATION_PATH = settings.db_path.parent / "calibration.md"


def _channel_median_views() -> float | None:
    """Median views from the perf snapshot — the bar a video is judged against."""
    p = settings.db_path.parent / f"{settings.brand_short.lower()}_perf_30d.json"
    if not p.exists():
        return None
    try:
        vids = json.loads(p.read_text()).get("videos", [])
        views = sorted(v["views"] for v in vids
                       if isinstance(v.get("views"), (int, float)))
        if not views:
            return None
        return float(views[len(views) // 2])
    except Exception:
        return None


def _grade(actual_views: int | None, median: float | None) -> str:
    if actual_views is None or not median:
        return "unknown"
    ratio = actual_views / median
    if ratio >= 2.0:  return "breakout"
    if ratio >= 1.25: return "overperformed"
    if ratio >= 0.75: return "as_expected"
    if ratio >= 0.4:  return "underperformed"
    return "flopped"


def run_postmortem(cycle_id: str | None = None) -> dict:
    """Fetch reality for all produced ideas; write calibration summary."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT idea_id, produced_video_id, fate_set_at, payload_json
               FROM ideas
               WHERE fate='produced' AND produced_video_id IS NOT NULL"""
        ).fetchall()
    if not rows:
        log.info("no produced ideas with linked videos — nothing to calibrate. "
                 "Link with: engine fate <idea_id> produced --video <youtube_id>")
        return {"checked": 0}

    provider = get_provider()
    video_ids = [r["produced_video_id"] for r in rows]
    metrics = provider.enrich_videos(video_ids) if provider.is_available() else {}
    median = _channel_median_views()

    lines = [
        "# Prediction calibration — produced ideas vs. reality",
        f"_Updated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} · "
        f"channel median views (30d window): "
        f"{int(median):,}_" if median else "_channel median unknown — run engine sync-performance_",
        "",
    ]
    checked = 0
    grades: dict[str, int] = {}
    for r in rows:
        p = json.loads(r["payload_json"])
        vid = r["produced_video_id"]
        m = metrics.get(vid) or {}
        views = m.get("views")
        # Best predicted title score, if scoring ran for this idea.
        tscores = p.get("title_scores") or []
        best_title_score = tscores[0]["score"] if tscores else None
        grade = _grade(views, median)
        grades[grade] = grades.get(grade, 0) + 1
        checked += 1

        with get_conn() as conn:
            conn.execute(
                """INSERT INTO video_performance
                   (video_id, idea_id, title, views, likes, comments, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(video_id) DO UPDATE SET
                     views=excluded.views, likes=excluded.likes,
                     comments=excluded.comments, fetched_at=excluded.fetched_at""",
                (vid, r["idea_id"], p.get("angle", "")[:200], views,
                 m.get("likes"), m.get("comments"),
                 datetime.utcnow().isoformat()),
            )

        views_s = f"{views:,}" if isinstance(views, int) else "?"
        conf = p.get("confidence")
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
        ts_s = f"{best_title_score:.0f}" if isinstance(best_title_score, (int, float)) else "?"
        lines.append(
            f"- **{grade.upper()}** — \"{p.get('angle','')[:90]}\"  \n"
            f"  predicted: confidence {conf_s}, best title score {ts_s}, "
            f"format {p.get('format','?')} → actual: {views_s} views "
            f"({m.get('likes') or '?'} likes, {m.get('comments') or '?'} comments)"
        )

    if checked:
        lines.append("")
        lines.append("## Pattern summary")
        lines.append(", ".join(f"{k}: {v}" for k, v in sorted(grades.items())))
        hi_conf_flops = [
            r for r in rows
            if json.loads(r["payload_json"]).get("confidence", 0) >= 0.75
            and _grade((metrics.get(r["produced_video_id"]) or {}).get("views"),
                        median) in ("underperformed", "flopped")
        ]
        if hi_conf_flops:
            lines.append(f"⚠ {len(hi_conf_flops)} high-confidence idea(s) "
                          "underperformed — the engine is overconfident in "
                          "this territory; weigh its enthusiasm down.")

    CALIBRATION_PATH.write_text("\n".join(lines) + "\n")
    log.info("post-mortem: %d video(s) graded → %s (written to %s)",
             checked, grades, CALIBRATION_PATH.name)
    return {"checked": checked, "grades": grades,
            "path": str(CALIBRATION_PATH)}
