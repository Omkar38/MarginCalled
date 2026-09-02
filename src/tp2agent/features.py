"""Live F* feature construction for the SPY T1/K2 selector.

The selector was trained on end-of-day data. This module rebuilds the same
feature vector from a live `RectangleCandidate`, so a model trained on the
study's dataset sees inputs of the same shape and the same units. Every
definition here was read off the study source rather than inferred:

    conservative_bidask.py  severity, normalized_severity, executable ratio
    features.py             the IV and delta blocks
    01_build_feature_dictionary.py:95-117   the strategy-selected block

WHY THIS IS A SEPARATE MODULE
The arithmetic is small but it is the one place where a silent error cannot be
caught downstream. A wrong denominator or a flipped sign does not raise - it
shifts an input off the distribution the model was fitted on and quietly
degrades every decision that follows. Isolating it makes it testable against
the study's own numbers.

TWO FEATURES ARE NOT AVAILABLE LIVE
    lc_pressure_max_le0     needs the pre-signal daily history of the episode
    feature_D_open_interest Alpaca's option feed reports no open interest

Together they carry 2.0% of F*'s permutation importance and 0.9% of its gain,
and both sit at stability 0.25 - selected in 3 of 12 folds. They are dropped
rather than imputed. Imputing a constant for a feature the model was fitted on
is not neutral: the model learned a response to its variation, and feeding it a
value that never occurred in training is its own distribution shift. Because the
selector is retrained rather than loaded from disk, the honest fix is to train
on the same 46 features that exist live, so training and inference match by
construction. `F_STAR_LIVE` is that list, and it is the single source of truth
for both.

STRATEGY DEPENDENCE
A rectangle does not have one feature vector, it has two - one per denomination.
T1 trades legs (A, D), K2 trades legs (B, D), and exactly two features read the
traded pair:

    strategy_selected_mid_sum          rank  3, 12.7% permutation importance
    strategy_selected_mean_spread_pct  rank 14,  6.1%

Nothing else differs between the two rows: F* contains no is_t1, is_k2 or
strategy_id column, so the denomination reaches the model only through those
two numbers. To choose a denomination you score both rows and compare.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from .rectangles import RectangleCandidate

__all__ = [
    "F_STAR_48",
    "F_STAR_LIVE",
    "LIVE_UNAVAILABLE",
    "STRATEGY_DEPENDENT",
    "FeatureError",
    "build_features",
    "feature_vector",
]


class FeatureError(ValueError):
    pass


# Rank order from F_star_48_features.csv. Retained so the live vector can be
# reconciled against the study's list; the order the model consumes is
# F_STAR_LIVE, which both training and inference must read from here.
F_STAR_48: tuple[str, ...] = (
    "feature_D_theta", "feature_F_T1", "strategy_selected_mid_sum",
    "feature_bidask_executable_ratio", "feature_D_ask", "feature_D_vega",
    "signal_month", "feature_T1_DTE", "feature_F_T2", "feature_severity",
    "feature_D_delta", "feature_iv_slope_T2", "feature_B_implied_volatility",
    "strategy_selected_mean_spread_pct", "feature_B_strike", "feature_A_theta",
    "feature_term_gap", "feature_strike_gap", "feature_B_theta",
    "feature_D_implied_volatility", "feature_D_mid",
    "feature_A_implied_volatility", "feature_T2_DTE", "feature_D_bid",
    "feature_A_strike", "feature_A_vega", "feature_iv_term_K2",
    "feature_avg_iv", "feature_underlying_price",
    "feature_K2_forward_adjusted", "feature_C_implied_volatility",
    "feature_B_vega", "feature_B_ask", "feature_A_bid", "feature_D_bid_size",
    "feature_D_strike", "feature_B_delta", "feature_B_spread_pct",
    "feature_A_delta", "feature_B_spread", "feature_K1_forward_adjusted",
    "feature_normalized_severity", "lc_pressure_max_le0",
    "feature_A_ask_size", "feature_D_open_interest", "feature_D_ask_size",
    "feature_rhs", "feature_iv_slope_T1",
)

LIVE_UNAVAILABLE: frozenset[str] = frozenset({
    "lc_pressure_max_le0",       # needs pre-signal daily history
    "feature_D_open_interest",   # Alpaca reports no open interest
})

F_STAR_LIVE: tuple[str, ...] = tuple(
    f for f in F_STAR_48 if f not in LIVE_UNAVAILABLE
)

STRATEGY_DEPENDENT: frozenset[str] = frozenset({
    "strategy_selected_mid_sum",
    "strategy_selected_mean_spread_pct",
})

# Denominator guard used by the study when forming ratio features
# (build_ml_dataset.py:78). Reproduced verbatim so live values match training.
STUDY_EPS = 1e-9

# Which legs each denomination actually trades (01_build_feature_dictionary.py:102).
TRADED_LEGS: dict[str, tuple[str, str]] = {
    "T1": ("A", "D"),   # buy A, sell D - same expiry
    "K2": ("B", "D"),   # buy B, sell D - spans expiries
}


def _spread_pct(q) -> float:
    """(ask - bid) / mid, the study's archive2_ingest.py:131-132 definition."""
    mid = q.mid
    return (q.ask - q.bid) / mid if mid > 0 else float("nan")


def build_features(
    cand: RectangleCandidate,
    strategy: str,
    underlying_price: float,
    signal_date: date | None = None,
) -> dict[str, float]:
    """Build one (rectangle, denomination) feature row.

    `strategy` is "T1" or "K2" and selects which pair of legs the two
    strategy-dependent features read. Returns every live F* feature; the two
    unavailable ones are absent rather than filled.
    """
    key = strategy.upper()
    if key not in TRADED_LEGS:
        raise FeatureError(
            f"strategy must be one of {sorted(TRADED_LEGS)}, got {strategy!r}"
        )
    if not underlying_price > 0:
        raise FeatureError(f"underlying_price must be positive, got {underlying_price}")

    sig = signal_date or cand.signal_date
    legs = {"A": cand.A, "B": cand.B, "C": cand.C, "D": cand.D}
    iv = {n: (o.iv if o.iv is not None else float("nan")) for n, o in legs.items()}

    f: dict[str, float] = {}

    # Per-leg quote, size, greek and IV terms.
    for name, opt in legs.items():
        q = opt.quote
        f[f"feature_{name}_bid"] = q.bid
        f[f"feature_{name}_ask"] = q.ask
        f[f"feature_{name}_mid"] = q.mid
        f[f"feature_{name}_bid_size"] = q.bid_size
        f[f"feature_{name}_ask_size"] = q.ask_size
        f[f"feature_{name}_spread"] = q.spread
        f[f"feature_{name}_spread_pct"] = _spread_pct(q)
        f[f"feature_{name}_strike"] = opt.strike
        f[f"feature_{name}_delta"] = opt.greek("delta")
        f[f"feature_{name}_theta"] = opt.greek("theta")
        f[f"feature_{name}_vega"] = opt.greek("vega")
        f[f"feature_{name}_implied_volatility"] = iv[name]

    # TP2 geometry. The feature block deliberately mixes two price bases, and
    # the distinction is not cosmetic - it was verified column by column against
    # the study's own 354,974-row dataset:
    #
    #   feature_severity / feature_rhs / feature_normalized_severity
    #       MID prices. Exact match to C_mid*D_mid - A_mid*B_mid and its
    #       normalisation, to 0.000e+00 over 50,000 rows.
    #   feature_bidask_executable_ratio
    #       CROSSABLE quotes, (C_bid*D_bid)/(A_ask*B_ask) - the ratio exists
    #       precisely to say whether the mid-priced signal survives at tradable
    #       prices, so it must not be computed from mids.
    #
    # The conservative_bidask_* columns in the study are a separate diagnostic
    # set that happens to share the word "severity"; they are NOT these
    # features. RectangleCandidate.normalized_severity is the conservative
    # detection-side quantity and is deliberately not reused here.
    mid_lhs = cand.A.quote.mid * cand.B.quote.mid
    mid_rhs = cand.C.quote.mid * cand.D.quote.mid
    mid_gross = mid_lhs + mid_rhs
    f["feature_lhs"] = mid_lhs
    f["feature_rhs"] = mid_rhs
    f["feature_severity"] = mid_rhs - mid_lhs
    f["feature_normalized_severity"] = (
        (mid_rhs - mid_lhs) / mid_gross if mid_gross > 0 else float("nan")
    )
    # Crossable-quote products: the sides a real trade must actually pay.
    ba_lhs = cand.A.quote.ask * cand.B.quote.ask
    ba_rhs = cand.C.quote.bid * cand.D.quote.bid
    # The +EPS is the study's, not a guard of ours (build_ml_dataset.py:82).
    # It must be reproduced: it is baked into the values the model was fitted
    # on. Dropping it shifts the ratio by ~1e-6 relative where ba_lhs is small,
    # which is precisely where this feature is largest and carries the most
    # weight - the ratio ranks 4th in F* by permutation importance.
    f["feature_bidask_executable_ratio"] = ba_rhs / (ba_lhs + STUDY_EPS)

    # IV surface. Signs taken verbatim from features.py:37-40.
    f["feature_iv_slope_T1"] = iv["D"] - iv["A"]
    f["feature_iv_slope_T2"] = iv["B"] - iv["C"]
    f["feature_iv_term_K2"] = iv["B"] - iv["D"]
    f["feature_avg_iv"] = sum(iv.values()) / 4.0

    # Surface location and market state.
    f["feature_T1_DTE"] = float((cand.T1 - sig).days)
    f["feature_T2_DTE"] = float((cand.T2 - sig).days)
    f["feature_term_gap"] = float((cand.T2 - cand.T1).days)
    f["feature_strike_gap"] = cand.K2 - cand.K1
    f["feature_F_T1"] = cand.F_T1
    f["feature_F_T2"] = cand.F_T2
    f["feature_K1_forward_adjusted"] = cand.K1_adj
    f["feature_K2_forward_adjusted"] = cand.K2_adj
    f["feature_underlying_price"] = underlying_price
    f["signal_month"] = float(sig.month)

    # The only two features that know which denomination is being scored.
    l1, l2 = TRADED_LEGS[key]
    f["strategy_selected_mid_sum"] = (
        legs[l1].quote.mid + legs[l2].quote.mid
    )
    f["strategy_selected_mean_spread_pct"] = 0.5 * (
        _spread_pct(legs[l1].quote) + _spread_pct(legs[l2].quote)
    )
    return f


def feature_vector(
    cand: RectangleCandidate,
    strategy: str,
    underlying_price: float,
    signal_date: date | None = None,
    order: tuple[str, ...] = F_STAR_LIVE,
) -> list[float]:
    """The ordered vector the model consumes.

    Refuses to return a vector containing NaN. A missing greek or an
    unpriceable leg must surface as an error here, not as a plausible-looking
    number that silently moves the model's output.
    """
    row = build_features(cand, strategy, underlying_price, signal_date)
    missing = [n for n in order if n not in row]
    if missing:
        raise FeatureError(f"features not built: {missing}")
    bad = [n for n in order if not math.isfinite(row[n])]
    if bad:
        raise FeatureError(
            f"non-finite feature values for {bad}; refusing to score a rectangle "
            f"with an incomplete feature vector"
        )
    return [float(row[n]) for n in order]
