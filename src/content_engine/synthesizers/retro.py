"""Weekly retro agent: distills the owner's triage history (fates + reasons)
into a living editorial guide at style/editorial_guide.md.

Why distill instead of stuffing raw history into the idea prompt: the raw
fate-bucketed lists grow linearly forever and the local models pattern-match
the literal angles rather than internalizing the taste behind them. A 450-word
distilled guide generalizes ("never pitch X-shaped theses") and stays constant
size as history grows.

Regenerates only when triage state has changed since the last write — the
guide is a pure function of fates, so unchanged fates = unchanged guide."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime

from ..config import settings
from ..db import get_conn, record_agent_run
from ..ollama_client import _ollama_chat, render_prompt

log = logging.getLogger("engine.retro")

GUIDE_PATH = settings.style_dir / "editorial_guide.md"
_STATE_MARKER = "<!-- triage-state: "


def _triage_state_hash() -> str:
    """Hash of all decided fates — changes iff the owner triaged something."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT idea_id, fate, COALESCE(fate_reason,'') FROM ideas
               WHERE fate != 'pending' ORDER BY idea_id"""
        ).fetchall()
    blob = "|".join(f"{r[0]}:{r[1]}:{r[2]}" for r in rows)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _load_history() -> tuple[str, int]:
    """Render triage history for the prompt. Returns (text, n_decided)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT fate, fate_reason, payload_json FROM ideas
               WHERE fate != 'pending'
               ORDER BY fate, fate_set_at DESC"""
        ).fetchall()
    if not rows:
        return "", 0
    lines: list[str] = []
    labels = {"rejected": "REJECTED", "parked": "PARKED", "produced": "PRODUCED"}
    current = None
    for r in rows:
        if r["fate"] != current:
            current = r["fate"]
            lines.append(f"\n=== {labels.get(current, current.upper())} ===")
        p = json.loads(r["payload_json"])
        reason = f"  ← owner: \"{r['fate_reason']}\"" if r["fate_reason"] else ""
        lines.append(f"- {p.get('angle', '')[:200]}{reason}")
    return "\n".join(lines), len(rows)


def _load_calibration_notes() -> str:
    """Pull the postmortem calibration summary if it exists (see postmortem.py)."""
    p = settings.db_path.parent / "calibration.md"
    if p.exists():
        return p.read_text()[:2000]
    return "(no produced-video calibration data yet)"


def load_guide() -> str:
    """Read the current guide for prompt injection. Empty-state text if absent."""
    if GUIDE_PATH.exists():
        # Strip the state marker comment line.
        lines = [ln for ln in GUIDE_PATH.read_text().splitlines()
                 if not ln.startswith(_STATE_MARKER)]
        return "\n".join(lines).strip()
    return ("(no editorial guide yet — triage some ideas and run "
            "`engine retro` to generate one)")


def maybe_refresh(cycle_id: str | None = None, *, force: bool = False) -> dict:
    """Regenerate the guide iff triage state changed since the last write."""
    state = _triage_state_hash()
    if not force and GUIDE_PATH.exists():
        head = GUIDE_PATH.read_text()[:200]
        if f"{_STATE_MARKER}{state} -->" in head:
            log.info("editorial guide is current (state %s) — skipping retro", state)
            return {"refreshed": False, "state": state}

    history, n = _load_history()
    if n < 5:
        log.info("only %d decided ideas — not enough triage history for a "
                 "meaningful retro (need 5+)", n)
        return {"refreshed": False, "reason": f"only {n} decided ideas"}

    prompt = render_prompt(
        "editorial_retro",
        triage_history=history,
        calibration_notes=_load_calibration_notes(),
    )
    t0 = time.monotonic()
    try:
        resp = _ollama_chat(settings.heavy.ollama_tag, prompt,
                             options={"temperature": 0.3, "top_p": 0.9})
        guide = resp["message"]["content"].strip()
    except Exception as e:
        log.error("retro generation failed: %s", e)
        return {"refreshed": False, "error": str(e)[:200]}
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if not guide.startswith("##"):
        # Model added preamble despite instructions — trim to first heading.
        idx = guide.find("##")
        if idx > 0:
            guide = guide[idx:]

    header = (f"{_STATE_MARKER}{state} -->\n"
              f"<!-- generated {datetime.utcnow().isoformat()} from {n} decided ideas -->\n\n")
    GUIDE_PATH.write_text(header + guide + "\n")
    with get_conn() as conn:
        record_agent_run(conn, cycle_id=cycle_id, agent="editorial_retro",
                          model_tier="heavy",
                          output={"n_decided": n, "chars": len(guide)},
                          elapsed_ms=elapsed_ms)
    log.info("editorial guide refreshed from %d decided ideas (%d chars)",
             n, len(guide))
    return {"refreshed": True, "n_decided": n, "chars": len(guide),
            "path": str(GUIDE_PATH)}
