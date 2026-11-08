"""Тесты синхронизации на настоящих git-репозиториях: две машины и общий удалённый."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domovoy import sync  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def g(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class SyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for p in (patch.dict(os.environ, GIT_ENV),
                  patch.object(sync, "STATE_DIR", root / "state"),
                  patch.object(sync, "read_target", return_value=(183, None))):
            p.start()
            self.addCleanup(p.stop)
        post = patch.object(sync, "post_event", return_value=(True, "ok"))
        self.post = post.start()
        self.addCleanup(post.stop)

        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.a, self.b = root / "a", root / "b"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.a)], check=True, capture_output=True)
        g(self.a, "checkout", "-q", "-b", "main")
        write(self.a, "prompt.md", "правила\n")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: x\n")
        write(self.a, "domovoy/code.py", "x = 1\n")
        g(self.a, "add", "-A")
        g(self.a, "commit", "-q", "-m", "начало")
        g(self.a, "push", "-q", "origin", "main")
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.b)], check=True, capture_output=True)

    def remote_file(self, rel: str) -> str:
        return g(self.remote, "show", f"main:{rel}")

    def test_data_committed_and_pushed_code_left_alone(self):
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: x\nСделано: x\n")
        write(self.a, "data/table.md", "| компания |\n")
        write(self.a, "domovoy/code.py", "x = 2\n")
        self.assertEqual(sync.run(self.a), sync.OK)
        self.assertIn("Сделано: x", self.remote_file("journal/2026-09.md"))
        self.assertIn("компания", self.remote_file("data/table.md"))
        self.assertIn("x = 1", self.remote_file("domovoy/code.py"))
        self.assertIn("M domovoy/code.py", g(self.a, "status", "--porcelain"))

    def test_foreign_staged_file_not_committed(self):
        write(self.a, "domovoy/code.py", "x = 3\n")
        g(self.a, "add", "domovoy/code.py")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: y\n")
        self.assertEqual(sync.run(self.a), sync.OK)
        self.assertIn("x = 1", self.remote_file("domovoy/code.py"))
        self.assertIn("M  domovoy/code.py", g(self.a, "status", "--porcelain"))

    def test_pulls_other_machine_and_pushes_own(self):
        write(self.b, "prompt.md", "правила v2\n")
        g(self.b, "commit", "-qam", "промт с другой машины")
        g(self.b, "push", "-q", "origin", "main")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: x\nСилы: 3/5\n")
        self.assertEqual(sync.run(self.a), sync.OK)
        self.assertEqual((self.a / "prompt.md").read_text(encoding="utf-8"), "правила v2\n")
        self.assertIn("Силы: 3/5", self.remote_file("journal/2026-09.md"))

    def test_conflict_rolls_back_and_notifies_once(self):
        write(self.b, "journal/2026-09.md", "## 2026-09-21\nПлан: из b\n")
        g(self.b, "commit", "-qam", "b")
        g(self.b, "push", "-q", "origin", "main")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: из a\n")
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.assertFalse((self.a / ".git" / "rebase-merge").exists())
        self.assertIn("из a", g(self.a, "show", "HEAD:journal/2026-09.md"))
        self.assertIn("из b", self.remote_file("journal/2026-09.md"))
        self.post.assert_called_once()
        payload = self.post.call_args.args[2]
        self.assertFalse(payload["wake"])
        self.assertIn("journal/2026-09.md", payload["text"])
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.post.assert_called_once()

    def test_autostash_conflict_markers_never_pushed(self):
        """Агент пишет журнал между коммитом данных и rebase, а на GitHub те же
        строки уже правлены: rebase с autostash возвращает 0 и оставляет
        маркеры. Они не должны уйти в коммит ни в этом прогоне, ни в следующем."""
        write(self.b, "journal/2026-09.md", "## 2026-09-21\nПлан: из b\n")
        g(self.b, "commit", "-qam", "b")
        g(self.b, "push", "-q", "origin", "main")
        write(self.a, "data/table.md", "| x |\n")
        commit_data = sync.commit_data

        def commit_then_agent_writes(repo):
            out = commit_data(repo)
            write(repo, "journal/2026-09.md", "## 2026-09-21\nПлан: из a, после коммита\n")
            return out

        with patch.object(sync, "commit_data", side_effect=commit_then_agent_writes):
            self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.assertNotIn("<<<<<<<", self.remote_file("journal/2026-09.md"))
        self.assertNotIn("<<<<<<<", g(self.a, "show", "HEAD:journal/2026-09.md"))
        self.post.assert_called_once()
        self.assertIn("journal/2026-09.md", self.post.call_args.args[2]["text"])

    def test_conflict_with_new_local_commits_notifies_once(self):
        write(self.b, "journal/2026-09.md", "## 2026-09-21\nПлан: из b\n")
        g(self.b, "commit", "-qam", "b")
        g(self.b, "push", "-q", "origin", "main")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: из a\n")
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        # Пока конфликт не сведён, агент продолжает писать: каждый прогон
        # добавляет локальный коммит, а сообщение должно быть одно.
        write(self.a, "data/table.md", "| новая строка |\n")
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.assertIn("новая строка", g(self.a, "show", "HEAD:data/table.md"))
        self.post.assert_called_once()

    def test_non_conflict_rebase_failure_does_not_call_human(self):
        write(self.b, "notes.txt", "с другой машины\n")
        g(self.b, "add", "notes.txt")
        g(self.b, "commit", "-qm", "заметка")
        g(self.b, "push", "-q", "origin", "main")
        write(self.a, "notes.txt", "локальный неотслеживаемый\n")
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.assertFalse((self.a / ".git" / "rebase-merge").exists())
        self.post.assert_not_called()

    def test_rebase_killed_by_timeout_is_aborted(self):
        write(self.b, "journal/2026-09.md", "## 2026-09-21\nПлан: x\nс другой машины\n")
        g(self.b, "commit", "-qam", "b")
        g(self.b, "push", "-q", "origin", "main")
        write(self.a, "data/table.md", "| строка |\n")
        real = sync.git

        def git_with_timeout(repo, *args, check=True):
            if args[:1] == ("rebase",) and "--abort" not in args:
                # Rebase встал посреди работы, и тут его убил таймаут.
                real(repo, "rebase", "-q", "--exec", "false", "origin/main", check=False)
                raise sync.GitError("таймаут 60 с (git rebase)")
            return real(repo, *args, check=check)

        with patch.object(sync, "git", side_effect=git_with_timeout):
            self.assertEqual(sync.run(self.a), sync.ERROR)
        self.assertFalse((self.a / ".git" / "rebase-merge").exists())
        self.assertIn("строка", g(self.a, "show", "HEAD:data/table.md"))
        self.assertEqual(sync.run(self.a), sync.OK)
        self.post.assert_not_called()

    def test_stuck_rebase_on_entry_is_reported_once(self):
        (self.a / ".git" / "rebase-merge").mkdir()
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        self.assertIn("rebase --abort", self.post.call_args.args[2]["text"])
        self.assertFalse(self.post.call_args.args[2]["wake"])
        self.assertEqual(sync.run(self.a, dry_run=True), sync.ERROR)
        self.post.assert_called_once()

    def test_busy_lock_without_wait_skips_at_once(self):
        held = sync._lock(0)
        self.addCleanup(held.close)
        write(self.a, "journal/2026-09.md", "правка\n")
        self.assertEqual(sync.run(self.a, lock_wait=0), sync.ERROR)
        self.assertIn("journal/2026-09.md", g(self.a, "status", "--porcelain"))

    def test_other_branch_untouched(self):
        g(self.a, "checkout", "-q", "-b", "опыт")
        write(self.a, "journal/2026-09.md", "правка\n")
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.assertIn("journal/2026-09.md", g(self.a, "status", "--porcelain"))

    def test_nothing_to_do(self):
        before = g(self.a, "rev-parse", "HEAD")
        self.assertEqual(sync.run(self.a), sync.OK)
        self.assertEqual(g(self.a, "rev-parse", "HEAD"), before)

    def test_offline_keeps_local_commit(self):
        g(self.a, "remote", "set-url", "origin", str(self.remote.parent / "нет.git"))
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nОтчёт\n")
        self.assertEqual(sync.run(self.a), sync.OFFLINE)
        self.assertEqual(g(self.a, "status", "--porcelain"), "")
        self.assertIn("Отчёт", g(self.a, "show", "HEAD:journal/2026-09.md"))

    def test_offline_streak_notifies_once_and_success_resets(self):
        good = str(self.remote)
        g(self.a, "remote", "set-url", "origin", str(self.remote.parent / "нет.git"))
        for _ in range(sync.FAIL_NOTIFY_AFTER - 1):
            self.assertEqual(sync.run(self.a), sync.OFFLINE)
        self.post.assert_not_called()
        self.assertEqual(sync.run(self.a), sync.OFFLINE)
        self.post.assert_called_once()
        payload = self.post.call_args.args[2]
        self.assertFalse(payload["wake"])
        self.assertIn(f"{sync.FAIL_NOTIFY_AFTER} раз подряд", payload["text"])
        self.assertIn("GitHub недоступен", payload["text"])
        sync.run(self.a)                                    # та же ошибка: не повторяется
        self.post.assert_called_once()
        g(self.a, "remote", "set-url", "origin", good)
        self.assertEqual(sync.run(self.a), sync.OK)
        self.assertFalse((sync.STATE_DIR / "failures.json").exists())
        # Новая полоса сбоев снова доходит до Ивана.
        g(self.a, "remote", "set-url", "origin", str(self.remote.parent / "нет.git"))
        for _ in range(sync.FAIL_NOTIFY_AFTER):
            sync.run(self.a)
        self.assertEqual(self.post.call_count, 2)

    def test_other_branch_streak_is_counted(self):
        g(self.a, "checkout", "-q", "-b", "опыт")
        for _ in range(sync.FAIL_NOTIFY_AFTER):
            self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        self.assertIn("ветка опыт", self.post.call_args.args[2]["text"])

    def test_dry_run_is_not_counted(self):
        for _ in range(sync.FAIL_NOTIFY_AFTER + 1):
            sync.run(self.a, dry_run=True)
        self.assertFalse((sync.STATE_DIR / "failures.json").exists())

    def test_dry_run_changes_nothing(self):
        before = g(self.a, "rev-parse", "HEAD")
        write(self.a, "journal/2026-09.md", "правка\n")
        self.assertEqual(sync.run(self.a, dry_run=True), sync.OK)
        self.assertEqual(g(self.a, "rev-parse", "HEAD"), before)
        self.assertIn("journal/2026-09.md", g(self.a, "status", "--porcelain"))

    # --- ревью 11.10.2026 ---

    def push_from_b(self, rel: str, text: str) -> None:
        write(self.b, rel, text)
        g(self.b, "add", "-A")
        g(self.b, "commit", "-qm", "b")
        g(self.b, "push", "-q", "origin", "main")

    def autostash_conflict(self) -> None:
        """Конфликт autostash в журнале, как в test_autostash_conflict_markers_never_pushed."""
        self.push_from_b("journal/2026-09.md", "## 2026-09-21\nПлан: из b\n")
        write(self.a, "data/table.md", "| x |\n")
        commit_data = sync.commit_data

        def commit_then_agent_writes(repo):
            out = commit_data(repo)
            write(repo, "journal/2026-09.md", "## 2026-09-21\nПлан: из a, после коммита\n")
            return out

        with patch.object(sync, "commit_data", side_effect=commit_then_agent_writes):
            self.assertEqual(sync.run(self.a), sync.CONFLICT)

    def real_stuck_rebase(self) -> None:
        """Настоящий незаконченный rebase: HEAD отсоединён, лежит rebase-merge."""
        self.push_from_b("prompt.md", "правила v2\n")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: x\nУтро\n")
        g(self.a, "commit", "-qam", "журнал ПК")
        g(self.a, "fetch", "-q", "origin")
        subprocess.run(["git", "-C", str(self.a), "rebase", "-q", "-x", "false", "origin/main"],
                       capture_output=True)
        self.assertTrue((self.a / ".git" / "rebase-merge").exists())
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.a), "symbolic-ref", "-q", "HEAD"],
                                           capture_output=True).returncode, 0)

    def test_dirty_code_with_incoming_commits_waits_without_rebase(self):
        """В5 (C44): недописанная правка кода на ПК, с GitHub пришла правка того
        же файла. Rebase с autostash оставил бы маркеры в исполняемом коде:
        синхронизация rebase не делает, один раз пишет и ждёт."""
        self.push_from_b("domovoy/code.py", "x = 2\n")
        write(self.a, "domovoy/code.py", "x = 3  # недописано\n")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: x\nСилы: 3/5\n")
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.assertEqual((self.a / "domovoy/code.py").read_text(encoding="utf-8"), "x = 3  # недописано\n")
        self.assertEqual(g(self.a, "stash", "list"), "")
        self.assertIn("Силы: 3/5", g(self.a, "show", "HEAD:journal/2026-09.md"))
        self.post.assert_called_once()
        payload = self.post.call_args.args[2]
        self.assertFalse(payload["wake"])
        self.assertIn("domovoy/code.py", payload["text"])
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        # Правку убрали: синхронизация идёт сама.
        g(self.a, "checkout", "--", "domovoy/code.py")
        self.assertEqual(sync.run(self.a), sync.OK)
        self.assertIn("Силы: 3/5", self.remote_file("journal/2026-09.md"))
        self.assertEqual((self.a / "domovoy/code.py").read_text(encoding="utf-8"), "x = 2\n")

    def test_partly_staged_code_keeps_index_when_behind(self):
        """X-E2: часть правки кода в индексе, с GitHub пришёл коммит журнала.
        Autostash вернул бы правку без разделения на подготовленное и нет."""
        self.push_from_b("journal/2026-09.md", "## 2026-09-21\nПлан: x\nс мака\n")
        write(self.a, "domovoy/code.py", "x = 2\n")
        g(self.a, "add", "domovoy/code.py")
        write(self.a, "domovoy/code.py", "x = 2\ny = 2\n")
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.assertIn("MM domovoy/code.py", g(self.a, "status", "--porcelain"))
        self.assertEqual(g(self.a, "show", ":domovoy/code.py"), "x = 2\n")

    def test_dirty_code_waiting_notifies_once_per_episode(self):
        """Ревью 11.10.2026: Иван пишет код на ПК, набор тронутых файлов
        растёт, а ожидание одно: сообщение одно, список файлов только в
        тексте. После удачной синхронизации новое ожидание снова сообщается."""
        write(self.a, "domovoy/other.py", "y = 1\n")
        g(self.a, "add", "-A")
        g(self.a, "commit", "-qm", "второй файл кода")
        self.push_from_b("journal/2026-09.md", "## 2026-09-21\nПлан: x\nс мака\n")
        write(self.a, "domovoy/code.py", "x = 3\n")
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        self.assertIn("domovoy/code.py", self.post.call_args.args[2]["text"])
        write(self.a, "domovoy/other.py", "y = 2\n")
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        g(self.a, "checkout", "--", "domovoy")
        self.assertEqual(sync.run(self.a), sync.OK)
        g(self.b, "pull", "-q", "--rebase", "origin", "main")
        self.push_from_b("journal/2026-09.md", "## 2026-09-21\nПлан: x\nс мака, ещё\n")
        write(self.a, "domovoy/other.py", "y = 3\n")
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.assertEqual(self.post.call_count, 2)
        self.assertIn("domovoy/other.py", self.post.call_args.args[2]["text"])

    def test_markers_left_after_git_add_are_not_committed(self):
        """X-E3: после конфликта autostash кто-то сделал `git add`, не убрав
        маркеры. Пометки конфликта больше нет, но маркеры на GitHub не уходят.
        Об этом отдельное сообщение со своим ключом: прежнее про конфликт
        советует git status, а он теперь показывает обычную правку (ревью
        11.10.2026)."""
        self.autostash_conflict()
        self.post.assert_called_once()
        g(self.a, "add", "prompt.md", "journal", "data")
        self.assertEqual(sync.unmerged_paths(self.a), [])
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.assertNotIn("<<<<<<<", self.remote_file("journal/2026-09.md"))
        self.assertNotIn("<<<<<<<", g(self.a, "show", "HEAD:journal/2026-09.md"))
        self.assertEqual(self.post.call_count, 2)
        text = self.post.call_args.args[2]["text"]
        self.assertIn("journal/2026-09.md", text)
        self.assertIn("git add", text)
        self.assertNotIn("git status", text)
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.assertEqual(self.post.call_count, 2)

    def test_markers_in_last_local_commit_are_not_pushed(self):
        """X-E3 (ревью 11.10.2026): маркеры уже закоммичены руками (git add и
        git commit или rebase --continue), дерево чистое. Такой коммит не
        отправляется, сообщение одно."""
        before = g(self.remote, "rev-parse", "main")
        write(self.a, "journal/2026-09.md",
              "## 2026-09-21\n<<<<<<< HEAD\nПлан: из b\n=======\nПлан: из a\n>>>>>>> журнал ПК\n")
        g(self.a, "commit", "-qam", "сведено руками")
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.assertEqual(g(self.remote, "rev-parse", "main"), before)
        self.post.assert_called_once()
        text = self.post.call_args.args[2]["text"]
        self.assertIn("journal/2026-09.md", text)
        self.assertIn("коммит", text)
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.post.assert_called_once()

    def test_markers_in_intermediate_commit_are_not_pushed(self):
        """X-E3 (ревью 11.10.2026): следующий коммит маркеры убрал, итоговое
        дерево чистое, но промежуточный коммит с ними ушёл бы на GitHub."""
        before = g(self.remote, "rev-parse", "main")
        write(self.a, "data/table.md", "| x |\n<<<<<<< Updated upstream\n| y |\n>>>>>>> Stashed changes\n")
        g(self.a, "add", "-A")
        g(self.a, "commit", "-qm", "с маркерами")
        write(self.a, "data/table.md", "| x |\n| y |\n")
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.assertNotIn("<<<<<<<", g(self.a, "show", "HEAD:data/table.md"))
        self.assertEqual(g(self.remote, "rev-parse", "main"), before)
        self.post.assert_called_once()
        self.assertIn("data/table.md", self.post.call_args.args[2]["text"])

    def test_markers_in_merged_side_branch_are_not_pushed(self):
        """Ревью 11.10.2026 (второй круг, 3): боковая ветка добавила маркеры и
        убрала их, merge с чистым итогом. Её коммиты проверяются и не уходят;
        на git 2.55 тест проходит и без --full-history, флаг страховка от
        упрощения истории с путями (ревью 11.10.2026, третий круг)."""
        before = g(self.remote, "rev-parse", "main")
        g(self.a, "checkout", "-q", "-b", "side")
        write(self.a, "data/table.md", "| x |\n<<<<<<< HEAD\n| y |\n>>>>>>> b\n")
        g(self.a, "add", "-A")
        g(self.a, "commit", "-qm", "с маркерами")
        g(self.a, "rm", "-q", "data/table.md")
        g(self.a, "commit", "-qm", "убрал")
        g(self.a, "checkout", "-q", "main")
        g(self.a, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        self.assertEqual(g(self.remote, "rev-parse", "main"), before)
        self.assertIn("data/table.md", self.post.call_args.args[2]["text"])

    def test_queued_watch_line_goes_out_with_next_sync(self):
        """Ревью 11.10.2026 (второй круг, 1): строка сторожа, не дождавшаяся
        замка, уходит в журнал и на GitHub следующим прогоном синхронизации, а
        не только следующей строкой сторожа (тихие прогоны её не переносят)."""
        from datetime import datetime

        from domovoy import journal
        from domovoy.common import TZ
        with patch.object(sync, "DATA_LOCK_WAIT_SEC", 0):
            held = sync._lock(0)
            try:
                journal.append_or_raise("Сторож", ["Почта: S"], datetime(2026, 9, 21, 9, 10, tzinfo=TZ),
                                        self.a / "journal")
            finally:
                held.close()
        self.assertNotIn("Почта: S", (self.a / "journal" / "2026-09.md").read_text(encoding="utf-8"))
        self.assertEqual(sync.run(self.a), sync.OK)
        self.assertIn("Почта: S", self.remote_file("journal/2026-09.md"))
        self.assertFalse(journal.QUEUE_FILE.exists())

    def test_wake_with_busy_lock_gets_conflict_warning(self):
        """Ревью 11.10.2026 (второй круг, 4): пробуждение не дождалось замка, а
        прогон, который его держал, кончился обычным конфликтом с откатом
        rebase. Дерево чистое, но предупреждение агенту нужно."""
        from functools import partial

        from domovoy import wake
        write(self.b, "journal/2026-09.md", "## 2026-09-21\nПлан: из b\n")
        g(self.b, "commit", "-qam", "b")
        g(self.b, "push", "-q", "origin", "main")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: из a\n")
        self.assertEqual(sync.run(self.a), sync.CONFLICT)
        with patch.object(wake.sync, "run_detailed", return_value=(sync.ERROR, sync.Stop())), \
                patch.object(wake.sync, "blocked_reason", partial(sync.blocked_reason, self.a)):
            warning = wake.refresh()
        self.assertIn("стоит на конфликте с GitHub", warning)
        self.assertIn("journal/2026-09.md", warning)

    def test_wake_does_not_read_stop_while_sync_still_runs(self):
        """Ревью 11.10.2026 (третий круг, 3): прогон синхронизации держит
        замок дольше ожидания. Пробуждение не читает прежнюю причину и
        промежуточное дерево, а предупреждает, что синхронизация ещё идёт."""
        from domovoy import wake
        sync.save_stop(sync.Stop(sync.DIVERGED, "journal/2026-09.md"))
        held = sync._lock(0)
        try:
            with patch.object(wake, "STATE_WAIT_SEC", 0), \
                    patch.object(wake.sync, "run_detailed", return_value=(sync.ERROR, sync.Stop())):
                warning = wake.refresh()
            with patch.object(wake, "STATE_WAIT_SEC", 0), \
                    patch.object(wake.sync, "run_detailed", side_effect=OSError("упал")), \
                    patch.object(wake.sync, "REPO_ROOT", self.a):
                crashed = wake.refresh()
            self.assertIsNone(sync.blocked_reason(self.a, wait=0))
        finally:
            held.close()
        self.assertIn("ещё идёт", warning)
        self.assertNotIn("конфликте", warning)
        self.assertIn("ещё идёт", crashed)

    def test_heading_underline_is_not_a_marker(self):
        write(self.a, "data/table.md", "Отклики\n=======\n\n> цитата\n")
        self.assertEqual(sync.run(self.a), sync.OK)
        self.assertIn("=======", self.remote_file("data/table.md"))

    def test_real_stuck_rebase_with_detached_head_reported_at_once(self):
        """X-E4: во время настоящего rebase HEAD отсоединён. Сообщение «git
        встал» сразу и один раз, а не общее через восемь сбоев."""
        self.real_stuck_rebase()
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        payload = self.post.call_args.args[2]
        self.assertEqual(payload["title"], "git встал")
        self.assertIn("rebase --abort", payload["text"])
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        self.assertTrue((self.a / ".git" / "rebase-merge").exists())

    def test_manual_rebase_steps_notify_once(self):
        """X-E4 (ревью 11.10.2026): Иван сводит конфликт руками, rebase идёт
        по шагам, каждая запись в данные будит синхронизацию. Сообщение одно на
        всю операцию и не выдаёт живой rebase за оборванный; после её конца
        синхронизация идёт сама, а следующая операция снова сообщается."""
        self.push_from_b("journal/2026-09.md", "## 2026-09-21\nПлан: из b\n")
        self.push_from_b("data/table.md", "| из b |\n")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: из a\n")
        g(self.a, "commit", "-qam", "журнал ПК")
        write(self.a, "data/table.md", "| из a |\n")
        g(self.a, "add", "-A")
        g(self.a, "commit", "-qm", "таблица ПК")
        g(self.a, "fetch", "-q", "origin")

        def rebase(*args: str) -> None:
            subprocess.run(["git", "-C", str(self.a), "rebase", *args], capture_output=True,
                           env={**os.environ, "GIT_EDITOR": "true"})

        rebase("origin/main")                                # встал на первом коммите
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        text = self.post.call_args.args[2]["text"]
        self.assertIn("ждёт", text)
        self.assertIn("--continue", text)
        self.assertNotIn("по таймауту", text)
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: из b и из a\n")
        g(self.a, "add", "journal/2026-09.md")
        rebase("--continue")                                 # встал на втором
        self.assertTrue((self.a / ".git" / "rebase-merge").exists())
        self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        write(self.a, "data/table.md", "| из b |\n| из a |\n")
        g(self.a, "add", "data/table.md")
        rebase("--continue")
        self.assertFalse((self.a / ".git" / "rebase-merge").exists())
        self.assertEqual(sync.run(self.a), sync.OK)
        self.assertIn("из b и из a", self.remote_file("journal/2026-09.md"))
        # Та же пустая операция дважды: после конца первой вторая снова сообщается.
        for n in (2, 3):
            (self.a / ".git" / "rebase-merge").mkdir()
            self.assertEqual(sync.run(self.a), sync.ERROR)
            self.assertEqual(self.post.call_count, n)
            (self.a / ".git" / "rebase-merge").rmdir()
            self.assertEqual(sync.run(self.a), sync.OK)

    def test_foreign_rebase_started_during_run_is_not_aborted(self):
        """С1: сессия по скиллу начала свой rebase в тот момент, когда
        синхронизация уже вызывает свой; встала на конфликте, человек свёл
        файл и сделал git add. Чужой rebase не откатывается, а сообщается."""
        self.push_from_b("journal/2026-09.md", "## 2026-09-21\nПлан: из b\n")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: из a\n")
        g(self.a, "commit", "-qam", "сессия на ПК")
        real = sync.git
        started = []

        def git_with_foreign(repo, *args, check=True):
            if args[:1] == ("rebase",) and "--abort" not in args and not started:
                started.append(True)
                real(repo, "rebase", "-q", "origin/main", check=False)
                write(repo, "journal/2026-09.md", "## 2026-09-21\nПлан: из b и из a (сведено руками)\n")
                real(repo, "add", "journal/2026-09.md")
            return real(repo, *args, check=check)

        with patch.object(sync, "git", side_effect=git_with_foreign):
            self.assertEqual(sync.run(self.a), sync.ERROR)
        self.assertTrue(started)
        self.assertTrue((self.a / ".git" / "rebase-merge").exists())
        self.assertIn("сведено руками", (self.a / "journal/2026-09.md").read_text(encoding="utf-8"))
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[2]["title"], "git встал")

    def test_foreign_rebase_after_entry_check_is_not_pushed(self):
        """С1: чужой rebase начался после входной проверки, пока синхронизация
        коммитила данные: первый коммит он уже применил, на втором встал. Его
        половину не отправляем и не откатываем (ревью 11.10.2026: в прошлой
        версии теста отправлять было нечего)."""
        self.push_from_b("journal/2026-09.md", "## 2026-09-21\nПлан: из b\n")
        before = g(self.remote, "rev-parse", "main")
        write(self.a, "data/table.md", "| строка ПК |\n")
        g(self.a, "add", "-A")
        g(self.a, "commit", "-qm", "таблица ПК")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: из a\n")
        g(self.a, "commit", "-qam", "сессия на ПК")
        commit_data = sync.commit_data
        real_git = sync.git
        pushes = []

        def commit_then_foreign_rebase(repo):
            out = commit_data(repo)
            g(repo, "fetch", "-q", "origin")
            subprocess.run(["git", "-C", str(repo), "rebase", "-q", "origin/main"], capture_output=True)
            return out

        def git_spy(repo, *args, check=True):
            if args[:1] == ("push",):
                pushes.append(args)
            return real_git(repo, *args, check=check)

        with patch.object(sync, "commit_data", side_effect=commit_then_foreign_rebase), \
                patch.object(sync, "git", side_effect=git_spy):
            self.assertEqual(sync.run(self.a), sync.ERROR)
        self.assertTrue((self.a / ".git" / "rebase-merge").exists())
        # Первый коммит применён поверх GitHub: отправить было что.
        self.assertEqual(g(self.a, "rev-list", "--count", "origin/main..HEAD").strip(), "1")
        self.assertEqual(pushes, [])
        self.assertEqual(g(self.remote, "rev-parse", "main"), before)
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[2]["title"], "git встал")

    def test_changing_errors_notify_once_per_streak(self):
        """C46: сеть мигает, текст ошибки меняется, ERROR и OFFLINE чередуются.
        Сообщение одно на всю полосу сбоев, после удачного прогона снова."""
        real = sync.git
        n = [0]

        def flaky(repo, *args, check=True):
            if args[:1] == ("fetch",):
                n[0] += 1
                if n[0] % 2:
                    raise sync.GitError("таймаут 60 с (git fetch)")
                return subprocess.CompletedProcess(
                    args, 128, "", f"Connection closed by 140.82.121.{n[0]} port 22\n")
            return real(repo, *args, check=check)

        with patch.object(sync, "git", side_effect=flaky):
            for _ in range(2 * sync.FAIL_NOTIFY_AFTER):
                sync.run(self.a)
        self.post.assert_called_once()
        self.assertIn("синхронизация стоит", self.post.call_args.args[2]["title"])
        self.assertIs(self.post.call_args.args[2]["silent"], True)  # C48: без звука
        self.assertEqual(sync.run(self.a), sync.OK)
        with patch.object(sync, "git", side_effect=flaky):
            for _ in range(sync.FAIL_NOTIFY_AFTER):
                sync.run(self.a)
        self.assertEqual(self.post.call_count, 2)

    def test_hook_refusal_reason_reaches_notification(self):
        """Хук pre-commit (gitleaks в своих репозиториях с 08.11.2026) отклонил
        коммит данных с несколькими путями: уведомление называет причину, а не
        аргументы git commit. Раньше первые 200 знаков первой строки занимали
        сообщение коммита и пути, и причина до владельца не доходила."""
        reason = ("pre-commit: коммит отклонён: gitleaks нашёл похожее на секрет в индексе (2): "
                  "data/table.md:1, data/voronka.md:1")
        hook = self.a / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\necho '{reason}' >&2\necho '  совет' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: x\nСделано: x\n")
        write(self.a, "data/table.md", "| компания |\n")
        write(self.a, "data/voronka.md", "| вакансия |\n")
        for _ in range(sync.FAIL_NOTIFY_AFTER):
            self.assertEqual(sync.run(self.a), sync.ERROR)
        self.post.assert_called_once()
        text = self.post.call_args.args[2]["text"]
        self.assertIn(f"{reason} (git commit, путей: 3)", text)
        self.assertNotIn("автосинхронизация", text)
        self.assertNotIn("совет", text)

    def test_git_error_names_reason_before_command(self):
        """Текст GitError: причина первой, команда коротко после неё, без
        сообщения коммита и путей. stderr из пробелов: причина из stdout; CRLF;
        подсказки и предупреждение о концах строк перед причиной уходят ниже;
        нет вывода, код выхода; предел 500 знаков после выбора причины; пустая
        строка хука удалённого; в stderr только шум, причина в stdout."""
        commit = ("commit", "-q", "-m", "данные: x", "--", "a.md", "b.md")
        crlf = "warning: in the working copy of 'data/%d.md', LF will be replaced by CRLF the next time Git touches it"
        cases = [
            ((1, "", "\n".join(crlf % i for i in range(6)) + "\npre-commit: отказ\n"),
             # Предел 500 на текст с причиной первой: «pre-commit: отказ\n» 18 знаков.
             "pre-commit: отказ (git commit, путей: 2)\n" + "\n".join(crlf % i for i in range(6))[:482].rstrip()),
            ((1, "", "remote: \nremote: причина\nTo x\n"), "remote: причина (git commit, путей: 2)\nTo x"),
            ((1, "nothing to commit\n", crlf % 0 + "\n"), f"nothing to commit (git commit, путей: 2)\n{crlf % 0}"),
            ((1, "", "fatal: причина\n  подробности\n"), "fatal: причина (git commit, путей: 2)\n  подробности"),
            ((1, "nothing to commit\n", " \n"), "nothing to commit (git commit, путей: 2)"),
            ((1, "", "причина\r\nещё\r\n"), "причина (git commit, путей: 2)\nещё"),
            ((1, "", "hint: The hook was ignored\nwarning: in the working copy of 'a.md', LF will be "
                     "replaced by CRLF\n\npre-commit: отказ\n  деталь\n"),
             "pre-commit: отказ (git commit, путей: 2)\nhint: The hook was ignored\n"
             "warning: in the working copy of 'a.md', LF will be replaced by CRLF\n  деталь"),
            ((3, "", ""), "код выхода 3, без текста (git commit, путей: 2)"),
        ]
        for (code, out, err), expected in cases:
            with self.subTest(err=err or out), \
                    patch.object(sync.proc, "git", return_value=subprocess.CompletedProcess([], code, out, err)), \
                    self.assertRaises(sync.GitError) as caught:
                sync.git(self.a, *commit)
            self.assertEqual(str(caught.exception), expected)
        long = subprocess.CompletedProcess([], 1, "", "x" * 600 + "\nхвост\n")
        with patch.object(sync.proc, "git", return_value=long), self.assertRaises(sync.GitError) as caught:
            sync.git(self.a, "-c", "core.quotePath=false", "log", "--format=")
        self.assertEqual(str(caught.exception), "x" * 500 + " (git log)")
        self.assertEqual(sync.git(self.a, "rev-parse", "--verify", "нет-такого", check=False).returncode, 128)
        # Предел пришёлся на перевод строки: пустой строки в конце нет.
        self.assertEqual(sync.reason_lines(subprocess.CompletedProcess([], 1, "", "abc\ndef\n"), 4), ["abc"])
        self.assertEqual(sync.reason_lines(subprocess.CompletedProcess([], 1, "", "abc\n"), 0), [])
        status = subprocess.CompletedProcess([], 1, "a\0b\0", "warning: in the working copy of x\n")
        self.assertEqual(sync.reason_lines(status, 100, use_stdout=False), ["warning: in the working copy of x"])

    def test_git_timeout_names_command_briefly(self):
        """Таймаут: та же короткая команда, rebase и rebase --abort различимы,
        исходное исключение в __cause__."""
        for args, expected in ((("commit", "-m", "данные: x", "--", "a.md"), "таймаут 60 с (git commit, путей: 1)"),
                               (("rebase", "--abort"), "таймаут 60 с (git rebase --abort)"),
                               (("rebase", "-q", "--autostash", "origin/main"), "таймаут 60 с (git rebase)")):
            with self.subTest(args=args), \
                    patch.object(sync.proc, "git", side_effect=subprocess.TimeoutExpired("git", 60)), \
                    self.assertRaises(sync.GitError) as caught:
                sync.git(self.a, *args)
            self.assertEqual(str(caught.exception), expected)
            self.assertIsInstance(caught.exception.__cause__, subprocess.TimeoutExpired)

    def streak_notice(self, command: str, code: int, stderr: str, expected_code: int) -> str:
        """Текст уведомления о полосе сбоев, когда git <command> раз за разом
        отвечает code и stderr, а остальной git настоящий."""
        real = sync.git

        def failing(repo, *args, check=True):
            if args[:1] == (command,) and "--abort" not in args:
                return subprocess.CompletedProcess(args, code, "", stderr)
            return real(repo, *args, check=check)

        with patch.object(sync, "git", side_effect=failing):
            for _ in range(sync.FAIL_NOTIFY_AFTER):
                self.assertEqual(sync.run(self.a), expected_code)
        self.post.assert_called_once()
        return self.post.call_args.args[2]["text"]

    def test_push_refusal_reason_reaches_notification(self):
        """Отказ push: git печатает первой строкой «To <адрес>», причина ниже.
        Уведомление о полосе сбоев называет причину (ревью 08.11.2026)."""
        write(self.a, "data/table.md", "| компания |\n")
        text = self.streak_notice("push", 1, (
            "To github.com:owner/repo.git\n ! [rejected]        HEAD -> main (fetch first)\n"
            "error: failed to push some refs to 'github.com:owner/repo.git'\nhint: Updates were rejected\n"),
            sync.OFFLINE)
        self.assertIn("отправка не прошла: ! [rejected]        HEAD -> main (fetch first). ", text)

    def test_fetch_failure_reason_reaches_notification(self):
        """Ветка fetch берёт причину той же common.reason_lines: подсказка
        перед причиной уходит ниже."""
        text = self.streak_notice("fetch", 128, "hint: x\nfatal: unable to access: Could not resolve host\n",
                                  sync.OFFLINE)
        self.assertIn("GitHub недоступен, повтор на следующем прогоне (fatal: unable to access: "
                      "Could not resolve host). ", text)

    def test_rebase_failure_reason_reaches_notification(self):
        """Rebase без конфликта (замок индекса, неотслеживаемый файл на пути):
        причина в уведомлении, подсказка ниже."""
        write(self.b, "prompt.md", "правила v2\n")
        g(self.b, "commit", "-qam", "промт с другой машины")
        g(self.b, "push", "-q", "origin", "main")
        text = self.streak_notice("rebase", 1, "hint: x\nerror: причина отказа\n", sync.ERROR)
        self.assertIn("rebase не прошёл: error: причина отказа. ", text)

    def wake_prompt(self) -> str:
        """Утреннее пробуждение на этом репозитории: что получил агент."""
        from domovoy import wake
        real_run = sync.run_detailed
        with patch.dict(os.environ, {"DOMOVOY_CHAT_ID": "183"}), patch.object(wake, "load_env"), \
                patch.object(sync, "REPO_ROOT", self.a), \
                patch.object(sync, "run_detailed", lambda **kw: real_run(self.a, **kw)), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post:
            self.assertEqual(wake.main(["--morning", "--force"]), 0)
        return post.call_args.args[2]["prompt"]

    def test_wake_warns_agent_about_conflict(self):
        """C49: пробуждение при конфликте синхронизации предупреждает агента не
        править данные до разбора."""
        self.autostash_conflict()
        prompt = self.wake_prompt()
        self.assertIn("Синхронизация репозитория Домового стоит", prompt)
        self.assertIn("journal/2026-09.md", prompt)
        self.assertIn("ничего не записывай", prompt)

    def test_wake_warns_agent_about_stuck_rebase(self):
        """C49: незаконченный rebase синхронизация возвращает как ERROR, а не
        CONFLICT; предупреждение всё равно есть."""
        self.real_stuck_rebase()
        prompt = self.wake_prompt()
        self.assertIn("Синхронизация репозитория Домового стоит", prompt)
        self.assertIn("незаконченный rebase", prompt)

    def test_wake_warns_agent_about_plain_conflict(self):
        """C49 (ревью 11.10.2026): обычный конфликт, rebase откатан, маркеров
        в дереве нет. Агент всё равно узнаёт, что данные с другой машины не
        подтянуты и уже записанное в этих файлах переписывать нельзя."""
        self.push_from_b("journal/2026-09.md", "## 2026-09-21\nПлан: из b\n")
        write(self.a, "journal/2026-09.md", "## 2026-09-21\nПлан: из a\n")
        prompt = self.wake_prompt()
        self.assertFalse((self.a / ".git" / "rebase-merge").exists())
        self.assertEqual(sync.unmerged_paths(self.a), [])
        self.assertIn("Синхронизация репозитория Домового стоит на конфликте", prompt)
        self.assertIn("journal/2026-09.md", prompt)
        self.assertIn("не подтянуты", prompt)
        self.assertIn("не переписывай", prompt)

    def test_wake_says_incoming_not_pulled_while_code_is_dirty(self):
        """C49 (ревью 11.10.2026): синхронизация ждёт правку кода, коммиты с
        GitHub не подтянуты, агент работает по устаревшим данным и знает это."""
        self.push_from_b("journal/2026-09.md", "## 2026-09-21\nПлан: x\nс мака\n")
        write(self.a, "domovoy/code.py", "x = 3  # недописано\n")
        prompt = self.wake_prompt()
        self.assertIn("Синхронизация репозитория Домового ждёт", prompt)
        self.assertIn("domovoy/code.py", prompt)
        self.assertIn("не подтянуты", prompt)

    def test_wake_names_markers_left_after_git_add(self):
        """Ревью 11.10.2026: отдельная причина для маркеров после git add."""
        self.autostash_conflict()
        g(self.a, "add", "prompt.md", "journal", "data")
        prompt = self.wake_prompt()
        self.assertIn("маркер", prompt)
        self.assertIn("git add", prompt)
        self.assertIn("ничего не записывай", prompt)

    def test_wake_without_conflict_has_no_warning(self):
        self.assertNotIn("Синхронизация репозитория", self.wake_prompt())


if __name__ == "__main__":
    unittest.main()
