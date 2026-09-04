#!/usr/bin/env python3
"""Sample a screen recording into frames so the narration can be timed to it.

I cannot watch video. I can read images, so this turns a recording into a
contact sheet of timestamped frames: extract one every N seconds, and each can
then be identified by eye and mapped to the dashboard page it shows. From that
mapping the narration is re-cut to the pacing that was actually recorded, rather
than the recording being forced to match a script written in advance.

    python3 scripts/analyse_recording.py demo.mov
    python3 scripts/analyse_recording.py demo.mov --every 3 --out frames/
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"ffprobe failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video")
    ap.add_argument("--every", type=float, default=5.0,
                    help="seconds between sampled frames (default 5)")
    ap.add_argument("--out", default="frames",
                    help="directory for the extracted frames")
    a = ap.parse_args()

    src = Path(a.video)
    if not src.exists():
        sys.exit(f"not found: {src}")

    meta = probe(src)
    dur = float(meta["format"]["duration"])
    v = next((s for s in meta["streams"] if s.get("codec_type") == "video"), {})
    w, h = v.get("width"), v.get("height")

    print(f"{src.name}")
    print(f"  duration   {int(dur//60)}:{int(dur%60):02d}  ({dur:.1f}s)")
    print(f"  resolution {w}x{h}" + ("  (16:9)" if w and h and abs(w/h - 16/9) < 0.02 else ""))
    print(f"  video      {v.get('codec_name')}  {v.get('r_frame_rate')} fps")
    audio = [s for s in meta["streams"] if s.get("codec_type") == "audio"]
    print(f"  audio      {'yes - ' + audio[0].get('codec_name', '?') if audio else 'none (silent, as intended)'}")
    print()
    if dur > 300:
        print(f"  WARNING: {dur:.0f}s exceeds the 5-minute limit by {dur-300:.0f}s")
    else:
        print(f"  OK: {300-dur:.0f}s of headroom under the 5-minute limit")
    print()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("t*.jpg"):
        f.unlink()

    # One frame every --every seconds, each named by its timestamp so the file
    # name alone says when it was taken.
    n = 0
    t = 0.0
    while t < dur:
        dest = out / f"t{int(t):04d}s.jpg"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src),
             "-frames:v", "1", "-q:v", "3", str(dest)],
            capture_output=True,
        )
        if dest.exists():
            n += 1
        t += a.every
    print(f"  extracted {n} frames into {out}/ (one every {a.every:g}s)")
    print()
    print("  Next: have them read in order, note which dashboard page each shows,")
    print("  and the narration can be re-cut to those timings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
