"""Small SQLite cache for MusicBrainz responses - a re-run costs nothing. Misses are
cached too (as an explicit null marker with a date), so a track that genuinely has no
match on MusicBrainz isn't re-queried every single run."""
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

CACHE_PATH = Path("/srv/apps/music-tagging/mb_cache.sqlite")


def _key(call: str, *parts: str) -> str:
    raw = call + "|" + "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class MBCache:
    def __init__(self, path: Path = CACHE_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                call TEXT NOT NULL,
                response TEXT,
                is_miss INTEGER NOT NULL,
                fetched_date TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def get(self, call: str, *parts: str):
        """Returns (found, is_miss, value, fetched_date) - found=False means not cached at all."""
        k = _key(call, *parts)
        row = self.conn.execute(
            "SELECT response, is_miss, fetched_date FROM cache WHERE key=?", (k,)
        ).fetchone()
        if row is None:
            return False, None, None, None
        response, is_miss, fetched_date = row
        value = json.loads(response) if response else None
        return True, bool(is_miss), value, fetched_date

    def put(self, call: str, *parts: str, value=None, is_miss: bool = False):
        k = _key(call, *parts)
        self.conn.execute(
            "INSERT OR REPLACE INTO cache (key, call, response, is_miss, fetched_date) VALUES (?, ?, ?, ?, ?)",
            (k, call, json.dumps(value, ensure_ascii=False) if value is not None else None,
             int(is_miss), date.today().isoformat()),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
