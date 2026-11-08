"""Подпроцессы с тайм-аутом и git (ревизия повторов 07.11.2026, шаг 1).

Зачем не `subprocess.run(timeout=...)`: на сроке он шлёт SIGKILL одному
верхнему процессу. git, убитый SIGKILL, оставляет `.git/index.lock`, и все
следующие коммиты стоят; его хук и прочие потомки продолжают жить. Здесь
процесс всегда лидер своей группы, и на сроке и любом исключении (Ctrl+C,
сбой чтения) завершается вся группа: SIGTERM с ожиданием (git на нём сам
убирает замок), затем SIGKILL. Образец поведения `diary.run_git` (ревью
11.10.2026).

Только stdlib: модуль входит в открытую копию.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence

# Сколько ждём процесс после SIGKILL группе: зависнуть на этом ожидании
# (группа из одних зомби на маке) хуже, чем не дождаться. Для git (term_first)
# дольше: git в непрерываемом вводе-выводе на /mnt/c кончается не сразу, а
# вызывающий после возврата отпускает свой замок (diary.git_lock).
_REAP_WAIT = 5
_REAP_WAIT_TERM = 60

# git без терминала: не спрашивает пароль ни сам, ни через ssh. Без этого
# ручной запуск из терминала ждал ввода ключа до срока и решал «GitHub
# недоступен», под systemd отказ был сразу. Свою команду ssh вызывающего
# (GIT_SSH_COMMAND, GIT_SSH) не перебиваем: в ней может быть ключ.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}
GIT_SSH_BATCH = "ssh -o BatchMode=yes"


def _signal_group(pid: int, sig: int) -> bool:
    """Сигнал группе. False, если группы уже нет. PermissionError (на маке так
    отвечает группа из одних зомби) не доказывает, что группа исчезла."""
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _stop_group(proc: subprocess.Popen, *, term_first: bool, grace: float) -> None:
    """Завершить группу процесса: SIGTERM и опрос до grace, потом SIGKILL.
    Родителя попутно забираем (poll), чтобы его зомби не продлевал ожидание:
    смерть git не означает смерть его хука."""
    if term_first and _signal_group(proc.pid, signal.SIGTERM):
        deadline = time.monotonic() + grace
        killed = False
        try:
            while True:
                proc.poll()
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    killed = True
                    break
                except PermissionError:
                    pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
        finally:
            # И на втором Ctrl+C посреди ожидания: группа не должна пережить нас.
            if not killed:
                _signal_group(proc.pid, signal.SIGKILL)
    else:
        _signal_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=_REAP_WAIT_TERM if term_first else _REAP_WAIT)
    except subprocess.TimeoutExpired:
        pass


def run(
    args: Sequence[str], *, timeout: float, cwd: str | Path | None = None,
    input: str | None = None, env: Mapping[str, str] | None = None,
    term_first: bool = False, grace: float = 10,
) -> subprocess.CompletedProcess[str]:
    """Как `subprocess.run(args, capture_output=True, text=True, timeout=...)`,
    но в своей группе процессов: на сроке и любом исключении группа
    завершается (term_first: сначала SIGTERM и до grace секунд ожидания,
    иначе сразу SIGKILL), исключение пробрасывается (на сроке
    subprocess.TimeoutExpired). Без input stdin закрыт (DEVNULL): процесс не
    ждёт ввода с терминала. Вывод UTF-8, непрочитанные байты заменяются.
    Своя сессия отвязывает процесс от терминала: SIGHUP закрытого терминала
    до него не доходит, и с ручного запуска он доживает до своего срока."""
    proc = subprocess.Popen(
        list(args),
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
        text=True, encoding="utf-8", errors="replace",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except BaseException:
        try:
            _stop_group(proc, term_first=term_first, grace=grace)
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def git(repo: str | Path, *args: str, timeout: float = 60, grace: float = 10) -> subprocess.CompletedProcess[str]:
    """`git -C repo args` через run(term_first=True) без терминала (GIT_ENV).
    Код возврата не проверяет: это дело вызывающего."""
    env = {**os.environ, **GIT_ENV}
    if not env.get("GIT_SSH_COMMAND") and not env.get("GIT_SSH"):
        env["GIT_SSH_COMMAND"] = GIT_SSH_BATCH
    return run(["git", "-C", str(repo), *args], timeout=timeout,
               env=env, term_first=True, grace=grace)
