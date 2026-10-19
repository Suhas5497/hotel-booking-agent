"""Minimal MCP client. The agent only ever talks to tools through this JSON-RPC
interface, so swapping the in-process server for a remote/stdio one is a one-line change."""
from __future__ import annotations

import itertools
import json
import subprocess
import sys
import threading

from .mcp_server import PROTOCOL_VERSION, MCPServer


class InProcessTransport:
    def __init__(self, server: MCPServer):
        self.server = server

    def request(self, message: dict) -> dict | None:
        # Round-trip through JSON so in-process behaves exactly like the wire.
        return self.server.handle(json.loads(json.dumps(message)))


class StdioTransport:
    """Spawns `python -m goodfoods.mcp_server` and speaks newline-delimited JSON-RPC."""

    def __init__(self, command: list[str] | None = None):
        self.proc = subprocess.Popen(command or [sys.executable, "-m", "goodfoods.mcp_server"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self.lock = threading.Lock()

    def request(self, message: dict) -> dict | None:
        with self.lock:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
            if "id" not in message:
                return None
            line = self.proc.stdout.readline()
            if not line:
                raise ConnectionError("MCP server closed the connection")
            return json.loads(line)

    def close(self) -> None:
        self.proc.terminate()


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, transport):
        self.transport = transport
        self._ids = itertools.count(1)
        self.server_info: dict = {}
        self._tools: list[dict] | None = None

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        response = self.transport.request({"jsonrpc": "2.0", "id": next(self._ids), "method": method,
                                           "params": params or {}})
        if response is None:
            raise MCPError(f"No response to {method}")
        if "error" in response:
            raise MCPError(f"{response['error'].get('code')}: {response['error'].get('message')}")
        return response["result"]

    def initialize(self) -> "MCPClient":
        result = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": "goodfoods-agent", "version": "1.0.0"}})
        self.server_info = result.get("serverInfo", {})
        self.transport.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def list_tools(self) -> list[dict]:
        if self._tools is None:
            self._tools = self._rpc("tools/list")["tools"]
        return self._tools

    def call_tool(self, name: str, arguments: dict) -> tuple[dict, bool]:
        """Returns (payload, is_error). Protocol-level failures surface as a payload too,
        so the agent loop never crashes on a bad tool call."""
        try:
            result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        except MCPError as exc:
            return {"error": "TOOL_CALL_FAILED", "message": str(exc)}, True
        payload = result.get("structuredContent")
        if payload is None:
            text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"text": text}
        return payload, bool(result.get("isError"))
