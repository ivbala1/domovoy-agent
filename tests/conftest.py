"""Общее для тестов: замок дневника и журнал сторожа во временном каталоге, а
не в живом ~/.local/state/domovoy и journal/ репозитория. Сообщения по-русски,
как в исходнике; DOMOVOY_TEST_LANG=en гоняет набор по-английски."""
from __future__ import annotations

import os

# Пояс тестов без перехода на летнее время, задан до импорта domovoy: TZ
# вычисляется при импорте common из системного или DOMOVOY_TZ.
os.environ.setdefault("DOMOVOY_TZ", "Asia/Dubai")

import pytest  # noqa: E402

from domovoy import diary, i18n, journal, sync  # noqa: E402

try:
    from domovoy import watch
except ImportError:  # открытая копия без сторожа
    watch = None

try:
    from domovoy import reflect
except ImportError:  # открытая копия без рефлексии
    reflect = None

i18n.set_language(os.environ.get("DOMOVOY_TEST_LANG", "ru"))


@pytest.fixture(autouse=True)
def _diary_lock_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(diary, "LOCK_FILE", tmp_path / "diary.lock")


@pytest.fixture(autouse=True)
def _watch_journal_in_tmp(tmp_path, monkeypatch):
    """Сторож и слушатель пишут строку в журнал (domovoy.journal): в тестах
    это временный каталог, иначе прогон дописывал бы живой journal/."""
    jdir = tmp_path / "journal"
    jdir.mkdir()
    monkeypatch.setattr(journal, "JOURNAL_DIR", jdir)
    # Очередь строк, не дождавшихся замка синхронизации (ревью 11.10.2026).
    monkeypatch.setattr(journal, "QUEUE_FILE", tmp_path / "watch" / "journal-queue.jsonl")
    if watch is None:
        return
    monkeypatch.setattr(watch, "JOURNAL_DIR", jdir)
    # Файлы слушателя (разбуженное, пульс, замок отправки) тоже во временном каталоге.
    monkeypatch.setattr(watch, "LISTEN_DIR", tmp_path / "listen")
    # Срочное без ответа: тесты не читают живой data/srochno.md.
    monkeypatch.setattr(watch, "SROCHNO_FILE", tmp_path / "srochno.md")
    # Вопрос об автоотказе (22.10.2026) по умолчанию «отказов нет»: тесты прогона
    # подменяют оценку одним ответом на весь прогон, и второй вопрос получил бы
    # те же оценки. Тесты отказа берут настоящую функцию (REAL_REJECT_SCORES).
    monkeypatch.setattr(watch, "reject_scores", lambda *a, **kw: {})
    # Контекст чата Telegram (разбор 05.11.2026) по умолчанию прочитан и пуст:
    # тесты не ходят в живой Telegram. Ветку сбоя проверяет test_watch_context.
    monkeypatch.setattr(watch.sources, "telegram_context", lambda keys, budget=60: {k: [">> повод"] for k in keys})


@pytest.fixture(autouse=True)
def _wake_state_in_tmp(tmp_path, monkeypatch):
    """Недошедшие пробуждения (аудит 11.2026) пишутся во временный каталог."""
    from domovoy import wake, wakefail
    monkeypatch.setattr(wakefail, "FAILED_FILE", tmp_path / "wake" / "failed.json")


@pytest.fixture(autouse=True)
def _reflect_state_in_tmp(tmp_path, monkeypatch):
    """Утро читает состояние ночной рефлексии: в тестах не живой reflect.json."""
    if reflect is not None:
        monkeypatch.setattr(reflect, "STATE_FILE", tmp_path / "reflect.json")


@pytest.fixture(autouse=True)
def _sync_state_in_tmp(tmp_path, monkeypatch):
    """Строка сторожа в журнал берёт замок синхронизации (ревью 11.10.2026):
    в тестах его файл во временном каталоге, а не в живом состоянии."""
    monkeypatch.setattr(sync, "STATE_DIR", tmp_path / "sync")
