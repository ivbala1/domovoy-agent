"""Юнит пробуждения упал или убит systemd (OnFailure=): записать сбой туда же,
откуда его берёт сторож (FAILED_FILE), и он назовёт его Ивану одним тихим
сообщением. Аудит 11.2026: упавший до отправки (ошибка импорта) или убитый по
TimeoutStartSec юнит сам сбой на диск не пишет.

Вызывается юнитом domovoy-wake-failed@<имя упавшего юнита>.service. Если сам
wake.py за последние минуты уже записал причину (бот не принял событие), она
точнее и остаётся.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

# Только common: импорт wake и его зависимостей упал бы вместе с тем, о чём пишем.
from .common import STATE_ROOT, TZ
from .statefile import flock, read_json, write_json

# Пробуждения, которые бот не принял (аудит 11.2026): юнит падает, повтора нет,
# а сказать Ивану через лежащего бота нельзя. Сбой лежит здесь, его называет
# ближайший прогон сторожа (watch.wake_failure_notice), удачное пробуждение
# того же слота его снимает. {слот: {"at": ISO, "title": ..., "info": ...}}
FAILED_FILE = STATE_ROOT / "wake" / "failed.json"


# Чтение, правка и запись failed.json под замком: участников пять (wake, avia,
# interview, сторож, юнит OnFailure), без замка новый сбой мог пропасть.
LOCK_WAIT_SEC = 10


def _lock():
    return flock(FAILED_FILE.with_suffix(".lock"), LOCK_WAIT_SEC)


def wake_failures() -> dict[str, dict]:
    raw = read_json(FAILED_FILE, {})
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)
            and all(isinstance(v.get(f), str) for f in ("at", "title", "info"))}


def _write_failures(failed: dict[str, dict]) -> None:
    if not failed:
        FAILED_FILE.unlink(missing_ok=True)
        return
    FAILED_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(FAILED_FILE, failed)


def note_wake_failure(slot: str, title: str, info: str, when: datetime) -> bool:
    """False: записать не удалось, вызывающий не должен считать сбой названным."""
    try:
        with _lock():
            failed = wake_failures()
            failed[slot] = {"at": when.isoformat(timespec="minutes"), "title": title, "info": info[:200]}
            _write_failures(failed)
    except OSError as e:
        print(f"domovoy.wake: сбой пробуждения не записан: {e}", file=sys.stderr)
        return False
    return True


def forget_wake_failures(slots: list[str], told: dict[str, dict] | None = None) -> None:
    """told: снимок, о котором уже сказали; новый сбой того же слота остаётся."""
    try:
        with _lock():
            failed = wake_failures()
            gone = [s for s in slots if s in failed and (told is None or failed[s] == told.get(s))]
            if gone:
                _write_failures({k: v for k, v in failed.items() if k not in gone})
    except OSError as e:
        print(f"domovoy.wake: сбой пробуждения не снят: {e}", file=sys.stderr)

# Упавший юнит -> (слот в failed.json, название пробуждения для Ивана).
UNITS = {
    "domovoy-morning.service": ("morning", "Утро"),
    "domovoy-evening.service": ("evening", "Вечер"),
    "domovoy-prep.service": ("prep", "Подготовка"),
    "domovoy-search.service": ("search", "Поиск"),
    "domovoy-search-day.service": ("search_day", "Поиск днём"),
    # Сторож цены билетов (05.11.2026): календарь цен не ответил или наряд не принят.
    "domovoy-avia.service": ("avia", "Сторож цены билетов"),
}
FRESH = timedelta(minutes=15)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    unit = args[0] if args else ""
    if unit not in UNITS:
        print(f"domovoy.wakefail: неизвестный юнит «{unit}»", file=sys.stderr)
        return 1
    slot, title = UNITS[unit]
    now = datetime.now(TZ)
    known = wake_failures().get(slot)
    if known:
        try:
            if now - datetime.fromisoformat(known["at"]) < FRESH:
                print(f"domovoy.wakefail: {slot}: причина уже записана")
                return 0
        except ValueError:
            pass
    note_wake_failure(slot, title, f"юнит {unit} упал или убит systemd до отправки "
                           f"(journalctl --user -u {unit})", now)
    print(f"domovoy.wakefail: {slot}: сбой записан")
    return 0


if __name__ == "__main__":
    sys.exit(main())
