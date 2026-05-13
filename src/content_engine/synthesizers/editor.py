"""Prose polish pass. Takes structured agent output, rewrites the free-text fields
in unconstrained mode (no JSON-mode straitjacket), then merges back. Catches the
artifacts that JSON-constrained decoding bakes into Q4-quant model output.

Pure local. No Anthropic spend.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import settings
from ..db import get_conn, record_agent_run
from ..ollama_client import _ollama_chat, render_prompt
from ..schemas import AgentResult

log = logging.getLogger("engine.editor")
T = TypeVar("T", bound=BaseModel)

# Heuristic artifact patterns. Items present here = certain low quality.
# Backreferences don't compose cleanly in alternation, so we run patterns separately.
_ARTIFACT_PATTERNS = [
    re.compile(r"\b(\w+) \1\b", re.IGNORECASE),                # exact doubled word
    re.compile(r"\ba-[a-z]+\b", re.IGNORECASE),                # weird hyphenation
    re.compile(r"\bto-be-[a-z]+\b", re.IGNORECASE),
    re.compile(r"\b[a-z]+-[a-z]+ own\b", re.IGNORECASE),       # truncated mid-word
    re.compile(r"\{\s*\"//\""),                                # leaked JSON comment
    re.compile(r"<\|?[a-z_]+\|?>", re.IGNORECASE),             # control tokens
    re.compile(r"\b\w+\.{3,}\s*$"),                            # trailing ellipsis
    re.compile(r"\b[a-z]+_[a-z]+_[a-z]+_[a-z]+\b"),            # snake_case in prose
    re.compile(r"\b(theve|thier|teh|tothe|ofthe|inthe)\b", re.IGNORECASE),
]


def _near_doubled(text: str) -> list[str]:
    """Catch near-doubled adjacent words like 'salvaged salvage' (shared 5+ char stem)."""
    out: list[str] = []
    words = re.findall(r"\w+", text.lower())
    for i in range(len(words) - 1):
        a, b = words[i], words[i + 1]
        if a == b or len(a) < 5 or len(b) < 5:
            continue
        # shared prefix of 5+ chars
        common = 0
        for ca, cb in zip(a, b):
            if ca == cb:
                common += 1
            else:
                break
        if common >= 5:
            out.append(f"{a} {b}")
    return out


def detect_artifacts(text: str) -> list[str]:
    """Return list of artifact snippets found in text. Empty list = clean."""
    if not text:
        return []
    found: list[str] = []
    for pat in _ARTIFACT_PATTERNS:
        found.extend(m.group(0) for m in pat.finditer(text))
    found.extend(_near_doubled(text))
    return found[:10]


def _clean_truncated(text: str) -> str:
    """If text ends mid-sentence, snip back to last sentence boundary."""
    if not text:
        return text
    # If last char is alphanumeric and no terminal punctuation in last 30 chars, truncate.
    if text[-1].isalnum() and not re.search(r"[.!?][\"')\]]?\s*$", text):
        # Try to find last complete sentence.
        m = re.search(r"^.*[.!?][\"')\]]?(?=\s)", text, flags=re.DOTALL)
        if m and len(m.group(0)) > len(text) // 2:
            return m.group(0).strip()
    return text


def polish_fields(
    *, agent_name: str, draft: dict, fields_to_rewrite: list[str],
    cycle_id: str | None = None, tier: str = "polish",
) -> tuple[dict, AgentResult]:
    """Rewrite the given prose fields of `draft` using the heavy local model in
    unconstrained mode. Returns (polished_dict, AgentResult).

    Strategy:
      1. Compute a "draft_json" containing only the fields we want rewritten.
      2. Call model in non-JSON mode with a focused editor prompt.
      3. Parse the model's reply (it should still be JSON because the prompt asks).
      4. On parse failure, fall back to the original (don't break the pipeline).
      5. Run heuristic artifact scrub on whatever we end up with.
    """
    fields = [f for f in fields_to_rewrite if isinstance(draft.get(f), str) and draft[f].strip()]
    if not fields:
        return draft, AgentResult(agent=agent_name, model_tier=tier, output=draft, elapsed_ms=0)

    sub = {f: draft[f] for f in fields}
    prompt = render_prompt(
        "polish_prose",
        draft_json=json.dumps(sub, ensure_ascii=False, indent=2),
        fields_to_rewrite=", ".join(fields),
    )
    model = {
        "polish": settings.polish.ollama_tag,
        "heavy": settings.heavy.ollama_tag,
        "mid": settings.mid.ollama_tag,
    }.get(tier, settings.polish.ollama_tag)

    t0 = time.monotonic()
    try:
        # NOTE: no format_schema — we want unconstrained generation. Lower temp for editing.
        resp = _ollama_chat(model, prompt, options={"temperature": 0.2, "top_p": 0.9})
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        raw = resp["message"]["content"].strip()

        # Strip code fences if model added them.
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw, flags=re.MULTILINE).strip()

        polished_sub = json.loads(raw)
        if not isinstance(polished_sub, dict):
            raise ValueError(f"polish output not a dict: {type(polished_sub)}")

        polished = dict(draft)
        for f in fields:
            new_val = polished_sub.get(f)
            if isinstance(new_val, str) and new_val.strip():
                polished[f] = _clean_truncated(new_val.strip())

        with get_conn() as conn:
            record_agent_run(
                conn, cycle_id=cycle_id, agent=f"{agent_name}.polish",
                model_tier=tier, output=polished, elapsed_ms=elapsed_ms,
            )
        return polished, AgentResult(
            agent=f"{agent_name}.polish", model_tier=tier,
            output=polished, elapsed_ms=elapsed_ms,
        )
    except (json.JSONDecodeError, ValueError) as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.warning("polish failed for %s, keeping draft: %s", agent_name, str(e)[:200])
        with get_conn() as conn:
            record_agent_run(
                conn, cycle_id=cycle_id, agent=f"{agent_name}.polish",
                model_tier=tier, error=str(e)[:300], elapsed_ms=elapsed_ms,
            )
        # Apply at least the truncation cleanup to the original.
        cleaned = dict(draft)
        for f in fields:
            cleaned[f] = _clean_truncated(draft[f])
        return cleaned, AgentResult(
            agent=f"{agent_name}.polish", model_tier=tier, output=cleaned,
            error=str(e)[:300], elapsed_ms=elapsed_ms,
        )
