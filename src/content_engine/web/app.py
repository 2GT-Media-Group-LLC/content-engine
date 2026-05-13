"""FastAPI app for the content engine feedback loop.

Routes:
  GET  /              dashboard — latest cycle + history + Run now button
  GET  /cycle/{id}    full cycle view (mirrors weekly_report.html.j2)
  GET  /swipe/{id}    one-card-at-a-time idea triage with fate buttons
  POST /idea/{id}/fate     mark fate (rejected|parked|produced)
  POST /idea/{id}/video    attach a produced video_id
  POST /run                spawn `engine collect && engine run` detached
  GET  /run/status         JSON: running state, current cycle, log tail
  GET  /run/status/fragment HTMX fragment for live polling banner
  GET  /performance   2GT video performance + idea→video traceback
  GET  /health        container health probe

Reads the same SQLite DB the engine writes to. Triggered runs fork a
detached subprocess so the GUI process is free to keep serving."""
from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import settings
from ..db import get_conn, init_db

log = logging.getLogger("engine.web")

app = FastAPI(title="2GT Content Engine", docs_url=None, redoc_url=None)

_HERE = Path(__file__).parent
# Build Jinja env explicitly with cache_size=0 — works around a Jinja2 3.1.6 +
# Python 3.14 cache key incompatibility (unhashable dict in cache key).
_env = Environment(
    loader=FileSystemLoader(str(_HERE / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    cache_size=0,
)
templates = Jinja2Templates(env=_env)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    log.info("GUI ready — DB at %s", settings.db_path)


# ─── data accessors ──────────────────────────────────────────────────────────
def _cycles(limit: int = 25) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.*,
                      (SELECT COUNT(*) FROM ideas i WHERE i.cycle_id=c.id) AS n_ideas,
                      (SELECT COUNT(*) FROM ideas i WHERE i.cycle_id=c.id AND i.fate='produced') AS n_produced,
                      (SELECT COUNT(*) FROM ideas i WHERE i.cycle_id=c.id AND i.fate='parked') AS n_parked,
                      (SELECT COUNT(*) FROM ideas i WHERE i.cycle_id=c.id AND i.fate='rejected') AS n_rejected,
                      (SELECT COUNT(*) FROM ideas i WHERE i.cycle_id=c.id AND i.fate='pending') AS n_pending
               FROM cycles c ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _idea(idea_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["payload"] = json.loads(row["payload_json"])
        outline = conn.execute(
            "SELECT payload_json FROM outlines WHERE idea_id=?", (idea_id,)
        ).fetchone()
        out["outline"] = json.loads(outline["payload_json"]) if outline else None
        # Source signal previews for the swipe view.
        signal_ids = out["payload"].get("source_signal_ids", []) or []
        if signal_ids:
            placeholders = ",".join("?" * len(signal_ids))
            sigs = conn.execute(
                f"""SELECT s.platform, s.title, s.url, sm.one_line
                    FROM signals s LEFT JOIN summaries sm ON sm.signal_id=s.id
                    WHERE s.id IN ({placeholders}) LIMIT 12""",
                signal_ids,
            ).fetchall()
            out["source_signals"] = [dict(s) for s in sigs]
        else:
            out["source_signals"] = []
    return out


def _ideas_in_cycle(cycle_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM ideas WHERE cycle_id=?
               ORDER BY json_extract(payload_json, '$.confidence') DESC""",
            (cycle_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["payload"] = json.loads(r["payload_json"])
        out.append(d)
    return out


# ─── routes ──────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "db": str(settings.db_path)})


# ─── run orchestration ──────────────────────────────────────────────────────
# We keep the project root + venv python so the subprocess is invariant of
# whoever started uvicorn. RUNS_DIR holds per-trigger log files.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_BIN = _PROJECT_ROOT / ".venv" / "bin" / "engine"
_RUNS_DIR = _PROJECT_ROOT / "data" / "runs"
_RUNS_DIR.mkdir(parents=True, exist_ok=True)
_LOCK_FILE = _RUNS_DIR / ".manual_run.lock"

# A cycle row older than this with no finished_at is assumed dead — used to
# decide whether the "Run now" button should re-enable after a crash.
_STALE_AFTER = timedelta(minutes=45)


def _running_cycle() -> dict | None:
    """Return the most recent cycle still marked 'running' (not aged out)."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM cycles
               WHERE status='running' AND finished_at IS NULL
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return None
    try:
        started = datetime.fromisoformat(row["started_at"])
    except (TypeError, ValueError):
        return None
    if datetime.utcnow() - started > _STALE_AFTER:
        return None
    return dict(row)


def _read_lock() -> dict | None:
    if not _LOCK_FILE.exists():
        return None
    try:
        return json.loads(_LOCK_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_lock(payload: dict) -> None:
    _LOCK_FILE.write_text(json.dumps(payload))


def _clear_lock() -> None:
    _LOCK_FILE.unlink(missing_ok=True)


def _process_alive(pid: int | None) -> bool:
    """True iff PID exists AND is not a zombie. Critical because os.kill(pid, 0)
    returns success for zombies (process table entry still exists). For our
    purposes a finished-but-unreaped child is dead — the run is done."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        r = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "state="],
            capture_output=True, text=True, timeout=2,
        )
        state = (r.stdout or "").strip()
        if state.startswith("Z"):
            # Attempt to reap; we may not be the parent (start_new_session
            # detaches the pgrp but not the parent pointer for waitpid).
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            return False
    except (subprocess.SubprocessError, OSError):
        pass
    return True


def _engine_subprocess_alive() -> bool:
    """Scan ps for any `engine collect` or `engine run` process. Catches the
    weekly launchd cron (which doesn't write our lock file) and any rogue
    runs started from the CLI."""
    try:
        r = subprocess.run(
            ["/bin/ps", "-ax", "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            if (".venv/bin/engine collect" in line
                    or ".venv/bin/engine run" in line
                    or "content_engine.cli collect" in line
                    or "content_engine.cli run" in line):
                # Exclude the ps grep itself and bash wrappers that aren't
                # actually executing engine yet.
                if " grep " in line:
                    continue
                return True
    except (subprocess.SubprocessError, OSError):
        pass
    return False


def _reap_orphans(reason: str = "no live subprocess") -> int:
    """Mark every cycle still 'running' (with no live subprocess) as failed.
    Returns count reaped. Called from _is_running when we can prove nothing
    is actually executing."""
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE cycles SET status='failed', finished_at=?,
                                  notes = COALESCE(notes, '') || ' [reaped: ' || ? || ']'
               WHERE status='running' AND finished_at IS NULL""",
            (datetime.utcnow().isoformat(), reason),
        )
        n = cur.rowcount
    if n:
        log.info("reaped %d stale running cycle(s) — reason: %s", n, reason)
    return n


def _is_running() -> tuple[bool, dict | None, dict | None]:
    """Source-of-truth for the UI 'running' state.

    Authoritative ordering:
      1. Lock with alive PID → manual run is going.
      2. No lock, but an engine subprocess is alive in ps → likely the cron.
      3. Otherwise → reap any stale DB rows and report idle.
    """
    lock = _read_lock()

    # 1. Manual run path.
    if lock and _process_alive(lock.get("pid")):
        return True, _running_cycle(), lock
    if lock:
        _clear_lock()
        log.info("manual run lock had dead PID — cleared")

    # 2. Cron run path (or CLI-triggered run on the host).
    if _engine_subprocess_alive():
        return True, _running_cycle(), None

    # 3. Idle. Reap any DB rows that escaped finish_cycle().
    _reap_orphans()
    return False, None, None


@app.post("/run")
def trigger_run():
    """Fork a detached `engine collect && engine run` and return to dashboard."""
    running, cycle, lock = _is_running()
    if running:
        log.info("run already in progress (cycle=%s lock=%s) — ignoring",
                 cycle["id"] if cycle else None, lock.get("pid") if lock else None)
        return RedirectResponse("/?status=already_running", status_code=303)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    log_path = _RUNS_DIR / f"manual_{ts}.log"
    # bash -lc gives us shell semantics for "&&", plus loads any user profile
    # bits (PATH for ollama, etc.) the same way the launchd weekly cron does.
    cmd = f'{shlex.quote(str(_ENGINE_BIN))} collect && ' \
          f'{shlex.quote(str(_ENGINE_BIN))} run --notes "manual via GUI"'
    log.info("triggering manual run → %s", log_path.name)

    with open(log_path, "wb") as logf:
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", cmd],
            cwd=str(_PROJECT_ROOT),
            stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from parent group
        )
    _write_lock({
        "pid": proc.pid, "started_at": datetime.utcnow().isoformat(),
        "log_path": str(log_path),
    })
    return RedirectResponse("/?status=started", status_code=303)


@app.post("/run/cancel")
def cancel_run():
    """SIGTERM any live subprocess + mark in-flight DB cycle as cancelled."""
    lock = _read_lock()
    if lock and _process_alive(lock.get("pid")):
        try:
            os.killpg(os.getpgid(lock["pid"]), signal.SIGTERM)
            log.info("sent SIGTERM to manual run pgrp %s", lock["pid"])
        except (OSError, ProcessLookupError) as e:
            log.warning("kill failed: %s", e)
    _clear_lock()

    # Also flip the DB cycle so the banner can disappear immediately.
    with get_conn() as conn:
        conn.execute(
            """UPDATE cycles SET status='cancelled', finished_at=?,
                                  notes = COALESCE(notes, '') || ' [cancelled via GUI]'
               WHERE status='running' AND finished_at IS NULL""",
            (datetime.utcnow().isoformat(),),
        )
    return RedirectResponse("/?status=cancelled", status_code=303)


@app.get("/run/status")
def run_status() -> JSONResponse:
    running, cycle, lock = _is_running()
    log_tail = ""
    if lock and lock.get("log_path"):
        try:
            data = Path(lock["log_path"]).read_text(errors="replace")
            log_tail = "\n".join(data.splitlines()[-15:])
        except OSError:
            pass
    return JSONResponse({
        "running": running,
        "cycle_id": cycle["id"] if cycle else None,
        "started_at": (cycle or lock or {}).get("started_at"),
        "log_tail": log_tail,
    })


@app.get("/run/status/fragment", response_class=HTMLResponse)
def run_status_fragment(request: Request):
    """HTMX-polled fragment. Returns the banner HTML matching current state."""
    running, cycle, lock = _is_running()
    log_tail = ""
    if running and lock and lock.get("log_path"):
        try:
            data = Path(lock["log_path"]).read_text(errors="replace")
            log_tail = "\n".join(data.splitlines()[-8:])
        except OSError:
            pass
    return templates.TemplateResponse(
        request, "_run_status.html",
        {"running": running, "cycle": cycle, "lock": lock,
         "log_tail": log_tail, "brand": settings.brand_name},
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cycles = _cycles(25)
    latest = cycles[0] if cycles else None
    latest_ideas = _ideas_in_cycle(latest["id"]) if latest else []
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"cycles": cycles, "latest": latest,
         "latest_ideas": latest_ideas, "brand": settings.brand_name},
    )


@app.get("/cycle/{cycle_id}", response_class=HTMLResponse)
def cycle_view(request: Request, cycle_id: str):
    cycles = _cycles(25)
    target = next((c for c in cycles if c["id"] == cycle_id), None)
    if not target:
        raise HTTPException(404, f"cycle {cycle_id} not found")
    ideas = _ideas_in_cycle(cycle_id)
    return templates.TemplateResponse(
        request, "cycle.html",
        {"cycle": target, "ideas": ideas,
         "brand": settings.brand_name, "cycles": cycles},
    )


@app.get("/swipe/{cycle_id}", response_class=HTMLResponse)
def swipe_view(request: Request, cycle_id: str, idx: int = 0):
    ideas = _ideas_in_cycle(cycle_id)
    if not ideas:
        raise HTTPException(404, "no ideas in cycle")
    # Find first pending idea if no idx given.
    if idx == 0:
        pending = [i for i, x in enumerate(ideas) if x["fate"] == "pending"]
        if pending:
            idx = pending[0]
    idx = max(0, min(idx, len(ideas) - 1))
    current = _idea(ideas[idx]["idea_id"])
    return templates.TemplateResponse(
        request, "swipe.html",
        {"cycle_id": cycle_id, "idea": current,
         "idx": idx, "total": len(ideas), "brand": settings.brand_name},
    )


@app.post("/idea/{idea_id}/fate")
def set_fate(idea_id: str, fate: str = Form(...), reason: str = Form("")):
    if fate not in {"rejected", "parked", "produced", "pending"}:
        raise HTTPException(400, f"invalid fate {fate}")
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE ideas SET fate=?, fate_reason=?, fate_set_at=?
               WHERE idea_id=?""",
            (fate, reason or None, datetime.utcnow().isoformat(), idea_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"unknown idea {idea_id}")
        # Find cycle so we can route the user forward.
        row = conn.execute("SELECT cycle_id FROM ideas WHERE idea_id=?",
                           (idea_id,)).fetchone()
    cycle_id = row["cycle_id"]
    ideas = _ideas_in_cycle(cycle_id)
    cur_idx = next((i for i, x in enumerate(ideas) if x["idea_id"] == idea_id), 0)
    next_idx = min(cur_idx + 1, len(ideas) - 1)
    return RedirectResponse(f"/swipe/{cycle_id}?idx={next_idx}", status_code=303)


@app.post("/idea/{idea_id}/video")
def attach_video(idea_id: str, video_id: str = Form(...)):
    video_id = video_id.strip()
    if not video_id:
        raise HTTPException(400, "video_id required")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE ideas SET produced_video_id=?, fate=? WHERE idea_id=?",
            (video_id, "produced", idea_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"unknown idea {idea_id}")
        # Mirror to video_performance with what we know.
        conn.execute(
            """INSERT OR IGNORE INTO video_performance
               (video_id, idea_id, fetched_at) VALUES (?, ?, ?)""",
            (video_id, idea_id, datetime.utcnow().isoformat()),
        )
    return RedirectResponse(f"/idea/{idea_id}", status_code=303)


@app.get("/idea/{idea_id}", response_class=HTMLResponse)
def idea_detail(request: Request, idea_id: str):
    idea = _idea(idea_id)
    if not idea:
        raise HTTPException(404, f"idea {idea_id} not found")
    return templates.TemplateResponse(
        request, "idea.html",
        {"idea": idea, "brand": settings.brand_name},
    )


@app.get("/performance", response_class=HTMLResponse)
def performance(request: Request):
    with get_conn() as conn:
        videos = conn.execute(
            """SELECT vp.*, i.payload_json AS idea_payload
               FROM video_performance vp
               LEFT JOIN ideas i ON i.idea_id = vp.idea_id
               ORDER BY vp.fetched_at DESC"""
        ).fetchall()
    videos = [
        {**dict(v),
         "idea_payload": json.loads(v["idea_payload"]) if v["idea_payload"] else None}
        for v in videos
    ]
    perf_path = settings.db_path.parent / "2gt_perf_30d.json"
    perf_snapshot = json.loads(perf_path.read_text()) if perf_path.exists() else None
    return templates.TemplateResponse(
        request, "performance.html",
        {"videos": videos, "snapshot": perf_snapshot,
         "brand": settings.brand_name},
    )
