#!/bin/bash
# Manual diagnostic, not a timer. Read-only NCQ load on ONE disk (addressed by serial,
# never by /dev/sdX letter) and a count of libata errors it provokes.
# Usage: sudo ata-probe.sh <serial> [seconds]
# Written for the ASM1166 / hynix ICRC investigation (docs/incident-2026-09-18.md);
# reuse it after any cable/adapter/port change and before trusting a disk in a mirror.
set -euo pipefail

SERIAL="${1:?usage: ata-probe.sh <serial> [seconds]}"
SECS="${2:-30}"

DEV=$(ls /dev/disk/by-id/ata-*_"$SERIAL" 2>/dev/null | head -1 || true)
[ -n "$DEV" ] || { echo "no ata disk with serial $SERIAL" >&2; exit 1; }
REAL=$(readlink -f "$DEV")
SYS=$(readlink -f "/sys/block/$(basename "$REAL")/device")
ATA=$(echo "$SYS" | grep -oE 'ata[0-9]+' | head -1)
HOST=$(echo "$SYS" | grep -oE 'host[0-9]+' | head -1)

echo "disk:        $DEV -> $REAL ($ATA, $HOST)"
echo "queue_depth: $(cat "/sys/block/$(basename "$REAL")/device/queue_depth")"
echo "host LPM:    $(cat "/sys/class/scsi_host/$HOST/link_power_management_policy")"
echo "boot:        $(cat /proc/cmdline | grep -oE 'libata[^ ]*' || echo 'no libata.* params')"
echo "load:        fio --readonly read 256k iodepth=32 direct=1 for ${SECS}s"

START=$(date +"%Y-%m-%d %H:%M:%S")
FIO=$(fio --name=probe --filename="$DEV" --readonly --rw=read --bs=256k \
          --iodepth=32 --ioengine=libaio --direct=1 --runtime="$SECS" --time_based 2>&1 || true)
echo "$FIO" | grep -E "READ:" || echo "$FIO" | tail -3

J=$(journalctl -k --no-pager --since "$START")
count() { echo "$J" | grep -cE "$1" || true; }
echo "--- libata during the run ($ATA) ---"
echo "exceptions:      $(count "$ATA.00: exception")"
echo "ICRC:            $(count "$ATA.00: error:.*ICRC")"
echo "UNC:             $(count "$ATA.00: error:.*UNC")"
echo "IDNF/AMNF/other: $(count "$ATA.00: error:.*(IDNF|AMNF|MC|MCR|TK0NF)")"
echo "hard resets:     $(count "$ATA: hard resetting link")"
echo "link limited:    $(count "$ATA: limiting SATA link speed")"
echo "block I/O error: $(count "I/O error, dev $(basename "$REAL")")"
echo "xfer modes:      $(echo "$J" | grep -oE "configured for UDMA/[0-9]+" | sort | uniq -c | tr '\n' ';')"
