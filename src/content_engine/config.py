"""Engine configuration.

Two layers:
  1. Generic settings + model routing in this file.
  2. Channel-specific values (brand, audience, peer channels, subreddits, HN
     keywords) loaded from `channel.yaml` at project root.

`channel.yaml` is gitignored. The repo ships `channel.example.yaml` as a
template. If `channel.yaml` is missing, settings fall back to the example
file so the engine still runs (with generic defaults) for development."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# Load .env early so every module sees env vars (COMPOSIO_API_KEY, etc.).
# Use override=False so a real shell export wins over the file.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

log = logging.getLogger("engine.config")


# ─── Channel-specific config (loaded from channel.yaml) ──────────────────────
def _load_channel_yaml() -> dict:
    """Load channel.yaml from project root. Fall back to channel.example.yaml."""
    candidates = [ROOT / "channel.yaml", ROOT / "channel.example.yaml"]
    for p in candidates:
        if p.exists():
            try:
                import yaml
                data = yaml.safe_load(p.read_text()) or {}
                if p.name == "channel.example.yaml":
                    log.warning(
                        "Using channel.example.yaml (template). Copy it to "
                        "channel.yaml and customize for your channel."
                    )
                return data
            except Exception as e:
                log.error("failed to parse %s: %s", p, e)
    log.warning("No channel.yaml found. Using minimal defaults.")
    return {}


_CHANNEL: dict[str, Any] = _load_channel_yaml()


def _normalize_ollama_host() -> str:
    """Read OLLAMA_HOST and ensure it has a scheme. Ollama itself often sets
    this env var to bare 'host:port' (e.g. '0.0.0.0:11434' for LAN bind),
    which httpx rejects with UnsupportedProtocol."""
    raw = (os.getenv("OLLAMA_HOST") or "http://localhost:11434").strip()
    if not raw:
        raw = "http://localhost:11434"
    if not raw.startswith(("http://", "https://")):
        if raw.startswith(("0.0.0.0", "::", "[::]")):
            host_port = raw.split("/", 1)[0]
            port = host_port.split(":")[-1] if ":" in host_port else "11434"
            raw = f"http://localhost:{port}"
        else:
            raw = "http://" + raw
    return raw


@dataclass(frozen=True)
class ModelTier:
    name: str
    ollama_tag: str
    purpose: str
    max_ctx: int = 8192


@dataclass(frozen=True)
class Settings:
    ollama_host: str = field(default_factory=_normalize_ollama_host)

    db_path: Path = ROOT / "data" / "engine.db"
    vectors_path: Path = ROOT / "data" / "vectors"
    reports_dir: Path = ROOT / "reports"
    style_dir: Path = ROOT / "style"
    prompts_dir: Path = Path(__file__).parent / "prompts"
    templates_dir: Path = Path(__file__).parent / "reports" / "templates"

    # Routing — bias to smaller models, escalate on low confidence.
    embed: ModelTier = field(default_factory=lambda: ModelTier(
        name="embed", ollama_tag="nomic-embed-text", purpose="embeddings"))
    fast: ModelTier = field(default_factory=lambda: ModelTier(
        name="fast", ollama_tag="gemma4:e2b", purpose="triage / classify / sentiment"))
    mid: ModelTier = field(default_factory=lambda: ModelTier(
        name="mid", ollama_tag="gemma4:e4b", purpose="summarize / structured extraction"))
    heavy: ModelTier = field(default_factory=lambda: ModelTier(
        name="heavy", ollama_tag="qwen3:30b-a3b-instruct-2507-q8_0",
        purpose="idea + outline synthesis + polish (Qwen 3 A3B MoE)",
        max_ctx=16384))
    polish: ModelTier = field(default_factory=lambda: ModelTier(
        name="polish", ollama_tag="qwen3:30b-a3b-instruct-2507-q8_0",
        purpose="(alias of heavy) — kept for tier-name compatibility",
        max_ctx=16384))

    # Confidence thresholds for escalation.
    confidence_retry_below: float = 0.55
    confidence_escalate_below: float = 0.35

    # ─── YouTube data provider selection ─────────────────────────────────────
    # Which backend serves YouTube data (peer/own channel uploads, title
    # scoring, performance analytics). Values: "vidiq" (default), "composio",
    # or "noop". Per-provider auth is read from env:
    #   vidiq:    VIDIQ_API_KEY     (single bearer token to mcp.vidiq.com/mcp)
    #   composio: COMPOSIO_API_KEY  (legacy path; kept as fallback only)
    youtube_provider: str = field(
        default_factory=lambda: (os.getenv("YOUTUBE_PROVIDER") or "vidiq").strip().lower()
    )
    vidiq_mcp_endpoint: str = field(
        default_factory=lambda: (os.getenv("VIDIQ_MCP_ENDPOINT")
                                  or "https://mcp.vidiq.com/mcp").strip()
    )

    # ─── Channel identity (loaded from channel.yaml) ─────────────────────────
    brand_name: str = field(
        default_factory=lambda: _CHANNEL.get("brand", {}).get("name", "Your Channel")
    )
    brand_short: str = field(
        default_factory=lambda: _CHANNEL.get("brand", {}).get("short", "CH")
    )
    brand_niche: tuple[str, ...] = field(
        default_factory=lambda: tuple(_CHANNEL.get("brand", {}).get(
            "niche", ["homelab", "self-hosting", "AI", "virtualization"]
        ))
    )
    audience_summary: str = field(
        default_factory=lambda: (_CHANNEL.get("audience_summary") or
                                  "Mid-to-high technical viewers.").strip()
    )
    divisive_topics: tuple[dict, ...] = field(
        default_factory=lambda: tuple(_CHANNEL.get("divisive_topics", []))
    )

    @property
    def peer_channels(self) -> tuple[str, ...]:
        """Peer YouTube channel handles for the synthesizer prompt."""
        peers = (_CHANNEL.get("youtube") or {}).get("peer_channels", [])
        return tuple(p.get("handle", "") for p in peers if p.get("handle"))

    @property
    def peer_channels_full(self) -> list[dict]:
        """Full peer channel records {handle, name} for collectors."""
        return list((_CHANNEL.get("youtube") or {}).get("peer_channels", []))

    @property
    def own_channel_id(self) -> str:
        return (_CHANNEL.get("youtube") or {}).get("own_channel_id", "")

    @property
    def own_channel_title(self) -> str:
        return (_CHANNEL.get("youtube") or {}).get("own_channel_title", self.brand_name)

    @property
    def reddit_subreddits(self) -> list[tuple[str, int]]:
        rows = (_CHANNEL.get("reddit") or {}).get("subreddits", [])
        return [(r["name"], int(r.get("limit", 12))) for r in rows if r.get("name")]

    @property
    def hn_queries(self) -> list[str]:
        return list((_CHANNEL.get("hackernews") or {}).get("queries", []))

    @property
    def hn_min_points(self) -> int:
        return int((_CHANNEL.get("hackernews") or {}).get("min_points", 20))

    @property
    def hn_window_days(self) -> int:
        return int((_CHANNEL.get("hackernews") or {}).get("window_days", 14))

    # ─── Comment mining (vidiq-only; channel-mode = 1 credit-call per channel) ─
    @property
    def comments_enabled(self) -> bool:
        return bool((_CHANNEL.get("comments") or {}).get("enabled", True))

    @property
    def comments_per_channel(self) -> int:
        """Comment threads to pull per channel per cycle."""
        return int((_CHANNEL.get("comments") or {}).get("per_channel", 30))

    # ─── Outlier scan (niche discovery beyond the fixed peer list) ────────────
    @property
    def outliers_enabled(self) -> bool:
        return bool((_CHANNEL.get("outliers") or {}).get("enabled", True))

    @property
    def outlier_keywords(self) -> list[str]:
        """Keywords to scan for breakout videos. Defaults to first 3 niche
        topics, cleaned for search (parentheticals stripped, lowercased) —
        niche labels are prose ('AI (local + cloud)'), search queries aren't."""
        kws = (_CHANNEL.get("outliers") or {}).get("keywords")
        if kws:
            return list(kws)
        import re as _re
        cleaned = []
        for t in self.brand_niche[:3]:
            kw = _re.sub(r"\([^)]*\)", "", str(t)).strip().lower()
            if kw:
                cleaned.append(kw)
        return cleaned

    @property
    def outliers_per_keyword(self) -> int:
        return int((_CHANNEL.get("outliers") or {}).get("per_keyword", 8))

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vectors_path.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.style_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
