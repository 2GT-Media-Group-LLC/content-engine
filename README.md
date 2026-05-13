# Content Engine

A self-hosted, locally-run AI research team for technical YouTube channels.

It pulls weekly signal from Reddit, YouTube, RSS feeds, Hacker News, and GitHub
releases. It clusters the noise into trends. It hands you a brief: five video
ideas with outlines, source citations, and click-through-rate-scored titles.
It learns from which ideas you green-light, park, or reject.

Runs entirely on local Ollama models (Gemma 4 + Qwen 3). Zero per-cycle cost.
Zero data sent to outside services.

> **Plain-English explainer:** [`docs/how-it-works.html`](docs/how-it-works.html)
> (or the [PDF version](docs/how-it-works.pdf)) — shareable with non-technical
> stakeholders.

---

## Quick start

```bash
git clone <this-repo> content-engine
cd content-engine
./setup.sh                          # creates venv, installs deps, pulls models
cp channel.example.yaml channel.yaml
$EDITOR channel.yaml                # set your brand, channel ID, peers, niche

# Optional: enable OAuth-required sources (YouTube)
echo 'COMPOSIO_API_KEY=ak_yours_here' > .env

# One end-to-end run
.venv/bin/engine collect            # pull fresh signals (~30 sec)
.venv/bin/engine run                # summarize → cluster → ideate → render (~10 min)
.venv/bin/engine gui                # open http://127.0.0.1:8080 to triage
```

## What it does

```
┌─────────────┐    ┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│  Collect    │ →  │  Summarize  │ →  │  Cluster +   │ →  │  Synthesize │
│  5 streams  │    │  per signal │    │  rank trends │    │  ideas      │
└─────────────┘    └─────────────┘    └──────────────┘    └─────────────┘
                                                                ↓
┌─────────────┐    ┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│  Render     │ ←  │  Verify     │ ←  │  Score       │ ←  │  Outline    │
│  HTML brief │    │  citations  │    │  titles      │    │  top 3      │
└─────────────┘    └─────────────┘    └──────────────┘    └─────────────┘
                          ↓
                  ┌─────────────┐
                  │  Human      │  (you, in the GUI)
                  │  triage     │  ✓ Producing  🅿 Park  ✗ Reject
                  └─────────────┘
```

## Sources (out of the box, easy to swap)

| Source | Where it's configured | Auth |
|---|---|---|
| Reddit | `channel.yaml` → `reddit.subreddits` | none |
| YouTube (peers + own) | `channel.yaml` → `youtube` | Composio + Google OAuth |
| RSS / Atom blogs | `data/feeds.yaml` | none |
| Hacker News | `channel.yaml` → `hackernews.queries` | none |
| GitHub releases | `data/github_repos.yaml` | none (60/hr unauth) |

## What's in the repo

```
.
├── channel.example.yaml      copy → channel.yaml; defines your brand + sources
├── data/
│   ├── feeds.yaml             RSS/Atom registry (vendor blogs, tech press)
│   └── github_repos.yaml      GitHub release tracking
├── src/content_engine/        the engine
│   ├── collectors/            one module per source
│   ├── processors/            summarize, cluster
│   ├── synthesizers/          ideas, outlines, polish, citation verify
│   ├── reports/               styled HTML renderer
│   └── web/                   FastAPI feedback UI
├── scripts/
│   ├── install-launchd.sh     install macOS LaunchAgents (weekly + GUI)
│   └── templates/             plist templates
├── docs/
│   ├── how-it-works.html      explainer (non-technical → technical)
│   └── how-it-works.pdf       same, as PDF
└── tests/golden/              regression cases for the eval harness
```

## Configuration

Everything channel-specific lives in **`channel.yaml`** (gitignored):
- Brand name + short, niche topics, audience description
- Divisive-topic auto-flags
- Your YouTube channel ID + peer channels
- Subreddits, HN keywords, frequency limits

Feed and GitHub-release registries are separate YAMLs because they change more
often (per niche, not per channel):
- **`data/feeds.yaml`** — RSS/Atom sources
- **`data/github_repos.yaml`** — repos to track for releases

## Optional: weekly autonomous schedule + auto-start GUI (macOS)

```bash
./scripts/install-launchd.sh
```

Installs two LaunchAgents:
- `com.contentengine.weekly` — Sunday 3 AM, runs `collect && run`
- `com.contentengine.gui` — auto-starts the GUI at login, restarts on crash

## Commands

```
engine init                     check Ollama tier readiness, init DB
engine collect                  pull fresh signals from every configured source
engine run                      run the full pipeline on collected signals
engine gui [--port 8080]        launch the feedback UI
engine eval [--agent NAME]      run golden test cases
engine list-cycles              show recent pipeline runs
engine list-ideas [--fate F]    browse generated ideas
engine fate <idea_id> <fate>    mark an idea rejected | parked | produced
engine polish <idea_id>         export an idea as markdown for manual polish
engine extract-voice            re-extract voice guide from your scripts
engine sync-performance         refresh 2GT performance snapshot via vidiq
engine report <cycle_id>        re-render an existing cycle's HTML
```

## Hardware

Tested on Apple Silicon (M5 Max, 48 GB unified). The heavy model
(`qwen3:30b-a3b-instruct-2507-q8_0`) needs ~33 GB during inference; other tiers
are 2-10 GB each. A typical weekly cycle costs ~$0 in API spend (everything
local) and ~10 minutes of wall time.

Linux + sufficient RAM should work but is not exercised. Windows is untested.

## Privacy + secrets

- `.env` and `channel.yaml` are gitignored.
- Personal data (voice guide, performance snapshots, voice corpus, run logs)
  is gitignored — see `.gitignore` for the full list.
- A `gitleaks` pre-commit hook scans for accidentally-staged secrets if you
  install it: `pip install pre-commit && pre-commit install`.

## License

MIT — see [`LICENSE`](LICENSE).

## Why this exists

Build context: [`docs/how-it-works.html`](docs/how-it-works.html) walks through
the architecture for both technical and non-technical readers.
