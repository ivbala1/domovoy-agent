"""Журнал месяца journal/ГГГГ-ММ.md: файл месяца, разделы дня и запись кода.

Один модуль на всех писателей и читателей (ревизия повторов 07.11.2026, шаг 3).
Раздел дня начинается заголовком `## ГГГГ-ММ-ДД (дн)` и идёт до следующей
строки `## `. Есть ли раздел дня, решается по дате без дня недели: 03.11.2026
сторож сравнивал заголовок целиком и вместе с утром поставил два раздела одного
дня, а рефлексия писала в первый из них.

Пишут журнал: строки сторожа, слушателя и просмотра (`append`, очередь под
замком синхронизации), ночная рефлексия (`append_to_day` через `data.run`) и
агент по промту через `domovoy.data`."""
from __future__ import annotations

import contextlib
import json
import re
import sys
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

from .common import JOURNAL_DIR, REPO_ROOT, STATE_ROOT, WEEKDAYS_SHORT_RU
from .statefile import append_jsonl, flock, write_atomic

DAY_RE = re.compile(r"^## (\d{4}-\d\d-\d\d)\b")
# Очередь строк, не дождавшихся замка синхронизации (ревью 11.10.2026). Путь
# прежний, из каталога сторожа: строки, отложенные до выкатки, не теряются.
QUEUE_FILE = STATE_ROOT / "watch" / "journal-queue.jsonl"


def month_file(day: date, journal_dir: Path | None = None) -> Path:
    # Каталог по умолчанию берётся в момент вызова: так тесты подменяют
    # JOURNAL_DIR и не пишут в живой журнал.
    return (journal_dir or JOURNAL_DIR) / f"{day:%Y-%m}.md"


def month_files(first: date, last: date, journal_dir: Path | None = None) -> list[Path]:
    """Существующие файлы месяцев с first по last."""
    out, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        p = month_file(date(y, m, 1), journal_dir)
        if p.is_file():
            out.append(p)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def heading(day: date) -> str:
    return f"## {day:%Y-%m-%d} ({WEEKDAYS_SHORT_RU[day.weekday()]})"


def is_heading(line: str) -> bool:
    """Граница раздела: любая строка `## `, не только заголовок дня."""
    return line.startswith("## ")


def heading_day(line: str) -> date | None:
    """Дата заголовка дня; None: не заголовок дня или дата неверная."""
    m = DAY_RE.match(line)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _strip_bullet(line: str) -> str:
    s = line.strip()
    return s[2:].lstrip() if s.startswith(("- ", "* ")) else s


def days(first: date, last: date, journal_dir: Path | None = None) -> dict[date, list[str]]:
    """Строки журнала по дням [first, last], без пустых, маркер списка снят.
    Разделы одного дня сливаются по порядку в файле."""
    out: dict[date, list[str]] = {}
    for path in month_files(first, last, journal_dir):
        cur = None
        for line in _read(path).splitlines():
            if DAY_RE.match(line):
                d = heading_day(line)
                cur = d if d and first <= d <= last else None
                if cur:
                    out.setdefault(cur, [])
            elif cur and line.strip():
                out[cur].append(_strip_bullet(line))
    return out


def day_sections(text: str, day: date) -> list[tuple[int, int, int]]:
    """Разделы дня по порядку: (начало заголовка, начало тела, конец раздела)."""
    out = []
    pos = 0
    # Только по \n, как ищет domovoy.data: splitlines режет и по \x0c, \x85,
    # и «заголовок» оказался бы не с начала строки (ревью 08.11.2026).
    for line in text.split("\n"):
        if heading_day(line) == day:
            body = min(pos + len(line) + 1, len(text))
            nxt = text.find("\n## ", pos + len(line))
            out.append((pos, body, len(text) if nxt < 0 else nxt))
        pos += len(line) + 1
    return out


def section_lines(text: str, day: date) -> list[str]:
    """Строки всех разделов дня как есть, по порядку."""
    rows: list[str] = []
    for _begin, start, end in day_sections(text, day):
        rows += text[start:end].splitlines()
    return rows


# --- запись строки кода (сторож, слушатель, просмотр) ---------------------

def append(who: str, lines: list[str], when: datetime, journal_dir: Path | None = None) -> None:
    """Строки в журнал без исключения: сбой записи только предупреждение,
    строка вызывающего (сводка, пробуждение) важнее журнала."""
    try:
        append_or_raise(who, lines, when, journal_dir)
    except OSError as e:
        print(f"domovoy.journal: строка в журнал не записана: {e}", file=sys.stderr)


def append_or_raise(who: str, lines: list[str], when: datetime, journal_dir: Path | None = None) -> None:
    """Строка сторожа в журнал пишется кодом: ран пробуждения со сводкой
    untrusted, бот не даёт ему писать файлы. День дописывается в конец
    месячного файла; последний раздел не этого дня, значит заголовок ставится.

    Строки здесь без текста писем и сообщений (watch.journal_line): журнал
    читает каждое доверенное пробуждение, и чужой текст в нём был бы той же
    подсказкой агенту, от которой ран сторожа закрыт правами."""
    # Общий замок с синхронизацией (ревью 11.10.2026): строка, дописанная
    # посреди её rebase, пропадала при откате. Без замка журнал не трогаем
    # никогда: строка ждёт в очереди и уходит в журнал следующей записью.
    from . import sync

    path = month_file(when.date(), journal_dir)
    entry = {"path": str(path), "at": when.isoformat(), "lines": lines}
    if who != "Сторож":
        entry["who"] = who
    with sync.data_lock() as locked:
        # Новая строка сначала ложится в очередь и в журнал попадает только
        # переносом очереди (ревью 11.10.2026, третий круг): упади перенос
        # старых строк, новая не теряется, а ждёт в очереди вместе с ними.
        try:
            with _queue_lock():
                append_jsonl(QUEUE_FILE, entry)
        except OSError as e:
            if not locked:
                raise
            # Очередь недоступна, а замок взят: пишем строку сразу, как раньше.
            print(f"domovoy.journal: очередь журнала недоступна ({e}), пишу строку сразу", file=sys.stderr)
            _write_line(path, when, lines, when.date(), who)
            return
        if not locked:
            print("domovoy.journal: замок синхронизации не взят, строка в журнал отложена "
                  "до следующей записи или синхронизации", file=sys.stderr)
            return
        flush_queue(when.date())


@contextlib.contextmanager
def _queue_lock() -> Iterator[None]:
    """Свой замок очереди (ревью 11.10.2026, второй круг): дописать строку
    может параллельный прогон сторожа, пока очередь переносят, а перенос без
    замка удалил бы её вместе с перенесёнными."""
    with flock(QUEUE_FILE.with_suffix(".lock")):
        yield


def flush_queue(today: date) -> None:
    """Перенести отложенные строки в журнал (ревью 11.10.2026).
    Вызывать только под замком синхронизации: его берёт и строка сторожа, и
    прогон синхронизации, так очередь не ждёт следующей строки (второй круг).
    Перенесённая строка сразу уходит из очереди: сбой на следующей её не
    повторит. Упала сама перезапись очереди после записи в журнал (или
    процесс убит между ними), строку повторно не допишет _write_line: она
    уже в журнале (ревью 11.10.2026, третий круг)."""
    if not QUEUE_FILE.exists():
        return
    with _queue_lock():
        try:
            # Битый UTF-8 не роняет перенос: оборванная строка пропустится ниже,
            # испорченный байт внутри значения уйдёт в журнал символом U+FFFD.
            rest = QUEUE_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return
        while rest:
            raw = rest.pop(0)
            try:
                item = json.loads(raw)
                when = datetime.fromisoformat(item["at"])
                _write_line(Path(item["path"]), when, [str(x) for x in item["lines"]], today,
                            str(item.get("who") or "Сторож"))
            except (ValueError, KeyError, TypeError) as e:
                print(f"domovoy.journal: строка очереди журнала не прочитана ({e!r}), пропускаю", file=sys.stderr)
            write_atomic(QUEUE_FILE, "".join(f"{x}\n" for x in rest))
        QUEUE_FILE.unlink(missing_ok=True)


def _write_line(path: Path, when: datetime, lines: list[str], today: date, who: str = "Сторож") -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    body = "; ".join(lines)
    if _has_line(text, when, body, who):
        return
    chunk = ""
    # Строка встаёт под раздел своего дня, только если он последний в файле;
    # иначе заголовок повторяется (ревью 11.10.2026, четвёртый круг): перенос
    # вчерашней очереди ставит вчерашний заголовок ниже сегодняшнего, и
    # сегодняшняя строка без повтора легла бы под вчерашний день. День
    # сравнивается по дате: заголовок агента с другим днём недели не повод
    # ставить второй (03.11.2026).
    headings = [row for row in text.splitlines() if is_heading(row)]
    if not headings or heading_day(headings[-1]) != when.date():
        chunk += ("\n" if text and not text.endswith("\n") else "") + ("\n" if text else "") + heading(when.date()) + "\n\n"
    elif text and not text.endswith("\n"):
        chunk += "\n"
    # Отложенная строка прошлого дня с датой: иначе её примут за сегодняшнюю.
    stamp = f"{when:%H:%M}" if when.date() == today else f"{when:%d.%m %H:%M}"
    chunk += f"{who} {stamp}: {body}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(chunk)


def _has_line(text: str, when: datetime, body: str, who: str = "Сторож") -> bool:
    """Строка очереди уже в журнале (ревью 11.10.2026, третий круг): её
    дописали, а убрать из очереди не успели. Опознаётся по самой строке:
    отдельный идентификатор в журнале был бы шумом для агента, а та же строка
    в ту же минуту того же дня ничего нового не несёт. Поэтому две разные
    записи с одинаковым текстом в одну минуту одного дня (два сообщения
    одного отправителя в одном чате) сольются в одну строку: принятая цена,
    информации в них одинаково (ревью 11.10.2026, четвёртый круг). Строка с
    датой ищется по всему месячному файлу, строка со временем во всех
    разделах своего дня, сколько бы раз ни повторялся его заголовок
    (четвёртый круг): вчерашняя такая же строка не повод пропустить
    сегодняшнюю. Журнал дописывается, а не переписывается целиком: агент бота
    пишет его без замка, и замена файла стёрла бы его запись."""
    if f"{who} {when:%d.%m %H:%M}: {body}" in text.splitlines():
        return True
    return f"{who} {when:%H:%M}: {body}" in section_lines(text, when.date())


# --- строки в раздел дня (рефлексия) --------------------------------------

def day_edit(text: str, day: date, lines: list[str]) -> str | None:
    """Блок edit для domovoy.data: строки в конец последнего раздела дня,
    после утра и вечера, чтобы день читался сверху вниз (nov2). Старый текст
    это весь последний раздел с заголовком: он сверяется под замком записи,
    поэтому раздел следующего дня, открытый после полуночи, не мешает, а правка
    дня между чтением и записью даёт отказ и повтор. Уже записанные строки не
    повторяются ни в одном разделе дня; None: раздела нет или писать нечего."""
    found = day_sections(text, day)
    if not found:
        return None
    have = {line.strip() for line in section_lines(text, day)}
    fresh = [line for line in lines if line.strip() not in have]
    if not fresh:
        return None
    from .data import _MARKER_RE

    begin, start, end = found[-1]
    tail = text[begin:end].rstrip("\n")
    if _MARKER_RE.search(tail):
        # Строка «=======» внутри дня: такой old запись отвергнет, якорь
        # заголовок, строки встанут под ним.
        tail = text[begin:start].rstrip("\n")
    old = tail
    if _count_at_line_start(text, old) != 1:
        # Запись ищет old ровно один раз: два одинаковых раздела дня (или
        # одинаковые заголовки при «=======») дают якорь из нескольких строк
        # до раздела, без строк-маркеров записи (ревью 08.11.2026).
        rows = text[:begin].split("\n")[:-1]
        head = []
        while rows and _count_at_line_start(text, old) != 1:
            row = rows.pop()
            if _MARKER_RE.match(row):
                break
            head.insert(0, row)
            old = "\n".join(head) + "\n" + tail
    return "<<<<<<< old\n{0}\n=======\n{0}\n{1}\n>>>>>>> new\n".format(old, "\n".join(fresh))


def _count_at_line_start(text: str, part: str) -> int:
    n, at = 0, text.find(part)
    while at != -1:
        if at == 0 or text[at - 1] == "\n":
            n += 1
        at = text.find(part, at + 1)
    return n


def append_to_day(day: date, lines: list[str], root: Path = REPO_ROOT, journal_dir: Path | None = None) -> bool:
    """Строки в последний раздел дня через domovoy.data (замок, сверка old).
    False: файла или раздела дня нет, или все строки уже есть. Отказ записи
    поднимает data.Refused."""
    from . import data

    path = month_file(day, journal_dir)
    if not path.exists():
        return False
    block = day_edit(data._read(path), day, lines)
    if not block:
        return False
    data.run("edit", f"{path.parent.name}/{path.name}", block, root=root)
    return True
