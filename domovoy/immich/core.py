"""Чистая логика Immich-слоя: рендер машинного блока, замена в файле дня,
статус фото, поиск незакрытых дней. Без сети — тестируемо.

Машинный блок — ЕДИНСТВЕННОЕ, что этот слой пишет в тело дня, и только между
маркерами `<!-- machine:photos v1 -->` … `<!-- /machine:photos -->`.
Блок только пополняется: прежние миниатюры и ссылки остаются, новые кадры
добавляются в ту же сетку, пока в ней меньше PREVIEW_MAX, дальше идут лишь в
счётчик и реестр (ревью 11.10.2026). Целиком блок строится, только когда его
в дне нет. Правится лишь однозначный блок (photos_block_span): если маркер
процитирован в записи или блоков два, заметка не трогается вовсе, и
человеческий текст между маркерами уцелеет.
"""
from __future__ import annotations

import base64
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from ..i18n import _

MARK_OPEN = "<!-- machine:photos v1 -->"
MARK_CLOSE = "<!-- /machine:photos -->"
PREVIEW_MAX = 9       # кадров-ключей в тело дня (сетка 3×3 миниатюр)
THUMB_COLS = 3        # миниатюр в ряд: таблица, иначе Obsidian ставит столбиком
THUMB_WIDTH = 150     # ширина отображения (сам файл ужимается до 300px)
PENDING_PER_RUN = 30  # дней за один прогон: лимит на итерацию, не окно


@dataclass(frozen=True)
class Asset:
    id: str
    file_name: str
    sha1_hex: str
    taken_at: str  # ISO как отдаёт Immich


def asset_from_api(item: dict) -> Asset:
    """Immich отдаёт checksum как base64(SHA1) — раскодируем в hex: hex-строка
    сравнима глазами и грепается, base64 — нет."""
    checksum = item.get("checksum") or ""
    try:
        sha1 = base64.b64decode(checksum).hex()
    except Exception:
        sha1 = checksum
    return Asset(
        id=str(item.get("id") or ""),
        file_name=str(item.get("originalFileName") or ""),
        sha1_hex=sha1,
        taken_at=str(item.get("fileCreatedAt") or item.get("localDateTime") or ""),
    )


def sort_chronological(assets: list[Asset]) -> list[Asset]:
    """По времени съёмки, ранние первыми. Immich отдаёт новые сверху, но день
    в дневнике читается сверху вниз по времени, и реестр не должен спорить с
    порядком человеческих блоков."""
    return sorted(assets, key=lambda a: (a.taken_at, a.file_name))


def pick_key_frames(assets: list[Asset], n: int = PREVIEW_MAX) -> list[Asset]:
    """До n кадров-ключей: первый, последний и равномерно между ними.

    Отбор чисто временнóй, без распознавания сцен и лиц (дизайн его
    запрещает): начало, середина и конец дня почти всегда разные сюжеты.
    """
    ordered = sort_chronological(assets)
    if len(ordered) <= n:
        return ordered
    if n == 1:
        return [ordered[0]]
    step = (len(ordered) - 1) / (n - 1)
    return [ordered[round(i * step)] for i in range(n)]


def preview_cell(rel: str, url: str, width: int = THUMB_WIDTH) -> str:
    """Превью, нажатие на которое открывает снимок в Immich: обычная
    markdown-картинка внутри markdown-ссылки. Вставка `![[...]]` в ссылку не
    оборачивается (Obsidian смесь двух синтаксисов не разбирает). Картинка
    локальная и видна без сервера, адрес нужен только для перехода. Вид
    проверен Иваном на ПК и телефоне 04.10.2026. Черта перед шириной
    экранирована обратной чертой: ячейка стоит внутри таблицы."""
    return f"[![\\|{width}]({rel})]({url})"


def render_photos_block(
    base_url: str,
    assets: list[Asset],
    previews: dict[str, str] | None = None,
    registry_rel: str = "",
) -> str:
    """Компактный блок для тела дня: счётчик, кадры-ключи, ссылка на реестр.

    Длинный список ссылок с контрольными суммами (как было) визуально
    подавлял человеческую запись: десять строк машинного текста против одной
    строки того, что человек сказал. Полные данные живут в реестре, тело дня
    остаётся человеческим.
    """
    previews = previews or {}
    assets = sort_chronological(assets)
    lines = [MARK_OPEN]
    # Wiki-ссылка с подписью, а не markdown вокруг wiki: смесь двух синтаксисов
    # Obsidian не разбирает.
    tail = (" · [[{rel}|{label}]]".format(rel=registry_rel, label=_("все {count}").format(count=len(assets)))
            if registry_rel else "")
    lines.append("📷 " + _("{count} кадр(ов) в Immich").format(count=len(assets)) + tail)
    keys = pick_key_frames(assets)
    with_preview = [a for a in keys if a.id in previews]
    if with_preview:
        # Таблица, потому что подряд идущие ![[...]] Obsidian ставит столбиком,
        # а миниатюры дня должны читаться одной полосой, не простынёй.
        lines.append("")
        lines.append("| " + " | ".join([" "] * THUMB_COLS) + " |")
        lines.append("|" + "---|" * THUMB_COLS)
        rows = [
            with_preview[i : i + THUMB_COLS]
            for i in range(0, len(with_preview), THUMB_COLS)
        ]
        for row in rows:
            cells = [preview_cell(previews[a.id], f"{base_url}/photos/{a.id}") for a in row]
            cells += [" "] * (THUMB_COLS - len(cells))
            lines.append("| " + " | ".join(cells) + " |")
    for a in keys:
        if a.id not in previews:
            lines.append(f"- [{a.file_name}]({base_url}/photos/{a.id})")
    lines.append(MARK_CLOSE)
    return "\n".join(lines) + "\n"


def recorded_assets(day_text: str, registry: str = "") -> list[Asset]:
    """Полные id из блока и прежнего реестра (ревью 11.10.2026)."""
    span = photos_block_span(day_text)
    # Реестр мог записаться раньше первого блока дня (ревью 11.10.2026).
    block = day_text[span[0]:span[1]] if span else ""
    ids = re.findall(r"/photos/([^\s)]+)", block + registry)
    assets = {i: Asset(i, i, "", "") for i in dict.fromkeys(ids)}
    # Не теряем имя и контрольную сумму удалённого снимка при первом
    # чтении прежнего реестра (ревью 11.10.2026).
    for name, full, sha in re.findall(r"\[([^\]\n]*)\]\([^\n)]*/photos/([^\s)]+)\) \| `([^`]*)`", registry):
        assets[full] = Asset(full, name, sha, "")
    return list(assets.values())


_SEP_ROW_RE = re.compile(r"^\|(?:-+\|)+$")


def block_parts(block: str) -> tuple[list[str], list[str]]:
    """Ячейки миниатюр и прочие строки блока фото без маркеров, счётчика и
    разметки таблицы. Таблиц после прежних догрузок может быть несколько,
    ячейки собираются из всех (ревью 11.10.2026)."""
    cells: list[str] = []
    rest: list[str] = []
    for line in block.splitlines():
        if line in (MARK_OPEN, MARK_CLOSE) or line.startswith("📷 ") or not line.strip():
            continue
        if _SEP_ROW_RE.match(line):
            continue
        if line.startswith("|") and line.endswith("|"):
            # Черта внутри ячейки экранирована и окружена не пробелами.
            cells += [c.strip() for c in line[1:-1].split(" | ") if c.strip()]
        else:
            rest.append(line)
    return cells, rest


def shown_frames(day_text: str) -> int:
    """Сколько кадров блок уже показывает миниатюрой или ссылкой: это места
    сетки PREVIEW_MAX, занятые навсегда (ревью 11.10.2026)."""
    span = photos_block_span(day_text)
    if span is None:
        return 0
    cells, rest = block_parts(day_text[span[0]:span[1]])
    return len(cells) + sum(1 for line in rest if line.startswith("- ["))


def append_photos_block(
    day_text: str, base_url: str, keys: list[Asset], previews: dict[str, str],
    count: int, registry_rel: str,
) -> str:
    """Прежний блок с новым счётчиком и новыми кадрами в той же сетке: старые
    ячейки и ссылки сохраняются как есть (ревью 11.10.2026)."""
    span = photos_block_span(day_text)
    cells, rest = block_parts(day_text[span[0]:span[1]] if span else "")
    for a in sort_chronological(keys):
        url = f"{base_url}/photos/{a.id}"
        if a.id in previews:
            cells.append(preview_cell(previews[a.id], url))
        else:
            rest.append(f"- [{a.file_name}]({url})")
    tail = (" · [[{rel}|{label}]]".format(rel=registry_rel, label=_("все {count}").format(count=count))
            if registry_rel else "")
    lines = [MARK_OPEN, "📷 " + _("{count} кадр(ов) в Immich").format(count=count) + tail]
    if cells:
        lines.append("")
        lines.append("| " + " | ".join([" "] * THUMB_COLS) + " |")
        lines.append("|" + "---|" * THUMB_COLS)
        for i in range(0, len(cells), THUMB_COLS):
            row = cells[i : i + THUMB_COLS]
            row += [" "] * (THUMB_COLS - len(row))
            lines.append("| " + " | ".join(row) + " |")
    lines += rest
    lines.append(MARK_CLOSE)
    return "\n".join(lines) + "\n"


def local_hhmm(taken_at: str, tz) -> str:
    """Время съёмки в местной зоне. Immich отдаёт fileCreatedAt в UTC, и
    срез строки давал бы «11:46» там, где на снимке 15:46."""
    if not taken_at:
        return ""
    try:
        dt = datetime.fromisoformat(taken_at.replace("Z", "+00:00"))
    except ValueError:
        return taken_at[11:16]
    if dt.tzinfo is None:
        return dt.strftime("%H:%M")
    return dt.astimezone(tz).strftime("%H:%M")


def render_registry_note(day: date, base_url: str, assets: list[Asset], tz=UTC) -> str:
    """Реестр кадров дня как markdown-заметка, а не JSON.

    Wiki-ссылка на .json в Obsidian не открывается, а реестр должен быть
    читаемым человеком: это часть архива, а не служебный кэш.
    """
    lines = [
        "---",
        "schema: diary-photos/v1",
        "type: photo-registry",
        f"date: {day.isoformat()}",
        f"кадров: {len(assets)}",
        "sensitivity: private",
        "---",
        "# " + _("Кадры за {date}").format(date=day.strftime('%d.%m.%Y')),
        "",
        _("Полный список из Immich. День: {link}.").format(
            link=f"[[Дневник/{day.year}/{day.isoformat()}|{day.isoformat()}]]"),
        "",
        _("| Время | Файл | sha1 |"),
        "|---|---|---|",
    ]
    for a in sort_chronological(assets):
        t = local_hhmm(a.taken_at, tz)
        lines.append(f"| {t} | [{a.file_name}]({base_url}/photos/{a.id}) | `{a.sha1_hex[:16]}` |")
    return "\n".join(lines) + "\n"


class AmbiguousPhotosBlock(Exception):
    """Маркер блока фото в заметке есть, но блок не однозначен: не трогать."""


def replace_machine_block(day_text: str, block: str) -> str:
    """Заменить существующий machine:photos блок или добавить в конец файла.

    Заменяется только однозначный блок. Регулярка «от первого маркера до
    первого закрывающего» при маркере, процитированном в записи, вырезала бы
    весь текст между цитатой и настоящим блоком, поэтому любое другое
    вхождение маркера даёт AmbiguousPhotosBlock, и день остаётся как есть.
    """
    if MARK_OPEN in day_text or MARK_CLOSE in day_text:
        span = photos_block_span(day_text)
        if span is None:
            raise AmbiguousPhotosBlock("блок фото неоднозначен (маркер процитирован или блоков несколько)")
        start, end = span
        rest = day_text[end:]
        return day_text[:start] + block + (rest[1:] if rest.startswith("\n") else rest)
    sep = "" if day_text.endswith("\n\n") else ("\n" if day_text.endswith("\n") else "\n\n")
    return day_text + sep + block


_PHOTO_STATUS_RE = re.compile(r"^фото:\s*\S+\s*$", re.MULTILINE)


def set_photo_status(day_text: str, status: str) -> str:
    """`фото: pending` → synced|none. Только frontmatter-строка, ничего больше."""
    if status not in ("pending", "synced", "none"):
        raise ValueError(f"недопустимый статус {status!r}")
    return _PHOTO_STATUS_RE.sub(f"фото: {status}", day_text, count=1)


def find_pending_days(diary_dir: Path, limit: int | None = PENDING_PER_RUN) -> list[date]:
    """ВСЕ дни со статусом pending, старые первыми, не больше limit за прогон.

    Immich у Ивана ручного запуска и может лежать неделями: limit — это
    сколько разгребаем за одну итерацию, а не окно допустимости (ревью
    дизайна, находка про N). Остальное доберут следующие прогоны.
    """
    # Без лимита очередь сортирует вызывающий код (ревью 11.10.2026).
    out: list[date] = []
    for year_dir in sorted(diary_dir.glob("[0-9][0-9][0-9][0-9]")):
        for f in sorted(year_dir.glob("*.md")):
            try:
                d = date.fromisoformat(f.stem)
            except ValueError:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            except UnicodeDecodeError:
                print(f"immich: {f.name} не читается (не UTF-8), пропущен", file=sys.stderr)
                continue
            if re.search(r"^фото:\s*pending\s*$", text, re.MULTILINE):
                out.append(d)
                if limit is not None and len(out) >= limit:
                    return out
    return out


# Ячейка прежнего вида: вставка превью без ссылки. Имя файла несёт только
# первые восемь знаков идентификатора кадра.
_OLD_CELL_RE = re.compile(
    r"!\[\[(_attachments/[^\]|\\\n]+?/\d{4}-\d{2}-\d{2}_([0-9a-f]{8})\.jpg)\\\|(\d+)\]\]"
)
_OPEN_LINE_RE = re.compile(r"^" + re.escape(MARK_OPEN) + r"[ \t]*$", re.MULTILINE)
_CLOSE_LINE_RE = re.compile(r"^" + re.escape(MARK_CLOSE) + r"[ \t]*$", re.MULTILINE)


def photos_block_span(day_text: str) -> tuple[int, int] | None:
    """Границы блока фото, если он в заметке один и однозначен, иначе None.

    Маркеры считаются только отдельными строками и ровно по одному. Любое
    другое вхождение маркера (его процитировали в записи, блоков два) делает
    заметку неоднозначной: править её по регулярному выражению значило бы
    рисковать текстом человека, поэтому такая заметка пропускается.
    """
    opens = list(_OPEN_LINE_RE.finditer(day_text))
    closes = list(_CLOSE_LINE_RE.finditer(day_text))
    if len(opens) != 1 or len(closes) != 1:
        return None
    if day_text.count(MARK_OPEN) != 1 or day_text.count(MARK_CLOSE) != 1:
        return None
    if closes[0].start() < opens[0].end():
        return None
    return opens[0].start(), closes[0].end()


def unique_prefixes(asset_ids) -> dict[str, str]:
    """{первые восемь знаков: полный идентификатор} только для однозначных.
    Два кадра одного дня с общим началом идентификатора дали бы ссылку не на
    тот снимок, поэтому такие превью остаются без ссылки."""
    by_prefix: dict[str, set[str]] = {}
    for full in asset_ids:
        by_prefix.setdefault(full[:8], set()).add(full)
    return {prefix: next(iter(ids)) for prefix, ids in by_prefix.items() if len(ids) == 1}


def old_cells(day_text: str) -> list[str]:
    """Восьмизначные метки превью прежнего вида внутри блока фото."""
    span = photos_block_span(day_text)
    if span is None:
        return []
    return [c.group(2) for c in _OLD_CELL_RE.finditer(day_text[span[0]:span[1]])]


def relink_block(day_text: str, ids: dict[str, str], base_url: str) -> str:
    """Превью прежнего вида внутри блока фото сделать ссылками на снимки.

    Меняется только вид ячеек: набор превью, счётчик кадров и всё вне блока
    остаются как были. Превью, чьего кадра в ids нет (снимок удалён из
    Immich, идентификатор неоднозначен), остаётся вставкой без ссылки.
    Заметка с неоднозначным блоком не меняется. Повторный вызов ничего не
    меняет.
    """
    span = photos_block_span(day_text)
    if span is None:
        return day_text

    def cell(c: re.Match) -> str:
        full = ids.get(c.group(2))
        return preview_cell(c.group(1), f"{base_url}/photos/{full}", int(c.group(3))) if full else c.group(0)

    start, end = span
    return day_text[:start] + _OLD_CELL_RE.sub(cell, day_text[start:end]) + day_text[end:]


_COUNT_RE = re.compile(re.escape(MARK_OPEN) + r"\n📷 (\d+)\s")
_CLOSED_STATUS_RE = re.compile(r"^фото:\s*(synced|none)\s*$", re.MULTILINE)


def recorded_count(day_text: str) -> int:
    """Сколько кадров записано в блоке фото дня. Блока нет: ноль."""
    m = _COUNT_RE.search(day_text)
    return int(m.group(1)) if m else 0


def find_recheck_days(diary_dir: Path, today: date, window: int,
                      closed_dates: dict[str, str] | None = None) -> list[date]:
    """Уже закрытые дни, закрытые не раньше window суток назад, старые первыми.
    Дата закрытия берётся из closed_dates, без неё отсчёт от самого дня.

    Телефон догружает снимки в Immich с опозданием, в поездке на дни: день,
    закрытый по первому же ответу сервера, навсегда оставался «фото: none»
    (так вышло с 28.09 и 01.10.2026). Поэтому недавние дни переспрашиваются.
    """
    out: list[date] = []
    # Дата первого закрытия из состояния, старым записям оставляем прежний
    # отсчёт до первого успешного запроса (ревью 11.10.2026).
    for path in sorted(diary_dir.glob("[0-9][0-9][0-9][0-9]/*.md")):
        try:
            d = date.fromisoformat(path.stem)
            closed = date.fromisoformat((closed_dates or {}).get(path.stem, path.stem))
        except ValueError:
            continue
        if d >= today or not 0 <= (today - closed).days <= window:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        except UnicodeDecodeError:
            print(f"immich: {path.name} не читается (не UTF-8), пропущен", file=sys.stderr)
            continue
        if _CLOSED_STATUS_RE.search(text):
            out.append(d)
    return out


__all__ = [
    "AmbiguousPhotosBlock",
    "Asset",
    "MARK_CLOSE",
    "MARK_OPEN",
    "PENDING_PER_RUN",
    "PREVIEW_MAX",
    "THUMB_COLS",
    "THUMB_WIDTH",
    "asset_from_api",
    "pick_key_frames",
    "local_hhmm",
    "find_pending_days",
    "find_recheck_days",
    "old_cells",
    "photos_block_span",
    "preview_cell",
    "recorded_count",
    "recorded_assets",
    "append_photos_block",
    "block_parts",
    "shown_frames",
    "relink_block",
    "unique_prefixes",
    "render_photos_block",
    "render_registry_note",
    "sort_chronological",
    "replace_machine_block",
    "set_photo_status",
]
