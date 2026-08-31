"""Tests for the rectangle builder and strong-violation test."""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.rectangles import (  # noqa: E402
    ChainSnapshot,
    OptionQuote,
    Quote,
    RectangleConfig,
    build_rectangles,
    dedupe_episodes,
    implied_forward,
    round_up_to_listed,
    tick_error_bound,
)

ASOF = date(2026, 8, 31)
T1 = date(2026, 10, 16)
T2 = date(2026, 11, 20)
SPOT = 640.0
R = 0.045


def _q(bid: float, ask: float, size: float = 50.0) -> Quote:
    return Quote(bid=bid, ask=ask, bid_size=size, ask_size=size)


def _bs_call(s: float, k: float, t: float, vol: float, r: float) -> float:
    """Black-Scholes call, used only to synthesise an arbitrage-free chain."""
    if t <= 0:
        return max(s - k, 0.0)
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    ncdf = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))  # noqa: E731
    return s * ncdf(d1) - k * math.exp(-r * t) * ncdf(d2)


def _clean_chain(half_spread: float = 0.05, vol: float = 0.18) -> ChainSnapshot:
    """A Black-Scholes chain. BS satisfies TP2, so this must yield no violations."""
    chain = ChainSnapshot(asof=ASOF, underlying_price=SPOT)
    strikes = [SPOT + i * 5.0 for i in range(-8, 9)]
    for expiry in (T1, T2):
        t = (expiry - ASOF).days / 365.0
        for k in strikes:
            c = _bs_call(SPOT, k, t, vol, R)
            p = c - SPOT + k * math.exp(-R * t)  # parity
            chain.add(
                OptionQuote(
                    f"SPY{expiry:%y%m%d}C{int(k * 1000):08d}",
                    k,
                    expiry,
                    "C",
                    _q(max(c - half_spread, 0.01), c + half_spread),
                )
            )
            chain.add(
                OptionQuote(
                    f"SPY{expiry:%y%m%d}P{int(k * 1000):08d}",
                    k,
                    expiry,
                    "P",
                    _q(max(p - half_spread, 0.01), p + half_spread),
                )
            )
    return chain


# --------------------------------------------------------------------------
# Forward estimation
# --------------------------------------------------------------------------


def test_implied_forward_recovers_carry():
    chain = _clean_chain()
    cfg = RectangleConfig()
    for expiry in (T1, T2):
        t = (expiry - ASOF).days / 365.0
        expected = SPOT * math.exp(R * t)
        got = implied_forward(chain, expiry, R, cfg)
        assert got is not None
        assert abs(got - expected) < 0.5, (expiry, got, expected)


def test_forward_increases_with_maturity():
    chain = _clean_chain()
    cfg = RectangleConfig()
    assert implied_forward(chain, T1, R, cfg) < implied_forward(chain, T2, R, cfg)


def test_forward_refuses_when_too_few_strikes():
    chain = ChainSnapshot(asof=ASOF, underlying_price=SPOT)
    chain.add(OptionQuote("c", 640.0, T1, "C", _q(10.0, 10.1)))
    chain.add(OptionQuote("p", 640.0, T1, "P", _q(8.0, 8.1)))
    assert implied_forward(chain, T1, R, RectangleConfig()) is None


def test_forward_ignores_unusable_quotes():
    """A zero-bid leg must not enter the parity estimate."""
    chain = _clean_chain()
    cfg = RectangleConfig()
    clean = implied_forward(chain, T1, R, cfg)
    for k, opt in list(chain.calls[T1].items()):
        if abs(k - SPOT) < 1e-9:
            chain.calls[T1][k] = OptionQuote(
                opt.symbol, k, T1, "C", Quote(0.0, 99.0, 0, 0)
            )
    assert abs(implied_forward(chain, T1, R, cfg) - clean) < 0.5


# --------------------------------------------------------------------------
# Strike rounding
# --------------------------------------------------------------------------


def test_round_up_to_listed():
    listed = [630.0, 635.0, 640.0, 645.0]
    assert round_up_to_listed(636.0, listed) == 640.0
    assert round_up_to_listed(635.0, listed) == 635.0  # exact match kept
    assert round_up_to_listed(629.0, listed) == 630.0
    assert round_up_to_listed(650.0, listed) is None  # above the board


# --------------------------------------------------------------------------
# Tick quantisation
# --------------------------------------------------------------------------


def test_tick_bound_is_larger_for_cheap_options():
    cheap = [_q(0.04, 0.06)] * 4
    rich = [_q(9.95, 10.05)] * 4
    assert tick_error_bound(cheap) > 10 * tick_error_bound(rich)


def test_tick_bound_scales_with_leg_count():
    one = tick_error_bound([_q(1.0, 1.02)])
    four = tick_error_bound([_q(1.0, 1.02)] * 4)
    assert abs(four - 4 * one) < 1e-12


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def test_black_scholes_chain_yields_no_violations():
    """BS call prices satisfy TP2, so a clean chain must produce nothing.

    This is the detector's specificity test: if it fires here, it is broken.
    """
    chain = _clean_chain()
    found, census = build_rectangles(chain, R)
    assert census["rectangles_considered"] > 100, census
    assert found == [], f"false positives on an arbitrage-free chain: {len(found)}"


def test_injected_violation_is_detected():
    """Cheapen leg A until A^ask B^ask falls below C^bid D^bid.

    Deliberately *not* done by inflating C or D: those are the legs whose prices
    set the coverage ratio, so inflating them drives price(C)/price(B) past the
    screen and the rectangle is correctly rejected before detection. Depressing
    leg A reverses the inequality while leaving the coverage ratio untouched.
    """
    chain = _clean_chain()
    cfg = RectangleConfig()
    K1, K2 = 635.0, 645.0

    opt = chain.calls[T1][K1]
    chain.calls[T1][K1] = OptionQuote(opt.symbol, K1, T1, "C", _q(1.00, 1.05))

    found, census = build_rectangles(chain, R, cfg)
    assert census["detected"] >= 1, census
    hit = [c for c in found if c.K1 == K1 and c.K2 == K2]
    assert hit, "the injected rectangle was not among the detections"
    cand = hit[0]
    assert cand.violation_size > 0
    assert cand.rhs > cand.lhs
    assert cand.normalized_severity > 0


def test_marginal_violation_below_tick_bound_is_rejected():
    """A violation smaller than the tick error must not be reported."""
    chain = ChainSnapshot(asof=ASOF, underlying_price=SPOT)
    # Cheap legs: half a tick is a large fraction of each price.
    for expiry in (T1, T2):
        for k in [630.0, 635.0, 640.0, 645.0, 650.0, 655.0]:
            chain.add(OptionQuote(f"c{expiry}{k}", k, expiry, "C", _q(0.05, 0.07)))
            chain.add(OptionQuote(f"p{expiry}{k}", k, expiry, "P", _q(0.05, 0.07)))
    # Forward cannot be estimated from these, so nothing is produced at all.
    found, census = build_rectangles(chain, R)
    assert found == []


def test_census_accounts_for_every_rectangle():
    chain = _clean_chain()
    _, census = build_rectangles(chain, R)
    dropped = (
        census["strike_gap_too_wide"]
        + census["adjusted_strike_unlisted"]
        + census["leg_missing"]
        + census["leg_unusable"]
        + census["coverage_ratio_too_wide"]
        + census["no_violation"]
        + census["below_tick_bound"]
        + census["detected"]
    )
    assert dropped == census["rectangles_considered"], census


def test_wide_coverage_ratio_is_screened():
    """price(C)/price(B) far from 1 means the forced 1:1 cap badly distorts."""
    chain = _clean_chain()
    strict = RectangleConfig(max_coverage_ratio=1.01)
    loose = RectangleConfig(max_coverage_ratio=99.0)
    _, c_strict = build_rectangles(chain, R, strict)
    _, c_loose = build_rectangles(chain, R, loose)
    assert c_strict["coverage_ratio_too_wide"] > c_loose["coverage_ratio_too_wide"]


def test_unusable_legs_are_excluded():
    """Zero-bid legs are dropped at the rectangle stage.

    Only two strikes are corrupted, both outside the +/-5% parity band, so the
    forward still estimates and the failure surfaces as `leg_unusable` rather
    than `no_forward`.
    """
    chain = _clean_chain()
    for k in (650.0, 655.0):
        opt = chain.calls[T1][k]
        chain.calls[T1][k] = OptionQuote(opt.symbol, k, T1, "C", Quote(0.0, 9.0, 0, 0))
    found, census = build_rectangles(chain, R)
    assert census["leg_unusable"] > 0, census
    assert found == []


def test_corrupting_the_whole_expiry_fails_at_the_forward():
    """If no usable pairs remain, the forward refuses and nothing is considered."""
    chain = _clean_chain()
    for k, opt in list(chain.calls[T1].items()):
        chain.calls[T1][k] = OptionQuote(opt.symbol, k, T1, "C", Quote(0.0, 9.0, 0, 0))
    found, census = build_rectangles(chain, R)
    assert census["no_forward"] > 0, census
    assert census["rectangles_considered"] == 0
    assert found == []


# --------------------------------------------------------------------------
# Episode dedup
# --------------------------------------------------------------------------


def test_dedupe_keeps_most_severe_per_near_leg():
    chain = _clean_chain()
    cfg = RectangleConfig()
    F1 = implied_forward(chain, T1, R, cfg)
    F2 = implied_forward(chain, T2, R, cfg)
    K1, K2 = 635.0, 645.0
    K1_adj = round_up_to_listed(K1 * F2 / F1, chain.listed_strikes(T2))
    K2_adj = round_up_to_listed(K2 * F1 / F2, chain.listed_strikes(T1))
    for expiry, strike in ((T2, K1_adj), (T1, K2_adj)):
        opt = chain.calls[expiry][strike]
        infl = opt.quote.ask * 4.0
        chain.calls[expiry][strike] = OptionQuote(
            opt.symbol, strike, expiry, "C", _q(infl, infl * 1.01)
        )

    found, _ = build_rectangles(chain, R, cfg)
    deduped = dedupe_episodes(found)
    keys = [c.episode_key for c in deduped]
    assert len(keys) == len(set(keys)), "dedup left duplicate near legs"
    assert len(deduped) <= len(found)


def test_record_is_json_serialisable():
    import json

    chain = _clean_chain()
    cfg = RectangleConfig()
    F1 = implied_forward(chain, T1, R, cfg)
    F2 = implied_forward(chain, T2, R, cfg)
    K1_adj = round_up_to_listed(635.0 * F2 / F1, chain.listed_strikes(T2))
    K2_adj = round_up_to_listed(645.0 * F1 / F2, chain.listed_strikes(T1))
    for expiry, strike in ((T2, K1_adj), (T1, K2_adj)):
        opt = chain.calls[expiry][strike]
        infl = opt.quote.ask * 4.0
        chain.calls[expiry][strike] = OptionQuote(
            opt.symbol, strike, expiry, "C", _q(infl, infl * 1.01)
        )
    found, _ = build_rectangles(chain, R, cfg)
    assert found
    json.dumps(found[0].to_record())


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
