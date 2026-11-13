#!/bin/bash
# Task 35b-2: the schedule service's test suite runs INSIDE the freshly built image (see gate-gateway.sh). usage: gate-schedules.sh IMAGE
IMG=${1:?image}
exec docker run --rm --user 1000:1000 -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v /srv/compose:/srv/compose:ro -w /srv/compose/portal/schedules --entrypoint sh "$IMG" -c '
pip install -q --no-cache-dir --require-hashes --target /tmp/t -r requirements-test.txt >/tmp/pip.out 2>&1 || { echo "gate: pip install of the test requirements failed"; tail -5 /tmp/pip.out; exit 3; }
PYTHONPATH=/tmp/t python -W ignore -m unittest discover -s tests -t . >/tmp/out 2>&1; rc=$?
tail -6 /tmp/out; exit $rc'
