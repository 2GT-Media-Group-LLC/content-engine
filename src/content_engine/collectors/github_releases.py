"""GitHub releases collector. Tracks releases for the tools your audience runs.

Unauthenticated GitHub API allows 60 req/hr per IP, which is plenty at our
volume (one request per tracked repo per cycle). Reads data/github_repos.yaml."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import yaml

from ..config import settings
from ..schemas import RawSignal, SourcePlatform
from .base import ingest_signals

log = logging.getLogger("engine.gh")

_REGISTRY = settings.db_path.parent / "github_repos.yaml"
_API = "https://api.github.com"
_USER_AGENT = f"{settings.brand_short}-content-engine/0.1"


def _load_registry(path: Path | None = None) -> list[dict]:
    path = path or _REGISTRY
    if not path.exists():
        log.warning("no github registry at %s", path)
        return []
    return yaml.safe_load(path.read_text()) or []


def collect(per_repo_limit: int = 3,
            window_days: int = 30) -> dict:
    """Pull recent releases from each tracked repo."""
    registry = _load_registry()
    if not registry:
        return {"platform": "github", "repos": 0, "ingested": 0, "errors": []}

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    total = 0
    errors: list[str] = []

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=30.0, headers=headers) as client:
        for entry in registry:
            repo = entry.get("repo")
            tags = entry.get("tags", [])
            if not repo or "/" not in repo:
                continue
            try:
                log.info("fetching releases for %s", repo)
                r = client.get(
                    f"{_API}/repos/{repo}/releases",
                    params={"per_page": per_repo_limit},
                )
                if r.status_code == 404:
                    errors.append(f"{repo}: 404 (no releases or repo gone)")
                    continue
                r.raise_for_status()
                signals: list[RawSignal] = []
                for rel in r.json()[:per_repo_limit]:
                    if rel.get("draft") or rel.get("prerelease"):
                        continue
                    pub = rel.get("published_at")
                    posted_at = None
                    if pub:
                        try:
                            posted_at = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                            posted_at = posted_at.replace(tzinfo=None)
                            if posted_at < cutoff:
                                continue
                        except ValueError:
                            posted_at = None
                    tag = rel.get("tag_name") or rel.get("name") or "unknown"
                    title = f"{repo} {tag} — {rel.get('name','')}".strip(" —")
                    body = (rel.get("body") or "")[:5000]
                    signals.append(RawSignal(
                        platform=SourcePlatform.github,
                        external_id=f"{repo}@{tag}",
                        url=rel.get("html_url"),
                        author=repo,
                        title=title,
                        body=body,
                        posted_at=posted_at,
                        metrics={"reactions": (rel.get("reactions") or {}).get("total_count", 0)},
                        extra={"repo": repo, "tag": tag, "feed_tags": tags},
                    ))
                n = ingest_signals(signals)
                total += n
                log.info("  → %d release(s) from %s", n, repo)
                time.sleep(0.5)  # respect 60/hr unauth limit
            except Exception as e:
                errors.append(f"{repo}: {str(e)[:120]}")
                log.warning("repo %s failed: %s", repo, e)

    return {
        "platform": "github",
        "repos": len(registry),
        "ingested": total,
        "errors": errors,
        "ran_at": datetime.utcnow().isoformat(),
    }
