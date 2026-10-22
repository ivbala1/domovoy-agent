"""Тесты пробуждений: окно времени, состав события, fail-closed конфиг, повтор соединения."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domovoy import common  # noqa: E402
from domovoy import wake  # noqa: E402

TZ = ZoneInfo("Asia/Dubai")
BASE_ENV = {"PATH": os.environ.get("PATH", "")}


def _at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 14, h, m, tzinfo=TZ)


def _env(**kw: str) -> dict[str, str]:
    return {**BASE_ENV, **kw}


class WindowTests(unittest.TestCase):
    def test_morning_window(self):
        self.assertTrue(wake.in_window(_at(8, 31), "morning"))
        self.assertTrue(wake.in_window(_at(6, 0), "morning"))
        self.assertFalse(wake.in_window(_at(12, 0), "morning"))
        self.assertFalse(wake.in_window(_at(15, 0), "morning"))

    def test_search_window(self):
        """Поиск только 07:30-08:00: кончается до утра, поздний догон молчит."""
        self.assertTrue(wake.in_window(_at(7, 30), "search"))
        self.assertTrue(wake.in_window(_at(7, 59), "search"))
        self.assertFalse(wake.in_window(_at(7, 29), "search"))
        self.assertFalse(wake.in_window(_at(8, 0), "search"))
        self.assertFalse(wake.in_window(_at(6, 0), "search"))
        self.assertFalse(wake.in_window(_at(9, 0), "search"))

    def test_search_day_window(self):
        """Поиск днём только 15:00-15:30, поздний догон молчит."""
        self.assertTrue(wake.in_window(_at(15, 0), "search_day"))
        self.assertTrue(wake.in_window(_at(15, 29), "search_day"))
        self.assertFalse(wake.in_window(_at(14, 59), "search_day"))
        self.assertFalse(wake.in_window(_at(15, 30), "search_day"))
        self.assertFalse(wake.in_window(_at(7, 30), "search_day"))

    def test_midday_window(self):
        self.assertTrue(wake.in_window(_at(14, 0), "midday"))
        self.assertTrue(wake.in_window(_at(12, 0), "midday"))
        self.assertTrue(wake.in_window(_at(17, 59), "midday"))
        self.assertFalse(wake.in_window(_at(18, 0), "midday"))
        self.assertFalse(wake.in_window(_at(8, 30), "midday"))
        self.assertFalse(wake.in_window(_at(20, 30), "midday"))

    def test_evening_window(self):
        self.assertTrue(wake.in_window(_at(20, 30), "evening"))
        self.assertTrue(wake.in_window(_at(23, 30), "evening"))
        self.assertFalse(wake.in_window(_at(23, 31), "evening"))
        self.assertFalse(wake.in_window(_at(9, 0), "evening"))


class PromptTests(unittest.TestCase):
    def test_no_modes_in_code(self):
        """Режимов (разбор недели, будни, выходной) в коде нет: что делать,
        решает агент по промту (указание Ивана 13.09.2026)."""
        title, prompt = wake.build_prompt("morning", date(2026, 9, 13), "P", "J")
        self.assertNotIn("разбор недели", prompt)
        self.assertNotIn("Воскресенье", title)
        self.assertIn("P", prompt)
        self.assertIn("J", prompt)

    def test_weekday_morning_and_evening_differ(self):
        t1, p1 = wake.build_prompt("morning", date(2026, 9, 14), "P", "J")
        t2, p2 = wake.build_prompt("evening", date(2026, 9, 14), "P", "J")
        self.assertIn("Утро", p1)
        self.assertIn("Вечер", p2)
        self.assertNotEqual(t1, t2)

    def test_midday_is_its_own_moment(self):
        t, p = wake.build_prompt("midday", date(2026, 9, 21), "P", "J")
        self.assertEqual(t, "День")
        self.assertIn("День, 2026-09-21", p)
        self.assertNotIn("Утро", p)
        self.assertNotIn("Вечер", p)

    def test_search_is_its_own_moment(self):
        t, p = wake.build_prompt("search", date(2026, 10, 15), "P", "J")
        self.assertEqual(t, "Поиск")
        self.assertTrue(p.startswith("Поиск, 2026-10-15 (четверг). Действуй по промту Домового P"))
        self.assertIn("абзац «Поиск»", p)
        self.assertIn("J", p)
        self.assertNotIn("Утро", p)

    def test_search_day_is_its_own_moment(self):
        t, p = wake.build_prompt("search_day", date(2026, 10, 22), "P", "J")
        self.assertEqual(t, "Поиск днём")
        self.assertTrue(p.startswith("Поиск днём, 2026-10-22 (четверг). Действуй по промту Домового P"))
        self.assertIn("абзац «Поиск днём»", p)
        self.assertNotIn("Человека за клавиатурой нет", p)

class MainTests(unittest.TestCase):
    def setUp(self):
        # Живой запуск подтягивает репозиторий; в тестах git не трогаем.
        patcher = patch.object(wake, "refresh", return_value="")
        self.refresh = patcher.start()
        self.addCleanup(patcher.stop)

    def test_refresh_only_in_live_run(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")):
            wake.main(["--evening", "--dry-run", "--force"])
            self.refresh.assert_not_called()
            wake.main(["--evening", "--force"])
            self.refresh.assert_called_once()

    def test_prompt_points_to_repo_files(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post:
            wake.main(["--evening", "--force"])
        prompt = post.call_args.args[2]["prompt"]
        self.assertIn(str(common.PROMPT_FILE), prompt)
        self.assertIn(str(common.JOURNAL_DIR), prompt)
        self.assertIn(str(common.DIARY_RULES_FILE), prompt)
        self.assertNotIn("Dropbox", prompt)

    def test_fail_closed_without_config(self):
        with patch.dict(os.environ, _env(), clear=True), patch.object(wake, "load_env"):
            self.assertEqual(wake.main(["--morning", "--dry-run"]), 1)

    def test_zero_chat_is_config_error(self):
        # Ноль обработчик /event подменил бы чатом по умолчанию.
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="0"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event") as post:
            self.assertEqual(wake.main(["--morning", "--force"]), 1)
        post.assert_not_called()

    def test_non_numeric_is_config_error(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="abc"), clear=True), patch.object(wake, "load_env"):
            self.assertEqual(wake.main(["--morning", "--force"]), 1)

    def test_dry_run_does_not_post(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event") as post:
            rc = wake.main(["--evening", "--dry-run", "--force"])
        self.assertEqual(rc, 0)
        post.assert_not_called()

    def test_outside_window_is_silent(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event") as post, patch.object(wake, "datetime") as dt:
            dt.now.return_value = _at(15, 0)
            rc = wake.main(["--morning"])
        self.assertEqual(rc, 0)
        post.assert_not_called()

    def test_post_failure_is_error(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(False, "boom")):
            rc = wake.main(["--morning", "--force"])
        self.assertEqual(rc, 1)

    def test_payload_targets_private_chat_without_thread(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183", DOMOVOY_THREAD_ID=""), clear=True), \
                patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post, \
                patch.object(wake, "datetime") as dt:
            dt.now.return_value = _at(9, 0)          # 2026-09-14
            rc = wake.main(["--morning", "--force", "--day", "2026-09-14"])
        self.assertEqual(rc, 0)
        payload = post.call_args.args[2]
        self.assertEqual(payload["chat_id"], 183)
        self.assertNotIn("thread_id", payload)
        self.assertTrue(payload["wake"])
        self.assertEqual(payload["source"], "Домовой")
        self.assertIn("2026-09-14", payload["prompt"])
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:8788/event")

    def test_midday_continues_session(self):
        """Свежую сессию начинает только утро: дневная проверка продолжает
        утреннюю, чтобы видеть план дня."""
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), \
                patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post, \
                patch.object(wake, "datetime") as dt:
            dt.now.return_value = _at(14, 0)
            rc = wake.main(["--midday"])
        self.assertEqual(rc, 0)
        payload = post.call_args.args[2]
        self.assertEqual(payload["title"], "День")
        self.assertNotIn("fresh", payload)

    def test_substituted_day_refused_without_dry_run(self):
        """Чужая дата в --day будит живой чат только в сухом прогоне: 13.09.2026
        проба спланировала рабочий понедельник в семейное воскресенье."""
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), \
                patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post, \
                patch.object(wake, "datetime") as dt:
            dt.now.return_value = _at(9, 0)          # 2026-09-14, понедельник
            rc = wake.main(["--morning", "--force", "--day", "2026-09-20"])
        self.assertEqual(rc, 1)
        post.assert_not_called()

    def test_substituted_day_marked_in_dry_run(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), \
                patch.object(wake, "load_env"), \
                patch.object(wake, "datetime") as dt, \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            dt.now.return_value = _at(9, 0)
            rc = wake.main(["--morning", "--force", "--dry-run", "--day", "2026-09-20"])
        self.assertEqual(rc, 0)
        self.assertIn("пробный запуск", out.getvalue())
        self.assertIn("2026-09-14", out.getvalue())
        self.assertIn("2026-09-20", out.getvalue())

    def test_day_equal_to_today_is_not_marked(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), \
                patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post, \
                patch.object(wake, "datetime") as dt:
            dt.now.return_value = _at(9, 0)
            wake.main(["--morning", "--force", "--day", "2026-09-14"])
        payload = post.call_args.args[2]
        self.assertNotIn("ПРОБНЫЙ ЗАПУСК", payload["prompt"])
        self.assertNotIn("пробный запуск", payload["title"])

    def test_morning_is_fresh_session_evening_is_not(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post:
            wake.main(["--morning", "--force"])
            morning = post.call_args.args[2]
            wake.main(["--evening", "--force"])
            evening = post.call_args.args[2]
        self.assertTrue(morning.get("fresh"))
        self.assertNotIn("fresh", evening)

    def test_only_morning_is_silent(self):
        """Утро приходит без звука, вечер и день со звуком."""
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post:
            payloads = {}
            for slot in ("morning", "midday", "evening"):
                wake.main([f"--{slot}", "--force"])
                payloads[slot] = post.call_args.args[2]
        self.assertIs(payloads["morning"]["silent"], True)
        self.assertNotIn("silent", payloads["midday"])
        self.assertNotIn("silent", payloads["evening"])

    def test_morning_and_evening_have_no_notice(self):
        """Утро и вечер без пустой карточки «🔔 Домовой: …», первым приходит
        ответ; ручной день с карточкой."""
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post:
            payloads = {}
            for slot in ("morning", "midday", "evening"):
                wake.main([f"--{slot}", "--force"])
                payloads[slot] = post.call_args.args[2]
        for slot in ("morning", "evening"):
            self.assertIs(payloads[slot]["notice"], False)
            self.assertTrue(payloads[slot]["wake"])
        self.assertNotIn("notice", payloads["midday"])

    def test_search_payload(self):
        """Поиск 07:30: свежая сессия, без звука, без пустой карточки (решение
        Ивана 14.10.2026: утро не ждёт вакансий)."""
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post, \
                patch.object(wake, "datetime") as dt:
            dt.now.return_value = _at(7, 30)
            rc = wake.main(["--search"])
        self.assertEqual(rc, 0)
        payload = post.call_args.args[2]
        self.assertEqual(payload["title"], "Поиск")
        self.assertTrue(payload["wake"])
        self.assertIs(payload["fresh"], True)
        self.assertIs(payload["silent"], True)
        self.assertIs(payload["notice"], False)
        self.assertIn("абзац «Поиск»", payload["prompt"])

    def test_search_day_payload(self):
        """Поиск 15:00: дневная сессия (не свежая), без звука, без карточки
        (решение Ивана 22.10.2026)."""
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post, \
                patch.object(wake, "datetime") as dt:
            dt.now.return_value = _at(15, 0)
            rc = wake.main(["--search-day"])
        self.assertEqual(rc, 0)
        payload = post.call_args.args[2]
        self.assertEqual(payload["title"], "Поиск днём")
        self.assertNotIn("fresh", payload)
        self.assertIs(payload["silent"], True)
        self.assertIs(payload["notice"], False)
        self.assertIsNotNone(post.call_args.kwargs["still_wanted"])

    def test_search_day_window_closed_during_refresh(self):
        """Начал в 15:29, синхронизация кончилась в 15:31: поиск днём не уходит."""
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(common.httpx, "post") as post, patch.object(wake, "datetime") as dt:
            dt.now.side_effect = [_at(15, 29)] + [_at(15, 31)] * 3
            self.assertEqual(wake.main(["--search-day"]), 0)
        post.assert_not_called()

    def test_search_day_outside_window_is_silent(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event") as post, patch.object(wake, "datetime") as dt:
            dt.now.return_value = _at(15, 40)
            self.assertEqual(wake.main(["--search-day"]), 0)
        post.assert_not_called()

    def test_search_window_closed_during_refresh(self):
        """Начал в 07:59, синхронизация кончилась в 08:02: поиск не уходит."""
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(common.httpx, "post") as post, patch.object(wake, "datetime") as dt:
            dt.now.side_effect = [_at(7, 59)] + [_at(8, 2)] * 3
            self.assertEqual(wake.main(["--search"]), 0)
        post.assert_not_called()

    def test_search_post_error_after_window_stays_error(self):
        """Запрос ушёл в 07:59, таймаут чтения после 08:00: бот мог принять
        событие, это ошибка, а не тихий пропуск."""
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(common.httpx, "post", side_effect=httpx.ReadTimeout("t")) as post, \
                patch.object(wake, "datetime") as dt, patch("sys.stdout", new_callable=io.StringIO) as out:
            dt.now.side_effect = [_at(7, 59)] * 2 + [_at(8, 0)] * 3
            self.assertEqual(wake.main(["--search"]), 1)
        post.assert_called_once()
        self.assertNotIn("окно поиска закрылось", out.getvalue())

    def test_search_outside_window_is_silent(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183"), clear=True), patch.object(wake, "load_env"), \
                patch.object(wake, "post_event") as post, patch.object(wake, "datetime") as dt:
            dt.now.return_value = _at(8, 5)          # ПК проснулся после 08:00
            self.assertEqual(wake.main(["--search"]), 0)
        post.assert_not_called()

    def test_payload_with_thread(self):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="-100", DOMOVOY_THREAD_ID="7"), clear=True), \
                patch.object(wake, "load_env"), \
                patch.object(wake, "post_event", return_value=(True, "ok")) as post:
            wake.main(["--evening", "--force"])
        self.assertEqual(post.call_args.args[2]["thread_id"], 7)


class RefreshTests(unittest.TestCase):
    def test_sync_crash_does_not_cancel_wake(self):
        with patch.object(wake.sync, "run_detailed", side_effect=OSError("каталог состояния недоступен")) as run, \
                patch.object(wake.sync, "blocked_reason", return_value=""):
            self.assertEqual(wake.refresh(), "")
        # Замок не ждём: время службы пробуждения ограничено.
        run.assert_called_once_with(lock_wait=0)

    def test_conflict_always_warns(self):
        # Ревью 11.10.2026 (C49): конфликт без названной причины и без маркеров
        # в дереве всё равно предупреждение.
        with patch.object(wake.sync, "run_detailed", return_value=(wake.sync.CONFLICT, wake.sync.Stop())), \
                patch.object(wake.sync, "blocked_reason", return_value="") as blocked:
            warning = wake.refresh()
        self.assertIn("стоит на конфликте с GitHub", warning)
        self.assertIn("не подтянуты", warning)
        blocked.assert_not_called()


class PostEventTests(unittest.TestCase):
    def test_wake_gets_one_key_for_all_retries(self):
        """Ревью Codex 22.10.2026: повторы внутри вызова бот узнаёт по ключу."""
        seen = []

        def flaky(*a, json=None, **kw):
            seen.append(json.get("key"))
            if len(seen) == 1:
                raise httpx.ConnectError("refused")
            return SimpleNamespace(status_code=200, text="{}")

        with patch.object(common.httpx, "post", side_effect=flaky), patch.object(common.time, "sleep"):
            ok, _ = common.post_event("http://127.0.0.1:1/event", "", {"wake": True})
            common.post_event("http://127.0.0.1:1/event", "", {"wake": False})
        self.assertTrue(ok)
        self.assertEqual(len(set(seen[:2])), 1)
        self.assertTrue(seen[0].startswith("once-"))
        self.assertIsNone(seen[2])  # уведомление без пробуждения ключа не несёт

    def test_own_key_kept_and_stable(self):
        self.assertEqual(common.event_key("listen", ["b", "a"]), common.event_key("listen", ["a", "b"]))
        self.assertNotEqual(common.event_key("listen", ["a"]), common.event_key("watch", ["a"]))
        self.assertRegex(common.event_key("watch", ["x"]), r"^[A-Za-z0-9._:-]{8,128}$")

    def test_retries_on_connect_error_then_gives_up(self):
        calls = []

        def boom(*a, **kw):
            calls.append(kw.get("trust_env"))
            raise httpx.ConnectError("refused")

        with patch.object(common.httpx, "post", side_effect=boom), patch.object(common.time, "sleep") as sl:
            ok, info = common.post_event("http://127.0.0.1:1/event", "", {})
        self.assertFalse(ok)
        self.assertIn("после 3 попыток", info)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(v is False for v in calls))  # trust_env=False в каждой попытке
        self.assertEqual(sl.call_count, 2)

    def test_no_retry_on_read_timeout(self):
        with patch.object(common.httpx, "post", side_effect=httpx.ReadTimeout("slow")) as post, \
                patch.object(common.time, "sleep") as sl:
            ok, _ = common.post_event("http://127.0.0.1:1/event", "", {})
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 1)
        sl.assert_not_called()

    def test_bad_url_is_error_not_exception(self):
        ok, info = common.post_event("http://127.0.0.1:notaport/event", "", {})
        self.assertFalse(ok)
        self.assertTrue(info)

    def test_one_retry_on_502(self):
        """Ревью 11.10.2026 (C45): 502 бот отвечает, когда Telegram не принял
        уведомление, до запуска рана; повтор с паузой не будит агента дважды."""
        responses = [httpx.Response(502, text='{"error": "telegram send failed"}'),
                     httpx.Response(200, text='{"ok": true}')]
        with patch.object(common.httpx, "post", side_effect=responses) as post, \
                patch.object(common.time, "sleep") as sl:
            ok, _ = common.post_event("http://127.0.0.1:1/event", "", {})
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)
        sl.assert_called_once_with(common.CONNECT_RETRY_SLEEP)

    def test_502_retried_only_once(self):
        responses = [httpx.Response(502, text="x"), httpx.Response(502, text="y"), httpx.Response(200)]
        with patch.object(common.httpx, "post", side_effect=responses) as post, \
                patch.object(common.time, "sleep"):
            ok, info = common.post_event("http://127.0.0.1:1/event", "", {})
        self.assertFalse(ok)
        self.assertTrue(info.startswith("502"))
        self.assertEqual(post.call_count, 2)

    def test_other_statuses_not_retried(self):
        # 202 «wake unavailable»: уведомление Ивану уже ушло, повтор дал бы второе.
        for status in (202, 403, 500):
            with patch.object(common.httpx, "post", return_value=httpx.Response(status)) as post, \
                    patch.object(common.time, "sleep") as sl:
                ok, _ = common.post_event("http://127.0.0.1:1/event", "", {})
            self.assertFalse(ok)
            self.assertEqual(post.call_count, 1)
            sl.assert_not_called()


    def test_503_retried_until_accepted(self):
        """Бот готовится к перезапуску по заявке и отвечает 503 до уведомления:
        повтор не даёт второго пробуждения (claude-tg-bot restart.py)."""
        responses = [httpx.Response(503, text="r"), httpx.Response(503, text="r"),
                     httpx.Response(200, text='{"ok": true}')]
        with patch.object(common.httpx, "post", side_effect=responses) as post, \
                patch.object(common.time, "sleep") as sl:
            ok, _ = common.post_event("http://127.0.0.1:1/event", "", {})
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [common.BUSY_RETRY_SLEEP] * 2)

    def test_still_wanted_stops_retries(self):
        """Срок события вышел между повторами 503: дальше не шлём."""
        wanted = iter([True, False])
        with patch.object(common.httpx, "post", return_value=httpx.Response(503, text="r")) as post, \
                patch.object(common.time, "sleep"):
            ok, info = common.post_event("http://127.0.0.1:1/event", "", {}, still_wanted=lambda: next(wanted))
        self.assertFalse(ok)
        self.assertIn("срок", info)
        self.assertEqual(post.call_count, 1)

    def test_503_gives_up_after_budget(self):
        with patch.object(common.httpx, "post", return_value=httpx.Response(503, text="r")) as post, \
                patch.object(common.time, "sleep"):
            ok, info = common.post_event("http://127.0.0.1:1/event", "", {})
        self.assertFalse(ok)
        self.assertIn("перезапуск", info)
        self.assertEqual(post.call_count, common.BUSY_RETRIES)


class WindowEndTests(unittest.TestCase):
    def test_window_end_timestamp(self):
        at = datetime(2026, 10, 23, 7, 45, tzinfo=wake.TZ)
        self.assertEqual(wake.window_end(at, "search"), datetime(2026, 10, 23, 8, 0, tzinfo=wake.TZ).timestamp())
        self.assertEqual(wake.window_end(at.replace(hour=15), "search_day"),
                         datetime(2026, 10, 23, 15, 30, tzinfo=wake.TZ).timestamp())


class WorkshopWakeTests(unittest.TestCase):
    """Мастерская (проект 22.10.2026): поиски и наряды идут в её тему."""

    def setUp(self):
        patcher = patch.object(wake, "refresh", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv, **env):
        with patch.dict(os.environ, _env(DOMOVOY_CHAT_ID="183", **env), clear=True), \
                patch.object(wake, "load_env"), patch.object(wake, "post_event", return_value=(True, "ok")) as post:
            code = wake.main(argv)
        return code, post

    def test_search_goes_to_workshop_fresh(self):
        code, post = self._run(["--search-day", "--force"], DOMOVOY_WORKSHOP_CHAT_ID="-100", DOMOVOY_WORKSHOP_THREAD_ID="77")
        payload = post.call_args.args[2]
        self.assertEqual((code, payload["chat_id"], payload["thread_id"], payload["fresh"]), (0, -100, 77, True))
        self.assertIn("workshop.md", payload["prompt"])

    def test_search_stays_in_private_without_workshop(self):
        code, post = self._run(["--search-day", "--force"])
        self.assertEqual(post.call_args.args[2]["chat_id"], 183)

    def test_job_needs_workshop_and_brief(self):
        with tempfile.TemporaryDirectory() as d:
            brief = Path(d) / "job.md"
            brief.write_text("Слова Ивана: посчитай отклики за неделю", encoding="utf-8")
            code, post = self._run(["--job", "--brief-file", str(brief)])
            self.assertEqual(code, 1)
            post.assert_not_called()
            code, post = self._run(["--job", "--brief-file", str(brief)],
                                   DOMOVOY_WORKSHOP_CHAT_ID="-100", DOMOVOY_WORKSHOP_THREAD_ID="77")
            payload = post.call_args.args[2]
            self.assertEqual(code, 0)
            self.assertEqual((payload["thread_id"], payload["fresh"], payload["notice"]), (77, True, False))
            self.assertIn("посчитай отклики", payload["prompt"])
            code, post = self._run(["--job", "--brief-file", str(Path(d) / "нет.md")],
                                   DOMOVOY_WORKSHOP_CHAT_ID="-100", DOMOVOY_WORKSHOP_THREAD_ID="77")
            self.assertEqual(code, 1)

    def test_half_configured_workshop_is_error(self):
        code, post = self._run(["--search-day", "--force"], DOMOVOY_WORKSHOP_CHAT_ID="-100")
        self.assertEqual(code, 1)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()

