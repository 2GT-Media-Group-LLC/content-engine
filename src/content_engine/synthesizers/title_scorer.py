"""Title scoring post-pass for ideas. Two backends:

- `vidiq`: real CTR scoring via vidiq's title scorer (needs Composio Python SDK
  + COMPOSIO_API_KEY in the environment). Costs 5 credits per call.
- `heuristic`: deterministic fallback. Cheap, runs offline, captures the
  obvious clickability levers (length, numbers, parentheticals, all-caps,
  brackets, year tags, etc.).

Pipeline always falls back to heuristic if vidiq is unreachable, so we never
crash a cycle for missing API access. Each score is tagged with its source.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

from ..db import get_conn

log = logging.getLogger("engine.titles")

# Heuristic levers — empirically what tends to lift YouTube CTR for tech content.
_NUMBERS = re.compile(r"\b\d{1,4}\b")
_QUESTION = re.compile(r"\?")
_PARENS = re.compile(r"\([^)]+\)")
_YEAR = re.compile(r"\b(20\d{2})\b")
_VS = re.compile(r"\bvs\.?\b", re.IGNORECASE)
_NEGATIVE = re.compile(r"\b(stop|don't|never|why|wrong|broke|killed|dead|trap|sucks?)\b", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(I|my|I'm|I've)\b")


def _heuristic_score(title: str) -> float:
    """Return 0-100. Calibrated against patterns common in technical-channel
    breakouts (homelab / self-host / virtualization / AI niches)."""
    if not title:
        return 0.0
    s = 50.0  # neutral baseline

    n = len(title)
    # Length sweet spot 35-65 chars.
    if 35 <= n <= 65:      s += 8
    elif 20 <= n < 35:     s += 4
    elif 65 < n <= 80:     s += 2
    elif n < 20:           s -= 8
    elif n > 90:           s -= 12

    if _NUMBERS.search(title):    s += 4
    if _YEAR.search(title):       s += 3
    if _QUESTION.search(title):   s += 3
    if _PARENS.search(title):     s += 2
    if _VS.search(title):         s += 5
    if _NEGATIVE.search(title):   s += 6   # contrarian/curiosity-driven
    if _FIRST_PERSON.search(title): s += 4 # first-person POV ("Why I…" framing)

    # Penalties
    if title.isupper():           s -= 10  # ALL CAPS feels spammy
    if title.count("!") > 1:      s -= 4
    if title.lower().startswith("how to"):  s -= 2  # generic
    if "ultimate guide" in title.lower():   s -= 3
    if any(p in title.lower() for p in ("you won't believe", "shocking", "click here")):
        s -= 25

    return max(0.0, min(100.0, round(s, 1)))


@dataclass
class TitleScore:
    title: str
    score: float
    source: str  # "vidiq" or "heuristic"


def _score_vidiq(title: str) -> float | None:
    """Try vidiq via Composio Python SDK. Returns None if unreachable."""
    api_key = os.getenv("COMPOSIO_API_KEY")
    if not api_key:
        return None
    try:
        # Lazy import — composio_core is optional.
        from composio import Composio  # type: ignore

        client = Composio(api_key=api_key)
        result = client.tools.execute(
            "VIDIQ_SCORE_TITLE",
            arguments={"title": title, "type": "long"},
        )
        # Vidiq returns a 'score' key. Schema may vary; be defensive.
        data = result if isinstance(result, dict) else {}
        return float(data.get("score", data.get("data", {}).get("score", 0)))
    except Exception as e:
        log.debug("vidiq score failed for %r: %s", title[:40], str(e)[:120])
        return None


def score_titles(titles: list[str]) -> list[TitleScore]:
    """Score a list of titles. Tries vidiq first, falls back to heuristic."""
    out: list[TitleScore] = []
    for t in titles:
        v = _score_vidiq(t)
        if v is not None:
            out.append(TitleScore(title=t, score=v, source="vidiq"))
        else:
            out.append(TitleScore(title=t, score=_heuristic_score(t), source="heuristic"))
    return out


def score_cycle_ideas(cycle_id: str) -> dict:
    """For each idea in the cycle, score its suggested_titles and persist
    rankings into a new `title_scores` JSON field on the idea payload."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT idea_id, payload_json FROM ideas WHERE cycle_id=?", (cycle_id,)
        ).fetchall()

    n_ideas = 0
    n_titles = 0
    sources_used: dict[str, int] = {}
    for r in rows:
        payload = json.loads(r["payload_json"])
        titles = payload.get("suggested_titles", [])
        if not titles:
            continue
        scored = score_titles(titles)
        scored_dicts = [
            {"title": s.title, "score": s.score, "source": s.source}
            for s in sorted(scored, key=lambda x: -x.score)
        ]
        payload["title_scores"] = scored_dicts
        for s in scored:
            sources_used[s.source] = sources_used.get(s.source, 0) + 1
        n_ideas += 1
        n_titles += len(scored)
        with get_conn() as conn:
            conn.execute(
                "UPDATE ideas SET payload_json=? WHERE idea_id=?",
                (json.dumps(payload), r["idea_id"]),
            )

    summary = {
        "ideas_scored": n_ideas,
        "titles_scored": n_titles,
        "sources": sources_used,
    }
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agent_runs
               (cycle_id, agent, model_tier, output_json, elapsed_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                cycle_id, "score_titles", "(vidiq+heuristic)",
                json.dumps(summary), 0, datetime.utcnow().isoformat(),
            ),
        )
    log.info("scored %d titles across %d ideas — sources=%s",
             n_titles, n_ideas, sources_used)
    return summary
