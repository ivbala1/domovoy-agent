"""Файлы состояния: атомарная запись, чтение JSON с откладыванием битого,
журнал JSON-строк и замок (ревизия повторов 07.11.2026, шаг 2).

Только stdlib: модуль зовут и сторож, и открытая копия, и дневник.

- write_atomic: временный файл рядом (mkstemp), fsync, подмена os.replace.
  Временный файл убирается при любом сбое, включая KeyboardInterrupt.
  Обрыв посреди записи оставляет прежний файл целым.
- read_json: нет файла значит default без шума. Битый (не UTF-8, не JSON, не
  того вида) откладывается в «<имя>.corrupt-<время>» со строкой в stderr, и
  вызывающий начинает с default: следующая запись не затрёт разбор битого.
  strict=True: громкий отказ (StateError) вместо default, для тех, кому тихий
  сброс опаснее остановки (сторож цены билетов).
- append_jsonl, iter_jsonl: строки JSON; при чтении оборванный UTF-8 и битые
  строки пропускаются, а не роняют разбор.
- flock: замок на файле, ждать без срока, wait секунд или одну попытку (0).
"""

from __future__ import annotations

import contextlib
import copy
import errno
import fcntl
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

# Ошибки fsync, которые прощает fsync="lenient": drvfs (хранилище Obsidian на
# диске Windows) может fsync не уметь. Настоящая ошибка записи (EIO, ENOSPC)
# не прощается: подменять исправный файл ненадёжной копией нельзя.
LENIENT_FSYNC_ERRNOS = (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS)
TMP_SUFFIX = ".tmp"
# Сколько отложенных копий одного файла держать: битый файл, повторяющийся
# каждый прогон, иначе копился бы без конца.
CORRUPT_KEEP = 3


class StateError(ValueError):
    """Файл состояния есть, но не читается или не того вида (strict=True)."""


def _umask_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def write_atomic(path: Path, data: str | bytes, *, mode: int | None = 0o600,
                 fsync: str | bool = "strict", check: Callable[[], bool] | None = None) -> bool:
    """Записать файл целиком или оставить прежний. Каталог должен быть.

    mode: права нового файла; None сохраняет права прежнего, а нового берёт по
    umask (данные репозитория и Obsidian, не личное состояние).
    fsync: "strict" любая ошибка fsync это сбой записи; "lenient" прощает
    LENIENT_FSYNC_ERRNOS; False без fsync.
    check: зовётся после записи временного файла, перед подменой; False значит
    не подменять (временный убирается, ответ False). Так дневник сверяет, не
    тронули ли файл с телефона, пока писалась копия.
    Ответ True, если файл подменён."""
    path = Path(path)
    if fsync not in ("strict", "lenient", False):
        raise ValueError(f"fsync={fsync!r}: ждали 'strict', 'lenient' или False")
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=TMP_SUFFIX)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            if fsync:
                try:
                    os.fsync(f.fileno())
                except OSError as e:
                    if fsync != "lenient" or e.errno not in LENIENT_FSYNC_ERRNOS:
                        raise
        if mode is None:
            try:
                mode = path.stat().st_mode & 0o777
            except FileNotFoundError:
                mode = _umask_mode()
        os.chmod(tmp, mode)
        if check is not None and not check():
            os.unlink(tmp)
            return False
        os.replace(tmp, path)
        return True
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json(path: Path, obj, *, indent: int | None = None, sort_keys: bool = False,
               mode: int | None = 0o600, fsync: str | bool = "strict") -> bool:
    """write_atomic для JSON (ensure_ascii=False)."""
    return write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=sort_keys),
                        mode=mode, fsync=fsync)


def set_aside(path: Path, why: str) -> Path | None:
    """Отложить битый файл рядом как «<имя>.corrupt-<время UTC>» и сказать в
    stderr. Старше CORRUPT_KEEP копий убираются. Ответ: куда отложен, None
    если переименовать не вышло (тогда файл на месте, следующая запись его
    заменит, как и раньше)."""
    path = Path(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.corrupt-{stamp}")
    if dest.exists():
        dest = path.with_name(f"{path.name}.corrupt-{stamp}-{os.getpid()}")
    try:
        os.replace(path, dest)
    except OSError as e:
        print(f"domovoy.statefile: {path} битый ({why}), отложить не вышло: {e}", file=sys.stderr)
        return None
    print(f"domovoy.statefile: {path} битый ({why}), отложен в {dest.name}, начинаю с пустого",
          file=sys.stderr)
    with contextlib.suppress(OSError):
        old = sorted(path.parent.glob(f"{path.name}.corrupt-*"))
        for extra in old[:-CORRUPT_KEEP]:
            extra.unlink()
    return dest


def read_json(path: Path, default=None, *, kind: type | tuple[type, ...] = dict,
              quarantine: bool = True, strict: bool = False, raise_unreadable: bool = False):
    """JSON из файла состояния. Нет файла или он пустой (оборванная запись
    прежнего вида): копия default. Не читается (права, ввод-вывод): default и
    строка в stderr, сам файл не трогается, но следующая запись вызывающего его
    заменит; raise_unreadable: OSError пробрасывается (сторож, которому пустое
    состояние стоит повторных пробуждений). Не UTF-8, не JSON или не kind:
    файл откладывается (quarantine) и ответ default. strict: вместо default
    StateError, файл остаётся на месте (его разберёт человек); пустой файл при
    strict тоже битый: тихий сброс защиты от повтора хуже остановки."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return copy.deepcopy(default)
    except OSError as e:
        if raise_unreadable:
            raise
        if strict:
            raise StateError(f"состояние {path} не прочитано: {type(e).__name__}") from None
        print(f"domovoy.statefile: {path} не читается: {e}", file=sys.stderr)
        return copy.deepcopy(default)
    if not raw.strip():
        if strict:
            raise StateError(f"состояние {path} пустое")
        return copy.deepcopy(default)
    try:
        value = json.loads(raw.decode("utf-8"))
        why = "" if isinstance(value, kind) else "не того вида"
    except ValueError as e:
        why = type(e).__name__
    if why:
        if strict:
            raise StateError(f"состояние {path} не прочитано: {why}")
        if quarantine:
            set_aside(path, why)
        else:
            print(f"domovoy.statefile: {path} битый ({why}), начинаю с пустого", file=sys.stderr)
        return copy.deepcopy(default)
    return value


def append_jsonl(path: Path, rec, *, mode: int = 0o600) -> None:
    """Одна строка JSON в конец файла одной записью (O_APPEND): параллельный
    писатель не разорвёт строку посередине."""
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def iter_jsonl(path: Path) -> Iterator:
    """Записи JSON-строк. Нет файла: ничего. Оборванный посреди символа UTF-8
    (живая расшифровка сессии) и битые строки пропускаются. Прочие ошибки
    чтения (права) пробрасываются: решает вызывающий."""
    try:
        f = Path(path).open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    with f:
        for line in f:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


@contextlib.contextmanager
def flock(path: Path, wait: float | None = None, *, poll: float = 0.1) -> Iterator[None]:
    """Исключительный замок на файле path (создаётся, каталог тоже).
    wait None: ждать без срока; число: ждать столько секунд (0 одна попытка),
    не дождались: TimeoutError (это OSError)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        if wait is None:
            fcntl.flock(f, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"замок {path} занят дольше {wait:g} с") from None
                    time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
