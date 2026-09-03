#!/bin/bash
# Restart any scanner that has stopped scanning during market hours.
#
# The scanners survived the night as processes but stopped doing work: caffeinate
# held its assertions and the machine slept anyway, so the last scan was 00:32
# and nothing ran until it was noticed by hand at 08:33. On the entry-deadline
# day that failure mode costs the whole session, and a live process is not
# evidence of a live scan - only a fresh row in scans.csv is.
#
# Checks every 2 minutes. Restarts a scanner whose newest scan is older than
# STALE_MIN while the market is open.

cd "$(dirname "$0")/.." || exit 1
STALE_MIN=${STALE_MIN:-12}
ARGS="--interval 300 --market-hours --min-dte 0 --max-dte 400 --max-expiries 40 --trade --live-orders --shade 0.0 --max-orders 3"

in_market_hours() {
  local h m dow
  h=$(date +%H); m=$(date +%M); dow=$(date +%u)
  [ "$dow" -gt 5 ] && return 1
  [ "$h" -lt 9 ] && return 1
  [ "$h" -eq 9 ] && [ "$m" -lt 30 ] && return 1
  [ "$h" -ge 16 ] && return 1
  return 0
}

while true; do
  if in_market_hours; then
    for u in SPX SPY XSP; do
      f="data/$u/scans.csv"
      [ -f "$f" ] || continue
      last=$(tail -1 "$f" | cut -d, -f1)
      last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${last%.*}" "+%s" 2>/dev/null) || continue
      age=$(( ( $(date +%s) - last_epoch ) / 60 ))
      if [ "$age" -ge "$STALE_MIN" ]; then
        echo "$(date '+%F %T')  $u stale ${age}m -> restarting" >> logs/watchdog.log
        pkill -f "run_scanner.py --underlying $u"
        sleep 2
        nohup caffeinate -dimsu python3 -u scripts/run_scanner.py --underlying "$u" $ARGS \
          > "logs/${u}_scanner.log" 2>&1 &
      fi
    done
  fi
  sleep 120
done
