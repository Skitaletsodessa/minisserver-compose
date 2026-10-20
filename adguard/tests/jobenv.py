"""Binds the job modules' file locations to ONE test module's scratch directory. Several test modules run in one process and childsnapshot reads its paths when it is
first imported, so every test's setUp calls bind(TMP) again."""
import os
from pathlib import Path


def bind(tmp):
    os.environ["CHILD_SNAPSHOT"] = os.path.join(tmp, "out", "child-policy.json")
    os.environ["CHILD_STATE_DIR"] = os.path.join(tmp, "state")
    os.environ["CHILD_APPLY_STATUS"] = os.path.join(tmp, "child-apply-status.json")
    os.environ["CHILD_SOURCE_FILE"] = os.path.join(tmp, "source.conf")
    import childsnapshot
    childsnapshot.SNAPSHOT = Path(os.environ["CHILD_SNAPSHOT"])
    childsnapshot.STATE_DIR = Path(os.environ["CHILD_STATE_DIR"])
    childsnapshot.LAST_GOOD = childsnapshot.STATE_DIR / "last-good-snapshot.json"
    childsnapshot.STATUS = Path(os.environ["CHILD_APPLY_STATUS"])
    childsnapshot.SHADOW = childsnapshot.STATE_DIR / "shadow.json"
    childsnapshot.MODE_FILE = Path(os.environ["CHILD_SOURCE_FILE"])
