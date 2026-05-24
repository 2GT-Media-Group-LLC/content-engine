# Security notes

## May 2026 — Composio breach + migration to VidIQ MCP

In May 2026 Composio.dev was compromised. The engine had used Composio as the
OAuth aggregator for two YouTube tools (`YOUTUBE_LIST_CHANNEL_VIDEOS` for
peer/own-channel uploads and `VIDIQ_SCORE_TITLE` for title CTR scoring). At
that time the engine cached a `{toolkit → connected_account_id}` mapping at
`~/.cache/content-engine/composio_connected.json` for resilience against
Composio's flaky `connected_accounts` endpoint.

### What was done

1. **Migrated the default YouTube data provider to VidIQ MCP**
   (`https://mcp.vidiq.com/mcp`), called directly from Python via the small
   client in `src/content_engine/clients/mcp_http.py`. Bearer-auth with one
   API key. No third-party OAuth broker in the path.
2. **Kept Composio as an optional fallback** behind `YOUTUBE_PROVIDER=composio`,
   so users without a VidIQ subscription still have a YouTube path.
3. **Added `engine composio-disconnect`** to scrub the local credential cache
   in one command and print the manual steps to revoke server-side access.

### Recommended cleanup for any user who had Composio set up

```bash
.venv/bin/engine composio-disconnect
```

This removes `~/.cache/content-engine/composio_connected.json` and prints
the four manual steps:

1. Sign in at <https://app.composio.dev>
2. Settings → Connected Accounts → disconnect YouTube
3. Settings → API Keys → delete the key your engine was using
4. Remove `COMPOSIO_API_KEY=…` from this project's `.env`

After that, set `YOUTUBE_PROVIDER=vidiq` and `VIDIQ_API_KEY=…` in `.env`.

### Why the engine still keeps Composio code around

Provider abstraction in `src/content_engine/collectors/yt_provider.py` makes
the composio path essentially free to maintain — it's about 120 lines of
adapter code that lets a future user opt back in without forking the repo.
If you want to remove it entirely, delete:

- `src/content_engine/collectors/composio_client.py`
- `src/content_engine/collectors/composio_provider.py`
- The `"composio"` branch in `yt_provider._instantiate()`
- `composio_core` from `pyproject.toml` dependencies

and the engine still runs.

### Threat model going forward

- **VidIQ API key** lives only in `.env` (gitignored) and is sent only to
  `mcp.vidiq.com`. Rotate via VidIQ's app if exposed.
- **No other third-party broker** holds engine credentials. Reddit, Hacker
  News, RSS, and GitHub-releases collectors are all unauthenticated.
- **Local-only LLM inference** — Ollama doesn't egress.
- **Pre-commit hook** (`gitleaks` if installed) catches accidentally-staged
  secrets. See `.gitignore` for the full list of paths kept out of git.
