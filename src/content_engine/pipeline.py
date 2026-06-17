"""Orchestrator. Chains: collect → summarize → cluster → enrich → ideate →
score → keywords → outline → verify → render.

Every stage runs under a watchdog: a hard per-stage time budget enforced by a
timer thread that force-exits the process if exceeded. Born from a real
incident — a weekly launchd run once zombied for 525 minutes holding the run
lock. A killed stage exits with code 124 after marking the cycle failed;
launchd just tries again next week with a clean slate.

Stage timings are recorded as a `pipeline_health` agent run so the weekly
report can show where the time went."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from uuid import uuid4

from .db import get_conn, init_db, record_agent_run
from .processors import cluster as cluster_proc
from .processors import summarize as sum_proc
from .processors import transcripts as transcript_proc
from .reports.render import render_weekly
from .synthesizers import ideas as idea_syn
from .synthesizers import outlines as outline_syn
from .synthesizers.citation_verifier import verify_cycle
from .synthesizers.keyword_check import validate_cycle_ideas
from .synthesizers.retro import maybe_refresh as refresh_editorial_guide
from .synthesizers.title_scorer import score_cycle_ideas

log = logging.getLogger("engine.pipeline")

# Per-stage hard budgets (minutes). Generous — these exist to kill zombies,
# not to rush healthy work. Worst-case total ≈ 4h, vs. the 8.75h zombie.
_STAGE_BUDGET_MIN = {
    "retro": 15,
    "summarize": 60,
    "cluster": 10,
    "transcripts": 15,
    "resummarize": 25,
    "ideate": 75,
    "titles": 15,
    "keywords": 15,
    "outlines": 60,
    "citations": 15,
    "render": 5,
}


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


@contextmanager
def _stage(name: str, cycle_id: str, timings: dict[str, float]):
    """Run a pipeline stage under a hard time budget. If the budget trips,
    mark the cycle failed and force-exit (124) — a hung Ollama call can't be
    cancelled from Python reliably, so we kill the whole process and let the
    scheduler retry next cycle."""
    budget_min = _STAGE_BUDGET_MIN.get(name, 30)

    def _kill():
        log.error("WATCHDOG: stage %r exceeded %d min budget — force-exiting "
                  "(cycle %s marked failed)", name, budget_min, cycle_id)
        try:
            finish_cycle(cycle_id, f"failed: watchdog killed stage {name}")
        except Exception:
            pass
        os._exit(124)

    timer = threading.Timer(budget_min * 60, _kill)
    timer.daemon = True
    timer.start()
    t0 = time.monotonic()
    try:
        yield
    finally:
        timer.cancel()
        timings[name] = round(time.monotonic() - t0, 1)
        log.info("[%s] stage %s done in %.1fs", cycle_id, name, timings[name])


def run_processing_only(cycle_id: str, *, top_clusters: int = 5,
                         cluster_distance: float = 0.32) -> dict:
    """Skip collection (assumes signals already in DB). Useful while iterating."""
    timings: dict[str, float] = {}

    # Pre-flight: a missing routed model used to surface mid-cluster as a slow
    # 10-min watchdog kill (every cluster_label 404'd on the absent fast tier).
    # Catch it here in ~1s with a copy-pasteable fix, and mark the cycle failed
    # cleanly instead of force-exiting later.
    from .ollama_client import ensure_tiers_available, OllamaError
    try:
        ensure_tiers_available()
    except OllamaError as e:
        log.error("pre-flight check failed — aborting cycle %s:\n%s", cycle_id, e)
        finish_cycle(cycle_id, f"failed: {str(e).splitlines()[0]}")
        raise

    with _stage("retro", cycle_id, timings):
        log.info("[%s] refreshing editorial guide from triage history…", cycle_id)
        retro = refresh_editorial_guide(cycle_id)

    with _stage("summarize", cycle_id, timings):
        log.info("[%s] summarizing pending signals…", cycle_id)
        n_sum = sum_proc.summarize_pending(cycle_id, limit=200)

    with _stage("cluster", cycle_id, timings):
        log.info("[%s] clustering recent summaries (threshold=%.2f)…",
                 cycle_id, cluster_distance)
        n_clust = cluster_proc.cluster_recent(
            cycle_id, days=7, distance_threshold=cluster_distance)

    with _stage("transcripts", cycle_id, timings):
        log.info("[%s] fetching transcripts for top-cluster videos…", cycle_id)
        tx = transcript_proc.enrich_top_clusters(cycle_id, top_n=top_clusters)

    n_resum = 0
    if tx.get("fetched"):
        with _stage("resummarize", cycle_id, timings):
            log.info("[%s] re-summarizing %d transcript-enriched signal(s)…",
                     cycle_id, tx["fetched"])
            n_resum = sum_proc.summarize_pending(cycle_id, limit=tx["fetched"] + 5)

    with _stage("ideate", cycle_id, timings):
        log.info("[%s] generating ideas for top %d clusters…", cycle_id, top_clusters)
        n_ideas = idea_syn.generate_for_clusters(cycle_id, top_n=top_clusters)

    with _stage("titles", cycle_id, timings):
        log.info("[%s] scoring suggested titles…", cycle_id)
        title_summary = score_cycle_ideas(cycle_id)

    with _stage("keywords", cycle_id, timings):
        log.info("[%s] validating search demand per idea…", cycle_id)
        kw_summary = validate_cycle_ideas(cycle_id)

    with _stage("outlines", cycle_id, timings):
        log.info("[%s] generating outlines for top 3 ideas…", cycle_id)
        n_outlines = outline_syn.generate_for_ideas(cycle_id, only_top_n=3)

    with _stage("citations", cycle_id, timings):
        log.info("[%s] verifying citations…", cycle_id)
        cite_summary = verify_cycle(cycle_id)

    # Persist stage health BEFORE rendering so the report can show it.
    with get_conn() as conn:
        record_agent_run(
            conn, cycle_id=cycle_id, agent="pipeline_health", model_tier="-",
            output={"stage_seconds": timings,
                     "editorial_guide_refreshed": bool(retro.get("refreshed")),
                     "transcripts_fetched": tx.get("fetched", 0)},
            elapsed_ms=int(sum(timings.values()) * 1000),
        )

    with _stage("render", cycle_id, timings):
        log.info("[%s] rendering report…", cycle_id)
        report_path = render_weekly(cycle_id)

    finish_cycle(cycle_id, "ok")
    return {
        "cycle_id": cycle_id,
        "editorial_guide": "refreshed" if retro.get("refreshed") else "current",
        "summarized": n_sum,
        "clusters": n_clust,
        "transcripts": tx.get("fetched", 0),
        "resummarized": n_resum,
        "ideas": n_ideas,
        "title_scoring": title_summary,
        "keywords": kw_summary,
        "outlines": n_outlines,
        "citations": cite_summary,
        "stage_seconds": timings,
        "report_path": str(report_path),
    }
