"""Minimal eval harness. Runs golden test cases against each agent and reports
per-case pass/fail. Goal is regression detection on model swaps and prompt edits.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .ollama_client import run_agent
from .schemas import IdeaCandidate, SignalSummary

log = logging.getLogger("engine.eval")

GOLDEN_ROOT = settings.db_path.parent.parent / "tests" / "golden"

# Map agent name -> (prompt_name, schema_class, default_starting_tier)
AGENT_REGISTRY: dict[str, tuple[str, type, str]] = {
    "summarize_signal": ("summarize_signal", SignalSummary, "mid"),
    "generate_idea": ("generate_idea", IdeaCandidate, "heavy"),
    # cluster_label uses a private schema in cluster.py; skip for v1.
}


@dataclass
class CaseResult:
    case_id: str
    agent: str
    passed: bool
    failures: list[str]
    output: dict | None
    confidence: float | None
    elapsed_ms: int


def _check_summary(out: SignalSummary, expect: dict) -> list[str]:
    fails = []
    if (mc := expect.get("min_confidence")) is not None and out.confidence < mc:
        fails.append(f"confidence {out.confidence:.2f} < {mc}")
    if (mn := expect.get("min_novelty")) is not None and out.novelty < mn:
        fails.append(f"novelty {out.novelty:.2f} < {mn}")
    if (xn := expect.get("max_novelty")) is not None and out.novelty > xn:
        fails.append(f"novelty {out.novelty:.2f} > max {xn}")
    if (tops := expect.get("must_include_topics_any_of")):
        topic_strs = [t.value for t in out.topics]
        if not any(t in topic_strs for t in tops):
            fails.append(f"topics {topic_strs} miss any of {tops}")
    if (sents := expect.get("sentiment_in")):
        if out.sentiment.value not in sents:
            fails.append(f"sentiment {out.sentiment.value} not in {sents}")
    if (kws := expect.get("one_line_keywords_any")):
        ol = (out.one_line or "").lower()
        if not any(k.lower() in ol for k in kws):
            fails.append(f"one_line missing any of {kws}: {out.one_line!r}")
    return fails


def _check_idea(out: IdeaCandidate, expect: dict) -> list[str]:
    fails = []
    if (mc := expect.get("min_confidence")) is not None and out.confidence < mc:
        fails.append(f"confidence {out.confidence:.2f} < {mc}")
    if (xf := expect.get("max_fatigue")) is not None and out.fatigue_score > xf:
        fails.append(f"fatigue {out.fatigue_score:.2f} > max {xf}")
    if (ntit := expect.get("min_titles")) is not None and len(out.suggested_titles) < ntit:
        fails.append(f"titles {len(out.suggested_titles)} < {ntit}")
    return fails


CHECK_FNS = {
    "summarize_signal": _check_summary,
    "generate_idea": _check_idea,
}


def run_eval(agents: list[str] | None = None) -> dict:
    """Run all golden cases for the given agents (or all if None). Returns summary."""
    agents = agents or list(AGENT_REGISTRY.keys())
    all_results: list[CaseResult] = []

    for agent_name in agents:
        if agent_name not in AGENT_REGISTRY:
            log.warning("unknown agent %s, skipping", agent_name)
            continue
        prompt_name, schema_cls, tier = AGENT_REGISTRY[agent_name]
        case_dir = GOLDEN_ROOT / agent_name
        if not case_dir.exists():
            log.warning("no golden cases for %s at %s", agent_name, case_dir)
            continue

        for case_path in sorted(case_dir.glob("*.json")):
            spec = json.loads(case_path.read_text())
            log.info("eval %s/%s", agent_name, spec.get("id", case_path.stem))
            parsed, agent_result = run_agent(
                agent_name=agent_name,
                prompt_name=prompt_name,
                schema=schema_cls,
                prompt_vars=spec["prompt_vars"],
                starting_tier=tier,
                cycle_id=f"eval_{agent_name}",
            )
            failures: list[str] = []
            if spec.get("expect", {}).get("must_validate_schema") and parsed is None:
                failures.append("schema validation failed (all tiers)")
            elif parsed is not None:
                check = CHECK_FNS.get(agent_name)
                if check:
                    failures.extend(check(parsed, spec.get("expect", {})))

            all_results.append(CaseResult(
                case_id=spec.get("id", case_path.stem),
                agent=agent_name,
                passed=len(failures) == 0,
                failures=failures,
                output=parsed.model_dump(mode="json") if parsed else None,
                confidence=getattr(parsed, "confidence", None) if parsed else None,
                elapsed_ms=agent_result.elapsed_ms,
            ))

    summary = {
        "total": len(all_results),
        "passed": sum(1 for r in all_results if r.passed),
        "failed": sum(1 for r in all_results if not r.passed),
        "results": [r.__dict__ for r in all_results],
    }
    return summary
