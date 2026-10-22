#!/usr/bin/env python3
"""Generates the string tables of the copy deck (docs/portal/ui-copy-ru.md) from gw/strings.py, so the deck and the product cannot drift apart.

  python tools/gen_copy_deck.py                 # prints the generated part
  python tools/gen_copy_deck.py --write DOC     # replaces what is between the markers in DOC
  python tools/gen_copy_deck.py --check DOC     # exit 1 when DOC differs (used by tests/test_copydeck.py)

Families (prefix -> section): k_ the parents' tab and the child's page (default view), sch_ the children's schedule editor, hh_ the household schedule editor,
kd_ the collapsed "Подробности для взрослых" (technical words allowed ONLY there)."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from gw.strings import S, ERRORS  # noqa: E402

BEGIN, END = "<!-- BEGIN GENERATED STRINGS -->", "<!-- END GENERATED STRINGS -->"
SECTIONS = [("k_", "Родительский контроль: список детей, плитка, окно «Открыть доступ», страница ребёнка (по умолчанию видно только это)"),
            ("sch_", "Расписание ребёнка («Когда открыты YouTube и Roblox»)"),
            ("hh_", "Расписание домашних устройств (розетки, пылесосы)"),
            ("kd_", "«Подробности для взрослых» (свёрнуто; только здесь допустимы технические слова)")]
ERROR_KEYS = ["maps_always_open", "validation_failed", "max_exception", "latest_end", "midnight", "too_late", "max_on", "allowed_range", "min_gap", "overlap", "version_conflict",
              "forbidden", "schedule_service_unreachable", "adguard_unavailable"]


def cell(v):
    t = " / ".join(v) if isinstance(v, list) else v
    return t.replace("|", "\\|")


def generate():
    out = [BEGIN, ""]
    for prefix, title in SECTIONS:
        out += ["### " + title, "", "| Ключ | Текст |", "|---|---|"]
        for k in S:
            if k.startswith(prefix):
                out.append("| `%s` | %s |" % (k, cell(S[k])))
        out.append("")
    out += ["### Тексты ошибок, которые видит родитель (сервер отвечает кодом, страница показывает фразу)", "", "| Код | Текст |", "|---|---|"]
    for k in ERROR_KEYS:
        out.append("| `%s` | %s |" % (k, cell(ERRORS.get(k, "—"))))
    out += ["", END]
    return "\n".join(out)


def splice(doc_text, gen):
    a, b = doc_text.index(BEGIN), doc_text.index(END) + len(END)
    return doc_text[:a] + gen + doc_text[b:]


if __name__ == "__main__":
    g = generate()
    if "--write" in sys.argv:
        path = sys.argv[sys.argv.index("--write") + 1]
        t = open(path, encoding="utf-8").read()
        open(path, "w", encoding="utf-8", newline="\n").write(splice(t, g))
    elif "--check" in sys.argv:
        path = sys.argv[sys.argv.index("--check") + 1]
        t = open(path, encoding="utf-8").read()
        sys.exit(0 if splice(t, g) == t else 1)
    else:
        print(g)
