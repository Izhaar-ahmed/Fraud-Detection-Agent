"""Synchronous client for the tigergraph-mcp server over stdio.

The server (pip package tigergraph-mcp, console script `tigergraph-mcp`)
runs as a child process for the lifetime of the client. A background thread
owns the asyncio loop and the mcp ClientSession; `call()` blocks until the
tool returns.

Server responses come from tigergraph_mcp.response_formatter.format_response:
one TextContent whose text starts with a fenced ```json block holding
{"success", "operation", "summary", "data", "error", ...}. `call()` returns
that dict's `data` (or raises McpToolError when success is false).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from typing import Any


class McpToolError(RuntimeError):
    pass


def parse_tool_text(text: str) -> dict:
    """Extract the structured JSON envelope from a tigergraph-mcp tool reply."""
    start = text.find("```json")
    body = text[start + len("```json"):].lstrip() if start >= 0 else text.lstrip()
    envelope, _ = json.JSONDecoder().raw_decode(body)
    return envelope


class McpClient:
    def __init__(self, env: dict[str, str], command: str = "tigergraph-mcp", args: list[str] | None = None,
                 timeout_s: float = 300.0):
        exe = shutil.which(command)
        if exe is None:
            raise FileNotFoundError(f"{command} not found on PATH (pip install tigergraph-mcp)")
        self._exe, self._args = exe, list(args or [])
        self._env = {**os.environ, **env}
        self._timeout = timeout_s
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._session = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="tigergraph-mcp", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=60):
            raise TimeoutError("tigergraph-mcp did not start within 60s")
        if self._error is not None:
            raise RuntimeError(f"tigergraph-mcp failed to start: {self._error}") from self._error

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._stop = asyncio.Event()
        params = StdioServerParameters(command=self._exe, args=self._args, env=self._env)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as exc:  # surfaced to the constructor or the next call
            self._error = exc
            self._ready.set()

    def list_tools(self) -> list[str]:
        async def go():
            res = await self._session.list_tools()
            return [t.name for t in res.tools]
        return asyncio.run_coroutine_threadsafe(go(), self._loop).result(self._timeout)

    def call_raw(self, tool: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise RuntimeError(f"MCP session not available: {self._error}")

        async def go():
            return await self._session.call_tool(tool, arguments)

        res = asyncio.run_coroutine_threadsafe(go(), self._loop).result(self._timeout)
        return "\n".join(getattr(c, "text", "") for c in res.content)

    def call(self, tool: str, arguments: dict[str, Any]) -> dict:
        text = self.call_raw(tool, arguments)
        try:
            env = parse_tool_text(text)
        except ValueError as exc:
            raise McpToolError(f"{tool}: unparseable reply: {text[:500]}") from exc
        if not env.get("success", False):
            raise McpToolError(f"{tool}: {env.get('error') or env.get('summary') or text[:500]}")
        return env.get("data") or {}

    def close(self) -> None:
        if self._stop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=10)
