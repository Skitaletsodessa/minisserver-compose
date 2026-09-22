#!/bin/bash
# Kills Immich ffmpeg processes that have hung, and logs every kill (Task 08, docs/measurements.md 2026-09-21/22).
#
# Why: Immich has no timeout on ffmpeg. Some inputs (multi-stream HEVC phone clips with odd frame rates, and videos Immich
# extracts from motion photos) send the VAAPI path into a 100 % CPU loop that ignores SIGTERM. The videoConversion queue
# has concurrency 1, so ONE hung process blocks every video behind it (958 were stuck for ~2 h during the import).
# A healthy transcode runs faster than real time, so: kill any ffmpeg older than max(45 s, 15 x the input's duration).
# If the duration cannot be read, do NOT guess low: fall back to 30 minutes. The killed job fails and Immich falls back
# to the next mode (VAAPI enc + software decode, then software), which completes.
LOG=/srv/apps/ffmpeg-watchdog.log
while true; do
  for pid in $(pgrep -x ffmpeg); do
    et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
    [ "${et:-0}" -gt 45 ] || continue
    inp=$(tr '\0' '\n' < /proc/$pid/cmdline 2>/dev/null | grep -A1 -x -- '-i' | tail -1)
    dur=$(docker exec immich_server timeout -s KILL 10 ffprobe -v error -show_entries format=duration -of csv=p=0 "$inp" 2>/dev/null | head -1)
    if [ -n "$dur" ]; then thr=$(awk -v d="$dur" 'BEGIN{t=15*d; if (t<45) t=45; printf "%d", t}'); else thr=1800; fi
    if [ "${et:-0}" -gt "$thr" ]; then
      mode=cpu; tr '\0' ' ' < /proc/$pid/cmdline | grep -q -e '-hwaccel' -e 'vaapi' && mode=vaapi
      echo "$(date +%FT%T) kill pid=$pid elapsed=${et}s threshold=${thr}s duration=${dur:-unknown}s mode=$mode input=$inp" >> "$LOG"
      kill -9 "$pid"
    fi
  done
  sleep 5
done
