# TP2 Agent — Build Checklist

**Deadline:** Fri 4 Sep 2026 · **Sessions left:** Mon 31 Aug, Tue 1, Wed 2, Thu 3, Fri 4
**Rule:** explain → build → test → tick. Nothing ticks without a passing test.

---

## Components

### 1. Theory gate — Propositions 2.1–2.2 ✅ DONE

- [x] `src/tp2agent/theory_gate.py` — zero third-party dependencies
- [x] Prop 2.1(i): no ex-date in $(t_0, T_2]$ → `NO_DISTRIBUTION`
- [x] Prop 2.1(ii): per-contract zero-premium condition, all four legs required
- [x] Prop 2.2: certificate $V^q > M := \bar D_2 D^b + \bar D_1 C^b$
- [x] Four-way cascade → `NO_DISTRIBUTION` / `DIVIDEND_SPANNING` / `DIVIDEND_BOUND` / `UNRESOLVED`
- [x] Dividend provenance guard — amounts not announced by the signal date are discarded
- [x] JSON audit record via `GateResult.to_record()`
- [x] **17/17 tests pass**, including the paper's published break-even horizons

**Verification:** `python3 tests/test_theory_gate.py`

The load-bearing test is `test_break_even_horizon_matches_paper`: at $\delta/K = 0.003$ the
condition must flip at **55 days for $r=2\%$** and **22 days for $r=5\%$**, matching the
figures printed in the paper. It passes.

**Design constraint this surfaced.** A realistic SPY distribution (~$1.80 against a ~$640
strike) needs **~21 calendar days** between the ex-date and expiry at $r=5\%$ before
Prop 2.1(ii) can hold. A near leg expiring inside that window can *never* be certified
zero-premium. → **The scanner must not build rectangles whose $T_1$ sits just past an
ex-dividend date.** Captured in `test_near_leg_too_close_to_ex_date_fails`.

---

### 2. Live Alpaca chain adapter 🟡 IN PROGRESS

**Startup probe written** — `scripts/probe_account.py`, read-only, GET-only, audited:
the sole method reference in the file is `method="GET"`; no POST/PUT/DELETE, no
`/orders` path, and `_get` refuses the live trading host outright.
**Awaiting a run with real credentials.**

- [x] Paper-endpoint assertion; live host refused by `_get`
- [x] OPRA vs indicative feed detection (probe section 2)
- [x] Account census: equity, options level, buying power (probe section 1)
- [x] Universe census for target expiries (probe section 3)
- [x] Quote-health and field-availability census (probe sections 4-5)
- [x] **PROBE RUN — 30 Aug 2026.** Results below.
- [x] Account verified: **$100,000.00 equity, options level 3, buying power $400k,
      ACTIVE, not blocked.** Meets every competition requirement.
- [x] Universe verified: 16 Oct **221 calls** (24,310 pairs), 20 Nov **141**,
      18 Dec **158**. Strikes 300–1000. Three usable expiries.
- [x] Open interest **empirically confirmed ABSENT** (0/442) — `feature_D_open_interest`
      is dead, as the schema review predicted.
- [x] Greeks + IV present on 376/442 (85%).
- [ ] **BLOCKER: OPRA denied — HTTP 403 "OPRA agreement is not signed."**
      Note this is an *agreement*, not a subscription error. Sign it in the Alpaca
      dashboard (Market Data / Agreements), then re-run the probe. If it still
      denies afterwards, it is the $99/mo Algo Trader Plus decision.
- [ ] Re-run probe after signing; record final feed mode

**Resolved feed mode: SHADOW (indicative)** — pending the agreement above.

⚠️ **Do not tune screens on the current quote-health numbers.** 93.9% usable under a
50% spread screen, median relative spread 3.0%, p90 22.2% — but these are *indicative*
quotes (Alpaca-derived, 15-min delayed) sampled with markets closed. Real OPRA NBBO on
far-dated SPY calls will be materially wider. The paper's joint screens left 5–7% of
candidates; 93.9% is not comparable and almost certainly an artefact of derived quotes.
- [ ] SPY option chain fetch — **calls and puts** (puts needed for the parity forward)
- [ ] Quote-quality gate: positive bid, not crossed, size, relative spread, staleness
- [ ] Universe filter: both $T_1$ and $T_2$ **after 4 Sep**; $T_1$ clear of the ex-date
      by the ≥21-day margin from component 1
- [ ] Rate-limit-aware polling (200/min free, 10k/min paid)

---

### 3. Rectangle builder + strong-violation test ✅ DONE

- [x] `src/tp2agent/rectangles.py` — zero third-party dependencies
- [x] Parity-implied forwards, cross-sectional **median** over the ±5% band, min-5-strike guard
- [x] Refuses (returns `None`) rather than guessing when too few strikes qualify
- [x] $\tilde K_1 = K_1 F_{T_2}/F_{T_1}$, $\tilde K_2 = K_2 F_{T_1}/F_{T_2}$, rounded **up** to listed
- [x] Strong-violation test $A^a B^a < C^b D^b$
- [x] **Tick-quantisation bound** computed per rectangle; violation must clear it plus a buffer
- [x] Coverage ratio $C_w/B_w$ emitted and screened at 1.25 *before* detection
- [x] Full drop census — every considered rectangle accounted for
- [x] Episode dedup keyed on the near-leg contract
- [x] **16/16 tests pass**

**Verification:** `python3 tests/test_rectangles.py`

The specificity test is `test_black_scholes_chain_yields_no_violations`: Black–Scholes
prices satisfy TP2, so a synthetic BS chain must yield **zero** detections across
>100 considered rectangles. It does. If the detector ever fires there, it is broken.

**Finding 1 confirmed empirically.** On a clean BS chain the coverage ratio
$\text{price}(C)/\text{price}(B)$ came out at **1.113** for the $K_1{=}635, K_2{=}645$
rectangle — above 1, as the algebra predicts, on every rectangle. The screen works:
an attempt to inject a violation by inflating leg $C$ drove the ratio to **3.34** and
was correctly rejected before detection.

---

### 4. Coverage-capped position construction ✅ DONE

- [x] `src/tp2agent/position.py` — zero third-party dependencies
- [x] Theoretical weights $B_w, C_w$; short-heaviness asserted and logged
- [x] Integer ratio search; defaults to 1:1, the only always-covered form
- [x] Uncovered ratios **refused**, never sent
- [x] **FOUR_LEG** (default): long A, long B, short C, short D — two covered verticals
- [x] **TWO_LEG**: kept for comparison, flagged as a directional debit spread
- [x] Economics recomputed **after** rounding: entry cash, max loss, commissions
- [x] Riskless-credit-spread guard (stale-quote detector)
- [x] Alpaca MLeg leg payload shape; ≤4 legs enforced
- [x] **17/17 tests pass**

**Verification:** `python3 tests/test_position.py`

**The economics change, and it is disclosed on every spec.** Capping shorts at longs
destroys the paper's credit-equals-violation property. The TWO_LEG form becomes a
*debit bull call spread* — directionally long, not a TP2 trade. FOUR_LEG is the
default because it keeps all four contracts, is fully covered at both expiries, and
has a bounded worst case. Every `PositionSpec` carries a note saying the paper's
construction was not preserved; `test_capping_note_is_always_present` enforces it.

---

### 5. Deterministic risk gates ✅ DONE

- [x] `src/tp2agent/risk.py` — zero third-party dependencies
- [x] **16 stable `RejectCode` values** for the audit log and narration
- [x] **Re-validation gate**: violation re-tested on fresh quotes before send —
      `VIOLATION_GONE` if corrected, `VIOLATION_DECAYED` below 50% retained,
      and the fresh violation must still clear its tick bound
- [x] Quote staleness bound (30s default)
- [x] Per-trade cap 0.25% equity, **commissions included** in the figure
- [x] Aggregate cap 1% equity; buying-power check
- [x] Daily stop 0.75%; kill switch; reconciliation-failure halt
- [x] Position count cap; **duplicate-leg-exposure** check (wash-trade protection)
- [x] Entry cutoff 15:55 and entry deadline Thu 3 Sep; deadline outranks cutoff
- [x] Theory categories gated — `UNRESOLVED` cannot trade
- [x] `should_flatten` deliberately **independent of entry gates**
- [x] **26/26 tests pass**

**Verification:** `python3 tests/test_risk.py`

**Design note.** Every gate runs and every failure is collected — one rejection
never masks another, so the audit record shows all the reasons a trade was refused.
Enforced by `test_every_failure_is_collected_not_just_the_first`.

---

### 6. LightGBM $F^*_{46}$ retrain ⬜

- [ ] Load `tp2_t1k2_token_dataset.parquet` (354,974 × 199)
- [ ] Drop `lc_pressure_max_le0` and `feature_D_open_interest` (both unavailable live)
- [ ] Retrain with the repo's existing walk-forward folds
- [ ] Isotonic calibration on the validation year
- [ ] Feature-parity test: live vector vs stored episode vector, same order, same scaling
- [ ] Ship as **rank score**, never displayed as a probability (0.695 stated vs 37.7% realised)

---

### 7. LLM narrator + audit log ⬜

- [ ] Append-only audit log: quotes, determinant, category, score, every gate result, request ID
- [ ] Narrator reads the log and writes a human-readable decision record
- [ ] **No return path into the pipeline** — assert the narrator cannot mutate any decision
- [ ] Abstention narration (the strongest demo material)

---

## Operational milestones

- [ ] **Coverage probe** — submit a 2:3 long:short MLeg far from market, confirm rejection
- [ ] Confirm dedicated paper account: $100,000, options level 3
- [ ] **Settle OPRA** — $99/mo Algo Trader Plus, or run labelled shadow mode
- [ ] **Verify the SPY September ex-dividend date and amount** — direct input to component 1
- [ ] Ask Discord: exact P&L measurement timestamp, open-position treatment, flat-by-deadline?
- [ ] Mon 31: shadow run, count survivors at every gate
- [ ] Tue 1: first live entries, 15:45–15:55 ET
- [ ] Thu 3 evening: **record the video**
- [ ] Fri 4 morning: flatten, export audit trail, submit

---

## Slip rule

If the theory gate is not driving a live scanner by **Monday close**, cut component 6
entirely and ship components 1–5 + 7 with a transparent rule-based score.
**The gate alone is a complete submission. The model alone is not.**

---

## Open questions

| Question | Blocks | Status |
|---|---|---|
| P&L measurement timestamp / open positions | exit policy | unanswered |
| SPY Sep 2026 ex-date + amount | component 1 inputs | **unverified — assumed ~18 Sep** |
| OPRA entitlement | live mode vs shadow | undecided |
| MLeg coverage rejection on ratio orders | component 4 | probe not yet run |
