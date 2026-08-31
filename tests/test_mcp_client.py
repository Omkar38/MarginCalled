"""Tests for the MCP client.

The transport and guards are tested against a stub server so the suite stays
offline and fast. Tests requiring the real Alpaca MCP server and live
credentials are skipped automatically when either is absent.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.mcp_client import (  # noqa: E402
    READ_ONLY_TOOLSETS,
    AlpacaMCPClient,
    MCPError,
)

ROOT = Path(__file__).resolve().parents[1]

# A stub MCP server: speaks just enough JSON-RPC to exercise the client.
STUB = r'''
import json, sys
TOOLS = [
    {"name": "get_account_info"}, {"name": "get_all_positions"},
    {"name": "get_account_activities"}, {"name": "update_account_config"},
]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    if mid is None:
        continue
    method = msg.get("method")
    if method == "initialize":
        out = {"protocolVersion": "2024-11-05", "capabilities": {}}
    elif method == "tools/list":
        out = {"tools": TOOLS}
    elif method == "tools/call":
        name = msg["params"]["name"]
        out = {"content": [{"type": "text", "text": json.dumps({"tool": name})}]}
    else:
        print(json.dumps({"jsonrpc":"2.0","id":mid,
                          "error":{"code":-32601,"message":"no such method"}}),
              flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": out}), flush=True)
'''


def _stub_client(tmp: Path) -> AlpacaMCPClient:
    script = tmp / "stub_server.py"
    script.write_text(STUB)
    os.environ.setdefault("APCA_API_KEY_ID", "PKSTUB0000000000")
    os.environ.setdefault("APCA_API_SECRET_KEY", "stubsecret")
    return AlpacaMCPClient(server_cmd=[sys.executable, str(script)])


# --------------------------------------------------------------------------
# Credential guards
# --------------------------------------------------------------------------


def test_live_key_is_refused():
    saved = os.environ.get("APCA_API_KEY_ID")
    os.environ["APCA_API_KEY_ID"] = "AKLIVE123456"
    os.environ.setdefault("APCA_API_SECRET_KEY", "x")
    try:
        AlpacaMCPClient()
    except MCPError as exc:
        assert "live key" in str(exc).lower()
        return
    finally:
        if saved is not None:
            os.environ["APCA_API_KEY_ID"] = saved
    raise AssertionError("a live AK key must be refused")


def test_paper_mode_is_forced_in_the_child_env():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c = _stub_client(Path(d))
        assert c.env["ALPACA_PAPER_TRADE"] == "true"
        assert c.env["ALPACA_TOOLSETS"] == READ_ONLY_TOOLSETS
        assert "trading" not in c.env["ALPACA_TOOLSETS"]


def test_credentials_are_mapped_to_the_server_variable_names():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c = _stub_client(Path(d))
        assert c.env["ALPACA_API_KEY"] == os.environ["APCA_API_KEY_ID"]
        assert c.env["ALPACA_SECRET_KEY"] == os.environ["APCA_API_SECRET_KEY"]


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def test_handshake_and_tool_listing():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        with _stub_client(Path(d)) as c:
            tools = c.list_tools()
            assert {t["name"] for t in tools} >= {"get_account_info"}


def test_tool_call_returns_text_content():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        with _stub_client(Path(d)) as c:
            out = c.account()
            assert json.loads(out)["tool"] == "get_account_info"


def test_rpc_error_is_raised():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        with _stub_client(Path(d)) as c:
            try:
                c._rpc("no/such/method")
            except MCPError as exc:
                assert "no such method" in str(exc)
                return
    raise AssertionError("an RPC error must raise")


def test_missing_server_binary_is_reported_clearly():
    os.environ.setdefault("APCA_API_KEY_ID", "PKSTUB0000000000")
    os.environ.setdefault("APCA_API_SECRET_KEY", "stubsecret")
    c = AlpacaMCPClient(server_cmd=["/nonexistent/mcp-server-binary"])
    try:
        c.start()
    except MCPError as exc:
        assert "not found" in str(exc).lower()
        return
    raise AssertionError("a missing binary must raise MCPError")


# --------------------------------------------------------------------------
# The safety property
# --------------------------------------------------------------------------


def test_mutation_detection_catches_non_order_writes():
    """update_account_config cannot trade but still changes state."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        with _stub_client(Path(d)) as c:
            assert c.assert_no_order_tools() == []
            assert "update_account_config" in c.mutating_tools()


def test_real_server_loads_no_order_tools():
    """Against the real Alpaca MCP server, no order tool may be registered.

    Skipped when the server or credentials are unavailable.
    """
    binary = ROOT / ".venv" / "bin" / "alpaca-mcp-server"
    if not binary.exists():
        print("      (skipped: alpaca-mcp-server not installed)")
        return
    sys.path.insert(0, str(ROOT / "src"))
    from tp2agent.alpaca import _load_dotenv

    _load_dotenv()
    if not os.environ.get("APCA_API_KEY_ID", "").startswith("PK"):
        print("      (skipped: no paper credentials)")
        return
    with AlpacaMCPClient() as c:
        tools = c.list_tools()
        assert tools, "server loaded no tools"
        assert c.assert_no_order_tools() == [], "an order tool is exposed"


def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
