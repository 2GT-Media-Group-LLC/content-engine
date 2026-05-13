"""Voice guide extractor. Reads a corpus of script openings/closings (from
data/voice_corpus/scripts_signals.json) and produces style/voice_guide.md via a
single Qwen A3B call. Free generation (no JSON-mode); we save the markdown verbatim."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import settings
from ..ollama_client import _ollama_chat, render_prompt

log = logging.getLogger("engine.voice")


def _format_corpus(signals: dict) -> str:
    parts = []
    for title, sig in signals.items():
        parts.append(f"\n=== SCRIPT: {title} ({sig.get('total_chars', 0)} chars) ===")
        parts.append(f"\n[OPENING]\n{sig.get('opening', '').strip()}")
        if sig.get("second_segment"):
            parts.append(f"\n[CONTINUATION]\n{sig['second_segment'].strip()}")
        parts.append(f"\n[CLOSING]\n{sig.get('closing', '').strip()}")
    return "\n".join(parts)


def extract_voice_guide(
    corpus_path: Path | None = None,
    out_path: Path | None = None,
) -> Path:
    """Run a one-shot voice extraction. Writes style/voice_guide.md."""
    corpus_path = corpus_path or (settings.db_path.parent / "voice_corpus" / "scripts_signals.json")
    out_path = out_path or (settings.style_dir / "voice_guide.md")

    if not corpus_path.exists():
        raise FileNotFoundError(f"corpus not found at {corpus_path}")

    signals = json.loads(corpus_path.read_text())
    n = len(signals)
    log.info("extracting voice from %d scripts", n)

    prompt = render_prompt(
        "extract_voice",
        n_scripts=n,
        corpus=_format_corpus(signals),
    )
    # Free generation, low temperature for descriptive accuracy.
    resp = _ollama_chat(
        settings.heavy.ollama_tag, prompt,
        options={"temperature": 0.3, "top_p": 0.9, "num_ctx": 16384},
    )
    md = resp["message"]["content"].strip()
    # Strip code fences if model wrapped the markdown.
    if md.startswith("```"):
        import re as _re
        md = _re.sub(r"^```[a-z]*\n?|\n?```$", "", md, flags=_re.MULTILINE).strip()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    log.info("wrote voice guide: %s (%d chars)", out_path, len(md))
    return out_path
