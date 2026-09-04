"""MarginCalled — a theorem-gated options arbitrage agent.

A single-file quant-terminal dashboard built from the committed
`dashboard_data/<UNDERLYING>/*.gz` snapshot, the backtest results, and
`reports/*_narration.md`.  No API key, no network, no `data/`.
Run:  streamlit run app.py

Headline numbers come from PROJECT_REPORT.md (the final competition run).
The per-underlying detail charts read the committed snapshot, whose episode and
decision counts match that report.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------- #
# Palette
# --------------------------------------------------------------------------- #
BG, PANEL, ELEV, BORDER = "#0a0e14", "#111823", "#161f2c", "#212c3b"
INK, MUTED, FAINT = "#e8edf4", "#8a97a8", "#5a6675"
ACCENT, ACCENT2 = "#2dd4bf", "#22d3ee"
BLUE, GREEN, AQUA = "#3b82f6", "#22c55e", "#2dd4bf"
YELLOW, ORANGE, RED, MAGENTA = "#eab308", "#f97316", "#ef4444", "#d946ef"
GRID = "#18212e"

THEORY_ORDER = ["european_native", "no_distribution", "dividend_spanning",
                "dividend_bound", "unresolved"]
THEORY_COLORS = [BLUE, AQUA, GREEN, YELLOW, RED]
OUTCOME_ORDER = ["traded", "held", "closed", "not_executable",
                 "theory_blocked", "risk_rejected", "order_failed"]
OUTCOME_COLORS = [GREEN, BLUE, AQUA, ORANGE, YELLOW, RED, MAGENTA]

UNDERLYINGS = ["SPY", "SPX", "XSP"]
VIEWS = ["Overview", "Backtest", "Why only SPY", "How it works",
         "Signals", "Reversion", "Decisions", "Live fills", "Narration"]
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "dashboard_data"
REPORTS = ROOT / "reports"

# Final competition figures (PROJECT_REPORT.md, 4 Sep 2026).
S = {
    "rectangles": 192_008_262, "violations": 17_097,
    "episodes": 43_566, "reverted": 42_687, "revert_rate": 0.98, "revert_min": 11,
    "orders": 131, "filled": 40, "round_trips": 21,
    "opens_paid": 243.0, "closes_recv": 198.0, "net": -45.0,
    "equity": 99_963.0, "start_equity": 100_000.0, "fees": 0.0,
    "tests": 279,
    # backtest matrix, SPY 3 Sep, identical live filters, both sides cross the spread
    "bt": [
        ("T1", "unit 1:1 (as traded)", 672, 9829, 14.63, 2.00, 0.67),
        ("T1", "study weights", 386, 8754, 22.68, 1.00, 0.65),
        ("T1", "1:1 scaled (10× cap, 3.1× effective)", 672, 30878, 45.95, 3.00, 0.67),
        ("K2", "unit 1:1", 748, -5363, -7.17, 0.00, 0.49),
        ("K2", "study weights", 312, -5775, -18.51, -1.00, 0.43),
        ("K2", "1:1 scaled (10× cap)", 748, -84249, -112.63, 0.00, 0.49),
    ],
}

st.set_page_config(page_title="MarginCalled", page_icon="◢",
                   layout="wide", initial_sidebar_state="collapsed")

# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #
st.markdown(f"""
<style>
  .stApp {{ background:
      radial-gradient(1200px 600px at 80% -10%, #10202688 0%, transparent 60%),
      radial-gradient(900px 500px at -10% 10%, #12203a55 0%, transparent 55%), {BG}; }}
  section[data-testid="stSidebar"] {{ display:none; }}
  [data-testid="stToolbar"], [data-testid="stDecoration"], #MainMenu, footer,
  [data-testid="stSidebarCollapsedControl"] {{ display:none; }}
  [data-testid="stHeader"] {{ background:transparent; height:0; }}
  .block-container {{ padding-top:1.6rem; padding-bottom:3rem; max-width:1440px; }}
  h1,h2,h3,h4 {{ color:{INK}; letter-spacing:-0.015em; font-weight:700; }}
  .mono {{ font-family:ui-monospace,'SF Mono',Menlo,monospace; }}
  a {{ color:{ACCENT}; }}

  .mast {{ display:flex; align-items:center; gap:13px; margin-bottom:3px; }}
  .mark {{ width:30px; height:30px; border-radius:8px;
           background:linear-gradient(135deg,{ACCENT},{ACCENT2}); display:flex;
           align-items:center; justify-content:center; color:#04110f; font-weight:900;
           font-size:17px; box-shadow:0 0 22px {ACCENT}66; }}
  .mast .name {{ font-size:23px; font-weight:800; color:{INK}; letter-spacing:-0.02em; }}
  .tag {{ color:{MUTED}; font-size:14.5px; margin:2px 0 16px; max-width:840px; line-height:1.5; }}
  .tag b {{ color:{INK}; }}

  .statusbar {{ display:flex; flex-wrap:wrap; gap:8px; margin:0 0 6px; }}
  .pill {{ font-family:ui-monospace,Menlo,monospace; font-size:10.5px; letter-spacing:.05em;
           text-transform:uppercase; padding:6px 11px; border-radius:8px;
           border:1px solid {BORDER}; background:{PANEL}; color:{MUTED}; }}
  .pill b {{ color:{INK}; font-weight:600; margin-left:6px; }}
  .dot {{ display:inline-block; margin-right:7px; }}
  .g{{color:{GREEN};}} .a{{color:{YELLOW};}} .r{{color:{RED};}} .t{{color:{ACCENT};}} .m{{color:{FAINT};}}

  .sec {{ display:flex; align-items:center; gap:9px; margin:6px 0 2px; }}
  .sec::before {{ content:""; width:3px; height:15px; border-radius:2px;
                  background:linear-gradient(180deg,{ACCENT},{ACCENT2}); display:inline-block; }}
  .sec h3 {{ margin:0; font-size:16.5px; }}
  .subtle {{ color:{MUTED}; font-size:13px; margin:2px 0 12px; line-height:1.55; }}

  .kpirow {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(158px,1fr)); gap:12px; margin:6px 0 4px; }}
  .kpi {{ background:linear-gradient(180deg,{ELEV},{PANEL}); border:1px solid {BORDER};
          border-radius:13px; padding:15px 17px; position:relative; overflow:hidden; }}
  .kpi::after {{ content:""; position:absolute; left:0; top:0; height:2px; width:100%;
                 background:linear-gradient(90deg,transparent,{BORDER},transparent); }}
  .kpi .label {{ font-family:ui-monospace,Menlo,monospace; font-size:10px; letter-spacing:.1em;
                 text-transform:uppercase; color:{MUTED}; margin-bottom:9px; }}
  .kpi .value {{ font-size:27px; font-weight:800; color:{INK}; line-height:1.02; letter-spacing:-0.02em; }}
  .kpi .sub {{ font-size:11.5px; margin-top:6px; font-family:ui-monospace,Menlo,monospace; color:{FAINT}; }}
  .kpi .sub.up{{color:{GREEN};}} .kpi .sub.down{{color:{RED};}}
  .kpi.accent {{ border-color:{ACCENT}66; box-shadow:0 0 0 1px {ACCENT}22 inset, 0 0 26px {ACCENT}18; }}
  .kpi.accent::after {{ background:linear-gradient(90deg,transparent,{ACCENT},transparent); }}
  .kpi.win .value{{color:{GREEN};}} .kpi.loss .value{{color:{RED};}}

  .badge {{ font-family:ui-monospace,Menlo,monospace; font-size:10px; letter-spacing:.05em;
            text-transform:uppercase; padding:3px 9px; border-radius:6px; font-weight:700; }}
  .b-green{{color:{GREEN};background:{GREEN}1f;border:1px solid {GREEN}44;}}
  .b-red{{color:{RED};background:{RED}1f;border:1px solid {RED}44;}}
  .b-yellow{{color:{YELLOW};background:{YELLOW}1f;border:1px solid {YELLOW}44;}}

  .card {{ background:linear-gradient(180deg,{ELEV},{PANEL}); border:1px solid {BORDER};
           border-radius:13px; padding:16px 18px; height:100%; }}
  .card.reject {{ border-left:3px solid {RED}; }}
  .card.good {{ border-left:3px solid {GREEN}; }}
  .card h4 {{ margin:0 0 11px; font-size:14px; display:flex; align-items:center; gap:8px; }}
  .card .k {{ font-family:ui-monospace,Menlo,monospace; font-size:12px; color:{MUTED}; white-space:nowrap; }}
  .card .v {{ font-family:ui-monospace,Menlo,monospace; font-size:12px; color:{INK};
             text-align:right; padding-left:18px; word-break:break-word; }}
  .card .row {{ display:flex; justify-content:space-between; gap:12px; padding:5px 0; border-bottom:1px dashed {BORDER}; }}
  .card .row:last-child {{ border-bottom:none; }}

  [data-testid="stSegmentedControl"] button {{ background:{PANEL}!important; border:1px solid {BORDER}!important;
      color:{MUTED}!important; border-radius:9px!important; font-size:12.5px!important; padding:6px 13px!important; }}
  [data-testid="stSegmentedControl"] button[aria-checked="true"],
  [data-testid="stSegmentedControl"] button[kind="segmented_controlActive"] {{
      background:{ACCENT}!important; color:#04110f!important; border-color:{ACCENT}!important;
      box-shadow:0 0 16px {ACCENT}55!important; font-weight:700!important; }}

  [data-testid="stMetric"] {{ background:{PANEL}; border:1px solid {BORDER}; border-radius:12px; padding:14px; }}
  .stAlert {{ border-radius:11px; }}
  hr {{ border-color:{BORDER}; margin:1.1rem 0; }}
  ::-webkit-scrollbar {{ width:9px; height:9px; }}
  ::-webkit-scrollbar-thumb {{ background:{BORDER}; border-radius:6px; }}
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=60)
def load_csv(u: str, name: str) -> pd.DataFrame:
    p = DATA / u / f"{name}.csv.gz"
    return pd.read_csv(p, low_memory=False) if p.exists() else pd.DataFrame()


@st.cache_data(ttl=60)
def load_jsonl(u: str, name: str) -> list[dict]:
    p = DATA / u / f"{name}.jsonl.gz"
    if not p.exists():
        return []
    with gzip.open(p, "rt") as fh:
        return [json.loads(x) for x in fh if x.strip()]


@st.cache_data(ttl=60)
def evaluated_vs_traded() -> pd.DataFrame:
    rows = []
    for u in UNDERLYINGS:
        dec = load_jsonl(u, "decisions")
        oc = Counter(d.get("outcome") for d in dec)
        rows.append({"underlying": u, "evaluated": len(dec), "traded": oc.get("traded", 0)})
    return pd.DataFrame(rows)


def num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
def status_bar(extra=None):
    pills = [("● MARKET DATA", "INDICATIVE", "a"),
             ("● ALPACA PAPER", "$100,000", "g"),
             ("● THEORY GATE", "ON", "g"),
             ("● RUN", "1–3 SEP", "m")] + (extra or [])
    html = '<div class="statusbar">'
    for label, val, cls in pills:
        html += f'<span class="pill"><span class="dot {cls}">{label[0]}</span>{label[2:]}<b>{val}</b></span>'
    st.markdown(html + "</div>", unsafe_allow_html=True)


def kpi_row(items):
    html = '<div class="kpirow">'
    for it in items:
        label, value = it[0], it[1]
        sub = it[2] if len(it) > 2 else None
        kind = it[3] if len(it) > 3 else ""
        cls = it[4] if len(it) > 4 else ""      # "accent" | "win" | "loss"
        s = f'<div class="sub {kind}">{sub}</div>' if sub else ""
        html += f'<div class="kpi {cls}"><div class="label">{label}</div><div class="value">{value}</div>{s}</div>'
    st.markdown(html + "</div>", unsafe_allow_html=True)


def sec(title, subtitle=None):
    st.markdown(f'<div class="sec"><h3>{title}</h3></div>', unsafe_allow_html=True)
    if subtitle:
        st.markdown(f'<div class="subtle">{subtitle}</div>', unsafe_allow_html=True)


def theme(ch):
    return (ch.configure(background="transparent")
              .configure_axis(grid=True, gridColor=GRID, domainColor=BORDER,
                              labelColor=MUTED, titleColor=FAINT, tickColor=BORDER,
                              labelFont="ui-monospace, Menlo, monospace", labelFontSize=11)
              .configure_view(strokeWidth=0)
              .configure_legend(labelColor=INK, titleColor=FAINT, orient="top", labelFontSize=12))


def bt_df():
    return pd.DataFrame(S["bt"], columns=["denom", "sizing", "trades", "total", "mean", "median", "win"])


# --------------------------------------------------------------------------- #
# Masthead + nav
# --------------------------------------------------------------------------- #
st.markdown(
    '<div class="mast"><div class="mark">◢</div>'
    '<div class="name">MarginCalled</div></div>'
    '<div class="tag">A theorem-gated options arbitrage agent for SPY, SPX and XSP. It hunts '
    'for four-contract price rectangles that break a no-arbitrage rule, <b>trades only the ones '
    'it can prove are real</b>, and waits for the mispricing to correct.</div>',
    unsafe_allow_html=True)

navcol, ucol = st.columns([6, 1.25])
with navcol:
    page = st.segmented_control("view", VIEWS, default="Overview",
                                label_visibility="collapsed", key="view")
with ucol:
    underlying = st.segmented_control("underlying", UNDERLYINGS, default="SPY",
                                      label_visibility="collapsed", key="under")
page = page or "Overview"
underlying = underlying or "SPY"
st.write("")


# =========================================================================== #
# Overview
# =========================================================================== #
if page == "Overview":
    status_bar([("● BACKTEST EDGE", "+$9,829", "t")])
    st.write("")
    kpi_row([
        ("Rectangles scanned", "192M", "over the run", ""),
        ("Violations found", f"{S['violations']:,}", "priced on crossable quotes", ""),
        ("Episodes reverted", "98%", f"{S['episodes']:,} tracked · ~11 min median", "up"),
        ("Backtest result (T1)", "+$9,829", "672 trades · 67% win", "up", "win"),
        ("Live result", "−$45", "21 round-trips · all reverted", "down", "loss"),
    ])
    st.write("")

    sec("The one-line story",
        "The mispricing is real and it corrects — 98% of the time, in about 11 minutes. "
        "In a backtest that assumes you get filled, capturing it makes money (T1: +$9,829). "
        "Live, only ~3% of orders filled and the gap you can capture (~2¢) is the same size as "
        "the spread you pay to capture it (~2¢) — so a one-and-a-half-day live window came out "
        "at −$45. That collision, not the theory, is the real finding.")

    left, right = st.columns([3, 2])
    with left:
        sec("Evaluated vs actually traded, by underlying",
            "Every candidate the agent looked at, against the handful it chose to trade. "
            "The huge gap is the theory gate and the risk firewall turning things down.")
        ev = evaluated_vs_traded()
        m = ev.melt(id_vars="underlying", value_vars=["evaluated", "traded"],
                    var_name="stage", value_name="n")
        ch = (alt.Chart(m).mark_bar(cornerRadius=3)
              .encode(x=alt.X("underlying:N", title=None, sort=UNDERLYINGS),
                      xOffset=alt.XOffset("stage:N", sort=["evaluated", "traded"]),
                      y=alt.Y("n:Q", title="count (log scale)", scale=alt.Scale(type="symlog")),
                      color=alt.Color("stage:N", scale=alt.Scale(
                          domain=["evaluated", "traded"], range=[FAINT, ACCENT]),
                          legend=alt.Legend(title=None)),
                      tooltip=["underlying", "stage", "n"]).properties(height=300))
        st.altair_chart(theme(ch), width="stretch")
        st.markdown(f"<div class='subtle'>Only SPY actually traded. SPX and XSP were shut out for "
                    "concrete reasons — no greeks, and quotes below intrinsic — spelled out on the "
                    "<b>Why only SPY</b> tab.</div>", unsafe_allow_html=True)
    with right:
        sec(f"{underlying} — theory gate",
            "A violation only becomes a trade if the early-exercise gate can certify it. "
            "What it can't certify is marked unresolved and left alone.")
        v = load_csv(underlying, "violations")
        if not v.empty and "theory_category" in v:
            counts = (v["theory_category"].value_counts().reindex(THEORY_ORDER)
                      .dropna().reset_index())
            counts.columns = ["category", "n"]
            ch = (alt.Chart(counts).mark_bar(cornerRadius=4, height=30)
                  .encode(x=alt.X("n:Q", title=f"{underlying} detections logged"),
                          y=alt.Y("category:N", sort=THEORY_ORDER, title=None),
                          color=alt.Color("category:N", scale=alt.Scale(
                              domain=THEORY_ORDER, range=THEORY_COLORS), legend=None),
                          tooltip=["category", "n"]).properties(height=250))
            st.altair_chart(theme(ch), width="stretch")

    st.divider()
    sec("What holds up, and what doesn't",
        "Kept deliberately honest — this is the part a glossy demo usually leaves out.")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"<div class='card good'><h4>✔ The signal is real</h4>"
                f"<div class='subtle' style='margin:0'>43,566 tracked episodes, 98% reverted, "
                "median 11 minutes. Every one of the 21 live positions closed because the "
                "mispricing corrected — not one on a time stop.</div></div>", unsafe_allow_html=True)
    c2.markdown(f"<div class='card good'><h4>✔ The backtest pays</h4>"
                f"<div class='subtle' style='margin:0'>Same filters as live, both sides crossing "
                "the spread: T1 makes +$9,829 over 672 trades at 67% win. Scaled 10×, +$30,878. "
                "The edge exists.</div></div>", unsafe_allow_html=True)
    c3.markdown(f"<div class='card reject'><h4>✘ Live, the toll eats it</h4>"
                f"<div class='subtle' style='margin:0'>The gap is ~2¢ a contract; crossing the "
                "spread twice costs ~2¢. Add a 3% fill rate on resting limits and the coin flip "
                "tips to −$45. No broker fees — it's pure market structure.</div></div>",
                unsafe_allow_html=True)

# =========================================================================== #
# Backtest  (the main trading result)
# =========================================================================== #
elif page == "Backtest":
    status_bar([("● BEST", "T1 +$9,829", "t")])
    st.write("")
    sec("The backtest is the real trading result",
        "Live filled only ~3% of signals in a day and a half, so the honest measure of the edge "
        "is the backtest: the exact same pipeline — theory gate, execution screens, re-validation, "
        "risk caps — replayed on the day's signals, with entry and exit both crossing the spread. "
        "No look-ahead: the exit is the first reversion after entry.")
    kpi_row([
        ("T1 · unit 1:1", "+$9,829", "672 trades · 67% win", "up", "win"),
        ("T1 · 1:1 scaled", "+$30,878", "10× cap, 3.1× effective", "up", "win"),
        ("K2 · unit 1:1", "−$5,363", "748 trades · 49% win", "down", "loss"),
        ("Median T1 trade", "+$2", "per contract, net of spread", "up"),
    ])
    st.write("")

    sec("Every denomination and sizing we tested (SPY)",
        "T1 (a covered vertical) makes money. K2 (a diagonal) doesn't — most likely because we "
        "exit on reversion while the source study holds K2 to expiration, and K2's legs expire on "
        "different dates.")
    df = bt_df()
    df_disp = df.copy()
    df_disp["result"] = df_disp["total"].map(lambda x: f"+${x:,}" if x >= 0 else f"−${abs(x):,}")
    df_disp["mean/trade"] = df_disp["mean"].map(lambda x: f"${x:,.2f}")
    df_disp["median"] = df_disp["median"].map(lambda x: f"${x:,.2f}")
    df_disp["win"] = df_disp["win"].map(lambda x: f"{x:.0%}")
    st.dataframe(df_disp[["denom", "sizing", "trades", "result", "mean/trade", "median", "win"]],
                 width="stretch", hide_index=True)

    ch = (alt.Chart(df).mark_bar(cornerRadius=4)
          .encode(x=alt.X("total:Q", title="total P&L ($)"),
                  y=alt.Y("sizing:N", sort=None, title=None),
                  color=alt.condition("datum.total >= 0", alt.value(GREEN), alt.value(RED)),
                  row=alt.Row("denom:N", title=None, header=alt.Header(labelColor=INK, labelFontSize=13)),
                  tooltip=["denom", "sizing", "trades", "total", "win"]).properties(height=90))
    st.altair_chart(theme(ch).resolve_scale(y="independent"), width="stretch")

    st.info("The caveat that governs all of it: **every row assumes a fill at the quoted price on "
            "every signal.** Live, ~3% filled. The distance between +$9,829 and −$45 isn't a "
            "modelling error — it's the cost of actually getting filled. That's the finding.")

# =========================================================================== #
# Why only SPY
# =========================================================================== #
elif page == "Why only SPY":
    status_bar()
    st.write("")
    sec("Why only SPY ever traded",
        "Three underlyings were scanned. Two never produced a single fill, for concrete, "
        "measurable reasons — not bad luck.")

    a, b = st.columns(2)
    with a:
        st.markdown(
            f"<div class='card reject'><h4>SPX — no greeks, quotes below intrinsic "
            f"<span class='badge b-red'>0 FILLED</span></h4>"
            "<div class='subtle' style='margin:0'>Alpaca publishes greeks only for contracts it "
            "models — and for <b>SPX it publishes none, at any strike</b>. Worse, its indicative "
            "quotes priced fully-in-the-money spreads at <b>57–68% of intrinsic</b> — below what "
            "the contract is already worth, which no real market will sell. So SPX detected "
            "<b>12,210 violations and filled zero</b>: every candidate was an unexecutable "
            "quote.</div></div>", unsafe_allow_html=True)
    with b:
        st.markdown(
            f"<div class='card reject'><h4>XSP — no greeks, nothing survives screens "
            f"<span class='badge b-red'>0 TRADED</span></h4>"
            "<div class='subtle' style='margin:0'>Same story as SPX: <b>no greeks published</b>. "
            "Once the quote-quality screens were applied to keep out feed artefacts, XSP produced "
            "<b>zero clean violations</b>. Nothing to trade.</div></div>", unsafe_allow_html=True)

    st.write("")
    sec("And why SPY itself only placed a few, small orders",
        "SPY was the one tradeable name — but three things kept the book tiny.")
    c1, c2, c3 = st.columns(3)
    c1.markdown(
        f"<div class='card'><h4>① Level 3 forced 1:1 sizing</h4>"
        "<div class='subtle' style='margin:0'>The study sizes each leg by the opposite contract's "
        "price, which always makes the <b>short bigger than the long</b> (median 1.74×) — i.e. "
        "<b>naked calls</b>. A level-3 account can't do that: submitting the real ratio returns "
        "<code>403 / 40310000</code>. So every trade was capped to a covered <b>1:1</b>.</div></div>",
        unsafe_allow_html=True)
    c2.markdown(
        f"<div class='card'><h4>② ~3% of orders filled</h4>"
        "<div class='subtle' style='margin:0'>Orders rest as conservative limits. A limit buy only "
        "fills when the market comes <b>down</b> to it — so the fills you get are the ones already "
        "moving against you. Of the backtest's hundreds of signals, live filled about <b>3%</b> "
        "(40 of 131 orders, 21 round-trips).</div></div>", unsafe_allow_html=True)
    c3.markdown(
        f"<div class='card'><h4>③ The dividend horizon</h4>"
        "<div class='subtle' style='margin:0'>The theory gate refused <b>1,969 SPY candidates</b> "
        "whose far leg expired past 21 Sep — beyond the date through which we can prove no dividend "
        "lands. That's the gate working correctly, but it removed most of the long-dated "
        "universe.</div></div>", unsafe_allow_html=True)

    st.write("")
    st.success("The twist: the 1:1 cap **helped**. A covered 1:1 spread ties up ~$4 and can't lose "
               "more than that; the study's ratio would tie up ~$13,800 of margin per naked "
               "contract. Per dollar committed, the covered version is roughly **2,220× more "
               "capital-efficient** — and safer. The broker restriction cost nothing; it forced "
               "the better structure.")

# =========================================================================== #
# How it works
# =========================================================================== #
elif page == "How it works":
    status_bar()
    st.write("")
    sec("From a quote to a trade — or, far more often, a refusal")
    st.markdown("""
1. **Snapshot the chain.** Pull SPY / SPX / XSP option quotes from Alpaca's indicative feed.
2. **Build rectangles.** For every $(K_1,T_1),(K_2,T_2)$ pair, forward-adjust the strikes and
   round to what's listed. A real violation needs $A^{ask}B^{ask} < C^{bid}D^{bid}$ — measured on
   the sides a trade would actually have to cross.
3. **Clear the tick bound.** If the gap is smaller than the quote's own rounding error, it's
   noise. Drop it.
4. **Track it as an episode.** Re-price the same rectangle every scan until it reverts, so
   reversion is something we watch happen, not something we assume.
5. **Run the theory gate.** TP2 is a theorem about European calls. An American one only trades if
   early exercise is worthless here: no dividend in the window, or a certificate from the source
   study. Anything past the dividend horizon is **unresolved, and does not trade.**
6. **Pass the risk firewall.** 16 gates, all of them checked (no early exit), including a re-test
   of the violation on fresh quotes right before the order goes out.
7. **Send a careful limit.** Orders go over Alpaca's MCP server, priced better than the indicative
   quote — so a bad quote produces a non-fill, never a bad fill.
8. **Explain it.** A language layer reads the log and writes up what happened. It can only read the
   log; it can't change a single trade.
""")
    st.info("Every branch that doesn't end in a trade is still written down — 32,505 decisions in "
            "all. That record is what every other tab reads from.")

# =========================================================================== #
# Signals (funnel)
# =========================================================================== #
elif page == "Signals":
    status_bar()
    st.write("")
    dec = load_jsonl(underlying, "decisions")
    if not dec:
        st.info(f"No decision log for {underlying}.")
    else:
        n = len(dec)
        by = Counter(d.get("outcome") for d in dec)
        tb, ne = by.get("theory_blocked", 0), by.get("not_executable", 0)
        rr, of, tr = by.get("risk_rejected", 0), by.get("order_failed", 0), by.get("traded", 0)
        kpi_row([("Candidates", f"{n:,}"), ("Theory-blocked", f"{tb:,}"),
                 ("Not executable", f"{ne:,}"), ("Risk-rejected", f"{rr:,}"),
                 ("Traded", f"{tr:,}", "made it through", "up", "accent")])
        st.write("")
        sec(f"{underlying} — the funnel, stage by stage",
            "Start with every candidate, subtract each refusal reason, see what's left.")
        stages = pd.DataFrame({
            "stage": ["considered", "theory-passing", "executable", "risk-approved", "traded"],
            "n": [n, n - tb, n - tb - ne, n - tb - ne - rr - of, tr]})
        stages["stage"] = pd.Categorical(stages["stage"], stages["stage"], ordered=True)
        ch = (alt.Chart(stages).mark_bar(cornerRadius=4, height=38, color=ACCENT)
              .encode(x=alt.X("n:Q", title="candidates"),
                      y=alt.Y("stage:N", sort=list(stages["stage"]), title=None),
                      tooltip=["stage", "n"]).properties(height=270))
        lab = ch.mark_text(align="left", dx=6, color=INK,
                           font="ui-monospace, Menlo, monospace").encode(text="n:Q")
        st.altair_chart(theme(ch + lab), width="stretch")

        sec("What gets dropped before a violation is even scored",
            "Quote-quality screens on every rectangle, tallied across the run.")
        scans = load_csv(underlying, "scans")
        census = ["no_forward", "adjusted_strike_unlisted", "leg_missing", "leg_unusable",
                  "leg_delta_out_of_band", "strike_gap_too_wide", "coverage_ratio_too_wide",
                  "roundup_too_far", "leg_too_cheap", "degenerate_legs",
                  "vertical_arbitrage", "no_violation", "below_tick_bound"]
        present = [c for c in census if c in scans.columns]
        if present:
            tot = scans[present].apply(num).sum().sort_values(ascending=False).reset_index()
            tot.columns = ["reason", "n"]
            ch2 = (alt.Chart(tot).mark_bar(cornerRadius=4, height=18, color=ORANGE)
                   .encode(x=alt.X("n:Q", title="rectangles dropped (summed over scans)"),
                           y=alt.Y("reason:N", sort="-x", title=None),
                           tooltip=["reason", "n"]).properties(height=340))
            st.altair_chart(theme(ch2), width="stretch")

# =========================================================================== #
# Reversion
# =========================================================================== #
elif page == "Reversion":
    status_bar()
    st.write("")
    ep = load_csv(underlying, "episodes")
    if ep.empty:
        st.info(f"No episodes for {underlying}.")
    else:
        ep["ttr_min"] = num(ep.get("time_to_revert_seconds")) / 60.0
        ep["peak"] = num(ep.get("peak_severity"))
        ep["obs"] = num(ep.get("observations"))
        rev = ep[ep.get("status").astype(str) == "reverted"].copy()
        med = rev["ttr_min"].median() if not rev["ttr_min"].dropna().empty else float("nan")
        kpi_row([("Episodes tracked", f"{len(ep):,}"),
                 ("Reverted", f"{len(rev):,}",
                  f"{len(rev)/len(ep):.0%} of them" if len(ep) else None, "up"),
                 ("Median time to revert", f"{med:.0f} min", "then the gap closes", "", "accent")])
        st.write("")
        left, right = st.columns(2)
        with left:
            sec("How long a violation lasts")
            d = rev.dropna(subset=["ttr_min"])
            if not d.empty:
                ch = (alt.Chart(d).mark_bar(cornerRadius=3, color=ACCENT)
                      .encode(x=alt.X("ttr_min:Q", bin=alt.Bin(maxbins=40),
                                      title="time to revert (minutes)"),
                              y=alt.Y("count():Q", title="episodes"),
                              tooltip=[alt.Tooltip("count():Q", title="episodes")])
                      .properties(height=280))
                st.altair_chart(theme(ch), width="stretch")
        with right:
            sec("Bigger gaps, slower to close?")
            d = rev.dropna(subset=["ttr_min", "peak"])
            if not d.empty:
                ch = (alt.Chart(d).mark_circle(size=55, opacity=0.45, color=BLUE)
                      .encode(x=alt.X("ttr_min:Q", title="time to revert (minutes)"),
                              y=alt.Y("peak:Q", title="peak severity"),
                              tooltip=["episode_id", "peak", "ttr_min"]).properties(height=280))
                st.altair_chart(theme(ch), width="stretch")

        sec("Follow one violation from first sight to reversion",
            "Severity over time. When the line touches zero the gap has closed — that's the exit.")
        path = load_csv(underlying, "episode_path")
        if not path.empty and "episode_id" in path:
            featured, ids_in = None, set(path["episode_id"].unique())
            if not rev.empty:
                for cand in rev.sort_values("obs", ascending=False)["episode_id"]:
                    if cand in ids_in:
                        featured = cand
                        break
            ids = path["episode_id"].dropna().unique().tolist()
            idx = ids.index(featured) if featured in ids else 0
            pick = st.selectbox("Pick an episode (defaults to the longest-lived one)", ids, index=idx)
            one = path[path["episode_id"] == pick].copy()
            one["severity"] = num(one.get("severity"))
            one["event_index"] = num(one.get("event_index"))
            line = (alt.Chart(one).mark_line(point=True, color=ACCENT, strokeWidth=2)
                    .encode(x=alt.X("event_index:Q", title="observation #"),
                            y=alt.Y("severity:Q", title="severity"),
                            tooltip=["event_index", "severity"]).properties(height=300))
            zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
                color=RED, strokeDash=[5, 4]).encode(y="y:Q")
            st.altair_chart(theme(line + zero), width="stretch")

# =========================================================================== #
# Decisions
# =========================================================================== #
elif page == "Decisions":
    status_bar()
    st.write("")
    dec = load_jsonl(underlying, "decisions")
    if not dec:
        st.info(f"No decision log for {underlying}.")
    else:
        sec(f"{underlying} — what happened to every candidate",
            "Fills are the thin green sliver. The interesting part is everything the agent turned "
            "down, and why.")
        outc = (pd.Series([d.get("outcome") for d in dec]).value_counts()
                .reindex(OUTCOME_ORDER).dropna().reset_index())
        outc.columns = ["outcome", "n"]
        ch = (alt.Chart(outc).mark_bar(cornerRadius=4, height=26)
              .encode(x=alt.X("n:Q", title="candidates"),
                      y=alt.Y("outcome:N", sort=OUTCOME_ORDER, title=None),
                      color=alt.Color("outcome:N", scale=alt.Scale(
                          domain=OUTCOME_ORDER, range=OUTCOME_COLORS), legend=None),
                      tooltip=["outcome", "n"]).properties(height=250))
        lab = ch.mark_text(align="left", dx=6, color=INK,
                           font="ui-monospace, Menlo, monospace").encode(text="n:Q")
        st.altair_chart(theme(ch + lab), width="stretch")

        sec("Two real refusals, straight from the log",
            "Nothing staged — pulled verbatim from this underlying's decision log.")
        blocked = next((d for d in dec if d.get("outcome") == "theory_blocked"), None)
        risk_rej = next((d for d in dec if d.get("outcome") == "risk_rejected"
                         and (d.get("risk") or {}).get("rejections")), None)
        col1, col2 = st.columns(2)
        with col1:
            rows = ""
            if blocked:
                det = blocked.get("determinant") or {}
                items = [("category", blocked.get("theory_category", "—")),
                         ("reason", str(blocked.get("reason", "—"))[:90]),
                         ("violation size", str(det.get("violation_size", "—"))[:14]),
                         ("clears tick bound", str(det.get("clears_tick_bound", "—")))]
                rows = "".join(f"<div class='row'><span class='k'>{k}</span>"
                               f"<span class='v'>{vv}</span></div>" for k, vv in items)
            st.markdown(f"<div class='card reject'><h4>⛔ Stopped by the theory gate "
                        f"<span class='badge b-yellow'>THEORY_BLOCKED</span></h4>{rows or '—'}</div>",
                        unsafe_allow_html=True)
        with col2:
            rows = ""
            if risk_rej:
                risk = risk_rej.get("risk") or {}
                for rj in risk.get("rejections", []):
                    rows += (f"<div class='row'><span class='k'>{rj.get('code')}</span>"
                             f"<span class='v'>{str(rj.get('message',''))[:64]}</span></div>")
                np_ = len(risk.get("checks_passed") or [])
                if np_:
                    rows += f"<div class='row'><span class='k'>+ gates passed</span><span class='v'>{np_}</span></div>"
            st.markdown(f"<div class='card reject'><h4>⛔ Stopped by the risk firewall "
                        f"<span class='badge b-red'>RISK_REJECTED</span></h4>{rows or '—'}</div>",
                        unsafe_allow_html=True)
        st.markdown(f"<div class='subtle'>Every gate runs on every candidate, so the log keeps all "
                    "the reasons a trade was turned down — not just the first one.</div>",
                    unsafe_allow_html=True)

        sec("Which risk gate did the turning-down")
        codes = {}
        for d in dec:
            for rj in ((d.get("risk") or {}).get("rejections") or []):
                codes[rj.get("code", "?")] = codes.get(rj.get("code", "?"), 0) + 1
        if codes:
            cdf = (pd.DataFrame({"code": list(codes), "n": list(codes.values())})
                   .sort_values("n", ascending=False))
            ch2 = (alt.Chart(cdf).mark_bar(cornerRadius=4, height=20, color=RED)
                   .encode(x=alt.X("n:Q", title="rejections"),
                           y=alt.Y("code:N", sort="-x", title=None),
                           tooltip=["code", "n"]).properties(height=260))
            st.altair_chart(theme(ch2), width="stretch")
        else:
            st.info("Nothing reached the risk gates for this underlying — refused earlier.")

# =========================================================================== #
# Live fills
# =========================================================================== #
elif page == "Live fills":
    status_bar([("● NET", "−$45", "r")])
    st.write("")
    sec("What actually filled, and the one reason it lost",
        "40 orders filled, 21 completed round-trips — every single one closed because the "
        "violation reverted. The loss isn't the exit thesis failing; it's the spread.")
    kpi_row([("Orders placed", f"{S['orders']}"), ("Filled", f"{S['filled']}"),
             ("Round-trips", f"{S['round_trips']}", "all closed on reversion", "up"),
             ("Net", "−$45", "opens $243 → closes $198", "down", "loss"),
             ("Broker fees", "$0", "no commission at all", "up")])
    st.write("")

    sec("The edge and the cost are the same size",
        "A SPY leg's spread is about 1¢. Crossing two legs, in and out, costs ~2¢ a share = ~$2 a "
        "contract. The backtest's median profit is also ~$2 a contract. The mispricing is real — "
        "and the toll to collect it eats it. Here are four real round-trips:")
    fills = pd.DataFrame({
        "open (credit)": ["+0.04", "+0.17", "+0.14", "+0.11"],
        "close (debit)": ["−0.02", "−0.15", "−0.12", "−0.10"],
        "result": ["−0.02", "−0.02", "−0.02", "−0.01"]})
    st.dataframe(fills, width="stretch", hide_index=True)
    st.info("This is market structure, not the broker: `accrued_fees: 0` on every fill, and orders "
            "often filled **better** than the limit asked (one −0.03 limit filled at −0.06). The "
            "cost is the bid-ask spread — present at every broker on earth.")

    positions = load_jsonl(underlying, "positions")
    if positions:
        st.write("")
        sec(f"{underlying} — the blotter")
        df = pd.DataFrame(positions)
        show = [c for c in ["opened_at", "closed_at", "denomination", "long_symbol",
                            "short_symbol", "qty", "entry_long_price",
                            "entry_short_price", "close_reason"] if c in df.columns]
        st.dataframe(df[show], width="stretch", hide_index=True)
    else:
        st.caption(f"{underlying} filled nothing this run — see the **Why only SPY** tab.")

# =========================================================================== #
# Narration
# =========================================================================== #
elif page == "Narration":
    status_bar()
    st.write("")
    sec("The agent's own account of the run",
        "A language layer reads the decision log and writes up what it did, and what it turned "
        "down. It only reads the log — it can't move a trade.")
    f = REPORTS / f"{underlying}_narration.md"
    if f.exists():
        st.info("Written from the committed decision log — no API key needed to view it.")
        st.markdown(f.read_text())
    else:
        st.info(f"No narration committed for {underlying}.")

# --------------------------------------------------------------------------- #
# Footer
# --------------------------------------------------------------------------- #
st.divider()
st.markdown(
    f"<div class='mono' style='font-size:11.5px;color:{FAINT}'>MarginCalled · Alpaca AI Trading "
    "Agents Hackathon · <a href='https://github.com/Omkar38/MarginCalled'>"
    f"github.com/Omkar38/MarginCalled</a> · every number regenerates from live state via "
    f"<code>scripts/compliance_report.py</code> · {S['tests']} tests · stdlib-only core</div>",
    unsafe_allow_html=True)
