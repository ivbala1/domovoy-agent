"""Общее для тестов: замок дневника и журнал сторожа во временном каталоге, а
не в живом ~/.local/state/domovoy и journal/ репозитория. Сообщения по-русски,
как в исходнике; DOMOVOY_TEST_LANG=en гоняет набор по-английски."""
from __future__ import annotations

import os

# Пояс тестов без перехода на летнее время, задан до импорта domovoy: TZ
# вычисляется при импорте common из системного или DOMOVOY_TZ.
os.environ.setdefault("DOMOVOY_TZ", "Asia/Dubai")

import pytest  # noqa: E402

from domovoy import diary, i18n, sync  # noqa: E402

try:
    from domovoy import watch
except ImportError:  # открытая копия без сторожа
    watch = None

i18n.set_language(os.environ.get("DOMOVOY_TEST_LANG", "ru"))


@pytest.fixture(autouse=True)
def _diary_lock_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(diary, "LOCK_FILE", tmp_path / "diary.lock")


@pytest.fixture(autouse=True)
def _watch_journal_in_tmp(tmp_path, monkeypatch):
    """Сторож пишет строку в журнал после доставки сводки: в тестах это
    временный каталог, иначе прогон дописывал бы живой journal/."""
    if watch is None:
        return
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setattr(watch, "JOURNAL_DIR", journal)
    # Очередь строк, не дождавшихся замка синхронизации (ревью 11.10.2026).
    monkeypatch.setattr(watch, "JOURNAL_QUEUE_FILE", tmp_path / "watch" / "journal-queue.jsonl")
    # Файлы слушателя (разбуженное, пульс, замок отправки) тоже во временном каталоге.
    monkeypatch.setattr(watch, "LISTEN_DIR", tmp_path / "listen")


@pytest.fixture(autouse=True)
def _sync_state_in_tmp(tmp_path, monkeypatch):
    """Строка сторожа в журнал берёт замок синхронизации (ревью 11.10.2026):
    в тестах его файл во временном каталоге, а не в живом состоянии."""
    monkeypatch.setattr(sync, "STATE_DIR", tmp_path / "sync")
