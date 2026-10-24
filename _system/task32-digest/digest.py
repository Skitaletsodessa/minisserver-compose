#!/usr/bin/env python3
"""Task 32 daily digest (TEMPORARY, removed at the end of the task): sends the text in /var/lib/task32/digest.txt once per calendar day through watchnotify.
Empty or missing file = nothing is sent. The marker /var/lib/task32/last-sent holds the date of the last message."""
import datetime
import os
import sys

sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify  # noqa: E402

DIR = "/var/lib/task32"
TEXT = os.path.join(DIR, "digest.txt")
MARK = os.path.join(DIR, "last-sent")


def main():
    today = datetime.date.today().isoformat()
    try:
        text = open(TEXT, encoding="utf-8").read().strip()
    except OSError:
        text = ""
    last = open(MARK).read().strip() if os.path.exists(MARK) else ""
    if not text or last == today:
        print("digest: nothing to send (text %s, last sent %s)" % ("present" if text else "empty", last or "never"))
        return 0
    watchnotify.send("Задача 32 (робот ETA Tiger), итоги дня:\n" + text[:3500])
    open(MARK, "w").write(today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
