"""Tests for coverage-capped position construction."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tp2agent.position import (  # noqa: E402
    CONTRACT_MULTIPLIER,
    PositionConfig,
    Side,
    Structure,
    build_position,
    integer_ratio,
    theoretical_weights,
)
from tp2agent.rectangles import (  # noqa: E402
    RectangleConfig,
    build_rectangles,
    implied_forward,
    OptionQuote,
    Quote,
)
from test_rectangles import R, T1, T2, _clean_chain, _q  # noqa: E402


def _realistic_candidate():
    """A rectangle with untouched clean-chain quotes.

    build_position does not require a violation, so this gives realistic vertical
    economics: leg A is the lower strike at T1 and therefore genuinely dearer than
    leg D, making the T1 vertical a debit spread as it is in a real market.
    """
    from tp2agent.rectangles import RectangleCandidate

    chain = _clean_chain()
    cfg = RectangleConfig()
    F1 = implied_forward(chain, T1, R, cfg)
    F2 = implied_forward(chain, T2, R, cfg)
    K1, K2, K1_adj, K2_adj = 635.0, 645.0, 640.0, 645.0
    A = chain.calls[T1][K1]
    B = chain.calls[T2][K2]
    C = chain.calls[T2][K1_adj]
    D = chain.calls[T1][K2_adj]
    lhs = A.quote.ask * B.quote.ask
    rhs = C.quote.bid * D.quote.bid
    return RectangleCandidate(
        signal_date=chain.asof, T1=T1, T2=T2,
        K1=K1, K2=K2, K1_adj=K1_adj, K2_adj=K2_adj,
        A=A, B=B, C=C, D=D, F_T1=F1, F_T2=F2,
        lhs=lhs, rhs=rhs, violation_size=rhs - lhs,
        tick_bound=0.0, coverage_ratio=C.quote.mid / B.quote.mid,
    )


def _candidate():
    """A detected rectangle, produced by cheapening leg A (see test_rectangles)."""
    chain = _clean_chain()
    cfg = RectangleConfig()
    opt = chain.calls[T1][635.0]
    chain.calls[T1][635.0] = OptionQuote(opt.symbol, 635.0, T1, "C", _q(1.00, 1.05))
    found, _ = build_rectangles(chain, R, cfg)
    hit = [c for c in found if c.K1 == 635.0 and c.K2 == 645.0]
    assert hit, "fixture failed to produce the expected rectangle"
    return hit[0]


# --------------------------------------------------------------------------
# The short-heavy result
# --------------------------------------------------------------------------


def test_weights_are_always_short_heavy():
    """C_w > B_w on every rectangle - the structural blocker."""
    cand = _candidate()
    long_w, short_w = theoretical_weights(cand)
    assert short_w > long_w, (long_w, short_w)


def test_integer_ratio_forces_one_to_one_by_default():
    assert integer_ratio(10.0, 13.0, max_denominator=1) == (1, 1)
    assert integer_ratio(1.0, 99.0, max_denominator=1) == (1, 1)


def test_integer_ratio_can_approximate_when_allowed():
    long_n, short_n = integer_ratio(10.0, 15.0, max_denominator=4)
    assert short_n / long_n == 1.5


def test_uncovered_ratio_is_rejected():
    """A short-heavy integer ratio must be refused, not sent."""
    cand = _candidate()
    cfg = PositionConfig(max_ratio_denominator=4, require_covered=True)
    spec = build_position(cand, cfg)
    if spec.rejected_reason:
        assert "uncovered" in spec.rejected_reason
    else:
        longs = sum(l.ratio_qty for l in spec.legs if l.side is Side.BUY)
        shorts = sum(l.ratio_qty for l in spec.legs if l.side is Side.SELL)
        assert shorts <= longs


# --------------------------------------------------------------------------
# Four-leg structure
# --------------------------------------------------------------------------


def test_four_leg_has_four_legs_and_is_covered():
    spec = build_position(_candidate(), PositionConfig(structure=Structure.FOUR_LEG))
    assert spec.is_executable, spec.rejected_reason
    assert len(spec.legs) == 4
    assert spec.is_covered
    buys = [l for l in spec.legs if l.side is Side.BUY]
    sells = [l for l in spec.legs if l.side is Side.SELL]
    assert len(buys) == 2 and len(sells) == 2


def test_four_leg_respects_alpaca_leg_limit():
    spec = build_position(_candidate(), PositionConfig(structure=Structure.FOUR_LEG))
    assert len(spec.legs) <= 4, "Alpaca accepts at most four legs"


def test_four_leg_max_loss_is_positive_and_bounded():
    spec = build_position(_candidate(), PositionConfig(structure=Structure.FOUR_LEG))
    assert spec.max_loss > 0
    # Bounded by the sum of both vertical widths, times the multiplier.
    cand = spec.candidate
    width_t1 = cand.D.strike - cand.A.strike
    width_t2 = cand.B.strike - cand.C.strike
    assert spec.max_loss <= (width_t1 + width_t2) * CONTRACT_MULTIPLIER + 1e-6


def test_four_leg_buys_at_ask_and_sells_at_bid():
    spec = build_position(_candidate(), PositionConfig(structure=Structure.FOUR_LEG))
    cand = spec.candidate
    by_symbol = {l.symbol: l for l in spec.legs}
    assert by_symbol[cand.A.symbol].entry_price == cand.A.quote.ask
    assert by_symbol[cand.B.symbol].entry_price == cand.B.quote.ask
    assert by_symbol[cand.C.symbol].entry_price == cand.C.quote.bid
    assert by_symbol[cand.D.symbol].entry_price == cand.D.quote.bid


def test_entry_cash_matches_leg_prices():
    spec = build_position(_candidate(), PositionConfig(structure=Structure.FOUR_LEG))
    c = spec.candidate
    expected = (
        (c.C.quote.bid + c.D.quote.bid) - (c.A.quote.ask + c.B.quote.ask)
    ) * CONTRACT_MULTIPLIER
    assert abs(spec.entry_cash - expected) < 1e-6


# --------------------------------------------------------------------------
# Two-leg structure and the economics change
# --------------------------------------------------------------------------


def test_two_leg_is_a_debit_spread():
    """Capping the T1 denomination 1:1 turns the paper's credit into a debit."""
    spec = build_position(
        _realistic_candidate(), PositionConfig(structure=Structure.TWO_LEG)
    )
    assert spec.is_executable, spec.rejected_reason
    assert len(spec.legs) == 2
    assert spec.entry_cash < 0, "long lower strike costs more than the short higher one"
    assert spec.max_loss > 0


def test_riskless_credit_spread_is_refused_as_stale_quotes():
    """A long call spread taken in for a credit cannot lose - so it is not real.

    In live quotes that means stale or crossed data, not free money, so the
    builder refuses rather than sending it. The injected fixture cheapens leg A
    hard enough to produce exactly this case.
    """
    spec = build_position(_candidate(), PositionConfig(structure=Structure.TWO_LEG))
    assert not spec.is_executable
    assert "non-positive max loss" in (spec.rejected_reason or "")


def test_denominations_record_the_economics_warning():
    """Both T1 and K2 must disclose that the 1:1 cap changes the economics."""
    for structure, label in ((Structure.T1, "T1"), (Structure.K2, "K2")):
        spec = build_position(
            _realistic_candidate(), PositionConfig(structure=structure)
        )
        assert spec.is_executable, (structure, spec.rejected_reason)
        assert any(label in n for n in spec.notes), structure
        assert any("does not survive the cap" in n for n in spec.notes), structure


def test_k2_buys_the_far_leg_and_shorts_the_near():
    """K2: buy B (K2,T2), sell D (K2~,T1). Both denominations short D."""
    cand = _realistic_candidate()
    spec = build_position(cand, PositionConfig(structure=Structure.K2))
    buys = [l for l in spec.legs if l.side is Side.BUY]
    sells = [l for l in spec.legs if l.side is Side.SELL]
    assert len(buys) == 1 and len(sells) == 1
    assert buys[0].symbol == cand.B.symbol
    assert sells[0].symbol == cand.D.symbol


def test_t1_buys_the_near_leg_and_shorts_the_near():
    """T1: buy A (K1,T1), sell D (K2~,T1). Both legs share T1."""
    cand = _realistic_candidate()
    spec = build_position(cand, PositionConfig(structure=Structure.T1))
    buys = [l for l in spec.legs if l.side is Side.BUY]
    sells = [l for l in spec.legs if l.side is Side.SELL]
    assert buys[0].symbol == cand.A.symbol
    assert sells[0].symbol == cand.D.symbol
    assert buys[0].expiry == sells[0].expiry, "T1 legs must share an expiry"


def test_capping_note_is_always_present():
    """Every spec must disclose that the paper's credit property is lost."""
    for structure in (Structure.TWO_LEG, Structure.FOUR_LEG):
        spec = build_position(
            _realistic_candidate(), PositionConfig(structure=structure)
        )
        assert any("does not survive this cap" in n for n in spec.notes), structure


# --------------------------------------------------------------------------
# Commissions
# --------------------------------------------------------------------------


def test_default_cost_is_zero_matching_alpaca_paper():
    """The contest scores a commission-free paper account, so the default is 0."""
    from tp2agent.position import COMMISSION_PER_CONTRACT_SIDE

    assert COMMISSION_PER_CONTRACT_SIDE == 0.0
    spec = build_position(
        _realistic_candidate(), PositionConfig(structure=Structure.FOUR_LEG)
    )
    assert spec.commissions_round_trip == 0.0
    assert spec.max_loss_with_commissions == spec.max_loss


def test_commissions_scale_with_leg_count_when_charged():
    from tp2agent.position import COMMISSION_IBKR_LITE

    cfg2 = PositionConfig(
        structure=Structure.TWO_LEG, commission_per_contract_side=COMMISSION_IBKR_LITE
    )
    cfg4 = PositionConfig(
        structure=Structure.FOUR_LEG, commission_per_contract_side=COMMISSION_IBKR_LITE
    )
    two = build_position(_realistic_candidate(), cfg2)
    four = build_position(_candidate(), cfg4)
    assert abs(two.commissions_round_trip - 2 * 2 * 0.65) < 1e-9
    assert abs(four.commissions_round_trip - 4 * 2 * 0.65) < 1e-9
    assert four.max_loss_with_commissions > four.max_loss


def test_alpaca_regulatory_rate_is_an_order_of_magnitude_below_ibkr():
    """The source study's $0.65/side is ~13x Alpaca's live regulatory cost."""
    from tp2agent.position import (
        COMMISSION_ALPACA_REGULATORY,
        COMMISSION_IBKR_LITE,
    )

    assert COMMISSION_IBKR_LITE / COMMISSION_ALPACA_REGULATORY > 10


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_degenerate_widths_are_rejected():
    """If the strike ordering is wrong the position must be refused."""
    cand = _candidate()
    bad = OptionQuote(cand.D.symbol, cand.A.strike - 5.0, cand.D.expiry, "C", cand.D.quote)
    object.__setattr__(cand, "D", bad)
    spec = build_position(cand, PositionConfig(structure=Structure.FOUR_LEG))
    assert not spec.is_executable
    assert spec.rejected_reason is not None


def test_fractional_quantity_cannot_reach_a_payload():
    """Options trade in whole contracts. The study's weights are continuous -
    it says so explicitly - so a fractional weight is a theoretical size, never
    an order. The boundary refuses it rather than rounding silently."""
    from tp2agent.position import PositionLeg, Side

    for bad in (1.5, 0.5, 2.0001):
        leg = PositionLeg("SPY261016C00640000", 640.0, "2026-10-16", Side.BUY, bad, 1.0)
        try:
            leg.to_alpaca_leg()
        except ValueError as exc:
            assert "whole number of contracts" in str(exc)
            continue
        raise AssertionError(f"fractional qty {bad} must be refused")


def test_zero_or_negative_quantity_refused():
    from tp2agent.position import PositionLeg, Side

    for bad in (0, -1):
        leg = PositionLeg("SPY261016C00640000", 640.0, "2026-10-16", Side.BUY, bad, 1.0)
        try:
            leg.to_alpaca_leg()
        except ValueError as exc:
            assert ">= 1" in str(exc)
            continue
        raise AssertionError(f"qty {bad} must be refused")


def test_every_built_position_has_whole_contracts():
    """The weights are 400:427 style floats; what ships must be integers."""
    for structure in (Structure.T1, Structure.K2, Structure.FOUR_LEG):
        spec = build_position(
            _realistic_candidate(), PositionConfig(structure=structure)
        )
        for leg in spec.legs:
            assert isinstance(leg.ratio_qty, int), (structure, leg.ratio_qty)
            payload = leg.to_alpaca_leg()
            assert payload["ratio_qty"].isdigit()


def test_alpaca_leg_payload_shape():
    spec = build_position(_candidate(), PositionConfig(structure=Structure.FOUR_LEG))
    for leg in spec.legs:
        payload = leg.to_alpaca_leg()
        assert set(payload) == {"symbol", "side", "ratio_qty", "position_intent"}
        assert payload["side"] in ("buy", "sell")
        assert payload["ratio_qty"] == "1"


def test_record_is_json_serialisable():
    import json

    spec = build_position(_candidate(), PositionConfig(structure=Structure.FOUR_LEG))
    text = json.dumps(spec.to_record())
    assert "four_leg" in text
    assert "coverage_ratio" in text



def _scored_candidate():
    """A candidate carrying greeks and IV on all four legs.

    _realistic_candidate() deliberately omits them, which is why the selector
    abstains on it - that is the correct behaviour, not a fixture convenience.
    """
    from dataclasses import replace as _replace

    c = _realistic_candidate()
    def enrich(o, d, th, v, iv):
        return _replace(o, greeks={"delta": d, "theta": th, "vega": v}, iv=iv)
    return _replace(
        c,
        A=enrich(c.A, 0.55, -0.11, 0.21, 0.20),
        B=enrich(c.B, 0.45, -0.12, 0.22, 0.24),
        C=enrich(c.C, 0.50, -0.13, 0.23, 0.22),
        D=enrich(c.D, 0.40, -0.14, 0.24, 0.26),
    )


# --------------------------------------------------------------------------
# Denomination selection and abstention
# --------------------------------------------------------------------------


def test_choice_is_indexable_for_backwards_compatibility():
    from tp2agent.position import Choice

    c = Choice(Structure.T1, "because")
    assert c[0] is Structure.T1 and c[1] == "because"
    assert not c.abstained


def test_abstention_is_expressible():
    from tp2agent.position import Choice

    c = Choice(None, "below threshold")
    assert c.abstained and c.structure is None


def test_model_selector_picks_the_higher_probability():
    from tp2agent.position import ModelDenominationSelector

    seen = []

    def scorer(vec):
        seen.append(len(vec))
        # K2 is scored second; give it the higher probability.
        return 0.60 if len(seen) == 2 else 0.55

    sel = ModelDenominationSelector(scorer, min_probability=0.5)
    c = sel.choose("SPY", _scored_candidate(), 645.0)
    assert c.structure is Structure.K2, c.reason
    assert seen == [46, 46], "one 46-feature vector per denomination"
    assert c.score_map == {"T1": 0.55, "K2": 0.60}


def test_model_selector_abstains_below_threshold():
    """The study's rule: trade the better denomination only if it clears 0.5."""
    from tp2agent.position import ModelDenominationSelector

    sel = ModelDenominationSelector(lambda v: 0.42, min_probability=0.5)
    c = sel.choose("SPY", _scored_candidate(), 645.0)
    assert c.abstained
    assert "below the 0.50 threshold" in c.reason


def test_model_selector_abstains_when_features_cannot_be_built():
    """A missing greek means we do not know what we would be scoring; guessing
    T1 there would present an absence of information as a prediction."""
    from tp2agent.position import ModelDenominationSelector

    cand = _scored_candidate()
    cand.D.greeks.pop("vega", None)
    sel = ModelDenominationSelector(lambda v: 0.99, min_probability=0.5)
    c = sel.choose("SPY", cand, 645.0)
    assert c.abstained
    assert "features unavailable" in c.reason


def test_model_selector_does_not_score_spx():
    """Only T1 is submittable on SPX, so there is nothing to choose between and
    the model must not be consulted."""
    from tp2agent.position import ModelDenominationSelector

    def scorer(vec):
        raise AssertionError("the model must not be called when only one denomination is allowed")

    c = ModelDenominationSelector(scorer).choose("SPX", _realistic_candidate(), 6400.0)
    assert c.structure is Structure.T1
    assert not c.abstained


def test_structure_for_refuses_to_default_after_an_abstention():
    """structure_for cannot represent "no trade"; it must raise rather than
    silently convert a stand-down into a position."""
    from tp2agent.position import ModelDenominationSelector, structure_for

    sel = ModelDenominationSelector(lambda v: 0.1, min_probability=0.5)
    try:
        structure_for("SPY", sel, _realistic_candidate(), 645.0)
    except ValueError as exc:
        assert "abstained" in str(exc)
        return
    raise AssertionError("an abstention must not silently become a structure")


def test_t1_max_loss_is_the_debit():
    """T1 is a bull call spread: K1 < K2~, so the debit is the whole risk."""
    from tp2agent.position import PositionConfig, build_position

    c = _realistic_candidate()
    spec = build_position(c, PositionConfig(structure=Structure.T1))
    debit = c.A.quote.ask - c.D.quote.bid
    assert abs(spec.max_loss - max(debit, 0.0) * 100) < 1e-9


def test_k2_max_loss_includes_the_strike_gap():
    """K2 is a diagonal. When K2~ rounds below K2 the package loses
    (K2 - K2~) at T1 on top of the debit, and a max loss that is understated
    is the one number a risk cap cannot tolerate."""
    from dataclasses import replace as _replace

    from tp2agent.position import PositionConfig, build_position

    c = _realistic_candidate()
    # Force a one-point gap between the long B strike and the short D strike.
    c = _replace(c, D=_replace(c.D, strike=c.B.strike - 1.0))
    spec = build_position(c, PositionConfig(structure=Structure.K2))
    debit = c.B.quote.ask - c.D.quote.bid
    assert abs(spec.max_loss - (max(debit, 0.0) + 1.0) * 100) < 1e-9, spec.max_loss


def test_k2_calendar_max_loss_is_just_the_debit():
    """With K2~ == K2 the diagonal collapses to a calendar spread and the
    debit is again the whole risk - the common case, 96.8% of SPY rectangles."""
    from dataclasses import replace as _replace

    from tp2agent.position import PositionConfig, build_position

    c = _realistic_candidate()
    c = _replace(c, D=_replace(c.D, strike=c.B.strike))
    spec = build_position(c, PositionConfig(structure=Structure.K2))
    debit = c.B.quote.ask - c.D.quote.bid
    assert abs(spec.max_loss - max(debit, 0.0) * 100) < 1e-9


def test_spread_quoted_below_intrinsic_is_refused():
    """A vertical cannot trade far below intrinsic - that would be free money,
    so the quote is broken rather than cheap.

    Live on the indicative feed: SPX 6860/6890 with the forward ~800 points
    above both strikes, so the spread is worth essentially its full 30-point
    width, quoted so the package cost 11.14. That fake discount was what
    registered as a TP2 violation, and the resulting order could never fill.
    """
    from dataclasses import replace as _replace

    from tp2agent.position import PositionConfig, build_position

    c = _scored_candidate()
    # Forward far above both strikes -> the spread is worth its whole width.
    c = _replace(c, F_T1=c.D.strike + 800.0)
    width = c.D.strike - c.A.strike
    assert width > 0
    # Quote the long leg so the debit is a small fraction of that width.
    cheap = _replace(c.A, quote=_replace(c.A.quote, ask=c.D.quote.bid + 0.05 * width))
    c = _replace(c, A=cheap)
    spec = build_position(c, PositionConfig(structure=Structure.T1))
    assert not spec.is_executable
    assert "below intrinsic" in (spec.rejected_reason or ""), spec.rejected_reason


def test_a_normal_debit_spread_is_untouched_by_the_intrinsic_floor():
    from tp2agent.position import PositionConfig, build_position

    spec = build_position(_scored_candidate(), PositionConfig(structure=Structure.T1))
    assert "below intrinsic" not in (spec.rejected_reason or "")


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
