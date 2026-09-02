"""Read-only Alpaca market-data client.

Fetches the SPY option chain and converts it into the `ChainSnapshot` that
`rectangles.build_rectangles` consumes. Both calls and puts are retrieved: the
parity-implied forward needs put quotes, and every downstream quantity depends on
that forward.

SAFETY. This module is read-only by construction. `_get` issues GET exclusively,
refuses the live trading host outright, and refuses any host outside the paper
trading and market-data domains. There is no order-submission code path here;
execution lives elsewhere and goes through risk.evaluate first.
"""

from __future__ import annotations

import json
import math
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .rectangles import ChainSnapshot, OptionQuote, Quote

__all__ = [
    "AlpacaError",
    "FeedMode",
    "parse_occ_symbol",
    "AlpacaDataClient",
]

TRADING_HOST = "https://paper-api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"
LIVE_HOST = "https://api.alpaca.markets"

_CTX = ssl.create_default_context()
DEFAULT_TIMEOUT = 30


class AlpacaError(RuntimeError):
    pass


def _load_dotenv() -> None:
    """Load .env from the project root without overriding a real env var.

    Standard library only, and the file is gitignored so it never leaves this
    machine.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            if name and name not in os.environ:
                os.environ[name] = value.strip().strip("'\"")


@dataclass(frozen=True)
class FeedMode:
    """Which options feed this account can actually read."""

    feed: str  # "opra" or "indicative"

    @property
    def is_live(self) -> bool:
        return self.feed == "opra"

    @property
    def label(self) -> str:
        return "LIVE (OPRA)" if self.is_live else "SHADOW (indicative, 15-min delayed)"


def parse_occ_symbol(symbol: str) -> tuple[str, date, str, float]:
    """Parse an OCC option symbol.

    ``SPY261016C00640000`` -> ("SPY", date(2026, 10, 16), "C", 640.0)

    The layout is fixed-width from the right: 8 digits of strike in thousandths,
    one character for the right, six for the date. Everything before that is the
    underlying, so tickers of any length parse correctly.
    """
    if len(symbol) < 16:
        raise AlpacaError(f"not an OCC symbol: {symbol!r}")
    strike_part = symbol[-8:]
    right = symbol[-9]
    date_part = symbol[-15:-9]
    underlying = symbol[:-15]

    if right not in ("C", "P"):
        raise AlpacaError(f"bad right {right!r} in {symbol!r}")
    try:
        expiry = datetime.strptime(date_part, "%y%m%d").date()
        strike = int(strike_part) / 1000.0
    except ValueError as exc:
        raise AlpacaError(f"cannot parse {symbol!r}: {exc}") from exc
    return underlying, expiry, right, strike


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    # Alpaca returns nanosecond precision; datetime handles at most microseconds.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())[:6].ljust(6, "0")
        offset = tail[len(digits) :] if len(tail) > len(digits) else ""
        for marker in ("+", "-"):
            idx = tail.find(marker)
            if idx != -1:
                offset = tail[idx:]
                break
        text = f"{head}.{digits}{offset or '+00:00'}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class AlpacaDataClient:
    """Read-only client. Credentials come from the environment only."""

    def __init__(self, key: str | None = None, secret: str | None = None) -> None:
        if key is None and secret is None:
            _load_dotenv()
        self.key = (key or os.environ.get("APCA_API_KEY_ID", "")).strip()
        self.secret = (secret or os.environ.get("APCA_API_SECRET_KEY", "")).strip()
        if not self.key or not self.secret:
            raise AlpacaError(
                "No credentials. Put them in .env (gitignored) or export "
                "APCA_API_KEY_ID and APCA_API_SECRET_KEY."
            )
        if self.key.startswith("AK"):
            raise AlpacaError(
                "REFUSED: live key (AK...). This project is paper-only; use PK keys."
            )

    # -- transport ---------------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> tuple[int, dict]:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        if url.startswith(LIVE_HOST):
            raise AlpacaError(f"REFUSED: live trading host in {url}")
        if not (url.startswith(TRADING_HOST) or url.startswith(DATA_HOST)):
            raise AlpacaError(f"REFUSED: unknown host in {url}")

        req = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID": self.key,
                "APCA-API-SECRET-KEY": self.secret,
            },
            method="GET",
        )
        assert req.get_method() == "GET", "client must be read-only"
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT, context=_CTX) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"raw": body[:400]}
        except urllib.error.URLError as exc:
            raise AlpacaError(f"network error: {exc.reason}") from exc

    # -- account and feed --------------------------------------------------

    def account(self) -> dict:
        status, data = self._get(f"{TRADING_HOST}/v2/account")
        if status != 200:
            raise AlpacaError(f"account failed ({status}): {data}")
        return data

    def resolve_feed(self, underlying: str = "SPY") -> FeedMode:
        """Determine the best feed this account can read. Fails closed to shadow."""
        for feed in ("opra", "indicative"):
            status, data = self._get(
                f"{DATA_HOST}/v1beta1/options/snapshots/{underlying}",
                {"feed": feed, "limit": 1},
            )
            if status == 200 and data.get("snapshots"):
                return FeedMode(feed)
        raise AlpacaError("no options feed available on this account")

    def spot(self, symbol: str = "SPY") -> float:
        """Reference underlying price.

        Used only to bound the moneyness band when selecting strikes; the forward
        itself comes from put-call parity, so an approximate spot is sufficient.
        """
        for feed in ("sip", "iex"):
            status, data = self._get(
                f"{DATA_HOST}/v2/stocks/{symbol}/snapshot", {"feed": feed}
            )
            if status != 200:
                continue
            for key in ("latestTrade", "latestQuote", "dailyBar", "prevDailyBar"):
                node = data.get(key) or {}
                for field in ("p", "c"):
                    if node.get(field):
                        return float(node[field])
                bid, ask = node.get("bp"), node.get("ap")
                if bid and ask:
                    return 0.5 * (float(bid) + float(ask))
        raise AlpacaError(f"could not determine spot for {symbol}")

    def discover_expiries(
        self, underlying: str, min_dte: int = 30, max_dte: int = 150
    ) -> list[date]:
        """Distinct call expiries within a DTE window, following pagination.

        The contracts endpoint pages, and the first page is all near-dated, so a
        single unpaginated call silently returns only the front of the calendar.
        """
        today = date.today()
        seen: set[date] = set()
        token: str | None = None
        while True:
            params = {
                "underlying_symbols": underlying,
                "type": "call",
                "limit": 10_000,
                "expiration_date_gte": (today).isoformat(),
            }
            if token:
                params["page_token"] = token
            status, data = self._get(f"{TRADING_HOST}/v2/options/contracts", params)
            if status != 200:
                raise AlpacaError(f"contracts failed ({status}): {str(data)[:200]}")
            for row in data.get("option_contracts") or []:
                try:
                    expiry = datetime.strptime(row["expiration_date"], "%Y-%m-%d").date()
                except (KeyError, ValueError):
                    continue
                if min_dte <= (expiry - today).days <= max_dte:
                    seen.add(expiry)
            token = data.get("next_page_token")
            if not token:
                break
        return sorted(seen)

    def dividends(
        self, symbol: str, start: date, end: date
    ) -> list[tuple[date, float, date]]:
        """Announced cash dividends: (ex_date, amount, announced_on).

        Sourced from Alpaca's corporate-actions endpoint, which is authoritative
        for what is actually declared. `announced_on` is approximated by the
        process date when present; absent that, the ex-date is used, which is the
        conservative choice since it cannot make an amount look known earlier
        than it was.
        """
        status, data = self._get(
            f"{DATA_HOST}/v1/corporate-actions",
            {
                "symbols": symbol,
                "types": "cash_dividend",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 100,
            },
        )
        if status != 200:
            raise AlpacaError(f"corporate actions failed ({status}): {str(data)[:200]}")
        out: list[tuple[date, float, date]] = []
        for row in (data.get("corporate_actions") or {}).get("cash_dividends", []):
            try:
                ex = datetime.strptime(row["ex_date"], "%Y-%m-%d").date()
                rate = float(row["rate"])
            except (KeyError, ValueError):
                continue
            declared_raw = row.get("process_date") or row.get("ex_date")
            try:
                declared = datetime.strptime(declared_raw, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                declared = ex
            out.append((ex, rate, declared))
        return sorted(out)

    def dividend_horizon(
        self, symbol: str, as_of: date, quarterly_days: int = 95
    ) -> tuple[date, list[tuple[date, float, date]]]:
        """How far ahead the dividend picture is actually known.

        Returns the last date for which the absence of a distribution can be
        asserted, plus the announced dividends. SPY pays quarterly, so beyond
        roughly one quarter past the last announced ex-date an undeclared
        distribution is near-certain. Rectangles whose far leg extends past that
        horizon must not be certified European-equivalent on the grounds that no
        dividend is *listed* - the dividend simply has not been declared yet.
        """
        divs = self.dividends(symbol, as_of.replace(year=as_of.year - 1), as_of.replace(year=as_of.year + 1))
        past = [d for d in divs if d[0] <= as_of]
        if not past:
            return as_of, divs
        return past[-1][0] + timedelta(days=quarterly_days), divs

    def discover_quoted_expiries(
        self,
        underlying: str,
        feed: str,
        min_dte: int = 30,
        max_dte: int = 150,
        max_pages: int = 12,
    ) -> list[date]:
        """Expiries that actually carry quotes, discovered from the snapshot feed.

        The contracts endpoint lists every listed expiry including dailies, but on
        the indicative feed many of those carry no quotes at all - SPX dailies
        return contracts and zero snapshots. Discovering from the snapshot feed
        instead guarantees every expiry returned is one the scanner can actually
        price, which is the difference between an empty scan and a working one.
        """
        today = date.today()
        seen: set[date] = set()
        token: str | None = None
        for _ in range(max_pages):
            params: dict = {"feed": feed, "limit": 1000}
            if token:
                params["page_token"] = token
            status, data = self._get(
                f"{DATA_HOST}/v1beta1/options/snapshots/{underlying}", params
            )
            if status != 200:
                raise AlpacaError(f"snapshots failed ({status}): {str(data)[:200]}")
            for symbol, snap in (data.get("snapshots") or {}).items():
                quote = snap.get("latestQuote") or {}
                if not quote.get("bp") or not quote.get("ap"):
                    continue
                try:
                    _, expiry, _, _ = parse_occ_symbol(symbol)
                except AlpacaError:
                    continue
                if min_dte <= (expiry - today).days <= max_dte:
                    seen.add(expiry)
            token = data.get("next_page_token")
            if not token:
                break
        return sorted(seen)

    def implied_spot(
        self, underlying: str, expiry: date, feed: str, r: float = 0.045
    ) -> float:
        """Underlying level derived from the option chain itself.

        Index underlyings (SPX, XSP, VIX, DJX) are not in Alpaca's market-data
        offering, so `spot()` fails for them. Put-call parity recovers the level:
        at the strike where |C_mid - P_mid| is smallest the option pair is at the
        money, and F = K + exp(rT)(C_mid - P_mid) evaluated there is the forward.
        Discounting back gives a usable spot proxy.

        This is only ever used to bound the moneyness band when selecting strikes;
        the forwards the detector actually uses are re-estimated cross-sectionally
        in rectangles.implied_forward.
        """
        snaps = self.option_snapshots(underlying, expiry, feed)
        calls: dict[float, float] = {}
        puts: dict[float, float] = {}
        for symbol, snap in snaps.items():
            try:
                _, _, right, strike = parse_occ_symbol(symbol)
            except AlpacaError:
                continue
            q = snap.get("latestQuote") or {}
            bid, ask = q.get("bp"), q.get("ap")
            if bid is None or ask is None or bid <= 0 or ask <= bid:
                continue
            (calls if right == "C" else puts)[strike] = 0.5 * (float(bid) + float(ask))

        shared = sorted(set(calls) & set(puts))
        if not shared:
            raise AlpacaError(f"no usable call/put pair for {underlying} {expiry}")

        atm = min(shared, key=lambda k: abs(calls[k] - puts[k]))
        t = max((expiry - date.today()).days / 365.0, 1e-6)
        forward = atm + math.exp(r * t) * (calls[atm] - puts[atm])
        return forward * math.exp(-r * t)

    def resolve_spot(
        self, underlying: str, expiry: date, feed: str, r: float = 0.045
    ) -> tuple[float, str]:
        """Spot from the stock feed if available, else derived from the chain."""
        try:
            return self.spot(underlying), "stock_snapshot"
        except AlpacaError:
            return self.implied_spot(underlying, expiry, feed, r), "put_call_parity"

    # -- chain -------------------------------------------------------------

    def option_snapshots(
        self, underlying: str, expiry: date, feed: str, page_limit: int = 1000
    ) -> dict[str, dict]:
        """All snapshots for one expiry, following pagination to the end."""
        out: dict[str, dict] = {}
        token: str | None = None
        while True:
            params = {
                "feed": feed,
                "limit": page_limit,
                "expiration_date": expiry.isoformat(),
            }
            if token:
                params["page_token"] = token
            status, data = self._get(
                f"{DATA_HOST}/v1beta1/options/snapshots/{underlying}", params
            )
            if status != 200:
                raise AlpacaError(f"snapshots failed ({status}): {str(data)[:200]}")
            out.update(data.get("snapshots") or {})
            token = data.get("next_page_token")
            if not token:
                return out

    def snapshots_for_symbols(self, symbols: list[str], feed: str) -> dict[str, dict]:
        """Snapshots for specific contracts, for re-pricing just before sending.

        The scan-wide snapshot is minutes old by the time an order is built.
        Re-testing the violation on it proves nothing, so this fetches only the
        four legs that are about to be traded.
        """
        if not symbols:
            return {}
        status, data = self._get(
            f"{DATA_HOST}/v1beta1/options/snapshots",
            {"symbols": ",".join(symbols), "feed": feed},
        )
        if status != 200:
            raise AlpacaError(f"symbol snapshots failed ({status}): {str(data)[:200]}")
        return data.get("snapshots") or {}

    def build_chain(
        self,
        expiries: list[date],
        feed: str,
        underlying: str = "SPY",
        asof: date | None = None,
        spot: float | None = None,
    ) -> tuple[ChainSnapshot, dict[str, int]]:
        """Fetch the chain and convert it to a ChainSnapshot.

        Returns the snapshot and a parse census, so a run can report how many
        contracts were dropped and why rather than silently thinning the universe.
        """
        chain = ChainSnapshot(
            asof=asof or date.today(),
            underlying_price=(
                spot
                if spot is not None
                # resolve_spot falls back to put-call parity for index
                # underlyings, whose spot is absent from Alpaca's market data.
                else self.resolve_spot(underlying, expiries[0], feed)[0]
            ),
        )
        census = {
            "snapshots_returned": 0,
            "unparseable_symbol": 0,
            "no_quote": 0,
            "added_calls": 0,
            "added_puts": 0,
        }
        newest: datetime | None = None

        for expiry in expiries:
            snaps = self.option_snapshots(underlying, expiry, feed)
            census["snapshots_returned"] += len(snaps)

            for symbol, snap in snaps.items():
                try:
                    _, sym_expiry, right, strike = parse_occ_symbol(symbol)
                except AlpacaError:
                    census["unparseable_symbol"] += 1
                    continue

                q = snap.get("latestQuote") or {}
                bid, ask = q.get("bp"), q.get("ap")
                if bid is None or ask is None:
                    census["no_quote"] += 1
                    continue

                ts = _parse_ts(q.get("t"))
                if ts and (newest is None or ts > newest):
                    newest = ts

                iv = snap.get("impliedVolatility")
                chain.add(
                    OptionQuote(
                        symbol=symbol,
                        strike=strike,
                        expiry=sym_expiry,
                        right=right,
                        quote=Quote(
                            bid=float(bid),
                            ask=float(ask),
                            bid_size=float(q.get("bs") or 0),
                            ask_size=float(q.get("as") or 0),
                        ),
                        greeks=dict(snap.get("greeks") or {}),
                        iv=float(iv) if iv is not None else None,
                    )
                )
                census["added_calls" if right == "C" else "added_puts"] += 1

        census["newest_quote_ts"] = newest.isoformat() if newest else None  # type: ignore[assignment]
        return chain, census

    @staticmethod
    def quote_age_seconds(census: dict) -> float:
        """Age of the freshest quote in a chain build, in seconds.

        Returns infinity when no timestamp was available, so a missing timestamp
        fails the staleness gate rather than silently passing it.
        """
        raw = census.get("newest_quote_ts")
        if not raw:
            return float("inf")
        ts = _parse_ts(raw)
        if ts is None:
            return float("inf")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
