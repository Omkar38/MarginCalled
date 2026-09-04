# Streamlit dashboard — build brief

Everything needed to build a Streamlit app for this project **from the GitHub
repository alone** — no local machine, no credentials, no access to this
conversation.

**Read this first:**

- All data is in **`dashboard_data/<UNDERLYING>/`**, gzip-compressed and
  committed to the repo. `data/` is gitignored and **will not exist** after a
  clone — do not reference it.
- There is **no `.env` and no API access.** The app must render entirely from
  the committed files. Any live-Alpaca section is optional and must be wrapped
  so its absence changes nothing.
- Files are `.csv.gz` and `.jsonl.gz`. `pandas.read_csv` reads `.gz` natively;
  with the stdlib use `gzip.open(path, "rt")`.

```python
import gzip, csv, json
import pandas as pd

df = pd.read_csv("dashboard_data/SPY/violations.csv.gz")          # pandas

with gzip.open("dashboard_data/SPY/episodes.csv.gz", "rt") as fh:  # stdlib
    rows = list(csv.DictReader(fh))

with gzip.open("dashboard_data/SPY/decisions.jsonl.gz", "rt") as fh:
    decisions = [json.loads(line) for line in fh if line.strip()]
```

> **Not a competition requirement.** The Alpaca AI Trading Agents Hackathon
> mandates only: the Alpaca Trading API plus the MCP server or CLI, options
> trading, a dedicated paper account, and a one-page write-up. A dashboard is
> for the "Presentation & Execution" judging criterion.

---

## 1. What the agent does, in one paragraph

Call prices must satisfy **total positivity of order 2 (TP2)**. For expiries
$T_1<T_2$ and strikes ordered by forward moneyness,
$C(K_1,T_1)C(K_2,T_2) \ge C(\tilde K_1,T_2)C(\tilde K_2,T_1)$. Four contracts form
a **rectangle** — labelled **A** $(K_1,T_1)$, **B** $(K_2,T_2)$, **C**
$(\tilde K_1,T_2)$, **D** $(\tilde K_2,T_1)$. When
$A^{ask}B^{ask} < C^{bid}D^{bid}$ the surface is inconsistent: a **violation**.
The agent enters the violation (buy A, sell D — the "T1 denomination") and exits
when it **reverts**. Underlyings scanned: **SPY**, **SPX**, **XSP**.

---

## 2. Data files

All under **`dashboard_data/<UNDERLYING>/`** where UNDERLYING ∈ {SPY, SPX, XSP},
each file gzip-compressed with a `.gz` suffix. Column names and JSON shapes
below are exactly as stored.

### `scans.csv` — one row per scan (~88 SPY rows)
```
ts, feed, spot, quote_age_s, duration_s, expiry_pairs, rectangles_considered,
no_forward, adjusted_strike_unlisted, leg_missing, leg_unusable,
leg_delta_out_of_band, strike_gap_too_wide, coverage_ratio_too_wide,
roundup_too_far, leg_too_cheap, degenerate_legs, vertical_arbitrage,
no_violation, below_tick_bound, detected, episodes, margin_count, margin_min,
margin_p05, margin_p25, margin_median, margin_p75, margin_p95, margin_max,
raw_positive_margins, near_misses
```
`ts` is ISO local time. The columns from `no_forward` to `below_tick_bound` are a
**drop census** — every rectangle considered is accounted for by exactly one
reason. **Good for a funnel chart.**

### `violations.csv` — one row per detected violation per scan (~11,600 SPY)
```
ts, feed, spot, episode_key, T1, T2, K1, K2, K1_adj, K2_adj, F_T1, F_T2,
sym_A, sym_B, sym_C, sym_D,
A_bid, A_ask, B_bid, B_ask, C_bid, C_ask, D_bid, D_ask,
lhs, rhs, violation_size, normalized_severity, tick_bound, coverage_ratio,
theory_category, theory_tradable, exec_tradable, exec_reasons, min_leg_mid,
A_delta, A_gamma, A_theta, A_vega, A_rho, A_iv, A_bid_size, A_ask_size,
  (same block for B, C, D)
```
- `lhs` = A_ask·B_ask, `rhs` = C_bid·D_bid, `violation_size` = rhs − lhs
- `normalized_severity` = violation_size / (lhs + rhs)
- `theory_category` ∈ `european_native`, `no_distribution`, `dividend_spanning`,
  `dividend_bound`, `unresolved`. Only the first four may trade.
- `theory_tradable`, `exec_tradable` are the strings `"True"`/`"False"`

### `episodes.csv` — one row per tracked rectangle (~9,470 SPY)
```
episode_id, underlying, T1, T2, K1, K2, K1_adj, K2_adj,
sym_A, sym_B, sym_C, sym_D, theory_category,
first_seen, last_seen, last_violating, observations, violating_observations,
first_severity, peak_severity, peak_at, last_severity,
status, reverted_at, duration_seconds, time_to_revert_seconds
```
`status` ∈ `active`, `reverted`. **`time_to_revert_seconds` is the headline
strategy metric** — how long a violation took to correct.

### `episode_path.csv` — time series per episode (~79,600 SPY rows)
```
episode_id, ts, event_index, observable, violating, severity, violation_size,
lhs, rhs, missing_legs,
A_bid, A_ask, B_bid, B_ask, C_bid, C_ask, D_bid, D_ask,
A_delta, A_iv, B_delta, B_iv, C_delta, C_iv, D_delta, D_iv
```
`violating` and `observable` are `"True"`/`"False"` strings. **Use this to draw a
single episode's life** — severity over time from detection to reversion.

### `positions.jsonl` — one JSON object per line, actual trades (23 SPY)
```json
{"episode_id","underlying","denomination","order_id","opened_at",
 "long_symbol","short_symbol","long_expiry","short_expiry",
 "entry_long_price","entry_short_price","qty",
 "closed_at","close_order_id","close_reason","notes"}
```
`closed_at` is `null` while open. `close_reason` ∈ `reverted`, `expiry_near`,
`time_stop`, `deadline`.

### `decisions.jsonl` — every candidate considered (~10,900 SPY)
```json
{"decision_id","ts","underlying","episode_key","outcome","stage","reason",
 "theory_category","denomination","selector","determinant","quotes","risk",
 "order","broker","extra"}
```
`outcome` ∈ `traded`, `risk_rejected`, `not_executable`, `theory_blocked`,
`order_failed`, `held`, `closed`. Nested dicts:
- `determinant`: `lhs`, `rhs`, `violation_size`, `normalized_severity`,
  `severity_over_rhs`, `tick_bound`, `tick_bound_required`, `clears_tick_bound`,
  `K1`, `K2`, `K1_adj`, `K2_adj`, `T1`, `T2`
- `quotes`: `{"A": {"symbol","strike","expiry","bid","ask"}, ...}` for A,B,C,D
- `risk`: **empty `{}` unless the candidate reached the risk gates.** Populated
  only for `outcome` in `traded` and `risk_rejected`; empty for
  `not_executable`, `theory_blocked` and `order_failed`, which are refused
  earlier. When populated: `approved` (bool), `rejections` (list of
  `{code, message}`), `checks_passed` (list of strings), `violation_retained`,
  `fresh_violation_size`. **Always `d.get("risk") or {}`** — indexing it
  directly will crash on ~96% of rows.

  Observed distribution on SPY (10,923 rows):

  | outcome | n | risk populated |
  |---|---|---|
  | `not_executable` | 8,506 | no |
  | `theory_blocked` | 1,969 | no |
  | `risk_rejected` | 383 | **yes** |
  | `traded` | 50 | **yes** |
  | `order_failed` | 15 | no |
- `order`: `indicative_net`, `package_spread`, `shade`, `limit_price`,
  `is_debit`, `qty`, `legs`, `notes`. **Empty `{}` unless an order was built** —
  populated only for `traded` and `order_failed`.
- `broker`: `order_id`, `status`. Empty unless an order was sent.
- `selector`, `extra`: often `{}`. Same rule — always `.get(k) or {}`.

**This file is the richest source** — it explains *why* each candidate was or
wasn't traded. Refusals are as complete as fills.

### `orders.jsonl` — raw order payloads and broker responses.

---

## 3. Row counts as of 2026-09-03

| underlying | decisions | positions | violations |
|---|---|---|---|
| SPY | 10,923 | 23 | 11,646 |
| SPX | 18,920 | 0 | 34,936 |
| XSP | 2,662 | (no file) | 3,421 |

**Handle missing files** — `data/XSP/positions.jsonl` does not exist. Guard every
read with `Path(...).exists()`.

---

## 4. Live account data — NOT available

There is no `.env` in the repository and no API credentials, by design: the file
is gitignored so keys never reach GitHub. **Build the dashboard entirely from
`dashboard_data/`.**

If you want an optional live panel, gate it so the app is unaffected when the
variables are absent:

```python
import os
live = os.environ.get("APCA_API_KEY_ID") and os.environ.get("APCA_API_SECRET_KEY")
if live:
    ...   # paper host only: https://paper-api.alpaca.markets
else:
    st.caption("Live account panel disabled — no credentials configured.")
```

Never call `api.alpaca.markets` (the live host). The account figures needed for
the headline numbers are in section 6 and need no API call.

## 4b. The LLM narrator — no key required

The agent has a language-model layer that reads the decision log and writes a
plain-English account of what it did and why it refused what it refused. **Its
output is already generated and committed**, so the dashboard needs no API key,
no credentials and no network:

```
reports/SPY_narration.md
reports/SPX_narration.md
reports/XSP_narration.md
```

```python
from pathlib import Path
import streamlit as st

for u in ("SPY", "SPX", "XSP"):
    f = Path(f"reports/{u}_narration.md")
    if f.exists():
        with st.expander(f"{u} — what the agent did, in its own words"):
            st.markdown(f.read_text())
```

These were written by `claude-opus-5` from `decisions.jsonl` on 2026-09-03. They
are static text; treat them as a report, and say in the UI that they describe
that session.

**Optional: live narration.** Only if you want the app to generate new prose on
demand. It needs an Anthropic API key and it spends real credits **per viewer**,
so a public deployment can run up a bill. If you do it:

- Put the key in `.streamlit/secrets.toml` as `ANTHROPIC_API_KEY`, never in
  code, and add `.streamlit/secrets.toml` to `.gitignore`
- On Streamlit Community Cloud use the **Secrets** panel, not a file
- Gate it behind a button so it never runs on page load
- Fall back to the committed narration when the key is absent

```python
key = st.secrets.get("ANTHROPIC_API_KEY")
if key and st.button("Generate fresh narration"):
    ...   # POST https://api.anthropic.com/v1/messages
else:
    st.markdown(Path("reports/SPY_narration.md").read_text())
```

The repo's own implementation is `src/tp2agent/narrator.py` — it is stdlib-only
(`urllib`), has no trading permissions, and falls back to a deterministic
template when the API is unavailable. Note that the key used here is
identity-linked and also requires an `anthropic-workspace-id` header; the
committed narrations avoid all of that.

---

## 4c. Why the quantities are 1:1 — put this in the app

Two different constraints get confused here. **One is a hard broker prohibition;
the other was our own limitation.** A dashboard page explaining this is one of
the more interesting things the project has to say.

### The RATIO is forbidden

The study sizes each leg by the *opposing* contract's price. Table 5.1: the
T1 denomination goes **long `C(K2,T2)` shares of A** and **short `C(K̃1,T2)`
shares of D**.

Because the ordering condition forces `K̃1 < K2`, contract C is the lower strike
at T2 and is therefore **always dearer than B**. So the short quantity always
exceeds the long quantity. Measured across 668 real SPY candidates on
2026-09-03: **ratio from 1.03 to 33.77, median 1.74, not one at or below 1.0.**

Short more than long means selling calls you do not own — **naked**. The account
is Alpaca **options level 3**, which permits spreads only. Submitting the real
thing returns:

```
long 1 : short 2  ->  HTTP 403, code 40310000
                      "account not eligible to trade uncovered option contracts"
```

No rescaling escapes it: a ratio above 1 stays above 1 at 2:3, 4:6 or any
multiple. **1:1 is not the nearest covered approximation — it is the only one.**

Both denominations the study finds profitable, T1 and K2, are short-heavy. The
two that *are* coverable, T2 and K1, are the two the study reports produce
negative average profits. **There is no configuration that is both profitable and
legal at level 3.** Options level 4 is the only route, and that is an Alpaca
approval, not a code change.

### The SIZE was never forbidden — and we under-used it

Trading 10 long against 10 short is still 1:1, still covered, still level-3
legal. Nothing stopped it except our own sizing code, which always returned one
contract.

The cost was large. Across 433 orders the **median max loss per trade was $4**
against a **$2,500 risk cap** — about 1/625th of the budget available. On the
same signals and the same filters:

| sizing | trades | total P&L | median max loss |
|---|---|---|---|
| unit 1:1 (what was traded) | 672 | +$8,498 | $2 |
| paper weights, integer-rounded | 386 | +$6,049 | $700 |
| **1:1 scaled 10x** | 672 | **+$35,293** | $20 |

The honest ceiling is **liquidity, not risk**: these are penny options quoting
one or two lots, so a 600-contract fill is not real even though the risk budget
allows it. The 10x figure uses a deliberate liquidity ceiling
(`--max-scale`), and even then sits at 1/125th of the cap.

**The summary worth displaying:** the broker forbade the *shape* of the study's
position, not its *size*. The shape could not be traded at all; the size could
have been perhaps ten times larger and was not.

Reproduce with:
```
python3 scripts/backtest_weighted.py --underlying SPY --sizing unit
python3 scripts/backtest_weighted.py --underlying SPY --sizing weighted
python3 scripts/backtest_weighted.py --underlying SPY --sizing scaled --max-scale 10
```
All three prices every surviving episode at quoted prices and assumes it filled.
Live, 17 orders filled for about **-$28**. That gap between backtest and account
is a result in itself and should be shown next to these numbers.

---

## 4d. Why the live account lost money — one reason

**The bid-ask spread. Nothing else.**

Alpaca charged nothing. Verified on the account: `accrued_fees: 0`, no
commission field on any filled order, and fills routinely came in *better* than
the limit asked — one order with a `-0.03` limit filled at `-0.06`. The broker
gave price improvement, not friction.

The cost is market structure. You buy at the ask and sell at the bid, and the
difference is what a market maker earns for standing there. On these contracts:

```
SPY leg spreads, 5,786 observations on 2026-09-03
   A leg median  $0.010
   D leg median  $0.010
   two legs, in and out                 = $0.020/share = $2 per contract

backtest median profit per trade        = +$2 per contract
```

**The edge and the cost of collecting it are the same number.** A ~2-cent
mispricing exists and does correct — 21 of 21 live positions closed because the
violation reverted — but crossing the spread twice to capture it costs ~2 cents.

That leaves the sign to execution, and a resting limit order loses that coin
flip by construction: **a limit buy fills only when the market comes down to
it.** The backtest assumes a fill at the quoted price on all 672 signals; the
agent got 21, and those 21 are precisely the ones already moving against the
position. On penny options a single tick is 20-30% of the contract's value, so
there is no room for that to average out.

### Exact backtest P&L, both denominations

Same filters as live, no look-ahead, entry and exit both crossing the spread.
Weighted sizing is Table 5.1 normalised so the long leg is one contract and the
short leg the nearest whole number, because a market cannot sell 1.74 contracts.

| denomination | sizing | trades | total | mean | median | win |
|---|---|---|---|---|---|---|
| **T1** (long A, short D) | unit 1:1 | 672 | **+$9,829** | +$14.63 | +$2.00 | 67% |
| **T1** | paper weights | 386 | **+$8,754** | +$22.68 | +$1.00 | 65% |
| **K2** (long B, short D) | unit 1:1 | 748 | **-$5,363** | -$7.17 | $0.00 | 49% |
| **K2** | paper weights | 312 | **-$5,775** | -$18.51 | -$1.00 | 43% |

Two things to say about this honestly:

- **T1 is profitable in backtest and K2 is not**, though the study reports both
  performing well. The difference is the exit: the study holds every position to
  expiration, this agent exits on reversion. K2 is a diagonal whose legs expire
  on different dates, so closing it early is a materially different trade.
- **All four figures assume a fill on every signal.** Live, 3% filled, and the
  realised result was about -$45 across 21 round trips. The gap between any row
  above and that number is the execution cost, not a modelling error.

Reproduce:
```
python3 scripts/backtest_weighted.py --underlying SPY --denom T1 --sizing weighted
python3 scripts/backtest_weighted.py --underlying SPY --denom K2 --sizing weighted
```

## 4e. How T1 or K2 is chosen — it is not, yet

**Terminology.** "Paper weights" throughout this document means the quantities in
**Table 5.1 of Glasserman, Li & Pirjol**, where each leg's size is the *opposing*
contract's price:

| denomination | long leg | its quantity | short leg | its quantity |
|---|---|---|---|---|
| **T1** | A = $(K_1,T_1)$ | $C(K_2,T_2)$ = **B's price** | D = $(\tilde K_2,T_1)$ | $C(\tilde K_1,T_2)$ = **C's price** |
| **K2** | B = $(K_2,T_2)$ | $C(K_1,T_1)$ = **A's price** | D = $(\tilde K_2,T_1)$ | $C(\tilde K_1,T_2)$ = **C's price** |

### The selection is built but inert

`run_scanner.py` line 217 calls `structure_for(underlying)` **once**, at startup,
and every trade thereafter uses that one value. There is no per-candidate choice.

```
SPY -> T1   "no model available; defaulting to T1"
SPX -> T1   "T1 is the only submittable denomination"
XSP -> T1   "T1 is the only submittable denomination"
```

- **SPX and XSP:** correct and forced. Both are European and Alpaca rejects a
  multi-leg order whose legs span expiries (`HTTP 422 / 42210000`), so K2 - a
  diagonal - cannot be submitted at all. There is nothing to choose between.
- **SPY:** T1 is a **fallback, not a decision.** SPY is American so both are
  legal, but no model is loaded.

`ModelDenominationSelector` exists in `src/tp2agent/position.py` and is tested:
it builds one 46-feature vector **per denomination** (they differ in exactly two
features - `strategy_selected_mid_sum` and `strategy_selected_mean_spread_pct`),
scores both, takes the higher, and **abstains** if neither clears 0.5. It is
never instantiated by the scanner, because the trained model was not supplied.

**Say this plainly in the dashboard.** An agent that reports "no model available;
defaulting to T1" in its own decision log is being more useful than one that
implies a choice it never made. The backtest reflects the same limitation:
`--denom T1` and `--denom K2` are separate runs, not a selector picking between
them, which is why the results appear as two tables rather than one.

---

## 4f. Could the paper's ratio be traded at another broker?

Yes. **1 long x 2 short is a ratio spread** - a standard, widely traded
structure. Nothing about it is impossible; Alpaca refuses it only because this
account is options level 3. Level 4, or an equivalent tier elsewhere, permits it.

**But the margin makes it pointless.** The uncovered contract is a naked short
call, and Reg T requires

```
premium + max(20% x underlying - out-of-money amount, 10% x underlying)
```

which on these contracts is:

| contract | OTM by | margin per naked contract |
|---|---|---|
| SPY 790 call | 17 | **$13,765** |
| SPY 797 call | 24 | $13,062 |
| SPY 806 call | 33 | $12,161 |

Our trades wanted a median of two shorts per long, so one naked contract each.
Comparing return on the capital actually committed, on the same signals:

| structure | capital tied up | mean profit | return on capital |
|---|---|---|---|
| **covered 1:1** (traded) | **$4** | $14.63 | **365.8%** |
| 1x2 ratio (paper weights) | $13,765 | $22.68 | **0.165%** |

**The covered version earns about 2,220x more per dollar committed**, and its
risk is bounded at the debit while the ratio spread's is unlimited above the
short strike.

**Why the paper does not see this.** GLP measure profit *per trade*, never profit
*per dollar of margin*. They hold fractional positions to expiration and model no
margin at all - the constraint simply does not exist in their framework. It is
the first thing that bites when the strategy meets a real account.

**The conclusion worth displaying:** the broker restriction cost nothing. It
forced the structure that is both far more capital-efficient and far less risky.
The thing that actually cost money was the bid-ask spread (section 4d).

---

## 4g. Complete results, and every reason the portfolio stayed small

**The authoritative document is `PROJECT_REPORT.md` (also `reports/PROJECT_REPORT.pdf`).**
Everything below is the short form for the dashboard; the report has the
derivations.

### Project totals

| | |
|---|---|
| rectangles considered | **192,008,262** |
| violations detected | **17,097** |
| episodes tracked | **43,566** |
| **episodes reverted** | **42,687 — 98%** |
| median time to revert | **~11 minutes** |
| decisions logged | **32,505** |
| orders placed / filled | 131 / 40 |
| completed round-trips | **21, all closed on reversion** |
| broker fees | **$0.00** |
| final equity | **$99,963** (from $100,000) |

### Full backtest matrix (SPY, 3 Sep, identical live filters, no look-ahead)

| denomination | sizing | trades | total | mean | median | win |
|---|---|---|---|---|---|---|
| **T1** | unit 1:1 | 672 | **+$9,829** | +$14.63 | +$2.00 | 67% |
| **T1** | paper weights | 386 | **+$8,754** | +$22.68 | +$1.00 | 65% |
| **T1** | 1:1 scaled (10x cap, **3.1x effective**) | 672 | **+$30,878** | +$45.95 | +$3.00 | 67% |
| **K2** | unit 1:1 | 748 | **-$5,363** | -$7.17 | $0.00 | 49% |
| **K2** | paper weights | 312 | **-$5,775** | -$18.51 | -$1.00 | 43% |
| **K2** | 1:1 scaled (10x cap) | 748 | **-$84,249** | -$112.63 | $0.00 | 49% |

**On the scaled row.** The 10x is a *cap*, not the multiple achieved. Scale is
`cap / max_loss` and max_loss is the debit, so a trade with a large debit has a
bigger potential P&L *and* a smaller permitted scale. Trades over $50 took a
median **1x**; those under took **10x**. The multiple lands on the trades that
contribute least, so the effective gain is **3.1x, not 10x**. Quoting "median
10x" beside a 3x total is true and misleading at the same time - report the
effective figure.

**Never show a backtest row without the live result beside it.** All six assume a
fill on every signal; 3% actually filled, for **-$45** realised.

Why paper weights trade less — one gate, and the difference matches exactly:

| filter | unit rejects | weighted rejects |
|---|---|---|
| theory gate | 3,579 | 3,579 |
| cheapest leg | 95 | 95 |
| coverage ratio | 67 | 67 |
| **max loss > 2.5% equity** | **76** | **362** |
| survived | **672** | **386** |

A covered 1:1 spread cannot lose more than its debit (median **$2**). One naked
short makes the loss width-scaled (median **$700**) - 350x the measured risk - so
the cap refuses it.

### Every reason the portfolio stayed small, ranked

1. **The edge and its collection cost are the same size.** A ~2c mispricing that
   costs ~2c to cross twice. *This explains the loss by itself.*
2. **Adverse selection.** A resting limit buy fills only when the market comes
   down to it; 3% filled, and those are the ones already moving against us.
3. **The indicative feed is not the market.** At a 10% spread screen: 21,109
   contracts measured, **zero violations**.
4. **Two of three underlyings were untradeable.** No greeks at any strike for SPX
   or XSP; SPX quoted fully-ITM spreads at 57-68% of intrinsic.
5. **The dividend horizon** correctly blocked 1,969 SPY candidates whose far leg
   expired past 21 September.
6. **Options level 3 forced 1:1** - which **cost nothing and helped** (section 4f).
7. **~1.5 sessions** of live trading; live began nine minutes before a close.
8. **Defects found while live** - fourteen, catalogued in the report.

**None of these say the strategy is wrong.** 98% of violations reverted, on
schedule, and all 21 live positions closed on their thesis.

---

## 5. Suggested pages

1. **Overview** — equity, realised P&L, positions open, round-trips today, win
   rate. Big numbers.
2. **Funnel** — from `scans.csv`: rectangles considered → measured → detected →
   executable → traded. Shows most candidates are *refused*, which is the point.
3. **Episodes** — histogram of `time_to_revert_seconds`; scatter of
   `peak_severity` vs time-to-revert; a picker to draw one episode's severity
   path from `episode_path.csv`.
4. **Decisions** — `outcome` counts, and a breakdown of `risk.rejections[].code`
   and `reason`. The most distinctive page: the agent explaining its refusals.
5. **Trades** — `positions.jsonl` as a table with entry/exit and `close_reason`;
   note **all closes so far are `reverted`**.
6. **Narrations** — render `reports/*_narration.md` with `st.markdown`. See
   section 4b: **no API key is needed for this.**

---

## 6. Key numbers to surface (2026-09-03 session)

- 17 orders filled, **17 round-trips, every one closed `reverted`**
- Realised P&L **−$28**; equity ~$99,971 from $100,000
- Backtest through the same filters: unit 1:1 **640 trades, +$4,702, 66% win**;
  paper weights **293 trades, +$2,587, 58% win** (`reports/BACKTEST_SIZING.md`)
- At a 10% spread screen, **21,109 contracts measured, zero violations** —
  violations on this feed exist only where spreads are wide
- Alpaca publishes greeks for only **75 of 244** SPY contracts on one expiry

---

## 7. Honest caveats to display, not hide

- Detection runs on Alpaca's **indicative** feed (model-derived), not OPRA.
  Violations are measurements of that feed, not claims about the market.
- **Sizing is 1:1**, not the study's weights. The study's are always short-heavy
  and need naked calls; the account is options level 3 and Alpaca returns
  `HTTP 403 / 40310000` for them.
- SPX detected 34,936 violations and **filled nothing** — its quotes price
  fully-ITM spreads below intrinsic.
- Backtest figures assume every surviving episode filled at quoted prices. Live,
  17 filled. That gap is a result, not an error.

---

## 8. Practical notes

- Everything is **stdlib-readable**: `csv` + `json`. Only Streamlit and a plot
  library are new dependencies. `pandas` is fine but not required by the repo.
- **Timestamps are ISO local time** in CSVs (`2026-09-03T14:39:01`) but **UTC in
  broker responses** (`2026-09-03T18:39:01Z`). Converting one to the other caused
  two real bugs in this project — convert explicitly.
- Booleans in CSVs are the **strings** `"True"`/`"False"`, not `0`/`1`.
- Files are appended to while scanners run; read with `st.cache_data(ttl=60)` and
  never hold a handle open.
- Numeric columns can be empty or `nan` — coerce with a helper that returns
  `None` rather than raising.
- Suggested run: `streamlit run app.py` from the repository root so the relative
  `dashboard_data/` paths resolve. Add `streamlit` and `pandas` to a
  `requirements.txt`; the repo itself needs nothing else.
- The snapshot is static — it does not update. Treat it as the record of the
  2026-09-03 session rather than a live feed, and say so in the UI.
