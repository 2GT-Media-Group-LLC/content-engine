"""Idea generator: takes labeled clusters + channel context → IdeaCandidate(s).

Uses the heaviest local tier by default since synthesis quality matters more
than throughput here. Escalates to fail-open (None) if even heavy can't
produce valid JSON.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from ..config import settings
from ..db import get_conn
from ..ollama_client import run_agent
from ..schemas import IdeaCandidate
from .editor import polish_fields, detect_artifacts

log = logging.getLogger("engine.ideas")

# Prose fields worth a second-pass rewrite. Structured fields (confidence,
# fatigue_score, format, etc.) stay untouched.
_PROSE_FIELDS = ("angle", "why_now", "audience_fit")


def _idea_id(cluster_id: int, label: str) -> str:
    return "idea_" + hashlib.sha1(f"{cluster_id}|{label}".encode()).hexdigest()[:12]


def _load_voice_guide() -> str:
    p = settings.style_dir / "voice_guide.md"
    if p.exists():
        return p.read_text()[:4000]
    return "(voice guide not yet extracted — use neutral, technically literate tone)"


def _load_recent_videos() -> str:
    """Recent own-channel video performance context — what's working on the
    channel right now. Loaded from data/<brand_short>_perf_30d.json (vidiq
    snapshot) so the synthesizer can weight ideas toward formats/topics that
    have actually been performing."""
    p = settings.db_path.parent / f"{settings.brand_short.lower()}_perf_30d.json"
    if p.exists():
        try:
            data = json.loads(p.read_text())
            lines = [
                f"Window: last {data.get('window', '30d')}, +{data.get('subs_gained_30d', 0)} subs",
                "Recent video performance (sorted by views):",
            ]
            for v in data.get("videos", [])[:8]:
                lines.append(
                    f"  - [{v.get('tier','?')}] {v['title']} — {v['views']:,} views, "
                    f"{v['avg_view_pct']}% AVD, {v['likes']} likes, {v['comments']} comments"
                )
            if data.get("what_works"):
                lines.append("WHAT'S WORKING:")
                for w in data["what_works"]: lines.append(f"  + {w}")
            if data.get("what_underperforms"):
                lines.append("WHAT'S UNDERPERFORMING:")
                for w in data["what_underperforms"]: lines.append(f"  - {w}")
            return "\n".join(lines)
        except Exception as e:
            log.warning("failed to parse %s: %s", p.name, e)
    # Fallback when no perf snapshot exists yet.
    return "(no recent performance snapshot — run `engine sync-performance`)"


def _load_prior_ideas_by_fate(days: int = 90) -> dict[str, list[dict]]:
    """Return prior ideas bucketed by fate so the prompt can give the model
    different instructions per bucket. Earlier versions sent every idea as
    'don't re-pitch' undifferentiated; the model couldn't tell rejected
    from pending, so it cheerfully re-pitched anything."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT idea_id, fate, fate_reason, payload_json
               FROM ideas WHERE created_at >= ?
               ORDER BY fate_set_at DESC NULLS LAST, created_at DESC""",
            (cutoff,),
        ).fetchall()
    out: dict[str, list[dict]] = {
        "rejected": [], "parked": [], "produced": [], "pending": []
    }
    for r in rows:
        p = json.loads(r["payload_json"])
        bucket = r["fate"] if r["fate"] in out else "pending"
        out[bucket].append({
            "idea_id": r["idea_id"],
            "angle": p.get("angle", ""),
            "reason": r["fate_reason"] or "",
            "source_signal_ids": p.get("source_signal_ids", []),
        })
    return out


def _format_prior_ideas_for_prompt(buckets: dict[str, list[dict]]) -> str:
    """Render the fate-bucketed prior ideas with bucket-specific guidance."""
    parts = []
    if buckets["rejected"]:
        parts.append("REJECTED — NEVER pitch these angles or close variants:")
        for x in buckets["rejected"][:30]:
            reason = f"  (reason: {x['reason']})" if x['reason'] else ""
            parts.append(f"  ✗ {x['angle'][:180]}{reason}")
    if buckets["parked"]:
        parts.append("\nPARKED — only pitch if a major new event clearly justifies it:")
        for x in buckets["parked"][:15]:
            parts.append(f"  🅿 {x['angle'][:180]}")
    if buckets["produced"]:
        parts.append("\nPRODUCED (already a video) — don't re-pitch, but story shapes that worked here are good signal:")
        for x in buckets["produced"][:15]:
            parts.append(f"  ✓ {x['angle'][:180]}")
    if buckets["pending"]:
        parts.append("\nPENDING (on a current or recent brief, not yet decided) — don't duplicate:")
        for x in buckets["pending"][:25]:
            parts.append(f"  · {x['angle'][:180]}")
    if not parts:
        return "(no prior ideas in memory yet)"
    return "\n".join(parts)


def _rejected_signal_sets(days: int = 90) -> list[frozenset[int]]:
    """For each rejected idea, return the set of signal_ids that cluster used.
    A candidate cluster whose signals overlap heavily with one of these sets
    is the same trend the user already rejected — skip it."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    out: list[frozenset[int]] = []
    with get_conn() as conn:
        for r in conn.execute(
            """SELECT payload_json FROM ideas
               WHERE fate='rejected' AND created_at >= ?""",
            (cutoff,),
        ):
            sig_ids = json.loads(r["payload_json"]).get("source_signal_ids") or []
            if sig_ids:
                out.append(frozenset(sig_ids))
    return out


def _overlap_with_rejected(cluster_signal_ids: list[int],
                           rejected_sets: list[frozenset[int]]) -> float:
    """Return the max Jaccard-like ratio (intersection / candidate size)
    between this cluster's signals and any rejected idea's signals."""
    if not cluster_signal_ids or not rejected_sets:
        return 0.0
    cand = frozenset(cluster_signal_ids)
    best = 0.0
    for r in rejected_sets:
        if not r:
            continue
        # asymmetric: how much of THIS cluster is in the rejected one.
        overlap = len(cand & r) / max(1, len(cand))
        if overlap > best:
            best = overlap
    return best


def _rejected_angle_embeddings() -> list[tuple[str, list[float]]]:
    """One-shot: embed every rejected angle from the last 90 days for
    post-generation similarity check. Returns list of (angle, vector)."""
    cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat()
    angles: list[str] = []
    with get_conn() as conn:
        for r in conn.execute(
            """SELECT payload_json FROM ideas
               WHERE fate IN ('rejected','parked') AND created_at >= ?""",
            (cutoff,),
        ):
            angle = json.loads(r["payload_json"]).get("angle", "").strip()
            if angle:
                angles.append(angle)
    if not angles:
        return []
    try:
        from ..ollama_client import embed
        vecs = embed(angles)
        return list(zip(angles, vecs))
    except Exception as e:
        log.warning("could not embed rejected angles: %s", e)
        return []


def _cosine(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _is_too_similar_to_rejected(angle: str,
                                rejected_vecs: list[tuple[str, list[float]]],
                                threshold: float = 0.78) -> tuple[bool, str, float]:
    """Embed `angle`, compare to each rejected angle vec. Return (match, which, score)."""
    if not rejected_vecs or not angle:
        return (False, "", 0.0)
    try:
        from ..ollama_client import embed
        v = embed(angle)[0]
    except Exception:
        return (False, "", 0.0)
    best_score = 0.0
    best_angle = ""
    for prev_angle, prev_vec in rejected_vecs:
        score = _cosine(v, prev_vec)
        if score > best_score:
            best_score = score
            best_angle = prev_angle
    return (best_score >= threshold, best_angle, best_score)


def _select_diverse_clusters(rows: list, top_n: int, max_per_topic: int = 2,
                              rejected_sets: list[frozenset[int]] | None = None,
                              overlap_threshold: float = 0.5) -> tuple[list, list]:
    """Pick top_n clusters with topic diversity AND no heavy overlap with
    previously-rejected ideas. Returns (selected, skipped_for_overlap)."""
    rejected_sets = rejected_sets or []
    by_topic: dict[str, list] = {}
    skipped_for_overlap: list[dict] = []

    for r in rows:
        topics = json.loads(r["dominant_topics_json"])
        primary = topics[0] if topics else "other"
        # Skip clusters that overlap heavily with anything the user rejected.
        sig_ids = json.loads(r["signal_ids_json"])
        ov = _overlap_with_rejected(sig_ids, rejected_sets)
        if ov >= overlap_threshold:
            skipped_for_overlap.append({"label": r["label"], "overlap": ov})
            continue
        by_topic.setdefault(primary, []).append(r)

    for buck in by_topic.values():
        buck.sort(key=lambda r: r["heat_score"], reverse=True)

    selected: list = []
    counts: dict[str, int] = {}
    while len(selected) < top_n:
        progress = False
        for topic, buck in sorted(by_topic.items(),
                                  key=lambda kv: -kv[1][0]["heat_score"] if kv[1] else 0):
            if counts.get(topic, 0) >= max_per_topic or not buck:
                continue
            selected.append(buck.pop(0))
            counts[topic] = counts.get(topic, 0) + 1
            progress = True
            if len(selected) >= top_n:
                break
        if not progress:
            break

    selected.sort(key=lambda r: r["heat_score"], reverse=True)
    return selected[:top_n], skipped_for_overlap


def generate_for_clusters(cycle_id: str, top_n: int = 5,
                           max_per_topic: int = 2,
                           pool_multiplier: int = 3) -> int:
    """Generate ideas for the top-N clusters, enforcing topic diversity AND
    fate-aware deduplication against prior rejected/parked ideas.

    pool_multiplier controls backfill: we actually select up to
    top_n * pool_multiplier clusters as a candidate pool, then stop the
    generation loop after we've successfully *saved* top_n ideas. This way
    clusters dropped by the post-generation similarity check (model
    regurgitated a previously-rejected angle) or by generation failure
    don't reduce yield below the target."""
    with get_conn() as conn:
        all_rows = conn.execute(
            """SELECT * FROM clusters WHERE cycle_id = ?
               ORDER BY heat_score DESC""",
            (cycle_id,),
        ).fetchall()

    rejected_sets = _rejected_signal_sets()
    rejected_vecs = _rejected_angle_embeddings()
    log.info("dedup context: %d rejected clusters by signal-overlap, "
             "%d rejected/parked angles embedded for similarity check",
             len(rejected_sets), len(rejected_vecs))

    pool_size = max(top_n, top_n * pool_multiplier)
    clusters, skipped = _select_diverse_clusters(
        list(all_rows), pool_size, max_per_topic,
        rejected_sets=rejected_sets,
    )
    if skipped:
        log.info("skipped %d cluster(s) for overlap with rejected ideas: %s",
                 len(skipped), [(s['label'][:40], f"{s['overlap']:.0%}") for s in skipped[:5]])
    log.info("selected %d clusters across %d distinct primary topics "
             "(target=%d, pool=%d for backfill)",
             len(clusters),
             len({json.loads(c['dominant_topics_json'])[0] for c in clusters}) if clusters else 0,
             top_n, pool_size)

    if not clusters:
        log.info("no clusters to generate ideas from")
        return 0

    voice = _load_voice_guide()
    recent_vids = _load_recent_videos()
    prior_buckets = _load_prior_ideas_by_fate()
    prior = _format_prior_ideas_for_prompt(prior_buckets)
    n = 0
    skipped_for_similarity: list[tuple[str, str, float]] = []

    for c in clusters:
        if n >= top_n:
            log.info("hit idea target (%d); stopping after %d of %d pool clusters tried",
                     top_n, clusters.index(c), len(clusters))
            break
        signal_ids = json.loads(c["signal_ids_json"])
        with get_conn() as conn:
            sums = conn.execute(
                f"""SELECT s.platform, s.title, sm.one_line, sm.key_points_json
                    FROM signals s JOIN summaries sm ON sm.signal_id = s.id
                    WHERE s.id IN ({','.join('?' * len(signal_ids))})
                    LIMIT 25""",
                signal_ids,
            ).fetchall()
        summary_block = "\n".join(
            f"- ({s['platform']}) {s['one_line']}" for s in sums
        )

        idea_id = _idea_id(c["id"], c["label"])
        result, _ = run_agent(
            agent_name="generate_idea",
            prompt_name="generate_idea",
            schema=IdeaCandidate,
            prompt_vars={
                "audience_summary": settings.audience_summary,
                "recent_videos": recent_vids,
                "voice_guide": voice,
                "peer_channels": ", ".join(settings.peer_channels),
                "prior_ideas": prior,
                "label": c["label"],
                "heat_score": f"{c['heat_score']:.2f}",
                "avg_sentiment": f"{c['avg_sentiment']:.2f}",
                "dominant_topics": c["dominant_topics_json"],
                "representative_quote": (c["representative_quote"] or "")[:240],
                "summaries": summary_block,
                "signal_ids": json.dumps(signal_ids),
                "cluster_id": c["id"],
                "idea_id": idea_id,
            },
            starting_tier="heavy",
            cycle_id=cycle_id,
        )
        if not result:
            log.warning("idea gen failed for cluster %d", c["id"])
            continue

        # Post-generation similarity check: if the model ignored the rejected
        # list and produced something close to a rejected angle anyway, skip it.
        too_similar, prev_angle, score = _is_too_similar_to_rejected(
            result.angle, rejected_vecs
        )
        if too_similar:
            log.info("idea for cluster %d too similar (%.2f) to rejected "
                     "angle: %r — skipping", c["id"], score, prev_angle[:80])
            skipped_for_similarity.append((result.angle[:90], prev_angle[:90], score))
            continue

        # Pass 2 — prose polish in unconstrained mode. Catches Q4-quant artifacts
        # (doubled words, leaked tokens, weird hyphenation) that JSON-mode bakes in.
        draft = result.model_dump(mode="json")
        artifacts_pre = sum(
            len(detect_artifacts(draft.get(f, ""))) for f in _PROSE_FIELDS
        )
        if artifacts_pre > 0 or any(len(draft.get(f, "")) > 0 for f in _PROSE_FIELDS):
            polished, _ = polish_fields(
                agent_name="generate_idea", draft=draft,
                fields_to_rewrite=list(_PROSE_FIELDS),
                cycle_id=cycle_id, tier="polish",
                schema=IdeaCandidate,
            )
            artifacts_post = sum(
                len(detect_artifacts(polished.get(f, ""))) for f in _PROSE_FIELDS
            )
            log.info("idea %s: polish artifacts %d → %d",
                     draft["idea_id"], artifacts_pre, artifacts_post)
            try:
                final = IdeaCandidate.model_validate(polished)
            except Exception as e:
                log.warning("polished idea %s failed re-validation, keeping draft: %s",
                            draft["idea_id"], str(e)[:200])
                final = result
        else:
            final = result

        with get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO ideas
                   (idea_id, cycle_id, payload_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    final.idea_id, cycle_id,
                    final.model_dump_json(),
                    datetime.utcnow().isoformat(),
                ),
            )
        n += 1

    if skipped_for_similarity:
        log.info("post-gen similarity skipped %d candidate(s)",
                 len(skipped_for_similarity))
        for a, prev, s in skipped_for_similarity:
            log.info("  · %.2f  cand=%r  ≈  prev=%r", s, a, prev)
    return n
