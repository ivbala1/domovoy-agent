"""Части prompt.md: ядро, разделы, пределы, рост (domovoy/promptparts.py)."""
import re
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domovoy import promptparts as pp  # noqa: E402

CORE = """# Домовой

| Раздел по случаю | Когда читать |
|---|---|
| Утро | утро |
| Поиск | поиск |

## Журнал

```
## 2026-09-14 (пн)
```
"""


def doc(utro: str = "Утро.\n", poisk: str = "Поиск.\n", core: str = CORE) -> str:
    return (core + "\n# Разделы по случаю\n\n## Утро (было в «Будни»)\n\n" + utro
            + "\n## Поиск\n\n" + poisk)


class SplitTests(unittest.TestCase):
    def test_parts(self):
        lay = pp.split(doc())
        self.assertEqual(lay.table, ["Утро", "Поиск"])
        self.assertEqual(list(lay.sections), ["Утро", "Поиск"])   # «## 2026» в блоке кода ядра не раздел
        self.assertEqual(lay.core, len((CORE + "\n").encode("utf-8")))
        self.assertEqual(lay.total, len(doc().encode("utf-8")))
        self.assertEqual(pp.problems(lay), [])

    def test_no_layout(self):
        self.assertIsNone(pp.split("# Домовой\n\nправила\n"))
        self.assertTrue(pp.problems(None))

    def test_table_mismatch(self):
        bad = pp.problems(pp.split(doc().replace("| Поиск | поиск |\n", "| Здоровье | всегда |\n")))
        self.assertTrue(any("«Здоровье»" in p and "заголовка нет" in p for p in bad))
        self.assertTrue(any("«Поиск» не назван" in p for p in bad))

    @unittest.mock.patch.object(pp, "LINE_MAX_CHARS", 10 ** 6)
    def test_limits_exact_bytes(self):
        core = CORE + "я" * ((pp.CORE_MAX_BYTES - len((CORE + "\n").encode()) ) // 2)
        self.assertEqual(pp.problems(pp.split(doc(core=core))), [])
        self.assertTrue(any(p.startswith("ядро") for p in pp.problems(pp.split(doc(core=core + "яя")))))
        big = "ж" * (pp.SECTION_MAX_BYTES // 2 + 1)
        self.assertTrue(any("«Поиск»" in p for p in pp.problems(pp.split(doc(poisk=big)))))

    def test_core_after_edit(self):
        text = doc()
        self.assertEqual(pp.core_after_edit(text, "Утро.", "Утро длиннее."), pp.split(text).core)
        self.assertEqual(pp.core_after_edit(text, "## Журнал", "## Журнал!"), pp.split(text).core + 1)
        self.assertIsNone(pp.core_after_edit(text, "нет такого", "x"))


class GrowthTests(unittest.TestCase):
    def test_no_history(self):
        lines = pp.growth(pp.split(doc()), None)
        self.assertTrue(all("нет данных" in ln for ln in lines[:-1]))

    def test_new_section_and_growth(self):
        before = pp.split(doc(poisk="П.\n").replace("## Утро (было в «Будни»)\n\nУтро.\n", "")
                          .replace("| Утро | утро |\n", ""))
        now = pp.split(doc(poisk="П. " + "ж" * 100 + "\n"))
        lines = pp.growth(now, before)
        self.assertIn("новый", next(ln for ln in lines if ln.startswith("- Утро")))
        self.assertIn("рост больше 10%", next(ln for ln in lines if ln.startswith("- Поиск")))

    def test_move_is_not_growth(self):
        text = "Абзац, который переезжает. " * 10 + "\n"
        before = pp.split(doc(utro="Утро.\n" + text))
        now = pp.split(doc(poisk="Поиск.\n" + text))
        line = next(ln for ln in pp.growth(now, before) if ln.startswith("- Поиск"))
        self.assertIn("похоже на перенос из «Утро»", line)

    def test_no_layout_now(self):
        self.assertIn("раскладки нет", pp.growth(None, None)[0])


class LivePromptTests(unittest.TestCase):
    def test_live_prompt_layout(self):
        """Живой промт: ядро и разделы в пределах, таблица совпадает с заголовками."""
        p = Path(__file__).resolve().parents[1] / "prompt.md"
        if not p.exists():
            self.skipTest("нет prompt.md")
        lay = pp.split(p.read_text(encoding="utf-8"))
        if lay is None:
            self.skipTest("prompt.md без раскладки")
        self.assertEqual(pp.problems(lay), [])

    def _live(self) -> str:
        p = Path(__file__).resolve().parents[1] / "prompt.md"
        if not p.exists():
            self.skipTest("нет prompt.md")
        return p.read_text(encoding="utf-8")

    def test_eight_principles(self):
        """В «Как решать» ровно восемь принципов: новый вытесняет старый, а не дописывается."""
        text = self._live()
        if "\n## Как решать\n" not in text:
            self.skipTest("нет раздела «Как решать»")
        block = text.split("\n## Как решать\n", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(len(re.findall(r"^\d+\. \*\*", block, re.M)), 8)

    def test_live_rules_total(self):
        """Все тексты правил вместе и поправки в пределах."""
        root = Path(__file__).resolve().parents[1]
        if not (root / "prompt.md").exists():
            self.skipTest("нет prompt.md")
        self.assertEqual(pp.rules_problems(pp.rules_total(root)), [])


class RuleTextTests(unittest.TestCase):
    def test_line_history_and_dash_flagged(self):
        bad = pp.problems(pp.split(doc(utro="ж" * (pp.LINE_MAX_CHARS + 1) + "\n")))
        self.assertTrue(any("знаков больше" in p for p in bad))
        self.assertTrue(any("решение Ивана" in p for p in pp.problems(pp.split(doc(utro="(решение Ивана 12.11)\n")))))
        self.assertTrue(any("тире" in p for p in pp.problems(pp.split(doc(utro="а \u2014 б\n")))))
        self.assertEqual(pp.problems(pp.split(doc(utro="решение Ивана об отпуске\n"))), [])

    def test_file_limit(self):
        big = ("ж" * 1000 + "\n") * (pp.FILE_MAX_BYTES // 2000 + 1)
        lay = pp.split(doc(utro=big[: len(big) // 2], poisk=big[len(big) // 2:]))
        self.assertTrue(any(p.startswith("prompt.md") for p in pp.problems(lay)))

    def test_data_header(self):
        self.assertEqual(pp.data_header("# Т\n\nФормат.\n\n| a |\n"), "# Т\n\nФормат.\n\n")
        self.assertEqual(pp.data_header("# Т\n- строка\n"), "# Т\n")
        self.assertEqual(pp.data_header("Поиск 25.10: строка\n"), "")
        self.assertEqual(pp.data_header("# M\nСроки.\nИнтернет до: 2026-11-13\n"), "# M\nСроки.\n")
        self.assertEqual(pp.data_header("# A\nУсловия.\n```json\n{}\n```\n"), "# A\nУсловия.\n")
        self.assertEqual(pp.data_header("# P\nФормат строки: sku | что\n\n76218_3122 | 7197\n"),
                         "# P\nФормат строки: sku | что\n\n")

    def test_rules_total_counts_parts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "prompt.md").write_text("абв", encoding="utf-8")
            (root / "diary.md").write_text("д", encoding="utf-8")
            (root / "data" / "zhdet.md").write_text("# Ж\nФормат\n| x |\n", encoding="utf-8")
            (root / "data" / "popravki.md").write_text("# П\n- 2026-11-12 «x»\n", encoding="utf-8")
            parts = pp.rules_total(root)
            self.assertEqual(parts["prompt.md"], 6)
            self.assertEqual(parts["diary.md"], 2)
            self.assertEqual(parts["шапки data/*.md"], len("# Ж\nФормат\n".encode()))
            self.assertEqual(parts["data/popravki.md"], len("# П\n- 2026-11-12 «x»\n".encode()))
            self.assertEqual(pp.rules_total(root, {"diary.md": "дд"})["diary.md"], 4)
            with unittest.mock.patch.object(pp, "RULES_MAX_BYTES", 10):
                self.assertTrue(pp.rules_problems(parts))

    def test_event_texts_counted(self):
        root = Path(__file__).resolve().parents[1]
        self.assertGreater(pp.rules_total(root)["тексты событий в коде"], 1000)


if __name__ == "__main__":
    unittest.main()
