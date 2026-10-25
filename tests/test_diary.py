"""Тесты дневника: сохранность файла дня, повторы, события, шапка, лента, git."""
from __future__ import annotations

import fcntl
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domovoy import diary  # noqa: E402

TZ = ZoneInfo("Asia/Dubai")
NOW = datetime(2026, 10, 4, 20, 45, tzinfo=TZ)
DAY = date(2026, 10, 4)

OLD_DAY = (
    "---\n"
    "schema: diary-day/v1\n"
    "type: day\n"
    "date: 2026-10-04\n"
    "sensitivity: private\n"
    "люди: []\n"
    "места: []\n"
    "фото: synced\n"
    "---\n"
    "\n"
    "###### 09:10\n"
    "<!-- src:tg:-100:5215:7001 sha:aaaaaaaa kind:text -->\n"
    "Писал сам, руками.\n"
    "\n"
    "<!-- machine:photos v1 -->\n"
    "📷 3 кадр(ов) в Immich\n"
    "<!-- /machine:photos -->\n"
)


class VaultCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        (self.vault / "Дневник" / "2026").mkdir(parents=True)
        self.day_path = self.vault / "Дневник" / "2026" / "2026-10-04.md"
        self._env = patch.dict(os.environ, {"DOMOVOY_VAULT_DIR": str(self.vault)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def run_cli(self, *argv: str, stdin: str = "", now: datetime = NOW) -> tuple[int, str]:
        out = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(stdin)), \
                patch.object(sys, "stdout", out), patch.object(sys, "stderr", out):
            rc = diary.main(list(argv), now=now)
        return rc, out.getvalue()


class AddTests(VaultCase):
    def test_creates_day_with_skeleton(self):
        rc, _ = self.run_cli("add", "--label", "день", stdin="Гуляли в парке.\n")
        self.assertEqual(rc, diary.OK)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nschema: diary-day/v1\n"))
        self.assertIn("фото: pending\n", text)
        self.assertIn("###### 20:45\n<!-- src:domovoy:2026-10-04:день sha:", text)
        self.assertIn("kind:retold logged:2026-10-04T20:45 -->\nГуляли в парке.\n", text)

    def test_existing_text_survives_and_block_goes_before_machine_tail(self):
        self.day_path.write_text(OLD_DAY, encoding="utf-8")
        rc, _ = self.run_cli("add", "--label", "день", stdin="Вечером варили креветки.\n")
        self.assertEqual(rc, diary.OK)
        text = self.day_path.read_text(encoding="utf-8")
        # Всё, что было до машинного хвоста, осталось побайтно в начале файла.
        human_part = OLD_DAY[: OLD_DAY.index("<!-- machine:photos")].rstrip("\n")
        self.assertTrue(text.startswith(human_part))
        self.assertIn("<!-- machine:photos v1 -->\n📷 3 кадр(ов) в Immich\n<!-- /machine:photos -->\n", text)
        self.assertLess(text.index("креветки"), text.index("<!-- machine:photos"))

    def test_same_label_is_not_written_twice(self):
        self.run_cli("add", "--label", "день", stdin="Первый рассказ.\n")
        before = self.day_path.read_text(encoding="utf-8")
        rc, out = self.run_cli("add", "--label", "день", stdin="Тот же рассказ ещё раз.\n")
        self.assertEqual(rc, diary.DUPLICATE)
        self.assertIn("уже есть", out)
        self.assertEqual(self.day_path.read_text(encoding="utf-8"), before)

    def test_label_prefix_is_a_different_block(self):
        self.run_cli("add", "--label", "день", stdin="Раз.\n")
        rc, _ = self.run_cli("add", "--label", "день-2", stdin="Два.\n")
        self.assertEqual(rc, diary.OK)
        rc, _ = self.run_cli("add", "--label", "день", stdin="Три.\n")
        self.assertEqual(rc, diary.DUPLICATE)

    def test_original_goes_into_folded_callout(self):
        stdin = "Съездили в центр.\n\n=== исходные слова ===\nну мы это\n\nсъездили короче в центр\n"
        self.run_cli("add", "--label", "день", stdin=stdin)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn(
            "Съездили в центр.\n\n> [!quote]- Исходные слова\n> ну мы это\n>\n> съездили короче в центр\n",
            text,
        )
        self.assertNotIn("=== исходные слова ===", text)

    def test_other_day_is_marked_as_written_later(self):
        morning = datetime(2026, 10, 5, 8, 40, tzinfo=TZ)
        rc, _ = self.run_cli(
            "add", "--day", "2026-10-04", "--label", "вечер", stdin="Потом пошли в гости.\n", now=morning,
        )
        self.assertEqual(rc, diary.OK)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("###### позже (записано 05.10)\n", text)
        self.assertIn("logged:2026-10-05T08:40", text)

    def test_night_goes_to_yesterday(self):
        night = datetime(2026, 10, 5, 1, 30, tzinfo=TZ)
        self.run_cli("add", "--label", "ночь", stdin="Досмотрели кино.\n", now=night)
        self.assertTrue(self.day_path.is_file())
        self.assertIn("###### 01:30\n", self.day_path.read_text(encoding="utf-8"))

    def test_people_and_places_into_header(self):
        (self.vault / "Люди").mkdir()
        (self.vault / "Люди" / "Тома.md").write_text("x", encoding="utf-8")
        rc, out = self.run_cli(
            "add", "--label", "день", "--people", "Тома", "Кто-то", "--places", "Тольятти",
            stdin="Были у Томы.\n",
        )
        self.assertEqual(rc, diary.OK)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertEqual(diary.read_fm_list(text, "люди"), ["Тома", "Кто-то"])
        self.assertEqual(diary.read_fm_list(text, "места"), ["Тольятти"])
        self.assertIn("у «Кто-то» нет карточки", out)
        self.assertNotIn("у «Тома» нет карточки", out)

    def test_family_card_in_family_folder(self):
        (self.vault / "Семья" / "Аня").mkdir(parents=True)
        (self.vault / "Семья" / "Аня" / "Аня.md").write_text("x", encoding="utf-8")
        (self.vault / "Петя").mkdir()
        (self.vault / "Петя" / "Петя.md").write_text("x", encoding="utf-8")
        rc, out = self.run_cli("add", "--label", "день", "--people", "Аня", "Петя",
                               stdin="Гуляли.\n")
        self.assertEqual(rc, diary.OK)
        self.assertNotIn("нет карточки", out)

    def test_empty_text_and_bad_label_refused(self):
        self.assertEqual(self.run_cli("add", "--label", "день", stdin="  \n")[0], diary.ERROR)
        self.assertEqual(self.run_cli("add", "--label", "два слова", stdin="Текст.\n")[0], diary.ERROR)
        self.assertFalse(self.day_path.exists())

    def test_dry_run_writes_nothing(self):
        rc, out = self.run_cli("add", "--label", "день", "--dry-run", stdin="Текст.\n")
        self.assertEqual(rc, diary.OK)
        self.assertIn("Текст.", out)
        self.assertFalse(self.day_path.exists())

    def test_time_must_be_a_real_clock_time(self):
        """Ревью 11.10.2026 (C42): 99:99, чужие цифры и перевод строки в конце не время."""
        for bad in ("99:99", "24:00", "12:60", "٠٩:٣٠", "09:30\n", "9:30"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cli("add", "--label", "день", "--time", bad, stdin="Текст.\n")[0], diary.ERROR)
                self.assertEqual(self.run_cli("fact", "Событие", "--time", bad)[0], diary.ERROR)
        self.assertFalse(self.day_path.exists())
        self.assertEqual(self.run_cli("fact", "Полночь", "--time", "00:00")[0], diary.OK)
        self.assertEqual(self.run_cli("add", "--label", "день", "--time", "23:59", stdin="Текст.\n")[0], diary.OK)

    def test_parallel_add_with_same_words_under_another_label_is_a_repeat(self):
        """Ревью 11.10.2026 (X-C3): запуск B с теми же исходными словами и другой
        меткой целиком проходит между проверкой запуска A и его замком."""
        story = "Пересказ.\n=== исходные слова ===\nгуляли в парке с Аней, было тепло\n"
        real_rewrite = diary.rewrite
        codes = []

        def racing(path, transform, **kw):
            if path == self.day_path and not codes:
                with patch.object(diary, "rewrite", real_rewrite):
                    codes.append(self.run_cli("add", "--label", "вечер", stdin=story)[0])
            return real_rewrite(path, transform, **kw)

        with patch.object(diary, "rewrite", racing):
            rc, out = self.run_cli("add", "--label", "день", stdin=story)
        self.assertEqual(codes, [diary.OK])
        self.assertEqual(rc, diary.DUPLICATE, out)
        self.assertIn("уже записаны под другой меткой", out)
        self.assertEqual(self.day_path.read_text(encoding="utf-8").count("> [!quote]- Исходные слова"), 1)


class FactTests(VaultCase):
    def test_fact_creates_block_before_photos(self):
        self.day_path.write_text(OLD_DAY, encoding="utf-8")
        rc, _ = self.run_cli("fact", "Пришло приглашение на собеседование", "--source", "почта", "--time", "20:45")
        self.assertEqual(rc, diary.OK)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn(
            "<!-- machine:domovoy v1 -->\n📌 События дня\n"
            "- 20:45 Пришло приглашение на собеседование (почта)\n<!-- /machine:domovoy -->\n",
            text,
        )
        self.assertLess(text.index("machine:domovoy"), text.index("machine:photos"))
        self.assertIn("Писал сам, руками.\n", text)

    def test_second_fact_appended_and_repeat_ignored(self):
        self.run_cli("fact", "Заплатил квартплату", "--source", "слова Ивана")
        self.run_cli("fact", "Вернули долг")
        before = self.day_path.read_text(encoding="utf-8")
        rc, out = self.run_cli("fact", "Заплатил  квартплату", "--source", "слова Ивана")
        self.assertEqual(rc, diary.OK)
        self.assertIn("уже записано", out)
        self.assertEqual(self.day_path.read_text(encoding="utf-8"), before)
        self.assertEqual(len(diary.event_lines(before)), 2)

    def test_entry_after_fact_stays_above_events(self):
        self.run_cli("fact", "Вернули долг")
        self.run_cli("add", "--label", "день", stdin="Рассказ дня.\n")
        text = self.day_path.read_text(encoding="utf-8")
        self.assertLess(text.index("Рассказ дня."), text.index("machine:domovoy"))

    def test_time_of_telling_is_not_time_of_event(self):
        """В 20:45 Иван говорит «утром оплатил страховку»: без --time времени в строке нет."""
        self.run_cli("fact", "Оплатил страховку", "--source", "слова Ивана")
        self.run_cli("fact", "Оплатил квартплату", "--day", "2026-10-01")
        self.assertIn("\n- Оплатил страховку (слова Ивана)\n", self.day_path.read_text(encoding="utf-8"))
        other = (self.vault / "Дневник" / "2026" / "2026-10-01.md").read_text(encoding="utf-8")
        self.assertIn("\n- Оплатил квартплату\n", other)

    def test_negation_is_not_a_repeat(self):
        self.run_cli("fact", "Не оплатил страховку", "--source", "слова Ивана")
        self.run_cli("fact", "Оплатил страховку", "--time", "18:00")
        lines = diary.event_lines(self.day_path.read_text(encoding="utf-8"))
        self.assertEqual(len(lines), 2)

    def test_events_differing_only_in_brackets_or_time_are_both_kept(self):
        for text in ("Встреча (утром)", "Встреча (вечером)", "10:00 поезд", "11:00 поезд"):
            self.assertEqual(self.run_cli("fact", text)[0], diary.OK)
        self.assertEqual(len(diary.event_lines(self.day_path.read_text(encoding="utf-8"))), 4)


class MarkTests(VaultCase):
    def test_mark_adds_without_dropping_block_style_list(self):
        block_style = OLD_DAY.replace("люди: []\n", "люди:\n  - Аня\n")
        self.day_path.write_text(block_style, encoding="utf-8")
        rc, _ = self.run_cli("mark", "--people", "Аня", "Маша", "--places", "Сочи")
        self.assertEqual(rc, diary.OK)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn('люди:\n  - "Аня"\n  - "Маша"\n', text)
        self.assertEqual(diary.read_fm_list(text, "люди"), ["Аня", "Маша"])
        self.assertEqual(diary.read_fm_list(text, "места"), ["Сочи"])

    def test_mark_without_day_file_is_error(self):
        self.assertEqual(self.run_cli("mark", "--people", "Аня")[0], diary.ERROR)

    def test_list_with_yaml_comment_is_refused_not_broken(self):
        """Ревью 11.10.2026 (X-C2): mark отвергает до записи, add пишет рассказ и предупреждает."""
        commented = OLD_DAY.replace("люди: []\n", "люди: # знакомые\n  - Тома\n")
        self.day_path.write_text(commented, encoding="utf-8")
        rc, out = self.run_cli("mark", "--people", "Аня")
        self.assertEqual(rc, diary.ERROR)
        self.assertIn("в непонятном виде", out)
        self.assertEqual(self.day_path.read_text(encoding="utf-8"), commented)
        rc, out = self.run_cli("add", "--label", "день", "--people", "Аня", stdin="Были у Томы.\n")
        self.assertEqual(rc, diary.OK)
        self.assertIn("список «люди» в шапке дня в непонятном виде", out)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("Были у Томы.", text)
        self.assertTrue(text.startswith(commented[: commented.index("---\n\n")]))

    def test_header_changed_while_waiting_or_retrying(self):
        """Шапка проверяется в каждой попытке записи (ревью 11.10.2026)."""
        rewrite = diary.rewrite
        for command in ("mark", "add"):
            for retry in (False, True):
                for fresh in (OLD_DAY.replace("люди: []", "люди: [] # с телефона"),
                              OLD_DAY.replace("места: []", "места: [] # с телефона"),
                              "День без шапки\n"):
                    with self.subTest(command=command, retry=retry, fresh=fresh):
                        self.day_path.write_text(OLD_DAY, encoding="utf-8")
                        calls = []

                        def racing_rewrite(path, transform, **kwargs):
                            if not retry:
                                path.write_text(fresh, encoding="utf-8")

                            def racing_transform(text):
                                result = transform(text)
                                calls.append(text)
                                if retry and len(calls) == 1:
                                    path.write_text(fresh, encoding="utf-8")
                                return result

                            return rewrite(path, racing_transform, **kwargs)

                        argv = [command, "--people", "Аня", "--places", "Сочи"]
                        if command == "add":
                            argv += ["--label", "день"]
                        with patch.object(diary, "rewrite", racing_rewrite), \
                                patch.object(diary, "_after_write") as after:
                            rc, out = self.run_cli(*argv, stdin="Новый рассказ.\n")
                        if command == "mark":
                            self.assertEqual(rc, diary.ERROR)
                            self.assertEqual(self.day_path.read_text(encoding="utf-8"), fresh)
                            after.assert_not_called()
                            self.assertNotIn("это уже есть", out)
                        else:
                            self.assertEqual(rc, diary.OK)
                            self.assertIn("не записаны", out)
                            self.assertIn("Новый рассказ.", self.day_path.read_text(encoding="utf-8"))
                        self.assertEqual(len(calls), 2 if retry else 1)

    def test_add_warning_uses_final_retry_not_old_header(self):
        """Устаревшее предупреждение не выдаётся (ревью 11.10.2026)."""
        commented = OLD_DAY.replace("люди: []", "люди: [] # с телефона")
        self.day_path.write_text(commented, encoding="utf-8")
        rewrite = diary.rewrite
        calls = []

        def racing_rewrite(path, transform, **kwargs):
            def racing_transform(text):
                result = transform(text)
                calls.append(text)
                if len(calls) == 1:
                    path.write_text(OLD_DAY, encoding="utf-8")
                return result
            return rewrite(path, racing_transform, **kwargs)

        with patch.object(diary, "rewrite", racing_rewrite), patch.object(diary, "_after_write"):
            rc, out = self.run_cli("add", "--label", "день", "--people", "Аня", stdin="Рассказ.\n")
        self.assertEqual(rc, diary.OK)
        self.assertNotIn("не записаны", out)
        self.assertEqual(diary.read_fm_list(self.day_path.read_text(encoding="utf-8"), "люди"), ["Аня"])


class RunGitCleanupTests(unittest.TestCase):
    def test_interrupted_or_failed_communicate_reaps_process(self):
        """Исключение не оставляет git за замком (ревью 11.10.2026)."""
        for error in (KeyboardInterrupt(), RuntimeError("сбой чтения")):
            with self.subTest(error=type(error).__name__):
                proc = subprocess.Popen(
                    [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    start_new_session=True,
                )
                try:
                    self.assertEqual(proc.stdout.readline().strip(), "ready")
                    with patch.object(diary.subprocess, "Popen", return_value=proc), \
                            patch.object(proc, "communicate", side_effect=error), \
                            patch.object(diary, "GIT_TERM_GRACE", 0.2):
                        with self.assertRaises(type(error)) as raised:
                            diary.run_git(Path("."), "status")
                    self.assertIs(raised.exception, error)
                    self.assertIsNotNone(proc.poll())
                    self.assertTrue(proc.stdout.closed)
                    self.assertTrue(proc.stderr.closed)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
                    proc.stdout.close()
                    proc.stderr.close()

    def test_timeout_kills_hook_even_after_parent_exits(self):
        """Дочерний процесс игнорирует SIGTERM (ревью 11.10.2026)."""
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder) / "ready"
            child_code = (
                "import signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(ready)!r}).touch(); time.sleep(30)"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child_code!r}]).wait()"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True,
            )
            killpg = os.killpg
            parent_at_kill = []

            def send_group_signal(pid, sig):
                if sig == signal.SIGKILL:
                    parent_at_kill.append(proc.poll())
                return killpg(pid, sig)

            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "дочерний процесс не запустился")
                with patch.object(diary.subprocess, "Popen", return_value=proc), \
                        patch.object(diary.os, "killpg", side_effect=send_group_signal) as sent, \
                        patch.object(diary, "GIT_TIMEOUT", 0.1), \
                        patch.object(diary, "GIT_TERM_GRACE", 0.2):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        diary.run_git(Path("."), "commit")
                self.assertIsNotNone(proc.poll())
                self.assertIn(((proc.pid, signal.SIGKILL), {}), sent.call_args_list)
                self.assertEqual(parent_at_kill, [-signal.SIGTERM])
                self.assertTrue(proc.stdout.closed)
                self.assertTrue(proc.stderr.closed)
            finally:
                try:
                    killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # На маке завершающаяся группа может отказать в сигнале.
                    pass
                proc.wait()
                proc.stdout.close()
                proc.stderr.close()


class RewriteTests(unittest.TestCase):
    def test_foreign_edit_between_read_and_write_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "day.md"
            path.write_text("было\n", encoding="utf-8")
            calls = []

            def transform(text: str) -> str:
                calls.append(text)
                if len(calls) == 1:
                    # Правка с телефона приехала, пока мы готовили свою версию.
                    path.write_text("было\nс телефона\n", encoding="utf-8")
                return text + "наше\n"

            self.assertTrue(diary.rewrite(path, transform))
            self.assertEqual(path.read_text(encoding="utf-8"), "было\nс телефона\nнаше\n")
            self.assertEqual(len(calls), 2)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["day.md"])

    def test_gives_up_when_file_keeps_changing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "day.md"
            path.write_text("0\n", encoding="utf-8")
            n = [0]

            def transform(text: str) -> str:
                n[0] += 1
                path.write_text(f"{n[0]}\n" * (n[0] + 1), encoding="utf-8")
                return text + "наше\n"

            with self.assertRaises(diary.DayFileChanged):
                diary.rewrite(path, transform)
            self.assertNotIn("наше", path.read_text(encoding="utf-8"))

    def test_busy_lock_refuses_and_leaves_file(self):
        """Второй свой писатель (фото) держит замок: запись ждёт, потом отказ."""
        import fcntl

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "day.md"
            path.write_text("было\n", encoding="utf-8")
            diary.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(diary.LOCK_FILE, "a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                with patch.object(diary, "LOCK_WAIT_SEC", 0.2), self.assertRaises(TimeoutError):
                    diary.rewrite(path, lambda t: t + "наше\n")
                self.assertEqual(path.read_text(encoding="utf-8"), "было\n")
            self.assertTrue(diary.rewrite(path, lambda t: t + "наше\n"))
            self.assertEqual(path.read_text(encoding="utf-8"), "было\nнаше\n")

    def test_missing_file_without_skeleton_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(diary.rewrite(Path(d) / "нет.md", lambda t: t + "x"))
            self.assertEqual(list(Path(d).iterdir()), [])


class FeedAndStatusTests(VaultCase):
    def _day(self, iso: str, body: str = "###### 10:00\n<!-- src:domovoy:x:y sha:1 kind:retold -->\nТекст.\n") -> None:
        d = date.fromisoformat(iso)
        p = diary.day_file_path(self.vault / "Дневник", d)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(diary.build_day_skeleton(d) + "\n" + body, encoding="utf-8")

    def test_feed_is_rebuilt_whole_and_keeps_fourteen(self):
        for i in range(1, 21):
            self._day(f"2026-09-{i:02d}")
        self.assertTrue(diary.write_feed(self.vault / "Дневник"))
        feed = (self.vault / "Дневник" / "Лента.md").read_text(encoding="utf-8")
        embeds = [ln for ln in feed.splitlines() if ln.startswith("![[")]
        self.assertEqual(len(embeds), 14)
        self.assertEqual(embeds[0], "![[Дневник/2026/2026-09-20]]")
        self.assertEqual(embeds[-1], "![[Дневник/2026/2026-09-07]]")
        self.assertIn("## [[Дневник/2026/2026-09-20|20.09.2026, вс]]", feed)
        # Догрузка старого дня порядок не ломает, повтор ничего не меняет.
        self._day("2026-08-30")
        self.assertFalse(diary.write_feed(self.vault / "Дневник"))

    def test_add_rebuilds_feed(self):
        self.run_cli("add", "--label", "день", stdin="Текст.\n")
        feed = (self.vault / "Дневник" / "Лента.md").read_text(encoding="utf-8")
        self.assertIn("![[Дневник/2026/2026-10-04]]", feed)

    def test_broken_feed_is_rebuilt_and_write_succeeds(self):
        """Ревью 11.10.2026 (C38): лента не в UTF-8 (обрыв посреди буквы)."""
        feed = self.vault / "Дневник" / "Лента.md"
        feed.write_bytes("Лента".encode("utf-8")[:3])
        rc, out = self.run_cli("add", "--label", "день", stdin="Гуляли.\n")
        self.assertEqual(rc, diary.OK, out)
        self.assertNotIn("лента не пересобрана", out)
        self.assertIn("![[Дневник/2026/2026-10-04]]", feed.read_text(encoding="utf-8"))
        feed.write_bytes(b"\xff\xfe")
        rc, out = self.run_cli("feed")
        self.assertEqual((rc, out.strip()), (diary.OK, "лента пересобрана"))

    def test_feed_failure_does_not_change_the_write_result(self):
        """Ревью 11.10.2026 (C38): любой сбой ленты только предупреждение, коммит идёт."""
        with patch.object(diary, "write_feed", side_effect=ValueError("сбой ленты")), \
                patch.object(diary, "commit", return_value="закоммичено") as commit:
            rc, out = self.run_cli("add", "--label", "день", stdin="Гуляли.\n")
        self.assertEqual(rc, diary.OK, out)
        self.assertIn("лента не пересобрана: сбой ленты", out)
        commit.assert_called_once()

    def test_stale_run_does_not_drop_a_new_day_from_feed(self):
        """Ревью 11.10.2026 (X-C4): запуск B с новым днём целиком проходит, пока
        запуск A ждёт замок ленты; A не должен вернуть ленту без нового дня."""
        self._day("2026-10-03")
        feed = self.vault / "Дневник" / "Лента.md"
        real_rewrite = diary.rewrite
        done = []

        def racing(path, transform, **kw):
            if path == feed and not done:
                done.append(True)
                with patch.object(diary, "rewrite", real_rewrite):
                    self.run_cli("add", "--label", "день", stdin="Новый день.\n")
            return real_rewrite(path, transform, **kw)

        with patch.object(diary, "rewrite", racing):
            self.assertEqual(self.run_cli("fact", "--day", "2026-10-03", "Вернули долг")[0], diary.OK)
        self.assertTrue(done)
        self.assertIn("![[Дневник/2026/2026-10-04]]", feed.read_text(encoding="utf-8"))

    def test_status_names_gaps_and_missing_rollup(self):
        self._day("2026-10-04")
        self.run_cli("fact", "Вернули долг")
        rc, out = self.run_cli("status", "--days", "2")
        self.assertEqual(rc, diary.OK)
        self.assertIn("2026-10-04 вс: записей 1, событий 1, люди: нет, фото: pending", out)
        self.assertIn("2026-10-03 сб: файла дня нет", out)
        self.assertIn("сводки недель: все на месте", out)

    def test_status_lists_every_missed_rollup_not_only_last_week(self):
        for iso in ("2026-09-29", "2026-10-06", "2026-10-21"):   # W40, W41, W43; в W42 дней нет
            self._day(iso)
        monday_after = datetime(2026, 10, 26, 8, 30, tzinfo=TZ)
        out = self.run_cli("status", "--days", "1", now=monday_after)[1]
        for week, since in (("2026-W40", "28.09"), ("2026-W41", "05.10"), ("2026-W43", "19.10")):
            self.assertIn(f"сводка недели с {since}: {week}.md НЕТ", out)
        self.assertNotIn("2026-W42", out)
        self.assertNotIn("2026-W39", out)   # до Домового сводки писал бот
        self.assertNotIn("2026-W44", out)   # неделя ещё идёт
        rollups = self.vault / "Дневник" / "_Роллапы"
        rollups.mkdir()
        (rollups / "2026-W40.md").write_text("x", encoding="utf-8")   # оборванная запись
        (rollups / "2026-W41.md").write_text("сводка " * 20, encoding="utf-8")
        out = self.run_cli("status", "--days", "1", now=monday_after)[1]
        self.assertIn("2026-W40.md НЕТ", out)
        self.assertNotIn("2026-W41", out)

    def test_status_sees_text_written_by_hand(self):
        self._day("2026-10-04", body="Сегодня писал сам, с телефона.\n")
        out = self.run_cli("status", "--days", "1")[1]
        self.assertIn("записей 0 (но есть текст, записанный руками", out)
        self._day("2026-10-04", body="<!-- machine:photos v1 -->\n📷 3 кадр(ов)\n<!-- /machine:photos -->\n")
        self.assertNotIn("руками", self.run_cli("status", "--days", "1")[1])

    def test_last_week_on_monday_is_the_week_just_ended(self):
        monday, name = diary.last_week(date(2026, 10, 5))
        self.assertEqual((monday, name), (date(2026, 9, 28), "2026-W40.md"))


class FrontmatterListTests(unittest.TestCase):
    DAY = "---\ntype: day\nлюди: []\nместа: []\n---\nтекст\n"

    def test_comma_and_quotes_survive_a_second_edit(self):
        text = diary.add_fm_values(self.DAY, "места", ["Тольятти, парк", 'Кафе "Дом"'])
        text = diary.add_fm_values(text, "места", ["Сочи"])
        self.assertEqual(diary.read_fm_list(text, "места"), ["Тольятти, парк", 'Кафе "Дом"', "Сочи"])
        self.assertIn('места: ["Тольятти, парк", "Кафе \\"Дом\\"", "Сочи"]', text)

    def test_block_list_with_quoted_items(self):
        block = '---\nтип: x\nместа:\n  - "Тольятти, парк"\n  - \'Дом у "Томы"\'\n---\n'
        self.assertEqual(diary.read_fm_list(block, "места"), ["Тольятти, парк", 'Дом у "Томы"'])
        text = diary.add_fm_values(block, "места", ["Кафе: у моря"])
        self.assertEqual(diary.read_fm_list(text, "места"), ["Тольятти, парк", 'Дом у "Томы"', "Кафе: у моря"])

    def test_missing_key_is_added_not_silently_dropped(self):
        text = diary.add_fm_values("---\ntype: day\n---\nтекст\n", "люди", ["Тома"])
        self.assertEqual(diary.read_fm_list(text, "люди"), ["Тома"])
        self.assertTrue(text.endswith("---\nтекст\n"))

    def test_apostrophe_inside_a_name_is_a_letter(self):
        self.assertEqual(diary.read_fm_list("---\nлюди: [O'Brien, Маша]\n---\n", "люди"), ["O'Brien", "Маша"])

    def test_doubled_apostrophe_inside_single_quotes(self):
        text = "---\nместа: ['O''Brien, парк', 'Аня']\n---\n"
        self.assertEqual(diary.read_fm_list(text, "места"), ["O'Brien, парк", "Аня"])
        again = diary.add_fm_values(text, "места", ["Сочи"])
        self.assertEqual(diary.read_fm_list(again, "места"), ["O'Brien, парк", "Аня", "Сочи"])

    def test_backslash_and_quote_inside_double_quotes(self):
        text = diary.add_fm_values("---\nместа: []\n---\n", "места", ['C:\\Фото, "архив"', "it's"])
        self.assertEqual(diary.read_fm_list(text, "места"), ['C:\\Фото, "архив"', "it's"])
        self.assertEqual(diary.read_fm_list(diary.add_fm_values(text, "места", ["ещё"]), "места"),
                         ['C:\\Фото, "архив"', "it's", "ещё"])

    def test_block_items_that_yaml_would_misread_are_quoted(self):
        block = "---\nместа:\n  - Дом\n---\n"
        text = diary.add_fm_values(block, "места", ["null", "*дом", "да: нет", "две\nстроки"])
        self.assertIn('  - "null"\n  - "*дом"\n  - "да: нет"\n  - "две строки"\n', text)
        self.assertEqual(diary.read_fm_list(text, "места"), ["Дом", "null", "*дом", "да: нет", "две строки"])

    def test_old_single_quoted_inline_is_still_read(self):
        self.assertEqual(diary.read_fm_list("---\nлюди: ['Аня', Тома]\n---\n", "люди"), ["Аня", "Тома"])

    def test_list_with_yaml_comment_is_left_as_is(self):
        """Ревью 11.10.2026 (X-C2): комментарий принимался за значение и ломал шапку."""
        for name, body in (
            ("после ключа", "люди: # знакомые\n  - Тома\n"),
            ("после инлайн-списка", 'люди: ["Тома"] # знакомые\n'),
            ("у пункта", "люди:\n  - Тома # подруга\n"),
            ("строкой среди пунктов", "люди:\n  # друзья\n  - Тома\n"),
        ):
            with self.subTest(name):
                text = f"---\ntype: day\n{body}места: []\n---\nтекст\n"
                self.assertEqual(diary.add_fm_values(text, "люди", ["Аня"]), text)
                self.assertTrue(diary.fm_list_unsupported(text, "люди"))

    def test_hash_inside_quotes_is_a_letter_not_a_comment(self):
        text = diary.add_fm_values('---\nместа: ["Кафе #1"]\n---\n', "места", ["Сочи"])
        self.assertEqual(diary.read_fm_list(text, "места"), ["Кафе #1", "Сочи"])
        text = diary.add_fm_values("---\nместа:\n  - 'Дом #2'\n  - Кафе#3\n---\n", "места", ["Сочи"])
        self.assertEqual(diary.read_fm_list(text, "места"), ["Дом #2", "Кафе#3", "Сочи"])


class RepeatAndRecoveryTests(VaultCase):
    def test_taken_label_shows_what_is_already_there(self):
        self.run_cli("add", "--label", "день", stdin="Гуляли у монастыря Ркони.\n")
        rc, out = self.run_cli("add", "--label", "день", stdin="Совсем другой рассказ.\n")
        self.assertEqual(rc, diary.DUPLICATE)
        self.assertIn("«Гуляли у монастыря Ркони.", out)

    def test_same_original_words_under_another_label_are_a_repeat(self):
        stdin = "Пересказ раз.\n=== исходные слова ===\nну мы гуляли\n"
        self.run_cli("add", "--label", "день", stdin=stdin)
        before = self.day_path.read_text(encoding="utf-8")
        again = "Пересказ иначе.\n=== исходные слова ===\nну мы гуляли\n"
        rc, out = self.run_cli("add", "--label", "прогулка", stdin=again)
        self.assertEqual(rc, diary.DUPLICATE)
        self.assertIn("уже записаны под другой меткой", out)
        self.assertEqual(self.day_path.read_text(encoding="utf-8"), before)

    def test_short_original_inside_longer_callout_is_not_a_repeat(self):
        """Выноска «устал» лежит началом более длинной: это другой рассказ."""
        self.run_cli("add", "--label", "день", stdin="Раз.\n=== исходные слова ===\nустал\nлёг спать\n")
        rc, out = self.run_cli("add", "--label", "вечер", stdin="Два.\n=== исходные слова ===\nустал\n")
        self.assertEqual(rc, diary.OK, out)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("Два.\n", text)
        self.assertEqual(text.count("> [!quote]- Исходные слова"), 2)
        # А та же короткая выноска целиком теперь повтор, в том числе в конце файла без перевода строки.
        self.day_path.write_text(text.rstrip("\n"), encoding="utf-8")
        rc, out = self.run_cli("add", "--label", "утро", stdin="Три.\n=== исходные слова ===\nустал\n")
        self.assertEqual(rc, diary.DUPLICATE)

    def test_empty_day_file_gets_skeleton(self):
        self.day_path.write_text("", encoding="utf-8")
        rc, _ = self.run_cli("add", "--label", "день", "--people", "Аня", stdin="Гуляли.\n")
        self.assertEqual(rc, diary.OK)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nschema: diary-day/v1\n"))
        self.assertIn("фото: pending\n", text)
        self.assertEqual(diary.read_fm_list(text, "люди"), ["Аня"])

    def test_day_without_header_is_not_given_one(self):
        """Непустой день, заведённый руками, шапку не получает и не теряет текст."""
        self.day_path.write_text("Писал руками.\n", encoding="utf-8")
        rc, out = self.run_cli("add", "--label", "день", "--people", "Тома", stdin="Пересказ.\n")
        self.assertEqual(rc, diary.OK)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("Писал руками.\n"))
        self.assertIn("нет шапки", out)

    def test_broken_utf8_day_is_named_not_a_date_error(self):
        self.day_path.write_bytes(b"---\n\xff\xfe\n---\n")
        rc, out = self.run_cli("status", "--days", "2")
        self.assertEqual(rc, diary.OK)
        self.assertIn("2026-10-04 вс: файл дня не читается", out)
        self.assertIn("2026-10-03 сб: файла дня нет", out)
        rc, out = self.run_cli("add", "--label", "день", stdin="Текст.\n")
        self.assertEqual(rc, diary.ERROR)
        self.assertIn("запись не удалась", out)
        self.assertNotIn("не понял дату", out)

    def test_torn_marker_line_is_a_repeat_not_a_date_error(self):
        """Ревью 11.10.2026 (C41): день обрывается строкой метки без перевода строки."""
        self.day_path.write_text(
            diary.build_day_skeleton(DAY)
            + "\n###### 20:40\n<!-- src:domovoy:2026-10-04:день sha:1 kind:retold logged:2026-10-04T20:40 -->",
            encoding="utf-8",
        )
        rc, out = self.run_cli("add", "--label", "день", stdin="Гуляли.\n")
        self.assertEqual(rc, diary.DUPLICATE, out)
        self.assertNotIn("не понял дату", out)

    def test_only_a_bad_date_is_called_a_date_error(self):
        """Ревью 11.10.2026 (C41): посторонний ValueError это «запись не удалась»."""
        for argv, stdin in (
            (("add", "--label", "день", "--text-file", "текст\0.md"), ""),   # NUL в имени файла
            (("fact", "Оплатил \udc80 счёт"), ""),                             # одиночный суррогат
        ):
            with self.subTest(argv[0]):
                rc, out = self.run_cli(*argv, stdin=stdin)
                self.assertEqual(rc, diary.ERROR)
                self.assertIn("запись не удалась", out)
                self.assertNotIn("не понял дату", out)
        for argv in (("add", "--day", "2026-13-01", "--label", "день"), ("rollup", "--week", "2026-W60"),
                     ("sessions", "--day", "вчера")):
            with self.subTest(argv[0]):
                rc, out = self.run_cli(*argv, stdin="Текст.\n")
                self.assertEqual(rc, diary.ERROR)
                self.assertIn("не понял дату", out)

    def test_mark_on_day_without_header_is_an_error(self):
        self.day_path.write_text("Писал руками, без шапки.\n", encoding="utf-8")
        rc, out = self.run_cli("mark", "--people", "Тома")
        self.assertEqual(rc, diary.ERROR)
        self.assertIn("нет шапки", out)

    def test_stale_tmp_is_swept_and_fresh_one_left(self):
        folder = self.day_path.parent
        stale = folder / "2026-10-01.md.abc.tmp"
        fresh = folder / "2026-10-02.md.def.tmp"
        stale.write_text("остаток", encoding="utf-8")
        fresh.write_text("идёт запись", encoding="utf-8")
        old = datetime(2026, 10, 1, tzinfo=TZ).timestamp()
        os.utime(stale, (old, old))
        self.run_cli("add", "--label", "день", stdin="Текст.\n")
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_real_write_error_is_not_swallowed(self):
        import errno
        with patch.object(diary.os, "fsync", side_effect=OSError(errno.EIO, "диск")):
            rc, out = self.run_cli("add", "--label", "день", stdin="Текст.\n")
        self.assertEqual(rc, diary.ERROR)
        self.assertFalse(self.day_path.exists())
        self.assertEqual(list(self.day_path.parent.iterdir()), [])
        with patch.object(diary.os, "fsync", side_effect=OSError(errno.EINVAL, "не умею")):
            self.assertEqual(self.run_cli("add", "--label", "день", stdin="Текст.\n")[0], diary.OK)


class RollupTests(VaultCase):
    def test_rollup_gets_header_and_is_not_overwritten(self):
        rc, _ = self.run_cli("rollup", "--week", "2026-W40", stdin="28.09: приехали.\n\nГлавное: дорога.\n")
        self.assertEqual(rc, diary.OK)
        text = (self.vault / "Дневник" / "_Роллапы" / "2026-W40.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nschema: diary-rollup/v1\ntype: rollup\nweek: 2026-W40\nstart: 2026-09-28\nend: 2026-10-04\n"))
        self.assertIn("# Неделя 28.09 - 04.10.2026\n\n28.09: приехали.", text)
        rc, out = self.run_cli("rollup", "--week", "2026-W40", stdin="другая сводка\n")
        self.assertEqual(rc, diary.DUPLICATE)
        self.assertIn("Главное: дорога.", (self.vault / "Дневник" / "_Роллапы" / "2026-W40.md").read_text(encoding="utf-8"))

    def test_summary_that_appeared_meanwhile_is_not_overwritten(self):
        """Между проверкой готовности и записью сводку положил другой запуск."""
        rollups = self.vault / "Дневник" / "_Роллапы"
        rollups.mkdir()
        (rollups / "2026-W40.md").write_text("сводка другого запуска " * 5, encoding="utf-8")
        with patch.object(diary, "rollup_done", return_value=False):
            rc, out = self.run_cli("rollup", "--week", "2026-W40", stdin="моя сводка\n")
        self.assertEqual(rc, diary.DUPLICATE)
        self.assertIn("сводка другого запуска", (rollups / "2026-W40.md").read_text(encoding="utf-8"))

    def test_torn_file_is_replaced_and_bad_week_refused(self):
        rollups = self.vault / "Дневник" / "_Роллапы"
        rollups.mkdir()
        (rollups / "2026-W40.md").write_text("", encoding="utf-8")
        self.assertEqual(self.run_cli("rollup", "--week", "2026-W40", stdin="настоящая сводка недели\n")[0], diary.OK)
        self.assertIn("настоящая", (rollups / "2026-W40.md").read_text(encoding="utf-8"))
        self.assertEqual(self.run_cli("rollup", "--week", "неделя-40", stdin="x\n")[0], diary.ERROR)


class CommitTests(VaultCase):
    def _git(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(self.vault), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def _init(self) -> None:
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.org")
        self._git("config", "user.name", "T")
        self._git("config", "commit.gpgsign", "false")
        (self.vault / "Заметка.md").write_text("чужое\n", encoding="utf-8")
        self._git("add", "Заметка.md")
        self._git("commit", "-q", "-m", "начало")

    def test_no_git_is_not_an_error(self):
        rc, out = self.run_cli("add", "--label", "день", stdin="Текст.\n")
        self.assertEqual(rc, diary.OK)
        self.assertIn("git в хранилище нет", out)

    def test_commits_diary_and_leaves_foreign_changes_alone(self):
        self._init()
        (self.vault / "Заметка.md").write_text("чужое, правка Ивана\n", encoding="utf-8")
        (self.vault / "Staged.md").write_text("уже в индексе\n", encoding="utf-8")
        self._git("add", "Staged.md")
        rc, out = self.run_cli("add", "--label", "день", stdin="Текст.\n")
        self.assertEqual(rc, diary.OK)
        self.assertIn("git: закоммичено", out)
        committed = self._git("show", "--name-only", "--format=%s", "HEAD")
        self.assertIn("дневник: 2026-10-04 день", committed)
        self.assertIn("2026-10-04.md", committed)
        self.assertNotIn("Заметка.md", committed)
        self.assertNotIn("Staged.md", committed)
        status = self._git("status", "--porcelain")
        self.assertIn(" M ", status)        # правка Ивана так и не закоммичена
        self.assertIn("A  Staged.md", status)  # и его индекс не тронут

    def test_nothing_to_commit(self):
        self._init()
        self.assertIn("нечего коммитить", self.run_cli("commit")[1])

    def test_failed_commit_is_visible_and_repeat_finishes_it(self):
        self._init()
        lock = self.vault / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        rc, out = self.run_cli("add", "--label", "день", stdin="Текст.\n")
        self.assertEqual(rc, diary.OK)             # запись сохранена
        self.assertIn("не прошёл", out)             # но коммит нет, и это сказано
        self.assertIn("index.lock", self.run_cli("status", "--days", "1")[1])
        lock.unlink()
        self.assertIn("незакоммиченные изменения", self.run_cli("status", "--days", "1")[1])
        rc, out = self.run_cli("add", "--label", "день", stdin="Текст.\n")   # повтор после сбоя
        self.assertEqual(rc, diary.DUPLICATE)
        self.assertIn("git: закоммичено", out)
        self.assertNotIn("git:", self.run_cli("status", "--days", "1")[1])

    def test_any_repeated_write_command_finishes_a_torn_commit(self):
        """После сбоя коммита доделать его должен любой следующий вызов, даже повтор."""
        self._init()
        lock = self.vault / ".git" / "index.lock"
        for argv, stdin in (
            (("fact", "Вернули долг"), ""),
            (("mark", "--places", "Тольятти"), ""),
            (("rollup", "--week", "2026-W40"), "сводка недели, достаточно длинная для готовой\n"),
        ):
            if argv[0] == "mark":
                self.run_cli("add", "--label", "день", stdin="Текст.\n")
            lock.write_text("", encoding="utf-8")
            self.assertIn("не прошёл", self.run_cli(*argv, stdin=stdin)[1])
            lock.unlink()
            out = self.run_cli(*argv, stdin=stdin)[1]       # тот же вызов ещё раз: писать нечего
            self.assertIn("git: закоммичено", out, argv)
            self.assertNotIn("git:", self.run_cli("status", "--days", "1")[1])

    def test_leftover_tmp_never_reaches_history(self):
        self._init()
        (self.day_path.parent / "2026-10-02.md.x1.tmp").write_text("идёт запись", encoding="utf-8")
        self.run_cli("add", "--label", "день", stdin="Текст.\n")
        self.assertNotIn(".tmp", self._git("show", "--name-only", "--format=", "HEAD"))

    def test_tmp_already_in_index_is_still_not_committed(self):
        self._init()
        (self.vault / "Дневник" / "2026" / "2026-10-02.md.x1.tmp").write_text("остаток", encoding="utf-8")
        self._git("add", "-f", "--", "Дневник")
        self.run_cli("add", "--label", "день", stdin="Текст.\n")
        self.assertNotIn(".tmp", self._git("show", "--name-only", "--format=", "HEAD"))

    def test_folder_unknown_to_git_does_not_break_commit(self):
        """Ревью 11.10.2026 (C40): каталог превью есть, а git о нём ничего не знает."""
        photos = self.vault / "_attachments" / "дневник"
        for name, ignore, file in (
            ("пустой каталог", "", ""),
            ("только .tmp", "", "2026-10-04_a.jpg.x1.tmp"),
            ("*.jpg в .gitignore", "*.jpg\n", "2026-10-04_a.jpg"),
            ("_attachments/ в .gitignore", "_attachments/\n", "2026-10-04_a.jpg"),
        ):
            with self.subTest(name):
                shutil.rmtree(self.vault / ".git", ignore_errors=True)
                shutil.rmtree(self.vault / "_attachments", ignore_errors=True)
                (self.vault / ".gitignore").write_text(ignore, encoding="utf-8")
                self._init()
                photos.mkdir(parents=True)
                if file:
                    (photos / file).write_bytes(b"jpg")
                self.day_path.write_text(f"{name}\n", encoding="utf-8")
                self.assertEqual(diary.commit(self.vault, "дневник: проба"), "закоммичено")
                self.assertIn("2026-10-04.md", self._git("show", "--name-only", "--format=", "HEAD"))

    def test_ignored_folder_with_tracked_files_is_still_committed(self):
        """Ревью 11.10.2026 (C40): каталог попал в .gitignore, когда файлы в нём уже были в git."""
        self._init()
        photo = self.vault / "_attachments" / "дневник" / "2026-10-04_a.jpg"
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b"jpg")
        self._git("add", "_attachments")
        self._git("commit", "-q", "-m", "фото")
        (self.vault / ".gitignore").write_text("_attachments/\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-q", "-m", "игнор")
        photo.write_bytes(b"jpg2")
        self.day_path.write_text("день\n", encoding="utf-8")
        self.assertEqual(diary.commit(self.vault, "дневник: проба"), "закоммичено")
        self.assertEqual(self._git("status", "--porcelain", "--", "_attachments", "Дневник"), "")

    def test_folder_deleted_whole_is_committed_and_counted_as_debt(self):
        """Ревью 11.10.2026 (C40): удаления каталога целиком идут в коммит и в долг status."""
        self._init()
        (self.vault / "Люди").mkdir()
        (self.vault / "Люди" / "Тома.md").write_text("карточка\n", encoding="utf-8")
        self._git("add", "Люди")
        self._git("commit", "-q", "-m", "люди")
        shutil.rmtree(self.vault / "Люди")
        self.assertIn("git: есть незакоммиченные", self.run_cli("status", "--days", "1")[1])
        rc, out = self.run_cli("commit")
        self.assertEqual(rc, diary.OK)
        self.assertIn("git: закоммичено", out)
        self.assertIn("Люди/Тома.md", self._git("-c", "core.quotePath=false", "show", "--name-only", "--format=", "HEAD"))
        self.assertNotIn("git:", self.run_cli("status", "--days", "1")[1])

    def test_staged_move_out_of_a_deleted_folder_is_committed_whole(self):
        """Ревью 11.10.2026 (C40): git mv из «Люди» в «Дневник», «Люди» исчез с диска;
        обе стороны переименования идут в коммит, в индексе ничего не остаётся."""
        self._init()
        (self.vault / "Люди").mkdir()
        (self.vault / "Люди" / "Тома.md").write_text("карточка\n", encoding="utf-8")
        self._git("add", "Люди")
        self._git("commit", "-q", "-m", "люди")
        self._git("mv", "Люди/Тома.md", "Дневник/Тома.md")
        shutil.rmtree(self.vault / "Люди", ignore_errors=True)
        self.assertEqual(diary.commit(self.vault, "дневник: перенос"), "закоммичено")
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_week_review_file_inside_diary_folder_is_committed(self):
        """26.10.2026: подробности разбора недели лежат в «Дневник/_Недели Домового»
        и идут в коммит вместе с дневником, отдельного пути в COMMIT_PATHS нет."""
        self._init()
        week = self.vault / "Дневник" / "_Недели Домового" / "2026-W43.md"
        week.parent.mkdir(parents=True, exist_ok=True)
        week.write_text("разбор\n", encoding="utf-8")
        self.assertEqual(diary.commit(self.vault, "Домовой: разбор недели 2026-W43"), "закоммичено")
        self.assertIn("Дневник/_Недели Домового/2026-W43.md",
                      self._git("-c", "core.quotePath=false", "show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_failed_commit_command_exits_with_error(self):
        """Ревью 11.10.2026 (C39): команда commit при сбое отвечала 0."""
        self._init()
        self.day_path.write_text("день\n", encoding="utf-8")
        lock = self.vault / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        rc, out = self.run_cli("commit")
        self.assertEqual(rc, diary.ERROR)
        self.assertIn("не прошёл", out)
        lock.unlink()
        self.assertEqual(self.run_cli("commit")[0], diary.OK)
        self.assertEqual(self.run_cli("commit")[0], diary.OK)      # нечего коммитить тоже не ошибка

    def test_git_timeout_does_not_leave_index_lock(self):
        """Ревью 11.10.2026 (C39): медленный коммит (здесь хук) упирается в таймаут;
        git получает SIGTERM и сам убирает index.lock, а не умирает от SIGKILL с ним."""
        self._init()
        hook = self.vault / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\nsleep 3\n", encoding="utf-8")
        hook.chmod(0o755)
        self._git("config", "core.hooksPath", str(hook.parent))   # общий hooksPath машины не в счёт
        self.day_path.write_text("день\n", encoding="utf-8")
        with patch.object(diary, "GIT_TIMEOUT", 1):
            self.assertEqual(diary.commit(self.vault, "дневник: проба"), "git недоступен: TimeoutExpired")
        self.assertFalse((self.vault / ".git" / "index.lock").exists())
        hook.unlink()
        self.assertEqual(diary.commit(self.vault, "дневник: проба"), "закоммичено")

    def _hold_git_lock_while_git_runs(self) -> threading.Thread:
        """Свой git (коммит фото) идёт: держит замок git и .git/index.lock, через 0,3 с кончает."""
        index_lock = self.vault / ".git" / "index.lock"
        held = open(diary.LOCK_FILE.with_name("diary-git.lock"), "a")
        fcntl.flock(held, fcntl.LOCK_EX)
        index_lock.write_text("", encoding="utf-8")

        def finish():
            time.sleep(0.3)
            index_lock.unlink()
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()

        t = threading.Thread(target=finish)
        t.start()
        return t

    def test_commit_and_status_wait_for_running_git(self):
        """Ревью 11.10.2026 (C39): коммиты дневника и фото и проверка status идут
        по очереди под замком git, а не наперегонки за index.lock."""
        self._init()
        self.day_path.write_text("день\n", encoding="utf-8")
        t = self._hold_git_lock_while_git_runs()
        try:
            self.assertEqual(diary.commit(self.vault, "дневник: проба"), "закоммичено")
        finally:
            t.join()
        t = self._hold_git_lock_while_git_runs()
        try:
            self.assertEqual(diary.git_debt(self.vault), "")
        finally:
            t.join()

    def test_git_check_failure_is_not_no_debt(self):
        """Ревью 11.10.2026 (C39, смежная): сбой или таймаут git status не значит «долга нет»."""
        self._init()
        index = self.vault / ".git" / "index"
        good = index.read_bytes()
        index.write_bytes(b"not an index")
        self.assertIn("git не проверен", diary.git_debt(self.vault))
        index.write_bytes(good)
        with tempfile.TemporaryDirectory() as fake:      # git, который зависает
            (Path(fake) / "git").write_text("#!/bin/sh\nexec /bin/sleep 5\n", encoding="utf-8")
            (Path(fake) / "git").chmod(0o755)
            with patch.dict(os.environ, {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}), \
                    patch.object(diary, "GIT_TIMEOUT", 0.5):
                self.assertIn("git не проверен", diary.git_debt(self.vault))


if __name__ == "__main__":
    unittest.main()
