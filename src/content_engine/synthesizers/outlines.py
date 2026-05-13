"""Outline + sources generator. Runs after idea generation. For each idea, produces
a full ScriptOutline with sections, beats, sources to read, and open questions.

Local-only by default (Qwen 3 30B-A3B Q8 polish tier). Caller can opt into Claude
polish for outlines they decide to ship via `engine polish <idea_id>`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from ..config import settings
from ..db import get_conn
from ..ollama_client import run_agent
from ..schemas import ScriptOutline
from .editor import polish_fields

log = logging.getLogger("engine.outlines")


def _load_voice_guide() -> str:
    p = settings.style_dir / "voice_guide.md"
    if p.exists():
        return p.read_text()[:4000]
    return "(voice guide not yet extracted — use neutral, technically literate tone)"


def _signals_block(conn, signal_ids: list[int]) -> str:
    """Render source signals with their URLs for the outline prompt."""
    if not signal_ids:
        return "(no source signals)"
    placeholders = ",".join("?" * len(signal_ids))
    rows = conn.execute(
        f"""SELECT s.platform, s.title, s.url, s.body, sm.one_line, sm.key_points_json
            FROM signals s LEFT JOIN summaries sm ON sm.signal_id = s.id
            WHERE s.id IN ({placeholders})""",
        signal_ids,
    ).fetchall()
    out = []
    for r in rows:
        body_excerpt = (r["body"] or "")[:300]
        out.append(
            f"- [{r['platform']}] {r['title']}\n"
            f"  url: {r['url']}\n"
            f"  summary: {r['one_line']}\n"
            f"  excerpt: {body_excerpt}"
        )
    return "\n".join(out)


def generate_for_ideas(cycle_id: str, only_top_n: int | None = None) -> int:
    """Generate outlines for ideas in this cycle. If only_top_n is set, only
    process the top-N highest-confidence ideas (saves time on weak ones)."""
    with get_conn() as conn:
        q = """SELECT i.idea_id, i.payload_json FROM ideas i
               LEFT JOIN outlines o ON o.idea_id = i.idea_id
               WHERE i.cycle_id = ? AND o.idea_id IS NULL
               ORDER BY json_extract(i.payload_json, '$.confidence') DESC"""
        if only_top_n:
            q += f" LIMIT {int(only_top_n)}"
        rows = conn.execute(q, (cycle_id,)).fetchall()

    if not rows:
        log.info("no ideas to outline")
        return 0

    voice = _load_voice_guide()
    n = 0

    for r in rows:
        payload = json.loads(r["payload_json"])
        signal_ids = payload.get("source_signal_ids", [])
        with get_conn() as conn:
            sig_block = _signals_block(conn, signal_ids)
            cluster_ids = payload.get("source_cluster_ids", [])
            quote = ""
            if cluster_ids:
                cluster = conn.execute(
                    "SELECT representative_quote FROM clusters WHERE id = ? LIMIT 1",
                    (cluster_ids[0],),
                ).fetchone()
                if cluster and cluster["representative_quote"]:
                    quote = cluster["representative_quote"]

        result, _ = run_agent(
            agent_name="generate_outline",
            prompt_name="generate_outline",
            schema=ScriptOutline,
            prompt_vars={
                "idea_id": payload["idea_id"],
                "voice_guide": voice,
                "format": payload.get("format", "long_form"),
                "angle": payload.get("angle", ""),
                "why_now": payload.get("why_now", ""),
                "audience_fit": payload.get("audience_fit", ""),
                "risk_flags": json.dumps(payload.get("risk_flags", [])),
                "source_signals": sig_block,
                "representative_quote": quote,
            },
            starting_tier="heavy",
            cycle_id=cycle_id,
        )
        if not result:
            log.warning("outline gen failed for idea %s", payload["idea_id"])
            continue

        # Polish the prose-y fields (hook, cta) on the polish tier — they're free-text and
        # benefit from the cleaner unconstrained model. Sections/sources are structured.
        draft = result.model_dump(mode="json")
        polished, _ = polish_fields(
            agent_name="generate_outline", draft=draft,
            fields_to_rewrite=["hook", "cta"],
            cycle_id=cycle_id, tier="polish",
        )
        try:
            final = ScriptOutline.model_validate(polished)
        except Exception as e:
            log.warning("polished outline %s failed re-validation, keeping draft: %s",
                        payload["idea_id"], str(e)[:200])
            final = result

        with get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO outlines
                   (idea_id, payload_json, created_at, model_tier)
                   VALUES (?, ?, ?, ?)""",
                (
                    payload["idea_id"], final.model_dump_json(),
                    datetime.utcnow().isoformat(), "heavy+polish",
                ),
            )
        n += 1
    return n
