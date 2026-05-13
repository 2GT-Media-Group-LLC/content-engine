"""Per-signal summarizer agent. Runs on `mid` tier by default, escalates on
low confidence."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from ..config import settings
from ..db import get_conn
from ..ollama_client import run_agent
from ..schemas import SignalSummary

log = logging.getLogger("engine.summarize")


def summarize_pending(cycle_id: str, limit: int = 100) -> int:
    """Summarize signals that don't yet have a summary. Returns count summarized."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.* FROM signals s
               LEFT JOIN summaries sm ON sm.signal_id = s.id
               WHERE sm.signal_id IS NULL
               ORDER BY s.posted_at DESC NULLS LAST
               LIMIT ?""",
            (limit,),
        ).fetchall()

    n = 0
    for row in rows:
        body = (row["body"] or "")[:6000]  # keep prompt under context budget
        metrics = json.loads(row["metrics_json"] or "{}")
        result, _ = run_agent(
            agent_name="summarize_signal",
            prompt_name="summarize_signal",
            schema=SignalSummary,
            prompt_vars={
                "signal_id": row["id"],
                "platform": row["platform"],
                "title": row["title"] or "",
                "author": row["author"] or "",
                "url": row["url"] or "",
                "metrics": json.dumps(metrics),
                "body": body or "(no body)",
            },
            starting_tier="mid",
            cycle_id=cycle_id,
        )
        if result is None:
            log.warning("summarize failed for signal %s", row["id"])
            continue

        with get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO summaries
                   (signal_id, one_line, key_points_json, topics_json, sentiment,
                    novelty, confidence, model_tier, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"], result.one_line,
                    json.dumps(result.key_points),
                    json.dumps([t.value for t in result.topics]),
                    result.sentiment.value, result.novelty, result.confidence,
                    settings.mid.name, datetime.utcnow().isoformat(),
                ),
            )
        n += 1
    return n
