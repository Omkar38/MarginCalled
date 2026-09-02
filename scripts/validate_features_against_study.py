"""Cross-validate tp2agent.features against the study's own token dataset.

The live feature builder has to reproduce, from a live chain, the exact
quantities the selector was trained on. Unit tests can only check that the code
matches what we BELIEVE the definitions are; this checks it against what the
study actually computed, row by row, on the 354,974-row token dataset.

It caught three things a self-consistent test never would have:

  1. feature_severity / feature_rhs / feature_normalized_severity are built
     from MID prices, not from the crossable A_ask*B_ask and C_bid*D_bid
     products. The conservative_bidask_* columns share the word "severity" but
     are a different set.
  2. feature_bidask_executable_ratio IS crossable-quote based - the feature set
     genuinely mixes the two price bases.
  3. That ratio carries the study's own +1e-9 in the denominator, which shifts
     it by ~1e-6 relative exactly where the ratio is largest.

Needs pandas and the study checkout, so it is a script rather than a test:

    "$STUDY/.venv/bin/python" scripts/validate_features_against_study.py

Exits non-zero on any divergence.
"""
import sys, math, datetime as dt
import pandas as pd, numpy as np
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
from tp2agent.rectangles import Quote, OptionQuote, RectangleCandidate
from tp2agent.features import build_features

import os
PQ = os.environ.get(
    "TP2_STUDY_PARQUET",
    os.path.expanduser("~/Downloads/FinalQuant copy 2/TP2MVTransformer/data/tp2_t1k2_token_dataset.parquet"),
)
if not os.path.exists(PQ):
    sys.exit(f"study dataset not found at {PQ}; set TP2_STUDY_PARQUET")
df = pd.read_parquet(PQ)
LEGS="ABCD"
per=[f"feature_{l}_{s}" for l in LEGS for s in ("bid","ask","mid","spread_pct","implied_volatility","delta","theta","vega","strike","bid_size","ask_size")]
per=[c for c in per if c in df.columns]
DERIVED=["feature_severity","feature_rhs","feature_normalized_severity","feature_bidask_executable_ratio",
 "feature_iv_slope_T1","feature_iv_slope_T2","feature_iv_term_K2","feature_avg_iv",
 "strategy_selected_mid_sum","strategy_selected_mean_spread_pct","feature_term_gap","feature_strike_gap"]
extra=["feature_T1_DTE","feature_T2_DTE","feature_F_T1","feature_F_T2","feature_underlying_price",
 "feature_K1_forward_adjusted","feature_K2_forward_adjusted","strategy"]
sub=df[per+DERIVED+extra].dropna()
rng=np.random.default_rng(7)
N=min(20000,len(sub))
sub=sub.iloc[rng.choice(len(sub),size=N,replace=False)]
print(f"validating {len(sub):,} real study rows ({sub['strategy'].value_counts().to_dict()})\n")
SIG=dt.date(2024,1,3)
worst={k:0.0 for k in DERIVED}
for r in sub.itertuples(index=False):
    d=r._asdict()
    def leg(l):
        q=Quote(d[f"feature_{l}_bid"],d[f"feature_{l}_ask"],d.get(f"feature_{l}_bid_size") or 1.0,d.get(f"feature_{l}_ask_size") or 1.0)
        return OptionQuote(f"SYN{l}",d[f"feature_{l}_strike"],SIG,"C",q,
            {"delta":d[f"feature_{l}_delta"],"theta":d[f"feature_{l}_theta"],"vega":d[f"feature_{l}_vega"]},
            d[f"feature_{l}_implied_volatility"])
    A,B,C,D=(leg(l) for l in LEGS)
    lhs=A.quote.ask*B.quote.ask; rhs=C.quote.bid*D.quote.bid
    cand=RectangleCandidate(signal_date=SIG,
        T1=SIG+dt.timedelta(days=int(d["feature_T1_DTE"])), T2=SIG+dt.timedelta(days=int(d["feature_T2_DTE"])),
        K1=d["feature_A_strike"], K2=d["feature_B_strike"],
        K1_adj=d["feature_K1_forward_adjusted"], K2_adj=d["feature_K2_forward_adjusted"],
        A=A,B=B,C=C,D=D, F_T1=d["feature_F_T1"], F_T2=d["feature_F_T2"],
        lhs=lhs, rhs=rhs, violation_size=rhs-lhs, tick_bound=0.0, coverage_ratio=1.0)
    got=build_features(cand, d["strategy"], d["feature_underlying_price"], SIG)
    for k in DERIVED:
        exp=d[k]
        if not (isinstance(exp,(int,float)) and math.isfinite(exp)): continue
        worst[k]=max(worst[k],abs(got[k]-exp)/max(1.0,abs(exp)))
print(f"{'feature':<38}{'max rel err':>13}   verdict")
print("-"*70)
ok=True
for k in DERIVED:
    v=worst[k]
    if v<1e-12: verdict="EXACT"
    else: verdict="MISMATCH"; ok=False
    print(f"{k:<38}{v:>13.2e}   {verdict}")
print("-"*70)
print(f"{'ALL 12 DERIVED FEATURES REPRODUCE THE STUDY' if ok else 'DIVERGENCE'} over {len(sub):,} rows")
sys.exit(0 if ok else 1)
