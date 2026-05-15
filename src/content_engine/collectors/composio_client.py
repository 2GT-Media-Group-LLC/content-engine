"""Thin Composio Python SDK wrapper. Returns a usable client when COMPOSIO_API_KEY
is set; otherwise returns a NoopClient that logs but doesn't fetch — so the engine
can run end-to-end on local data even before the SDK is wired."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("engine.composio")

# Where to cache the last-known-good {toolkit -> connected_account_id} mapping.
# Lets us survive transient HTTP 500s from Composio's connectedAccounts endpoint
# without falling all the way through to "<toolkit>_not_authorized" for the run.
_CACHE_PATH = Path.home() / ".cache" / "content-engine" / "composio_connected.json"


class NoopComposio:
    """Stub used when no API key is configured. Every call logs and returns []."""
    def __init__(self):
        log.warning("COMPOSIO_API_KEY not set — collectors will run in stub mode. "
                    "No remote data will be fetched. Set the env var to enable.")

    def execute(self, slug: str, arguments: dict | None = None,
                account: str | None = None) -> dict:
        log.info("[STUB] would call %s(%s)", slug, arguments)
        return {"_stub": True, "data": {}}


def get_client() -> Any:
    """Return a Composio client (real or stub). The real client lazy-imports the
    composio_core package so we don't error on missing optional dep."""
    api_key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not api_key:
        return NoopComposio()
    try:
        from composio import Composio  # type: ignore
        from composio.client.enums import Action  # type: ignore
        os.environ["COMPOSIO_API_KEY"] = api_key
        client = Composio()

        # Eagerly index connected accounts by toolkit name so we can attach
        # the right connected_account to auth-requiring action calls. Composio
        # v3 doesn't auto-select them — passing nothing yields:
        #   InvalidParams: `connected_account` cannot be `None`
        accounts_by_toolkit: dict[str, str] = {}
        last_err: Exception | None = None
        # Composio's connectedAccounts endpoint occasionally returns HTTP 500
        # transiently. Retry with exponential backoff before giving up.
        for attempt in range(3):
            try:
                for a in (client.connected_accounts.get() or []):
                    # Accept either snake or camel from the SDK.
                    app = (getattr(a, "appUniqueId", None)
                           or getattr(a, "appName", None)
                           or getattr(a, "app_unique_id", None))
                    aid = getattr(a, "id", None)
                    status = (getattr(a, "status", "") or "").upper()
                    if app and aid and status == "ACTIVE":
                        accounts_by_toolkit[app.lower()] = aid
                last_err = None
                break
            except Exception as e:
                last_err = e
                wait = 1.5 ** attempt  # 1s, 1.5s, 2.25s
                log.info("connected_accounts.get attempt %d/3 failed (%s); "
                         "retry in %.1fs", attempt + 1, e, wait)
                time.sleep(wait)
        if last_err is not None:
            # Fall back to the on-disk cache from a prior successful run so
            # one bad upstream call doesn't disable every auth'd toolkit.
            log.warning("could not list Composio connected accounts after 3 "
                        "attempts: %s", last_err)
            try:
                if _CACHE_PATH.exists():
                    cached = json.loads(_CACHE_PATH.read_text())
                    if isinstance(cached, dict) and cached:
                        accounts_by_toolkit = {k: v for k, v in cached.items()
                                                if isinstance(v, str)}
                        log.warning("using cached toolkit mapping from %s "
                                    "(%d entries)",
                                    _CACHE_PATH, len(accounts_by_toolkit))
            except Exception as ce:
                log.warning("could not load cached toolkit mapping: %s", ce)
        else:
            # Persist the fresh mapping for the next run's fallback.
            try:
                _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                _CACHE_PATH.write_text(json.dumps(accounts_by_toolkit, indent=2))
            except Exception as ce:
                log.debug("could not write toolkit cache: %s", ce)
        log.info("composio toolkits connected: %s",
                 sorted(accounts_by_toolkit.keys()) or "(none)")

        class _Wrap:
            connected: dict[str, str] = accounts_by_toolkit  # noqa: F841

            def execute(self, slug: str, arguments=None, account=None) -> dict:
                toolkit = slug.split("_", 1)[0].lower() if "_" in slug else slug.lower()
                kwargs: dict = {
                    "action": Action(slug),
                    "params": arguments or {},
                    "entity_id": "default",
                }
                # Auto-attach the connected account for this toolkit if we have one.
                acct_id = accounts_by_toolkit.get(toolkit)
                if acct_id:
                    kwargs["connected_account"] = acct_id
                resp = client.actions.execute(**kwargs)
                return resp if isinstance(resp, dict) else {"data": resp}
        return _Wrap()
    except ImportError:
        log.error("composio package not installed. Run: pip install composio_core")
        return NoopComposio()
    except Exception as e:
        log.error("Composio client init failed: %s", e)
        return NoopComposio()
