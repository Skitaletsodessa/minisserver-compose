#!/bin/bash
# Task 24 E3: Funnel throughput in the evening window. A MEASUREMENT, not an optimisation.
# Method (and its limit, stated): the server requests its own PUBLIC Funnel address (resolved through 1.1.1.1 and pinned with
# --resolve, because MagicDNS would hand out its own tailnet IP and bypass Funnel), so the data goes house -> Tailscale's ingress
# -> back over the tailnet to the house and out through the server's answer: it exercises BOTH directions of the house line and
# Tailscale's relay, but it is not a third-party phone on mobile data (the 2026-09-22 figures, 11 up / 18.7 down Mbit/s, were
# measured that way). Treat the number as "what the path can carry", compare it with the old figures, do not read it as a
# phone's experience. It downloads the biggest static files Immich (:10000) and Navidrome (:8443) serve without a login.
set -uo pipefail
OUT=/var/lib/funnel-throughput; mkdir -p "$OUT"
H=minisserver.tail6bf4d5.ts.net
PUB=$(dig @1.1.1.1 "$H" +short | grep -E '^[0-9.]+$' | head -1)
[ -n "$PUB" ] || { echo "cannot resolve the public Funnel address"; exit 1; }
res=()
: > "$OUT/last.txt"
echo "$(date '+%F %H:%M')  public address $PUB (window 19:00-22:00 expected; actual hour $(date +%H))" >> "$OUT/last.txt"

get() { curl -s -m 120 --resolve "$H:$1:$PUB" "${@:2}"; }

measure() { # label port page-path
  local label=$1 port=$2 page=$3 base="https://$H:$2" assets a sz tmp bytes secs total=0 time=0 best=0 n=0 mbit
  assets=$(get "$port" "$base$page" | grep -oE '(href|src)="[^"]*\.(js|css)"' | sed -E 's/^(href|src)="//; s/"$//' | sort -u | head -30)
  tmp=$(mktemp)
  for a in $assets; do
    case "$a" in ./*) a="${page%/}/${a#./}";; /*) ;; *) a="/$a";; esac
    sz=$(get "$port" -o /dev/null -w '%{size_download}' "$base$a")
    echo "$sz $a" >> "$tmp"
  done
  for round in 1 2 3; do
    while read -r sz a; do
      read -r bytes secs < <(get "$port" -o /dev/null -w '%{size_download} %{time_total}\n' "$base$a")
      [ "${bytes:-0}" -gt 100000 ] || continue
      mbit=$(awk "BEGIN{printf \"%.1f\", $bytes*8/$secs/1000000}")
      echo "  $label r$round $a ${bytes}B ${secs}s ${mbit} Mbit/s" >> "$OUT/last.txt"
      total=$((total + bytes)); time=$(awk "BEGIN{print $time+$secs}"); n=$((n+1))
      awk "BEGIN{exit !($mbit > $best)}" && best=$mbit
    done < <(sort -rn "$tmp" | head -4)
  done
  rm -f "$tmp"
  if [ "$n" -gt 0 ]; then
    res+=("$label avg $(awk "BEGIN{printf \"%.1f\", $total*8/$time/1000000}") / best $best Mbit/s ($n downloads, $((total/1024)) KiB)")
  else
    res+=("$label no usable files")
  fi
}
measure immich 10000 /
measure navidrome 8443 /app/
summary="Funnel throughput $(date '+%d.%m %H:%M') (server -> its own public Funnel address, so both house-line directions are used): ${res[*]}. For comparison, a phone on mobile data on 2026-09-22 got 11 up / 18.7 down Mbit/s."
echo "$summary" | tee -a "$OUT/results.log"
cat "$OUT/last.txt"
[ -n "${FUNNEL_NO_SEND:-}" ] || python3 /srv/compose/_system/lib/watchnotify.py send "minisserver $summary"
