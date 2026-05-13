"""Thin SQLite layer. Keep schema in this file — easy to grep, easy to evolve."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT,
    author TEXT,
    title TEXT,
    body TEXT,
    posted_at TEXT,
    metrics_json TEXT,
    extra_json TEXT,
    collected_at TEXT NOT NULL,
    UNIQUE(platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_platform_posted ON signals(platform, posted_at);

CREATE TABLE IF NOT EXISTS summaries (
    signal_id INTEGER PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
    one_line TEXT NOT NULL,
    key_points_json TEXT NOT NULL,
    topics_json TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    novelty REAL NOT NULL,
    confidence REAL NOT NULL,
    model_tier TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    label TEXT NOT NULL,
    signal_ids_json TEXT NOT NULL,
    dominant_topics_json TEXT NOT NULL,
    avg_sentiment REAL NOT NULL,
    heat_score REAL NOT NULL,
    representative_quote TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clusters_cycle ON clusters(cycle_id);

CREATE TABLE IF NOT EXISTS ideas (
    idea_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,        -- full IdeaCandidate JSON
    fate TEXT NOT NULL DEFAULT 'pending', -- pending | rejected | parked | produced
    fate_reason TEXT,
    fate_set_at TEXT,
    produced_video_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ideas_cycle ON ideas(cycle_id);
CREATE INDEX IF NOT EXISTS idx_ideas_fate ON ideas(fate);

CREATE TABLE IF NOT EXISTS outlines (
    idea_id TEXT PRIMARY KEY REFERENCES ideas(idea_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model_tier TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS video_performance (
    video_id TEXT PRIMARY KEY,
    idea_id TEXT,                       -- nullable: not every video came from an idea
    title TEXT,
    published_at TEXT,
    views INTEGER,
    avg_view_duration_sec REAL,
    ctr REAL,
    likes INTEGER,
    comments INTEGER,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY(idea_id) REFERENCES ideas(idea_id)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    agent TEXT NOT NULL,
    model_tier TEXT NOT NULL,
    input_hash TEXT,
    output_json TEXT,
    error TEXT,
    confidence REAL,
    elapsed_ms INTEGER NOT NULL,
    escalated_from TEXT,                -- prior tier if this was an escalation
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent, created_at);

CREATE TABLE IF NOT EXISTS cycles (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    notes TEXT
);
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or settings.db_path
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: Path | None = None) -> None:
    with _connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ─── Convenience writers ──────────────────────────────────────────────────────
def upsert_signal(conn: sqlite3.Connection, sig: dict) -> int:
    """Insert or update a raw signal. Returns its row id."""
    cur = conn.execute(
        """INSERT INTO signals
           (platform, external_id, url, author, title, body, posted_at,
            metrics_json, extra_json, collected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(platform, external_id) DO UPDATE SET
             metrics_json=excluded.metrics_json,
             collected_at=excluded.collected_at
           RETURNING id""",
        (
            sig["platform"], sig["external_id"], sig.get("url"), sig.get("author"),
            sig.get("title"), sig.get("body"),
            sig["posted_at"].isoformat() if sig.get("posted_at") else None,
            json.dumps(sig.get("metrics", {})),
            json.dumps(sig.get("extra", {})),
            sig.get("collected_at", datetime.utcnow()).isoformat(),
        ),
    )
    return cur.fetchone()[0]


def record_agent_run(conn: sqlite3.Connection, **kw) -> None:
    conn.execute(
        """INSERT INTO agent_runs
           (cycle_id, agent, model_tier, input_hash, output_json, error,
            confidence, elapsed_ms, escalated_from, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kw.get("cycle_id"), kw["agent"], kw["model_tier"],
            kw.get("input_hash"), json.dumps(kw.get("output")) if kw.get("output") else None,
            kw.get("error"), kw.get("confidence"), kw["elapsed_ms"],
            kw.get("escalated_from"), datetime.utcnow().isoformat(),
        ),
    )
