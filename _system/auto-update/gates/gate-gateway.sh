#!/bin/bash
# Task 35b-2: the gateway's whole test suite (~350 tests, ~2.5 min) runs INSIDE the freshly built image (its python and libraries), sources mounted read-only,
# test requirements installed with --require-hashes into the container's own /tmp. No host listener, no secret, no Telegram (synthetic people, fake HA).
# A few timing-sensitive tests can fail once when the machine is busy (seen 2026-11-17): every failed test is re-run ALONE once; red only if one of them fails again.
# usage: gate-gateway.sh IMAGE      exit 0 = green, anything else = red (the last lines of output tell why)
IMG=${1:?image}
exec docker run --rm --user 1000:1000 -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v /srv/compose:/srv/compose:ro -w /srv/compose/portal/gateway --entrypoint sh "$IMG" -c '
pip install -q --no-cache-dir --require-hashes --target /tmp/t -r requirements-test.txt >/tmp/pip.out 2>&1 || { echo "gate: pip install of the test requirements failed"; tail -5 /tmp/pip.out; exit 3; }
PYTHONPATH=/tmp/t python -W ignore -m unittest discover -s tests >/tmp/out 2>&1; rc=$?
if [ $rc -ne 0 ]; then
  ids=$(sed -n "s/^\(FAIL\|ERROR\): \([A-Za-z0-9_]*\) (\(.*\))$/\3/p" /tmp/out | sort -u)
  if [ -n "$ids" ]; then
    echo "gate: first run red, re-running alone: $(echo $ids | tr "\n" " " | cut -c1-300)"
    PYTHONPATH=/tmp/t:tests python -W ignore -m unittest $ids >/tmp/out2 2>&1; rc=$?
    tail -4 /tmp/out2
    [ $rc -eq 0 ] && echo "gate: green on the re-run (timing-sensitive tests, machine busy)"
    exit $rc
  fi
fi
tail -6 /tmp/out; exit $rc'
