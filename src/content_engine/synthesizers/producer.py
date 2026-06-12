"""Production pack agent: turns a green-lit idea into a ready-to-shoot
handoff document with one command (`engine produce <idea_id>`).

Gathers everything pre-production needs:
  1. the idea + its outline (generated on the spot if missing)
  2. source signals with verified URLs
  3. transcripts of the 2 most-viewed related YouTube videos (what the niche
     already said — so the script can add, not repeat)
  4. provider-generated title candidates scored for CTR, merged with the
     idea's own suggestions
  5. optionally an AI-generated thumbnail (22 vidiq credits — opt-in flag)

Output: reports/production/<idea_id>.md — markdown so it drops straight into
Obsidian/Notion/a Claude session for scripting."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from ..collectors.yt_provider import get_provider
from ..config import settings
from ..db import get_conn

log = logging.getLogger("engine.producer")

_PACK_DIR = settings.reports_dir / "production"


def build_production_pack(idea_id: str, *, thumbnail: bool = False) -> Path:
    """Assemble the pack. Raises ValueError for unknown idea ids."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT idea_id, cycle_id, fate, payload_json FROM ideas WHERE idea_id=?",
            (idea_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown idea_id: {idea_id}")
    payload = json.loads(row["payload_json"])
    provider = get_provider()

    # ── 1. Outline (generate if the cycle pass skipped this idea) ────────────
    with get_conn() as conn:
        orow = conn.execute(
            "SELECT payload_json FROM outlines WHERE idea_id=?", (idea_id,),
        ).fetchone()
    outline = json.loads(orow["payload_json"]) if orow else None
    if outline is None:
        log.info("no outline yet for %s — generating now", idea_id)
        from .outlines import generate_for_ideas
        generate_for_ideas(row["cycle_id"])  # fills any missing outlines in cycle
        with get_conn() as conn:
            orow = conn.execute(
                "SELECT payload_json FROM outlines WHERE idea_id=?", (idea_id,),
            ).fetchone()
        outline = json.loads(orow["payload_json"]) if orow else None

    # ── 2. Source signals ────────────────────────────────────────────────────
    sig_ids = payload.get("source_signal_ids", [])
    sources: list[dict] = []
    related_yt: list[dict] = []
    if sig_ids:
        with get_conn() as conn:
            for s in conn.execute(
                f"""SELECT platform, title, url, author, external_id, metrics_json
                    FROM signals WHERE id IN ({','.join('?' * len(sig_ids))})""",
                sig_ids,
            ):
                m = json.loads(s["metrics_json"] or "{}")
                sources.append({"platform": s["platform"], "title": s["title"],
                                 "url": s["url"], "author": s["author"]})
                if s["platform"] == "youtube":
                    related_yt.append({"videoId": s["external_id"],
                                        "title": s["title"],
                                        "views": m.get("views") or 0})

    # ── 3. Transcripts of the top-2 related videos ───────────────────────────
    transcripts: list[dict] = []
    if provider.is_available():
        for v in sorted(related_yt, key=lambda x: -(x["views"] or 0))[:2]:
            text = provider.get_transcript(v["videoId"])
            if text:
                transcripts.append({"title": v["title"],
                                     "videoId": v["videoId"],
                                     "excerpt": text[:3000]})

    # ── 4. Title candidates: provider-generated + idea's own, all scored ─────
    own_titles = payload.get("suggested_titles", [])
    recent_own: list[str] = []
    perf = settings.db_path.parent / f"{settings.brand_short.lower()}_perf_30d.json"
    if perf.exists():
        try:
            recent_own = [v["title"] for v in
                           json.loads(perf.read_text()).get("videos", [])[:10]]
        except Exception:
            pass
    title_board: list[dict] = []
    if provider.is_available():
        generated = provider.generate_titles(
            title=own_titles[0] if own_titles else payload.get("angle", ""),
            description=payload.get("why_now", ""),
            previous_titles=recent_own, n=5,
        )
        for g in generated:
            title_board.append({**g, "origin": "vidiq"})
        for t in own_titles:
            score = provider.score_title(t, channel_id=settings.own_channel_id or None)
            title_board.append({"title": t, "score": score, "origin": "engine"})
    else:
        title_board = [{"title": t, "score": None, "origin": "engine"}
                       for t in own_titles]
    title_board.sort(key=lambda x: -(x.get("score") or 0))

    # ── 5. Thumbnail (opt-in: 22 credits) ────────────────────────────────────
    thumb = None
    if thumbnail and provider.is_available():
        best_title = title_board[0]["title"] if title_board else payload.get("angle", "")
        concepts = payload.get("thumbnail_concepts") or []
        thumb = provider.generate_thumbnail(
            title=best_title,
            description=payload.get("why_now", ""),
            direction=concepts[0] if concepts else None,
            transcript=transcripts[0]["excerpt"] if transcripts else None,
        )

    # ── Render ───────────────────────────────────────────────────────────────
    md = _render_pack(payload, outline, sources, transcripts, title_board, thumb)
    _PACK_DIR.mkdir(parents=True, exist_ok=True)
    out = _PACK_DIR / f"{idea_id}.md"
    out.write_text(md)
    log.info("production pack → %s (%d titles, %d transcripts, thumb=%s)",
             out, len(title_board), len(transcripts), bool(thumb))
    return out


def _render_pack(payload: dict, outline: dict | None, sources: list[dict],
                 transcripts: list[dict], titles: list[dict],
                 thumb: dict | None) -> str:
    L: list[str] = []
    L.append(f"# Production pack — {payload.get('angle', '(no angle)')}")
    L.append(f"_Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} · "
             f"format: {payload.get('format', '?')} · "
             f"confidence: {payload.get('confidence', '?')}_")
    L.append("")
    L.append("## Why now")
    L.append(payload.get("why_now", ""))
    if payload.get("risk_flags"):
        L.append("")
        L.append("**Risk flags:** " + "; ".join(payload["risk_flags"]))
    kd = payload.get("keyword_data") or {}
    if kd.get("keyword"):
        L.append("")
        L.append(f"**Search demand:** \"{kd['keyword']}\" — "
                 f"volume {kd.get('volume', '?')}, "
                 f"competition {kd.get('competition', '?')}"
                 + (f", ~{kd['monthly_searches']:,}/mo"
                    if kd.get("monthly_searches") else ""))

    L.append("")
    L.append("## Title board (highest score first)")
    for t in titles:
        score = f"{t['score']:.0f}" if isinstance(t.get("score"), (int, float)) else "—"
        L.append(f"- [{score}] {t['title']}  `({t['origin']})`")

    if thumb:
        L.append("")
        L.append("## Generated thumbnail")
        L.append(f"![thumbnail]({thumb['url']})")
        if thumb.get("score") is not None:
            L.append(f"_Self-score: {thumb['score']:.0f}_")
    elif payload.get("thumbnail_concepts"):
        L.append("")
        L.append("## Thumbnail concepts")
        for c in payload["thumbnail_concepts"]:
            L.append(f"- {c}")

    if outline:
        L.append("")
        L.append("## Outline")
        L.append(f"**Hook (first {outline.get('cold_open_seconds', 15)}s):** "
                 f"{outline.get('hook', '')}")
        L.append("")
        for i, sec in enumerate(outline.get("sections", []), 1):
            dur = sec.get("duration_sec", 0)
            L.append(f"### {i}. {sec.get('title', '')} (~{dur // 60}m{dur % 60:02d}s)")
            for b in sec.get("beats", []):
                L.append(f"- {b}")
            if sec.get("b_roll_ideas"):
                L.append(f"  - _b-roll: {'; '.join(sec['b_roll_ideas'])}_")
        L.append("")
        L.append(f"**CTA:** {outline.get('cta', '')}")
        if outline.get("voice_notes"):
            L.append("")
            L.append("**Voice notes:** " + "; ".join(outline["voice_notes"]))
        if outline.get("open_questions"):
            L.append("")
            L.append("## Verify before scripting")
            for q in outline["open_questions"]:
                L.append(f"- [ ] {q}")

    L.append("")
    L.append("## Sources")
    for s in sources:
        L.append(f"- [{s['platform']}] [{s['title']}]({s['url']})"
                 + (f" — {s['author']}" if s.get("author") else ""))

    if transcripts:
        L.append("")
        L.append("## What the niche already said (transcript excerpts)")
        L.append("_Add to this conversation; don't repeat it._")
        for t in transcripts:
            L.append("")
            L.append(f"### {t['title']} (youtu.be/{t['videoId']})")
            L.append(f"> {t['excerpt'][:1500]}…")

    return "\n".join(L) + "\n"
