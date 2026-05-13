"""Embed summaries, cluster them, label clusters. Pure local — no Anthropic spend."""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from pydantic import BaseModel, Field
from sklearn.cluster import AgglomerativeClustering

from ..config import settings
from ..db import get_conn
from ..ollama_client import embed, run_agent
from ..schemas import TopicTag

log = logging.getLogger("engine.cluster")


class _ClusterLabel(BaseModel):
    cluster_id: int
    label: str = Field(..., max_length=120)
    representative_quote: str | None = Field(None, max_length=240)
    confidence: float = Field(..., ge=0, le=1)


def _sentiment_to_score(s: str) -> float:
    return {
        "very_negative": -1.0, "negative": -0.5, "neutral": 0.0,
        "positive": 0.5, "very_positive": 1.0,
    }.get(s, 0.0)


def _heat(signal_count: int, avg_novelty: float, avg_age_hours: float) -> float:
    """Volume × novelty, decayed by age. Tunable."""
    recency = 1.0 / (1.0 + avg_age_hours / 48.0)  # half-life ~2 days
    return signal_count * (0.4 + 0.6 * avg_novelty) * recency


def cluster_recent(cycle_id: str, days: int = 7, distance_threshold: float = 0.32) -> int:
    """Cluster summaries from the last N days. Returns cluster count."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.id, s.title, s.posted_at, s.platform,
                      sm.one_line, sm.key_points_json, sm.topics_json,
                      sm.sentiment, sm.novelty
               FROM signals s
               JOIN summaries sm ON sm.signal_id = s.id
               WHERE s.posted_at >= ? OR s.posted_at IS NULL
               ORDER BY s.id""",
            (cutoff,),
        ).fetchall()

    if len(rows) < 2:
        log.info("cluster: only %d summaries, skipping", len(rows))
        return 0

    texts = [
        f"{r['one_line']} :: {' / '.join(json.loads(r['key_points_json']))}"
        for r in rows
    ]
    log.info("cluster: embedding %d summaries", len(texts))
    vecs = np.array(embed(texts))

    # Cosine distance via normalized vectors + euclidean.
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1
    vecs_n = vecs / norms

    clusterer = AgglomerativeClustering(
        n_clusters=None, metric="cosine",
        linkage="average", distance_threshold=distance_threshold,
    )
    labels = clusterer.fit_predict(vecs_n)

    cluster_ids: list[int] = []
    for lbl in sorted(set(labels)):
        member_idxs = [i for i, l in enumerate(labels) if l == lbl]
        if len(member_idxs) < 1:
            continue
        members = [rows[i] for i in member_idxs]

        signal_ids = [m["id"] for m in members]
        topic_counter: Counter[str] = Counter()
        for m in members:
            for t in json.loads(m["topics_json"]):
                topic_counter[t] += 1
        dominant = [t for t, _ in topic_counter.most_common(3)]

        sentiments = [_sentiment_to_score(m["sentiment"]) for m in members]
        avg_sent = float(np.mean(sentiments))

        novelties = [float(m["novelty"]) for m in members]
        avg_nov = float(np.mean(novelties))

        ages: list[float] = []
        for m in members:
            if m["posted_at"]:
                try:
                    posted = datetime.fromisoformat(m["posted_at"])
                    # Normalize to naive UTC for arithmetic with utcnow().
                    if posted.tzinfo is not None:
                        posted = posted.astimezone(tz=None).replace(tzinfo=None)
                    age = (datetime.utcnow() - posted).total_seconds() / 3600
                    ages.append(max(0.0, age))
                except (ValueError, TypeError):
                    pass
        avg_age = float(np.mean(ages)) if ages else 72.0
        heat = _heat(len(members), avg_nov, avg_age)

        # Ask local model to label + pick rep quote.
        summary_block = "\n".join(
            f"- ({m['platform']}) {m['one_line']}" for m in members[:20]
        )
        # Insert placeholder cluster row first to get an id.
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO clusters
                   (cycle_id, label, signal_ids_json, dominant_topics_json,
                    avg_sentiment, heat_score, representative_quote, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cycle_id, "(unlabeled)", json.dumps(signal_ids),
                    json.dumps(dominant), avg_sent, heat, None,
                    datetime.utcnow().isoformat(),
                ),
            )
            cluster_db_id = cur.lastrowid

        labeled, _ = run_agent(
            agent_name="cluster_label",
            prompt_name="cluster_label",
            schema=_ClusterLabel,
            prompt_vars={
                "cluster_id": cluster_db_id,
                "signal_ids": json.dumps(signal_ids[:20]),
                "dominant_topics": json.dumps(dominant),
                "avg_sentiment": f"{avg_sent:.2f}",
                "heat_score": f"{heat:.2f}",
                "summaries": summary_block,
            },
            starting_tier="fast",
            cycle_id=cycle_id,
        )
        if labeled:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE clusters SET label=?, representative_quote=? WHERE id=?",
                    (labeled.label, labeled.representative_quote, cluster_db_id),
                )
        cluster_ids.append(cluster_db_id)

    log.info("cluster: created %d clusters from %d signals", len(cluster_ids), len(rows))
    return len(cluster_ids)
