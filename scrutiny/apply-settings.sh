#!/bin/bash
# Scrutiny keeps its UI settings in its own database (the scrutiny-config volume), i.e. outside
# git. This re-applies the ones we deliberately changed; idempotent, run it after rebuilding the stack.
#   repeat_notifications=false : stop re-alerting every 6 h on a device that is already failed.
#     Ivan's decision 2026-09-19 (docs/incident-2026-09-18.md): the Seagate's historical
#     Command_Timeout trips Scrutiny's own threshold forever; _system/smart-change-watch covers
#     changes instead. Global setting - it applies to every disk.
set -euo pipefail
API=http://127.0.0.1:8080/api/settings
curl -sf "$API" | python3 -c '
import json, sys
d = json.load(sys.stdin)["settings"]
d["metrics"]["repeat_notifications"] = False
json.dump(d, sys.stdout)
' | curl -sf -X POST -H "Content-Type: application/json" --data @- -o /dev/null "$API"
curl -sf "$API" | python3 -c 'import json,sys; print(json.load(sys.stdin)["settings"]["metrics"])'
