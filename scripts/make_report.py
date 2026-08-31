#!/usr/bin/env python3
"""Render the scan CSVs as an HTML report with inline SVG charts.

Standard library only - no matplotlib, no pandas. Charts are hand-built SVG so the
project keeps its zero-dependency property and the output is a single self-contained
file that opens anywhere.

What it plots, and why:

  Margin distribution   The normalized TP2 margin (rhs - lhs) / rhs for every
                        rectangle that reached the violation test. Positive is a
                        violation. With zero violations this distribution is the
                        only evidence available, and its *shape* is the finding: a
                        real market produces margins crowding toward zero, while a
                        fitted arbitrage-free surface holds them bounded away.

  Closest over time     The per-scan maximum margin - how near the market came to
                        violating. Flat and far from zero means the feed is
                        synthetic, not that the market is quiet.

  Screen funnel         Where rectangles are lost. Distinguishes "nothing to find"
                        from "a screen is mis-set".

USAGE.
    python3 scripts/make_report.py                     # -> reports/scan_report.html
    python3 scripts/make_report.py --out somewhere.html
"""

from __future__ import annotations

import argparse
import html
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.store import HIST_EDGES, ScanStore  # noqa: E402

# Validated palette (dataviz reference instance).
# Ordinal ramps verified with validate_palette.js --ordinal in both modes.
LIGHT = {
    "surface": "#fcfcfb", "plane": "#f9f9f7",
    "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
    "grid": "#e1e0d9", "axis": "#c3c2b7",
    "series": "#2a78d6",
    "ramp": ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"],
    "good": "#0ca30c", "critical": "#d03b3b", "warning": "#fab219",
}
DARK = {
    "surface": "#1a1a19", "plane": "#0d0d0d",
    "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
    "grid": "#2c2c2a", "axis": "#383835",
    "series": "#3987e5",
    "ramp": ["#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#184f95"],
    "good": "#0ca30c", "critical": "#d03b3b", "warning": "#fab219",
}


def esc(text) -> str:
    return html.escape(str(text))


def _fmt_edge(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1:
        return f"{value:g}"
    return f"{value:.3f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- histogram


def svg_histogram(counts: list[int], width: int = 860, height: int = 300) -> str:
    """Bar chart of the margin distribution, with the violation threshold marked."""
    if not counts or not any(counts):
        return '<p class="empty">No margin samples recorded yet.</p>'

    pad_l, pad_r, pad_t, pad_b = 54, 16, 16, 52
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(counts)
    top = max(counts)
    slot = plot_w / n
    gap = 2.0  # surface gap between adjacent bars
    bar_w = max(1.0, slot - gap)

    # Index of the bin whose left edge is 0 - the violation threshold.
    zero_idx = next((i for i, e in enumerate(HIST_EDGES) if e >= 0), n)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Distribution of the normalized TP2 margin">'
    ]

    # Recessive gridlines + y axis
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h * (1 - frac)
        value = int(round(top * frac))
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'class="grid"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="tick" '
            f'text-anchor="end">{value:,}</text>'
        )

    # Bars, anchored to the baseline with rounded data-ends
    for i, count in enumerate(counts):
        if count <= 0:
            continue
        h = plot_h * (count / top)
        x = pad_l + i * slot + gap / 2
        y = pad_t + plot_h - h
        violating = i >= zero_idx
        cls = "bar-violation" if violating else "bar"
        lo, hi = HIST_EDGES[i], HIST_EDGES[i + 1]
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'rx="4" class="{cls}"><title>margin {_fmt_edge(lo)} to '
            f'{_fmt_edge(hi)}\n{count:,} rectangles'
            f'{" — VIOLATION" if violating else ""}</title></rect>'
        )

    # Violation threshold rule at margin = 0
    zx = pad_l + zero_idx * slot
    parts.append(
        f'<line x1="{zx:.1f}" y1="{pad_t}" x2="{zx:.1f}" y2="{pad_t + plot_h}" '
        f'class="threshold"/>'
        f'<text x="{zx + 6:.1f}" y="{pad_t + 12}" class="threshold-label">'
        f'violation threshold (margin = 0)</text>'
    )

    # Baseline and selective x labels
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" '
        f'y2="{pad_t + plot_h}" class="axis"/>'
    )
    for i in range(0, n, max(1, n // 9)):
        x = pad_l + i * slot + slot / 2
        parts.append(
            f'<text x="{x:.1f}" y="{pad_t + plot_h + 18}" class="tick" '
            f'text-anchor="middle">{_fmt_edge(HIST_EDGES[i])}</text>'
        )
    parts.append(
        f'<text x="{pad_l + plot_w / 2:.1f}" y="{height - 8}" class="axis-title" '
        f'text-anchor="middle">normalized margin (rhs − lhs) / rhs</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------- time series


def svg_timeseries(rows: list[dict], width: int = 860, height: int = 260) -> str:
    """Per-scan closest margin, with the violation threshold marked."""
    points = []
    for row in rows:
        try:
            value = float(row["margin_max"])
        except (KeyError, ValueError):
            continue
        if math.isnan(value):
            continue
        points.append((row["ts"], value))
    if len(points) < 2:
        return '<p class="empty">Need at least two scans to plot a trend.</p>'

    pad_l, pad_r, pad_t, pad_b = 62, 16, 16, 46
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [v for _, v in points]
    lo, hi = min(values), max(0.0, max(values))
    span = (hi - lo) or 1.0
    lo -= span * 0.12
    hi += span * 0.12
    span = hi - lo

    def yfor(v: float) -> float:
        return pad_t + plot_h * (1 - (v - lo) / span)

    def xfor(i: int) -> float:
        return pad_l + plot_w * (i / (len(points) - 1))

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Closest margin to violation, per scan">'
    ]
    for frac in (0, 0.5, 1.0):
        v = lo + span * frac
        y = yfor(v)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'class="grid"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="tick" '
            f'text-anchor="end">{v:+.3f}</text>'
        )

    if lo <= 0 <= hi:
        zy = yfor(0.0)
        parts.append(
            f'<line x1="{pad_l}" y1="{zy:.1f}" x2="{width - pad_r}" y2="{zy:.1f}" '
            f'class="threshold"/>'
            f'<text x="{width - pad_r - 4}" y="{zy - 6:.1f}" class="threshold-label" '
            f'text-anchor="end">violation threshold</text>'
        )

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{xfor(i):.1f},{yfor(v):.1f}"
        for i, (_, v) in enumerate(points)
    )
    parts.append(f'<path d="{path}" class="line"/>')

    for i, (ts, v) in enumerate(points):
        parts.append(
            f'<circle cx="{xfor(i):.1f}" cy="{yfor(v):.1f}" r="4" class="dot">'
            f"<title>{esc(ts)}\nclosest margin {v:+.4f}</title></circle>"
        )

    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" '
        f'y2="{pad_t + plot_h}" class="axis"/>'
    )
    for i in (0, len(points) - 1):
        label = points[i][0][11:16]
        anchor = "start" if i == 0 else "end"
        parts.append(
            f'<text x="{xfor(i):.1f}" y="{pad_t + plot_h + 18}" class="tick" '
            f'text-anchor="{anchor}">{esc(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# -------------------------------------------------------------------- funnel


def svg_funnel(row: dict, width: int = 860) -> str:
    """Where rectangles are lost, as a horizontal ordinal bar chart."""
    stages = [
        ("considered", int(row.get("rectangles_considered", 0) or 0)),
        ("strike gap too wide", int(row.get("strike_gap_too_wide", 0) or 0)),
        ("coverage ratio too wide", int(row.get("coverage_ratio_too_wide", 0) or 0)),
        ("reached violation test", int(row.get("no_violation", 0) or 0)
         + int(row.get("below_tick_bound", 0) or 0)
         + int(row.get("detected", 0) or 0)),
        ("detected", int(row.get("detected", 0) or 0)),
    ]
    top = max((v for _, v in stages), default=0)
    if not top:
        return '<p class="empty">No rectangles considered yet.</p>'

    row_h, gap = 30, 10
    pad_l, pad_r, pad_t = 190, 70, 8
    height = pad_t + len(stages) * (row_h + gap)
    plot_w = width - pad_l - pad_r

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Rectangle screen funnel">'
    ]
    for i, (label, value) in enumerate(stages):
        y = pad_t + i * (row_h + gap)
        w = plot_w * (value / top) if top else 0
        parts.append(
            f'<text x="{pad_l - 12}" y="{y + row_h / 2 + 4:.0f}" class="tick" '
            f'text-anchor="end">{esc(label)}</text>'
            f'<rect x="{pad_l}" y="{y}" width="{max(w, 2):.1f}" height="{row_h}" '
            f'rx="4" class="funnel funnel-{i}">'
            f"<title>{esc(label)}: {value:,}</title></rect>"
            f'<text x="{pad_l + max(w, 2) + 10:.1f}" y="{y + row_h / 2 + 4:.0f}" '
            f'class="value">{value:,}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------- page


def build(store: ScanStore) -> str:
    scans = store.read_scans()
    violations = store.read_violations()
    hists = store.read_margin_histograms()

    totals = [0] * (len(HIST_EDGES) - 1)
    for row in hists:
        for i in range(len(totals)):
            try:
                totals[i] += int(row.get(f"bin_{i}", 0) or 0)
            except ValueError:
                pass

    closest = float("nan")
    measured = 0
    for row in scans:
        try:
            value = float(row["margin_max"])
            measured += int(row["margin_count"])
            if math.isnan(closest) or value > closest:
                closest = value
        except (KeyError, ValueError):
            continue

    feed = scans[-1]["feed"] if scans else "unknown"
    latest = scans[-1] if scans else {}
    n_viol = len(violations)

    def tokens(mode: dict) -> str:
        ramp = "".join(
            f"  --funnel-{i}: {c};\n" for i, c in enumerate(mode["ramp"])
        )
        return (
            f'  --surface: {mode["surface"]};\n  --plane: {mode["plane"]};\n'
            f'  --ink: {mode["ink"]};\n  --ink2: {mode["ink2"]};\n'
            f'  --muted: {mode["muted"]};\n  --grid: {mode["grid"]};\n'
            f'  --axis: {mode["axis"]};\n  --series: {mode["series"]};\n'
            f'  --good: {mode["good"]};\n  --critical: {mode["critical"]};\n'
            f'  --warning: {mode["warning"]};\n{ramp}'
        )

    status_cls = "critical" if n_viol == 0 else "good"
    status_text = (
        "none detected" if n_viol == 0 else f"{n_viol:,} recorded"
    )

    feed_note = (
        '<div class="callout"><strong>Indicative feed.</strong> These quotes are '
        "Alpaca&rsquo;s derivatives of OPRA, documented as unsuitable for live "
        "trading decisions. A fitted arbitrage-free surface satisfies TP2 "
        "identically, so margins stay bounded away from zero and no violation can "
        "appear. Thresholds must not be tuned on this data.</div>"
        if feed == "indicative"
        else ""
    )

    rows_html = "".join(
        f"<tr><td>{esc(r['ts'])}</td><td>{esc(r['feed'])}</td>"
        f"<td class=\"num\">{esc(r['spot'])}</td>"
        f"<td class=\"num\">{int(r['rectangles_considered']):,}</td>"
        f"<td class=\"num\">{int(r['margin_count']):,}</td>"
        f"<td class=\"num\">{float(r['margin_median']):+.4f}</td>"
        f"<td class=\"num\">{float(r['margin_max']):+.4f}</td>"
        f"<td class=\"num\">{int(r['detected']):,}</td></tr>"
        for r in scans[-40:]
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TP2 Scan Report</title>
<style>
:root {{
  color-scheme: light;
{tokens(LIGHT)}}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
{tokens(DARK)}  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
{tokens(DARK)}}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background: var(--plane); color: var(--ink);
  font: 14px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }}
.wrap {{ max-width: 960px; margin: 0 auto; padding: 32px 20px 64px; }}
h1 {{ font-size: 24px; margin: 0 0 4px; letter-spacing: -0.01em; }}
h2 {{ font-size: 15px; margin: 0 0 2px; }}
.sub {{ color: var(--ink2); margin: 0 0 24px; }}
.card {{ background: var(--surface); border: 1px solid var(--grid);
  border-radius: 10px; padding: 18px 20px; margin: 0 0 18px; }}
.card p.note {{ color: var(--ink2); margin: 2px 0 14px; font-size: 13px; }}
.stats {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr));
  gap: 14px; margin: 0 0 18px; }}
.stat {{ background: var(--surface); border:1px solid var(--grid);
  border-radius:10px; padding:14px 16px; }}
.stat .k {{ color: var(--muted); font-size:12px; text-transform:uppercase;
  letter-spacing:.04em; }}
.stat .v {{ font-size:26px; font-variant-numeric: tabular-nums; margin-top:4px; }}
.stat .v.good {{ color: var(--good); }}
.stat .v.critical {{ color: var(--critical); }}
.callout {{ background: var(--surface); border:1px solid var(--warning);
  border-left-width:4px; border-radius:8px; padding:12px 16px; margin:0 0 18px;
  color: var(--ink2); }}
.callout strong {{ color: var(--ink); }}
svg {{ width:100%; height:auto; display:block; overflow:visible; }}
.grid {{ stroke: var(--grid); stroke-width:1; }}
.axis {{ stroke: var(--axis); stroke-width:1; }}
.tick, .value {{ fill: var(--muted); font-size:11px;
  font-variant-numeric: tabular-nums; }}
.value {{ fill: var(--ink2); }}
.axis-title {{ fill: var(--ink2); font-size:12px; }}
.bar {{ fill: var(--series); }}
.bar-violation {{ fill: var(--critical); }}
.bar:hover, .bar-violation:hover, .funnel:hover {{ opacity:.75; }}
.threshold {{ stroke: var(--critical); stroke-width:2; stroke-dasharray:4 3; }}
.threshold-label {{ fill: var(--critical); font-size:11px; }}
.line {{ fill:none; stroke: var(--series); stroke-width:2;
  stroke-linejoin:round; stroke-linecap:round; }}
.dot {{ fill: var(--series); stroke: var(--surface); stroke-width:2; }}
.funnel-0 {{ fill: var(--funnel-0); }} .funnel-1 {{ fill: var(--funnel-1); }}
.funnel-2 {{ fill: var(--funnel-2); }} .funnel-3 {{ fill: var(--funnel-3); }}
.funnel-4 {{ fill: var(--funnel-4); }}
.empty {{ color: var(--muted); font-style:italic; margin:8px 0; }}
table {{ border-collapse: collapse; width:100%; font-size:12.5px;
  font-variant-numeric: tabular-nums; }}
th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid var(--grid); }}
th {{ color: var(--muted); font-weight:600; }}
td.num {{ text-align:right; }}
details {{ margin-top: 10px; }}
summary {{ cursor:pointer; color: var(--ink2); }}
.tablewrap {{ overflow-x:auto; }}
</style></head><body><div class="wrap">

<h1>TP2 Scan Report</h1>
<p class="sub">SPY option-surface violation scanner &middot; generated
{esc(datetime.now().strftime('%Y-%m-%d %H:%M'))} &middot; feed: <strong>{esc(feed)}</strong></p>

{feed_note}

<div class="stats">
  <div class="stat"><div class="k">Scans</div><div class="v">{len(scans):,}</div></div>
  <div class="stat"><div class="k">Rectangles measured</div>
    <div class="v">{measured:,}</div></div>
  <div class="stat"><div class="k">Closest to violating</div>
    <div class="v">{closest:+.4f}</div></div>
  <div class="stat"><div class="k">Violations</div>
    <div class="v {status_cls}">{esc(status_text)}</div></div>
</div>

<div class="card">
  <h2>Distribution of the TP2 margin</h2>
  <p class="note">Every rectangle that reached the violation test. A bar to the
  right of the dashed rule is a violation. The <em>shape</em> is the finding:
  real quotes crowd toward zero; a fitted surface stays bounded away.</p>
  {svg_histogram(totals)}
</div>

<div class="card">
  <h2>Closest approach to violating, per scan</h2>
  <p class="note">The single nearest rectangle in each scan. Flat and far from
  the rule means the feed is smooth, not that the market is quiet.</p>
  {svg_timeseries(scans)}
</div>

<div class="card">
  <h2>Screen funnel &mdash; latest scan</h2>
  <p class="note">Where rectangles are lost. Distinguishes &ldquo;nothing to
  find&rdquo; from &ldquo;a screen is mis-set&rdquo;.</p>
  {svg_funnel(latest)}
</div>

<div class="card">
  <h2>Scan log</h2>
  <p class="note">Most recent 40 scans. Full history in
  <code>data/scans.csv</code>; violations in <code>data/violations.csv</code>.</p>
  <div class="tablewrap"><table>
  <thead><tr><th>time</th><th>feed</th><th>spot</th><th>considered</th>
  <th>measured</th><th>median</th><th>closest</th><th>detected</th></tr></thead>
  <tbody>{rows_html or '<tr><td colspan="8">no scans yet</td></tr>'}</tbody>
  </table></div>
</div>

</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Render scan CSVs as an HTML report.")
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("reports/scan_report.html"))
    args = ap.parse_args()

    store = ScanStore(args.data_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(store), encoding="utf-8")

    scans = store.read_scans()
    print(f"  scans      {len(scans):,}")
    print(f"  violations {len(store.read_violations()):,}")
    print(f"  report ->  {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
