"""Keyword validation: ground each idea in real YouTube search demand.

For every idea in the cycle:
  1. fast-tier local pass extracts the natural search phrase for the angle
  2. provider.keyword_research returns volume / competition / opportunity
  3. result lands in payload["keyword_data"], rendered in the brief

This answers the question the CTR title score can't: is anyone actually
searching for this? An idea can have a 75-scoring title into a 12-volume
keyword void. Costs 5 vidiq credits per idea (~25/week)."""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from ..collectors.yt_provider import get_provider
from ..db import get_conn
from ..ollama_client import run_agent

log = logging.getLogger("engine.keywords")


class SearchPhrase(BaseModel):
    keyword: str = Field(..., min_length=3, max_length=60)
    confidence: float = Field(..., ge=0, le=1)


def validate_cycle_ideas(cycle_id: str) -> dict:
    """Attach keyword_data to every idea in the cycle. Returns summary."""
    provider = get_provider()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT idea_id, payload_json FROM ideas WHERE cycle_id=?",
            (cycle_id,),
        ).fetchall()
    if not rows:
        return {"ideas_checked": 0}

    checked = 0
    researched = 0
    for r in rows:
        payload = json.loads(r["payload_json"])
        if payload.get("keyword_data"):
            continue  # already validated (re-run safety)
        angle = payload.get("angle", "")
        if not angle:
            continue

        phrase_result, _ = run_agent(
            agent_name="extract_keyword",
            prompt_name="extract_keyword",
            schema=SearchPhrase,
            prompt_vars={
                "angle": angle,
                "topics": ", ".join(payload.get("risk_flags", [])[:2]) or "general",
            },
            starting_tier="fast",
            cycle_id=cycle_id,
        )
        if phrase_result is None:
            continue
        checked += 1
        kw = phrase_result.keyword.strip().lower()

        kd: dict = {"keyword": kw}
        if provider.is_available():
            research = provider.keyword_research(kw)
            if research:
                kd.update({
                    "volume": research.get("volume"),
                    "competition": research.get("competition"),
                    "score": research.get("score"),
                    "monthly_searches": research.get("monthly_searches"),
                    "related": research.get("related", [])[:5],
                })
                researched += 1

        payload["keyword_data"] = kd
        with get_conn() as conn:
            conn.execute(
                "UPDATE ideas SET payload_json=? WHERE idea_id=?",
                (json.dumps(payload), r["idea_id"]),
            )

    log.info("keyword validation: %d phrase(s) extracted, %d researched via %s",
             checked, researched, provider.name)
    return {"ideas_checked": checked, "researched": researched}
