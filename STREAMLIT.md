# Streamlit dashboard — build brief

Everything needed to build a Streamlit app for this project **without reading the
codebase**. All paths are relative to the repository root
(`~/Downloads/Lablab/tp2-agent`).

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

All under `data/<UNDERLYING>/` where UNDERLYING ∈ {SPY, SPX, XSP}.
**Ignore `*.bak` and `scans_pre_delta_screen.csv`** — superseded.

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

## 4. Live account data (optional, needs credentials)

Credentials live in `.env` (**gitignored, never commit or display**):
`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`.

```python
import os, urllib.request, json
H = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
     "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}
def get(path):
    r = urllib.request.Request("https://paper-api.alpaca.markets" + path, headers=H)
    return json.loads(urllib.request.urlopen(r, timeout=15).read())
account   = get("/v2/account")     # equity, cash, options_approved_level
positions = get("/v2/positions")
orders    = get("/v2/orders?status=all&limit=200")
```
**Paper host only.** Never call `api.alpaca.markets`. Wrap in try/except — the
dashboard must render from CSVs alone if the API is unreachable.

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
6. **Narrations** — render `reports/*_narration.md` (LLM-written) with
   `st.markdown`.

---

## 6. Key numbers to surface (2026-09-03 session)

- 17 orders filled, **17 round-trips, every one closed `reverted`**
- Realised P&L **−$28**; equity ~$99,971 from $100,000
- Backtest through the same filters: unit 1:1 **640 trades, +$4,702, 66% win**;
  study weights **293 trades, +$2,587, 58% win** (`reports/BACKTEST_SIZING.md`)
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
  `data/` paths resolve.
