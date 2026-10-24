#!/usr/bin/env python3
"""Make a shareable capture from the raw dp-*.jsonl. The raw files never contain the key or the device id (the logger writes only DP changes), so the
redaction is about the CONTENT of values: any long string keeps its length and the first 16 characters only.
Run: python3 redact.py RAW.jsonl > docs/robots/captures/NAME.jsonl"""
import json
import sys


def _cut(v):
    return v[:16] + "...(%d chars)" % len(v) if isinstance(v, str) and len(v) > 16 else v


def redact(rec):
    out = dict(rec)
    for key in ("value", "head"):
        if key in out:
            out[key] = _cut(out[key])
    if isinstance(out.get("was"), dict):
        out["was"] = {k: _cut(x) for k, x in out["was"].items()}
    return out


def main(path):
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            print(json.dumps(redact(json.loads(line)), ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
