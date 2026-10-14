"""Синхронизация репозитория Домового с GitHub.

Журнал, таблицы и промт правит агент: бот на ПК пишет файлы, сессия Claude на
любой машине тоже. Сам git ничего не коммитит, поэтому на ПК одно и то же
(закоммитить изменения данных, подтянуть чужие, отправить свои) делают:
domovoy-sync.path сразу после записи в данные, domovoy-sync.timer страховкой
раз в 15 минут и каждое пробуждение перед событием. Чем короче окно между
записью на ПК и отправкой, тем реже конфликт с правкой на другой машине.

Коммитятся только данные (DATA_PATHS). Код синхронизация не коммитит
никогда: полуготовая правка кода на ПК не должна уехать на GitHub сама. Уже
сделанные руками локальные коммиты ветки отправляются вместе с данными.

Конфликт (одни и те же строки правили на двух машинах) не решается: rebase
откатывается, свой коммит остаётся локально, в личный диалог уходит одно
сообщение с именами файлов, сводит человек. О том же конфликте повторно не
пишем. Файл с маркерами конфликта (так кончается autostash, чью правку
подтянутое задело) не коммитится никогда, в том числе после `git add`, а
локальный коммит, который их добавил, не отправляется, даже если следующий
коммит их убрал: синхронизация стоит до разбора, об оставшихся маркерах своё
сообщение (ревью 11.10.2026).
Не конфликтная ошибка rebase человека не зовёт, её повторит следующий прогон.
Rebase, оборванный таймаутом git, откатывается в том же прогоне; если
незаконченный rebase или merge уже лежал на входе или начался во время
прогона (обрыв службы, ручная работа, сессия по скиллу), его синхронизация не
трогает: ждёт его конца и пишет об этом в личный диалог один раз на операцию.

Незакоммиченная правка вне данных (код на ПК) при новых коммитах на GitHub
останавливает rebase: autostash спрятал бы и вернул её вместе с данными, при
конфликте с маркерами в исполняемом коде. Синхронизация один раз пишет и
ждёт, пока правку закоммитят или уберут (ревью 11.10.2026). Свои писатели
данных в коде (строка сторожа в журнал) берут тот же замок, что и прогон
синхронизации (data_lock). Почему синхронизация стоит, прогон отдаёт
пробуждению (run_detailed): оно предупреждает агента.

Остальные сбои (не та ветка, GitHub недоступен, отправка отклонена, ошибка
git) повторяет следующий прогон молча; FAIL_NOTIFY_AFTER таких прогонов
подряд (около двух часов по таймеру) дают одно сообщение в личный диалог на
всю полосу сбоев, даже если текст ошибки меняется; удачный прогон счёт
сбрасывает.

Exit-коды: 0 успех или нечего делать, 1 ошибка git или не та ветка,
2 конфликт, 4 GitHub недоступен или отказал (повтор на следующем прогоне).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO, NamedTuple

from .common import REPO_ROOT, STATE_ROOT, TZ, ConfigError, bot_endpoint, load_env, post_event, read_target
from .i18n import _

DATA_PATHS = ("prompt.md", "diary.md", "journal", "data")
REMOTE = "origin"
BRANCH = "main"
GIT_TIMEOUT = 60
LOCK_WAIT_SEC = 90
DATA_LOCK_WAIT_SEC = 20.0       # писатель данных ждёт прогон синхронизации: обычно он идёт секунды
STATE_DIR = STATE_ROOT / "sync"
FAIL_NOTIFY_AFTER = 8           # прогонов подряд с ошибкой до сообщения: таймер раз в 15 минут

OK, ERROR, CONFLICT, OFFLINE = 0, 1, 2, 4

# Почему синхронизация стоит, для пробуждения агента (ревью 11.10.2026):
# BLOCKED в файлах маркеры конфликта или идёт rebase или merge, данные не править;
# DIVERGED конфликт с GitHub, rebase откатан, свои записи в локальном коммите,
# чужие не подтянуты; WAITING ждёт правку кода, коммиты с GitHub не подтянуты.
BLOCKED, DIVERGED, WAITING = "blocked", "diverged", "waiting"
# RUNNING другой прогон держит замок дольше ожидания, состояние не прочитано
# (ревью 11.10.2026, третий круг).
RUNNING = "running"


class Stop(NamedTuple):
    kind: str = ""      # BLOCKED, DIVERGED, WAITING, RUNNING; пусто, если не стоит
    detail: str = ""


class GitError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(_("git {args}: таймаут {timeout} с").format(args=' '.join(args), timeout=GIT_TIMEOUT)) from e
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()[:500]}")
    return proc


def changed_data_paths(repo: Path) -> list[str]:
    """Изменённые, новые и удалённые файлы данных, включая старое имя при переименовании."""
    out = git(repo, "status", "--porcelain", "-z", "--untracked-files=all", "--", *DATA_PATHS).stdout
    entries = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" and i < len(entries):
            paths.append(entries[i])
            i += 1
    return paths


def commit_data(repo: Path) -> list[str]:
    paths = changed_data_paths(repo)
    if not paths:
        return []
    git(repo, "add", "-A", "--", *paths)
    shown = ", ".join(paths[:4]) + (f" и ещё {len(paths) - 4}" if len(paths) > 4 else "")
    # Пути в commit: чужое, уже добавленное в индекс руками, в этот коммит не попадёт.
    git(repo, "commit", "-q", "-m", f"данные: {shown}",
        "-m", f"автосинхронизация Домового, {socket.gethostname()}", "--", *paths)
    return paths


def _count(repo: Path, spec: str) -> int:
    return int(git(repo, "rev-list", "--count", spec).stdout.strip() or 0)


def unmerged_paths(repo: Path) -> list[str]:
    out = git(repo, "diff", "--name-only", "-z", "--diff-filter=U", check=False).stdout
    return [p for p in out.split("\0") if p]


# Только строки «<<<<<<<» и «>>>>>>>»: семь «=» бывают подчёркиванием заголовка.
CONFLICT_MARKER = re.compile(rb"^(?:<{7}|>{7})(?: |\r?$)", re.M)


def marker_paths(repo: Path, paths: list[str]) -> list[str]:
    """Файлы со строками маркеров конфликта (ревью 11.10.2026): после `git add`
    пометка конфликта снята, unmerged_paths файл уже не видит, а коммит унёс
    бы маркеры на GitHub."""
    found: list[str] = []
    for rel in paths:
        try:
            data = (repo / rel).read_bytes()
        except OSError:          # удалён
            continue
        if CONFLICT_MARKER.search(data):
            found.append(rel)
    return found


MARKER_LINE = re.compile(r"(?:<{7}|>{7})(?: |\r?$)")


def outgoing_marker_paths(repo: Path, upstream: str) -> list[str]:
    """Файлы данных, где отправляемые коммиты upstream..HEAD добавили строки
    маркеров конфликта (ревью 11.10.2026): маркеры, закоммиченные руками
    (`git add`, затем commit или `rebase --continue`), не уходят на GitHub,
    даже если следующий коммит их убрал. Смотрится каждый коммит, а не
    итоговое дерево; у merge разница с первым родителем. --full-history
    (ревью 11.10.2026, второй круг): с путями git log может упрощать
    историю, флаг явно требует обойти и коммиты боковой ветки, слитой с
    чистым итогом. Что без него такая ветка выпадает, на git 2.55 не
    воспроизведено, флаг оставлен как страховка (третий круг)."""
    out = git(repo, "-c", "core.quotePath=false", "log", "-p", "-U0", "--no-color", "--no-ext-diff",
              "--no-renames", "--full-history", "--diff-merges=first-parent", "--format=", f"{upstream}..HEAD",
              "--", *DATA_PATHS).stdout
    found: list[str] = []
    current = ""
    header = False
    for line in out.split("\n"):
        if line.startswith("diff --git "):
            header, current = True, ""
        elif header:
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line.startswith("@@"):
                header = False
        elif line.startswith("+") and current and MARKER_LINE.match(line, 1) and current not in found:
            found.append(current)
    return found


def dirty_code_paths(repo: Path) -> list[str]:
    """Незакоммиченные правки отслеживаемых файлов вне данных, в индексе или в
    дереве (ревью 11.10.2026). Неотслеживаемые не в счёт: autostash их не прячет."""
    excludes = [f":(exclude){p}" for p in DATA_PATHS]
    out = git(repo, "status", "--porcelain", "-z", "--untracked-files=no", "--no-renames",
              "--", ".", *excludes).stdout
    return [entry[3:] for entry in out.split("\0") if entry]


def _lock(wait: float) -> IO[str] | None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(STATE_DIR / "sync.lock", "w")
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(1)


@contextmanager
def data_lock(wait: float | None = None) -> Iterator[bool]:
    """Общий замок писателей данных в коде и прогона синхронизации (ревью
    11.10.2026): строка, дописанная посреди rebase, пропадала при его откате.
    Тот же файл и flock, что у run(). Отдаёт, взят ли замок. Не дождались или
    каталог состояния недоступен: без замка писатель в данные не пишет,
    сторож откладывает строку в очередь (ревью 11.10.2026: rebase держит
    замок до 60 с, его откат стёр бы строку). Обычно прогон идёт секунды."""
    wait = DATA_LOCK_WAIT_SEC if wait is None else wait
    try:
        lock = _lock(wait)
    except OSError as e:
        print(f"domovoy.sync: замок синхронизации недоступен: {e}", file=sys.stderr)
        lock = None
    try:
        yield lock is not None
    finally:
        if lock is not None:
            lock.close()


def _notify_once(state_name: str, key: str, title: str, text: str) -> None:
    """Одно сообщение в личный диалог без пробуждения на один и тот же ключ:
    синхронизация идёт раз в 15 минут, а сообщать о стоящей надо один раз."""
    state_file = STATE_DIR / state_name
    try:
        if json.loads(state_file.read_text(encoding="utf-8")).get("key") == key:
            return
    except (OSError, ValueError):
        pass
    try:
        chat_id, thread_id = read_target()
    except ConfigError as e:
        print(f"domovoy.sync: сообщить «{title}» некуда: {e}", file=sys.stderr)
        return
    payload: dict = {"source": _("синхронизация"), "title": title, "text": text, "wake": False, "chat_id": chat_id}
    if thread_id is not None:
        payload["thread_id"] = thread_id
    url, secret = bot_endpoint()
    ok, info = post_event(url, secret, payload)
    if ok:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"key": key}), encoding="utf-8")
    else:
        print(f"domovoy.sync: сообщение «{title}» не принято: {info}", file=sys.stderr)


def report_conflict(repo: Path, files: list[str], notify: bool) -> None:
    shown = ", ".join(files)
    print(f"domovoy.sync: конфликт в {shown}, автосинхронизация стоит до ручного разбора",
          file=sys.stderr)
    if not notify:
        return
    upstream = f"{REMOTE}/{BRANCH}"
    # HEAD в ключ не входит: пока конфликт не сведён, каждый прогон добавляет
    # локальный коммит с новыми записями журнала, а сообщать надо один раз.
    base = git(repo, "merge-base", "HEAD", upstream, check=False).stdout.strip()
    up = git(repo, "rev-parse", upstream, check=False).stdout.strip()
    key = f"{base}:{up}:{','.join(sorted(files))}"
    host = socket.gethostname()
    _notify_once("conflict.json", key, _("конфликт в git"), _(
        "Одни и те же строки правились в двух местах: {shown}. Своя версия на {host} цела, "
        "на GitHub другая. Автосинхронизация встала, свести руками в ~/src/domovoy "
        "на {host}: git status покажет, что делать."
    ).format(shown=shown, host=host))


STUCK_NAMES = ("rebase-merge", "rebase-apply", "MERGE_HEAD")


def report_dirty_code(files: list[str], notify: bool) -> None:
    """Правка вне данных при новых коммитах на GitHub (ревью 11.10.2026):
    rebase не делаем, один раз сообщаем и ждём человека."""
    shown = ", ".join(files[:4]) + (_(" и ещё {count}").format(count=len(files) - 4) if len(files) > 4 else "")
    print(f"domovoy.sync: незакоммиченная правка вне данных ({shown}), rebase не делаю",
          file=sys.stderr)
    if not notify:
        return
    host = socket.gethostname()
    # Ключ один на эпизод ожидания (ревью 11.10.2026): набор файлов растёт,
    # пока Иван пишет код, а ожидание то же. Удачный прогон удаляет dirty.json.
    _notify_once("dirty.json", "ожидание", _("синхронизация ждёт"), _(
        "В ~/src/domovoy на {host} незакоммиченная правка вне данных: {shown}. "
        "С GitHub пришли новые коммиты, а подтянуть их, не задев эту правку, нельзя. "
        "Пока её не закоммитить или не убрать (git stash), журнал и таблицы с этой машины "
        "на GitHub не уезжают; дальше синхронизация пойдёт сама."
    ).format(host=host, shown=shown))


def stuck_reason(names: list[str]) -> str:
    return _("незаконченный {what}").format(what="merge" if names == ["MERGE_HEAD"] else "rebase")


def markers_reason(files: list[str], committed: bool) -> str:
    if committed:
        return _("строки маркеров конфликта в локальных коммитах ({files})").format(files=', '.join(files))
    return _("строки маркеров конфликта в {files} (git add снял пометку, а сами строки остались)").format(
        files=', '.join(files))


def _stuck_key(gitdir: Path, names: list[str]) -> str:
    """Ключ одной операции (ревью 11.10.2026): что и на что переносится, а не
    время каталога rebase-merge, которое меняет каждый шаг ручного сведения."""
    parts = []
    for name in names:
        path = gitdir / name
        files = [path / f for f in ("orig-head", "onto", "head-name")] if path.is_dir() else [path]
        values = []
        for f in files:
            try:
                values.append(f.read_text(encoding="utf-8").strip())
            except OSError:
                values.append("")
        parts.append(f"{name}:{'|'.join(values)}")
    return ",".join(parts)


def report_stuck(gitdir: Path, names: list[str], notify: bool) -> None:
    """Незаконченный rebase или merge на входе или чужой, начатый во время
    прогона: его ведут руками или он оборвался, а пока он лежит, журнал и
    таблицы с этой машины не уезжают. Сообщение одно на операцию, stuck.json
    сбрасывает первый прогон после её конца (ревью 11.10.2026)."""
    print("domovoy.sync: в репозитории незаконченный rebase или merge, не трогаю", file=sys.stderr)
    if not notify:
        return
    host = socket.gethostname()
    what = "merge" if names == ["MERGE_HEAD"] else "rebase"
    _notify_once("stuck.json", _stuck_key(gitdir, names), _("git встал"), _(
        "В ~/src/domovoy на {host} не закончен git {what}. Синхронизация ждёт его конца и ничего в нём "
        "не трогает, а журнал и таблицы с этой машины до тех пор на GitHub не уезжают. Если его ведут "
        "руками, довести до конца (git {what} --continue), дальше синхронизация пойдёт сама. Если его "
        "никто не ведёт (git оборвался), git status покажет, что делать, обычно git {what} --abort."
    ).format(host=host, what=what))


def report_markers(files: list[str], committed: bool, notify: bool) -> None:
    """Строки маркеров конфликта, с которых `git add` уже снял пометку (ревью
    11.10.2026): git status их не покажет, а сообщение о конфликте отсылает к
    нему. Свой ключ на эпизод, удачный прогон удаляет markers.json."""
    shown = ", ".join(files)
    where = "в локальных коммитах" if committed else "в рабочем дереве"
    print(f"domovoy.sync: строки маркеров конфликта {where}: {shown}, не отправляю", file=sys.stderr)
    if not notify:
        return
    host = socket.gethostname()
    if committed:
        text = _(
            "В локальных коммитах ~/src/domovoy на {host} строки маркеров конфликта <<<<<<< или >>>>>>> "
            "в {shown}: так бывает, если после ручного сведения сделать git add и закоммитить, не убрав их. "
            "Такие коммиты синхронизация на GitHub не отправляет. Убрать маркеры надо из самих коммитов "
            "(git rebase -i origin/main и правка коммита): исправление новым коммитом не поможет, "
            "старый уехал бы вместе с ним. До тех пор журнал и таблицы с этой машины на GitHub не уезжают."
        ).format(host=host, shown=shown)
    else:
        text = _(
            "В ~/src/domovoy на {host} в {shown} остались строки маркеров конфликта <<<<<<< или >>>>>>>. "
            "git add снял пометку конфликта, но сами строки он не убирает: убрать их руками, дальше "
            "синхронизация пойдёт сама. До тех пор журнал и таблицы с этой машины на GitHub не уезжают."
        ).format(host=host, shown=shown)
    _notify_once("markers.json", "коммиты" if committed else "дерево", _("маркеры конфликта"), text)


def flush_watch_queue() -> None:
    """Отложенные строки сторожа в журнал под замком прогона (ревью
    11.10.2026, второй круг): иначе очередь ждала бы следующей строки
    сторожа, а тихие прогоны её не переносят."""
    try:
        from . import watch
    except ImportError:  # открытая копия без сторожа
        return
    try:
        watch.journal_flush_queue(datetime.now(TZ).date())
    except OSError as e:
        print(f"domovoy.sync: очередь строк сторожа не перенесена: {e}", file=sys.stderr)


def save_stop(stop: Stop) -> None:
    """Причина остановки последнего прогона (ревью 11.10.2026, второй круг):
    пробуждение, не дождавшееся замка, читает её у прогона, который замок
    держал (saved_stop). Обычный конфликт с откатанным rebase и ожидание
    правки кода в дереве не видны."""
    path = STATE_DIR / "stop.json"
    if stop.kind:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stop._asdict(), ensure_ascii=False), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def saved_stop(wait: float | None = None) -> Stop:
    """Причина остановки последнего прогона, под замком: идущий прогон
    сначала запишет свою (ревью 11.10.2026, второй круг). Замок не дождались,
    тогда RUNNING, а не прежняя причина: идущий прогон её ещё перепишет
    (ревью 11.10.2026, третий круг)."""
    with data_lock(wait) as locked:
        if not locked:
            return Stop(RUNNING)
        try:
            data = json.loads((STATE_DIR / "stop.json").read_text(encoding="utf-8"))
            return Stop(str(data.get("kind", "")), str(data.get("detail", "")))
        except (OSError, ValueError, AttributeError):
            return Stop()


def _fail(code: int, message: str) -> tuple[int, str, Stop]:
    print(f"domovoy.sync: {message}", file=sys.stderr)
    return code, message, Stop()


def count_failure(code: int, error: str, notify: bool) -> None:
    """Сбой, о котором никто не сообщил: счёт подряд, на пороге одно сообщение.
    код OK это успех: счёт и прошлое сообщение сбрасываются,
    чтобы следующая полоса сбоев снова дошла до Ивана."""
    path = STATE_DIR / "failures.json"
    if code == OK:
        for name in ("failures.json", "failing.json", "dirty.json", "markers.json"):
            (STATE_DIR / name).unlink(missing_ok=True)
        return
    try:
        count = int(json.loads(path.read_text(encoding="utf-8")).get("count", 0))
    except (OSError, ValueError, AttributeError):
        count = 0
    count += 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"count": count}), encoding="utf-8")
    if count < FAIL_NOTIFY_AFTER or not notify:
        return
    first = (error.strip().splitlines() or ["?"])[0][:200]
    host = socket.gethostname()
    # Ключ без кода и текста ошибки (ревью 11.10.2026): сеть мигает, текст
    # меняется, ERROR и OFFLINE чередуются, а сообщение одно на полосу сбоев.
    # Удачный прогон удаляет failing.json, и следующая полоса снова дойдёт.
    _notify_once("failing.json", "полоса сбоев", _("синхронизация стоит"), _(
        "Синхронизация ~/src/domovoy на {host} не проходит {count} раз подряд: {first}. "
        "Журнал и таблицы с этой машины на GitHub не уезжают; сама повторяет каждые 15 минут, "
        "посмотреть journalctl --user -u domovoy-sync."
    ).format(host=host, count=count, first=first))


def blocked_reason(repo: Path | None = None, wait: float | None = None) -> str | None:
    """Почему данные сейчас править нельзя (ревью 11.10.2026): незаконченный
    rebase или merge, файлы с конфликтом или с его маркерами; пусто, если
    можно. Её спрашивает пробуждение, когда прогон синхронизации сам причину
    не назвал (замок занят другим прогоном, исключение). Под замком: идущий
    прогон синхронизации сначала доделает своё. Замок не дождались: None,
    промежуточное дерево идущего прогона не читаем (ревью 11.10.2026,
    третий круг)."""
    repo = repo or REPO_ROOT
    reasons: list[str] = []
    with data_lock(wait) as locked:
        if not locked:
            return None
        gitdir = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
        stuck = [name for name in STUCK_NAMES if (gitdir / name).exists()]
        if stuck:
            reasons.append(stuck_reason(stuck))
        files = unmerged_paths(repo)
        if files:
            reasons.append(_("конфликт в {files}").format(files=", ".join(files)))
        else:
            marked = marker_paths(repo, changed_data_paths(repo))
            if marked:
                reasons.append(markers_reason(marked, committed=False))
    return ", ".join(reasons)


def _sync(repo: Path, notify: bool, dry_run: bool) -> tuple[int, str, Stop]:
    """(код, ошибка для счёта сбоев, почему стоит для пробуждения). Ошибка
    пустая, если о сбое уже сообщено отдельно (конфликт, незаконченный rebase,
    правка кода, маркеры) или сбоя нет."""
    # Незаконченный rebase проверяется раньше ветки (ревью 11.10.2026): во
    # время rebase HEAD отсоединён, и проверка ветки отвечала бы общей ошибкой.
    gitdir = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    stuck_names = [name for name in STUCK_NAMES if (gitdir / name).exists()]
    if stuck_names:
        report_stuck(gitdir, stuck_names, notify and not dry_run)
        return ERROR, "", Stop(BLOCKED, stuck_reason(stuck_names))
    if not dry_run:
        # Операция кончилась: следующая снова дойдёт до Ивана (ревью 11.10.2026).
        (STATE_DIR / "stuck.json").unlink(missing_ok=True)
    branch = git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
    if branch != BRANCH:
        return _fail(ERROR, _("ветка {branch}, а не {expected}, не трогаю").format(
            branch=branch or _("(отсоединённая)"), expected=BRANCH))
    stuck = unmerged_paths(repo)
    if stuck:
        # Файл с маркерами конфликта (прошлый autostash) не коммитить:
        # `git add` снял бы пометку конфликта, и маркеры уехали бы на GitHub.
        report_conflict(repo, stuck, notify and not dry_run)
        return CONFLICT, "", Stop(BLOCKED, _("конфликт в {files}").format(files=", ".join(stuck)))
    # Пометку конфликта уже сняли `git add`, а маркеры остались (ревью 11.10.2026):
    # своё сообщение, прежнее про конфликт отсылает к git status.
    marked = marker_paths(repo, changed_data_paths(repo))
    if marked:
        report_markers(marked, False, notify and not dry_run)
        return CONFLICT, "", Stop(BLOCKED, markers_reason(marked, committed=False))

    if dry_run:
        paths = changed_data_paths(repo)
        print(f"закоммитил бы: {', '.join(paths) if paths else 'нечего'}")
    else:
        flush_watch_queue()
        committed = commit_data(repo)
        if committed:
            print(f"domovoy.sync: закоммичено {', '.join(committed)}")

    fetch = git(repo, "fetch", "-q", REMOTE, BRANCH, check=False)
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip().splitlines()
        return _fail(OFFLINE, _("GitHub недоступен, повтор на следующем прогоне")
                     + (f" ({detail[0][:200]})" if detail else ""))
    upstream = f"{REMOTE}/{BRANCH}"
    behind = _count(repo, f"HEAD..{upstream}")
    if dry_run:
        print(f"позади {behind}, впереди {_count(repo, f'{upstream}..HEAD')}")
        return OK, "", Stop()
    # Чужой rebase или merge, начатый после входной проверки (сессия по
    # скиллу), не наш: ни откатывать его, ни отправлять его половину нельзя
    # (ревью 11.10.2026).
    stuck_names = [name for name in STUCK_NAMES if (gitdir / name).exists()]
    if stuck_names:
        report_stuck(gitdir, stuck_names, notify)
        return ERROR, "", Stop(BLOCKED, stuck_reason(stuck_names))
    if behind:
        # Правка вне данных (ревью 11.10.2026): autostash спрятал бы и вернул
        # её вместе с данными, при конфликте с маркерами в исполняемом коде.
        dirty = dirty_code_paths(repo)
        if dirty:
            report_dirty_code(dirty, notify)
            return ERROR, "", Stop(WAITING, ", ".join(dirty))
        try:
            rebase = git(repo, "rebase", "-q", "--autostash", upstream, check=False)
        except GitError:
            # Таймаут убил git посреди rebase: откатить сразу, иначе каждый
            # следующий прогон упирался бы в незаконченный rebase. Перед вызовом
            # его не было, значит он наш.
            if any((gitdir / name).exists() for name in ("rebase-merge", "rebase-apply")):
                git(repo, "rebase", "--abort", check=False)
            raise
        if rebase.returncode != 0:
            out = rebase.stderr or rebase.stdout
            if rebase.returncode == 128 and ("rebase-merge" in out or "rebase-apply" in out):
                # Наш rebase не начался: чужой успел между проверкой и вызовом
                # (git называет его каталог на любом языке). Не трогаем.
                names = [name for name in STUCK_NAMES if (gitdir / name).exists()]
                report_stuck(gitdir, names, notify)
                return ERROR, "", Stop(BLOCKED, stuck_reason(names))
            files = unmerged_paths(repo)
            if any((gitdir / name).exists() for name in ("rebase-merge", "rebase-apply")):
                git(repo, "rebase", "--abort", check=False)
            if not files:
                # Не конфликт (замок индекса, неотслеживаемый файл на пути
                # подтянутого): следующий прогон повторит сам, человек не нужен.
                return _fail(ERROR, _("rebase не прошёл: {output}").format(output=(rebase.stderr or rebase.stdout).strip()[:300]))
            report_conflict(repo, files, notify)
            # Rebase откатан, маркеров в дереве нет, но данные с другой машины
            # не подтянуты: пробуждение должно это знать (ревью 11.10.2026).
            return CONFLICT, "", Stop(DIVERGED, ", ".join(files))
        # Код 0 ещё не успех: autostash мог вернуться с конфликтом, тогда
        # маркеры лежат в рабочем дереве, а правка дублем в stash.
        files = unmerged_paths(repo)
        if files:
            report_conflict(repo, files, notify)
            return CONFLICT, "", Stop(BLOCKED, _("конфликт в {files}").format(files=", ".join(files)))
        print(f"domovoy.sync: подтянуто коммитов: {behind}")
    ahead = _count(repo, f"{upstream}..HEAD")
    if ahead:
        # Маркеры, закоммиченные руками, проверяются в каждом отправляемом
        # коммите (ревью 11.10.2026).
        marked = outgoing_marker_paths(repo, upstream)
        if marked:
            report_markers(marked, True, notify)
            return CONFLICT, "", Stop(BLOCKED, markers_reason(marked, committed=True))
        push = git(repo, "push", "-q", REMOTE, f"HEAD:{BRANCH}", check=False)
        if push.returncode != 0:
            # Чаще всего гонка: другая машина отправила между fetch и push.
            # Следующий прогон подтянет и отправит.
            return _fail(OFFLINE, _("отправка не прошла: {output}").format(output=push.stderr.strip()[:300]))
        print(f"domovoy.sync: отправлено коммитов: {ahead}")
    return OK, "", Stop()


def run_detailed(repo: Path = REPO_ROOT, *, notify: bool = True, dry_run: bool = False,
                 lock_wait: float = LOCK_WAIT_SEC) -> tuple[int, Stop]:
    """Прогон с причиной остановки для пробуждения (ревью 11.10.2026). Замок
    занят: причины нет, её ищут в самом репозитории (blocked_reason)."""
    lock = _lock(lock_wait)
    if lock is None:
        print(f"domovoy.sync: замок занят дольше {lock_wait:g} с, пропускаю", file=sys.stderr)
        return ERROR, Stop()
    try:
        try:
            code, error, stop = _sync(repo, notify, dry_run)
        except GitError as e:
            code, error, stop = _fail(ERROR, str(e))
        if not dry_run:
            try:
                save_stop(stop)
            except OSError as e:
                print(f"domovoy.sync: причина остановки не сохранена: {e}", file=sys.stderr)
        # Пробный прогон ничего не отправлял: ни успехом, ни сбоем он не считается.
        if not dry_run and (code == OK or error):
            count_failure(code, error, notify)
        return code, stop
    finally:
        lock.close()


def run(repo: Path = REPO_ROOT, *, notify: bool = True, dry_run: bool = False,
        lock_wait: float = LOCK_WAIT_SEC) -> int:
    return run_detailed(repo, notify=notify, dry_run=dry_run, lock_wait=lock_wait)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="domovoy.sync", description="Синхронизация данных Домового с GitHub")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что уехало бы, ничего не коммитить и не отправлять")
    args = parser.parse_args(argv)
    load_env()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
