# lablab.ai submission fields

## Submission Title  *(5–50 chars)*

```
MarginCalled — TP2 Options Arbitrage Agent
```
*(42 characters)*

---

## Short Description  *(50–255 chars)*

```
An autonomous options agent that finds four-contract price rectangles breaking a
no-arbitrage theorem, proves each one is real before trading, and exits when the
mispricing corrects. 98% of 43,566 tracked violations reverted.
```
*(224 characters)*

---

## Long Description  *(min 100 words / 600 chars, max 2000)*

```
MarginCalled does not predict the market. It looks for prices that cannot all be
correct at once.

Call prices must satisfy total positivity of order 2: for two expiries and two
strikes, one product of prices must exceed another once strikes are forward-adjusted.
Four contracts form a "rectangle". When the inequality fails on crossable quotes, the
surface is internally inconsistent — not mispriced against a model, but provably
inconsistent. The agent trades that and exits when it corrects.

Every candidate passes an early-exercise theory gate before it can trade. TP2 is a
theorem about European calls, so an American one only qualifies if early exercise is
worthless — no dividend in the window, or a certificate from the source study.
Crucially, an empty dividend list is not evidence of no dividend: anything past the
provable horizon is marked unresolved and left alone.

Survivors pass 16 deterministic risk gates, all evaluated with no early exit, including
a re-test of the violation on fresh quotes immediately before the order goes out.
Orders go over Alpaca's MCP server as multi-leg limits priced better than the
indicative quote, so a bad quote produces a non-fill rather than a bad fill.

Results: 192M rectangles scanned, 17,097 violations found, 43,566 episodes tracked to
resolution. 98% reverted, median 11 minutes. Live, 21 round-trips — every one closed
because its violation reverted, none on a timer.

The account still lost $37, and that is the finding. The mispricing is worth ~2¢ and
the bid-ask spread costs ~2¢, so execution decides the sign. An end-of-day study
measuring mid-prices cannot discover this, because mid-prices are not tradeable.

Every refusal is logged — 32,505 decisions with full evidence — and a language layer
reads that log and explains them. It has no tools and cannot place an order.
```
*(~1,850 characters)*

---

## Categories / Event Tracks

Select whichever the form offers from:

- **Options** *(primary — every position is a multi-leg option spread)*
- **Volatility / Arbitrage**
- **Autonomous agents**
- **Portfolio overlays** *(only if the form has no better fit)*

---

## Technologies Used

```
Alpaca Trading API, Alpaca MCP Server, Alpaca Options Market Data,
Python 3 (standard library only in the core), Streamlit, Claude (Opus) for the
narration layer, pandas, Altair, ffmpeg
```

Worth stating explicitly if there is room: **the core has no third-party
dependencies** — detection, the theory gate, risk and execution are standard library
only. Streamlit, pandas and Altair are used by the dashboard, not the agent.

---

## Links

- **Repository:** `github.com/Omkar38/MarginCalled`
- **Live dashboard:** `margincalled.streamlit.app`
- **Report:** `PROJECT_REPORT.md` / `reports/PROJECT_REPORT.pdf`

---

## If asked "what makes this different"

Most trading agents predict. This one proves. It refuses far more than it trades —
8,506 candidates rejected as unexecutable, 1,969 blocked by the theory gate, 383 by
risk — and it records every refusal as fully as every fill. The interesting output is
the reasoning, not the P&L.
