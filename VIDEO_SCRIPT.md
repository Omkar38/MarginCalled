# Demo video — script and production guide

Target: **under 5 minutes**. The script below is ~700 words, which at a normal
narration pace lands at **4:30–4:45**.

---

## The script

Each block is one screen. The `[SHOW]` line tells you what to have on screen
while that paragraph is read.

---

**[SHOW: the Streamlit dashboard, Overview page]**

> MarginCalled is an autonomous options trading agent. It doesn't predict where
> the market is going. It looks for prices that are internally inconsistent —
> arrangements that cannot all be correct at once — and trades until the
> inconsistency resolves.

**[SHOW: the TP2 inequality, or a slide with the four contracts A B C D]**

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

**[SHOW: dashboard, Episodes page — the reversion histogram]**

> Over the competition the agent scanned a hundred and ninety two million
> rectangles, detected seventeen thousand violations, and tracked forty three
> thousand episodes to resolution.
>
> Ninety eight percent of them reverted. Median time, eleven minutes.
>
> That is the central claim of the strategy, and it held.

**[SHOW: dashboard, Trades page — the 21 round trips]**

> Live, on a hundred thousand dollar Alpaca paper account, it placed a hundred
> and thirty one orders through the MCP server. Forty filled. Twenty one
> completed round trips — and every single one closed because its violation
> reverted. Not one closed on a time stop or a deadline.
>
> The final equity was ninety nine thousand nine hundred and sixty three
> dollars. A loss of thirty seven.

**[SHOW: dashboard, the spread-versus-edge comparison]**

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

**[SHOW: dashboard, Backtest page — the sizing table]**

> Backtesting the same signals through the same filters shows the strategy is
> profitable on paper. The T-one denomination returns nine thousand eight hundred
> dollars across six hundred and seventy two trades, winning sixty seven percent
> of the time.
>
> But every backtest row assumes you fill at the quoted price on every signal.
> Live, three percent filled — because a resting limit order only fills when the
> market moves toward it, which means the fills you get are the ones already
> going against you.

**[SHOW: dashboard, the margin comparison]**

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

**[SHOW: dashboard, Decisions page — the refusals]**

> The agent logs every decision, not just the ones that became trades. Thirty two
> thousand of them, with the quotes, the determinant, and every risk gate that
> ran. An LLM layer reads that log and writes a plain English account of what the
> agent did and why it refused what it refused — and it's a reader, not a
> participant. It has no tools and cannot place an order.

**[SHOW: the repository, or the report PDF]**

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

Record **silently** — narration is added afterwards. Move deliberately; give each
page 20–30 seconds.

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
