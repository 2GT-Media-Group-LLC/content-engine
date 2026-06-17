"""Typer CLI. Slim, scriptable entry point. The GUI will sit on top of these."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .collectors.base import ingest_signals, signals_from_reddit_listing, signals_from_youtube_videos
from .config import settings
from .db import get_conn, init_db
from .pipeline import finish_cycle, new_cycle, run_processing_only
from .reports.render import render_weekly

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command()
def init():
    """Initialize the SQLite DB and confirm Ollama tiers are pulled."""
    from .ollama_client import check_tiers, OllamaError
    init_db()
    table = Table(title="Routing config", show_header=True, header_style="bold magenta")
    table.add_column("Tier"); table.add_column("Model"); table.add_column("Pulled?"); table.add_column("Purpose")
    try:
        tiers = check_tiers()
    except OllamaError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=2)
    missing = []
    # check_tiers() de-dupes by tag; map back to a row per (tag, present).
    for tier, ok in tiers:
        table.add_row(
            tier.name, tier.ollama_tag,
            "[green]✓[/green]" if ok else "[red]missing[/red]",
            tier.purpose,
        )
        if not ok:
            missing.append(tier.ollama_tag)
    console.print(table)
    console.print(f"[dim]DB: {settings.db_path}[/dim]")
    console.print(f"[dim]Reports: {settings.reports_dir}[/dim]")
    if missing:
        console.print("\n[bold red]Missing models — pull before running:[/bold red]")
        for tag in missing:
            console.print(f"  ollama pull {tag}")


@app.command(name="ingest-youtube-json")
def ingest_youtube_json(path: Path):
    """Ingest a vidiq channel videos JSON dump."""
    raw = json.loads(path.read_text())
    items = raw if isinstance(raw, list) else [raw]
    total = 0
    for L in items:
        videos = L.get("videos", [])
        channel = {
            "channelId": L.get("channelId"),
            "channelTitle": L.get("channelTitle") or L.get("title"),
            "is_owned": L.get("is_owned", False),
        }
        total += ingest_signals(signals_from_youtube_videos(videos, channel))
    console.print(f"[green]ingested[/green] {total} youtube signal(s)")


@app.command(name="ingest-reddit-json")
def ingest_reddit_json(path: Path):
    """Ingest a Reddit JSON dump. Accepts: a single Reddit listing
    (kind/data/children), an array of listings, or a flat array of post dicts."""
    raw = json.loads(path.read_text())
    items = raw if isinstance(raw, list) else [raw]
    is_flat = bool(items) and isinstance(items[0], dict) and "id" in items[0] \
        and "kind" not in items[0] and "data" not in items[0]
    total = 0
    if is_flat:
        sigs = signals_from_reddit_listing({"children": [{"kind": "t3", "data": p} for p in items]})
        total += ingest_signals(sigs)
    else:
        for L in items:
            sigs = signals_from_reddit_listing(L)
            total += ingest_signals(sigs)
    console.print(f"[green]ingested[/green] {total} reddit signal(s)")


@app.command()
def run(top_clusters: int = 5, cluster_distance: float = 0.32, notes: str = ""):
    """Run the processing pipeline (assumes signals already collected)."""
    from .ollama_client import OllamaError
    cid = new_cycle(notes=notes or None)
    try:
        result = run_processing_only(cid, top_clusters=top_clusters, cluster_distance=cluster_distance)
    except OllamaError as e:
        # Pre-flight already marked the cycle failed + logged the fix.
        console.print(f"[bold red]✗ pre-flight check failed[/bold red]\n{e}")
        raise typer.Exit(code=2)
    table = Table(title=f"Cycle {cid}", show_header=False)
    for k, v in result.items():
        table.add_row(str(k), str(v))
    console.print(table)
    console.print(f"\n[bold green]Open the report:[/bold green] file://{result['report_path']}")


@app.command()
def fate(idea_id: str, status: str, reason: str = "",
         video: str = typer.Option("", "--video",
                                    help="YouTube video ID (for produced ideas — "
                                         "enables post-mortem calibration)")):
    """Mark an idea's fate: rejected | parked | produced."""
    if status not in {"rejected", "parked", "produced"}:
        raise typer.BadParameter("status must be one of: rejected, parked, produced")
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE ideas SET fate=?, fate_reason=?, fate_set_at=?,
               produced_video_id=COALESCE(NULLIF(?, ''), produced_video_id)
               WHERE idea_id=?""",
            (status, reason or None, datetime.utcnow().isoformat(),
             video, idea_id),
        )
    if cur.rowcount == 0:
        raise typer.BadParameter(f"unknown idea_id: {idea_id}")
    msg = f"[green]marked[/green] {idea_id} → {status}"
    if video:
        msg += f" (video {video} — run [bold]engine post-mortem[/bold] in ~30 days)"
    console.print(msg)


@app.command()
def report(cycle_id: str):
    """Re-render report for a given cycle."""
    out = render_weekly(cycle_id)
    console.print(f"file://{out}")


@app.command()
def gui(host: str = "127.0.0.1", port: int = 8080, reload: bool = False):
    """Launch the web GUI for idea triage + performance tracking."""
    import uvicorn
    console.print(f"[bold green]{settings.brand_name} Engine GUI[/bold green]  →  http://{host}:{port}")
    uvicorn.run("content_engine.web.app:app", host=host, port=port,
                reload=reload, log_level="info")


@app.command()
def collect(
    provider: str = typer.Option(
        "", "--provider",
        help="Override YOUTUBE_PROVIDER for this run (vidiq | composio | noop).",
    ),
):
    """Pull fresh signals from every configured source.

    Sources:
      - reddit: direct public JSON (no auth)
      - youtube: via active provider (vidiq by default, composio as fallback)
      - outliers: breakout niche videos beyond the peer list (vidiq)
      - youtube_comments: audience questions mined from comments (vidiq)
      - blog: RSS/Atom feeds in data/feeds.yaml
      - hackernews: Algolia HN search (no auth)
      - github: GitHub releases in data/github_repos.yaml (unauth, 60/hr)
    """
    from .collectors import (reddit, youtube, feeds, hackernews,
                              github_releases, comments, outliers)
    yt_kwargs = {"force_provider": provider} if provider else {}
    results = []
    results.append(reddit.collect())
    results.append(youtube.collect(**yt_kwargs))
    results.append(outliers.collect(**yt_kwargs))
    results.append(comments.collect(**yt_kwargs))
    results.append(feeds.collect())
    results.append(hackernews.collect())
    results.append(github_releases.collect())
    t = Table(title="Collection summary", show_header=True, header_style="bold magenta")
    t.add_column("Source"); t.add_column("Provider"); t.add_column("Ingested"); t.add_column("Errors")
    for r in results:
        err = r.get("errors") or []
        err_str = ("; ".join(err)[:100] + ("…" if len(err) > 2 else "")) if err else "-"
        label = r.get("collector") or r["platform"]
        t.add_row(label, r.get("provider", "-"), str(r["ingested"]), err_str)
    console.print(t)


@app.command(name="sync-performance")
def sync_performance(
    provider: str = typer.Option(
        "", "--provider",
        help="Override YOUTUBE_PROVIDER for this run (vidiq | composio).",
    ),
):
    """Refresh the own-channel performance snapshot via the active provider.

    Updates data/<brand_short>_perf_30d.json — read by the idea synthesizer
    for per-cycle performance context. Only vidiq currently supports this
    (Composio v3 doesn't expose vidiq's analytics endpoint)."""
    import json as _json
    from .collectors.yt_provider import get_provider

    p = get_provider(force=provider or None)
    console.print(f"[dim]provider={p.name}[/dim]")
    if not p.is_available():
        console.print(f"[red]provider {p.name} unavailable[/red] — check API key in .env")
        raise typer.Exit(code=2)

    own_id = settings.own_channel_id
    if not own_id:
        console.print("[red]no own_channel_id configured[/red] in channel.yaml")
        raise typer.Exit(code=2)

    snapshot = p.get_channel_analytics(own_id, days=30)
    if snapshot is None:
        console.print(f"[yellow]provider {p.name} doesn't support analytics[/yellow] "
                       f"— snapshot not refreshed. Edit data/{settings.brand_short.lower()}_perf_30d.json "
                       f"manually or switch to YOUTUBE_PROVIDER=vidiq.")
        raise typer.Exit(code=1)

    # Tier each video (deterministic) + distill what_works narratives (local LLM)
    # so the idea synthesizer reads a fully-populated snapshot.
    from .synthesizers.perf_insights import assign_tiers, fill_narratives
    assign_tiers(snapshot)
    fill_narratives(snapshot)

    out_path = settings.db_path.parent / f"{settings.brand_short.lower()}_perf_30d.json"
    out_path.write_text(_json.dumps(snapshot.model_dump(exclude_none=False), indent=2))
    tiers = {}
    for v in snapshot.videos:
        if v.tier:
            tiers[v.tier] = tiers.get(v.tier, 0) + 1
    console.print(f"[green]synced[/green] {settings.brand_name} performance snapshot "
                   f"→ {out_path.name} ({len(snapshot.videos)} videos, tiers: {tiers})")
    if snapshot.what_works:
        console.print("[dim]what's working:[/dim]")
        for w in snapshot.what_works:
            console.print(f"  [green]+[/green] {w}")


@app.command()
def retro(force: bool = typer.Option(False, "--force",
                                      help="Regenerate even if triage state unchanged")):
    """Distill triage history (fates + reasons) into style/editorial_guide.md.

    The guide is injected into every idea-generation prompt — the engine's
    living memory of what you green-light, park, and reject. Runs
    automatically at the start of each pipeline cycle; this command forces
    a refresh on demand."""
    from .synthesizers.retro import maybe_refresh
    result = maybe_refresh(force=force)
    if result.get("refreshed"):
        console.print(f"[green]refreshed[/green] editorial guide from "
                       f"{result['n_decided']} decided ideas → {result['path']}")
    else:
        console.print(f"[dim]not refreshed:[/dim] "
                       f"{result.get('reason') or result.get('error') or 'already current'}")


@app.command(name="post-mortem")
def post_mortem():
    """Compare predictions vs. reality for produced videos.

    For every idea marked produced with a --video ID: fetches current stats,
    grades the prediction (breakout → flopped vs. channel median), updates
    video_performance, and writes data/calibration.md — which feeds back
    into the editorial guide on the next retro."""
    from .synthesizers.postmortem import run_postmortem
    result = run_postmortem()
    if not result.get("checked"):
        console.print("[yellow]no produced ideas with linked videos[/yellow] — "
                       "link one: engine fate <idea_id> produced --video <yt_id>")
    else:
        console.print(f"[green]graded[/green] {result['checked']} video(s): "
                       f"{result.get('grades', {})}")
        console.print(f"[dim]→ {result['path']}[/dim]")


@app.command()
def produce(idea_id: str,
            thumbnail: bool = typer.Option(False, "--thumbnail",
                                            help="Also generate an AI thumbnail "
                                                 "(22 vidiq credits)")):
    """Build a production pack for a green-lit idea.

    Gathers outline, verified sources, transcripts of the top related videos,
    a scored title board (provider-generated + engine suggestions), and
    optionally a generated thumbnail — one markdown handoff doc, ready for
    scripting."""
    from .synthesizers.producer import build_production_pack
    try:
        path = build_production_pack(idea_id, thumbnail=thumbnail)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    console.print(f"[bold green]production pack ready[/bold green] → {path}")
    console.print(f"[dim]open: file://{path}[/dim]")


@app.command(name="composio-disconnect")
def composio_disconnect():
    """Scrub locally-stored Composio credentials + print revocation steps.

    Run this after migrating to YOUTUBE_PROVIDER=vidiq if you want to close
    the Composio blast radius. Removes ~/.cache/content-engine/composio_connected.json
    and tells you what to revoke on app.composio.dev's side."""
    from pathlib import Path as _Path
    cache = _Path.home() / ".cache" / "content-engine" / "composio_connected.json"
    if cache.exists():
        cache.unlink()
        console.print(f"[green]removed[/green] {cache}")
    else:
        console.print(f"[dim]no cache file at {cache}[/dim]")
    console.print()
    console.print("[bold yellow]Manual steps to fully revoke Composio access:[/bold yellow]")
    console.print("  1. Sign in at https://app.composio.dev")
    console.print("  2. Go to Settings → Connected Accounts")
    console.print("  3. Disconnect YouTube (and any other toolkit you connected)")
    console.print("  4. Go to Settings → API Keys and delete the API key your engine was using")
    console.print("  5. Remove COMPOSIO_API_KEY=… from this project's .env")
    console.print()
    console.print("[dim]The engine will keep working — VidIQ is the default provider.[/dim]")


@app.command(name="extract-voice")
def extract_voice_cmd():
    """Extract a voice guide from data/voice_corpus/scripts_signals.json."""
    from .synthesizers.voice import extract_voice_guide
    p = extract_voice_guide()
    console.print(f"[green]wrote[/green] {p}")


@app.command(name="eval")
def eval_cmd(agent: str = ""):
    """Run golden eval cases. Pass --agent to scope to one agent."""
    from .eval import run_eval
    agents = [agent] if agent else None
    summary = run_eval(agents)
    t = Table(title=f"Eval: {summary['passed']}/{summary['total']} passed",
              show_header=True, header_style="bold magenta")
    t.add_column("Agent"); t.add_column("Case"); t.add_column("Result"); t.add_column("Conf"); t.add_column("ms")
    t.add_column("Failures")
    for r in summary["results"]:
        result = "[green]PASS[/green]" if r["passed"] else "[red]FAIL[/red]"
        conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "-"
        fails = "; ".join(r["failures"]) if r["failures"] else ""
        t.add_row(r["agent"], r["case_id"], result, conf, str(r["elapsed_ms"]), fails[:80])
    console.print(t)
    raise typer.Exit(code=0 if summary["failed"] == 0 else 1)


@app.command()
def polish(idea_id: str, out: Path | None = None):
    """Export a green-lit idea + its source signals as a markdown handoff for a
    Claude Code polish session. Pure local file write — no API calls."""
    with get_conn() as conn:
        idea_row = conn.execute(
            "SELECT * FROM ideas WHERE idea_id=?", (idea_id,)
        ).fetchone()
        if not idea_row:
            raise typer.BadParameter(f"unknown idea_id: {idea_id}")
        payload = json.loads(idea_row["payload_json"])
        cluster_ids = payload.get("source_cluster_ids", [])
        signal_ids = payload.get("source_signal_ids", [])
        clusters = []
        if cluster_ids:
            placeholders = ",".join("?" * len(cluster_ids))
            clusters = conn.execute(
                f"SELECT * FROM clusters WHERE id IN ({placeholders})", cluster_ids
            ).fetchall()
        sigs = []
        if signal_ids:
            placeholders = ",".join("?" * len(signal_ids))
            sigs = conn.execute(
                f"""SELECT s.title, s.url, s.platform, sm.one_line, sm.key_points_json
                    FROM signals s LEFT JOIN summaries sm ON sm.signal_id=s.id
                    WHERE s.id IN ({placeholders})""",
                signal_ids,
            ).fetchall()

    out = out or settings.reports_dir / f"polish_{idea_id}.md"
    lines = [
        f"# Polish handoff — {idea_id}",
        "",
        "Drop this into a Claude Code session and ask: \"polish this idea for the brief"
        " and draft a script outline.\" Claude has memory of the channel context already.",
        "",
        "## Generated draft",
        "",
        f"**Format:** {payload.get('format')}",
        f"**Confidence:** {payload.get('confidence')}",
        f"**Fatigue:** {payload.get('fatigue_score')}",
        f"**Risk flags:** {', '.join(payload.get('risk_flags', [])) or 'none'}",
        "",
        f"### Angle\n{payload.get('angle')}",
        "",
        f"### Why now\n{payload.get('why_now')}",
        "",
        f"### Audience fit\n{payload.get('audience_fit')}",
        "",
        "### Suggested titles",
        *(f"- {t}" for t in payload.get("suggested_titles", [])),
        "",
        "### Thumbnail concepts",
        *(f"- {t}" for t in payload.get("thumbnail_concepts", [])),
        "",
        "## Source clusters",
    ]
    for c in clusters:
        lines += [
            f"- **{c['label']}** (heat {c['heat_score']:.1f}, sentiment {c['avg_sentiment']:+.2f})",
            f"  - quote: {c['representative_quote'] or '—'}",
        ]
    lines += ["", "## Source signals (raw)"]
    for s in sigs:
        lines += [
            f"- **[{s['platform']}]** {s['title']}",
            f"  - {s['url']}",
            f"  - summary: {s['one_line']}" if s["one_line"] else "",
        ]
    out.write_text("\n".join(l for l in lines if l is not None))
    console.print(f"[green]wrote[/green] {out}")
    console.print("[dim]Open this in a Claude Code session for polish + outline.[/dim]")


@app.command(name="list-cycles")
def list_cycles(limit: int = 10):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cycles ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    t = Table(show_header=True, header_style="bold magenta")
    t.add_column("Cycle"); t.add_column("Started"); t.add_column("Finished"); t.add_column("Status"); t.add_column("Notes")
    for r in rows:
        t.add_row(r["id"], r["started_at"], r["finished_at"] or "-", r["status"], r["notes"] or "")
    console.print(t)


@app.command(name="list-ideas")
def list_ideas(cycle_id: str | None = None, fate: str | None = None):
    q = "SELECT idea_id, cycle_id, fate, json_extract(payload_json, '$.angle') AS angle, " \
        "json_extract(payload_json, '$.confidence') AS conf FROM ideas WHERE 1=1"
    args: list = []
    if cycle_id:
        q += " AND cycle_id=?"; args.append(cycle_id)
    if fate:
        q += " AND fate=?"; args.append(fate)
    q += " ORDER BY conf DESC LIMIT 50"
    with get_conn() as conn:
        rows = conn.execute(q, args).fetchall()
    t = Table(show_header=True, header_style="bold magenta")
    t.add_column("ID"); t.add_column("Cycle"); t.add_column("Fate"); t.add_column("Conf"); t.add_column("Angle")
    for r in rows:
        t.add_row(r["idea_id"], r["cycle_id"], r["fate"],
                  f"{r['conf']:.2f}" if r["conf"] is not None else "-",
                  (r["angle"] or "")[:80])
    console.print(t)


if __name__ == "__main__":
    app()
