"""Запись общих данных под замком синхронизации (ревью Codex 22.10.2026)."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domovoy import data  # noqa: E402
from domovoy import promptparts as pp  # noqa: E402


@contextlib.contextmanager
def _held():
    yield True


@contextlib.contextmanager
def _busy():
    yield False


def _edit(old: str, new: str) -> str:
    return f"<<<<<<< old\n{old}=======\n{new}>>>>>>> new\n"


class DataWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        (self.root / "journal").mkdir()
        self.table = self.root / "data" / "voronka.md"
        self.table.write_text("| Acme | ждём |\n| Beta | ждём |\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cmd(self, cmd: str, rel: str, stdin: str, lock=_held) -> str:
        return data.run(cmd, rel, stdin, root=self.root, lock=lock)

    def test_append_creates_and_appends(self) -> None:
        self.run_cmd("append", "journal/2026-10.md", "- 21:40 первая")
        self.run_cmd("append", "journal/2026-10.md", "- 21:41 вторая\n")
        self.assertEqual((self.root / "journal" / "2026-10.md").read_text(encoding="utf-8"),
                         "- 21:40 первая\n- 21:41 вторая\n")

    def test_edit_unique_block(self) -> None:
        self.run_cmd("edit", "data/voronka.md", _edit("| Acme | ждём |\n", "| Acme | отказ |\n"))
        self.assertEqual(self.table.read_text(encoding="utf-8"), "| Acme | отказ |\n| Beta | ждём |\n")

    def test_edit_all_or_nothing(self) -> None:
        before = self.table.read_text(encoding="utf-8")
        stdin = _edit("| Acme | ждём |\n", "| Acme | отказ |\n") + _edit("| Gamma |\n", "x\n")
        with self.assertRaises(data.Refused):
            self.run_cmd("edit", "data/voronka.md", stdin)
        self.assertEqual(self.table.read_text(encoding="utf-8"), before)

    def test_edit_ambiguous_refused(self) -> None:
        with self.assertRaises(data.Refused):
            self.run_cmd("edit", "data/voronka.md", _edit("| ждём |", "| x |"))

    def test_text_outside_blocks_refused(self) -> None:
        with self.assertRaises(data.Refused):
            self.run_cmd("edit", "data/voronka.md", "лишнее\n" + _edit("| Acme | ждём |\n", "y\n"))

    def test_paths_limited_to_data(self) -> None:
        for rel in ("domovoy/wake.py", "../etc/passwd", "data/x.txt", "journal", "README.md"):
            with self.subTest(rel=rel), self.assertRaises(data.Refused):
                self.run_cmd("append", rel, "x")
        (self.root / "data" / "link.md").symlink_to(self.root / "README.md")
        with self.assertRaises(data.Refused):
            self.run_cmd("append", "data/link.md", "x")
        self.run_cmd("append", "prompt.md", "x")

    def test_busy_lock_writes_nothing(self) -> None:
        before = self.table.read_text(encoding="utf-8")
        with self.assertRaises(data.Refused):
            self.run_cmd("edit", "data/voronka.md", _edit("| Acme | ждём |\n", "z\n"), lock=_busy)
        self.assertEqual(self.table.read_text(encoding="utf-8"), before)



class DataWriteEdgeTests(DataWriteTests):
    def test_old_checked_against_original(self) -> None:
        # Второй old есть только в new первого блока: отказ, файл прежний.
        stdin = _edit("| Acme | ждём |\n", "| Acme | X |\n") + _edit("| Acme | X |\n", "| Y |\n")
        with self.assertRaises(data.Refused):
            self.run_cmd("edit", "data/voronka.md", stdin)

    def test_overlapping_blocks_refused(self) -> None:
        stdin = _edit("| Acme | ждём |\n| Beta", "a") + _edit("| Beta | ждём |\n", "b\n")
        with self.assertRaises(data.Refused):
            self.run_cmd("edit", "data/voronka.md", stdin)

    def test_marker_inside_text_refused(self) -> None:
        with self.assertRaises(data.Refused):
            self.run_cmd("edit", "data/voronka.md", _edit("| Acme | ждём |\n", "x\n=======\ny\n"))

    def test_crlf_preserved(self) -> None:
        self.table.write_bytes("| A | 1 |\r\n| B | 2 |\r\n".encode())
        self.run_cmd("edit", "data/voronka.md", _edit("| A | 1 |\r\n", "| A | 9 |\r\n"))
        self.run_cmd("append", "data/voronka.md", "| C | 3 |\n")
        self.assertEqual(self.table.read_bytes(), "| A | 9 |\r\n| B | 2 |\r\n| C | 3 |\r\n".encode())

    def test_delete_line_with_empty_new(self) -> None:
        self.run_cmd("edit", "data/voronka.md", _edit("| Beta | ждём |\n", ""))
        self.assertEqual(self.table.read_text(encoding="utf-8"), "| Acme | ждём |\n")

    def test_missing_dir_refused_and_mode_kept(self) -> None:
        with self.assertRaises(data.Refused):
            self.run_cmd("append", "data/nope/x.md", "x")
        self.table.chmod(0o640)
        self.run_cmd("edit", "data/voronka.md", _edit("| Acme | ждём |\n", "z\n"))
        self.assertEqual(self.table.stat().st_mode & 0o777, 0o640)
        out = self.run_cmd("append", "data/new.md", "x")
        self.assertIn("создан", out)
        self.assertNotEqual((self.root / "data" / "new.md").stat().st_mode & 0o777, 0o600)

    def test_only_whole_lines_match(self) -> None:
        self.table.write_text("new status: pending\n", encoding="utf-8")
        with self.assertRaises(data.Refused):
            self.run_cmd("edit", "data/voronka.md", _edit("status: pending\n", "status: done\n"))

    def test_last_line_without_newline(self) -> None:
        self.table.write_text("| A |\n| B |", encoding="utf-8")
        self.run_cmd("edit", "data/voronka.md", _edit("| B |\n", "| C |\n"))
        self.assertEqual(self.table.read_text(encoding="utf-8"), "| A |\n| C |")

    def test_last_line_without_newline_crlf(self) -> None:
        self.table.write_bytes(b"A\r\nB")
        self.run_cmd("edit", "data/voronka.md", _edit("B\r\n", "C\r\n"))
        self.assertEqual(self.table.read_bytes(), b"A\r\nC")

    def test_stuck_rebase_refused(self) -> None:
        (self.root / ".git" / "rebase-merge").mkdir(parents=True)
        from domovoy import sync
        if "rebase-merge" not in sync.STUCK_NAMES:
            self.skipTest("нет rebase-merge в STUCK_NAMES")
        with self.assertRaises(data.Refused):
            self.run_cmd("append", "journal/2026-10.md", "x")


class RulesWallTests(DataWriteTests):
    """Тексты правил: ран бота их не пишет, сумма и поправки под пределом."""

    def setUp(self) -> None:
        super().setUp()
        self.zhdet = self.root / "data" / "zhdet.md"
        self.zhdet.write_text("# Ждёт\n\nФормат строки.\n\n- дело\n", encoding="utf-8")

    def bot(self):
        return mock.patch.dict(os.environ, {"CLAUDE_TG_CHAT_KEY": "123:0"})

    def test_bot_run_refused_on_rule_files(self) -> None:
        with self.bot():
            for rel in ("prompt.md", "diary.md"):
                with self.subTest(rel=rel), self.assertRaises(data.Refused):
                    self.run_cmd("append", rel, "новое правило")
            with self.assertRaises(data.Refused):
                self.run_cmd("edit", "data/zhdet.md", _edit("Формат строки.\n", "Каждое утро весь список.\n"))
            self.run_cmd("append", "data/zhdet.md", "- ещё дело")           # строки данных можно
            self.run_cmd("edit", "data/zhdet.md", _edit("- дело\n", "- дело, снято\n"))
            self.run_cmd("append", "data/popravki.md", "- 2026-11-12 «по пятницам зал не ставь»")
        self.assertFalse((self.root / "prompt.md").exists())
        self.assertIn("Формат строки.", self.zhdet.read_text(encoding="utf-8"))

    def test_bot_run_updates_key_value_and_json_data(self) -> None:
        pakety = self.root / "data" / "pakety.md"
        pakety.write_text("# Пакеты\n\nСроки пакетов.\n\nСвязь до: 2026-11-13\nЗвонки до: 2026-11-20\n", encoding="utf-8")
        bilety = self.root / "data" / "bilety.md"
        bilety.write_text('# Билеты\n\nУсловия.\n\n```json\n{"price": 16000}\n```\n', encoding="utf-8")
        with self.bot():
            self.run_cmd("edit", "data/pakety.md", _edit("Связь до: 2026-11-13\n", "Связь до: 2026-12-13\n"))
            self.run_cmd("edit", "data/bilety.md", _edit('{"price": 16000}\n', '{"price": 15000}\n'))
            with self.assertRaises(data.Refused):
                self.run_cmd("edit", "data/pakety.md", _edit("Сроки пакетов.\n", "Каждое утро напоминай.\n"))

    def test_desktop_session_may_edit_header(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_TG_CHAT_KEY", None)
            self.run_cmd("edit", "data/zhdet.md", _edit("Формат строки.\n", "Формат: дело.\n"))

    def test_popravki_over_limit_refused(self) -> None:
        line = "- 2026-11-12 «" + "я" * 200 + "»"
        with self.bot():
            for _ in range(pp.POPRAVKI_MAX_BYTES // len(line.encode())):
                self.run_cmd("append", "data/popravki.md", line)
            with self.assertRaises(data.Refused) as cm:
                self.run_cmd("append", "data/popravki.md", line)
        self.assertIn("Поправка Ивана", str(cm.exception))

    def test_total_wall_refuses_growth_allows_shrink(self) -> None:
        os.environ.pop("CLAUDE_TG_CHAT_KEY", None)
        with mock.patch.object(pp, "RULES_MAX_BYTES", 200):
            (self.root / "diary.md").write_text("д" * 150, encoding="utf-8")
            with self.assertRaises(data.Refused):
                self.run_cmd("append", "diary.md", "ещё правило")
            self.run_cmd("edit", "diary.md", _edit("д" * 150 + "\n", "д" * 10 + "\n"))


if __name__ == "__main__":
    unittest.main()
