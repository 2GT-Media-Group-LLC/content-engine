"""Render a styled HTML weekly report. Tailwind via CDN keeps it self-contained."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import settings
from ..db import get_conn


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(settings.templates_dir)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
    )


def _gather(cycle_id: str) -> dict:
    with get_conn() as conn:
        cycle = conn.execute(
            "SELECT * FROM cycles WHERE id=?", (cycle_id,)
        ).fetchone()
        clusters = conn.execute(
            """SELECT * FROM clusters WHERE cycle_id=? ORDER BY heat_score DESC""",
            (cycle_id,),
        ).fetchall()
        ideas = conn.execute(
            """SELECT * FROM ideas WHERE cycle_id=?
               ORDER BY json_extract(payload_json, '$.confidence') DESC""",
            (cycle_id,),
        ).fetchall()
        # outlines keyed by idea_id
        outlines_rows = conn.execute(
            """SELECT o.idea_id, o.payload_json FROM outlines o
               JOIN ideas i ON i.idea_id = o.idea_id
               WHERE i.cycle_id=?""",
            (cycle_id,),
        ).fetchall()
        outlines = {r["idea_id"]: json.loads(r["payload_json"]) for r in outlines_rows}
        signal_count = conn.execute(
            """SELECT COUNT(*) AS n FROM signals s
               WHERE EXISTS (SELECT 1 FROM summaries sm WHERE sm.signal_id=s.id)"""
        ).fetchone()["n"]
        platform_breakdown = conn.execute(
            """SELECT platform, COUNT(*) AS n FROM signals s
               WHERE EXISTS (SELECT 1 FROM summaries sm WHERE sm.signal_id=s.id)
               GROUP BY platform ORDER BY n DESC"""
        ).fetchall()
        agent_stats = conn.execute(
            """SELECT agent, model_tier, COUNT(*) AS n,
                      AVG(elapsed_ms) AS avg_ms,
                      AVG(confidence) AS avg_conf,
                      SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errs
               FROM agent_runs WHERE cycle_id=?
               GROUP BY agent, model_tier ORDER BY agent, model_tier""",
            (cycle_id,),
        ).fetchall()
        health_row = conn.execute(
            """SELECT output_json FROM agent_runs
               WHERE cycle_id=? AND agent='pipeline_health'
               ORDER BY created_at DESC LIMIT 1""",
            (cycle_id,),
        ).fetchone()

    return {
        "cycle_id": cycle_id,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "started_at": cycle["started_at"] if cycle else None,
        "finished_at": cycle["finished_at"] if cycle else None,
        "signal_count": signal_count,
        "platform_breakdown": [dict(r) for r in platform_breakdown],
        "clusters": [
            {
                **dict(c),
                "dominant_topics": json.loads(c["dominant_topics_json"]),
                "signal_ids": json.loads(c["signal_ids_json"]),
            }
            for c in clusters
        ],
        "ideas": [
            {
                "row": dict(i),
                "p": json.loads(i["payload_json"]),
                "outline": outlines.get(i["idea_id"]),
            }
            for i in ideas
        ],
        "agent_stats": [dict(r) for r in agent_stats],
        "health": (json.loads(health_row["output_json"])
                    if health_row and health_row["output_json"] else None),
    }


def render_weekly(cycle_id: str, out_path: Path | None = None) -> Path:
    data = _gather(cycle_id)
    env = _env()
    tpl = env.get_template("weekly_report.html.j2")
    html = tpl.render(**data, brand=settings.brand_name)
    out_path = out_path or settings.reports_dir / f"weekly_{cycle_id}.html"
    out_path.write_text(html)
    return out_path
