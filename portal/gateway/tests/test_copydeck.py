"""Task 31 Part B/E: the copy deck is the product's text. Plain Russian for the parents' default view; the technical words live only in the collapsed adult details."""
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from gw.strings import S, ERRORS  # noqa: E402

DECK = os.environ.get("COPY_DECK") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(ROOT))), "docs", "portal", "ui-copy-ru.md")
DEFAULT_VIEW = ("k_", "sch_", "hh_")
BANNED = [r"\bdns\b", r"snapshot", r"правила применены", r"заблокировано запросов", r"\bip\b", r"\bmac\b", r"\bapi\b", r"\bhttps?\b", r"\btarget\b", r"\bexception\b",
          r"guardrail", r"\bcron\b", r"\brules?\b", r"\bparity\b", r"\bsync\b", r"\bjob\b", r"ошибка \d", r"\bcode\b", r"[a-z]+_[a-z_]+"]
LATIN_OK = {"YouTube", "Roblox", "Telegram", "Android", "Mi", "WiFi"}          # brand and app names that a parent knows by their Latin spelling


def plain(v):
    return " ".join(v) if isinstance(v, list) else v


class DefaultViewIsPlain(unittest.TestCase):
    def test_no_technical_words_in_the_default_view(self):
        bad = []
        for k, v in S.items():
            if not k.startswith(DEFAULT_VIEW):
                continue
            t = re.sub(r"\{[a-z_]+\}", "", plain(v))
            if k == "k_c_sites_ph":
                t = t.replace("school.example.org", "")                      # a placeholder that shows what a site name looks like
            for pat in BANNED:
                if re.search(pat, t, re.I):
                    bad.append((k, pat))
            for w in re.findall(r"[A-Za-z]{2,}", t):
                if w not in LATIN_OK:
                    bad.append((k, "latin word " + w))
        self.assertEqual(bad, [])

    def test_the_family_is_not_empty_and_the_details_may_be_technical(self):
        self.assertGreater(len([k for k in S if k.startswith("k_")]), 60)
        self.assertGreater(len([k for k in S if k.startswith("sch_")]), 20)
        self.assertGreater(len([k for k in S if k.startswith("hh_")]), 15)
        self.assertIn("DNS", S["kd_dns_yes"])                                  # the adult details keep the technical truth

    def test_the_parent_facing_error_texts_have_no_codes(self):
        for k in ("maps_always_open", "validation_failed", "max_exception", "latest_end", "midnight", "too_late", "max_on", "allowed_range", "min_gap", "overlap",
                  "version_conflict", "forbidden", "schedule_service_unreachable"):
            self.assertNotRegex(ERRORS[k], r"[A-Za-z_]{4,}", k)

    def test_the_tile_sentences_from_the_task_are_there(self):
        self.assertEqual(S["k_open_access"], "Открыть доступ")
        self.assertIn("Интернет ограничен", S["k_s_closed_pl"])
        self.assertEqual(S["k_s_all"], "Открыто всё до {t}")
        self.assertEqual(S["k_s_sunday"], "Сегодня воскресенье: YouTube и Roblox закрыты")
        self.assertEqual(S["k_always"].format(list="x"), "Всегда открыто: x")
        self.assertEqual([S["k_d_30"], S["k_d_60"], S["k_d_120"], S["k_d_eod"]], ["30 минут", "1 час", "2 часа", "До конца дня"])
        self.assertEqual(S["k_all_note"], "Откроется весь интернет, кроме того, что вы сами запретили, и защитных списков.")
        self.assertEqual(S["k_snack_undo"], "Отменить")
        self.assertEqual(S["kd_title"], "Подробности для взрослых")
        self.assertEqual(S["sch_err_to"].format(to="21:00"), "Дольше 21:00 нельзя — это общее ограничение, его меняет Иван.")


@unittest.skipUnless(os.path.exists(DECK), "the docs repository is not next to the gateway (the deck lives in docs/portal/ui-copy-ru.md)")
class DeckMatchesTheProduct(unittest.TestCase):
    def test_the_generated_tables_equal_the_strings(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gen_copy_deck.py"), "--check", DECK], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "run: python tools/gen_copy_deck.py --write %s" % DECK)


if __name__ == "__main__":
    unittest.main()
