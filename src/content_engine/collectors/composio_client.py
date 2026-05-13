"""Thin Composio Python SDK wrapper. Returns a usable client when COMPOSIO_API_KEY
is set; otherwise returns a NoopClient that logs but doesn't fetch — so the engine
can run end-to-end on local data even before the SDK is wired."""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("engine.composio")


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
        except Exception as e:
            log.warning("could not list Composio connected accounts: %s", e)
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
