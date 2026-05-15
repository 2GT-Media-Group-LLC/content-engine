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
- [Optional integrations: Composio + vidiq](#optional-integrations-composio--vidiq)
  - [What is Composio? Do I need it?](#what-is-composio-do-i-need-it)
  - [What is vidiq? Do I need it?](#what-is-vidiq-do-i-need-it)
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

`setup.sh` scaffolds an `.env` file. The only variable you might need to
change up front:

```dotenv
# Where Ollama listens. Default is fine if Ollama is local.
OLLAMA_HOST=http://localhost:11434

# OPTIONAL — only needed for YouTube auto-collection or vidiq title scoring.
# See "Optional integrations" below before adding this.
# COMPOSIO_API_KEY=ak_...
```

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
| YouTube (peers + own) | `channel.yaml` → `youtube` | **Composio + Google OAuth** (optional — see below) |

You can run the engine end-to-end with **zero auth**: drop the YouTube
collector and the engine will happily ideate from the four no-auth sources.

---

## Optional integrations: Composio + vidiq

These two third-party services are **completely optional**. The engine works
without either. Add them only if you want the specific capabilities each one
unlocks.

### What is Composio? Do I need it?

**[Composio](https://composio.dev/)** is a managed-OAuth aggregator. Instead
of you wiring up a Google Cloud project, an OAuth consent screen, and a
refresh-token-handling auth flow just to call the YouTube Data API, you:

1. Create a free Composio account at <https://app.composio.dev>
2. Click "Connect" on the YouTube tile and grant consent in the browser
3. Copy your API key from **Settings → API Keys**

Composio then exposes YouTube (and 200+ other services) as one Python SDK call:
`client.tools.execute("YOUTUBE_LIST_CHANNEL_VIDEOS", arguments={...})`. It
holds the OAuth tokens, refreshes them, and the engine never sees your Google
credentials.

**What you get if you set `COMPOSIO_API_KEY`:**
- The `youtube` collector pulls recent uploads from your own channel + the
  peer channels you listed in `channel.yaml`. This is the fastest way to know
  "what topics is the niche actively covering" so the engine can flag fatigue.
- The `title_scorer` can call vidiq's `VIDIQ_SCORE_TITLE` action through the
  Composio SDK (see next section).

**What you give up by skipping it:**
- The YouTube collector logs a warning and skips. You still get Reddit, HN,
  RSS, and GitHub signal — typically 80%+ of weekly volume.
- Title scoring falls back to a deterministic heuristic (length, numbers,
  parentheticals, contrarian language, etc.). Cheaper, but less calibrated
  than vidiq's CTR model.

**Cost:** Composio has a generous free tier — fine for a single-channel weekly
cron. No credit card required to sign up.

**Skip Composio if:** you want a fully zero-auth setup, you're fine without
peer-channel signal, or you already have your own YouTube Data API wiring.

### What is vidiq? Do I need it?

**[vidiq](https://vidiq.com/)** is a third-party YouTube analytics + creator
tooling service. The engine uses exactly two things from it, both via
Composio:

1. **`VIDIQ_SCORE_TITLE`** — vidiq's CTR-prediction model returns a 0-100
   score for a candidate title. The title scorer ranks each idea's
   `suggested_titles` so the brief surfaces the strongest one first.
2. **Channel performance snapshot** — `engine sync-performance` pulls your
   own-channel metrics (last 30 days of views, AVD, subs gained per video)
   and writes them to `data/<brand_short>_perf_30d.json`. The idea
   synthesizer reads that file so it can weight new ideas toward
   formats/topics that are actually performing on **your** channel.

**What you give up by skipping it:**
- Title scoring uses the heuristic backend instead of vidiq. The heuristic
  catches the obvious clickability levers (length sweet spot 35-65 chars,
  numbers, year tags, parentheticals, contrarian language, first-person
  framing, ALL-CAPS penalty, "ultimate guide" penalty, clickbait-phrase
  penalty). It's deterministic, runs offline, and costs zero credits.
- The synthesizer can't weight ideas by your channel's actual recent
  performance. Ideas are still generated — they just don't know that, e.g.,
  your last three Proxmox videos overperformed.

**Cost:** vidiq is a paid SaaS. Title scoring costs ~5 credits per call. A
weekly cycle ranks ~15 titles, so ~75 credits/week. The free tier covers a
handful of cycles for evaluation.

**Skip vidiq if:** you don't want a paid third-party dependency, you're early
on your channel and don't have meaningful performance data yet, or the
heuristic title score is good enough for your workflow.

**TL;DR setup matrix:**

| Setup | What you can do |
|---|---|
| No Composio, no vidiq | Reddit + HN + RSS + GitHub. Heuristic titles. Generic perf context. **Default.** |
| Composio only | All of the above + YouTube peer-channel + own-channel signal. Heuristic titles. |
| Composio + vidiq | All of the above + vidiq-scored titles + perf-weighted idea synthesis. **Most powerful.** |

---

## Configuration files

| File | Purpose | Tracked? |
|---|---|---|
| `channel.yaml` | brand, niche, audience, peers, subreddits, HN queries | gitignored |
| `channel.example.yaml` | template + documented defaults | tracked |
| `.env` | `COMPOSIO_API_KEY`, `OLLAMA_HOST` | gitignored |
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
