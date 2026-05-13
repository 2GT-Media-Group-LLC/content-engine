"""Orchestrator. Chains: collect → summarize → cluster → ideate → render."""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from .db import get_conn, init_db
from .processors import cluster as cluster_proc
from .processors import summarize as sum_proc
from .reports.render import render_weekly
from .synthesizers import ideas as idea_syn
from .synthesizers import outlines as outline_syn
from .synthesizers.citation_verifier import verify_cycle
from .synthesizers.title_scorer import score_cycle_ideas

log = logging.getLogger("engine.pipeline")


def new_cycle(notes: str | None = None) -> str:
    init_db()
    cid = "c_" + datetime.utcnow().strftime("%Y%m%d_%H%M") + "_" + uuid4().hex[:6]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO cycles (id, started_at, status, notes) VALUES (?, ?, 'running', ?)",
            (cid, datetime.utcnow().isoformat(), notes),
        )
    log.info("started cycle %s", cid)
    return cid


def finish_cycle(cycle_id: str, status: str = "ok") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE cycles SET finished_at=?, status=? WHERE id=?",
            (datetime.utcnow().isoformat(), status, cycle_id),
        )


def run_processing_only(cycle_id: str, *, top_clusters: int = 5,
                         cluster_distance: float = 0.32) -> dict:
    """Skip collection (assumes signals already in DB). Useful while iterating."""
    log.info("[%s] summarizing pending signals…", cycle_id)
    n_sum = sum_proc.summarize_pending(cycle_id, limit=200)

    log.info("[%s] clustering recent summaries (threshold=%.2f)…", cycle_id, cluster_distance)
    n_clust = cluster_proc.cluster_recent(cycle_id, days=7, distance_threshold=cluster_distance)

    log.info("[%s] generating ideas for top %d clusters…", cycle_id, top_clusters)
    n_ideas = idea_syn.generate_for_clusters(cycle_id, top_n=top_clusters)

    log.info("[%s] scoring suggested titles…", cycle_id)
    title_summary = score_cycle_ideas(cycle_id)

    log.info("[%s] generating outlines for top 3 ideas…", cycle_id)
    n_outlines = outline_syn.generate_for_ideas(cycle_id, only_top_n=3)

    log.info("[%s] verifying citations…", cycle_id)
    cite_summary = verify_cycle(cycle_id)

    log.info("[%s] rendering report…", cycle_id)
    report_path = render_weekly(cycle_id)
    finish_cycle(cycle_id, "ok")
    return {
        "cycle_id": cycle_id,
        "summarized": n_sum,
        "clusters": n_clust,
        "ideas": n_ideas,
        "title_scoring": title_summary,
        "outlines": n_outlines,
        "citations": cite_summary,
        "report_path": str(report_path),
    }
