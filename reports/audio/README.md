# Narration audio

One continuous track, already timed to `demo_raw.mov`. **Drop it on the timeline
at 00:00 and it lines up** — no cutting, no nudging.

| file | use |
|---|---|
| `narration.wav` | 48 kHz PCM — best for Premiere, Final Cut, DaVinci, Audacity |
| `narration.mp3` | 192 kbps — smallest, works anywhere |
| `narration.m4a` | AAC — what `ffmpeg` used to build `reports/demo.mp4` |

**Length 270.7s against a 272.0s video** — 1.3s of silence at the end, which is
intended. Start it at zero and do not stretch it.

## Where each section lands

Silence sits between sections, so each block begins exactly when its page
appears. This is why the track must start at 00:00 and not be trimmed.

| start | section | page on screen |
|---|---|---|
| 0:00 | what the agent is | Overview |
| 0:18 | the TP2 inequality | How it works |
| 0:48 | 98% reverted, median 11 min | Reversion |
| 1:08 | the fills, and the 2c-vs-2c reason for the loss | Live fills |
| 2:08 | the backtest matrix and the 3% fill caveat | Backtest |
| 2:58 | why only SPY, and the naked-call constraint | Why only SPY |
| 3:28 | the audit log and the language layer | Narration |
| 3:58 | the funnel and the three closing findings | Signals |

## If you want a better voice

These were generated with the macOS `say` voice, which is serviceable but
obviously synthetic. The per-section scripts are in `narration_v2/*.txt`.

Paste them into ElevenLabs (free tier covers ~10 minutes/month), export each
section, and place them at the start times above. The timing work is already
done — only the voice changes.

## Rebuilding it

```bash
for f in narration_v2/*.txt; do
  say -v Samantha -r 175 -f "$f" -o "${f%.txt}.aiff"
done
# then re-anchor: see the adelay/amix command in the project history
```
