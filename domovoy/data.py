"""Запись общих данных Домового под замком синхронизации (ревью Codex 22.10.2026).

Журнал и таблицы `data/` пишут оба Домовых (личка и мастерская) и сторож.
Сторож пишет под `sync.data_lock()`, а инструменты Edit и Write агентов этот
замок не берут: строка, записанная посреди rebase синхронизации, пропадала
при его откате, а две сессии могли перетереть правки друг друга. Агент меняет
эти файлы только этой командой, текст подаётся на stdin (разделитель heredoc
в кавычках, чтобы shell не раскрывал `$` и обратные кавычки):

    cd ~/src/domovoy && .venv/bin/python -m domovoy.data append journal/2026-10.md <<'DOMOVOY_EOF'
    - 21:40 строка журнала
    DOMOVOY_EOF

    cd ~/src/domovoy && .venv/bin/python -m domovoy.data edit data/voronka.md <<'DOMOVOY_EOF'
    <<<<<<< old
    | Acme | ждём ответа |
    =======
    | Acme | отказ 22.10 |
    >>>>>>> new
    DOMOVOY_EOF

append дописывает в конец существующего каталога (новый файл создаёт и
говорит об этом). edit заменяет строки целиком: каждый old ищется в исходном
файле и обязан встретиться ровно один раз, блоки не пересекаются, применяются
все или ни одного. Текст сверяется с файлом под замком, поэтому устаревшее
чтение не перетрёт чужую правку. Концы строк и права файла сохраняются.

Граница защиты: бот отказывает Write и Edit по этим путям, но запись через
Bash (`>>`, `sed -i`) не ловит; это правило промта, а не запрет кодом.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

from .common import REPO_ROOT

# Что считается общими данными: те же пути, что коммитит синхронизация.
DATA_DIRS = ("journal", "data")
DATA_FILES = ("prompt.md", "diary.md")
_BLOCK_RE = re.compile(r"^<<<<<<< old\r?\n(.*?)^=======\r?\n(.*?)^>>>>>>> new(?:\r?\n|\Z)", re.S | re.M)
_MARKER_RE = re.compile(r"^(?:<<<<<<< old|=======|>>>>>>> new)\r?$", re.M)


class Refused(Exception):
    """Запись не сделана: путь не тот, текст не найден, замок не взят."""


def resolve(rel: str, root: Path = REPO_ROOT) -> Path:
    """Путь данных внутри репозитория: .md в journal/ или data/, либо prompt.md
    и diary.md. Выход наружу и символические ссылки в любом звене отклоняются."""
    root = root.resolve()
    raw = Path(rel)
    if raw.is_absolute():
        try:
            raw = raw.relative_to(root)
        except ValueError:
            raise Refused(f"domovoy.data: {rel} вне репозитория Домового") from None
    parts = raw.parts
    if not parts or ".." in parts:
        raise Refused(f"domovoy.data: {rel} вне репозитория Домового")
    ok = (len(parts) == 1 and parts[0] in DATA_FILES) or (
        len(parts) >= 2 and parts[0] in DATA_DIRS and raw.suffix == ".md")
    if not ok:
        raise Refused(f"domovoy.data: {rel} не общие данные (journal/*.md, data/*.md, prompt.md, diary.md)")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise Refused(f"domovoy.data: {rel}: символическая ссылка в пути, ничего не записано")
    return path


def parse_blocks(text: str) -> list[tuple[str, str]]:
    blocks = [(m.group(1), m.group(2)) for m in _BLOCK_RE.finditer(text)]
    if not blocks:
        raise Refused("domovoy.data: на stdin нет блоков «<<<<<<< old / ======= / >>>>>>> new», ничего не записано")
    if _BLOCK_RE.sub("", text).strip():
        raise Refused("domovoy.data: на stdin есть текст вне блоков замены, ничего не записано")
    for i, (old, new) in enumerate(blocks, 1):
        if _MARKER_RE.search(old) or _MARKER_RE.search(new):
            raise Refused(f"domovoy.data: блок {i}: строка-маркер внутри текста, ничего не записано")
    return blocks


def _excerpt(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:60]


def apply_edit(current: str, blocks: list[tuple[str, str]]) -> str:
    """Все old ищутся в исходном тексте, каждый ровно один раз (и с
    перекрытиями) и только с начала строки, диапазоны не пересекаются; замены
    с конца. Конец файла без перевода строки считается концом строки."""
    nl = "\r\n" if "\r\n" in current else "\n"
    missing_nl = bool(current) and not current.endswith("\n")
    if missing_nl:
        current += nl
    spans = []
    for i, (old, new) in enumerate(blocks, 1):
        if not old.strip():
            raise Refused(f"domovoy.data: блок {i}: пустой old, для дописывания есть append")
        found, start = [], current.find(old)
        while start != -1:
            # Только целые строки: «status: x» не находится внутри «new status: x».
            if start == 0 or current[start - 1] == "\n":
                found.append(start)
            start = current.find(old, start + 1)
        if len(found) != 1:
            hint = ("перечитай файл: строки целиком, без номеров строк Read" if not found
                    else "добавь соседние строки, чтобы текст стал единственным")
            raise Refused(f"domovoy.data: блок {i} («{_excerpt(old)}»): найден {len(found)} раз, нужно ровно 1; "
                          f"{hint}; ничего не записано")
        spans.append((found[0], found[0] + len(old), new, i))
    spans.sort()
    for (_s1, e1, _n1, i1), (s2, _e2, _n2, i2) in zip(spans, spans[1:]):
        if s2 < e1:
            raise Refused(f"domovoy.data: блоки {i1} и {i2} пересекаются, ничего не записано")
    out = current
    for start, end, new, _i in reversed(spans):
        out = out[:start] + new + out[end:]
    return out[:-len(nl)] if missing_nl and out.endswith(nl) else out


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            mode = path.stat().st_mode & 0o777
        else:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


def _git_stuck(root: Path) -> list[str]:
    """Незаконченный rebase или merge: дерево промежуточное, его откат стёр бы запись."""
    from . import sync
    if not (root / ".git").exists():
        return []
    # Каталог git как у синхронизации: в рабочей копии worktree .git это файл.
    try:
        gitdir = Path(sync.git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
    except Exception:
        gitdir = root / ".git"
    return [name for name in sync.STUCK_NAMES if (gitdir / name).exists()]


def run(cmd: str, rel: str, stdin: str, root: Path = REPO_ROOT, lock=None) -> str:
    """Одна запись под замком. Отдаёт строку итога, при отказе Refused."""
    from . import sync

    resolve(rel, root)
    blocks = parse_blocks(stdin) if cmd == "edit" else None
    if cmd == "append" and not stdin.strip():
        raise Refused("domovoy.data: пустой текст для дописывания")
    with (lock or sync.data_lock)() as locked:
        if not locked:
            raise Refused("domovoy.data: замок синхронизации занят, ничего не записано; "
                          "повтори ту же команду через минуту")
        stuck = _git_stuck(root.resolve())
        if stuck:
            raise Refused(f"domovoy.data: синхронизация стоит на разборе ({', '.join(stuck)}), "
                          "ничего не записано; скажи Ивану")
        # Путь заново под замком: каталог мог смениться после первой проверки.
        path = resolve(rel, root)
        if not path.parent.is_dir():
            raise Refused(f"domovoy.data: нет каталога {path.parent.name}, проверь путь; ничего не записано")
        if cmd == "append":
            created = not path.exists()
            current = "" if created else _read(path)
            nl = "\r\n" if "\r\n" in current else "\n"
            sep = "" if not current or current.endswith("\n") else nl
            body = stdin.rstrip("\r\n").replace("\r\n", "\n").replace("\n", nl)
            _write(path, current + sep + body + nl)
            return f"{'создан новый файл и дописано' if created else 'дописано'}: {rel}"
        if not path.exists():
            raise Refused(f"domovoy.data: {rel} нет, для нового файла есть append")
        _write(path, apply_edit(_read(path), blocks))
        return f"заменено: {rel}, блоков {len(blocks)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="domovoy.data")
    parser.add_argument("cmd", choices=("append", "edit"))
    parser.add_argument("path", help="путь от корня репозитория: journal/2026-10.md, data/voronka.md")
    args = parser.parse_args(argv)
    try:
        stdin = sys.stdin.buffer.read().decode("utf-8")
        print(run(args.cmd, args.path, stdin))
    except Refused as e:
        print(e, file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as e:
        # Запись атомарна: при любой ошибке файл прежний.
        print(f"domovoy.data: {type(e).__name__}: {e}; ничего не записано", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
