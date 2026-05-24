# Content Engine

A self-hosted, locally-run AI research team for technical YouTube channels.

It pulls weekly signal from Reddit, YouTube, RSS feeds, Hacker News, and GitHub
releases. It clusters the noise into trends. It hands you a brief: five video
ideas with outlines, source citations, and click-through-rate-scored titles.
It learns from which ideas you green-light, park, or reject.

Runs entirely on local Ollama models (Gemma 4 + Qwen 3). Zero per-cycle cost.
Zero data sent to outside services unless you explicitly opt into a third-party
integration (see [Optional integrations](#optional-integrations-composio--vidiq)).

---

## Table of contents

- [What it does](#what-it-does)
- [Hardware + prerequisites](#hardware--prerequisites)
- [Setup, step by step](#setup-step-by-step)
- [Sources (out of the box)](#sources-out-of-the-box)
- [YouTube data provider: VidIQ (default) or Composio (fallback)](#youtube-data-provider-vidiq-default-or-composio-fallback)
  - [What is VidIQ? Do I need it?](#what-is-vidiq-do-i-need-it)
  - [What is Composio? Do I need it?](#what-is-composio-do-i-need-it)
  - [Switching providers + cost reference](#switching-providers--cost-reference)
- [Configuration files](#configuration-files)
- [Optional: weekly autonomous schedule + auto-start GUI (macOS)](#optional-weekly-autonomous-schedule--auto-start-gui-macos)
- [Commands](#commands)
- [What's in the repo](#whats-in-the-repo)
- [Privacy + secrets](#privacy--secrets)
- [License](#license)

---

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

Every weekly cycle produces a styled HTML brief, persisted ideas with full
source traceback, and a "swipe deck" UI for triage. Rejection/park reasons
feed back into the next cycle so the engine learns what your channel does and
doesn't want to talk about.

---

## Hardware + prerequisites

**Tested on Apple Silicon (M5 Max, 48 GB unified).** Linux + sufficient RAM
should work but is not exercised. Windows is untested.

| Requirement | Why | Notes |
|---|---|---|
| **Python 3.11+** | engine runtime | 3.11 / 3.12 / 3.13 / 3.14 all tested |
| **[Ollama](https://ollama.com/download)** | local LLM inference | must be reachable on `OLLAMA_HOST` (default `http://localhost:11434`) |
| **~40 GB free RAM during inference** | the heavy tier (`qwen3:30b-a3b-instruct-2507-q8_0`) needs ~33 GB at peak | other tiers are 2-10 GB |
| **~50 GB free disk** | Ollama model weights | one-time download |
| **macOS** (for the launchd helper) | optional auto-schedule + auto-start GUI | the engine itself is OS-agnostic |

A typical weekly cycle costs **~$0** in API spend (everything runs on Ollama)
and **~10 minutes** of wall time.

---

## Setup, step by step

### 1. Install Ollama and start the server

```bash
# macOS
brew install ollama
# or download the .app from https://ollama.com/download

# verify it's running and reachable
ollama list                       # should print an empty table the first time
curl http://localhost:11434/      # should print "Ollama is running"
```

If Ollama runs on another machine on your LAN (e.g. a beefier server), set
`OLLAMA_HOST=http://<host>:11434` in `.env` (covered below).

### 2. Clone and run the installer

```bash
git clone https://github.com/2GT-Media-Group-LLC/content-engine.git
cd content-engine
./setup.sh
```

`setup.sh` is idempotent. It will:
1. Verify Python 3.11+
2. Create `.venv/` and install the package + dependencies
3. Pull the four Ollama models the engine routes to
   (`nomic-embed-text`, `gemma4:e2b`, `gemma4:e4b`,
   `qwen3:30b-a3b-instruct-2507-q8_0`)
4. Scaffold `channel.yaml` from `channel.example.yaml` (if missing)
5. Scaffold `.env` (if missing)
6. Create `data/`, `data/runs/`, `data/vectors/`, `reports/`
7. Run `engine init` to build the SQLite schema and confirm tier readiness

The first run takes 10-30 minutes depending on your network — most of that is
pulling ~40 GB of model weights.

### 3. Configure your channel

Edit **`channel.yaml`** (created from `channel.example.yaml`). The defaults are
tuned for a homelab / self-hosting / AI / virtualization channel; replace with
your own niche. Key fields:

```yaml
brand:
  name: "Your Channel Name"      # full name (used in prompts + report titles)
  short: "YOUR"                  # short tag, often the channel acronym
  niche:                         # ordered list of topics your channel covers
    - homelab
    - self-hosting

audience_summary: |              # plain-English description fed into every prompt
  Mid-to-high technical viewers comfortable with Linux, containers,
  and virtualization.

divisive_topics:                 # topics that auto-flag risk_flags on ideas
  - name: "AI (local or cloud)"
    note: "Audience splits between AI-slop critics and enthusiasts"

youtube:
  own_channel_id: "UCXXXX..."    # find at https://www.youtube.com/account_advanced
  own_channel_title: "Your Channel Name"
  peer_channels:                 # 3-5 channels in your niche to watch
    - { handle: "@LAWRENCESYSTEMS", name: "Lawrence Systems" }
    - { handle: "@CraftComputing",  name: "Craft Computing" }

reddit:
  subreddits:                    # public Reddit JSON — no auth needed
    - { name: homelab,    limit: 15 }
    - { name: selfhosted, limit: 15 }

hackernews:                      # Algolia HN search — no auth needed
  queries: [proxmox, homelab, ollama, ...]
  min_points: 20
  window_days: 14
```

Two registry files live in `data/` because they typically change per-niche
rather than per-channel:

- **`data/feeds.yaml`** — RSS/Atom blog sources
- **`data/github_repos.yaml`** — GitHub repos to track for releases

Edit those to match the tools/vendors your audience cares about.

### 4. (Optional) Set up `.env`

`setup.sh` copies `.env.example` into `.env` for you. Defaults look like:

```dotenv
# Where Ollama listens. Default is fine if Ollama runs locally.
OLLAMA_HOST=http://localhost:11434

# YouTube data provider — vidiq (recommended) | composio (legacy) | noop
YOUTUBE_PROVIDER=vidiq

# Get yours from https://app.vidiq.com (Settings → API / Integrations).
# VIDIQ_API_KEY=

# Optional: only if you self-host or proxy the MCP endpoint.
# VIDIQ_MCP_ENDPOINT=https://mcp.vidiq.com/mcp

# Legacy Composio fallback. See docs/SECURITY.md for the migration history.
# COMPOSIO_API_KEY=
```

Without a YouTube provider key the engine still runs — Reddit, Hacker News,
RSS, and GitHub releases all work unauthenticated and typically deliver
80%+ of weekly signal volume. See [YouTube data provider](#youtube-data-provider-vidiq-default-or-composio-fallback)
below for the trade-offs.

### 5. First run

```bash
.venv/bin/engine collect          # pull fresh signals (~30 sec to a few min)
.venv/bin/engine run              # summarize → cluster → ideate (~10 min)
.venv/bin/engine gui              # open http://127.0.0.1:8080 to triage
```

The `gui` command stays in the foreground. In a separate shell you can:
- `engine list-cycles` to see history
- `engine list-ideas --fate pending` to browse ideas in a terminal
- `engine fate <idea_id> rejected --reason "fatigue"` to triage from the CLI

---

## Sources (out of the box)

| Source | Where it's configured | Auth required |
|---|---|---|
| Reddit | `channel.yaml` → `reddit.subreddits` | **none** (public JSON API) |
| Hacker News | `channel.yaml` → `hackernews.queries` | **none** (Algolia HN search) |
| RSS / Atom blogs | `data/feeds.yaml` | **none** |
| GitHub releases | `data/github_repos.yaml` | **none** (unauthenticated, 60 req/hr) |
| YouTube (peers + own) | `channel.yaml` → `youtube`; provider chosen via `YOUTUBE_PROVIDER` in `.env` | **VidIQ API key** (default) or Composio OAuth (legacy) — both optional, see below |

You can run the engine end-to-end with **zero auth**: drop the YouTube
collector and the engine will happily ideate from the four no-auth sources.

---

## YouTube data provider: VidIQ (default) or Composio (fallback)

The engine talks to YouTube through a pluggable provider, selected by
`YOUTUBE_PROVIDER` in `.env`. Both providers are **optional** — without
either, the engine still ideates from Reddit, Hacker News, RSS, and
GitHub-releases (typically 80%+ of weekly signal volume). Pick a provider
when you want peer-channel signal, real own-channel performance data, or
CTR-scored titles.

> **Security note.** Composio.dev was compromised in May 2026. The engine
> migrated its default YouTube path to VidIQ MCP and kept Composio only as
> an optional fallback for users without a VidIQ subscription. See
> [`docs/SECURITY.md`](docs/SECURITY.md) for the incident record and the
> manual cleanup steps if you previously had Composio set up.

### What is VidIQ? Do I need it?

**[VidIQ](https://vidiq.com/)** is a paid YouTube analytics + creator
tooling service. It exposes its full toolset through an MCP server at
`https://mcp.vidiq.com/mcp`. The engine talks to it directly from Python
via a small streamable-HTTP client — no third-party broker, no OAuth
flow, no Google Cloud project. Just one API key in `.env`.

**Setup (5 minutes):**

1. Sign up / sign in at <https://app.vidiq.com> on a paid tier that
   includes API access.
2. Authorize your own YouTube channel inside VidIQ (Settings → Channels →
   add channel and grant access). Required for the analytics path.
3. Grab your API key from VidIQ's settings.
4. Drop it in `.env`:
   ```dotenv
   YOUTUBE_PROVIDER=vidiq
   VIDIQ_API_KEY=...
   ```
5. `engine collect` and `engine sync-performance` now work end-to-end.

**What you get with VidIQ:**

- **Peer + own-channel video collection** — `vidiq_channel_videos` pulls
  each channel's recent uploads (own) or most-popular videos (peers, as a
  proven trend signal). Free-tier descriptions, tags, view/like/comment
  counts via batch enrichment (`vidiq_get_videos_by_ids`).
- **Real CTR-scored titles** — `vidiq_score_title` returns a 0-100
  prediction calibrated on actual YouTube data. The brief sorts each
  idea's suggested titles by this score; heuristic still kicks in as a
  per-title fallback if a call fails.
- **Automatic 30-day performance snapshot** — `engine sync-performance`
  combines `vidiq_channel_analytics` + `vidiq_channel_stats` and writes
  `data/<brand_short>_perf_30d.json`. The idea synthesizer reads that file
  to weight new ideas toward formats and topics that are actually
  performing on **your** channel. (Composio's path cannot do this — see
  below.)

**Cost:** Most VidIQ tools charge 5 credits per call.  A typical weekly
cycle for one channel + 4 peers costs **~120 credits** all-in (channel
videos, batch enrichment, analytics, ~15 title scores). VidIQ paid tiers
typically include thousands of credits per month, so this comfortably fits.

**Skip VidIQ if:** you don't have a paid subscription, you don't want a
paid dependency at all, or you're fine running with no YouTube signal at
all (heuristic title scorer is solid for technical-channel content).

### What is Composio? Do I need it?

**[Composio](https://composio.dev/)** is a managed-OAuth aggregator. It
used to be the engine's default YouTube path: you'd grant the engine
YouTube access through Composio's OAuth flow, and the engine would call
`YOUTUBE_LIST_CHANNEL_VIDEOS` through Composio's Python SDK.

**Composio is now a legacy fallback only.** Reasons to still use it:

- You don't have a VidIQ subscription and don't want a paid dependency.
- You want a no-payment OAuth-only path for peer-channel uploads.

**Setup:**

1. Create a Composio account at <https://app.composio.dev>
2. Click "Connect" on the YouTube tile and grant consent
3. Copy your API key from **Settings → API Keys**
4. Drop it in `.env`:
   ```dotenv
   YOUTUBE_PROVIDER=composio
   COMPOSIO_API_KEY=...
   ```

**Limitations vs VidIQ:**

- **No channel analytics.** Composio v3 doesn't expose VidIQ's analytics
  endpoint, so `engine sync-performance` can't auto-populate the
  performance snapshot. You'd need to maintain
  `data/<brand_short>_perf_30d.json` by hand.
- **No per-video enrichment.** Composio's YouTube wrapper returns
  metadata only (title, ID, publishedAt) — no view/like counts, no
  descriptions in `RawSignal.body`. The summarizer and clusterer see
  less signal per video.
- **Title scoring** still works through `VIDIQ_SCORE_TITLE` if you also
  connect the VidIQ toolkit inside Composio.

**After the 2026 breach** — if you had Composio set up before, run
`engine composio-disconnect` and follow the printed steps to revoke
server-side access. The engine will keep working — VidIQ is the default.

### Switching providers + cost reference

The choice lives in one env var:

```dotenv
YOUTUBE_PROVIDER=vidiq      # or composio, or noop
```

You can also override for a single run with `--provider`:

```bash
engine collect --provider vidiq             # one-off test against VidIQ
engine collect --provider composio          # fall back temporarily
engine sync-performance --provider vidiq    # only vidiq supports this
```

**Setup matrix:**

| Setup | What you can do | Cost / week |
|---|---|---|
| **Neither** (default if no keys) | Reddit + HN + RSS + GitHub signal. Heuristic title scoring. No own-channel perf context. | $0 |
| **Composio only** | Adds peer + own YouTube uploads (titles only, no metrics, no descriptions). Heuristic titles unless you also connect VidIQ inside Composio. No auto perf snapshot. | $0 (free Composio tier) |
| **VidIQ only** | Adds peer + own YouTube uploads with full metrics, descriptions, tags. VidIQ-scored titles. Auto-refreshed perf snapshot. **Recommended.** | ~120 credits (~$0 if your VidIQ plan already includes them) |

---

## Configuration files

| File | Purpose | Tracked? |
|---|---|---|
| `channel.yaml` | brand, niche, audience, peers, subreddits, HN queries | gitignored |
| `channel.example.yaml` | template + documented defaults | tracked |
| `.env` | `YOUTUBE_PROVIDER`, `VIDIQ_API_KEY`, `COMPOSIO_API_KEY`, `OLLAMA_HOST` | gitignored |
| `.env.example` | template + documented defaults for `.env` | tracked |
| `data/feeds.yaml` | RSS/Atom registry | tracked |
| `data/github_repos.yaml` | GitHub release tracking | tracked |
| `style/voice_guide.md` | (optional) extracted personal voice profile | gitignored |
| `data/<brand_short>_perf_30d.json` | (optional) own-channel perf snapshot | gitignored |

---

## Optional: weekly autonomous schedule + auto-start GUI (macOS)

```bash
./scripts/install-launchd.sh
```

Installs two LaunchAgents:
- `com.contentengine.weekly` — Sunday 3 AM, runs `collect && run`
- `com.contentengine.gui` — auto-starts the GUI at login, restarts on crash

Plist files are templated from `scripts/templates/` — they substitute your
project root, brand label, and GUI port at install time.

---

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
engine sync-performance         refresh own-channel performance snapshot via vidiq
engine report <cycle_id>        re-render an existing cycle's HTML
```

---

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
│   ├── synthesizers/          ideas, outlines, polish, citation verify, title score
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

---

## Privacy + secrets

- `.env`, `channel.yaml`, and `data/*_perf_30d.json` are gitignored.
- Personal data (voice guide, performance snapshots, voice corpus, run logs,
  the SQLite DB itself) is gitignored — see [`.gitignore`](.gitignore) for the
  full list.
- A `gitleaks` pre-commit hook scans for accidentally-staged secrets if you
  install it: `pip install pre-commit && pre-commit install`.
- Without Composio + vidiq, **no signal data ever leaves your machine** —
  inference runs against local Ollama and all sources are unauthenticated
  public APIs.

---

## License

MIT — see [`LICENSE`](LICENSE).

---

## Why this exists

Build context: [`docs/how-it-works.html`](docs/how-it-works.html) walks
through the architecture for both technical and non-technical readers, using
the 2GuysTek YouTube channel as a concrete worked example.
