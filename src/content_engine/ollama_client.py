"""Ollama harness with structured output, retry, and tier escalation.

Every agent call goes through `run_agent`. The harness:
  1. Renders a versioned prompt template.
  2. Calls the requested tier with JSON-mode (Ollama `format=schema`).
  3. Validates against the Pydantic schema.
  4. On parse failure or low confidence → escalates to the next tier.
  5. Logs every attempt to the agent_runs table for later eval.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import ModelTier, settings
from .db import get_conn, record_agent_run
from .schemas import AgentResult

log = logging.getLogger("engine.ollama")
T = TypeVar("T", bound=BaseModel)

_TIER_ORDER = ["fast", "mid", "heavy"]
_TIERS: dict[str, ModelTier] = {
    "fast": settings.fast,
    "mid": settings.mid,
    "heavy": settings.heavy,
    "polish": settings.polish,
    "embed": settings.embed,
}


# ─── Prompt registry ──────────────────────────────────────────────────────────
_PROMPT_CACHE: dict[str, str] = {}


def load_prompt(name: str) -> str:
    if name not in _PROMPT_CACHE:
        path = settings.prompts_dir / f"{name}.txt"
        if not path.exists():
            raise FileNotFoundError(f"Prompt '{name}' not found at {path}")
        _PROMPT_CACHE[name] = path.read_text()
    return _PROMPT_CACHE[name]


def render_prompt(name: str, **vars) -> str:
    """Render a prompt template with {{var}} substitution. Brand variables
    (brand_name, brand_short, brand_niche, audience_summary, divisive_topics)
    are auto-injected from settings so every template can reference them
    without each caller passing them explicitly."""
    tpl = load_prompt(name)
    # Auto-inject brand context unless caller overrode it.
    defaults = {
        "brand_name": settings.brand_name,
        "brand_short": settings.brand_short,
        "brand_niche": ", ".join(settings.brand_niche),
        "audience_summary_default": settings.audience_summary,
        "divisive_topics_block": "\n".join(
            f"  - {t.get('name', '')}: {t.get('note', '')}"
            for t in settings.divisive_topics
        ),
    }
    merged = {**defaults, **vars}
    out = tpl
    for k, v in merged.items():
        out = out.replace(f"{{{{{k}}}}}", str(v))
    return out


# ─── Low-level Ollama calls ───────────────────────────────────────────────────
class OllamaError(RuntimeError):
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    retry=retry_if_exception_type((httpx.HTTPError, OllamaError)),
    reraise=True,
)
def _ollama_chat(model: str, prompt: str, *, format_schema: dict | None = None,
                 system: str | None = None, options: dict | None = None) -> dict:
    payload: dict = {
        "model": model,
        "messages": (
            ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": prompt}]
        ),
        "stream": False,
        "options": options or {"temperature": 0.4},
    }
    if format_schema is not None:
        payload["format"] = format_schema
    with httpx.Client(timeout=900.0) as client:
        r = client.post(f"{settings.ollama_host}/api/chat", json=payload)
        if r.status_code != 200:
            raise OllamaError(f"Ollama HTTP {r.status_code}: {r.text[:300]}")
        return r.json()


def embed(text: str | list[str], model: str | None = None) -> list[list[float]]:
    """Returns list of vectors (one per input)."""
    model = model or settings.embed.ollama_tag
    inputs = [text] if isinstance(text, str) else text
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            f"{settings.ollama_host}/api/embed",
            json={"model": model, "input": inputs},
        )
        r.raise_for_status()
        return r.json()["embeddings"]


# ─── High-level: run an agent with schema + escalation ────────────────────────
def _hash_input(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def _next_tier(current: str) -> str | None:
    if current not in _TIER_ORDER:
        return None
    i = _TIER_ORDER.index(current)
    return _TIER_ORDER[i + 1] if i + 1 < len(_TIER_ORDER) else None


def run_agent(
    *,
    agent_name: str,
    prompt_name: str,
    schema: Type[T],
    prompt_vars: dict,
    starting_tier: str = "mid",
    system: str | None = None,
    cycle_id: str | None = None,
    confidence_field: str = "confidence",
) -> tuple[T | None, AgentResult]:
    """Run an agent. Auto-escalates on parse failure or low confidence.

    Returns (parsed_output_or_none, AgentResult).
    """
    rendered = render_prompt(prompt_name, **prompt_vars)
    input_hash = _hash_input(rendered)
    schema_json = schema.model_json_schema()

    tier_name = starting_tier
    last_error: str | None = None
    escalated_from: str | None = None

    while tier_name:
        tier = _TIERS[tier_name]
        t0 = time.monotonic()
        try:
            resp = _ollama_chat(
                tier.ollama_tag, rendered,
                format_schema=schema_json, system=system,
                # Explicitly bound the context so the model loads with a
                # right-sized KV cache instead of inheriting whatever default
                # Ollama (or another client) left set. Keep all tiers of the
                # same model on the same value so it loads once and is reused.
                options={"temperature": 0.4, "num_ctx": tier.max_ctx},
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            content = resp["message"]["content"]
            parsed = schema.model_validate_json(content)

            conf = getattr(parsed, confidence_field, None)
            with get_conn() as conn:
                record_agent_run(
                    conn, cycle_id=cycle_id, agent=agent_name, model_tier=tier_name,
                    input_hash=input_hash, output=parsed.model_dump(mode="json"),
                    confidence=conf, elapsed_ms=elapsed_ms,
                    escalated_from=escalated_from,
                )

            # Escalate if confidence is too low and we have a stronger tier.
            if (
                conf is not None
                and conf < settings.confidence_retry_below
                and _next_tier(tier_name) is not None
            ):
                log.info("agent=%s tier=%s confidence=%.2f → escalating",
                         agent_name, tier_name, conf)
                escalated_from = tier_name
                tier_name = _next_tier(tier_name)  # type: ignore[assignment]
                continue

            return parsed, AgentResult(
                agent=agent_name, model_tier=tier_name,
                output=parsed.model_dump(mode="json"),
                confidence=conf, elapsed_ms=elapsed_ms,
                raw_response=content,
            )
        except (ValidationError, json.JSONDecodeError, OllamaError) as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            last_error = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("agent=%s tier=%s failed: %s",
                        agent_name, tier_name, last_error)
            with get_conn() as conn:
                record_agent_run(
                    conn, cycle_id=cycle_id, agent=agent_name, model_tier=tier_name,
                    input_hash=input_hash, error=last_error, elapsed_ms=elapsed_ms,
                    escalated_from=escalated_from,
                )
            escalated_from = tier_name
            tier_name = _next_tier(tier_name)  # type: ignore[assignment]

    return None, AgentResult(
        agent=agent_name, model_tier=escalated_from or starting_tier,
        error=last_error or "All tiers exhausted", elapsed_ms=0,
    )


def list_local_models() -> list[str]:
    with httpx.Client(timeout=10) as client:
        r = client.get(f"{settings.ollama_host}/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]


# ─── Tier readiness ───────────────────────────────────────────────────────────
def _required_tiers() -> list[ModelTier]:
    """Distinct model tiers the pipeline routes to, de-duped by tag (polish is
    an alias of heavy), in a sensible display order."""
    seen: set[str] = set()
    out: list[ModelTier] = []
    for t in (settings.embed, settings.fast, settings.mid, settings.heavy, settings.polish):
        if t.ollama_tag not in seen:
            seen.add(t.ollama_tag)
            out.append(t)
    return out


def check_tiers() -> list[tuple[ModelTier, bool]]:
    """Return [(tier, is_present), ...] for every routed model. Raises
    OllamaError if Ollama itself is unreachable (a distinct, actionable
    failure from 'a model is missing')."""
    try:
        local = set(list_local_models())
    except (httpx.HTTPError, OSError) as e:
        raise OllamaError(
            f"could not reach Ollama at {settings.ollama_host}: {e}"
        ) from e
    out: list[tuple[ModelTier, bool]] = []
    for t in _required_tiers():
        present = any(t.ollama_tag in m or m.startswith(t.ollama_tag) for m in local)
        out.append((t, present))
    return out


def missing_tiers() -> list[ModelTier]:
    """Routed models not currently pulled in Ollama. Empty list = all good."""
    return [t for t, ok in check_tiers() if not ok]


def ensure_tiers_available() -> None:
    """Pre-flight gate. Raises OllamaError with copy-pasteable `ollama pull`
    lines if any routed model is missing. Cheap (one /api/tags call) — call it
    before a long pipeline run so a missing model fails in ~1s instead of
    surfacing mid-cluster as a slow watchdog kill."""
    missing = missing_tiers()
    if not missing:
        return
    tags = ", ".join(t.ollama_tag for t in missing)
    pulls = "\n".join(f"  ollama pull {t.ollama_tag}" for t in missing)
    raise OllamaError(
        f"{len(missing)} routed model(s) missing from Ollama: {tags}\n"
        f"Pull them, then re-run:\n{pulls}"
    )
