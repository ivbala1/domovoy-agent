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

Тексты правил (`prompt.md`, `diary.md`, шапки `data/*.md`) ран бота (есть
`CLAUDE_TG_CHAT_KEY`) не пишет: их правит настольная сессия по «ок» Ивана,
через ревью (сессии, запущенные ботом на ПК, эту переменную не получают).
`data/popravki.md` ран бота пишет. Запись, после которой тексты правил вместе
или поправки выходят за предел `promptparts`, отклоняется, если она их не
уменьшает.

Граница защиты: бот отказывает Write и Edit по этим путям, но запись через
Bash (`>>`, `sed -i`) не ловит; запрет здесь ограждает привычку, а не защищает.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import promptparts as pp
from .common import REPO_ROOT
from .statefile import write_atomic

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
    """Файл данных целиком или прежний, права прежнего файла сохраняются."""
    write_atomic(path, text, mode=None)


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


POPRAVKI = "data/popravki.md"


def check_rules(root: Path, path: Path, current: str, new: str) -> None:
    """Отказ, если запись меняет тексты правил там, где нельзя: ран бота правит
    prompt.md, diary.md или шапку таблицы; тексты правил вместе или поправки
    после записи за пределом и не уменьшились."""
    rel = path.relative_to(root).as_posix()
    if rel.startswith("journal/"):
        return
    rules = rel in DATA_FILES or rel == POPRAVKI or (
        bool(current) and pp.data_header(current) != pp.data_header(new))
    if not rules:
        return
    if os.environ.get("CLAUDE_TG_CHAT_KEY") and rel != POPRAVKI:
        raise Refused(f"domovoy.data: {rel}: тексты правил ран бота не правит (их правит настольная сессия "
                      "по «ок» Ивана); исполни поправку сейчас и запиши её строкой в data/popravki.md; "
                      "ничего не записано")
    before, after = pp.rules_total(root), pp.rules_total(root, {rel: new})
    if rel == "prompt.md":
        # Новая беда раскладки отклоняется, если правка не укорачивает файл.
        fresh = set(pp.problems(pp.split(new))) - set(pp.problems(pp.split(current)))
        if fresh and len(new.encode()) >= len(current.encode()):
            raise Refused(f"domovoy.data: prompt.md: {'; '.join(sorted(fresh))}; ничего не записано")
    if pp.rules_problems(after) and sum(after.values()) > sum(before.values()):
        hint = ("исполни поправку и запиши строкой журнала «Поправка Ивана: …»" if rel == POPRAVKI
                else "убери лишнее в той же правке или не вноси")
        raise Refused(f"domovoy.data: {'; '.join(pp.rules_problems(after))}; {hint}; ничего не записано")


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
            new = current + sep + body + nl
            check_rules(root.resolve(), path, current, new)
            _write(path, new)
            return f"{'создан новый файл и дописано' if created else 'дописано'}: {rel}"
        if not path.exists():
            raise Refused(f"domovoy.data: {rel} нет, для нового файла есть append")
        current = _read(path)
        new = apply_edit(current, blocks)
        check_rules(root.resolve(), path, current, new)
        _write(path, new)
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
