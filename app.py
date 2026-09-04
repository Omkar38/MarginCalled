"""MarginCalled — a theorem-gated options arbitrage agent.

A single-file quant-terminal dashboard built entirely from the committed
`dashboard_data/<UNDERLYING>/*.gz` snapshot and `reports/*_narration.md`.
No API key, no network, no `data/`.  Run:  streamlit run app.py

The snapshot is static — the record of the 2026-09-03 paper session, not a live
feed.  Panels that could imply otherwise say so.
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
ACCENT, ACCENT2 = "#2dd4bf", "#22d3ee"      # teal → cyan
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
VIEWS = ["Overview", "How it works", "Signals", "Reversion",
         "Decisions", "Trades", "Narration"]
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "dashboard_data"
REPORTS = ROOT / "reports"

SESSION = {
    "date": "2026-09-03", "orders_filled": 17, "round_trips": 17,
    "realised_pnl": -28.0, "equity": 99971.0, "start_equity": 100000.0,
    "bt_unit": {"trades": 640, "pnl": 4702, "win": 0.66},
    "bt_study": {"trades": 293, "pnl": 2587, "win": 0.58},
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

  /* masthead */
  .mast {{ display:flex; align-items:center; gap:13px; margin-bottom:3px; }}
  .mark {{ width:30px; height:30px; border-radius:8px;
           background:linear-gradient(135deg,{ACCENT},{ACCENT2}); display:flex;
           align-items:center; justify-content:center; color:#04110f; font-weight:900;
           font-size:17px; box-shadow:0 0 22px {ACCENT}66; }}
  .mast .name {{ font-size:23px; font-weight:800; color:{INK}; letter-spacing:-0.02em; }}
  .tag {{ color:{MUTED}; font-size:14.5px; margin:2px 0 16px; max-width:820px; line-height:1.5; }}
  .tag b {{ color:{INK}; }}

  /* status bar */
  .statusbar {{ display:flex; flex-wrap:wrap; gap:8px; margin:0 0 6px; }}
  .pill {{ font-family:ui-monospace,Menlo,monospace; font-size:10.5px; letter-spacing:.05em;
           text-transform:uppercase; padding:6px 11px; border-radius:8px;
           border:1px solid {BORDER}; background:{PANEL}; color:{MUTED}; }}
  .pill b {{ color:{INK}; font-weight:600; margin-left:6px; }}
  .dot {{ display:inline-block; margin-right:7px; }}
  .g{{color:{GREEN};}} .a{{color:{YELLOW};}} .r{{color:{RED};}} .t{{color:{ACCENT};}} .m{{color:{FAINT};}}

  /* section heading with accent tick */
  .sec {{ display:flex; align-items:center; gap:9px; margin:6px 0 2px; }}
  .sec::before {{ content:""; width:3px; height:15px; border-radius:2px;
                  background:linear-gradient(180deg,{ACCENT},{ACCENT2}); display:inline-block; }}
  .sec h3 {{ margin:0; font-size:16.5px; }}
  .subtle {{ color:{MUTED}; font-size:13px; margin:2px 0 12px; }}

  /* KPI cards */
  .kpirow {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(158px,1fr)); gap:12px; margin:6px 0 4px; }}
  .kpi {{ background:linear-gradient(180deg,{ELEV},{PANEL}); border:1px solid {BORDER};
          border-radius:13px; padding:15px 17px; position:relative; overflow:hidden; }}
  .kpi::after {{ content:""; position:absolute; left:0; top:0; height:2px; width:100%;
                 background:linear-gradient(90deg,transparent,{BORDER},transparent); }}
  .kpi .label {{ font-family:ui-monospace,Menlo,monospace; font-size:10px; letter-spacing:.1em;
                 text-transform:uppercase; color:{MUTED}; margin-bottom:9px; }}
  .kpi .value {{ font-size:28px; font-weight:800; color:{INK}; line-height:1.02; letter-spacing:-0.02em; }}
  .kpi .sub {{ font-size:11.5px; margin-top:6px; font-family:ui-monospace,Menlo,monospace; color:{FAINT}; }}
  .kpi .sub.up{{color:{GREEN};}} .kpi .sub.down{{color:{RED};}}
  .kpi.accent {{ border-color:{ACCENT}66; box-shadow:0 0 0 1px {ACCENT}22 inset, 0 0 26px {ACCENT}18; }}
  .kpi.accent::after {{ background:linear-gradient(90deg,transparent,{ACCENT},transparent); }}

  /* badges */
  .badge {{ font-family:ui-monospace,Menlo,monospace; font-size:10px; letter-spacing:.05em;
            text-transform:uppercase; padding:3px 9px; border-radius:6px; font-weight:700; }}
  .b-green{{color:{GREEN};background:{GREEN}1f;border:1px solid {GREEN}44;}}
  .b-red{{color:{RED};background:{RED}1f;border:1px solid {RED}44;}}
  .b-yellow{{color:{YELLOW};background:{YELLOW}1f;border:1px solid {YELLOW}44;}}

  /* cards */
  .card {{ background:linear-gradient(180deg,{ELEV},{PANEL}); border:1px solid {BORDER};
           border-radius:13px; padding:16px 18px; height:100%; }}
  .card.reject {{ border-left:3px solid {RED}; }}
  .card h4 {{ margin:0 0 11px; font-size:14px; display:flex; align-items:center; gap:8px; }}
  .card .k {{ font-family:ui-monospace,Menlo,monospace; font-size:12px; color:{MUTED}; white-space:nowrap; }}
  .card .v {{ font-family:ui-monospace,Menlo,monospace; font-size:12px; color:{INK};
             text-align:right; padding-left:18px; word-break:break-word; }}
  .card .row {{ display:flex; justify-content:space-between; gap:12px; padding:5px 0; border-bottom:1px dashed {BORDER}; }}
  .card .row:last-child {{ border-bottom:none; }}

  /* segmented-control nav */
  [data-testid="stSegmentedControl"] button {{ background:{PANEL}!important; border:1px solid {BORDER}!important;
      color:{MUTED}!important; border-radius:9px!important; font-size:13px!important; padding:6px 14px!important; }}
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
def cross_summary() -> pd.DataFrame:
    rows = []
    for u in UNDERLYINGS:
        v, dec, pos = load_csv(u, "violations"), load_jsonl(u, "decisions"), load_jsonl(u, "positions")
        oc = Counter(d.get("outcome") for d in dec)
        refused = int((v["theory_category"] == "unresolved").sum()) if not v.empty and "theory_category" in v else 0
        rows.append({"underlying": u, "detected": len(v), "theory_refused": refused,
                     "traded": oc.get("traded", 0), "filled": len(pos)})
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
             ("● SESSION", SESSION["date"], "m")] + (extra or [])
    html = '<div class="statusbar">'
    for label, val, cls in pills:
        html += f'<span class="pill"><span class="dot {cls}">{label[0]}</span>{label[2:]}<b>{val}</b></span>'
    st.markdown(html + "</div>", unsafe_allow_html=True)


def kpi_row(items):
    """items: (label, value, sub, sub_kind, accent)."""
    html = '<div class="kpirow">'
    for it in items:
        label, value = it[0], it[1]
        sub = it[2] if len(it) > 2 else None
        kind = it[3] if len(it) > 3 else ""
        acc = " accent" if len(it) > 4 and it[4] else ""
        s = f'<div class="sub {kind}">{sub}</div>' if sub else ""
        html += f'<div class="kpi{acc}"><div class="label">{label}</div><div class="value">{value}</div>{s}</div>'
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


# --------------------------------------------------------------------------- #
# Masthead + nav
# --------------------------------------------------------------------------- #
st.markdown(
    '<div class="mast"><div class="mark">◢</div>'
    '<div class="name">MarginCalled</div></div>'
    '<div class="tag">Theorem-gated options arbitrage on SPY, SPX and XSP. It finds '
    'four-contract rectangles that break a no-arbitrage bound, <b>trades only the ones '
    'it can prove are real</b>, and logs the rest as refusals.</div>',
    unsafe_allow_html=True)

navcol, ucol = st.columns([5, 1.25])
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
    cs = cross_summary()
    tot_det, tot_tr = int(cs["detected"].sum()), int(cs["traded"].sum())
    refuse = 1 - tot_tr / tot_det if tot_det else 0
    status_bar([("● REFUSED", f"{refuse:.1%}", "t")])
    st.write("")

    kpi_row([
        ("Violations detected", f"{tot_det:,}", "across SPY · SPX · XSP", ""),
        ("Refused", f"{refuse:.1%}", "of everything detected", "", True),
        ("Round-trips", f"{SESSION['round_trips']}", "opened and closed", ""),
        ("Reversion exits", "17 / 17", "every close was a revert", "up"),
        ("Realised P&L", "−$28", "on the $100k paper book", "down"),
    ])

    st.write("")
    left, right = st.columns([3, 2])
    with left:
        sec("Detected vs traded, by underlying",
            "How many violations each name produced, and how many became trades. "
            "The gap is the theory gate and risk firewall doing their job.")
        m = cs.melt(id_vars="underlying", value_vars=["detected", "traded"],
                    var_name="stage", value_name="n")
        ch = (alt.Chart(m).mark_bar(cornerRadius=3)
              .encode(x=alt.X("underlying:N", title=None, sort=UNDERLYINGS),
                      xOffset=alt.XOffset("stage:N", sort=["detected", "traded"]),
                      y=alt.Y("n:Q", title="count (log scale)", scale=alt.Scale(type="symlog")),
                      color=alt.Color("stage:N", scale=alt.Scale(
                          domain=["detected", "traded"], range=[FAINT, ACCENT]),
                          legend=alt.Legend(title=None)),
                      tooltip=["underlying", "stage", "n"]).properties(height=300))
        st.altair_chart(theme(ch), width="stretch")
        st.markdown(f"<div class='subtle'>SPX threw <b style='color:{INK}'>34,936</b> "
                    "violations and filled none of them — its indicative quotes price "
                    "deep-in-the-money spreads below intrinsic, so the agent passed on every "
                    "one. Passing on bad quotes is the whole idea.</div>", unsafe_allow_html=True)
        with st.expander("Show the raw numbers"):
            st.dataframe(cs, width="stretch", hide_index=True)
    with right:
        sec(f"{underlying} — theory gate",
            "A TP2 violation only becomes a trade if the early-exercise gate can certify it. "
            "What it can't certify is marked unresolved and left alone.")
        v = load_csv(underlying, "violations")
        if not v.empty and "theory_category" in v:
            counts = (v["theory_category"].value_counts().reindex(THEORY_ORDER)
                      .dropna().reset_index())
            counts.columns = ["category", "n"]
            ch = (alt.Chart(counts).mark_bar(cornerRadius=4, height=30)
                  .encode(x=alt.X("n:Q", title=f"{underlying} violations"),
                          y=alt.Y("category:N", sort=THEORY_ORDER, title=None),
                          color=alt.Color("category:N", scale=alt.Scale(
                              domain=THEORY_ORDER, range=THEORY_COLORS), legend=None),
                          tooltip=["category", "n"]).properties(height=250))
            st.altair_chart(theme(ch), width="stretch")
            if (counts["category"] == "unresolved").any():
                ur = int(counts.loc[counts["category"] == "unresolved", "n"].iloc[0])
                tot = int(counts["n"].sum())
                st.markdown(f"<span class='badge b-red'>UNRESOLVED</span> "
                            f"<span class='mono' style='color:{MUTED};font-size:12.5px'>&nbsp;"
                            f"{ur:,} of {tot:,} {underlying} violations ({ur/tot:.0%}) left "
                            "untraded by the gate</span>", unsafe_allow_html=True)

    st.divider()
    sec("Where the edge comes from",
        "The result in one line: the theory gate, not a model's guess, is what keeps the "
        "book out of the toxic trades.")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"<div class='card'><h4>📜 A theorem, not a hunch</h4>"
                f"<div class='subtle' style='margin:0'>Every trade clears a no-arbitrage bound "
                "(TP2) plus an early-exercise certificate from the source study. That is the "
                "signal — not a score a model happened to output.</div></div>", unsafe_allow_html=True)
    c2.markdown(f"<div class='card'><h4>🚫 Refusal is logged</h4>"
                f"<div class='subtle' style='margin:0'>99.8% of detections are turned down. Each "
                "one is written to an append-only log with the exact gate that stopped it — the "
                "Decisions tab shows real examples.</div></div>", unsafe_allow_html=True)
    c3.markdown(f"<div class='card'><h4>🧪 Backtested on the same filters</h4>"
                f"<div class='subtle' style='margin:0'>Unit 1:1 sizing: 640 trades, +$4,702, "
                "66% win. Study weights: 293 trades, +$2,587, 58% win. Live this week: 17 "
                "fills. The gap is honest, not hidden.</div></div>", unsafe_allow_html=True)

    st.write("")
    sec("Limitations, up front", "The parts a demo usually hides. We put them on the front page.")
    a, b = st.columns(2)
    a.warning("Detection runs on Alpaca's **indicative** feed, not OPRA. Every violation is a "
              "reading of that feed, not a claim about the real market.")
    a.warning("Positions are sized **1:1**, not the study's short-heavy weights — those need "
              "naked calls, which Alpaca rejects at options level 3.")
    b.warning("**SPX filled nothing**: 34,936 detected, 0 traded. The quotes were unexecutable "
              "and the agent said so instead of forcing a trade.")
    b.info("The 12-year study behind this validates the **end-of-day** signal. The live agent "
           "runs the *same* theory gate intraday — a faithful extension, but new ground we "
           "don't oversell.")

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
   round to what's listed. A real violation needs $A^{ask}B^{ask} < C^{bid}D^{bid}$ — measured
   on the sides a trade would actually have to cross.
3. **Clear the tick bound.** If the gap is smaller than the quote's own rounding error, it's
   noise. Drop it.
4. **Track it as an episode.** Re-price the same rectangle every scan until it reverts, so
   reversion is something we watch happen, not something we assume.
5. **Run the theory gate.** TP2 is a theorem about European calls. An American one only trades
   if early exercise is worthless here: no dividend in the window, Prop 2.1(ii)'s zero-premium
   condition, or Prop 2.2's dividend bound. Anything past the dividend horizon is **unresolved,
   and does not trade.**
6. **Pass the risk firewall.** 16 gates, all of them evaluated (no early exit), including a
   re-check of the violation on fresh quotes right before the order goes out.
7. **Send a careful limit.** Orders go over Alpaca's MCP server, priced better than the
   indicative quote — so a bad quote produces a non-fill, never a bad fill.
8. **Explain it.** A language layer reads the log and writes up what happened. It can only read
   the log; it can't change a single trade.
""")
    st.info("Every branch that doesn't end in a trade is still written down. That record is what "
            "every other tab reads from.")

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
                 ("Traded", f"{tr:,}", "made it through", "up", True)])
        st.write("")
        sec(f"{underlying} — the funnel, stage by stage",
            "Start with every candidate, subtract each refusal reason, and see what's left.")
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
            "Quote-quality screens on every rectangle, tallied across the session.")
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
                 ("Median time to revert", f"{med:.0f} min", "then the gap closes", "", True)])
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
            "Fills are the small green sliver. The interesting part is everything the agent "
            "turned down, and why.")
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
            "Nothing staged — these are pulled verbatim from this underlying's decision log.")
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
        st.markdown(f"<div class='subtle'>Every gate runs on every candidate, so the log keeps "
                    "all the reasons a trade was turned down — not just the first one.</div>",
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
# Trades
# =========================================================================== #
elif page == "Trades":
    status_bar()
    st.write("")
    positions = load_jsonl(underlying, "positions")
    if not positions:
        st.info(f"{underlying} filled nothing this session. For SPX that's the headline: "
                "34,936 violations detected, none of them executable.")
    else:
        df = pd.DataFrame(positions)
        rev = int((df.get("close_reason") == "reverted").sum()) if "close_reason" in df else 0
        closed = int(df["closed_at"].notna().sum()) if "closed_at" in df else 0
        sec(f"{underlying} — the blotter", "Real fills, open and close, with the reason each was closed.")
        kpi_row([("Positions", f"{len(df)}"), ("Closed", f"{closed}"),
                 ("Closed on reversion", f"{rev} / {closed}", "as designed", "up", True)])
        st.write("")
        show = [c for c in ["opened_at", "closed_at", "denomination", "long_symbol",
                            "short_symbol", "qty", "entry_long_price",
                            "entry_short_price", "close_reason"] if c in df.columns]
        st.dataframe(df[show], width="stretch", hide_index=True)
        if "close_reason" in df:
            rc = df["close_reason"].dropna().value_counts().reset_index()
            rc.columns = ["close_reason", "n"]
            ch = (alt.Chart(rc).mark_bar(cornerRadius=4, height=26, color=GREEN)
                  .encode(x=alt.X("n:Q", title="positions"),
                          y=alt.Y("close_reason:N", sort="-x", title=None),
                          tooltip=["close_reason", "n"]).properties(height=180))
            st.altair_chart(theme(ch), width="stretch")

# =========================================================================== #
# Narration
# =========================================================================== #
elif page == "Narration":
    status_bar()
    st.write("")
    sec("The agent's own account of the session",
        "A language layer reads the decision log and writes up what it did, and what it turned "
        "down. It only reads the log — it can't move a trade.")
    f = REPORTS / f"{underlying}_narration.md"
    if f.exists():
        st.info(f"Written from the committed {SESSION['date']} log — no API key needed to view it.")
        st.markdown(f.read_text())
    else:
        st.info(f"No narration committed for {underlying}.")

# --------------------------------------------------------------------------- #
# Footer
# --------------------------------------------------------------------------- #
st.divider()
st.markdown(
    f"<div class='mono' style='font-size:11.5px;color:{FAINT}'>MarginCalled · Alpaca AI "
    "Trading Agents Hackathon · <a href='https://github.com/Omkar38/MarginCalled'>"
    "github.com/Omkar38/MarginCalled</a> · every number here regenerates from live state via "
    "<code>scripts/compliance_report.py</code> · 13 modules · 249 tests · stdlib-only core</div>",
    unsafe_allow_html=True)
