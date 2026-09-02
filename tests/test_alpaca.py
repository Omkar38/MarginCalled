"""Tests for the read-only Alpaca client.

Offline: no network is touched. Transport is exercised through the host guards,
and parsing through fixtures shaped like real Alpaca responses.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.alpaca import (  # noqa: E402
    AlpacaDataClient,
    AlpacaError,
    FeedMode,
    parse_occ_symbol,
)

CREDS = {"key": "PKTEST0000000000", "secret": "secret"}


def _client() -> AlpacaDataClient:
    return AlpacaDataClient(**CREDS)


# --------------------------------------------------------------------------
# OCC symbol parsing
# --------------------------------------------------------------------------


def test_parse_standard_symbol():
    underlying, expiry, right, strike = parse_occ_symbol("SPY261016C00640000")
    assert underlying == "SPY"
    assert expiry == date(2026, 10, 16)
    assert right == "C"
    assert strike == 640.0


def test_parse_put_and_fractional_strike():
    _, _, right, strike = parse_occ_symbol("SPY261120P00642500")
    assert right == "P"
    assert strike == 642.5


def test_parse_handles_long_and_short_underlyings():
    for symbol, expected in (
        ("A261016C00640000", "A"),
        ("GOOGL261016C00640000", "GOOGL"),
        ("SPXW261016C00640000", "SPXW"),
    ):
        assert parse_occ_symbol(symbol)[0] == expected


def test_parse_high_and_low_strikes():
    assert parse_occ_symbol("SPY261016C01000000")[3] == 1000.0
    assert parse_occ_symbol("SPY261016C00000500")[3] == 0.5


def test_parse_rejects_malformed():
    for bad in ("SPY", "", "SPY261016X00640000", "SPY26101AC00640000"):
        try:
            parse_occ_symbol(bad)
        except AlpacaError:
            continue
        raise AssertionError(f"should have rejected {bad!r}")


# --------------------------------------------------------------------------
# Credential guards
# --------------------------------------------------------------------------


def test_live_key_is_refused():
    try:
        AlpacaDataClient(key="AKLIVEKEY123", secret="x")
    except AlpacaError as exc:
        assert "live key" in str(exc).lower()
        return
    raise AssertionError("a live AK key must be refused")


def test_missing_credentials_refused():
    """Hermetic: empty arguments fall back to the environment, so the
    environment has to be cleared for this to test what it claims.

    Without this the test passes on a bare machine and fails wherever
    credentials happen to be exported - which is exactly what happened when the
    compliance report ran the suite as subprocesses after loading .env.
    """
    import os

    saved = {k: os.environ.pop(k, None)
             for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY",
                       "ALPACA_API_KEY", "ALPACA_SECRET_KEY")}
    try:
        AlpacaDataClient(key="", secret="")
    except AlpacaError as exc:
        assert "credential" in str(exc).lower()
        return
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    raise AssertionError("missing credentials must raise")


# --------------------------------------------------------------------------
# Host guards — the read-only contract
# --------------------------------------------------------------------------


def test_live_trading_host_is_refused():
    client = _client()
    try:
        client._get("https://api.alpaca.markets/v2/account")
    except AlpacaError as exc:
        assert "live trading host" in str(exc).lower()
        return
    raise AssertionError("the live host must be refused")


def test_unknown_host_is_refused():
    client = _client()
    for url in ("https://evil.example.com/v2/account", "https://google.com"):
        try:
            client._get(url)
        except AlpacaError as exc:
            assert "unknown host" in str(exc).lower()
            continue
        raise AssertionError(f"{url} should have been refused")


def test_module_has_no_order_submission_path():
    """The client must not contain anything that can place an order."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "tp2agent" / "alpaca.py"
    ).read_text()
    for forbidden in ('method="POST"', "method='POST'", "/v2/orders", "DELETE"):
        assert forbidden not in source, f"found {forbidden!r} in a read-only client"
    assert source.count('method="GET"') >= 1


# --------------------------------------------------------------------------
# Feed mode
# --------------------------------------------------------------------------


def test_feed_mode_labels():
    assert FeedMode("opra").is_live
    assert "LIVE" in FeedMode("opra").label
    assert not FeedMode("indicative").is_live
    assert "SHADOW" in FeedMode("indicative").label


# --------------------------------------------------------------------------
# Quote age
# --------------------------------------------------------------------------


def test_quote_age_from_timestamp():
    ts = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
    age = AlpacaDataClient.quote_age_seconds({"newest_quote_ts": ts})
    assert 40 <= age <= 60, age


def test_missing_timestamp_fails_closed():
    """No timestamp must fail the staleness gate, not silently pass it."""
    assert AlpacaDataClient.quote_age_seconds({}) == float("inf")
    assert AlpacaDataClient.quote_age_seconds({"newest_quote_ts": None}) == float("inf")
    assert AlpacaDataClient.quote_age_seconds({"newest_quote_ts": "garbage"}) == float("inf")


def test_nanosecond_timestamps_parse():
    """Alpaca returns nanosecond precision; datetime accepts at most microseconds."""
    ts = "2026-08-31T13:41:02.123456789Z"
    age = AlpacaDataClient.quote_age_seconds({"newest_quote_ts": ts})
    assert age != float("inf"), "nanosecond timestamp should parse"


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
