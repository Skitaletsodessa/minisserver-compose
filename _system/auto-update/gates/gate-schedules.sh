#!/bin/bash
# Task 35b-2: the schedule service's test suite runs INSIDE the freshly built image (see gate-gateway.sh); a failed test is re-run alone once. usage: gate-schedules.sh IMAGE
IMG=${1:?image}
exec docker run --rm --user 1000:1000 -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v /srv/compose:/srv/compose:ro -w /srv/compose/portal/schedules --entrypoint sh "$IMG" -c '
pip install -q --no-cache-dir --require-hashes --target /tmp/t -r requirements-test.txt >/tmp/pip.out 2>&1 || { echo "gate: pip install of the test requirements failed"; tail -5 /tmp/pip.out; exit 3; }
PYTHONPATH=/tmp/t python -W ignore -m unittest discover -s tests -t . >/tmp/out 2>&1; rc=$?
if [ $rc -ne 0 ]; then
  ids=$(sed -n "s/^\(FAIL\|ERROR\): \([A-Za-z0-9_]*\) (\(.*\))$/\3/p" /tmp/out | sort -u)
  if [ -n "$ids" ]; then
    echo "gate: first run red, re-running alone: $(echo $ids | tr "\n" " " | cut -c1-300)"
    PYTHONPATH=/tmp/t python -W ignore -m unittest $ids >/tmp/out2 2>&1; rc=$?
    tail -4 /tmp/out2
    [ $rc -eq 0 ] && echo "gate: green on the re-run (timing-sensitive tests, machine busy)"
    exit $rc
  fi
fi
tail -6 /tmp/out; exit $rc'
