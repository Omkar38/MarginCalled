# MarginCalled — a TP2 arbitrage agent for listed index options

**Alpaca AI Trading Agents Hackathon, 28 Aug – 4 Sep 2026**

## What it trades

A call surface must satisfy **total positivity of order 2**. For expiries
$T_1 < T_2$ and strikes ordered by forward moneyness $K_1/F_{T_1} < K_2/F_{T_2}$:

$$C(K_1,T_1)\,C(K_2,T_2) \;\ge\; C(\tilde K_1,T_2)\,C(\tilde K_2,T_1), \qquad
\tilde K_1 = K_1\tfrac{F_{T_2}}{F_{T_1}},\;\; \tilde K_2 = K_2\tfrac{F_{T_1}}{F_{T_2}}$$

When this fails on **crossable quotes** — $A^{ask}B^{ask} < C^{bid}D^{bid}$ — the
surface is internally inconsistent. The agent enters the violation and exits on
reversion. It trades two denominations, both shorting the near leg D:

- **T1** — buy $A(K_1,T_1)$, sell $D(\tilde K_2,T_1)$ — a vertical
- **K2** — buy $B(K_2,T_2)$, sell $D(\tilde K_2,T_1)$ — a diagonal

SPX and XSP are European: Alpaca rejects multi-leg orders whose legs span
expiries (HTTP 422 / 42210000, verified live), so they trade **T1 only**. SPY is
American and may trade either.

## AI logic

Three layers, deliberately separated by what each is allowed to do.

**Deterministic.** The TP2 determinant and an early-exercise theory gate. TP2 is
a theorem about European calls, so before trading an American one the agent must
certify that early exercise carries no premium — via Proposition 2.1(ii)
($\delta_i + \sum_{j>i}\delta_j e^{-r(t_j-t_i)} \le K(1-e^{-r(T-t_i)})$), via
Proposition 2.2's dividend bound, or by proving no distribution occurs. Crucially,
**an empty dividend list is not evidence of no dividend**: the gate tracks the
date through which absence is assertable and returns `UNRESOLVED` beyond it.
`UNRESOLVED` cannot trade.

**Learned.** Where both denominations are submittable, a selector scores a
46-feature vector *per denomination* and takes the better only if it clears 0.5,
otherwise abstaining. A rectangle has two feature vectors, not one — the feature
set carries no strategy indicator, so the choice reaches the model through
exactly two features. Definitions were verified **bit-exact against the source
study's 354,974-row dataset**, which corrected three of them.

**Language.** A narrator turns the decision log into prose. It is a **reader, not
a participant**: it imports nothing from the decision path, nothing in the
decision path imports it (both asserted by tests), the model is given **no
tools**, and its output goes to a file the trading path never opens. If it
hallucinated entirely, the book would still be exactly what the deterministic
gates approved.

## Deterministic risk gates

**16 reject codes**, every one evaluated on every candidate — no short-circuit —
so a refusal records all of its reasons. Per-trade cap 2.5% of equity, aggregate
10%, daily stop 0.75%, position count cap, duplicate-leg exposure, quote
staleness, entry cutoff and deadline. The violation is **re-tested on fresh
quotes immediately before sending** (`VIOLATION_GONE`, `VIOLATION_DECAYED`).
`should_flatten` is independent of the entry gates: the conditions for getting
out must not depend on the machinery for getting in.

Sizing is capped at 1:1 — the only always-covered whole-contract ratio. The
source study takes fractional positions; a live market cannot, and the code
refuses a fractional ratio at the payload boundary. Max loss for the K2 diagonal
includes the strike gap $\max(K_2-\tilde K_2, 0)$, which the debit alone omits.

## Alpaca infrastructure

Orders are submitted **over the MCP server** (`alpaca-mcp-server`, trading
toolset) as `mleg` limit orders; REST is a fallback. The live trading host is
refused in code, and `type: "market"` is never constructed — a test asserts the
string does not appear in the executor.

That last point is the central design decision. Detection runs on Alpaca's
**indicative** feed, whose quotes are model-derived and demonstrably not
internally consistent, while the paper engine fills against **real NBBO**. The
signal is synthetic; the fill is real. A market order would convert a phantom
signal directly into a real position. So every order is a limit priced
*conservatively against the indicative quote* — shaded in units of the package's
own spread, clamped so it can never cross zero and become unfillable.

**Bad data therefore produces non-fills, not bad fills.** That is a risk control,
not a workaround.

## Engineering

13 modules, ~4,900 lines, **249 tests**, standard library only in the core.
Every considered candidate — not just every order — is written to an append-only
decision log with its quotes, determinant, theory category and full gate
results. `scripts/compliance_report.py` regenerates the evidence for every claim
above from live state.

Repository: `github.com/Omkar38/MarginCalled`
