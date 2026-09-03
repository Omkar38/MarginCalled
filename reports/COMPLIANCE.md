# Compliance evidence pack

Generated `2026-09-03T16:56:57` by `scripts/compliance_report.py`.

Every figure below is read from live state — the broker account, the running processes, the audit log, the test suite — not asserted. Re-run the script to reproduce it.

---

## Mandatory requirements

| requirement | status |
|---|---|
| Alpaca Trading API **plus** the MCP server | **MET** |
| Dedicated paper account, $100,000 | **MET** |
| Every strategy includes options trading | **MET** |
| One-page write-up (AI logic, risk gates, Alpaca infrastructure) | **MET** |

Verbatim from the published rules: *"Projects must use Alpaca's Trading API and either its MCP server or CLI"*, *"Strategies must incorporate options trading"*, *"Final submissions require a new dedicated Alpaca paper trading account"*.

The rules mandate **no LLM, no generative model and no particular AI provider**. The layers below are reported for transparency, not because any of them is required; an unexercised one is labelled so rather than described as if it ran.

Judging is on **P&L performance, technology implementation, creativity and originality, and presentation and execution**.

---

## 1. Alpaca infrastructure (MCP)

- order transport: **MCP** (`Executor(transport=Transport.MCP)`)
- toolsets requested: `account,assets,market_info,trading`
- live `alpaca-mcp-server` processes: **1**
- scanner wires MCP for orders: `True`
    - `85750 /opt/homebrew/Cellar/python@3.14/3.14.6/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/C`

---

## 2. The competition account

- account number: redacted (run with --show-account to include)
- status: `ACTIVE`
- equity: **$99,963.00**
- cash: $99,953.00
- options approved level: **3**
- trading blocked: `False`
- open positions: **3**
- open orders: **0**
- endpoint: `https://paper-api.alpaca.markets` (paper only; the live host is refused in code)

---

## 3. Options strategy

Two denominations from Table 5.1 of Glasserman, Li & Pirjol, both multi-leg option positions on listed calls:

- **T1** — buy A(K1,T1), sell D(K2~,T1). A vertical; one expiry.
- **K2** — buy B(K2,T2), sell D(K2~,T1). A diagonal; spans expiries.

SPX and XSP are European and can only submit same-expiry legs (Alpaca returns HTTP 422 / 42210000 for a spanning multi-leg order, verified live), so they trade **T1 only**. SPY is American and may trade **either**.

Entry is on a TP2 violation; the exit is on reversion.

---

## 4. AI logic

| layer | kind | status |
|---|---|---|
| Theory gate + 16 risk gates | deterministic | **RUNNING** |
| Denomination selector (46 features) | learned | **BUILT, NOT RUNNING** |
| Narrator (`claude-opus-5`) | language model | **RUNNING** |

> The selector has **no trained model loaded**, so it falls back to T1 and records the choice as a fallback rather than a prediction. Its feature builder is complete and verified bit-exact against the source study's 354,974-row dataset (`scripts/validate_features_against_study.py`), and it scores one vector per denomination with an abstention threshold - but until a scorer is supplied, no learned decision is being made.

**Deterministic layer.** The TP2 determinant and an early-exercise theory gate (Propositions 2.1-2.2). TP2 is a theorem about European calls, so before trading an American one the agent must certify that early exercise carries no premium. An empty dividend list is not evidence of no dividend: the gate tracks the date through which absence is assertable and returns UNRESOLVED beyond it, and UNRESOLVED cannot trade.

**Learned layer.** A rectangle has one feature vector per denomination, not one overall - the feature set carries no strategy indicator, so the choice reaches the model through exactly two features. The selector may abstain.

**Language layer.** The narrator is a **reader, not a participant**, and this is enforced rather than intended:

- it imports nothing from the decision path, and nothing in the decision path imports it - both directions asserted by tests
- the model is given **no tools** (asserted: `'tools'` does not appear in `narrator.py`)
- its output is written to a file the trading path never opens
- it falls back to a deterministic template on any failure, so a narration failure cannot look like a trading failure

If the language model hallucinated entirely, the trades that happened would still be exactly the trades the deterministic gates approved.

---

## 5. Deterministic risk gates

- per-trade max loss cap: **2.50%** of equity
- aggregate max loss cap: **10.00%**
- daily stop: **0.75%**
- max open positions: 5
- max quote age: 30s
- entry deadline: `2026-09-03`; daily cutoff `15:55:00`

**16 deterministic reject codes**, every one evaluated on every candidate (no short-circuit), so a refusal records all of its reasons:

```
  kill_switch                 position_not_executable     position_not_covered
  theory_unresolved           quotes_stale                violation_gone
  violation_decayed           max_loss_per_trade          max_aggregate_loss
  daily_stop                  too_many_positions          duplicate_leg_exposure
  after_daily_cutoff          after_entry_deadline        reconciliation_error
  insufficient_buying_power
```

---

## 6. Decision audit trail

- **SPY**: 10923 decisions — not_executable 8506, order_failed 15, risk_rejected 383, theory_blocked 1969, traded 50
- **SPX**: 18920 decisions — not_executable 13466, order_failed 3, risk_rejected 5424, traded 27
- **XSP**: 2662 decisions — not_executable 1974, risk_rejected 671, traded 17

**32505 decisions logged**, each with the quotes, the determinant, the theory category and every risk gate that ran. Refusals are recorded as fully as fills.

---

## Not yet met

- (optional, not required by the rules) AI layers not all running - see section 4

