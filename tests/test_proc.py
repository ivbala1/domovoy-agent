"""domovoy.proc: подпроцессы с тайм-аутом и git (ревизия повторов 07.11.2026, шаг 1)."""
from __future__ import annotations

import ast
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from domovoy import proc, sync

try:                                    # в открытую копию selfcheck не входит
    from domovoy import selfcheck
except ImportError:
    selfcheck = None

ROOT = Path(__file__).resolve().parents[1]

# Фальшивый git: кладёт index.lock, как git commit, и сам убирает его на
# SIGTERM. На SIGKILL замок остался бы, это и проверяем.
FAKE_SLOW_GIT = """#!/bin/sh
repo="$2"
lock="$repo/.git/index.lock"
trap 'rm -f "$lock"; echo term > "$repo/term"; exit 143' TERM
: > "$lock"
echo started > "$repo/started"
sleep 30 &
wait $!
"""

# Фальшивый git: печатает окружение и читает stdin до конца. С терминалом
# или открытым stdin cat ждал бы ввода до срока.
FAKE_ENV_GIT = """#!/bin/sh
echo "prompt=$GIT_TERMINAL_PROMPT"
echo "ssh=$GIT_SSH_COMMAND"
cat
printf 'bad:\\377\\n' >&2
"""


class FakeGit:
    """Подменяет git в PATH скриптом на время блока."""

    def __init__(self, script: str):
        self.script = script

    def __enter__(self) -> Path:
        self.dir = tempfile.TemporaryDirectory()
        exe = Path(self.dir.name) / "git"
        exe.write_text(self.script, encoding="utf-8")
        exe.chmod(0o755)
        self.env = patch.dict(os.environ, {"PATH": f"{self.dir.name}{os.pathsep}{os.environ['PATH']}"})
        self.env.start()
        return exe

    def __exit__(self, *exc) -> None:
        self.env.stop()
        self.dir.cleanup()


class GitTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".git").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_timeout_sends_sigterm_and_lock_is_gone(self) -> None:
        with FakeGit(FAKE_SLOW_GIT):
            start = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.git(self.repo, "commit", timeout=1.5, grace=5)
            elapsed = time.monotonic() - start
        self.assertTrue((self.repo / "started").exists())
        self.assertTrue((self.repo / "term").exists(), "git не получил SIGTERM")
        self.assertFalse((self.repo / ".git" / "index.lock").exists())
        self.assertLess(elapsed, 5, "ждали весь grace, хотя git вышел по SIGTERM")

    def test_sync_git_timeout_is_git_error_without_lock(self) -> None:
        """sync.git раньше шёл через subprocess.run: SIGKILL и замок навсегда."""
        with FakeGit(FAKE_SLOW_GIT), patch.object(sync, "GIT_TIMEOUT", 1.5):
            with self.assertRaises(sync.GitError):
                sync.git(self.repo, "commit")
        self.assertTrue((self.repo / "term").exists())
        self.assertFalse((self.repo / ".git" / "index.lock").exists())

    @unittest.skipIf(selfcheck is None, "selfcheck нет в открытой копии")
    def test_selfcheck_git_timeout_is_none(self) -> None:
        real = proc.git
        short = lambda repo, *a, **kw: real(repo, *a, **{**kw, "timeout": 1.5})  # noqa: E731
        with FakeGit(FAKE_SLOW_GIT), patch.object(proc, "git", short):
            self.assertIsNone(selfcheck._git(self.repo, "log"))
        self.assertFalse((self.repo / ".git" / "index.lock").exists())


class RealGitTimeoutTests(unittest.TestCase):
    def test_sync_commit_hook_timeout_leaves_no_index_lock(self) -> None:
        """Настоящий git: коммит упирается в срок на хуке, git получает SIGTERM
        и убирает index.lock; следующий коммит проходит."""
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)

            def g(*args: str) -> None:
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

            g("init", "-q")
            g("config", "user.email", "t@t")
            g("config", "user.name", "t")
            g("config", "commit.gpgsign", "false")   # подпись машины не в счёт
            hooks = repo / ".git" / "hooks"
            hooks.mkdir(exist_ok=True)
            hook = hooks / "pre-commit"
            hook.write_text("#!/bin/sh\nsleep 3\n", encoding="utf-8")
            hook.chmod(0o755)
            g("config", "core.hooksPath", str(hooks))   # общий hooksPath машины не в счёт
            (repo / "a.txt").write_text("a\n", encoding="utf-8")
            sync.git(repo, "add", "a.txt")
            with patch.object(sync, "GIT_TIMEOUT", 1):
                with self.assertRaises(sync.GitError):
                    sync.git(repo, "commit", "-q", "-m", "проба")
            self.assertFalse((repo / ".git" / "index.lock").exists())
            hook.unlink()
            sync.git(repo, "commit", "-q", "-m", "проба")
            self.assertEqual(sync.git(repo, "rev-list", "--count", "HEAD").stdout.strip(), "1")


class RunTests(unittest.TestCase):
    def test_git_env_is_non_interactive_and_stdin_closed(self) -> None:
        with FakeGit(FAKE_ENV_GIT), patch.dict(os.environ, {"GIT_TERMINAL_PROMPT": "1"}):
            os.environ.pop("GIT_SSH_COMMAND", None)
            os.environ.pop("GIT_SSH", None)
            r = proc.git(Path("."), "fetch", timeout=5)
        self.assertEqual(r.returncode, 0)
        self.assertIn("prompt=0", r.stdout)
        self.assertIn("ssh=ssh -o BatchMode=yes", r.stdout)
        self.assertIn("bad:�", r.stderr)      # байт не UTF-8 не роняет вызов

    def test_own_ssh_command_is_kept(self) -> None:
        with FakeGit(FAKE_ENV_GIT), patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i key"}):
            r = proc.git(Path("."), "fetch", timeout=5)
        self.assertIn("ssh=ssh -i key", r.stdout)

    def test_second_interrupt_during_grace_still_kills_group(self) -> None:
        p = subprocess.Popen(["sh", "-c", "trap '' TERM; echo ready; exec sleep 30"],
                             stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "ready")     # SIGTERM уже не действует
            with patch.object(proc.time, "sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    proc._stop_group(p, term_first=True, grace=10)
            p.wait(timeout=5)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()

    def test_input_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            r = proc.run(["sh", "-c", "pwd; cat"], timeout=5, cwd=folder, input="привет")
        self.assertEqual(r.stdout.splitlines(), [os.path.realpath(folder), "привет"])

    def test_kill_without_term_first_is_immediate(self) -> None:
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            proc.run(["sh", "-c", "trap '' TERM; sleep 30"], timeout=0.3, grace=10)
        self.assertLess(time.monotonic() - start, 5)

    def test_term_ignored_gets_kill_after_grace(self) -> None:
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            proc.run(["sh", "-c", "trap '' TERM; sleep 30"], timeout=0.3, term_first=True, grace=0.5)
        self.assertLess(time.monotonic() - start, 5)


# --- тест-преграда: git только через proc ------------------------------------

_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "create_subprocess_exec",
                     "create_subprocess_shell", "system", "execvp", "execlp", "spawnlp", "spawnvp"}


def _git_spawns(source: str) -> list[int]:
    """Строки, где git запускают мимо proc: список или кортеж, начинающийся с
    "git" (в том числе собранный заранее), и вызов subprocess с ним."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                    and (first.value == "git" or first.value.endswith("/git")):
                hits.append(node.lineno)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _SUBPROCESS_CALLS:
                for arg in node.args[:2]:       # execlp("git", "git", ...) тоже
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and arg.value.split()[:1] == ["git"]:
                        hits.append(node.lineno)
                        break
    return hits


class BarrierTests(unittest.TestCase):
    def test_barrier_catches_direct_git(self) -> None:
        self.assertTrue(_git_spawns('import subprocess\nsubprocess.run(["git", "status"])\n'))
        self.assertTrue(_git_spawns('cmd = ("git", "-C", "x")\nPopen(cmd)\n'))
        self.assertTrue(_git_spawns('subprocess.run("git status", shell=True)\n'))
        self.assertTrue(_git_spawns('asyncio.create_subprocess_exec("git", "fetch")\n'))
        self.assertFalse(_git_spawns('subprocess.run(["rclone", "copy"])\n'))

    def test_no_git_outside_proc(self) -> None:
        """git с записью без своей группы оставлял index.lock (sync.git до
        07.11.2026): запускать git только через proc.git."""
        found = []
        paths = [*(ROOT / "domovoy").rglob("*.py"), *(ROOT / "tools").rglob("*.py")]
        for path in sorted(paths):
            if path == ROOT / "domovoy" / "proc.py":
                continue
            for line in _git_spawns(path.read_text(encoding="utf-8")):
                found.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual(found, [], "git мимо domovoy.proc.git")


if __name__ == "__main__":
    unittest.main()
