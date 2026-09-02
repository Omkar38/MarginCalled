# The path, start to end

How a quote becomes a trade — or, far more often, becomes a recorded refusal.

This is the map of the whole system: every stage, what it does, what it throws
away, and where the evidence lands. Read top to bottom and you have followed a
single option quote from Alpaca's feed to a narrated decision.

---

## 0. What the agent is actually betting on

A call price surface must satisfy **total positivity of order 2 (TP2)**. For two
expiries $T_1 < T_2$ and two strikes with $K_1/F_{T_1} < K_2/F_{T_2}$:

$$C(K_1,T_1)\,C(K_2,T_2) \;\ge\; C(\tilde K_1,T_2)\,C(\tilde K_2,T_1)$$

where the strikes are **forward-adjusted** so both sides compare like with like:

$$\tilde K_1 = K_1 \frac{F_{T_2}}{F_{T_1}}, \qquad \tilde K_2 = K_2 \frac{F_{T_1}}{F_{T_2}}$$

Four contracts form a **rectangle**:

| label | contract | role |
|---|---|---|
| **A** | $(K_1, T_1)$ | near expiry, lower strike |
| **B** | $(K_2, T_2)$ | far expiry, higher strike |
| **C** | $(\tilde K_1, T_2)$ | far expiry, adjusted lower strike |
| **D** | $(\tilde K_2, T_1)$ | near expiry, adjusted higher strike |

When $A\cdot B < C\cdot D$ the inequality is violated. The bet is that the
violation **reverts** — the surface repairs itself — and we exit into that.

We trade two of the four denominations from the source study, both of which
short D:

- **T1** — buy A, sell D. Both at $T_1$, so one expiry.
- **K2** — buy B, sell D. Spans expiries.

**SPX trades T1 only. SPY may trade either**, chosen per candidate by a model.
That split is not a preference — it is enforced by the broker, see stage 6.

---

## 1. Market data — `alpaca.py`

Chain snapshots from Alpaca. Two feeds exist and the difference matters:

- **OPRA** — the real consolidated tape. Requires Algo Trader Plus ($99/mo).
  **Currently denied** (403, agreement unsigned).
- **indicative** — Alpaca's own model-derived quotes, 15-minute delayed.

We run on **indicative**, and the whole execution design follows from that one
fact. A determinant computed on indicative quotes measures Alpaca's derivation,
not the market. This feed has been observed offering quotes that breach the
vertical no-arbitrage bound outright — $17.38 on a $10 spread — so it is not
internally consistent.

**Forwards** are put–call parity implied, taken as a cross-sectional *median*
over strikes within ±5% of spot, requiring at least 5 qualifying strikes. If too
few qualify the module returns `None` rather than guessing a forward.

> Written: `scans.csv` (31 cols) — one row per scan, with the full drop census.

---

## 2. Rectangle construction — `rectangles.py`

Every $(K_1,T_1),(K_2,T_2)$ pair with $T_1 < T_2$ and the moneyness ordering.
Strikes are forward-adjusted and **rounded up** to listed strikes, which is the
conservative direction: rounding up raises the right-hand side, so it can only
make a violation harder to declare, never easier.

Then a **strong violation** test on the sides a trade must actually cross:

$$A^{\text{ask}} B^{\text{ask}} < C^{\text{bid}} D^{\text{bid}}$$

Two screens that are easy to get wrong:

- **Tick bound.** Quotes are quantised. A "violation" smaller than the
  quantisation error is an artefact. The exact bound is
  $\prod_i (1 + h/p_i) - 1$, not the first-order sum — the linearised form
  understated the error by 0.75%.
- **Detection is separated from execution.** Coverage ratio, price floors and
  moneyness bands are *execution* questions. Applying them during detection once
  removed 20,000 of 80,000 rectangles and took detections from 409 to 0. A
  rectangle that violates TP2 but cannot be traded is still a violation and is
  recorded as one.

> Written: `violations.csv` (67 cols) — per-leg quotes, greeks, IV, sizes.
> Alpaca keeps no history of these, so a scan that discards them destroys the
> only copy.

---

## 3. Episode tracking — `episodes.py`

A violation is not an event, it is an **episode** with a life. Each rectangle is
keyed on all four legs and re-priced every scan whether or not it is detected
again — which is what makes reversion *observable* rather than inferred from
absence. Peak severity and duration are tracked to reversion.

State persists across restarts. It did not always: an early version erased
itself on every restart and lost 27 SPX episodes.

> Written: `episodes.csv`, `episode_path.csv` — the full path of each episode.

---

## 4. The theory gate — `theory_gate.py`

TP2 is a theorem about **European** calls. SPY options are American, so before
trading one we must establish that early exercise carries no premium. Four ways
to pass, one to fail:

| category | meaning |
|---|---|
| `EUROPEAN_NATIVE` | SPX, XSP — cash-settled European, no question to answer |
| `NO_DISTRIBUTION` | no dividend in the window, and we can *prove* the absence |
| `DIVIDEND_SPANNING` | Prop 2.1(ii) holds: $\delta_i + \sum_{j>i}\delta_j e^{-r(t_j-t_i)} \le K(1-e^{-r(T-t_i)})$ |
| `DIVIDEND_BOUND` | Prop 2.2: the violation exceeds the dividend bound $M$ |
| `UNRESOLVED` | **cannot trade** |

The critical subtlety: **an empty dividend list is not evidence of no dividend.**
The gate tracks a *dividend horizon* — the date through which absence is
assertable — and anything past it is `UNRESOLVED`, not `NO_DISTRIBUTION`.

> This is why SPY currently yields no trades: the September dividend is
> undeclared, the horizon ends 2026-09-21, and every SPY violation is
> `UNRESOLVED`. It is the gate working, not failing.

---

## 5. Denomination selection — `position.py` + `features.py`

Only where more than one denomination is submittable, i.e. **SPY**.

`features.py` rebuilds the study's **F\*** feature vector live. The definitions
were verified bit-exact against the study's own 354,974-row dataset rather than
against our reading of it, which changed three of them:

- `feature_severity`, `feature_rhs`, `feature_normalized_severity` are built
  from **mid** prices — *not* the crossable ask/bid products.
- `feature_bidask_executable_ratio` **is** crossable-quote based. The feature set
  genuinely mixes both bases.
- That ratio carries the study's own `+1e-9` denominator guard, which is baked
  into what the model was fitted on.

**46 of the 48 features are available live.** The two that are not —
`lc_pressure_max_le0` (needs pre-signal daily history) and
`feature_D_open_interest` (Alpaca reports none) — carry 2.0% of permutation
importance and 0.9% of gain, both at stability 0.25. They are **dropped, not
imputed**: feeding a constant for a feature the model learned a response to is
its own distribution shift. `F_STAR_LIVE` is the 46-name list that training and
inference must share.

**A rectangle has two feature vectors, not one.** T1 reads legs (A, D), K2 reads
(B, D). F\* contains no `is_t1`/`is_k2`/`strategy_id`, so the two rows differ in
exactly two features — which together carry 18.8% of permutation importance.
Those two numbers are the model's only channel for knowing which trade it is
scoring.

`DenominationSelector.choose()` returns a `Choice` that **may abstain**: below
the 0.5 threshold, and also when the feature vector cannot be built. A missing
greek means we do not know what we would be scoring, and defaulting to T1 there
would present an absence of information as a prediction.

> **Open:** the trained scorer itself. Everything else is wired — pass any
> callable taking a 46-float vector and returning a probability.

---

## 6. Position construction — `position.py`

Weights come from the paper, then meet reality:

- **Whole contracts only.** The paper is a theoretical analysis and takes
  fractional positions; a live market does not. Enforced at the payload
  boundary, so a fractional ratio cannot reach Alpaca.
- **Covered structures only.** An uncovered ratio is refused, never sent.

Three broker constraints, all verified live against the competition account:

| structure | SPY | SPX |
|---|---|---|
| four-leg 1:1:1:1 | accepted | **422 / 42210000** |
| K2 (spans expiries) | accepted | **422 / 42210000** |
| T1 (same expiry) | accepted | accepted |
| uncovered 2:3 | **rejected** | **rejected** |

Alpaca refuses a multi-leg order whose European legs span different expirations.
That is the entire reason **SPX is T1-only** — a broker fact, not a modelling
choice.

---

## 7. Risk gates — `risk.py`

**16 stable reject codes.** Every gate is evaluated and every failure collected —
no short-circuit — so the log shows all the reasons a trade was refused, not
just the first.

- Per-trade cap 0.25% of equity, aggregate 1%, daily stop 0.75%
- Quote staleness bound
- Duplicate-leg exposure (wash-trade protection)
- Position count cap, buying-power check
- Entry cutoff, and a deadline that outranks it
- `UNRESOLVED` theory category cannot trade
- **Pre-send revalidation** — the violation is re-tested on fresh quotes
  immediately before submission (`VIOLATION_GONE`, `VIOLATION_DECAYED`)

`should_flatten` is deliberately **independent of the entry gates**: the
conditions for getting out must not depend on the machinery for getting in.

> The per-trade cap currently binds hard on SPX: median 1-lot max loss ~$11,940
> against a $250 cap, so only ~5% of SPX signals are tradable at present limits.

---

## 8. Execution — `executor.py` + `mcp_client.py`

Orders go out over **MCP** by default — the competition requires the Trading API
plus MCP or the CLI, and routing through MCP removes the argument. REST is a
fallback.

The central design idea turns the data weakness into a safety property:

> The signal is synthetic. The fill is real. Alpaca's paper engine matches
> against real NBBO, so a market order would convert a phantom signal directly
> into a real position.

So **every order is a limit order priced conservatively against the indicative
quote** — we demand terms strictly better than the phantom quote implies, shaded
in units of the package's own spread. If the real market is genuinely that good,
we fill at a price we modelled. If the quote was an artefact, the order is simply
not marketable.

**Bad data produces non-fills, not bad fills.** `type: "market"` is never
constructed, and a test asserts the string does not appear in the module.

Sign convention: Alpaca MLeg treats **positive as debit, negative as credit**.
Taking `abs()` would submit a credit spread as a debit — an order to pay what we
meant to receive.

---

## 9. Exits — `exits.py`

Entry is half the strategy. Triggers, in order of authority:

1. **DEADLINE** — the contest cutoff. Outranks everything.
2. **EXPIRY_NEAR** — the near leg is about to expire and stops behaving like the
   instrument the thesis was written about.
3. **REVERTED** — the determinant no longer violates. *This is the thesis
   resolving as intended.*
4. **TIME_STOP** — held too long without reverting.
5. **HOLD** — including when the episode status is unknown. An unknown state is
   not evidence the thesis resolved.

Closing inverts the entry as **one multi-leg order**. Legging out would leave a
naked short between fills — exactly the exposure the covered structure exists to
avoid.

---

## 10. The decision log — `audit.py`

Append-only JSONL. No update, no delete, no rewrite; a correction is a new
record. `orders.jsonl` records what was *sent*, which is a survivorship-biased
view — it cannot show the candidate that was detected and refused, which is
almost all of them.

Every considered candidate gets a record carrying the quotes, the determinant,
the theory category, the selector's scores, and **every risk gate that ran —
passes as well as failures**.

> Written: `decisions.jsonl`

---

## 11. The narrator — `narrator.py`

The AI reporting layer. **Records flow in, text flows out, nothing else.**

- It imports nothing from the decision path.
- Nothing in the decision path imports it.
- Both directions are asserted by tests.
- The model is given **no tools**, and its output is written to a file the
  trading path never opens.

If it hallucinates outright, the trades that happened are still exactly the
trades the deterministic gates approved. **The narration is wrong; the book is
not.**

`TemplateNarrator` is the default — deterministic, no key, no network, no
failure mode. `LLMNarrator` calls Claude for better prose and **falls back to the
template on any failure**, because a narration failure must never look like a
trading failure.

Narration exists because the agent's interesting behaviour is *refusal*. An agent
that can say precisely why it did not trade is demonstrating more than one that
only reports fills.

```
python3 scripts/narrate.py --data-dir data/SPY
python3 scripts/narrate.py --data-dir data/SPY --llm --out reports/SPY_narration.md
```

---

## Running it

```bash
caffeinate -ims python3 -u scripts/run_scanner.py \
    --underlying SPY --interval 300 --market-hours \
    --min-dte 0 --max-dte 400 --max-expiries 40 --trade
```

Identical for SPY, SPX and XSP. Add `--live-orders` to stop dry-running.

**Scan the whole expiry universe.** The CLI defaults (`--min-dte 30
--max-dte 150 --max-expiries 3`) look harmless and are not: they were costing
roughly 44x the SPY signal and 5x the SPX, and `--min-dte 30` in particular
excluded every SPY expiry inside the dividend horizon, which is exactly the set
the theory gate can actually resolve. Narrowing the date range to work around a
gate is the wrong instinct - the gate already filters correctly, and the
rectangles it refuses are still worth recording.

One scan of the full universe, per underlying:

| | expiries | violations | executable | risk-refused | orders |
|---|---|---|---|---|---|
| SPY | 29 | 265 | 63 | 0 | 2 |
| SPX | 13 | 796 | 437 | 35 | 5 (capped) |
| XSP | 36 | 155 | 58 | 0 | 5 (capped) |

Cost is 25-33 seconds per scan against a 300-second interval, so the full
universe is affordable and the narrow default bought nothing.

**XSP is not redundant with SPX.** It is the same S&P 500 index at one tenth the
notional, which changes its risk profile entirely rather than duplicating it:
SPX had 35 candidates refused on `max_loss_per_trade` in a single scan and XSP
had none, because a tenth-size contract sits inside the per-trade cap where the
full-size one does not.

---

## The funnel

```
   chain snapshot                    alpaca.py        scans.csv
        |
        v
   all rectangles  ~44,000           rectangles.py
        |
        |  forward-adjust, round up, quote quality
        v
   strong violations                 rectangles.py    violations.csv
        |
        |  tick bound
        v
   tracked episodes                  episodes.py      episodes.csv
        |
        |  early-exercise certificate   <-- SPY stops here today (UNRESOLVED)
        v
   theory-gated
        |
        |  execution screen: coverage, leg price
        v
   tradable
        |
        |  T1 or K2, or abstain            <-- SPY selector: model pending
        v
   sized position                    position.py
        |
        |  16 risk gates, all evaluated
        v
   approved
        |
        |  conservative limit, MCP
        v
   order  ---> fill ---> position ---> reversion ---> exit
        |
        +--> every step above also writes here:  decisions.jsonl
                                                       |
                                                       v
                                                  narrator.py
```

Every branch that does *not* continue downward is recorded. That is the point.

---

## State

**Built** — 13 modules, 4,842 lines, **241 tests passing**, standard library
only in the core.

| module | lines | tests |
|---|---|---|
| `rectangles.py` | 670 | 28 |
| `position.py` | 518 | 31 |
| `alpaca.py` | 517 | 14 |
| `episodes.py` | 515 | 20 |
| `theory_gate.py` | 402 | 20 |
| `risk.py` | 364 | 27 |
| `exits.py` | 309 | 14 |
| `narrator.py` | 295 | 16 |
| `executor.py` | 295 | 18 |
| `mcp_client.py` | 275 | 9 |
| `store.py` | 262 | — |
| `features.py` | 252 | 18 |
| `audit.py` | 168 | 11 |

**Collected so far** — SPY 289 violations / 223 episodes, SPX 2,790 / 2,603,
XSP 3 / 3, across ~89 scans each.

**Verified live against the competition account** — $100,000 equity, options
level 3; uncovered 2:3 rejected; four-leg accepted on SPY; SPX four-leg and K2
rejected with 422/42210000; buying power drawn on *fill*, not acceptance; options
buying power is $100,000, not the $400,000 headline.

**Left**

1. **The SPY T1/K2 scorer** — the trained model. Everything around it is wired.
2. **Trading decision** — the account is flat at exactly $100,000 with 0
   positions, and P&L is a primary judging criterion. Gated on the per-trade cap
   for SPX and on the dividend horizon for SPY.
3. **One-page write-up** — AI logic, deterministic risk gates, Alpaca
   infrastructure.
4. **Demo video ≤5 min, 16:9 cover, pitch deck, title/description/tags.**

**Known limitations, stated plainly**

- Detection runs on the **indicative feed**, which is not internally consistent.
  Violations are logged as measurements of that feed, not claims about the market.
- SPY is entirely `UNRESOLVED` until the September dividend is declared.
- Alpaca returns **no greeks or IV for SPX at all** (0 of 2,158 rows, where SPY
  is 195/195). This does not block SPX, which is T1-only and never consults the
  model — but no SPX feature vector can be built.
- No open interest in the snapshot, for any underlying.
