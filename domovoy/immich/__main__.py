"""Фото дня из Immich: закрыть `фото: pending` и перепроверить недавние дни.

Запускается своим юнитом domovoy-photos.service, который утреннее
пробуждение стартует хвостом (без таймера и без решения модели). Перенесён из claude-tg-bot
(skills/diary_immich) 04.10.2026. Без IMMICH_API_KEY или при лежащем сервере
выходит тихо (exit 0): Immich у Ивана ручного запуска (LUKS), недоступность
это штатный режим, а не ошибка. Незакрытые дни добираются при первом живом
прогоне.

Дни, закрытые не раньше RECHECK_DAYS суток назад, переспрашиваются: телефон
догружает снимки с опозданием, и день, закрытый по первому ответу сервера,
оставался без фото навсегда. За прогон не больше RECHECK_PER_RUN таких дней,
давно не спрошенные первыми (ревью 11.10.2026).

`--relink` разово переделывает превью прежнего вида (вставка без ссылки) в
ссылки на снимки во всех днях; набор превью и счётчики при этом не меняются.

Что пишет: машинный блок `machine:photos` в тело дня (записи не трогаются),
статус `фото:` в шапке, реестр в `_реестр/` при большом количестве кадров,
превью кадров-ключей в `_attachments/дневник/immich/`. Вне хранилища, в
`~/.local/state/domovoy/photos/<день>.json`: полные id кадров дня, дата первого
закрытия, последняя попытка и число отказов превью (ревью 11.10.2026).

Идентификация кадра: имя файла + SHA1-контрольная сумма от Immich + ссылка.
Осознанное отклонение от дизайна (там SHA256): качать каждый файл ради
пересчёта дорого, SHA1 из API решает ту же задачу находимости.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .. import diary
from ..common import TZ, load_env, STATE_ROOT
from ..i18n import _
from .core import (
    AmbiguousPhotosBlock,
    pick_key_frames,
    render_registry_note,
    PREVIEW_MAX,
    Asset,
    asset_from_api,
    find_pending_days,
    MARK_OPEN,
    find_recheck_days,
    old_cells,
    photos_block_span,
    recorded_count,
    recorded_assets,
    append_photos_block,
    shown_frames,
    PENDING_PER_RUN,
    relink_block,
    unique_prefixes,
    render_photos_block,
    replace_machine_block,
    set_photo_status,
)

TIMEOUT = 20.0
RECHECK_DAYS = 14     # от первого закрытия дня (ревью 11.10.2026)
RECHECK_PER_RUN = 14  # перепроверок за прогон, иначе они съедят время pending (ревью 11.10.2026)
PREVIEW_RETRIES = 3  # отказ превью закрываем ссылкой (ревью 11.10.2026)
PHOTOS_STATE = STATE_ROOT / "photos"
DEADLINE_SEC = 240    # после этого новых дней и загрузок не начинаем


PAGE_SIZE = 500
MAX_PAGES = 40   # 20 000 кадров за запрос: предохранитель от бесконечного листания


def search_all(client: httpx.Client, base: str, after: str, before: str) -> list[dict]:
    """Все страницы поиска. Одной страницей день на 650 кадров записался бы
    как 500 и навсегда считался полным: ответ снова давал бы те же 500."""
    items: list[dict] = []
    page = 1
    for _unused in range(MAX_PAGES):
        resp = client.post(
            f"{base}/api/search/metadata",
            json={"takenAfter": after, "takenBefore": before, "size": PAGE_SIZE, "page": page},
        )
        resp.raise_for_status()
        found = resp.json().get("assets") or {}
        items.extend(found.get("items") or [])
        nxt = found.get("nextPage")
        if not nxt:
            break
        page = int(nxt)
    return items


def within(asset: Asset, start: datetime, end: datetime) -> bool:
    """Кадр снят строго внутри [start, end). Граница takenBefore у Immich
    включительна, и полночный кадр иначе попадал бы в два дня."""
    try:
        taken = datetime.fromisoformat(asset.taken_at.replace("Z", "+00:00"))
    except ValueError:
        return True   # время не разобрать: верим серверу
    if taken.tzinfo is None:
        return True
    return start <= taken < end


def fetch_assets(client: httpx.Client, base: str, day: date, tz: ZoneInfo) -> list[Asset]:
    start = datetime.combine(day, dtime.min, tzinfo=tz)
    end = start + timedelta(days=1)
    assets = [asset_from_api(i) for i in search_all(client, base, start.isoformat(), end.isoformat())]
    return [a for a in assets if within(a, start, end)]


# 300px: миниатюра-напоминалка «что это был за день», а не просмотр кадра.
# Полный снимок открывается по ссылке в Immich, поэтому держать в vault
# 800px ради девяти кадров в день (это ~0.5 ГБ в год на телефоне) незачем.
PREVIEW_MAX_EDGE = 300
PREVIEW_QUALITY = 82


def download_preview(client: httpx.Client, base: str, asset: Asset, dest: Path) -> bool:
    """Скачать кадр и превратить в квадратную миниатюру PREVIEW_MAX_EDGE.

    Immich отдаёт `preview` в ~630 КБ: при трёх кадрах в день это больше
    полугигабайта в год в хранилище, который Syncthing разносит на телефон.
    После ужатия кадр весит порядка 100 КБ и остаётся смотрибельным.
    Pillow нет — кладём `thumbnail` (35 КБ) вместо полноразмерного.
    """
    try:
        r = client.get(f"{base}/api/assets/{asset.id}/thumbnail", params={"size": "preview"})
        r.raise_for_status()
        data = r.content
        try:
            import io

            from PIL import Image

            from PIL import ImageOps

            img = Image.open(io.BytesIO(data))
            # Квадрат с обрезкой по центру: в markdown задаётся только ширина,
            # поэтому вертикальный кадр вырастал втрое выше горизонтального и
            # сетка получалась рваной. Полный кадр открывается в Immich.
            img = ImageOps.fit(
                img.convert("RGB"),
                (PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE),
                Image.LANCZOS,
                centering=(0.5, 0.4),  # чуть выше центра: лица обычно там
            )
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=PREVIEW_QUALITY, optimize=True)
            data = buf.getvalue()
        except ImportError:
            r2 = client.get(
                f"{base}/api/assets/{asset.id}/thumbnail", params={"size": "thumbnail"}
            )
            r2.raise_for_status()
            data = r2.content

        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".tmp", dir=dest.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return True
    except Exception as e:  # noqa: BLE001 — превью не критично
        print(f"immich: превью {asset.id[:8]} не скачалось: {e}", file=sys.stderr)
        return False


def atomic_state(path: Path, state: dict) -> None:
    """Состояние вне дневника, атомарно (ревью 11.10.2026)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def read_state(day: date) -> dict:
    try:
        state = json.loads((PHOTOS_STATE / f"{day}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # Битый файл состояния не роняет прогон (ревью 11.10.2026).
    return state if isinstance(state, dict) else {}


def save_state(day: date, state: dict) -> bool:
    try:
        with diary.write_lock():
            atomic_state(PHOTOS_STATE / f"{day}.json", state)
        return True
    except OSError as e:
        print(f"immich: {day}: состояние не записано: {e}", file=sys.stderr)
        return False


def save_close_date(day: date, state: dict, today: date) -> bool:
    """До закрытия нужна сохранённая дата для перепроверок (ревью 11.10.2026)."""
    persisted = read_state(day).get("closed_on")
    state.setdefault("closed_on", persisted or today.isoformat())
    if save_state(day, state) or persisted:
        return True
    # Не подать дату неудавшегося закрытия следующей записью состояния
    # (ревью 11.10.2026).
    state.pop("closed_on", None)
    return False


def apply_to_day(path: Path, day: date, block: str | None, status: str,
                 registry: tuple[Path, str] | None = None) -> bool:
    """Правка дня тем же путём, что у команд дневника (diary.rewrite): общий
    замок с ними, сверка изменения между чтением и записью, подмена только
    после удачного fsync. Сбой одного дня печатается и не обрывает прогон:
    остальные дни этого утра закрываются как обычно."""
    def transform(text: str) -> str:
        new_text = set_photo_status(text, status)
        result = new_text if block is None else replace_machine_block(new_text, block)
        if registry is not None:
            # Реестр и блок под одним замком; готовый helper делает fsync и
            # атомарную подмену, без обрезки старого файла (ревью 11.10.2026).
            reg_path, reg_text = registry
            diary._rewrite_locked(reg_path, lambda _old: reg_text, "", 3)
        return result

    try:
        return diary.rewrite(path, transform)
    except AmbiguousPhotosBlock:
        print(f"immich: {day}: блок фото неоднозначен (маркер процитирован или блоков несколько), "
              "пропущено: посмотреть руками", file=sys.stderr)
    except (OSError, UnicodeDecodeError, diary.DayFileChanged) as e:
        print(f"immich: {day} не записан: {e}", file=sys.stderr)
    return False


def run_relink(
    vault_dir: Path, base: str, api_key: str, tz: ZoneInfo, dry_run: bool,
) -> int:
    """Превью прежнего вида во всех днях сделать ссылками на снимки в Immich.

    Меняется только вид ячеек внутри блока фото: набор превью, счётчик и
    текст дня остаются как были. Ссылку получает только кадр, который сервер
    подтвердил сейчас: в заметке-реестре дня остаются и удалённые снимки, и
    ссылка на такой вела бы в никуда. Кадр, которого в Immich больше нет или
    чей идентификатор по первым восьми знакам неоднозначен, остаётся вставкой
    без ссылки. Заметка с неоднозначным блоком фото пропускается. Повторный
    запуск доделывает оставшееся и готовое не трогает.
    """
    days: list[tuple[date, Path]] = []
    for year_dir in sorted(vault_dir.glob("[0-9][0-9][0-9][0-9]")):
        for f in sorted(year_dir.glob("*.md")):
            try:
                day = date.fromisoformat(f.stem)
                text = f.read_text(encoding="utf-8")
            except (ValueError, OSError):
                continue
            if MARK_OPEN in text and photos_block_span(text) is None:
                print(f"immich: {day}: блок фото неоднозначен (маркер процитирован или блоков несколько), "
                      "пропущено: посмотреть руками", file=sys.stderr)
            elif old_cells(text):
                days.append((day, f))
    if not days:
        print(_("immich: превью прежнего вида нет, переделывать нечего"))
        return 0

    client = httpx.Client(headers={"x-api-key": api_key}, timeout=TIMEOUT)
    changed = linked = left = 0
    try:
        try:
            client.get(f"{base}/api/server/ping").raise_for_status()
        except Exception as e:
            print(_("immich: Immich недоступен ({error}): без него не узнать, какие кадры ещё существуют, отложено").format(error=e))
            return 0
        for day, path in days:
            need = set(old_cells(path.read_text(encoding="utf-8")))
            try:
                ids = unique_prefixes(a.id for a in fetch_assets(client, base, day, tz))
            except Exception as e:
                print(f"immich: {day} запрос не удался: {e}", file=sys.stderr)
                left += len(need)
                continue
            found = need & ids.keys()
            left += len(need - found)
            if dry_run:
                print(_("[dry-run] {day}: ссылками станут {found} превью из {need}").format(day=day, found=len(found), need=len(need)))
                linked += len(found)
            elif found:
                # В итог идёт только то, что действительно записано.
                try:
                    if diary.rewrite(path, lambda text, ids=ids: relink_block(text, ids, base)):
                        changed += 1
                        linked += len(found)
                    else:
                        left += len(found)
                except (OSError, diary.DayFileChanged) as e:
                    print(f"immich: {day} не переписан: {e}", file=sys.stderr)
                    left += len(found)
    finally:
        client.close()
    print(_("immich: дней с превью прежнего вида {days}, переписано {changed}, "
            "ссылками стали {linked} превью, без ссылки остались {left}").format(
                days=len(days), changed=changed, linked=linked, left=left))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry_run = "--dry-run" in argv
    relink_mode = "--relink" in argv

    load_env()
    api_key = os.environ.get("IMMICH_API_KEY", "").strip()
    if not api_key:
        print(_("immich: IMMICH_API_KEY не задан — слой выключен"))
        return 0
    base = os.environ.get("IMMICH_URL", "").strip().rstrip("/")
    if not base:
        print(_("immich: IMMICH_URL не задан — слой выключен"))
        return 0
    vault_root = diary.vault_dir()
    vault_dir = vault_root / diary.DIARY_SUBDIR
    if not vault_dir.is_dir():
        print(f"immich: vault недоступен: {vault_dir}", file=sys.stderr)
        return 1
    tz = TZ

    def run() -> int:
        if relink_mode:
            return run_relink(vault_dir, base, api_key, tz, dry_run)
        today = datetime.now(tz).date()
        # Сначала ни разу не обработанные дни, затем давно пробованные:
        # застрявшие 30 дней не держат очередь (ревью 11.10.2026).
        # Вчерашний день идёт первым при любом завале, остальные как прежде
        # (решение владельца 12.10.2026).
        yesterday = today - timedelta(days=1)
        pending = [d for d in find_pending_days(vault_dir, limit=None) if d < today]
        pending.sort(key=lambda d: (d != yesterday, read_state(d).get("last_attempt", ""), d))
        pending = pending[:PENDING_PER_RUN]
        closed_dates = {}
        for p in PHOTOS_STATE.glob("????-??-??.json"):
            try:
                closed_dates[p.stem] = str(read_state(date.fromisoformat(p.stem)).get("closed_on", p.stem))
            except ValueError:
                continue    # чужой файл с похожим именем (ревью 11.10.2026)
        recheck = [d for d in find_recheck_days(vault_dir, today, RECHECK_DAYS, closed_dates)
                   if d not in pending]
        # Окно от даты закрытия после простоя Immich вмещает сотни дней:
        # без предела они съедали бы весь прогон и pending стоял бы
        # (ревью 11.10.2026).
        recheck.sort(key=lambda d: (read_state(d).get("last_attempt", ""), d))
        recheck = sorted(recheck[:RECHECK_PER_RUN])
        if not pending and not recheck:
            print(_("immich: незакрытых и недавних дней нет"))
            return 0

        # Клиент закрываем в finally, а не через `with`: ping делается ДО блока,
        # а повторный вход в контекст httpx.Client запрещён.
        client = httpx.Client(headers={"x-api-key": api_key}, timeout=TIMEOUT)
        try:
            client.get(f"{base}/api/server/ping").raise_for_status()
        except Exception as e:
            # Штатно: сервер ручного запуска. pending останется, доберём потом.
            print(_("immich: Immich недоступен ({error}) — отложено").format(error=e))
            client.close()
            return 0

        closed = 0
        written = 0
        started = time.monotonic()
        try:
            # Перепроверка первой: это по одному быстрому запросу на день, а у
            # дня есть срок (через RECHECK_DAYS суток он выходит из окна). Долгий
            # хвост pending иначе съедал бы всё время, и день так и уходил.
            for day in recheck + pending:
                if time.monotonic() - started > DEADLINE_SEC:
                    # Запуск идёт хвостом утреннего пробуждения и не должен
                    # упереться в предел времени юнита: остальное доберём завтра.
                    print(_("immich: время вышло, остальные дни в следующий раз"))
                    break
                # Даже сбой запроса или неоднозначный блок уступает очередь
                # следующему дню при новом прогоне (ревью 11.10.2026).
                state = read_state(day)
                if not dry_run:
                    # Не записалось: день всё равно обрабатываем, очередь лишь
                    # не сдвинется (ревью 11.10.2026).
                    state["last_attempt"] = datetime.now(tz).isoformat()
                    save_state(day, state)
                try:
                    assets = fetch_assets(client, base, day, tz)
                except Exception as e:
                    print(f"immich: {day} запрос не удался: {e}", file=sys.stderr)
                    continue

                path = vault_dir / f"{day.year:04d}" / f"{day.isoformat()}.md"
                try:
                    text = path.read_text(encoding="utf-8")
                    known = recorded_count(text)
                    reg_path = vault_dir / "_реестр" / f"{day.isoformat()} кадры.md"
                    registry_old = reg_path.read_text(encoding="utf-8") if reg_path.exists() else ""
                except (OSError, UnicodeDecodeError) as e:
                    print(f"immich: {day}: файл дня не читается: {e}", file=sys.stderr)
                    continue
                if MARK_OPEN in text and photos_block_span(text) is None:
                    print(f"immich: {day}: блок фото неоднозначен, пропущено", file=sys.stderr)
                    continue
                # Полные id, не число кадров; объединение никогда не вычитает
                # удалённые на сервере снимки (ревью 11.10.2026). Реестр,
                # опередивший день (сбой записи дня после подмены реестра), тоже
                # годится: в нём только настоящие кадры, а что из них уже в
                # блоке, видно по самому блоку.
                union = {a.id: a for a in recorded_assets(text, registry_old)}
                try:
                    union.update({a["id"]: Asset(**a) for a in state.get("assets", [])})
                except (TypeError, KeyError):
                    pass    # битое состояние: хватит блока и реестра
                union.update({a.id: a for a in assets})
                assets = list(union.values())
                # Старое превью без полного id тоже остаётся кадром: считаем
                # его отдельно, только если префикса нет в объединении
                # (ревью 11.10.2026).
                missing_prefixes = set(old_cells(text)) - {a.id[:8] for a in assets}
                count = max(len(assets) + len(missing_prefixes), known)
                span = photos_block_span(text)
                block_text = text[span[0]:span[1]] if span else ""
                if known:
                    # Сетка не растёт за PREVIEW_MAX: новые кадры занимают лишь
                    # свободные места, остальные идут в счётчик и реестр. Кадр с
                    # превью прежнего вида (вставка без ссылки) повторно не
                    # ставится (ревью 11.10.2026).
                    slots = PREVIEW_MAX - shown_frames(text)
                    fresh = [a for a in assets if f"/photos/{a.id})" not in block_text
                             and f"{day.isoformat()}_{a.id[:8]}.jpg" not in block_text]
                    keys = pick_key_frames(fresh, slots) if slots > 0 else []
                else:
                    # Блока нет (не было, убран руками или день пересоздан
                    # скелетом): строим его целиком по всем известным кадрам
                    # (ревью 11.10.2026).
                    keys = pick_key_frames(assets)
                if known and count == known and not keys:
                    if not dry_run:
                        status = "synced" if save_close_date(day, state, today) else "pending"
                        apply_to_day(path, day, None, status)
                    continue
                if not assets:
                    if dry_run:
                        print(_("[dry-run] {day}: 0 кадров → фото: none").format(day=day))
                    else:
                        status = "none" if save_close_date(day, state, today) else "pending"
                        changed = apply_to_day(path, day, None, status)
                        if changed:
                            written += 1
                    closed += 1
                    continue

                previews: dict[str, str] = {}
                registry_rel = ""
                timed_out = failed = False
                # Кадры-ключи качаем ВСЕГДА: правило «только если кадров ≤3»
                # почти не срабатывало (реальные дни 30-89 снимков), и день
                # оставался без единой картинки без сети.
                if not dry_run:
                    for a in keys:
                        rel = f"_attachments/дневник/immich/{day.isoformat()}_{a.id[:8]}.jpg"
                        dest = vault_dir.parent / rel
                        if dest.is_file() and dest.stat().st_size > 0:
                            previews[a.id] = rel          # уже скачан прошлым прогоном
                        elif time.monotonic() - started > DEADLINE_SEC:
                            timed_out = True
                            break
                        elif download_preview(client, base, a, dest):
                            previews[a.id] = rel
                        else:
                            failed = True
                    # Время не считается отказом. Любой возраст получает
                    # повтор; лишь реальные отказы ограничены (ревью 11.10.2026).
                    if len(previews) < len(keys) and time.monotonic() - started > DEADLINE_SEC:
                        timed_out = True
                    if failed and not timed_out:
                        state["failures"] = state.get("failures", 0) + 1
                    if len(previews) < len(keys) and (timed_out or state.get("failures", 0) < PREVIEW_RETRIES):
                        print(_("immich: {day}: превью не готовы, отложено").format(day=day))
                        apply_to_day(path, day, None, "pending")
                        save_state(day, state)
                        continue
                registry = None
                if len(assets) > PREVIEW_MAX:
                    registry_rel = f"Дневник/_реестр/{day.isoformat()} кадры"
                    registry = (reg_path, render_registry_note(day, base, assets, tz))

                if known:
                    block = append_photos_block(text, base, keys, previews, count, registry_rel)
                else:
                    block = render_photos_block(base, assets, previews, registry_rel)
                if dry_run:
                    print(_("[dry-run] {day}: {count} кадров, превью {previews}").format(day=day, count=count, previews=len(previews)))
                else:
                    status = "synced" if save_close_date(day, state, today) else "pending"
                    if apply_to_day(path, day, block, status, registry):
                        written += 1
                        # Прежние миниатюры остаются, даже после удаления кадра
                        # в Immich (ревью 11.10.2026).
                        state["failures"] = 0
                        state["assets"] = [asdict(a) for a in assets]
                        save_state(day, state)
                closed += 1
        finally:
            client.close()

        print(_("immich: обработано дней {closed} из {total}, записано {written}").format(
            closed=closed, total=len(pending) + len(recheck), written=written))
        return 0

    try:
        return run()
    finally:
        # Коммит при любом исходе: и после исключения, и когда сервер
        # недоступен, и когда в этом прогоне писать было нечего. Прошлый
        # прогон мог оборваться после записи дня и до git, а счётчик кадров
        # с тех пор совпадает, так что сам он уже ничего не запишет.
        if not dry_run:
            print(_("immich: git: {result}").format(result=diary.commit(vault_root, 'дневник: фото из Immich')))


if __name__ == "__main__":
    sys.exit(main())
