"""Пробуждения: утром, днём и вечером разбудить агента в личном диалоге Ивана с ботом.

Запускается таймерами domovoy-morning.timer (08:30), domovoy-evening.timer
(20:30 по местному времени) и с 14.10.2026 domovoy-search.timer (07:30 каждый день, до 18.10.2026 в будни:
поиск вакансий отдельным раном до утра, чтобы утро его не ждало), с 22.10.2026
domovoy-search-day.timer (15:00 каждый день: второй проход по свежему); дневной
слот 14:00 без таймера с 22.09.2026, только ручной запуск --midday. Модуль ничего не
сочиняет и claude сам не зовёт: он подтягивает репозиторий (чтобы агент читал
свежий промт), шлёт боту POST /event с wake=true, и дальше работает обычная
Claude-сессия диалога. Правила бот подмешивает пометкой /scope этого чата,
состояние живёт в журнале и в общей памяти, ответы Ивана продолжают ту же сессию.

Единственный гейт: окно местного времени (утро 06:00-12:00, поиск
07:30-08:00, поиск днём 15:00-15:30, день 12:00-18:00, вечер 19:00-23:30). Persistent=true догоняет пропуск после сна ПК,
но утренний план в 15:00 неуместен. Всё остальное (спросить ли ещё раз, что
переносить, писать ли журнал) решает агент, а не программа: указание Ивана
13.09.2026.

Exit-коды: 0 тихо или успех, 1 ошибка конфига или отправки, 2 неверные
аргументы (argparse).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import date, datetime

from . import sync
from .i18n import _
from .common import (
    DIARY_RULES_FILE,
    EXPIRED,
    JOURNAL_DIR,
    PROMPT_FILE,
    TZ,
    WEEKDAYS_RU,
    WORKSHOP_FILE,
    ConfigError,
    bot_endpoint,
    load_env,
    post_event,
    read_target,
    read_workshop_target,
)

MORNING_WINDOW = (6, 12)   # [06:00, 12:00)
# [07:30, 08:00): поиск кончается до утра 08:30. Догон после сна ПК позже
# 08:00 молчит: иначе поиск держал бы утро в очереди или шёл за ним и свежей
# сессией стирал утреннюю; пропущенное берёт следующий поиск окном 1-3 дня.
# С 07:30, а не раньше: догон до 07:30 и плановый запуск дали бы два поиска.
SEARCH_WINDOW = ((7, 30), (8, 0))
# [15:00, 15:30): второй проход по свежему (решение Ивана 22.10.2026). Догон
# после сна позже 15:30 молчит: свежее возьмёт поиск следующего утра.
SEARCH_DAY_WINDOW = ((15, 0), (15, 30))
MIDDAY_WINDOW = (12, 18)   # [12:00, 18:00): узкая проверка, план и вопросы не сюда
EVENING_WINDOW = (19, 23)  # [19:00, 23:30]
EVENING_LAST_MINUTE = 30
# Сколько пробуждение ждёт замок, чтобы прочитать причину остановки идущего
# прогона синхронизации (ревью 11.10.2026, третий круг): прогон сам ждёт
# замок до 90 с, а служба пробуждения ограничена 300 с.
STATE_WAIT_SEC = 90.0


def window_end(now_local: datetime, slot: str) -> float:
    """Конец окна поиска, Unix-время: бот после него событие не стартует."""
    end_h, end_m = (SEARCH_WINDOW if slot == "search" else SEARCH_DAY_WINDOW)[1]
    return now_local.replace(hour=end_h, minute=end_m, second=0, microsecond=0).timestamp()


def in_window(now_local: datetime, slot: str) -> bool:
    h, m = now_local.hour, now_local.minute
    if slot == "morning":
        return MORNING_WINDOW[0] <= h < MORNING_WINDOW[1]
    if slot == "search":
        return SEARCH_WINDOW[0] <= (h, m) < SEARCH_WINDOW[1]
    if slot == "search_day":
        return SEARCH_DAY_WINDOW[0] <= (h, m) < SEARCH_DAY_WINDOW[1]
    if slot == "midday":
        return MIDDAY_WINDOW[0] <= h < MIDDAY_WINDOW[1]
    if h < EVENING_WINDOW[0]:
        return False
    if h < EVENING_WINDOW[1]:
        return True
    return h == EVENING_WINDOW[1] and m <= EVENING_LAST_MINUTE


def build_prompt(slot: str, today: date, prompt_file: str, journal_dir: str) -> tuple[str, str]:
    """(title, prompt). Только дата и момент дня: режимов (будни, выходной,
    разбор недели) в коде нет, что сейчас полезно, решает агент по промту,
    журналу, календарю и инбоксу (указание Ивана 13.09.2026). ⚠️ Имена
    заказчиков в тексте запрещены: первое сообщение сессии закрепляет проект."""
    day = _("{date} ({weekday})").format(date=today.isoformat(), weekday=_(WEEKDAYS_RU[today.weekday()]))
    common = _(
        "Действуй по промту Домового {prompt_file}; журнал в {journal_dir} "
        "(файлы ГГГГ-ММ.md). Дневник владельца ведёшь ты, правила в {diary_rules}. "
        "Коротко, для телефона, без markdown."
    ).format(prompt_file=prompt_file, journal_dir=journal_dir, diary_rules=DIARY_RULES_FILE)
    if slot == "morning":
        return _("Утро"), _(
            "Утро, {day}. {common} Человека за клавиатурой нет: если запись "
            "в журнал отклонена или не подтверждена, не повторяй запрос, закончи ответ и скажи, "
            "что план предложен и не сохранён."
        ).format(day=day, common=common)
    if slot == "search":
        # Что искать, как отправлять и чем отчитаться, написано в промте
        # (абзац «Поиск»): код называет только момент. Дневник поиску не
        # нужен, поэтому без common.
        return _("Поиск"), _(
            "Поиск, {day}. Действуй по промту Домового {prompt_file}, абзац «Поиск»; "
            "журнал в {journal_dir} (файлы ГГГГ-ММ.md). Коротко, для телефона, без markdown. "
            "Человека за клавиатурой нет: если запись в журнал отклонена или не подтверждена, "
            "не повторяй запрос и закончи ответ."
        ).format(day=day, prompt_file=prompt_file, journal_dir=journal_dir)
    if slot == "search_day":
        # Иван днём не спит, поэтому без оговорки «человека нет»: что делать,
        # написано в промте (абзац «Поиск днём»).
        return _("Поиск днём"), _(
            "Поиск днём, {day}. Действуй по промту Домового {prompt_file}, абзац «Поиск днём»; "
            "журнал в {journal_dir} (файлы ГГГГ-ММ.md). Коротко, для телефона, без markdown."
        ).format(day=day, prompt_file=prompt_file, journal_dir=journal_dir)
    if slot == "midday":
        # Что именно проверять и когда молчать, написано в промте (абзац «Днём»):
        # код называет только момент дня.
        return _("День"), _(
            "День, {day}. {common} Человека за клавиатурой нет: если запись "
            "в журнал отклонена или не подтверждена, не повторяй запрос и закончи ответ."
        ).format(day=day, common=common)
    return _("Вечер"), _("Вечер, {day}. {common}").format(day=day, common=common)


BRIEF_LIMIT = 6000


def workshop_note() -> str:
    return _("Ты в мастерской Домового: сначала прочитай {workshop_file} (правила мастерской, "
             "модели субагентов, какие абзацы промта нужны), из {prompt_file} читай только эти абзацы.").format(
        workshop_file=WORKSHOP_FILE, prompt_file=PROMPT_FILE)


def _post_job(args: argparse.Namespace, workshop: tuple[int, int] | None) -> int:
    """Наряд из лички в мастерскую (проект 22.10.2026): текст наряда пишет
    личка в файл, событие с секретом шлёт этот скрипт, агенту секрет не виден.
    Свежая сессия, без звука и без карточки; срок сутки, наряд может ждать
    конца предыдущего."""
    if workshop is None:
        print("domovoy.wake: мастерская не настроена (DOMOVOY_WORKSHOP_CHAT_ID), наряд не отправлен",
              file=sys.stderr)
        return 1
    try:
        brief = Path(args.brief_file).read_text(encoding="utf-8").strip() if args.brief_file else ""
    except OSError as e:
        print(f"domovoy.wake: наряд не прочитан: {e}", file=sys.stderr)
        return 1
    if not brief:
        print("domovoy.wake: нужен непустой --brief-file", file=sys.stderr)
        return 1
    if len(brief) > BRIEF_LIMIT:
        print(f"domovoy.wake: наряд длиннее {BRIEF_LIMIT} знаков, сократи (чужую переписку ссылкой)",
              file=sys.stderr)
        return 1
    chat_id, thread_id = workshop
    first = " ".join(brief.splitlines()[0].split())[:50]
    title = _("Наряд: {first}").format(first=first)
    prompt = _("[Наряд мастерской] {note}\n\nНаряд:\n{brief}").format(note=workshop_note(), brief=brief)
    payload = {"source": _("Домовой"), "title": title, "text": "", "wake": True, "prompt": prompt,
               "chat_id": chat_id, "thread_id": thread_id, "fresh": True, "silent": True,
               "notice": False, "expires_in": 24 * 3600}
    if args.dry_run:
        print(f"[dry-run] chat={chat_id} thread={thread_id}\n{title}\n{prompt}")
        return 0
    url, secret = bot_endpoint()
    ok, info = post_event(url, secret, payload)
    if not ok:
        print(f"domovoy.wake: наряд не принят: {info}", file=sys.stderr)
        return 1
    print("domovoy.wake: наряд принят мастерской")
    return 0


def refresh() -> str:
    """Подтянуть репозиторий перед пробуждением. Неудача не отменяет
    пробуждение: вчерашний промт лучше пропущенного утра. Замок не ждём:
    если синхронизация уже идёт, она и подтянет, а время службы ограничено.

    Отдаёт предупреждение агенту, если синхронизация стоит, или пусто (ревью
    11.10.2026): агент с полными правами иначе писал бы в файлы с маркерами
    конфликта или не знал бы, что данные с другой машины не подтянуты.
    Причину называет сам прогон; замок держал другой прогон, тогда причину,
    которую тот сохранил (ревью 11.10.2026, второй круг); её нет или прогон
    упал, её ищут в самом репозитории."""
    try:
        rc, stop = sync.run_detailed(lock_wait=0)
    except Exception as e:  # noqa: BLE001
        print(f"domovoy.wake: синхронизация упала ({e!r}), будим по тому, что есть", file=sys.stderr)
    else:
        if rc == sync.OK:
            return ""
        print(f"domovoy.wake: синхронизация вернула {rc}, будим по тому, что есть", file=sys.stderr)
        if rc == sync.CONFLICT and not stop.kind:
            stop = sync.Stop(sync.DIVERGED)          # при конфликте предупреждаем всегда
        if stop.kind:
            return sync_warning(stop)
        # Замок держал другой прогон (ревью 11.10.2026, второй круг): его
        # причину, в том числе обычный конфликт с откатом и ожидание правки
        # кода, в дереве не видно, она сохранена им самим.
        try:
            stop = sync.saved_stop(STATE_WAIT_SEC)
        except Exception as e:  # noqa: BLE001
            print(f"domovoy.wake: причина остановки не прочитана ({e!r})", file=sys.stderr)
        if stop.kind:
            return sync_warning(stop)
    try:
        reason = sync.blocked_reason(wait=STATE_WAIT_SEC)
    except Exception as e:  # noqa: BLE001
        print(f"domovoy.wake: состояние репозитория не прочитано ({e!r})", file=sys.stderr)
        return ""
    if reason is None:
        return sync_warning(sync.Stop(sync.RUNNING))
    return sync_warning(sync.Stop(sync.BLOCKED, reason)) if reason else ""


def sync_warning(stop: sync.Stop) -> str:
    """Предупреждение агенту в начало события, своё на каждую причину
    остановки синхронизации (ревью 11.10.2026)."""
    if stop.kind == sync.RUNNING:
        return _(
            "⚠️ Синхронизация репозитория Домового ещё идёт: другой прогон не закончился за {seconds:g} с, "
            "поэтому записи с другой машины могут быть не подтянуты, а состояние репозитория не проверено. "
            "Перед записью в журнал, data/, prompt.md и diary.md проверь git status: если идёт rebase или merge "
            "или в файлах есть маркеры конфликта <<<<<<< и >>>>>>>, ничего не записывай. Скажи владельцу одной "
            "строкой, что синхронизация не закончилась вовремя."
        ).format(seconds=STATE_WAIT_SEC)
    if stop.kind == sync.WAITING:
        return _(
            "⚠️ Синхронизация репозитория Домового ждёт: на ПК незакоммиченная правка кода ({detail}), "
            "поэтому новые коммиты с GitHub сюда не подтянуты: журнал, таблицы и промт могут быть не самыми "
            "свежими, записей с другой машины здесь нет. Свои записи делай как обычно. Скажи владельцу одной "
            "строкой, что синхронизация ждёт, пока правку кода закоммитят или уберут."
        ).format(detail=stop.detail)
    if stop.kind == sync.DIVERGED:
        where = _(" в {detail}").format(detail=stop.detail) if stop.detail else ""
        return _(
            "⚠️ Синхронизация репозитория Домового стоит на конфликте с GitHub{where}: записи этой машины "
            "целы в локальном коммите, а записи с другой машины сюда не подтянуты, поэтому журнал и таблицы "
            "могут быть неполными. Уже записанное в этих файлах не переписывай, расхождение сводит владелец; "
            "новое дописывать можно. Скажи владельцу одной строкой, что синхронизация ждёт его разбора."
        ).format(where=where)
    return _(
        "⚠️ Синхронизация репозитория Домового стоит: {detail}. До разбора данные не правь: "
        "в журнал, data/, prompt.md и diary.md ничего не записывай (в файлах могут быть "
        "маркеры конфликта <<<<<<< и >>>>>>>, их сводит владелец). Скажи владельцу одной строкой, "
        "что синхронизация ждёт его разбора."
    ).format(detail=stop.detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="domovoy.wake")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--morning", action="store_const", const="morning", dest="slot")
    g.add_argument("--search", action="store_const", const="search", dest="slot")
    g.add_argument("--search-day", action="store_const", const="search_day", dest="slot")
    g.add_argument("--midday", action="store_const", const="midday", dest="slot")
    g.add_argument("--evening", action="store_const", const="evening", dest="slot")
    g.add_argument("--job", action="store_const", const="job", dest="slot",
                   help="наряд мастерской: текст из --brief-file (проект 22.10.2026)")
    parser.add_argument("--brief-file", default="", help="файл с нарядом для --job")
    parser.add_argument("--dry-run", action="store_true", help="показать событие, не слать и git не трогать")
    parser.add_argument("--force", action="store_true", help="игнорировать окно времени")
    parser.add_argument("--day", default="", help="дата ГГГГ-ММ-ДД вместо сегодняшней (ручной запуск)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    load_env()
    try:
        chat_id, thread_id = read_target()
        # Мастерская нужна только поискам и нарядам: опечатка в её настройке
        # не должна останавливать утро и вечер.
        workshop = read_workshop_target() if args.slot in ("search", "search_day", "job") else None
    except ConfigError as e:
        print(f"domovoy.wake: {e}, выходим", file=sys.stderr)
        return 1
    if args.slot == "job":
        return _post_job(args, workshop)

    now_local = datetime.now(TZ)
    real_today = now_local.date()
    today = real_today
    if args.day:
        try:
            today = date.fromisoformat(args.day)
        except ValueError:
            print(f"domovoy.wake: --day {args.day!r} не дата", file=sys.stderr)
            return 1
        if today != real_today and not args.dry_run:
            # 13.09.2026 проба с --day разбудила живой чат и тренер спланировал
            # рабочий понедельник в семейное воскресенье. Чужая дата только
            # в сухом прогоне.
            print("domovoy.wake: --day с чужой датой только вместе с --dry-run", file=sys.stderr)
            return 1

    if not args.force and not in_window(now_local, args.slot):
        print(f"domovoy.wake: вне окна ({args.slot}, {now_local:%H:%M}), молчим")
        return 0

    title, prompt = build_prompt(args.slot, today, str(PROMPT_FILE), str(JOURNAL_DIR))
    if today != real_today:
        title = _("{title} (пробный запуск)").format(title=title)
        prompt = _(
            "⚠️ ПРОБНЫЙ ЗАПУСК, дата подставлена флагом --day. Реальная дата "
            "{real_date} ({weekday}), в тексте ниже "
            "{date}.\n\n{prompt}"
        ).format(real_date=real_today.isoformat(), weekday=_(WEEKDAYS_RU[real_today.weekday()]),
                 date=today.isoformat(), prompt=prompt)
    payload: dict = {
        "source": _("Домовой"),
        "title": title,
        "text": "",
        "wake": True,
        "prompt": prompt,
        "chat_id": chat_id,
    }
    if args.slot == "morning":
        # Утро начинает день свежей сессией: состояние в журнале, а сессия за
        # сутки распухает (14.09: 666 тысяч токенов контекста, 3,5 $ за ход).
        payload["fresh"] = True
        # Утро без звука: уведомление и все сообщения рана приходят тихо, Иван
        # может ещё спать (просьба Ивана 09.10.2026). Вечер и сторож со звуком.
        payload["silent"] = True
    if args.slot == "search":
        # Поиск в 07:30 начинает свежую сессию и идёт без звука: Иван может
        # спать, а вчерашняя сессия большая. Утро в 08:30 начнёт свою свежую
        # и узнает итог поиска из журнала (решение Ивана 14.10.2026).
        payload["fresh"] = True
        payload["silent"] = True
    if args.slot == "search_day":
        # Без звука: Иван может быть за рулём (решение Ивана 22.10.2026). В
        # мастерской сессия свежая (ниже); без неё поиск идёт в дневной сессии
        # лички, не свежей: Иван днём отвечает на кнопки и вопросы.
        payload["silent"] = True
    if args.slot in ("search", "search_day") and workshop is not None:
        # Оба поиска идут в мастерской (решение Ивана 22.10.2026, решение 4):
        # личка свободна, утро не ждёт поиска. Сессия там всегда свежая:
        # задание по кнопке отклика самодостаточно, контекст поиска ему не нужен.
        chat_id, thread_id = workshop
        payload["chat_id"] = chat_id
        payload["fresh"] = True
        payload["prompt"] = workshop_note() + "\n\n" + payload["prompt"]
    if args.slot in ("morning", "evening", "search", "search_day"):
        # Без карточки «🔔 Домовой: Утро», «Вечер» и «Поиск»: текста у события нет,
        # пустая карточка приходила раньше ответа, первым теперь сам ответ
        # (просьба Ивана 14.10.2026). Старый бот поле не знает и шлёт карточку
        # как прежде. Ручной --midday с карточкой.
        payload["notice"] = False
    if thread_id is not None:
        payload["thread_id"] = thread_id
    if args.dry_run:
        print(f"[dry-run] chat={chat_id} thread={thread_id}\n{title}\n{prompt}")
        return 0

    warning = refresh()
    if warning:
        payload["prompt"] = f"{warning}\n\n{payload['prompt']}"
    # Поиск обязан кончиться до утра: окно проверяется ещё раз перед каждой
    # попыткой отправки, синхронизация и повторы занимают минуты (ревью Codex
    # 14.10.2026). Поиск днём так же: окно 15:30. Утро и вечер с широким
    # окном так не ограничены.
    gate = None
    if args.slot in ("search", "search_day") and not args.force:
        gate = lambda: in_window(datetime.now(TZ), args.slot)  # noqa: E731
        # Срок и у бота: принятое событие, которое ждало рестарта или чужого
        # рана, после конца окна не стартует (ревью Codex 22.10.2026).
        payload["expires_at"] = window_end(now_local, args.slot)
    url, secret = bot_endpoint()
    ok, info = post_event(url, secret, payload, still_wanted=gate)
    if not ok and info == EXPIRED:
        # Только отказ самой проверки окна: запрос не уходил. Ошибка HTTP
        # после 08:00 остаётся ошибкой, бот мог событие принять (ревью Codex).
        print("domovoy.wake: окно поиска закрылось до отправки, молчим")
        return 0
    if not ok:
        print(f"domovoy.wake: /event не принят: {info}", file=sys.stderr)
        return 1
    print(f"domovoy.wake: разбудил ({title})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
