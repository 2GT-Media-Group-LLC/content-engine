"""Citation verifier post-pass for outlines.

LLMs hallucinate URLs. This pass:
  1. Pulls the actual source signal URLs for each outline (via its idea's source_signal_ids).
  2. Adds a small whitelist of "always trustworthy" domains (the user's own channel,
     well-known docs sites we explicitly trust to exist).
  3. Marks each outline source as verified=True or verified=False.
  4. Logs a summary so we can track hallucination rate over time.

Pure local. Zero LLM calls.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from urllib.parse import urlparse

from ..db import get_conn

log = logging.getLogger("engine.cite_verify")

# Domains we trust to exist without checking each path. Be conservative —
# better to mark something unverified than to vouch for a hallucinated path.
ALWAYS_OK_DOMAINS = {
    "github.com",         # Real org/repos exist; specific paths still need a check.
    "docs.docker.com",
    "kubernetes.io",
    "wiki.archlinux.org",
    "ubuntu.com",
    "proxmox.com",
    "www.proxmox.com",
    "pve.proxmox.com",
    "forum.proxmox.com",
    "huggingface.co",
    "ollama.com",
    "ollama.ai",
    "openzfs.org",
    "openwrt.org",
    "tailscale.com",
    "wireguard.com",
}


def _normalize(url: str) -> str:
    """Coarse URL normalization: lowercase scheme/host, strip trailing slash."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower().lstrip("www.")
        path = p.path.rstrip("/") or "/"
        return f"{host}{path}"
    except Exception:
        return url.strip().lower()


def _signal_urls_for_idea(conn, idea_payload: dict) -> set[str]:
    """Collect all URLs from the source signals an idea was built from."""
    sig_ids = idea_payload.get("source_signal_ids", [])
    if not sig_ids:
        return set()
    placeholders = ",".join("?" * len(sig_ids))
    rows = conn.execute(
        f"SELECT url FROM signals WHERE id IN ({placeholders})", sig_ids
    ).fetchall()
    out = set()
    for r in rows:
        if r["url"]:
            out.add(_normalize(r["url"]))
    return out


def verify_cycle(cycle_id: str) -> dict:
    """Mark every outline source in this cycle as verified or speculative.
    Returns counts. Updates outlines table in place."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT o.idea_id, o.payload_json AS opl, i.payload_json AS ipl
               FROM outlines o JOIN ideas i ON i.idea_id = o.idea_id
               WHERE i.cycle_id = ?""",
            (cycle_id,),
        ).fetchall()

    if not rows:
        log.info("no outlines to verify for cycle %s", cycle_id)
        return {"outlines": 0, "sources_total": 0, "verified": 0, "speculative": 0}

    total_sources = 0
    total_verified = 0
    total_specul = 0

    for row in rows:
        outline = json.loads(row["opl"])
        idea = json.loads(row["ipl"])
        with get_conn() as conn:
            allow_signal_urls = _signal_urls_for_idea(conn, idea)

        for src in outline.get("sources", []):
            url = src.get("url", "")
            host = (urlparse(url).hostname or "").lower().lstrip("www.")
            norm = _normalize(url)
            verified = (
                norm in allow_signal_urls
                or any(norm.startswith(d + "/") or norm == d for d in ALWAYS_OK_DOMAINS)
                or host in ALWAYS_OK_DOMAINS
            )
            src["verified"] = bool(verified)
            total_sources += 1
            if verified:
                total_verified += 1
            else:
                total_specul += 1

        # Persist the updated outline back.
        with get_conn() as conn:
            conn.execute(
                "UPDATE outlines SET payload_json=? WHERE idea_id=?",
                (json.dumps(outline), row["idea_id"]),
            )

    summary = {
        "outlines": len(rows),
        "sources_total": total_sources,
        "verified": total_verified,
        "speculative": total_specul,
        "hallucination_rate": (
            round(total_specul / total_sources, 3) if total_sources else 0.0
        ),
    }
    log.info(
        "verified %d outlines, %d sources: %d verified / %d speculative (rate %.1f%%)",
        len(rows), total_sources, total_verified, total_specul,
        100 * summary["hallucination_rate"],
    )

    # Stash the summary into agent_runs telemetry.
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agent_runs
               (cycle_id, agent, model_tier, output_json, elapsed_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                cycle_id, "verify_citations", "(deterministic)",
                json.dumps(summary), 0, datetime.utcnow().isoformat(),
            ),
        )
    return summary
