# Demo video — script and production guide

Target: **under 5 minutes**. The script below is ~700 words, which at a normal
narration pace lands at **4:30–4:45**.

---

## The script

Each block is one screen. The `[SHOW]` line tells you what to have on screen
while that paragraph is read.

---

**[SHOW: page `Overview` — 13s]**

> MarginCalled is an autonomous options trading agent. It doesn't predict where
> the market is going. It looks for prices that are internally inconsistent —
> arrangements that cannot all be correct at once — and trades until the
> inconsistency resolves.

**[SHOW: page `How it works` — 36s. Longest block; scroll the funnel slowly]**

> The property it exploits is called total positivity of order two. For any two
> expiries and two strikes, the product of one pair of call prices must be at
> least the product of the other pair, once the strikes are adjusted to a common
> forward. Four contracts form a rectangle. When the inequality fails, the
> surface is inconsistent.
>
> This is not a model. It doesn't assume Black-Scholes or any particular
> dynamics. It follows from the requirement that no arbitrage exists at all. So
> a violation isn't a signal that something is mispriced relative to a model —
> it's proof the prices cannot all be right.

**[SHOW: page `Reversion` — 16s. The time-to-revert histogram]**

> Over the competition the agent scanned a hundred and ninety two million
> rectangles, detected seventeen thousand violations, and tracked forty three
> thousand episodes to resolution.
>
> Ninety eight percent of them reverted. Median time, eleven minutes.
>
> That is the central claim of the strategy, and it held.

**[SHOW: page `Live fills` — 23s. The round-trip table]**

> Live, on a hundred thousand dollar Alpaca paper account, it placed a hundred
> and thirty one orders through the MCP server. Forty filled. Twenty one
> completed round trips — and every single one closed because its violation
> reverted. Not one closed on a time stop or a deadline.
>
> The final equity was ninety nine thousand nine hundred and sixty three
> dollars. A loss of thirty seven.

**[SHOW: page `Live fills`, scroll to the spread-vs-edge panel — 35s]**

> So the mechanism worked perfectly and the account still lost money. That gap
> is the most useful thing this project found, and the reason is exact.
>
> The mispricing is worth about two cents. The bid-ask spread on these contracts
> is one cent per leg — two legs, in and out, is two cents. The edge and the cost
> of collecting it are the same number.
>
> This isn't the broker. Alpaca charged zero fees, and several orders filled
> better than we asked. It's market structure: you buy at the ask, sell at the
> bid, and the difference is what a market maker earns.

**[SHOW: page `Backtest` — 28s. The six-row sizing table]**

> Backtesting the same signals through the same filters shows the strategy is
> profitable on paper. The T-one denomination returns nine thousand eight hundred
> dollars across six hundred and seventy two trades, winning sixty seven percent
> of the time.
>
> But every backtest row assumes you fill at the quoted price on every signal.
> Live, three percent filled — because a resting limit order only fills when the
> market moves toward it, which means the fills you get are the ones already
> going against you.

**[SHOW: page `Why only SPY` — 39s. Longest block; let the margin table sit]**

> One more finding worth the competition's attention. The research paper sizes
> each leg by the opposing contract's price. That position is always short-heavy,
> which makes it a naked call, which our account cannot legally hold.
>
> We assumed that constraint cost us. It didn't. Priced against margin, the
> covered version we were forced into earns three hundred and sixty five percent
> on capital committed. The paper's version earns nought point one six percent,
> because Reg T wants thirteen thousand dollars of margin per naked contract to
> earn twenty two.
>
> The broker restriction pushed us into the structure that is two thousand times
> more capital efficient, and bounded the risk instead of leaving it unlimited.

**[SHOW: page `Decisions`, then `Narration` — 24s. Split ~12s each]**

> The agent logs every decision, not just the ones that became trades. Thirty two
> thousand of them, with the quotes, the determinant, and every risk gate that
> ran. An LLM layer reads that log and writes a plain English account of what the
> agent did and why it refused what it refused — and it's a reader, not a
> participant. It has no tools and cannot place an order.

**[SHOW: page `Signals`, then the repo or PROJECT_REPORT.pdf — 21s]**

> The signal is real and reverts on schedule. The theoretical position sizing is
> unusable in practice. And the binding constraint on a live account is the
> spread, not the mathematics.
>
> An end-of-day research paper cannot discover any of that, because mid prices
> aren't tradeable and margin doesn't exist in a backtest. It only appears when
> the strategy meets a real broker.

---

## Producing it

### 1. Record the screen

macOS built-in, no install:

```
Cmd + Shift + 5   ->  "Record Entire Screen" or "Record Selected Portion"
```

Start the dashboard first so you can click through it while recording:

```
streamlit run app.py
```

Record **silently** — narration is added afterwards.

**Do not give every page equal time.** The blocks are not equal length. Use this
cue sheet; the seconds are measured from the actual narration audio at 175 wpm:

| # | page | hold | cum. | what is on screen | script says |
|---|---|---|---|---|---|
| 1 | `Overview` | 13s | 0:13 | Rectangles scanned, Violations found, Episodes reverted, "The one-line story" | what the agent is |
| 2 | `How it works` | 36s | 0:49 | "From a quote to a trade — or, far more often, a refusal" + the numbered pipeline | the TP2 inequality and why it is not a model |
| 3 | `Reversion` | 16s | 1:05 | Episodes tracked, Median time to revert, "How long a violation lasts" histogram | **98% reverted, median 11 min** — matches exactly |
| 4 | `Live fills` (top) | 23s | 1:28 | Orders placed, "What actually filled" table | 131 placed, 40 filled, 21 round trips |
| 5 | `Live fills` (scroll) | 35s | 2:03 | **"The edge and the cost are the same size"** | the 2c vs 2c explanation — matches exactly |
| 6 | `Backtest` | 28s | 2:31 | T1 unit / scaled / K2 rows, "Median T1 trade" | the backtest numbers and the 3% fill caveat |
| 7 | `Why only SPY` | 39s | 3:10 | "Why only SPY ever traded", "three things kept the book tiny" | naked calls, Reg T margin, 2,220x capital efficiency |
| 8 | `Decisions` → `Narration` | 24s | 3:34 | "Two real refusals, straight from the log"; then "The agent's own account of the run" | the audit log and the LLM reader |
| 9 | `Signals` → repo / PDF | 21s | 3:55 | "What gets dropped before a violation is even scored" funnel | the three closing findings |

**Total 3:55.** Nine blocks, nine pages — but not one-to-one: `Live fills` carries
blocks 4 and 5, and blocks 8 and 9 each cover two screens.

**Where the fit is tightest** — blocks 3, 5, 6 and 7 land on pages built to say
the same thing, so the narration and the screen reinforce each other. Let those
sit.

**Where it is loosest** — block 2 explains the *mathematics* while `How it works`
shows the *pipeline*. They complement rather than mirror. If you want a tighter
fit, put a single slide with the four contracts A, B, C, D and the inequality on
screen for the first 20 seconds of that block, then cut to `How it works` for the
rest.

The simplest way to hit these marks: generate the audio first, play it back while
you record, and change page when you hear the next paragraph start.

### 2. Generate the voice

macOS has 177 built-in voices and needs no API key. `Samantha` is the best
default US English voice:

```bash
# put the spoken text (no [SHOW] lines, no markdown) in narration.txt
say -v Samantha -r 175 -f narration.txt -o narration.aiff
afconvert narration.aiff narration.m4a -f m4af -d aac    # smaller, ffmpeg-friendly
```

`-r 175` is words per minute; 165–185 sounds natural. Test a sentence first:

```bash
say -v Samantha -r 175 "The agent scanned a hundred and ninety two million rectangles."
```

**For a better voice**, ElevenLabs' free tier gives ~10 minutes per month, which
is plenty for one video, and sounds markedly more natural than `say`. Export MP3
and use it in place of `narration.m4a` below.

### 3. Combine

```bash
# check lengths match before combining
ffprobe -v error -show_entries format=duration -of csv=p=0 screen.mov
ffprobe -v error -show_entries format=duration -of csv=p=0 narration.m4a

ffmpeg -i screen.mov -i narration.m4a \
       -c:v libx264 -preset medium -crf 20 \
       -c:a aac -b:a 160k -shortest \
       demo.mp4
```

`-shortest` trims to whichever track ends first. If the video is shorter than the
audio, record a little extra and trim; if longer, hold on the final screen.

### 4. Check before submitting

```bash
ffprobe -v error -show_entries format=duration -of csv=p=0 demo.mp4   # under 300s
```

- Under **5:00**
- **1920x1080**, 16:9
- Audio audible, no clipping
- Text on screen legible at the size a judge will watch it

---

## Numbers to get right if you narrate live

| | |
|---|---|
| rectangles scanned | 192 million |
| violations detected | 17,097 |
| episodes tracked | 43,566 |
| **reverted** | **98%, median 11 minutes** |
| orders placed / filled | 131 / 40 |
| round trips | **21, all on reversion** |
| final equity | $99,963 |
| backtest T1 unit | +$9,829 over 672 trades, 67% win |
| the loss, in one line | **the edge is 2c and the spread is 2c** |
| capital efficiency | **365.8% covered vs 0.165% naked** |
