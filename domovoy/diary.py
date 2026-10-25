"""Дневник Ивана в Obsidian: запись дня, события, шапка, лента.

Что писать, решает Домовой по правилам diary.md. Этот модуль отвечает только
за сохранность: блок дописывается, а не переписывает день; повтор по метке не
дублируется; чужая правка между чтением и записью (Obsidian, Syncthing с
телефона и мака) не теряется; после записи папка дневника коммитится в git
хранилища. Путь записи перенесён из claude-tg-bot (diary.py) 04.10.2026, когда
дневник как отдельная машина был выключен.

Команды (python -m domovoy.diary ...):
  add     запись дня: пересказ из stdin, исходные слова после строки-разделителя
  fact    строка в блок «События дня»
  mark    люди и места в шапке дня
  rollup  сводка недели: текст из stdin, существующую не переписывает
  status  что записано за последние дни, каких сводок нет, всё ли в git
  sessions  реплики Ивана в других сессиях с агентами за день (ничего не пишет)
  feed    пересобрать ленту
  commit  закоммитить папку дневника (после правки файлов мимо этих команд)

Exit-коды: 0 записано или нечего делать, 1 ошибка, 2 неверные аргументы
(argparse), 3 уже записано: блок с такой меткой, те же исходные слова
целиком или готовая сводка недели.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from . import sessions
from .common import STATE_ROOT, TZ, WEEKDAYS_SHORT_RU, load_env
from .i18n import _, N_

# Хранилище Obsidian; на машине владельца путь в DOMOVOY_VAULT_DIR (.env).
DEFAULT_VAULT = Path.home() / "Obsidian"
DIARY_SUBDIR = "Дневник"
# До 04:00 пишем во вчерашний день: поздний вечер это ещё «сегодня» Ивана.
NIGHT_CUTOFF_HOUR = 4
FEED_DAYS = 14
ORIGINAL_SEPARATOR = "=== исходные слова ==="
# Тот же разделитель в английском шаблоне правил дневника (открытая копия).
ORIGINAL_SEPARATOR_EN = "=== original words ==="
EVENTS_OPEN = "<!-- machine:domovoy v1 -->"
EVENTS_CLOSE = "<!-- /machine:domovoy -->"
EVENTS_TITLE = N_("📌 События дня")
# Что уходит в git хранилища. Остальное хранилище Домовой не коммитит: там
# правки Ивана, и его незакоммиченное трогать нельзя. Подробности разбора
# недели (domovoy.review, решение Ивана 22.10.2026) с 26.10.2026 лежат в
# «Дневник/_Недели Домового» и входят в «Дневник» (до того «Домовой/Недели»).
COMMIT_PATHS = ("Дневник", "_attachments/дневник", "Люди")
GIT_TIMEOUT = 60
# Сколько git ждёт после SIGTERM по таймауту, прежде чем получить SIGKILL
# (ревью 11.10.2026).
GIT_TERM_GRACE = 10

OK, ERROR, DUPLICATE = 0, 1, 3

_LABEL_RE = re.compile(r"^[\w-]{1,40}$")
# Только ЧЧ:ММ из цифр ASCII в пределах суток, проверка fullmatch: прежняя
# \d{2}:\d{2} с $ пропускала 99:99, чужие цифры и перевод строки в конце
# (ревью 11.10.2026).
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_SCHEMA_DAY = "diary-day/v1"


def vault_dir() -> Path:
    # Команды дневника зовёт агент из рана бота: окружения Домового у него
    # нет, путь берётся из .env репозитория.
    load_env()
    return Path(os.environ.get("DOMOVOY_VAULT_DIR", "").strip() or DEFAULT_VAULT)


def diary_day(now: datetime) -> date:
    return (now - timedelta(hours=NIGHT_CUTOFF_HOUR)).date()


def day_file_path(diary_dir: Path, day: date) -> Path:
    return diary_dir / f"{day.year:04d}" / f"{day.isoformat()}.md"


def build_day_skeleton(day: date) -> str:
    """Шапка нового дня. Пустые поля означают «не размечено», не «не было».
    `фото: pending` читает слой Immich (domovoy.immich)."""
    return (
        "---\n"
        f"schema: {_SCHEMA_DAY}\n"
        "type: day\n"
        f"date: {day.isoformat()}\n"
        "sensitivity: private\n"
        "люди: []\n"
        "места: []\n"
        "фото: pending\n"
        "---\n"
    )


class DayFileChanged(RuntimeError):
    """Файл менялся при каждой попытке записи (Obsidian, Syncthing)."""


def _drop(tmp_name: str) -> None:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass


TMP_SUFFIX = ".tmp"
TMP_STALE_SEC = 600
# Общий замок всех своих писателей дневника (команды дневника и фото из
# Immich). Лежит на ext4 в каталоге состояния, а не в хранилище на drvfs:
# там flock не держится. Obsidian и Syncthing его не берут, от них по-прежнему
# только сверка перед подменой.
LOCK_FILE = STATE_ROOT / "diary.lock"
LOCK_WAIT_SEC = 30


@contextmanager
def write_lock(wait: float | None = None, lock_file: Path | None = None):
    """Замок на запись дня. Не дождались: TimeoutError (это OSError, и вызов
    отвечает «запись не удалась», как при любом сбое записи). lock_file
    другой только у замка git (git_lock, ревью 11.10.2026)."""
    wait = LOCK_WAIT_SEC if wait is None else wait
    lock_file = LOCK_FILE if lock_file is None else lock_file
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a") as f:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        _("замок дневника {lock_file} занят дольше {wait:g} с").format(lock_file=lock_file, wait=wait)
                    ) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def sweep_tmp(folder: Path) -> None:
    """Убрать временные файлы оборванных запусков. Живой запуск держит свой
    файл доли секунды, поэтому старше десяти минут это точно остаток: иначе
    полная копия дня висела бы в синхронизируемой папке."""
    try:
        for f in folder.glob(f"*.md.*{TMP_SUFFIX}"):
            if time.time() - f.stat().st_mtime > TMP_STALE_SEC:
                f.unlink()
    except OSError:
        pass


def rewrite(
    path: Path, transform, *, skeleton: str | None = None, retries: int = 3, errors: str = "strict"
) -> bool:
    """Переписать файл функцией transform(text) -> text. False = менять нечего.
    errors это обработка ошибок декодирования прежнего текста: "replace" только
    у производного файла, которому прежний текст не нужен (лента, ревью
    11.10.2026).

    Нет файла или он пустой (оборванная запись, синхронизация): начать со
    skeleton (None значит не создавать). Непустой файл без шапки (день,
    заведённый руками) остаётся как есть. Замена через mkstemp в том же
    каталоге и os.replace, никогда truncate-then-write. Свои писатели (команды
    дневника, фото) идут по очереди под общим замком write_lock. Перед
    подменой сверяются inode, mtime и размер: если файл за это время тронули
    (правка с телефона приехала через Syncthing), наша копия потеряла бы её,
    поэтому перечитываем и повторяем. Гонку с телефоном это сужает, а не
    закрывает: файловых замков у связки drvfs и Obsidian нет. Правка, пришедшая
    между последней сверкой и подменой, всё же будет затёрта; страховка от
    этого история в git хранилища и ночные снимки.
    """
    with write_lock():
        return _rewrite_locked(path, transform, skeleton, retries, errors)


def _rewrite_locked(path: Path, transform, skeleton: str | None, retries: int, errors: str = "strict") -> bool:
    sweep_tmp(path.parent)
    for _unused in range(retries):
        try:
            before = path.stat()
            current = path.read_text(encoding="utf-8", errors=errors)
        except FileNotFoundError:
            if skeleton is None:
                return False
            before = None
            current = skeleton
        if skeleton is not None and not current.strip():
            current = skeleton
        new_text = transform(current)
        if new_text == current:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=TMP_SUFFIX, dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as e:
                    # drvfs может не уметь fsync, и только это простительно:
                    # настоящая ошибка записи не должна кончиться подменой
                    # исправного файла ненадёжной копией.
                    if e.errno not in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
                        raise
            if before is None:
                changed = path.exists()
            else:
                now = path.stat()
                changed = (now.st_ino, now.st_mtime_ns, now.st_size) != (
                    before.st_ino, before.st_mtime_ns, before.st_size,
                )
            if changed:
                _drop(tmp_name)
                time.sleep(0.05)
                continue
            os.replace(tmp_name, path)
            return True
        except Exception:
            _drop(tmp_name)
            raise
    raise DayFileChanged(_("{name}: файл менялся {retries} раза подряд во время записи").format(name=path.name, retries=retries))


# --- блок записи ---------------------------------------------------------


def block_sha(text: str) -> str:
    """Метка sha в блоке информационная: ничто её не сверяет. После точечной
    правки опечатки (diary.md, с 07.10.2026) она расходится с текстом, и это
    нормально; формат блоков ради неё не меняется."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def quote_callout(original: str) -> str:
    """Исходные слова свёрнутой выноской: при чтении не мешают, но пересказ
    всегда можно сверить с тем, что было сказано."""
    lines = original.strip().splitlines()
    body = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in lines)
    return _("> [!quote]- Исходные слова\n{body}\n").format(body=body)


def build_block(
    header: str, marker: str, text: str, kind: str, logged: datetime, original: str = ""
) -> str:
    """Блок записи. Маркер отдельной строкой под заголовком: внутри строки
    заголовка Obsidian печатает HTML-комментарий жирным на весь экран."""
    text = text.strip()
    out = (
        f"###### {header}\n"
        f"<!-- {marker} sha:{block_sha(text)} kind:{kind} logged:{logged:%Y-%m-%dT%H:%M} -->\n"
        f"{text}\n"
    )
    if original.strip():
        out += "\n" + quote_callout(original)
    return out


def has_callout(day_text: str, original: str) -> bool:
    """Эти исходные слова уже лежат выноской целиком. Подстроки мало: короткая
    выноска «устал» входит в начало более длинной «устал / лёг спать», и
    новый рассказ принимался бы за повтор. Выноска считается найденной, только
    если за ней не идёт ещё строка той же выноски."""
    callout = quote_callout(original)
    pattern = r"(?m)^" + re.escape(callout[:-1]) + r"(?:\n(?!>)|\Z)"
    return re.search(pattern, day_text) is not None


def has_marker(day_text: str, marker: str) -> bool:
    # С пробелом после: метка «день» не должна совпасть с «день-2».
    return f"<!-- {marker} " in day_text


# Машинные блоки (события, фото) живут в хвосте файла. Запись, дописанная
# после них, разрывала бы хвост и при чтении прилипала к машинному тексту.
_MACHINE_START_RE = re.compile(r"^<!-- machine:", re.MULTILINE)


def append_block_text(day_text: str, block: str) -> str:
    """Дописать блок записи перед машинным хвостом, если он есть."""
    m = _MACHINE_START_RE.search(day_text)
    if m is None:
        return f"{day_text.rstrip(chr(10))}\n\n{block}"
    head = day_text[: m.start()].rstrip("\n")
    return f"{head}\n\n{block.rstrip()}\n\n{day_text[m.start():]}"


# --- события дня ---------------------------------------------------------

_EVENTS_RE = re.compile(
    re.escape(EVENTS_OPEN) + r"\n(.*?)" + re.escape(EVENTS_CLOSE) + r"\n?", re.S
)


def event_key(line: str) -> str:
    """Строка события для сверки повторов: целиком, с временем и источником.
    Границы «текст / время / источник» в готовой строке не угадываем: по
    вхождению «оплатил» терялось после «не оплатил», а при отбрасывании
    скобок «Встреча (утром)» совпадала со «Встреча (вечером)»."""
    return " ".join(line.strip().split()).casefold()


def add_event_line(day_text: str, when: str, text: str, source: str = "") -> str:
    """Строка в блок «События дня». То же событие второй раз не пишется:
    пробуждение, прерванное после записи, при повторе не должно двоить.
    Повтором считается только та же строка целиком."""
    text = " ".join(text.split())
    entry = "- " + (f"{when} " if when else "") + text + (f" ({source})" if source else "")
    m = _EVENTS_RE.search(day_text)
    if m is not None:
        if event_key(entry) in {event_key(ln) for ln in m.group(1).splitlines() if ln.startswith("- ")}:
            return day_text
        body = m.group(1).rstrip("\n") + "\n" + entry + "\n"
        return (
            day_text[: m.start()] + EVENTS_OPEN + "\n" + body + EVENTS_CLOSE + "\n"
            + day_text[m.end():]
        )
    block = f"{EVENTS_OPEN}\n{_(EVENTS_TITLE)}\n{entry}\n{EVENTS_CLOSE}\n"
    m2 = _MACHINE_START_RE.search(day_text)
    if m2 is None:
        return f"{day_text.rstrip(chr(10))}\n\n{block}"
    head = day_text[: m2.start()].rstrip("\n")
    return f"{head}\n\n{block}\n{day_text[m2.start():]}"


def event_lines(day_text: str) -> list[str]:
    m = _EVENTS_RE.search(day_text)
    if m is None:
        return []
    return [ln for ln in m.group(1).splitlines() if ln.startswith("- ")]


# --- списки в шапке ------------------------------------------------------
# Редактор свойств Obsidian переписывает инлайн-список (`люди: ["Аня"]`) в
# блочный (`люди:` и пункты `  - Аня`). Оба вида валидны; код, знавший
# только первый, считал блочное поле пустым и ломал YAML вставкой.
_FM_RE = re.compile(r"\A(---\n)(.*?\n)(---\n)", re.S)


def _fm_item(value: str) -> str:
    """Пункт списка всегда в двойных кавычках с экранированием. Без кавычек
    YAML читает `null` и `true` не как строки, `*дом` как ссылку на якорь, а
    запятая, двоеточие или кавычка внутри названия ломают шапку."""
    return json.dumps(value, ensure_ascii=False)


def _fm_key_span(lines: list[str], key: str) -> tuple[int, int, str] | None:
    """(индекс строки ключа, индекс за последним пунктом, inline-остаток)."""
    prefix = f"{key}:"
    for i, ln in enumerate(lines):
        if not ln.startswith(prefix):
            continue
        rest = ln[len(prefix):].strip()
        if rest:
            return i, i + 1, rest
        end = i + 1
        while end < len(lines) and lines[end].strip().startswith("- "):
            end += 1
        return i, end, ""
    return None


def _parse_scalar(raw: str) -> str:
    """Значение пункта списка: снять кавычки YAML вместе с экранированием."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return str(json.loads(raw))
        except json.JSONDecodeError:
            return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _split_inline(body: str) -> list[str]:
    """Пункты инлайн-списка: запятая внутри кавычек разделителем не считается,
    иначе место «Тольятти, парк» при следующей правке распалось бы на два."""
    out: list[str] = []
    cur: list[str] = []
    quote = ""
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            cur.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < len(body):
                cur.append(body[i + 1])       # экранированный знак целиком
                i += 1
            elif ch == quote:
                if quote == "'" and body[i + 1:i + 2] == "'":
                    cur.append("'")           # '' внутри одинарных кавычек это апостроф
                    i += 1
                else:
                    quote = ""
        elif ch in "\"'" and not "".join(cur).strip():
            # Кавычка открывает строку только в начале значения: апостроф в
            # середине (O'Brien) это обычная буква.
            quote = ch
            cur.append(ch)
        elif ch == ",":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [p for p in out if p.strip()]


def _has_comment(value: str) -> bool:
    """В значении есть комментарий YAML: «#» в начале или после пробела, вне
    кавычек. Кавычка открывает строку только в начале значения, как в
    _split_inline (ревью 11.10.2026)."""
    quote = ""
    start = True
    i = 0
    while i < len(value):
        ch = value[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 1
            elif ch == quote:
                if quote == "'" and value[i + 1:i + 2] == "'":
                    i += 1
                else:
                    quote = ""
        elif ch == "#" and (i == 0 or value[i - 1] in " \t"):
            return True
        elif ch in "\"'" and start:
            quote = ch
        elif ch in "[,":
            start = True
        elif ch not in " \t":
            start = False
        i += 1
    return False


def fm_list_unsupported(text: str, key: str) -> bool:
    """Список шапки в виде, который код переписать не умеет: комментарий YAML
    после ключа, после инлайн-списка, у пункта или строкой среди пунктов.
    Такой список не трогаем: правка принимала комментарий за значение и
    ломала шапку или вписывала «# ...» в имя (ревью 11.10.2026, X-C2)."""
    m = _FM_RE.match(text)
    if m is None:
        return False
    lines = m.group(2).splitlines()
    span = _fm_key_span(lines, key)
    if span is None:
        return False
    i, end, rest = span
    if _has_comment(rest) or any(_has_comment(ln.strip()[2:]) for ln in lines[i + 1:end]):
        return True
    # Строка-комментарий или пустая строка, а за ней снова пункты: они не
    # вошли бы в заменяемый участок.
    j = end
    while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
        j += 1
    return j > end and j < len(lines) and lines[j].strip().startswith("- ")


def has_frontmatter(text: str) -> bool:
    return _FM_RE.match(text) is not None


def read_fm_list(text: str, key: str) -> list[str]:
    m = _FM_RE.match(text)
    if m is None:
        return []
    lines = m.group(2).splitlines()
    span = _fm_key_span(lines, key)
    if span is None:
        return []
    i, end, rest = span
    if rest.startswith("[") and rest.endswith("]"):
        return [_parse_scalar(p) for p in _split_inline(rest[1:-1])]
    if rest:
        return [_parse_scalar(rest)]
    return [_parse_scalar(ln.strip()[2:]) for ln in lines[i + 1:end]]


def write_fm_list(text: str, key: str, values: list[str]) -> str:
    """Переписать список шапки, сохранив вид записи. Ключа в шапке нет (день
    заведён руками или давно): дописать его. Шапки нет вовсе: вернуть как есть."""
    m = _FM_RE.match(text)
    if m is None:
        return text
    lines = m.group(2).splitlines()
    span = _fm_key_span(lines, key)
    # Инлайн-пункты всегда в двойных кавычках с экранированием: это валидный
    # YAML и для запятых, и для кавычек внутри названия.
    inline = [f"{key}: [{', '.join(_fm_item(v) for v in values)}]"]
    if span is None:
        i = end = len(lines)
        block = inline
    else:
        i, end, rest = span
        if not rest and end > i + 1:  # блочный вид, сохраняем его
            indent = lines[i + 1][: len(lines[i + 1]) - len(lines[i + 1].lstrip())] or "  "
            block = [f"{key}:"] + [f"{indent}- {_fm_item(v)}" for v in values]
        else:
            block = inline
    new_fm = "\n".join(lines[:i] + block + lines[end:]) + "\n"
    return m.group(1) + new_fm + m.group(3) + text[m.end():]


def add_fm_values(day_text: str, key: str, values: list[str]) -> str:
    """Дописать значения в список шапки. Только добавляет: проставленное
    раньше (человеком или прошлой записью) не убирается. Список с
    комментарием YAML остаётся как есть (fm_list_unsupported)."""
    if fm_list_unsupported(day_text, key):
        return day_text
    items = read_fm_list(day_text, key)
    for v in values:
        v = " ".join(v.split())   # перевод строки внутри значения сломал бы шапку
        if v and v not in items:
            items.append(v)
    if items == read_fm_list(day_text, key):
        return day_text
    return write_fm_list(day_text, key, items)


def has_person_card(vault: Path, name: str) -> bool:
    """Списки «когда виделись» в карточках ищут имя ровно как назван файл
    карточки, поэтому имя без карточки стоит назвать вслух. Близкие живут
    своей папкой в «Семья» (решение Ивана 25.10.2026), раньше в корне."""
    return any(p.is_file() for p in (vault / "Люди" / f"{name}.md",
                                     vault / "Семья" / name / f"{name}.md",
                                     vault / name / f"{name}.md"))


# --- лента ---------------------------------------------------------------


def list_days(diary_dir: Path) -> list[date]:
    out: list[date] = []
    for year_dir in diary_dir.glob("[0-9][0-9][0-9][0-9]"):
        for f in year_dir.glob("*.md"):
            try:
                out.append(date.fromisoformat(f.stem))
            except ValueError:
                continue
    return sorted(out)


def render_feed(days: list[date]) -> str:
    """Лента целиком из списка файлов, новое сверху. Дни встроены как есть
    (вместе с фото), поэтому ленту не надо обновлять при правке самого дня."""
    lines = [
        "---",
        "schema: diary-feed/v2",
        "type: feed",
        "sensitivity: private",
        "---",
        _("# 📜 Лента"),
        "",
        _("Последние {days} дней с записями, новое сверху. Файл пересобирает Домовой: "
          "правки в нём затрутся, писать надо в день. Хаб: {hub}.").format(
            days=FEED_DAYS, hub=f"[[{DIARY_SUBDIR}/{DIARY_SUBDIR}|{_('Дневник')}]]"),
        "",
    ]
    for d in sorted(days, reverse=True)[:FEED_DAYS]:
        link = f"{DIARY_SUBDIR}/{d.year:04d}/{d.isoformat()}"
        lines.append(f"## [[{link}|{d:%d.%m.%Y}, {_(WEEKDAYS_SHORT_RU[d.weekday()])}]]")
        lines.append(f"![[{link}]]")
        lines.append("")
    return "\n".join(lines)


def write_feed(diary_dir: Path) -> bool:
    # Список дней собирается внутри преобразования, то есть под замком и
    # заново при каждой попытке: собранный до замка, он затирал ленту, которую
    # тем временем пересобрал запуск с новым днём (X-C4). Прежняя лента не
    # нужна, поэтому читается без строгой кодировки: битая (не UTF-8)
    # пересобирается, а не роняет запись (C38). Ревью 11.10.2026.
    return rewrite(
        diary_dir / "Лента.md", lambda _old: render_feed(list_days(diary_dir)), skeleton="", errors="replace"
    )


# --- что записано --------------------------------------------------------

_PHOTO_RE = re.compile(r"^фото:\s*(\S+)\s*$", re.MULTILINE)


# Первая неделя, сводку которой пишет Домовой (до неё их писал бот).
ROLLUPS_SINCE = date(2026, 9, 28)
ROLLUP_MIN_BYTES = 50   # меньше это оборванная запись, а не сводка
_MACHINE_BLOCK_RE = re.compile(r"<!-- machine:([\w-]+)[^>]*-->.*?<!-- /machine:\1 -->\n?", re.S)


def week_file(monday: date) -> str:
    year, week, _weekday = monday.isocalendar()
    return f"{year}-W{week:02d}.md"


def last_week(today: date) -> tuple[date, str]:
    """(понедельник прошлой недели, имя её файла сводки)."""
    monday = today - timedelta(days=today.weekday() + 7)
    return monday, week_file(monday)


def rollup_done(diary_dir: Path, monday: date) -> bool:
    try:
        return (diary_dir / "_Роллапы" / week_file(monday)).stat().st_size >= ROLLUP_MIN_BYTES
    except OSError:
        return False


def missing_rollups(diary_dir: Path, today: date) -> list[date]:
    """Понедельники всех закончившихся недель без сводки, в которых есть хотя
    бы один день с файлом. Не только прошлая неделя: после двух недель
    простоя более старая пропущенная сводка иначе забылась бы навсегда."""
    days = set(list_days(diary_dir))
    out: list[date] = []
    monday = ROLLUPS_SINCE
    this_monday = today - timedelta(days=today.weekday())
    while monday < this_monday:
        if any(monday + timedelta(days=i) in days for i in range(7)) and not rollup_done(diary_dir, monday):
            out.append(monday)
        monday += timedelta(days=7)
    return out


def hand_written(day_text: str) -> bool:
    """В дне есть текст вне машинных блоков: Иван мог писать руками, без
    пометки `src:`, и такой день нельзя считать пустым."""
    m = _FM_RE.match(day_text)
    body = day_text[m.end():] if m else day_text
    return bool(_MACHINE_BLOCK_RE.sub("", body).strip())


def git_debt(vault: Path) -> str:
    """Что в папках дневника записано, но не закоммичено ('' если всё в git).

    Ревью 11.10.2026: проверка под замком git, поэтому index.lock здесь не
    замок идущего коммита фото или записи, а остаток; status без
    необязательных замков; все пути, а не только существующие, иначе
    удалённый целиком каталог не считался долгом; сбой или таймаут git это
    не «долга нет», а строка с просьбой выполнить commit."""
    if not (vault / ".git").exists():
        return ""
    try:
        with git_lock():
            if (vault / ".git" / "index.lock").exists():
                return _("в хранилище остался .git/index.lock: коммиты не проходят, сказать владельцу")
            r = run_git(
                vault, "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all",
                "--", *COMMIT_PATHS, *_NO_TMP,
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        return _("git не проверен ({error}): выполнить commit").format(error=type(e).__name__)
    if r.returncode != 0:
        return _("git не проверен ({error}): выполнить commit").format(error=" ".join(r.stderr.split())[:200])
    return _("есть незакоммиченные изменения дневника: выполнить commit") if r.stdout.strip() else ""


def status_lines(diary_dir: Path, today: date, days: int) -> list[str]:
    """Сохранённый результат, а не память агента, говорит, что уже сделано:
    по этим строкам пробуждение доделывает пропущенное."""
    out: list[str] = []
    for i in range(days):
        d = today - timedelta(days=i)
        name = f"{d.isoformat()} {_(WEEKDAYS_SHORT_RU[d.weekday()])}"
        try:
            text = day_file_path(diary_dir, d).read_text(encoding="utf-8")
        except FileNotFoundError:
            out.append(_("{name}: файла дня нет").format(name=name))
            continue
        except UnicodeDecodeError:
            # Обрыв синхронизации с телефона: один битый день не должен
            # прятать строки остальных.
            out.append(_("{name}: файл дня не читается (не UTF-8), сказать владельцу").format(name=name))
            continue
        photo = _PHOTO_RE.search(text)
        people = ", ".join(read_fm_list(text, "люди")) or _("нет")
        marked = text.count("<!-- src:")
        by_hand = _(" (но есть текст, записанный руками: прочитать день)") if not marked and hand_written(text) else ""
        out.append(
            _("{name}: записей {marked}{by_hand}, событий {events}, люди: {people}, фото: {photo}").format(
                name=name, marked=marked, by_hand=by_hand, events=len(event_lines(text)),
                people=people, photo=photo.group(1) if photo else _("нет поля"),
            )
        )
    missing = missing_rollups(diary_dir, today)
    for monday in missing:
        out.append(_("сводка недели с {start}: {file} НЕТ, написать (команда rollup)").format(
            start=f"{monday:%d.%m}", file=week_file(monday)))
    if not missing:
        out.append(_("сводки недель: все на месте"))
    debt = git_debt(diary_dir.parent)
    if debt:
        out.append(_("git: {debt}").format(debt=debt))
    return out


# --- git хранилища -------------------------------------------------------

# Остаток оборванной записи (полная копия дня) в историю попадать не должен.
_NO_TMP = (":(exclude,glob)**/*.tmp",)

# Ответы commit, после которых команде commit нечего доделывать (ревью
# 11.10.2026: раньше она отвечала 0 и при сбое).
COMMITTED = N_("закоммичено")
NOTHING_TO_COMMIT = N_("нечего коммитить")
NO_GIT = N_("git в хранилище нет, коммит пропущен")


def git_lock():
    """Замок git хранилища (ревью 11.10.2026): коммиты дневника и фото и
    проверка git_debt идут по очереди, а не наперегонки за .git/index.lock.
    Отдельный от замка записи, рядом с ним: медленный git на drvfs не должен
    задерживать запись дня. Ждём не дольше одной команды git."""
    return write_lock(GIT_TIMEOUT, LOCK_FILE.with_name("diary-git.lock"))


def run_git(vault: Path, *args: str) -> subprocess.CompletedProcess:
    """git хранилища с таймаутом. Ревью 11.10.2026: по таймауту SIGTERM, а не
    SIGKILL, как у subprocess.run. На SIGTERM git сам убирает свой
    .git/index.lock, после SIGKILL замок оставался навсегда и останавливал
    все коммиты. Сигнал идёт всей группе процессов: хук git тоже должен
    кончиться. Не кончился за GIT_TERM_GRACE: SIGKILL. При прерывании или
    другом исключении тоже завершаем всю группу и ждём git (ревью 11.10.2026)."""
    proc = subprocess.Popen(
        ["git", "-C", str(vault), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=GIT_TIMEOUT)
    except BaseException:
        # Завершение git не означает завершение его хука (ревью 11.10.2026).
        # Проверяем всю группу, а родителя попутно забираем, чтобы его зомби
        # не продлевал ожидание. Замок нельзя отпустить с работающим git.
        try:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + GIT_TERM_GRACE
            while True:
                proc.poll()
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # Отказ проверки не доказывает, что группа исчезла.
                    pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                time.sleep(min(0.05, remaining))
            proc.wait()
        finally:
            proc.stdout.close()
            proc.stderr.close()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _status_paths(out: str) -> tuple[set[str], set[str], set[str]]:
    """Из `git status --porcelain -z` (ревью 11.10.2026): пути COMMIT_PATHS,
    где есть изменения (в том числе удаления, и уже подготовленные в индексе),
    где из них есть изменения в рабочем дереве, и где есть новые файлы."""
    changed: set[str] = set()
    dirty: set[str] = set()
    fresh: set[str] = set()
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        names = [entry[3:]]
        if entry[0] in "RC" or entry[1] in "RC":   # переименование: следом старое имя
            names.append(fields[i] if i < len(fields) else "")
            i += 1
        for n, name in enumerate(names):
            for p in COMMIT_PATHS:
                if name == p or name.startswith(p + "/"):
                    changed.add(p)
                    if n == 0 and entry[1] != " ":
                        dirty.add(p)
                    if entry.startswith("??"):
                        fresh.add(p)
    return changed, dirty, fresh


def commit(vault: Path, message: str) -> str:
    """Закоммитить папку дневника в git хранилища. Наружу ничего не уходит:
    отправка там закрыта хуком, история нужна, чтобы откатить порчу файла.
    Сбой коммита запись не отменяет, возвращается строкой для отчёта."""
    if not (vault / ".git").exists():
        return NO_GIT

    def git(*args: str) -> subprocess.CompletedProcess:
        return run_git(vault, *args)

    try:
        with git_lock():
            # Пути коммита это те, где git видит изменения, а не те, что
            # есть на диске (ревью 11.10.2026): каталог без известных git
            # файлов (пустой, из одних .tmp или игнорируемых) давал
            # «pathspec did not match» и ломал каждый коммит, а удалённый
            # целиком каталог в коммит не попадал.
            r = git("status", "--porcelain", "-z", "--untracked-files=all", "--", *COMMIT_PATHS, *_NO_TMP)
            if r.returncode != 0:
                return _("git status не прошёл: {error}").format(error=r.stderr.strip()[:200])
            changed, dirty, fresh = _status_paths(r.stdout)
            paths = [p for p in COMMIT_PATHS if p in changed]
            if not paths:
                return NOTHING_TO_COMMIT
            # Каталог с новыми файлами добавляется целиком; каталог только с
            # уже известными файлами через -u: так он не спотыкается о
            # .gitignore, если сам игнорируется, а файлы в нём уже в git.
            # Каталог без изменений в рабочем дереве (удаление уже в индексе)
            # в add не идёт: там его pathspec ничему не соответствует.
            for flags, group in (((), [p for p in paths if p in fresh]),
                                 (("-u",), [p for p in paths if p in dirty and p not in fresh])):
                if group:
                    r = git("add", *flags, "--", *group, *_NO_TMP)
                    if r.returncode != 0:
                        return _("git add не прошёл: {error}").format(error=r.stderr.strip()[:200])
            if git("diff", "--cached", "--quiet", "--", *paths, *_NO_TMP).returncode == 0:
                return NOTHING_TO_COMMIT
            # С путями после «--» в коммит идут только они: чужое, что уже лежит
            # в индексе, остаётся как было.
            r = git("commit", "-q", "-m", message, "--", *paths, *_NO_TMP)
            if r.returncode != 0:
                return _("git commit не прошёл: {error}").format(error=(r.stderr or r.stdout).strip()[:200])
    except TimeoutError as e:
        return _("git занят другим запуском (фото или запись), коммит отложен: {error}").format(error=e)
    except (OSError, subprocess.TimeoutExpired) as e:
        return _("git недоступен: {error}").format(error=type(e).__name__)
    return COMMITTED


# --- сводка недели -------------------------------------------------------

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def build_rollup(monday: date, body: str) -> str:
    sunday = monday + timedelta(days=6)
    return (
        "---\n"
        "schema: diary-rollup/v1\n"
        "type: rollup\n"
        f"week: {week_file(monday)[:-3]}\n"
        f"start: {monday.isoformat()}\n"
        f"end: {sunday.isoformat()}\n"
        "sensitivity: private\n"
        "---\n\n"
        + _("# Неделя {start} - {end}").format(start=f"{monday:%d.%m}", end=f"{sunday:%d.%m.%Y}")
        + f"\n\n{body.strip()}\n"
    )


# --- команды -------------------------------------------------------------


class BadDate(ValueError):
    """Неверная дата или неделя в аргументах. Только её main называет «не
    понял дату»: прежде так отвечал любой ValueError, и агент чинил верную
    дату по кругу (ревью 11.10.2026)."""


def _parse_day(raw: str, now: datetime) -> date:
    try:
        return date.fromisoformat(raw) if raw else diary_day(now)
    except ValueError as e:
        raise BadDate(str(e)) from None


def _commit_text(result: str) -> str:
    """Ответ commit для показа: постоянные ответы переводятся здесь, чтобы
    main сравнивал их по исходному тексту."""
    return _(result) if result in (COMMITTED, NOTHING_TO_COMMIT, NO_GIT) else result


def _after_write(vault: Path, message: str) -> None:
    diary_dir = vault / DIARY_SUBDIR
    try:
        write_feed(diary_dir)
    except Exception as e:
        # Лента производная: никакой её сбой не меняет ответ уже сделанной
        # записи и не отменяет коммит (ревью 11.10.2026).
        print(_("⚠️ лента не пересобрана: {error}").format(error=e), file=sys.stderr)
    print(_("git: {result}").format(result=_commit_text(commit(vault, message))))


def _warn_cards(vault: Path, people: list[str]) -> None:
    for name in people:
        if name.strip() and not has_person_card(vault, name.strip()):
            print(_("⚠️ у «{name}» нет карточки с таким именем: в списки «когда виделись» "
                    "день не попадёт. Имя должно совпадать с именем файла карточки.").format(name=name.strip()))


def cmd_add(args, vault: Path, now: datetime) -> int:
    if not _LABEL_RE.match(args.label):
        print(_("метка: буквы, цифры, дефис, до 40 знаков"), file=sys.stderr)
        return ERROR
    if args.time and not _TIME_RE.fullmatch(args.time):
        print(_("--time в виде ЧЧ:ММ"), file=sys.stderr)
        return ERROR
    raw = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else sys.stdin.read()
    sep = ORIGINAL_SEPARATOR if ORIGINAL_SEPARATOR in raw else ORIGINAL_SEPARATOR_EN
    text, _sep, original = raw.partition(sep)
    if args.original_file:
        original = Path(args.original_file).read_text(encoding="utf-8")
    if not text.strip():
        print(_("пустой текст записи"), file=sys.stderr)
        return ERROR

    day = _parse_day(args.day, now)
    if day == diary_day(now):
        header = args.time or f"{now:%H:%M}"
    else:
        # Время события в чужом дне неизвестно, если его не назвали: честно
        # помечаем, когда запись сделана.
        header = _("{time} (записано {date})").format(time=args.time or _("позже"), date=f"{now:%d.%m}")
    marker = f"src:domovoy:{day.isoformat()}:{args.label}"
    args.commit_message = f"дневник: {day.isoformat()} {args.label}"
    block = build_block(header, marker, text, args.kind, now, original)
    path = day_file_path(vault / DIARY_SUBDIR, day)

    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    if has_marker(existing, marker):
        start = existing.index(f"<!-- {marker} ")
        # Файл может кончаться строкой метки без перевода строки (оборванная
        # синхронизация): тогда превью пустое, а не ValueError (ревью 11.10.2026).
        eol = existing.find("\n", start)
        preview = " ".join(existing[eol + 1:].split())[:120] if eol >= 0 else ""
        print(_("блок с меткой «{label}» за {day} уже есть, ничего не записано. Он начинается так: «{preview}». "
                "Это тот же рассказ: делать ничего не надо. Другой рассказ или дополнение: взять другую метку.").format(
                    label=args.label, day=day, preview=preview))
        return DUPLICATE
    if original.strip() and has_callout(existing, original):
        # Те же исходные слова под другой меткой: агент после перезапуска
        # забыл, какую метку брал, а рассказ тот же.
        print(_("эти исходные слова за {day} уже записаны под другой меткой, ничего не записано.").format(day=day))
        return DUPLICATE

    same_original = False
    processed = ""

    def transform(day_text: str) -> str:
        nonlocal same_original, processed
        # Предупреждение относится к версии последней попытки под замком,
        # а не к прочитанной до ожидания шапке (ревью 11.10.2026).
        processed = day_text
        if has_marker(day_text, marker):
            return day_text
        # Исходные слова перепроверяются под замком, как и метка: параллельный
        # запуск с теми же словами и другой меткой мог записать их между
        # проверкой выше и замком (ревью 11.10.2026, X-C3).
        same_original = bool(original.strip()) and has_callout(day_text, original)
        if same_original:
            return day_text
        new_text = append_block_text(day_text, block)
        new_text = add_fm_values(new_text, "люди", args.people)
        return add_fm_values(new_text, "места", args.places)

    if args.dry_run:
        print(_("[dry-run] {path}\n{block}").format(path=path, block=block))
        return OK
    if not rewrite(path, transform, skeleton=build_day_skeleton(day)):
        if same_original:
            print(_("эти исходные слова за {day} уже записаны под другой меткой, ничего не записано.").format(day=day))
            return DUPLICATE
        # Метка появилась между проверкой и записью: параллельный запуск.
        print(_("блок с меткой «{label}» за {day} уже есть, ничего не записано.").format(label=args.label, day=day))
        return DUPLICATE
    print(_("записано: {path}").format(path=path))
    if (args.people or args.places) and not has_frontmatter(processed):
        print(_("⚠️ у файла дня нет шапки (заведён руками): люди и места не записаны, сказать владельцу"))
    for key, values in (("люди", args.people), ("места", args.places)):
        if values and fm_list_unsupported(processed, key):
            print(_("⚠️ список «{key}» в шапке дня в непонятном виде (комментарий YAML): "
                    "{key} не записаны, сказать владельцу").format(key=key))
    _warn_cards(vault, args.people)
    return OK


def cmd_rollup(args, vault: Path, now: datetime) -> int:
    m = _WEEK_RE.match(args.week)
    if not m:
        print(_("--week в виде ГГГГ-Wнн, как называет status"), file=sys.stderr)
        return ERROR
    try:
        monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError as e:
        raise BadDate(str(e)) from None
    body = sys.stdin.read()
    if not body.strip():
        print(_("пустой текст сводки"), file=sys.stderr)
        return ERROR
    diary_dir = vault / DIARY_SUBDIR
    path = diary_dir / "_Роллапы" / week_file(monday)
    args.commit_message = f"дневник: сводка недели {args.week}"
    if rollup_done(diary_dir, monday):
        print(_("сводка {week} уже есть, не переписана: {path}").format(week=args.week, path=path))
        return DUPLICATE
    text = build_rollup(monday, body)
    if args.dry_run:
        print(_("[dry-run] {path}\n{text}").format(path=path, text=text))
        return OK
    def transform(old: str) -> str:
        # Проверка готовности здесь, а не только выше: между той проверкой и
        # записью сводку мог положить другой запуск, и её нельзя затереть.
        # Оборванный пустой файл заменяется.
        return old if len(old.encode("utf-8")) >= ROLLUP_MIN_BYTES else text

    if not rewrite(path, transform, skeleton=""):
        print(_("сводка {week} уже есть, не переписана: {path}").format(week=args.week, path=path))
        return DUPLICATE
    print(_("сводка записана: {path}").format(path=path))
    return OK


def cmd_fact(args, vault: Path, now: datetime) -> int:
    if args.time and not _TIME_RE.fullmatch(args.time):
        print(_("--time в виде ЧЧ:ММ"), file=sys.stderr)
        return ERROR
    if not args.text.strip():
        print(_("пустой текст события"), file=sys.stderr)
        return ERROR
    day = _parse_day(args.day, now)
    # Время ставится, только если его назвали: «утром оплатил страховку»,
    # сказанное в 20:45, не должно стать событием в 20:45.
    when = args.time
    path = day_file_path(vault / DIARY_SUBDIR, day)
    args.commit_message = f"дневник: событие {day.isoformat()}"
    if args.dry_run:
        print(_("[dry-run] {path}: {when} {text} ({source})").format(path=path, when=when, text=args.text, source=args.source))
        return OK
    changed = rewrite(
        path, lambda t: add_event_line(t, when, args.text, args.source),
        skeleton=build_day_skeleton(day),
    )
    if not changed:
        print(_("такое событие за этот день уже записано"))
        return OK
    print(_("событие записано: {path}").format(path=path))
    return OK


def cmd_mark(args, vault: Path, now: datetime) -> int:
    day = _parse_day(args.day, now)
    path = day_file_path(vault / DIARY_SUBDIR, day)
    args.commit_message = f"дневник: люди и места {day.isoformat()}"
    if not path.is_file():
        print(_("файла дня {day} нет: сначала запись (add) или событие (fact)").format(day=day), file=sys.stderr)
        return ERROR

    def header_error(text: str) -> str:
        if not has_frontmatter(text):
            return _("у файла дня {day} нет шапки (заведён руками): люди и места записать некуда, сказать владельцу").format(day=day)
        bad = [key for key, values in (("люди", args.people), ("места", args.places))
               if values and fm_list_unsupported(text, key)]
        if bad:
            return _("в шапке дня {day} список «{keys}» в непонятном виде (комментарий YAML): "
                     "ничего не записано, сказать владельцу").format(day=day, keys="», «".join(bad))
        return ""

    rejected = ""

    def transform(day_text: str) -> str:
        nonlocal rejected
        # Все запрошенные поля проверяются до любых изменений, в каждой
        # попытке под замком, включая повтор после телефона (ревью 11.10.2026).
        rejected = header_error(day_text)
        if rejected:
            return day_text
        return add_fm_values(add_fm_values(day_text, "люди", args.people), "места", args.places)

    if args.dry_run:
        error = header_error(path.read_text(encoding="utf-8"))
        if error:
            print(error, file=sys.stderr)
            return ERROR
        print(_("[dry-run] {path}: люди {people}, места {places}").format(path=path, people=args.people, places=args.places))
        return OK
    changed = rewrite(path, transform)
    if rejected:
        print(rejected, file=sys.stderr)
        return ERROR
    print(_("шапка обновлена") if changed else _("в шапке это уже есть"))
    _warn_cards(vault, args.people)
    return OK


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="domovoy.diary")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help=_("запись дня: пересказ из stdin"))
    p.add_argument("--day", default="", help=_("ГГГГ-ММ-ДД, по умолчанию сегодня (до 04:00 вчера)"))
    p.add_argument("--label", required=True, help=_("метка блока, одна на рассказ: день, вечер, ..."))
    p.add_argument("--time", default="", help=_("ЧЧ:ММ события, по умолчанию сейчас"))
    p.add_argument("--kind", default="retold", choices=("retold", "verbatim"))
    p.add_argument("--text-file", default="", help=_("файл с текстом вместо stdin"))
    p.add_argument("--original-file", default="", help=_("файл с исходными словами"))
    p.add_argument("--people", nargs="*", default=[], help=_("с кем виделся (имена карточек)"))
    p.add_argument("--places", nargs="*", default=[])
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("fact", help=_("строка в «События дня»"))
    p.add_argument("text")
    p.add_argument("--day", default="")
    p.add_argument("--time", default="")
    p.add_argument("--source", default="", help=_("откуда известно: слова владельца, почта, ..."))
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("mark", help=_("люди и места в шапке дня"))
    p.add_argument("--day", default="")
    p.add_argument("--people", nargs="*", default=[])
    p.add_argument("--places", nargs="*", default=[])
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("rollup", help=_("сводка недели: текст из stdin, шапку и заголовок ставит команда"))
    p.add_argument("--week", required=True, help=_("ГГГГ-Wнн"))
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("status", help=_("что записано за последние дни"))
    p.add_argument("--days", type=int, default=7)

    p = sub.add_parser("sessions", help=_("реплики владельца в других сессиях за день"))
    p.add_argument("--day", default="")
    p.add_argument("--limit", type=int, default=sessions.MAX_TOTAL_CHARS, help=_("потолок выжимки в знаках"))

    sub.add_parser("feed", help=_("пересобрать ленту"))

    p = sub.add_parser("commit", help=_("закоммитить папку дневника"))
    p.add_argument("message", nargs="?", default="дневник: правка")

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    now = now or datetime.now(TZ)
    if args.cmd == "sessions":
        # Хранилище выжимке не нужно: журналы сессий лежат на самой машине.
        try:
            day = _parse_day(args.day, now)
        except BadDate as e:
            print(_("не понял дату: {error}").format(error=e), file=sys.stderr)
            return ERROR
        code, text = sessions.digest(day, args.limit)
        print(text, file=sys.stderr if code else sys.stdout)
        return code
    vault = vault_dir()
    diary_dir = vault / DIARY_SUBDIR
    if not diary_dir.is_dir():
        # Хранилище есть только на ПК: на другой машине писать некуда.
        print(_("хранилище недоступно: {path}").format(path=diary_dir), file=sys.stderr)
        return ERROR

    try:
        writer = {"add": cmd_add, "fact": cmd_fact, "mark": cmd_mark, "rollup": cmd_rollup}.get(args.cmd)
        if writer is not None:
            code = writer(args, vault, now)
            # Лента и коммит после любой команды записи, в том числе когда
            # сама она ничего не добавила (повтор): прошлый запуск мог
            # оборваться после записи файла и до коммита, и доделать это
            # должен первый же следующий вызов, каким бы он ни был.
            if code in (OK, DUPLICATE) and not args.dry_run:
                _after_write(vault, getattr(args, "commit_message", "дневник: правка"))
            return code
        if args.cmd == "status":
            print("\n".join(status_lines(diary_dir, diary_day(now), args.days)))
            return OK
        if args.cmd == "feed":
            print(_("лента пересобрана") if write_feed(diary_dir) else _("лента не изменилась"))
            return OK
        result = commit(vault, args.message)
        print(_("git: {result}").format(result=_commit_text(result)))
        # Несделанный коммит это код 1, а не 0 (ревью 11.10.2026).
        return OK if result in (COMMITTED, NOTHING_TO_COMMIT, NO_GIT) else ERROR
    except BadDate as e:
        print(_("не понял дату: {error}").format(error=e), file=sys.stderr)
        return ERROR
    except UnicodeDecodeError as e:
        # Наследник ValueError: своя ветка называет причину.
        print(_("запись не удалась: файл не читается как UTF-8: {error}").format(error=e), file=sys.stderr)
        return ERROR
    except (OSError, DayFileChanged, ValueError) as e:
        # Прочие ValueError (оборванный файл, NUL в имени файла, одиночный
        # суррогат в тексте) это сбой записи, а не дата (ревью 11.10.2026).
        print(_("запись не удалась: {error}").format(error=e), file=sys.stderr)
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
