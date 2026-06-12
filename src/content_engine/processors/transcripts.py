"""Transcript enrichment for cluster-winning YouTube videos.

Descriptions tell the summarizer what a video claims to be about; transcripts
tell it what the video actually says. Fetching transcripts for everything
would burn credits (5/video), so this runs AFTER clustering and only enriches
YouTube signals that landed in the top clusters — the ones that will feed
idea generation.

Flow per enriched signal:
  1. fetch transcript via the active provider
  2. UPDATE signals.body with a trimmed transcript (description kept as prefix)
  3. DELETE its summary row → summarize_pending() picks it up again
The caller re-runs summarize_pending afterwards so ideation sees the richer
summaries. Cluster membership/heat are not recomputed — acceptable, the
cluster was already hot enough to win on metadata alone."""
from __future__ import annotations

import json
import logging

from ..collectors.yt_provider import get_provider
from ..db import get_conn

log = logging.getLogger("engine.transcripts")

# Hard cap on provider calls per cycle: 8 transcripts = 40 vidiq credits.
_MAX_TRANSCRIPTS_PER_CYCLE = 8
_MAX_PER_CLUSTER = 2
_BODY_CAP = 7000  # keep within summarizer's 6000-char read + headroom


def enrich_top_clusters(cycle_id: str, top_n: int = 5) -> dict:
    """Fetch transcripts for up to _MAX_TRANSCRIPTS_PER_CYCLE YouTube videos
    in this cycle's top-N clusters. Returns counts for the pipeline log."""
    provider = get_provider()
    if not provider.is_available():
        return {"fetched": 0, "skipped": "provider unavailable"}

    with get_conn() as conn:
        clusters = conn.execute(
            """SELECT id, signal_ids_json FROM clusters
               WHERE cycle_id=? ORDER BY heat_score DESC LIMIT ?""",
            (cycle_id, top_n),
        ).fetchall()

    fetched = 0
    failed = 0
    enriched_ids: list[int] = []

    for c in clusters:
        if fetched >= _MAX_TRANSCRIPTS_PER_CYCLE:
            break
        sig_ids = json.loads(c["signal_ids_json"])
        if not sig_ids:
            continue
        with get_conn() as conn:
            yt_rows = conn.execute(
                f"""SELECT id, external_id, body, extra_json FROM signals
                    WHERE id IN ({','.join('?' * len(sig_ids))})
                      AND platform='youtube'""",
                sig_ids,
            ).fetchall()
        done_in_cluster = 0
        for row in yt_rows:
            if fetched >= _MAX_TRANSCRIPTS_PER_CYCLE or done_in_cluster >= _MAX_PER_CLUSTER:
                break
            extra = json.loads(row["extra_json"] or "{}")
            if extra.get("transcript_fetched"):
                continue  # already enriched in a previous cycle
            text = provider.get_transcript(row["external_id"])
            if not text:
                failed += 1
                continue
            # Keep the description as a prefix (it has links/specs the
            # transcript won't), then the transcript.
            desc = (row["body"] or "").strip()
            new_body = (desc + "\n\n[TRANSCRIPT]\n" + text)[:_BODY_CAP]
            extra["transcript_fetched"] = True
            with get_conn() as conn:
                conn.execute(
                    "UPDATE signals SET body=?, extra_json=? WHERE id=?",
                    (new_body, json.dumps(extra), row["id"]),
                )
                # Drop the old summary so summarize_pending re-does it
                # with the transcript in view.
                conn.execute("DELETE FROM summaries WHERE signal_id=?",
                             (row["id"],))
            fetched += 1
            done_in_cluster += 1
            enriched_ids.append(row["id"])

    if fetched:
        log.info("[%s] fetched %d transcript(s) for top clusters "
                 "(%d failed/unavailable); re-summarize will pick them up",
                 cycle_id, fetched, failed)
    return {"fetched": fetched, "failed": failed, "signal_ids": enriched_ids}
