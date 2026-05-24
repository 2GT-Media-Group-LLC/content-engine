"""Minimal MCP (Model Context Protocol) client over streamable HTTP.

We don't need a full MCP SDK — the spec is JSON-RPC 2.0 over HTTP with two
methods we care about: `tools/list` and `tools/call`. This module wraps both
in a tight client that survives transient failures via tenacity retries.

Designed for VidIQ's https://mcp.vidiq.com/mcp endpoint but provider-agnostic;
any MCP server that speaks streamable HTTP + Bearer auth will work.
"""
from __future__ import annotations

import itertools
import json
import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger("engine.mcp")


class MCPError(RuntimeError):
    """JSON-RPC error returned by the MCP server (non-transient — won't retry)."""


class MCPTransportError(RuntimeError):
    """Transport-level failure (5xx, timeouts, connection reset) — retryable."""


class MCPClient:
    """Tiny streamable-HTTP MCP client.

    Usage:
        c = MCPClient("https://mcp.vidiq.com/mcp", api_key)
        balance = c.call_tool("vidiq_balance", {})
        videos = c.call_tool("vidiq_channel_videos",
                              {"channelId": "@MrBeast", "videoFormat": "long"})
    """

    def __init__(self, endpoint: str, auth_token: str,
                 *, timeout: float = 60.0, user_agent: str = "content-engine/0.1"):
        self.endpoint = endpoint.rstrip("/")
        self._auth_token = auth_token
        self._timeout = timeout
        self._id_counter = itertools.count(1)
        self._headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
            # MCP streamable-HTTP servers commonly require both content-types in Accept.
            "Accept": "application/json, text/event-stream",
            "User-Agent": user_agent,
        }

    # ─── Public ──────────────────────────────────────────────────────────────
    def list_tools(self) -> list[dict]:
        """Return the server's tool catalog: [{name, description, inputSchema}]."""
        resp = self._rpc("tools/list", {})
        return list((resp or {}).get("tools") or [])

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """Call an MCP tool. Returns the parsed JSON of the first content block,
        falling back to the raw text if not JSON-decodable.

        MCP `tools/call` result shape:
            { "content": [{ "type": "text", "text": "..." }], "isError": bool }
        Most servers (including VidIQ) put a JSON blob inside the text block.
        """
        resp = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(resp, dict):
            return resp
        if resp.get("isError"):
            # Tool returned an error envelope. Surface it as MCPError (non-retried).
            blocks = resp.get("content") or []
            text = blocks[0].get("text") if blocks else str(resp)
            raise MCPError(f"{name} returned isError=true: {text[:300]}")
        blocks = resp.get("content") or []
        if not blocks:
            return resp
        first = blocks[0]
        if first.get("type") == "text" and isinstance(first.get("text"), str):
            text = first["text"]
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return first

    # ─── Internals ───────────────────────────────────────────────────────────
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type((MCPTransportError, httpx.HTTPError)),
        reraise=True,
    )
    def _rpc(self, method: str, params: dict) -> Any:
        """Single JSON-RPC 2.0 request/response. Retries transport errors only."""
        req_id = next(self._id_counter)
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        with httpx.Client(timeout=self._timeout) as cli:
            r = cli.post(self.endpoint, headers=self._headers, json=payload)
            # 5xx and 429 are transient; raise so tenacity retries.
            if r.status_code >= 500 or r.status_code == 429:
                raise MCPTransportError(
                    f"MCP {method} HTTP {r.status_code}: {r.text[:300]}"
                )
            # 4xx (other than 429) is a hard error — bad auth, bad request, etc.
            if r.status_code >= 400:
                raise MCPError(f"MCP {method} HTTP {r.status_code}: {r.text[:300]}")
            # Streamable HTTP can reply as application/json (single response)
            # or as text/event-stream (one or more "data:" framed messages).
            ctype = r.headers.get("content-type", "").lower()
            body = _parse_response_body(r.text, ctype)
            if "error" in body:
                err = body["error"]
                raise MCPError(
                    f"MCP {method} JSON-RPC error {err.get('code')}: "
                    f"{err.get('message')}"
                )
            return body.get("result")


def _parse_response_body(text: str, content_type: str) -> dict:
    """MCP streamable HTTP may return text/event-stream framed responses even
    for unary requests. Extract the first JSON-RPC message regardless."""
    text = text.strip()
    if not text:
        return {}
    if "event-stream" in content_type or text.startswith("event:") or text.startswith("data:"):
        # Pull the first "data: <json>" line.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload and payload != "[DONE]":
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        log.warning("MCP SSE response had no parseable data frame: %.200s", text)
        return {}
    # Plain JSON.
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise MCPError(f"MCP response not JSON: {e} — body: {text[:300]}")
