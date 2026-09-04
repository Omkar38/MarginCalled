#!/usr/bin/env python3
"""Make the synthetic narration sound less synthetic.

`say` reads at a constant pace and barely pauses at punctuation, which is most of
what makes it sound robotic. Three changes help, in order of how much they matter:

  1. Silences at clause and sentence boundaries. A human breathes; a synthesiser
     does not unless told to. `[[slnc N]]` inserts N milliseconds.
  2. A slower base rate. 175 wpm is brisk for narration; 158-165 sounds
     considered rather than hurried.
  3. Longer pauses before a number or a claim the listener should register.

The pauses cost time, so each section is measured afterwards and the rate nudged
per section to keep it inside its window - the alignment with the video matters
more than a uniform pace.

    python3 scripts/humanise_narration.py --rate 160
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

# start time and window length of each section, from the recording
WINDOWS = {"01": (0, 18), "02": (18, 30), "03": (48, 20), "04": (68, 60),
           "05": (128, 50), "06": (178, 30), "07": (208, 30), "08": (238, 34)}


def prosody(text: str) -> str:
    """Insert breath pauses. Longer at sentences, shorter at clauses."""
    t = " ".join(text.split())
    # A dash usually introduces the point of the sentence - let it land.
    t = re.sub(r'\s+[—-]\s+', ' [[slnc 260]] ', t)
    # Sentence end: a real breath.
    t = re.sub(r'([.!?])\s+', r'\1 [[slnc 380]] ', t)
    # Clause: a shorter one.
    t = re.sub(r',\s+', ', [[slnc 140]] ', t)
    # Before a figure the listener should take in.
    t = re.sub(r'\[\[slnc 140\]\] (about |roughly )?(\d)', r'[[slnc 220]] \1\2', t)
    return t


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip() or 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--voice", default="Samantha")
    ap.add_argument("--rate", type=int, default=160)
    ap.add_argument("--src", default="narration_v2")
    a = ap.parse_args()

    src = Path(a.src)
    print(f"voice {a.voice}, base rate {a.rate} wpm, breath pauses on\n")
    print(f"{'section':<26}{'window':>9}{'rate':>7}{'length':>9}  fit")
    ok = True
    for f in sorted(src.glob("*.txt")):
        key = f.stem.split("_")[0]
        start, span = WINDOWS[key]
        marked = prosody(f.read_text())
        (src / f"{f.stem}.prosody.txt").write_text(marked + "\n")

        rate = a.rate
        for _ in range(6):
            out = src / f"{f.stem}.aiff"
            subprocess.run(["say", "-v", a.voice, "-r", str(rate),
                            "-f", str(src / f"{f.stem}.prosody.txt"), "-o", str(out)],
                           capture_output=True)
            d = duration(out)
            if d <= span - 0.3:
                break
            # too long for its window: speak a little faster rather than cut words
            rate = int(rate * (d / (span - 0.6)) + 0.5)
        fit = "OK" if d <= span - 0.3 else f"OVER by {d-span:.1f}s"
        if "OVER" in fit:
            ok = False
        print(f"{f.stem:<26}{span:>8}s{rate:>7}{d:>8.1f}s  {fit}")
    print()
    print("all sections fit their windows" if ok else "some sections still overrun")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
