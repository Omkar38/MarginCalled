---
title: "MarginCalled — TP2 Arbitrage Agent"
subtitle: "Alpaca AI Trading Agents Hackathon, 28 Aug – 4 Sep 2026"
date: "4 September 2026"
geometry: margin=2.2cm
fontsize: 10pt
colorlinks: true
---

# 1. What this project is

An autonomous options-trading agent that detects violations of a **mathematical
property that call prices must satisfy**, trades them, and exits when the
violation corrects.

It is not a forecasting system. It makes no prediction about where the market
goes. It looks for prices that are internally inconsistent — arrangements that
cannot all be right at once — and takes the other side until the inconsistency
resolves.

Over the competition it scanned **192 million rectangles**, detected **17,097
violations**, tracked **43,566 episodes** to resolution, and placed **131 orders**
of which **40 filled**, producing **21 completed round-trips — every one of which
closed because the violation reverted**, exactly as the theory predicts.

Final paper-account equity: **\$99,963 from \$100,000**. A loss of **\$37**.

This report explains what was built, what happened, and — in detail — **why the
loss occurred**, because the reason is precise, measurable, and is the most
useful finding the project produced.

---

# 2. The theory, and why it matters

## 2.1 Total positivity of order 2

For a call option surface to be free of static arbitrage, prices must satisfy
**TP2**. For two expiries $T_1 < T_2$ and two strikes ordered by forward
moneyness $K_1/F_{T_1} < K_2/F_{T_2}$:

$$C(K_1,T_1)\,C(K_2,T_2) \;\ge\; C(\tilde K_1,T_2)\,C(\tilde K_2,T_1)$$

where the strikes are **forward-adjusted** so both sides compare like with like:

$$\tilde K_1 = K_1\frac{F_{T_2}}{F_{T_1}}, \qquad \tilde K_2 = K_2\frac{F_{T_1}}{F_{T_2}}$$

Four contracts form a **rectangle**:

| label | contract | role |
|---|---|---|
| **A** | $(K_1, T_1)$ | near expiry, lower strike |
| **B** | $(K_2, T_2)$ | far expiry, higher strike |
| **C** | $(\tilde K_1, T_2)$ | far expiry, adjusted lower strike |
| **D** | $(\tilde K_2, T_1)$ | near expiry, adjusted higher strike |

When $A \cdot B < C \cdot D$ the inequality fails: a **violation**.

## 2.2 Why this is worth trading

The property is not a model. It does not assume Black–Scholes, or any particular
dynamics. It follows from the requirement that the surface admit *no arbitrage at
all*. Glasserman, Li & Pirjol show it holds "for all strikes $K$ and expiries
$T > 0$" in Black–Scholes and in many models beyond it.

So a violation is not a signal that something is mispriced *relative to a model*.
It is a signal that **the prices cannot all be correct simultaneously**. That is a
far stronger statement, and it is why the trade is a bet on *reversion* rather
than on direction.

**Empirically, that bet was correct.** Of 43,566 tracked episodes, **42,687
reverted — 98%** — with a median time to reversion of **11 minutes**. The
mispricings are real and they correct quickly.

## 2.3 The critical detail: crossable prices

The agent tests the inequality on the prices a trade must actually **cross**:

$$A^{ask} \cdot B^{ask} < C^{bid} \cdot D^{bid}$$

Buying at the ask and selling at the bid means the cost of crossing the spread is
already inside the signal. This matters enormously and is revisited in §7.

---

# 3. What was built, in order

The project was built in ten stages, each verified before the next began.

## Stage 1 — Market data (`alpaca.py`, 517 lines)

Chain snapshots from Alpaca. Forwards are **put–call parity implied**, taken as a
cross-sectional median over strikes within ±5% of spot, requiring at least five
qualifying strikes. If too few qualify the module returns `None` rather than
guessing.

**Data limitation, established early and never resolved:** real-time OPRA
requires a \$99/month subscription that was denied (`403`). All detection runs on
Alpaca's **indicative** feed — model-derived quotes, not the consolidated tape.
This shaped every subsequent decision.

## Stage 2 — Rectangle construction (`rectangles.py`, 670 lines)

Every $(K_1,T_1),(K_2,T_2)$ pair with $T_1 < T_2$ and the correct moneyness
ordering. Strikes forward-adjusted and **rounded up** to listed strikes — the
conservative direction, since rounding up raises the right-hand side and can only
make a violation harder to declare.

Two screens matter:

- **Tick bound.** Quotes are quantised; a "violation" smaller than the
  quantisation error is an artefact. The exact bound is $\prod_i(1 + h/p_i) - 1$,
  not the first-order sum — the linearised form understated the error by 0.75%.
- **Detection is separated from execution.** Coverage ratios, price floors and
  moneyness bands are *execution* questions. Applying them during detection once
  removed 20,000 of 80,000 rectangles and took detections from 409 to zero.

## Stage 3 — Episode tracking (`episodes.py`, 515 lines)

A violation is not an event but an **episode with a life**. Each rectangle is
keyed on all four legs and **re-priced every scan whether or not it is detected
again** — which is what makes reversion *observable* rather than inferred from
absence.

## Stage 4 — The early-exercise gate (`theory_gate.py`, 402 lines)

TP2 is a theorem about **European** calls. SPY options are American, so before
trading one the agent must certify that early exercise carries no premium.

| category | basis |
|---|---|
| `EUROPEAN_NATIVE` | SPX, XSP — cash-settled, no question to answer |
| `NO_DISTRIBUTION` | no dividend in the window, **and the absence is provable** |
| `DIVIDEND_SPANNING` | Proposition 2.1(ii) holds |
| `DIVIDEND_BOUND` | Proposition 2.2: the violation exceeds the dividend bound |
| `UNRESOLVED` | **cannot trade** |

The subtlety that matters: **an empty dividend list is not evidence of no
dividend.** The gate tracks a *horizon* — the date through which absence is
assertable — and anything beyond it is `UNRESOLVED`. On SPY this rejected 1,969
candidates whose far leg expired past 21 September, correctly.

## Stage 5 — Position construction (`position.py`, 518 lines)

Whole contracts only, covered structures only. Uncovered ratios are refused
rather than sent. See §6 for why this constraint dominated the project.

## Stage 6 — Risk gates (`risk.py`, 364 lines)

**16 stable reject codes.** Every gate is evaluated and every failure collected —
no short-circuit — so the log shows all reasons a trade was refused, not just the
first. Per-trade cap, aggregate cap, daily stop, position count, duplicate-leg
exposure, quote staleness, entry cutoff, and a **pre-send re-validation** that
re-tests the violation on fresh quotes immediately before submitting.

## Stage 7 — Execution (`executor.py`, 295 lines)

Orders go out over the **MCP server** as `mleg` limit orders. The design turns
the data weakness into a safety property:

> The signal is synthetic. The fill is real. Alpaca's paper engine matches
> against real NBBO, so a market order would convert a phantom signal directly
> into a real position.

Every order is therefore a **limit** priced against the indicative quote. If the
real market is genuinely that good, it fills at a price we modelled; if the quote
was an artefact, it simply does not fill. **Bad data produces non-fills, not bad
fills.** `type: "market"` is never constructed, and a test asserts the string does
not appear in the module.

## Stage 8 — Exits (`exits.py`, 309 lines)

Triggers in order of authority: **deadline** → **near-expiry** → **reverted** →
**time stop** → hold. Reversion outranks the clock, so a position closes on its
thesis rather than on elapsed time. Closing inverts the entry as **one multi-leg
order** — legging out would leave a naked short between fills.

## Stage 9 — The decision log (`audit.py`, 168 lines)

Append-only. No update, no delete, no rewrite; a correction is a new record.
`orders.jsonl` records what was *sent*, which is survivorship-biased — it cannot
show the candidate that was detected and refused, which is almost all of them.
**32,505 decisions** were logged with full evidence.

## Stage 10 — The narrator (`narrator.py`, 295 lines)

An LLM layer that reads the decision log and writes a human-readable account.
**Records flow in, text flows out, nothing else.** It imports nothing from the
decision path, nothing in the decision path imports it, the model is given **no
tools**, and its output goes to a file the trading path never opens. If it
hallucinated entirely, the trades that happened would still be exactly the trades
the deterministic gates approved.

**It earned its place on the first run**, finding three defects in its own
reports — including one that mattered (§10).

---

# 4. What actually happened

## 4.1 Scanning

| underlying | scans | rectangles considered | violations detected |
|---|---|---|---|
| SPY | 88 | 114,409,284 | 4,887 |
| SPX | 19 | 27,106,432 | 12,210 |
| XSP | 7 | 50,492,546 | 0 |
| **total** | **114** | **192,008,262** | **17,097** |

## 4.2 Episodes — the strategy's core claim

| underlying | episodes | reverted | rate | median time to revert |
|---|---|---|---|---|
| SPY | 9,469 | 9,208 | **97%** | 660 s |
| SPX | 30,827 | 30,218 | **98%** | 646 s |
| XSP | 3,270 | 3,261 | **100%** | 680 s |
| **total** | **43,566** | **42,687** | **98%** | **~11 min** |

**This is the project's strongest empirical result.** The violations are real and
they correct, reliably and quickly.

## 4.3 The decision funnel

| outcome | SPY | SPX | XSP |
|---|---|---|---|
| not executable | 8,506 | 13,466 | 1,974 |
| theory blocked | 1,969 | — | — |
| risk rejected | 383 | 5,424 | 671 |
| **traded** | **50** | **27** | **17** |
| order failed | 15 | 3 | — |

## 4.4 Live trading

```
orders placed            131   (across 1, 2 and 3 September)
orders filled             40
completed round-trips     21
closed on reversion       21  (100%)
opens paid            $243.00
closes received       $198.00
net                    -$45.00
broker fees              $0.00
final equity        $99,963.00
```

**Every single position exited because its violation reverted.** Not one closed
on a time stop or a deadline. The exit thesis worked perfectly.

---

# 5. Backtest results

Every backtest below runs the **identical live pipeline** — theory gate,
execution screens, re-validation, risk caps — on the same day's signals. Entry
and exit both **cross the spread**. There is no look-ahead: exit is the first
reversion *after* entry.

## 5.1 The full matrix (SPY, 3 September)

| denomination | sizing | trades | total | mean | median | win rate |
|---|---|---|---|---|---|---|
| **T1** | unit 1:1 | 672 | **+\$9,829** | +\$14.63 | +\$2.00 | 67% |
| **T1** | paper weights | 386 | **+\$8,754** | +\$22.68 | +\$1.00 | 65% |
| **T1** | 1:1 scaled (10x cap, **3.1x effective**) | 672 | **+\$30,878** | +\$45.95 | +\$3.00 | 67% |
| **K2** | unit 1:1 | 748 | −\$5,363 | −\$7.17 | \$0.00 | 49% |
| **K2** | paper weights | 312 | −\$5,775 | −\$18.51 | −\$1.00 | 43% |
| **K2** | 1:1 scaled (10x cap) | 748 | -\$84,249 | -\$112.63 | \$0.00 | 49% |

**T1 is profitable; K2 is not** — though the source study reports both performing
well. The most likely cause is the exit: the study **holds every position to
expiration**, this agent **exits on reversion**. K2 is a diagonal whose legs
expire on different dates, so closing it early is a materially different trade.

## 5.2 Why the weighted version trades less

Identical filters reject identically except one:

| filter | unit rejects | weighted rejects |
|---|---|---|
| theory gate | 3,579 | 3,579 |
| cheapest leg | 95 | 95 |
| coverage ratio | 67 | 67 |
| **max loss > 2.5% equity** | **76** | **362** |
| **survived** | **672** | **386** |

The 286-trade difference is entirely the **risk cap**. A covered 1:1 spread
cannot lose more than its debit — median **\$2**. Add one naked short and max loss
becomes width-scaled — median **\$700**, a 350× increase in measured risk. The
weighted version is not finding fewer signals; it is being **refused by the risk
manager**, correctly.

## 5.3 Reading the scaled row correctly

The scaled row applies a **10x cap**, and 630 of 748 trades received the full 10x
— so the *median* multiple really is 10x. The total nevertheless grows only
**3.1x**, and the reason is worth stating because it is easy to misread.

Scale is `risk cap / max loss`, and max loss is the debit. A trade with a large
debit therefore has **more profit potential and less room to scale at the same
time.** The multiple lands hardest on the trades that matter least:

| | count | median scale |
|---|---|---|
| trades earning **>= \$50** | 106 | **1x** |
| trades earning **< \$50** | 642 | **10x** |

**Most trades got 10x; most profit got 1x.** Quoting "median 10x applied" beside a
3.1x total is true twice over and misleading once — the median counts trades, and
every trade counts equally regardless of size, so it describes the typical
*trade* and says nothing about the typical *dollar*.

Two consequences follow:

- The honest figure is the **effective** multiple, 3.1x, and the tool now prints
  both with the reason they differ.
- The risk manager is behaving correctly. It caps precisely the positions that
  are already large, which is what a risk cap is for. Scaling is not a free lever.

And the caveat from §7 still governs: scaling multiplies whatever the true
per-contract result is. If that is really **-\$2** as the live account says rather
than **+\$2** as the backtest assumes, then scaling makes the outcome worse, not
better. **Size amplifies a sign; it does not create one.**

## 5.4 The caveat that governs all of these

**Every row assumes a fill at the quoted price on every signal.** Live, 3% filled.
The gap between +\$9,829 and −\$45 is not a modelling error — **it is the
execution cost**, and it is the subject of §7.

---

# 6. Why the portfolio stayed small — every reason

Ranked by how much each actually mattered, with evidence.

## Reason 1 — The edge and the cost of collecting it are the same size

**This is the dominant reason and it explains the loss by itself.**

```
SPY leg spreads, 5,786 observations
   A leg median                            $0.010
   D leg median                            $0.010
   crossing both legs, in and out          $0.020/share = $2 per contract

backtest median profit per trade                        +$2 per contract
```

A ~2-cent mispricing genuinely exists and genuinely corrects. It costs ~2 cents
to cross the spread twice and capture it. **The signal is real and the toll to
collect it consumes it.**

Confirmed in the live fills — the pattern is unmistakable:

| open | close | result |
|---|---|---|
| +0.04 | −0.02 | −0.02 |
| +0.17 | −0.15 | −0.02 |
| +0.14 | −0.12 | −0.02 |
| +0.11 | −0.10 | −0.01 |

**This is not Alpaca's doing.** `accrued_fees: 0`. No commission on any filled
order. Fills routinely came in *better* than the limit asked — one order with a
`-0.03` limit filled at `-0.06`. The broker gave price improvement. The cost is
the bid-ask spread: market structure, present at every broker on earth.

## Reason 2 — Adverse selection on resting limit orders

The backtest assumes a fill on all 672 signals. The agent filled 21 — **about
3%**.

A limit buy fills **only when the market comes down to it**. So the fills obtained
are precisely the ones already moving against the position. This is not a bug; it
is what a resting limit order *is*. Combined with Reason 1, it reliably tips a
coin-flip onto the losing side.

## Reason 3 — The indicative feed is not the market

OPRA was denied (`403`, \$99/month). Detection ran on Alpaca's model-derived
quotes throughout. Consequences, all observed directly:

- SPX quoted a **15-point-wide spread at 16.05** — above its maximum possible
  value
- Fully-ITM SPX spreads quoted at **57–68% of intrinsic**, which no real market
  will sell
- At a 10% spread screen, **21,109 contracts measured and zero violations found**

That last figure is the sharpest statement of the problem: **on this feed,
violations exist only where spreads are wide.** Tighten to genuinely liquid
contracts and the signal disappears entirely.

## Reason 4 — Two of three underlyings were untradeable

Alpaca publishes greeks for only the contracts it models — **75 of 244** SPY calls
on one expiry. For **SPX and XSP it publishes none at any strike**.

SPX detected 12,210 violations and **filled nothing**, because every candidate was
a deep-ITM spread quoted below intrinsic. XSP, once quality screens were applied,
detected **zero**. Only SPY was tradeable.

## Reason 5 — The dividend horizon blocked most of SPY

The theory gate refused **1,969 SPY candidates** because their far leg expired
past 21 September, beyond the date through which dividend absence is assertable.
This is the gate working correctly — but it removed most of the long-dated
universe.

## Reason 6 — Options level 3 forced 1:1 sizing

Covered in §7. Notably, **this reason did not cost anything** — it improved the
outcome.

## Reason 7 — A very short live window

Live trading began at **15:51 on Wednesday 2 September**, nine minutes before the
close. Thursday 3 September was the only full session, and the entry cutoff at
15:55 ended it. Roughly **one and a half sessions** of live trading in total.

## Reason 8 — Time lost to defects found while live

Honest accounting: several bugs were found *during* the live session and cost
scanning time. They are catalogued in §10. The most expensive was a `NameError`
that silently killed every SPY scan for several cycles while the process stayed
alive.

---

# 7. The sizing constraint, and why it helped

## 7.1 What the study specifies

Table 5.1 of Glasserman, Li & Pirjol sizes each leg by the **opposing contract's
price**:

| denomination | long leg | its quantity | short leg | its quantity |
|---|---|---|---|---|
| **T1** | A | $C(K_2,T_2)$ = **B's price** | D | $C(\tilde K_1,T_2)$ = **C's price** |
| **K2** | B | $C(K_1,T_1)$ = **A's price** | D | $C(\tilde K_1,T_2)$ = **C's price** |

## 7.2 Why it cannot be submitted

Because $\tilde K_1 < K_2$, contract C is the lower strike at $T_2$ and is
therefore **always dearer than B**. The short quantity always exceeds the long.
Measured across 668 real candidates: **ratio 1.03 to 33.77, median 1.74, not one
at or below 1.0**.

Short more than long means **naked calls**. The account is options level 3.
Submitting the real thing returns:

```
long 1 : short 2  ->  HTTP 403, code 40310000
                      "account not eligible to trade uncovered option contracts"
```

No rescaling escapes it: a ratio above 1 stays above 1 at 2:3, 4:6 or any
multiple. **1:1 is not the nearest covered approximation — it is the only one.**

Both denominations the study finds profitable (T1, K2) are short-heavy. The two
that *are* coverable (T2, K1) are the two the study reports produce **negative**
average profits. **There is no configuration that is both profitable and legal at
level 3.**

## 7.3 Could it be traded elsewhere? Yes — and it would be far worse

A 1×2 ratio spread is standard and level 4 permits it. But the uncovered contract
is a naked short call, and Reg T requires

$$\text{premium} + \max(20\% \times S - \text{OTM amount},\; 10\% \times S)$$

which on these contracts is **\$12,000–\$13,800 of margin per naked contract**.

| structure | capital tied up | mean profit | return on capital |
|---|---|---|---|
| **covered 1:1** (traded) | **\$4** | \$14.63 | **365.8%** |
| 1×2 ratio (paper weights) | \$13,765 | \$22.68 | **0.165%** |

**The covered version earns roughly 2,220× more per dollar committed**, and its
loss is bounded at the debit while the ratio spread's is unlimited above the short
strike.

**The paper cannot see this** because it measures profit *per trade*, never profit
*per dollar of margin*. It holds fractional positions to expiration and models no
margin at all. The constraint simply does not exist in its framework — and it is
the first thing that binds when the strategy meets a real account.

**Conclusion: the broker restriction cost nothing. It forced the structure that is
both more capital-efficient and less risky.**

---

# 8. What would make this work in a real market

Ordered by how much each would change the outcome.

1. **Tighter spreads.** The edge is fixed at a couple of cents; the cost is not.
   Real OPRA data and liquid strikes shrink the denominator directly. This is the
   single highest-leverage change.
2. **Passive entry.** Resting *inside* the spread rather than crossing it converts
   a 2-cent cost into something smaller, at the price of fewer fills. The current
   agent always crosses.
3. **Not penny options.** On a 5-cent contract a one-tick spread is 20–30% of its
   value. The same strategy on dollar-priced contracts faces a far smaller
   proportional toll.
4. **Hold to expiration rather than exiting on reversion**, at least for K2. The
   study does exactly this, and it is the most plausible explanation for K2
   losing here while succeeding there.
5. **The denomination selector.** T1 returned +\$8,754 and K2 −\$5,775 on the same
   day. A working selector that avoided the losing denomination is worth roughly
   the difference. It is built and tested but was never supplied with a trained
   model.

---

# 9. Competition compliance

| requirement | status |
|---|---|
| Alpaca Trading API **plus** the MCP server | **MET** — MCP is the default transport, live-verified |
| Dedicated paper account, \$100,000 | **MET** — ACTIVE, options level 3 |
| Every strategy includes options trading | **MET** |
| One-page write-up | **MET** — `WRITEUP.md` |

The published rules mandate **no LLM, no generative model and no particular AI
provider**. The narrator is included because it proved useful, not because it was
required.

`scripts/compliance_report.py` regenerates this table from live state — the
broker account, the running processes, the audit log and the test suite — so
every claim is checkable rather than asserted.

---

# 10. Defects found, and what they teach

Fourteen genuine defects were found and fixed, most of them *while trading live*.
They share a shape worth naming: **the trading arithmetic was almost always
right; the records describing it were wrong.**

| defect | consequence |
|---|---|
| Exit-on-reversion could never fire | Positions keyed on the short-leg symbol, episodes on a four-leg hash. The lookup returned `None` every time. Silent, because an unknown status correctly means "hold". |
| Positions orphaned by restarts | `reconcile` tracked fills in memory; five legs sat at the broker with nothing watching them |
| Closing orders adopted as positions | A close sells the leg it bought, so one was adopted **inverted** — the agent would have "closed" a phantom by opening a real naked short |
| Re-adoption after closing | A position closed seconds ago is still at the broker; adoption took it back on |
| Cross-underlying contamination | The SPX scanner adopted SPY's entire book; both would have closed the same positions |
| Stale orders never recycled | Broker UTC compared against local time; age computed as −4 hours, so nothing ever expired |
| `NameError` killed every scan | Swallowed by a broad handler; the process stayed alive and produced nothing |
| Orders priced from stale quotes | Re-validated on fresh quotes, then priced from the old ones — every order ~3 cents under the market |
| Revalidation gate was vacuous | The candidate was compared against itself; 209 stale candidates would have been sent |
| Scanners silently stopped overnight | `caffeinate` held its assertions and the machine slept anyway |
| `normalized_severity` wrong denominator | Every severity figure reported was ~2× the study's convention |
| K2 long weight used B instead of A | Wrong per Table 5.1 |
| Backtest look-ahead | Exited at the last observation rather than the first reversion |
| Backtest overstated covered risk | Charged strike width to the covered leg — \$806 where the truth is \$4 |

**The lesson, stated plainly: a live process is not evidence of live work, and a
gate that never fires is indistinguishable from a gate with nothing to catch.**
Both cost real time here. The audit log and the narrator were what surfaced most
of them.

---

# 11. Conclusion

**The strategy's central claim held.** 43,566 tracked episodes, **98% reverted**,
median 11 minutes. Every one of the 21 live positions closed because its violation
corrected. The mathematics is sound and the phenomenon is real.

**The execution economics did not.** The mispricing is worth about two cents and
costs about two cents to collect. On this feed, at this account level, in these
contracts, there is no room between the two.

That is not a failed experiment. **It is the finding.** An end-of-day research
paper measuring mid-price profits cannot discover it, because mid prices are not
tradeable and margin does not exist in a backtest. It only appears when the
strategy meets a real broker, a real spread and a real risk manager — which is
precisely what this project did.

The most valuable output is therefore not the P&L. It is the demonstration that

- the signal is genuine and reverts on schedule,
- the theoretical position sizing is **unusable** — illegal at level 3, and
  2,220× less capital-efficient where it is legal,
- and the binding constraint on a real account is the **bid-ask spread**, not the
  theory.

---

*Repository: `github.com/Omkar38/MarginCalled` — 279 tests, standard library only
in the core. Every figure in this report is reproducible from the committed data
snapshot in `dashboard_data/`.*
