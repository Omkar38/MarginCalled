"""Minimal MCP stdio client for Alpaca's official MCP server.

The competition requires the Alpaca Trading API *plus* either the MCP server or
the CLI. This provides the MCP half.

Scope is deliberate and narrow: the MCP server is used for **reads only** -
account, positions, activities, portfolio history. Orders continue to go through
the deterministic REST path in the execution module, behind risk.evaluate.

That is not a shortcut. Routing risk-critical orders through an LLM tool-calling
loop puts a probabilistic component between a risk decision and a broker, and
nothing about this strategy needs that. The MCP surface is what the narrator
reads to explain what happened; the theorem and the gates decide what happens.

The server is spawned with ALPACA_TOOLSETS restricted, so it is not merely
convention that orders do not flow through here - the tools are not loaded.

No third-party dependency: MCP is JSON-RPC 2.0 over newline-delimited JSON on
stdio, which the standard library handles.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

__all__ = ["MCPError", "AlpacaMCPClient", "READ_ONLY_TOOLSETS"]

# Toolsets loaded on the server. "trading" is deliberately absent, so the
# order-placement tools are never registered in the first place.
READ_ONLY_TOOLSETS = "account,assets,market_info"

PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    pass


class AlpacaMCPClient:
    """Speaks JSON-RPC to a locally spawned Alpaca MCP server over stdio."""

    def __init__(
        self,
        server_cmd: list[str] | None = None,
        toolsets: str = READ_ONLY_TOOLSETS,
        paper: bool = True,
        timeout: float = 60.0,
    ) -> None:
        self.timeout = timeout
        self._id = 0
        self._proc: subprocess.Popen | None = None
        self._stderr: list[str] = []

        key = os.environ.get("APCA_API_KEY_ID", "").strip()
        secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
        if not key or not secret:
            raise MCPError(
                "No credentials. The MCP server reads ALPACA_API_KEY / "
                "ALPACA_SECRET_KEY, which are populated here from "
                "APCA_API_KEY_ID / APCA_API_SECRET_KEY."
            )
        if key.startswith("AK"):
            raise MCPError("REFUSED: live key (AK...). This project is paper-only.")

        self.env = {
            **os.environ,
            "ALPACA_API_KEY": key,
            "ALPACA_SECRET_KEY": secret,
            "ALPACA_PAPER_TRADE": "true" if paper else "false",
            "ALPACA_TOOLSETS": toolsets,
        }
        self.server_cmd = server_cmd or self._default_cmd()

    @staticmethod
    def _default_cmd() -> list[str]:
        """Prefer the project venv, so the server is pinned with the project."""
        root = Path(__file__).resolve().parents[2]
        venv = root / ".venv" / "bin" / "alpaca-mcp-server"
        if venv.exists():
            return [str(venv)]
        venv_py = root / ".venv" / "bin" / "python"
        if venv_py.exists():
            return [str(venv_py), "-m", "alpaca_mcp_server"]
        return [sys.executable, "-m", "alpaca_mcp_server"]

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "AlpacaMCPClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.server_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise MCPError(
                f"MCP server not found: {' '.join(self.server_cmd)}\n"
                f"Install with: .venv/bin/pip install alpaca-mcp-server"
            ) from exc

        # Drain stderr so a chatty server cannot deadlock on a full pipe.
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tp2-agent", "version": "1.0"},
            },
        )
        self._notify("notifications/initialized")

    def _drain_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for line in self._proc.stderr:
            self._stderr.append(line.rstrip())
            if len(self._stderr) > 200:
                del self._stderr[:100]

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self._proc.kill()
        finally:
            self._proc = None

    # -- transport ---------------------------------------------------------

    def _write(self, payload: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise MCPError("server is not running")
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        if not self._proc or not self._proc.stdout:
            raise MCPError("server is not running")
        self._id += 1
        req_id = self._id
        self._write(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        )

        while True:
            line = self._proc.stdout.readline()
            if not line:
                tail = "\n".join(self._stderr[-12:])
                raise MCPError(f"server closed the connection.\n{tail}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # server log noise on stdout
            if msg.get("id") != req_id:
                continue  # a notification or another response
            if "error" in msg:
                raise MCPError(f"{method}: {msg['error']}")
            return msg.get("result")

    # -- API ---------------------------------------------------------------

    def list_tools(self) -> list[dict]:
        return (self._rpc("tools/list") or {}).get("tools", [])

    def call(self, name: str, arguments: dict | None = None) -> str:
        """Invoke a tool and return its text content."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        chunks = []
        for item in (result or {}).get("content", []):
            if item.get("type") == "text":
                chunks.append(item.get("text", ""))
        return "\n".join(chunks)

    # Convenience wrappers for the reads the narrator needs.

    def account(self) -> str:
        return self.call("get_account_info")

    def positions(self) -> str:
        return self.call("get_all_positions")

    def activities(self, activity_type: str | None = None) -> str:
        args = {"activity_type": activity_type} if activity_type else {}
        return self.call("get_account_activities", args)

    def portfolio_history(self, period: str = "1D", timeframe: str = "5Min") -> str:
        return self.call(
            "get_portfolio_history", {"period": period, "timeframe": timeframe}
        )

    # Any tool whose name suggests it changes state. "order" words cover
    # execution; the mutation words catch things like update_account_config,
    # which cannot trade but can still alter the account.
    MUTATING_WORDS = (
        "place", "order", "buy", "sell", "close", "liquidate", "exercise",
        "update", "set_", "delete", "cancel", "create", "modify", "replace",
    )

    def mutating_tools(self) -> list[str]:
        """Tool names in the loaded set that could change account state.

        Empty is the goal: it makes "this surface is read-only" a property of
        the running process rather than a promise in a write-up.
        """
        found = []
        for tool in self.list_tools():
            name = (tool.get("name") or "").lower()
            if any(word in name for word in self.MUTATING_WORDS):
                found.append(tool.get("name"))
        return sorted(found)

    def assert_no_order_tools(self) -> list[str]:
        """Order-placement tools specifically. Kept separate from mutation."""
        risky = ("place", "order", "buy", "sell", "close", "liquidate", "exercise")
        found = []
        for tool in self.list_tools():
            name = (tool.get("name") or "").lower()
            if any(word in name for word in risky):
                found.append(tool.get("name"))
        return sorted(found)
