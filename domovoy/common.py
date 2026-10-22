"""Общее для пробуждений, сторожа и синхронизации: пути, пояс, адресат, событие боту.

Бот claude-tg-bot для Домового только транспорт: Домовой шлёт ему POST /event,
а разговор идёт обычной Claude-сессией личного диалога. Больше Домовой о боте
ничего не знает.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv

from .i18n import N_

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_FILE = REPO_ROOT / "prompt.md"
JOURNAL_DIR = REPO_ROOT / "journal"
# Правила дневника отдельным коротким файлом: его называет каждое пробуждение,
# иначе раздел в большом промте забывается первым.
DIARY_RULES_FILE = REPO_ROOT / "diary.md"
# Паспорт мастерской: правила долгой работы в своей теме (проект 22.10.2026).
WORKSHOP_FILE = REPO_ROOT / "workshop.md"
CONTACTS_FILE = REPO_ROOT / "data" / "voronka.md"
# Состояние машины (момент последней проверки сторожа, замок синхронизации)
# живёт вне репозитория: в git ему не место, и сессия claude сторожа не должна
# стартовать внутри репозитория с его CLAUDE.md.
STATE_ROOT = Path(os.environ.get("XDG_STATE_HOME", "").strip() or Path.home() / ".local" / "state") / "domovoy"



def _system_zone_name() -> str:
    """Имя часового пояса системы (Europe/Berlin) или пустая строка."""
    tz = os.environ.get("TZ", "").strip().lstrip(":")
    if "/" in tz:
        return tz
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return ""
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""


def _local_zone() -> ZoneInfo:
    name = os.environ.get("DOMOVOY_TZ", "").strip() or _system_zone_name()
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


# Пояс системы: в нём же идут таймеры systemd, отдельная настройка разъехалась
# бы с ними. DOMOVOY_TZ только для машины, где системный пояс не тот.
TZ = _local_zone()
WEEKDAYS_RU = (N_("понедельник"), N_("вторник"), N_("среда"), N_("четверг"), N_("пятница"), N_("суббота"),
               N_("воскресенье"))
WEEKDAYS_SHORT_RU = (N_("пн"), N_("вт"), N_("ср"), N_("чт"), N_("пт"), N_("сб"), N_("вс"))

DEFAULT_BOT_URL = "http://127.0.0.1:8788/event"
CONNECT_RETRIES = 3        # бот мог ещё не поднять /event после старта системы
CONNECT_RETRY_SLEEP = 20.0
# 503: бот ждёт конца работы перед перезапуском по заявке (claude-tg-bot,
# restart.py) и событие не принял. До 3 минут ожидания: укладывается в
# TimeoutStartSec=300 пробуждений. Дольше ждёт только долгий ран; тогда
# пробуждение пропадает, а сторож повторит через час (состояние не двигает).
BUSY_RETRIES = 7
BUSY_RETRY_SLEEP = 30.0
# Ответ post_event, когда срок события вышел до попытки: запрос не ушёл.
# Отличается от любой ошибки HTTP, после которой бот мог событие принять.
EXPIRED = "срок события вышел, не отправлено"


class ConfigError(Exception):
    pass


def load_env() -> None:
    load_dotenv(REPO_ROOT / ".env")


def read_target() -> tuple[int, int | None]:
    """(chat_id, thread_id) из DOMOVOY_CHAT_ID/DOMOVOY_THREAD_ID или ConfigError."""
    raw_chat = os.environ.get("DOMOVOY_CHAT_ID", "").strip()
    raw_thread = os.environ.get("DOMOVOY_THREAD_ID", "").strip()
    if not raw_chat:
        # Fail-closed: план дня в неправильном чате хуже недоставленного.
        raise ConfigError("DOMOVOY_CHAT_ID не задан")
    try:
        chat_id = int(raw_chat)
        thread_id = int(raw_thread) if raw_thread else None
    except ValueError:
        raise ConfigError("DOMOVOY_CHAT_ID/DOMOVOY_THREAD_ID не числа") from None
    if chat_id == 0:
        # Ноль обработчик /event заменил бы на чат по умолчанию: молчаливая
        # подмена адресата вместо отказа.
        raise ConfigError("DOMOVOY_CHAT_ID=0 недопустим")
    return chat_id, thread_id


def read_workshop_target() -> tuple[int, int] | None:
    """Тема мастерской (DOMOVOY_WORKSHOP_CHAT_ID и DOMOVOY_WORKSHOP_THREAD_ID)
    или None, если она не настроена: тогда долгая работа идёт в личке, как
    раньше. Задана наполовину или не числами: ConfigError (проект 22.10.2026)."""
    raw_chat = os.environ.get("DOMOVOY_WORKSHOP_CHAT_ID", "").strip()
    raw_thread = os.environ.get("DOMOVOY_WORKSHOP_THREAD_ID", "").strip()
    if not raw_chat and not raw_thread:
        return None
    try:
        chat_id, thread_id = int(raw_chat), int(raw_thread)
    except ValueError:
        raise ConfigError("DOMOVOY_WORKSHOP_CHAT_ID/DOMOVOY_WORKSHOP_THREAD_ID не числа") from None
    if chat_id == 0 or thread_id <= 0:
        raise ConfigError("DOMOVOY_WORKSHOP_CHAT_ID/DOMOVOY_WORKSHOP_THREAD_ID недопустимы")
    return chat_id, thread_id


def bot_endpoint() -> tuple[str, str]:
    url = os.environ.get("DOMOVOY_BOT_URL", "").strip() or DEFAULT_BOT_URL
    secret = os.environ.get("DOMOVOY_BOT_SECRET", "").strip()
    return url, secret


def event_key(kind: str, parts: Iterable[str]) -> str:
    """Ключ повтора пробуждения: те же поводы дают тот же ключ, и бот второй
    раз их не разбудит. Ответ 200 мог потеряться, а слушатель и сторож после
    неудачи шлют снова (ревью Codex 22.10.2026)."""
    digest = hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()[:40]
    return f"{kind}-{digest}"


def post_event(url: str, secret: str, payload: dict,
               still_wanted: Callable[[], bool] | None = None) -> tuple[bool, str]:
    """POST в loopback бота. trust_env=False: прокси из окружения не должен
    уносить локальный запрос с токеном наружу. Повтор при отказе соединения
    (бот ещё стартует) и один раз при ответе 502: бот так отвечает, когда
    Telegram не принял уведомление, до запуска рана (ревью 11.10.2026). После
    таймаута чтения и других ответов повторять нельзя, событие уже могло быть
    принято и агент уже разбужен. Попыток соединения не больше CONNECT_RETRIES:
    на этот предел рассчитан срок доставки у сторожа. Ответ 503 (бот готовится
    к перезапуску, событие не принято) повторяется отдельно, до BUSY_RETRIES.
    still_wanted проверяется перед каждой попыткой: событие со сроком (поиск
    до 08:00) после срока не уходит и после повторов (ревью Codex 14.10.2026).
    У пробуждения без своего ключа (event_key) ключ на этот вызов: повторы
    внутри него бот не задвоит."""
    headers = {"X-Approve-Token": secret} if secret else {}
    if payload.get("wake") and not payload.get("key"):
        payload = {**payload, "key": f"once-{uuid.uuid4().hex}"}
    retried_502 = False
    connects = busy = 0
    while True:
        if still_wanted is not None and not still_wanted():
            return False, EXPIRED
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=15.0, trust_env=False)
        except httpx.ConnectError as e:
            connects += 1
            if connects < CONNECT_RETRIES:
                time.sleep(CONNECT_RETRY_SLEEP)
                continue
            return False, f"нет соединения после {connects} попыток: {type(e).__name__}"
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
            return False, f"{type(e).__name__}: {e}"
        if r.status_code == 503:
            busy += 1
            if busy < BUSY_RETRIES:
                time.sleep(BUSY_RETRY_SLEEP)
                continue
            return False, f"бот готовится к перезапуску, {busy} попыток: {r.text[:200]}"
        if r.status_code == 502 and not retried_502:
            retried_502 = True
            time.sleep(CONNECT_RETRY_SLEEP)
            continue
        if r.status_code != 200:
            return False, f"{r.status_code} {r.text[:200]}"
        return True, r.text[:200]
