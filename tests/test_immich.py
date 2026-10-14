"""Тесты чистой логики Immich-слоя."""
from __future__ import annotations

import base64
import sys
import tempfile
import unittest

import pytest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domovoy.immich.core import (  # noqa: E402
    MARK_CLOSE,
    MARK_OPEN,
    PREVIEW_MAX,
    asset_from_api,
    find_pending_days,
    render_photos_block,
    replace_machine_block,
    set_photo_status,
)


@pytest.fixture(autouse=True)
def _photos_state_in_tmp(tmp_path, monkeypatch):
    # Состояние тестов не касается живой машины (ревью 11.10.2026).
    from domovoy.immich import __main__ as m
    monkeypatch.setattr(m, "PHOTOS_STATE", tmp_path / "photos", raising=False)

DAY = """---
schema: diary-day/v1
type: day
date: 2026-08-01
фото: pending
---

## 12:00 <!-- src:tg:1:2:3 sha:x kind:text -->
человеческий текст
"""


def _asset(i="abc-123", name="IMG_1.jpg"):
    return asset_from_api({
        "id": i,
        "originalFileName": name,
        "checksum": base64.b64encode(b"\x01\x02\x03").decode(),
        "fileCreatedAt": "2026-08-01T16:42:15+04:00",
    })


class AssetTests(unittest.TestCase):
    def test_checksum_decoded_to_hex(self):
        self.assertEqual(_asset().sha1_hex, "010203")

    def test_bad_checksum_kept_as_is(self):
        a = asset_from_api({"id": "x", "originalFileName": "f", "checksum": "не base64!"})
        self.assertTrue(a.sha1_hex)


class RenderBlockTests(unittest.TestCase):
    def test_small_day_with_previews(self):
        a = _asset()
        block = render_photos_block(
            "https://photos.example.com", [a], {a.id: "_attachments/дневник/immich/x.jpg"}
        )
        self.assertIn(MARK_OPEN, block)
        # Есть превью: показываем картинку, и она же ссылка на снимок в
        # Immich (локальная картинка видна и без сервера).
        self.assertIn(
            "[![\\|150](_attachments/дневник/immich/x.jpg)](https://photos.example.com/photos/abc-123)", block
        )
        self.assertIn("1 кадр", block)
        self.assertIn(MARK_CLOSE, block)

    def test_without_preview_falls_back_to_link(self):
        a = _asset()
        block = render_photos_block("https://photos.example.com", [a])
        self.assertIn("https://photos.example.com/photos/abc-123", block)

    def test_big_day_is_compact(self):
        # Тело дня не должно тонуть в машинных строках: счётчик, до трёх
        # кадров-ключей, ссылка на реестр.
        assets = [
            asset_from_api({"id": f"id{n}", "originalFileName": f"IMG_{n}.jpg",
                            "checksum": "", "fileCreatedAt": f"2026-08-01T{n:02d}:00:00Z"})
            for n in range(15)
        ]
        block = render_photos_block("https://x", assets, {}, "Дневник/_реестр/д кадры")
        self.assertIn("15 кадр", block)
        self.assertIn("[[Дневник/_реестр/д кадры|все 15]]", block)
        # Без превью — ссылки, но не больше числа кадров-ключей.
        self.assertLessEqual(
            len([l for l in block.splitlines() if l.startswith("- ")]), PREVIEW_MAX
        )

    def test_key_frames_are_first_middle_last(self):
        from domovoy.immich.core import pick_key_frames

        assets = [
            asset_from_api({"id": str(n), "originalFileName": f"{n}.jpg",
                            "checksum": "", "fileCreatedAt": f"2026-08-01T{n:02d}:00:00Z"})
            for n in range(10)
        ]
        # n=3 — крайние и середина; дефолтный n=9 на десяти кадрах берёт почти все.
        self.assertEqual([a.id for a in pick_key_frames(assets, 3)], ["0", "4", "9"])
        self.assertEqual(len(pick_key_frames(assets)), PREVIEW_MAX)
        self.assertEqual(len(pick_key_frames(assets[:2])), 2)
        self.assertEqual(len(pick_key_frames([])), 0)


class ReplaceBlockTests(unittest.TestCase):
    def test_insert_at_end(self):
        block = f"{MARK_OPEN}\nтело\n{MARK_CLOSE}\n"
        out = replace_machine_block(DAY, block)
        self.assertIn("человеческий текст", out)
        self.assertTrue(out.rstrip().endswith(MARK_CLOSE))

    def test_replace_existing(self):
        first = replace_machine_block(DAY, f"{MARK_OPEN}\nстарое\n{MARK_CLOSE}\n")
        second = replace_machine_block(first, f"{MARK_OPEN}\nновое\n{MARK_CLOSE}\n")
        self.assertNotIn("старое", second)
        self.assertIn("новое", second)
        self.assertEqual(second.count(MARK_OPEN), 1)

    def test_human_blocks_untouched(self):
        out = replace_machine_block(DAY, f"{MARK_OPEN}\nx\n{MARK_CLOSE}\n")
        self.assertIn("## 12:00 <!-- src:tg:1:2:3 sha:x kind:text -->", out)


    def test_quoted_marker_makes_day_untouchable(self):
        """Маркер в тексте записи и настоящий блок ниже: регулярка вырезала бы рассказ."""
        from domovoy.immich.core import AmbiguousPhotosBlock

        day = DAY + f"Пишу про сам дневник: блок начинается с {MARK_OPEN} и кончается.\nВажный рассказ.\n\n" \
            f"{MARK_OPEN}\nстарое\n{MARK_CLOSE}\n"
        with self.assertRaises(AmbiguousPhotosBlock):
            replace_machine_block(day, f"{MARK_OPEN}\nновое\n{MARK_CLOSE}\n")

    def test_replace_keeps_text_after_block(self):
        day = replace_machine_block(DAY, f"{MARK_OPEN}\nстарое\n{MARK_CLOSE}\n") + "\nхвост\n"
        out = replace_machine_block(day, f"{MARK_OPEN}\nновое\n{MARK_CLOSE}\n")
        self.assertEqual(out, day.replace("старое", "новое"))


class PhotoStatusTests(unittest.TestCase):
    def test_pending_to_synced(self):
        self.assertIn("фото: synced", set_photo_status(DAY, "synced"))

    def test_pending_to_none(self):
        self.assertIn("фото: none", set_photo_status(DAY, "none"))

    def test_body_mention_not_touched(self):
        text = DAY + "\nфото: pending в тексте не считается\n"
        out = set_photo_status(text, "synced")
        # только первое (frontmatter) вхождение
        self.assertIn("фото: pending в тексте", out)

    def test_invalid_status_raises(self):
        with self.assertRaises(ValueError):
            set_photo_status(DAY, "готово")


class FindPendingTests(unittest.TestCase):
    def test_finds_only_pending_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "2026"
            d.mkdir()
            (d / "2026-08-01.md").write_text(DAY, encoding="utf-8")
            (d / "2026-08-02.md").write_text(
                DAY.replace("фото: pending", "фото: synced"), encoding="utf-8"
            )
            (d / "2026-08-03.md").write_text(DAY, encoding="utf-8")
            (d / "мусор.md").write_text(DAY, encoding="utf-8")
            out = find_pending_days(Path(tmp))
            self.assertEqual(out, [date(2026, 8, 1), date(2026, 8, 3)])

    def test_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "2026"
            d.mkdir()
            for i in range(1, 8):
                (d / f"2026-07-{i:02d}.md").write_text(DAY, encoding="utf-8")
            self.assertEqual(len(find_pending_days(Path(tmp), limit=5)), 5)



class LocalTimeTests(unittest.TestCase):
    def test_utc_converted_to_local(self):
        from zoneinfo import ZoneInfo

        from domovoy.immich.core import local_hhmm

        tz = ZoneInfo("Asia/Dubai")
        # Файл 20260801_154628.jpg снят в 15:46 местного = 11:46Z
        self.assertEqual(local_hhmm("2026-08-01T11:46:28.000Z", tz), "15:46")

    def test_naive_kept(self):
        from zoneinfo import ZoneInfo

        from domovoy.immich.core import local_hhmm

        self.assertEqual(
            local_hhmm("2026-08-01T15:46:28", ZoneInfo("Asia/Dubai")), "15:46"
        )

    def test_garbage_and_empty(self):
        from zoneinfo import ZoneInfo

        from domovoy.immich.core import local_hhmm

        tz = ZoneInfo("Asia/Dubai")
        self.assertEqual(local_hhmm("", tz), "")
        self.assertEqual(local_hhmm("не дата", tz), "")


class ChronologyTests(unittest.TestCase):
    def test_sorted_ascending(self):
        from domovoy.immich.core import sort_chronological

        a = asset_from_api({"id": "1", "originalFileName": "b.jpg",
                            "fileCreatedAt": "2026-08-01T14:00:00Z", "checksum": ""})
        b = asset_from_api({"id": "2", "originalFileName": "a.jpg",
                            "fileCreatedAt": "2026-08-01T11:00:00Z", "checksum": ""})
        self.assertEqual([x.id for x in sort_chronological([a, b])], ["2", "1"])

    def test_block_is_chronological(self):
        from domovoy.immich.core import render_photos_block

        late = asset_from_api({"id": "late", "originalFileName": "z.jpg",
                               "fileCreatedAt": "2026-08-01T18:00:00Z", "checksum": ""})
        early = asset_from_api({"id": "early", "originalFileName": "a.jpg",
                                "fileCreatedAt": "2026-08-01T09:00:00Z", "checksum": ""})
        block = render_photos_block("https://x", [late, early])
        self.assertLess(block.index("early"), block.index("late"))
class RecheckTests(unittest.TestCase):
    """Перепроверка закрытых дней: снимки догружаются в Immich с опозданием."""

    def _vault(self, tmp: str, days: dict[str, str]) -> Path:
        root = Path(tmp) / "Дневник"
        for iso, status in days.items():
            p = root / iso[:4] / f"{iso}.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"---\ntype: day\nфото: {status}\n---\nтекст\n", encoding="utf-8")
        return root

    def test_recent_closed_days_are_rechecked_oldest_first(self):
        from domovoy.immich.core import find_recheck_days

        with tempfile.TemporaryDirectory() as tmp:
            root = self._vault(tmp, {
                "2026-09-10": "none",      # старше окна
                "2026-09-28": "none",
                "2026-10-01": "synced",
                "2026-10-03": "pending",   # не закрыт, это забота find_pending_days
                "2026-10-04": "none",      # сегодня ещё не кончилось
            })
            days = find_recheck_days(root, date(2026, 10, 4), 14)
        self.assertEqual(days, [date(2026, 9, 28), date(2026, 10, 1)])

    def test_recorded_count(self):
        from domovoy.immich.core import recorded_count

        self.assertEqual(recorded_count(DAY), 0)
        block = render_photos_block("https://x", [_asset("a"), _asset("b")])
        self.assertEqual(recorded_count(replace_machine_block(DAY, block)), 2)


class FetchTests(unittest.TestCase):
    def test_all_pages_are_read(self):
        from domovoy.immich.__main__ import search_all

        class Resp:
            def __init__(self, items, nxt):
                self._d = {"assets": {"items": items, "nextPage": nxt}}

            def raise_for_status(self):
                pass

            def json(self):
                return self._d

        class Client:
            def __init__(self):
                self.pages = []

            def post(self, url, json):
                self.pages.append(json["page"])
                return {1: Resp([{"id": "a"}], "2"), 2: Resp([{"id": "b"}], "3"), 3: Resp([{"id": "c"}], None)}[json["page"]]

        client = Client()
        self.assertEqual([i["id"] for i in search_all(client, "https://x", "A", "B")], ["a", "b", "c"])
        self.assertEqual(client.pages, [1, 2, 3])

    def test_midnight_frame_belongs_to_one_day_only(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from domovoy.immich.__main__ import within

        tz = ZoneInfo("Asia/Dubai")
        start = datetime(2026, 10, 1, tzinfo=tz)
        end = start + timedelta(days=1)
        at = lambda iso: asset_from_api({"id": "x", "originalFileName": "f", "fileCreatedAt": iso})  # noqa: E731
        self.assertTrue(within(at("2026-09-30T20:00:00.000Z"), start, end))    # 00:00 по Тольятти
        self.assertFalse(within(at("2026-10-01T20:00:00.000Z"), start, end))   # полночь следующего дня
        self.assertTrue(within(at("2026-10-01T19:59:59.000Z"), start, end))
        self.assertTrue(within(at("не дата"), start, end))


class RebuildTests(unittest.TestCase):
    """Перестройка закрытого дня: сбой сети не должен стоить уже сохранённых превью."""

    def setUp(self):
        import os
        from datetime import datetime, timedelta
        from unittest import mock
        from zoneinfo import ZoneInfo

        from domovoy.immich import __main__ as m

        self.m = m
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name)
        self.day = datetime.now(ZoneInfo("Asia/Dubai")).date() - timedelta(days=2)
        iso = self.day.isoformat()
        self.day_path = self.vault / "Дневник" / iso[:4] / f"{iso}.md"
        self.day_path.parent.mkdir(parents=True)
        self.previews = self.vault / "_attachments" / "дневник" / "immich"
        self.previews.mkdir(parents=True)
        old = {}
        for name in ("old11111", "old22222"):
            (self.previews / f"{iso}_{name}.jpg").write_bytes(b"jpg")
            old[name] = f"_attachments/дневник/immich/{iso}_{name}.jpg"
        block = render_photos_block("https://x", [_asset("old11111-x"), _asset("old22222-x")], {
            "old11111-x": old["old11111"], "old22222-x": old["old22222"]})
        self.day_path.write_text(f"---\ntype: day\nфото: synced\n---\nтекст\n\n{block}", encoding="utf-8")
        self.before = self.day_path.read_text(encoding="utf-8")
        self.assets = [asset_from_api({"id": f"new{i}0000-x", "originalFileName": f"I{i}.jpg",
                                       "fileCreatedAt": f"{iso}T0{i}:00:00.000Z"}) for i in range(5)]

        class Client:
            def __init__(self, **kw):
                pass

            def get(self, url):
                class R:
                    def raise_for_status(self):
                        pass
                return R()

            def close(self):
                pass

        for patcher in (
            mock.patch.dict(os.environ, {"IMMICH_API_KEY": "k", "IMMICH_URL": "https://photos.example.com", "DOMOVOY_VAULT_DIR": str(self.vault)}),
            mock.patch.object(m, "load_env"),
            mock.patch.object(m.httpx, "Client", Client),
            mock.patch.object(m, "fetch_assets", return_value=self.assets),
            mock.patch.object(m.diary, "commit", return_value="нечего коммитить"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_failed_downloads_leave_day_and_old_previews_untouched(self):
        from unittest import mock

        with mock.patch.object(self.m, "download_preview", return_value=False):
            self.assertEqual(self.m.main([]), 0)
        # Блок и превью целы; день возвращён в незакрытые, чтобы достроиться
        # позже, даже если к тому времени выйдет из окна перепроверки.
        self.assertEqual(self.day_path.read_text(encoding="utf-8"), self.before.replace("фото: synced", "фото: pending"))
        self.assertEqual(len(list(self.previews.iterdir())), 2)

    def test_postponed_day_is_finished_by_a_later_run(self):
        from unittest import mock

        def fake_download(client, base, asset, dest):
            dest.write_bytes(b"jpg")
            return True

        with mock.patch.object(self.m, "download_preview", return_value=False):
            self.m.main([])
        with mock.patch.object(self.m, "download_preview", side_effect=fake_download):
            self.m.main([])
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("фото: synced", text)
        self.assertIn("📷 7 кадр(ов) в Immich", text)

    def test_postponed_day_keeps_block_when_server_later_returns_fewer(self):
        """Перестройку отложили, а потом сервер отдал меньше кадров, чем записано."""
        from unittest import mock

        with mock.patch.object(self.m, "download_preview", return_value=False):
            self.m.main([])                                  # день ушёл в pending с прежним блоком
        pending_text = self.day_path.read_text(encoding="utf-8")
        # При меньшем или равном числе новые id всё равно требуют загрузки
        # (ревью 11.10.2026).
        with mock.patch.object(self.m, "fetch_assets", return_value=self.assets[:1]), \
                mock.patch.object(self.m, "download_preview", return_value=False):
            self.m.main([])
        self.assertEqual(self.day_path.read_text(encoding="utf-8"), pending_text)
        self.assertEqual(len(list(self.previews.iterdir())), 2)
        with mock.patch.object(self.m, "fetch_assets", return_value=[_asset("old11111-x"), _asset("old22222-x")]):
            self.m.main([])
        self.assertEqual(self.day_path.read_text(encoding="utf-8"), self.before)

    def test_postponed_rebuild_waits_for_full_set_at_any_age(self):
        """Отложенная перестройка не закрывается недостроенной, даже выйдя из окна."""
        import os
        from datetime import timedelta
        from unittest import mock

        old_day = self.day - timedelta(days=30)
        iso = old_day.isoformat()
        path = self.vault / "Дневник" / iso[:4] / f"{iso}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        block = render_photos_block("https://x", [_asset("old11111-x")], {})
        path.write_text(f"---\ntype: day\nфото: pending\n---\nтекст\n\n{block}", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        os.remove(self.day_path)
        with mock.patch.object(self.m, "download_preview", return_value=False):
            self.m.main([])
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_commit_runs_even_when_server_is_down(self):
        from unittest import mock

        class Down:
            def __init__(self, **kw):
                pass

            def get(self, url):
                raise OSError("нет связи")

            def close(self):
                pass

        with mock.patch.object(self.m.httpx, "Client", Down):
            self.assertEqual(self.m.main([]), 0)
        self.m.diary.commit.assert_called_once()
        self.m.diary.commit.reset_mock()
        self.assertEqual(self.m.main(["--dry-run"]), 0)
        self.m.diary.commit.assert_not_called()

    def test_complete_rebuild_adds_to_block_and_keeps_old_previews(self):
        from unittest import mock

        def fake_download(client, base, asset, dest):
            dest.write_bytes(b"jpg")
            return True

        with mock.patch.object(self.m, "download_preview", side_effect=fake_download) as dl:
            self.assertEqual(self.m.main([]), 0)
            self.assertEqual(dl.call_count, 5)
            # Второй прогон: кадров столько же, качать и писать нечего.
            self.assertEqual(self.m.main([]), 0)
            self.assertEqual(dl.call_count, 5)
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("📷 7 кадр(ов) в Immich", text)
        self.assertIn("текст\n", text)
        names = sorted(f.name for f in self.previews.iterdir())
        self.assertEqual(len(names), 7)
        self.assertTrue(any("old" in n for n in names))

    # Проверки находок ниже воспроизводят прежнее поведение (ревью 11.10.2026).
    def download_ok(self, client, base, asset, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpg")
        return True

    def make_old_pending(self):
        from datetime import timedelta
        self.day_path.unlink()
        day = self.day - timedelta(days=30)
        path = self.day_path.parent / f"{day}.md"
        path.write_text(DAY, encoding="utf-8")
        return day, path

    def test_review_xd1_union_when_server_replaces_frames(self):
        from unittest import mock
        for new_count in (1, 2):
            with self.subTest(new_count=new_count):
                self.day_path.write_text(self.before, encoding="utf-8")
                (self.m.PHOTOS_STATE / f"{self.day}.json").unlink(missing_ok=True)
                server = [_asset("old22222-x")] + self.assets[:new_count]
                with mock.patch.object(self.m, "fetch_assets", return_value=server), \
                        mock.patch.object(self.m, "download_preview", side_effect=self.download_ok):
                    self.m.main([])
                    self.m.main([])
                text = self.day_path.read_text(encoding="utf-8")
                self.assertIn("/photos/old11111-x", text)
                self.assertIn("/photos/new00000-x", text)
                self.assertIn(f"📷 {2 + new_count} кадр", text)
                self.assertTrue((self.previews / f"{self.day}_old11111.jpg").exists())

    def test_review_xd1_full_ids_include_frames_outside_preview_selection(self):
        from unittest import mock
        from domovoy.immich.core import render_registry_note
        old = [_asset(f"frame{i:03d}-x") for i in range(12)]
        reg = self.vault / "Дневник" / "_реестр" / f"{self.day} кадры.md"
        reg.parent.mkdir()
        reg.write_text(render_registry_note(self.day, "https://x", old), encoding="utf-8")
        self.day_path.write_text(replace_machine_block(DAY, render_photos_block("https://x", old)), encoding="utf-8")
        server = old[1:] + [self.assets[0]]
        with mock.patch.object(self.m, "fetch_assets", return_value=server), \
                mock.patch.object(self.m, "download_preview", side_effect=self.download_ok) as dl:
            self.m.main([])
            calls = dl.call_count
            self.m.main([])
        # Сетка уже показывает девять кадров: новый идёт в счётчик и реестр,
        # без десятой миниатюры (ревью 11.10.2026).
        self.assertEqual(calls, 0)
        self.assertEqual(dl.call_count, 0)
        self.assertIn("📷 13 кадр", self.day_path.read_text(encoding="utf-8"))
        self.assertIn("/photos/frame000-x", reg.read_text(encoding="utf-8"))
        self.assertIn("/photos/new00000-x", reg.read_text(encoding="utf-8"))
        self.assertEqual(len(self.m.read_state(self.day)["assets"]), 13)

    def test_review_c19_timeout_keeps_old_day_pending_without_failure(self):
        from unittest import mock
        day, path = self.make_old_pending()
        before = path.read_text(encoding="utf-8")
        clock = [0]
        def download(*args):
            clock[0] = self.m.DEADLINE_SEC + 1
            return False
        with mock.patch.object(self.m.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(self.m, "download_preview", side_effect=download):
            self.m.main([])
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.m.read_state(day).get("failures", 0), 0)
        with mock.patch.object(self.m, "download_preview", side_effect=self.download_ok):
            self.m.main([])
        self.assertIn("фото: synced", path.read_text(encoding="utf-8"))

    def test_review_c19_budget_ends_in_last_download_without_counting_failure(self):
        from unittest import mock
        day, path = self.make_old_pending()
        clock = [0]
        def download(*args):
            clock[0] = self.m.DEADLINE_SEC + 1
            return False
        with mock.patch.object(self.m, "fetch_assets", return_value=self.assets[:1]), \
                mock.patch.object(self.m.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(self.m, "download_preview", side_effect=download):
            self.m.main([])
        self.assertIn("фото: pending", path.read_text(encoding="utf-8"))
        self.assertEqual(self.m.read_state(day).get("failures", 0), 0)

    def test_review_xd4_old_day_preview_failure_is_retried(self):
        from unittest import mock
        day, path = self.make_old_pending()
        with mock.patch.object(self.m, "download_preview", return_value=False):
            self.m.main([])
        self.assertNotIn(MARK_OPEN, path.read_text(encoding="utf-8"))
        self.assertIn("фото: pending", path.read_text(encoding="utf-8"))
        with mock.patch.object(self.m, "download_preview", side_effect=self.download_ok) as dl:
            self.m.main([])
        self.assertGreater(dl.call_count, 0)
        self.assertIn("фото: synced", path.read_text(encoding="utf-8"))

    def test_review_c21_failed_preview_closes_after_three_attempts(self):
        from unittest import mock
        with mock.patch.object(self.m, "download_preview", return_value=False):
            for _ in range(3):
                self.m.main([])
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("фото: synced", text)
        self.assertIn("/photos/new00000-x", text)
        self.assertIn(f"{self.day}_old11111.jpg", text)
        self.assertTrue((self.previews / f"{self.day}_old11111.jpg").exists())

    def test_review_c21_queue_does_not_starve_new_pending_day(self):
        from datetime import timedelta
        from unittest import mock
        self.day_path.unlink()
        paths = []
        for n in range(31):
            d = self.day - timedelta(days=60 - n)
            p = self.day_path.parent / f"{d}.md"
            block = render_photos_block("https://x", [_asset("old11111-x"), _asset("old22222-x")])
            p.write_text(DAY + (block if n < 30 else ""), encoding="utf-8")
            paths.append(p)
        current = [None]
        def fetch(client, base, day, tz):
            current[0] = day
            return self.assets
        def download(*args):
            return self.download_ok(*args) if str(current[0]) == paths[-1].stem else False
        with mock.patch.object(self.m, "fetch_assets", side_effect=fetch), \
                mock.patch.object(self.m, "download_preview", side_effect=download):
            self.m.main([])
            self.m.main([])
        self.assertIn("фото: synced", paths[-1].read_text(encoding="utf-8"))

    def test_review_c21_ambiguous_days_also_yield_queue(self):
        from datetime import timedelta
        from unittest import mock
        self.day_path.unlink()
        for n in range(30):
            d = self.day - timedelta(days=60 - n)
            p = self.day_path.parent / f"{d}.md"
            p.write_text(DAY + f"Цитата {MARK_OPEN}.\n", encoding="utf-8")
        self.day_path.write_text(DAY, encoding="utf-8")
        with mock.patch.object(self.m, "download_preview", side_effect=self.download_ok):
            self.m.main([])
            self.m.main([])
        self.assertIn("фото: synced", self.day_path.read_text(encoding="utf-8"))

    def test_owner_yesterday_goes_first_in_long_pending_queue(self):
        """При завале незакрытых дней вчерашний обрабатывается первым же
        прогоном, даже когда очередь длиннее предела за прогон, а остальные
        идут от старых к новым (решение владельца 12.10.2026)."""
        from datetime import timedelta
        from unittest import mock
        self.day_path.unlink()
        yesterday = self.day + timedelta(days=1)
        old = []
        for n in range(self.m.PENDING_PER_RUN + 10):
            d = self.day - timedelta(days=80 - n)
            (self.day_path.parent / f"{d}.md").write_text(DAY, encoding="utf-8")
            old.append(d)
        y_path = self.day_path.parent / f"{yesterday}.md"
        y_path.write_text(DAY, encoding="utf-8")
        asked = []

        def fetch(client, base, day, tz):
            asked.append(day)
            return []

        with mock.patch.object(self.m, "fetch_assets", side_effect=fetch):
            self.m.main([])
        self.assertEqual(asked[0], yesterday)
        self.assertEqual(asked[1:], old[:self.m.PENDING_PER_RUN - 1])
        self.assertIn("фото: none", y_path.read_text(encoding="utf-8"))

    def test_review_c20_window_starts_at_first_close_and_is_not_refreshed(self):
        from datetime import timedelta
        from unittest import mock
        from domovoy.immich.core import find_recheck_days
        day, path = self.make_old_pending()
        with mock.patch.object(self.m, "fetch_assets", return_value=[]):
            self.m.main([])
        with mock.patch.object(self.m, "download_preview", side_effect=self.download_ok) as dl:
            self.m.main([])
        self.assertGreater(dl.call_count, 0)
        self.assertIn("фото: synced", path.read_text(encoding="utf-8"))
        closed = self.m.read_state(day)["closed_on"]
        self.assertEqual(closed, str(self.day + timedelta(days=2)))
        with mock.patch.object(self.m, "fetch_assets", return_value=self.assets):
            self.m.main([])
        self.assertEqual(self.m.read_state(day)["closed_on"], closed)
        first = date.fromisoformat(closed)
        self.assertIn(day, find_recheck_days(path.parent.parent, first + timedelta(days=14), 14, {str(day): closed}))
        self.assertNotIn(day, find_recheck_days(path.parent.parent, first + timedelta(days=15), 14, {str(day): closed}))
        self.assertNotIn("closed_on", path.read_text(encoding="utf-8"))

    def test_review_xd3_day_failure_after_registry_publish_is_recoverable(self):
        from unittest import mock
        from domovoy.immich.core import render_registry_note
        old = [_asset(f"frame{i:03d}-x") for i in range(12)]
        reg = self.vault / "Дневник" / "_реестр" / f"{self.day} кадры.md"
        reg.parent.mkdir()
        reg.write_text(render_registry_note(self.day, "https://x", old), encoding="utf-8")
        self.day_path.write_text(replace_machine_block(DAY, render_photos_block("https://x", old)), encoding="utf-8")
        before = self.day_path.read_text(encoding="utf-8")
        real = self.m.os.replace
        def fail_day(src, dest):
            if dest == self.day_path:
                raise OSError("сбой записи дня")
            return real(src, dest)
        server = old[1:] + self.assets[:2]
        with mock.patch.object(self.m, "fetch_assets", return_value=server), \
                mock.patch.object(self.m, "download_preview", side_effect=self.download_ok):
            with mock.patch.object(self.m.os, "replace", side_effect=fail_day):
                self.m.main([])
            self.assertEqual(self.day_path.read_text(encoding="utf-8"), before)
            self.m.main([])
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("/photos/new00000-x", reg.read_text(encoding="utf-8"))
        self.assertIn("/photos/frame000-x", text)
        self.assertIn("📷 14 кадр", text)

    def test_review_xd3_registry_fsync_failure_preserves_old_file_under_lock(self):
        import errno
        import fcntl
        from unittest import mock
        from domovoy.immich.core import render_registry_note
        reg = self.vault / "Дневник" / "_реестр" / f"{self.day} кадры.md"
        reg.parent.mkdir()
        old_assets = [_asset(f"old{i:05d}-x") for i in range(12)]
        old = render_registry_note(self.day, "https://x", old_assets)
        reg.write_text(old, encoding="utf-8")
        self.day_path.write_text(replace_machine_block(DAY, render_photos_block("https://x", old_assets)), encoding="utf-8")
        self.before = self.day_path.read_text(encoding="utf-8")
        real = self.m.diary._rewrite_locked
        checked = []
        def fail_registry(path, transform, skeleton, retries, *rest):
            if path == reg:
                with open(self.m.diary.LOCK_FILE, "a") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                checked.append(True)
                with mock.patch.object(self.m.os, "fsync", side_effect=OSError(errno.EIO, "EIO")):
                    return real(path, transform, skeleton, retries, *rest)
            return real(path, transform, skeleton, retries, *rest)
        with mock.patch.object(self.m.diary, "_rewrite_locked", side_effect=fail_registry), \
                mock.patch.object(self.m, "download_preview", side_effect=self.download_ok):
            self.m.main([])
        self.assertEqual(checked, [True])
        self.assertEqual(reg.read_text(encoding="utf-8"), old)
        self.assertEqual(self.day_path.read_text(encoding="utf-8"), self.before)
        self.assertEqual(list(reg.parent.glob("*.tmp")), [])
        self.m.diary.commit.assert_called_once()


    # Проверки оставшихся сбоев сохранения и подсчёта (ревью 11.10.2026).
    def test_review_close_requires_saved_date_and_late_photos_are_retried(self):
        from unittest import mock

        day, path = self.make_old_pending()
        before = path.read_text(encoding="utf-8")
        with mock.patch.object(self.m, "fetch_assets", return_value=[]), \
                mock.patch.object(self.m, "atomic_state", side_effect=OSError("сбой состояния")):
            self.m.main([])
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertNotIn("closed_on", self.m.read_state(day))
        with mock.patch.object(self.m, "fetch_assets", return_value=self.assets[:1]) as fetch, \
                mock.patch.object(self.m, "download_preview", side_effect=self.download_ok):
            self.m.main([])
        fetch.assert_called_once()
        text = path.read_text(encoding="utf-8")
        self.assertIn("фото: synced", text)
        self.assertIn("/photos/new00000-x", text)
        self.assertIn("человеческий текст\n", text)
        self.assertIn("closed_on", self.m.read_state(day))

    def test_review_first_block_failure_recovers_registry_without_deleted_frame(self):
        from unittest import mock

        self.day_path.write_text(DAY, encoding="utf-8")
        frames = self.frames(12)
        reg = self.vault / "Дневник" / "_реестр" / f"{self.day} кадры.md"
        real_replace = self.m.os.replace

        def fail_day(src, dest):
            if dest == self.day_path:
                raise OSError("сбой первого блока")
            return real_replace(src, dest)

        with mock.patch.object(self.m, "download_preview", side_effect=self.download_ok):
            with mock.patch.object(self.m, "fetch_assets", return_value=frames), \
                    mock.patch.object(self.m.os, "replace", side_effect=fail_day):
                self.m.main([])
            self.assertEqual(self.day_path.read_text(encoding="utf-8"), DAY)
            self.assertIn(f"/photos/{frames[0].id}", reg.read_text(encoding="utf-8"))
            with mock.patch.object(self.m, "fetch_assets", return_value=frames[1:]):
                self.m.main([])
                self.m.main([])
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("📷 12 кадр", text)
        self.assertIn("человеческий текст\n", text)
        self.assertIn(f"/photos/{frames[0].id}", reg.read_text(encoding="utf-8"))
        self.assertEqual(len(self.m.read_state(self.day)["assets"]), 12)

    def test_review_legacy_deleted_frame_counts_with_equal_server_total(self):
        from unittest import mock

        old = self.frames(3, "a")
        cells = []
        for a in old:
            rel = f"_attachments/дневник/immich/{self.day}_{a.id[:8]}.jpg"
            (self.vault / rel).write_bytes(b"jpg")
            cells.append(f"![[{rel}\\|150]]")
        block = (f"{MARK_OPEN}\n📷 3 кадр(ов) в Immich\n\n|   |   |   |\n|---|---|---|\n| "
                 + " | ".join(cells) + f" |\n{MARK_CLOSE}\n")
        self.day_path.write_text(replace_machine_block(DAY, block), encoding="utf-8")
        server = old[1:] + self.frames(1, "d")
        with mock.patch.object(self.m, "fetch_assets", return_value=server), \
                mock.patch.object(self.m, "download_preview", side_effect=self.download_ok) as dl:
            self.m.main([])
            self.m.main([])
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("📷 4 кадр", text)
        self.assertIn("человеческий текст\n", text)
        self.assertIn("/photos/d0000000-x", text)
        for cell in cells:
            self.assertIn(cell, text)
        self.assertEqual(text.count(".jpg"), 4)
        self.assertEqual(dl.call_count, 1)

    def test_review_last_attempt_failure_does_not_prevent_saved_close(self):
        from unittest import mock

        day, path = self.make_old_pending()
        real = self.m.atomic_state

        def fail_attempt(state_path, state):
            if "closed_on" not in state:
                raise OSError("сбой последней попытки")
            return real(state_path, state)

        with mock.patch.object(self.m, "fetch_assets", return_value=[]), \
                mock.patch.object(self.m, "atomic_state", side_effect=fail_attempt):
            self.m.main([])
        self.assertIn("фото: none", path.read_text(encoding="utf-8"))
        self.assertIn("closed_on", self.m.read_state(day))

    def test_review_dry_run_does_not_write_days_registry_previews_or_state(self):
        from unittest import mock
        from domovoy.immich.core import render_registry_note

        day, path = self.make_old_pending()
        reg = self.vault / "Дневник" / "_реестр" / f"{day} кадры.md"
        reg.parent.mkdir()
        reg.write_text(render_registry_note(day, "https://x", self.frames(12)), encoding="utf-8")

        def snapshot():
            roots = (self.vault, self.m.PHOTOS_STATE.parent)
            return {p: (p.read_bytes(), p.stat().st_mtime_ns)
                    for root in roots for p in root.rglob("*") if p.is_file()}

        before = snapshot()
        with mock.patch.object(self.m, "fetch_assets", return_value=self.frames(12)), \
                mock.patch.object(self.m, "download_preview") as download, \
                mock.patch.object(self.m, "save_state") as save, \
                mock.patch.object(self.m.diary, "rewrite") as rewrite:
            self.m.main(["--dry-run"])
            download.assert_not_called()
            save.assert_not_called()
            rewrite.assert_not_called()
        self.assertEqual(snapshot(), before)
        self.m.diary.commit.assert_not_called()

    # Доработка по второму кругу ревью (ревью 11.10.2026).
    def frames(self, n, prefix="n"):
        iso = self.day.isoformat()
        return [asset_from_api({"id": f"{prefix}{i:07d}-x", "originalFileName": f"{prefix}{i}.jpg",
                                "fileCreatedAt": f"{iso}T{i // 4:02d}:{(i % 4) * 10:02d}:00.000Z"})
                for i in range(n)]

    def test_review_removed_block_is_rebuilt_from_known_frames(self):
        """Блок пропал из дня (убран руками, день пересоздан скелетом), а
        состояние помнит кадры: блок строится целиком, не «0 кадров»."""
        import re
        from unittest import mock

        with mock.patch.object(self.m, "download_preview", side_effect=self.download_ok):
            self.m.main([])
            self.assertIn("📷 7 кадр", self.day_path.read_text(encoding="utf-8"))
            self.day_path.write_text("---\ntype: day\nфото: pending\n---\nтекст\n", encoding="utf-8")
            self.m.main([])
            self.m.main([])
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("фото: synced", text)
        self.assertIn("📷 7 кадр", text)
        self.assertEqual(len(re.findall(r"\[!\[", text)), 7)
        self.assertIn("/photos/old11111-x", text)

    def test_review_block_does_not_grow_past_grid(self):
        """Догрузки порциями: в блоке одна сетка не больше PREVIEW_MAX
        миниатюр, остальное в счётчике и реестре."""
        import re
        from unittest import mock

        frames = self.frames(36)
        reg = self.vault / "Дневник" / "_реестр" / f"{self.day} кадры.md"
        with mock.patch.object(self.m, "download_preview", side_effect=self.download_ok) as dl:
            for upto in (12, 24, 36):
                with mock.patch.object(self.m, "fetch_assets", return_value=frames[:upto]):
                    self.m.main([])
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("📷 38 кадр", text)
        self.assertEqual(len(re.findall(r"\[!\[", text)), PREVIEW_MAX)
        self.assertEqual(text.count("|---|---|---|"), 1)
        self.assertEqual(dl.call_count, PREVIEW_MAX - 2)
        self.assertIn("/photos/old11111-x", text)
        rows = re.findall(r"^\| [^|]* \| \[", reg.read_text(encoding="utf-8"), re.M)
        self.assertEqual(len(rows), 38)

    def test_review_recheck_is_bounded_and_rotates(self):
        """После долгого простоя Immich закрытых в окне дней сотни: за прогон
        перепроверяется не больше RECHECK_PER_RUN, незакрытый день не ждёт,
        и за несколько прогонов спрошены все."""
        import json
        from datetime import timedelta
        from unittest import mock

        per_run = getattr(self.m, "RECHECK_PER_RUN", 14)
        self.day_path.write_text(DAY, encoding="utf-8")       # незакрытый день
        state_dir = self.m.PHOTOS_STATE
        state_dir.mkdir(parents=True, exist_ok=True)
        closed = []
        for n in range(40):
            d = self.day - timedelta(days=60 - n)
            (self.day_path.parent / f"{d}.md").write_text(DAY.replace("pending", "none"), encoding="utf-8")
            (state_dir / f"{d}.json").write_text(
                json.dumps({"closed_on": str(self.day), "assets": []}), encoding="utf-8")
            closed.append(d)
        asked = []

        def fetch(client, base, day, tz):
            asked.append(day)
            return []

        seen = set()
        with mock.patch.object(self.m, "fetch_assets", side_effect=fetch):
            for _ in range(3):
                asked.clear()
                self.m.main([])
                self.assertLessEqual(len(asked), per_run + 1)
                seen.update(asked)
                if self.day in asked:
                    self.assertIn("фото: none", self.day_path.read_text(encoding="utf-8"))
        self.assertTrue(set(closed) <= seen)

    def test_review_legacy_registry_ahead_and_old_cells_are_not_double_counted(self):
        """Унаследованные данные без состояния: реестр опередил день, ячейки
        прежнего вида без ссылки. Счётчик по настоящему числу кадров, миниатюры
        не дублируются."""
        import re
        from unittest import mock
        from domovoy.immich.core import pick_key_frames, render_registry_note

        frames = self.frames(15)
        iso = self.day.isoformat()
        previews = {}
        for a in pick_key_frames(frames[:12]):
            rel = f"_attachments/дневник/immich/{iso}_{a.id[:8]}.jpg"
            (self.vault / rel).write_bytes(b"jpg")
            previews[a.id] = rel
        block = render_photos_block("https://x", frames[:12], previews, f"Дневник/_реестр/{iso} кадры")
        self.day_path.write_text(replace_machine_block(DAY.replace("pending", "synced"), block), encoding="utf-8")
        reg = self.vault / "Дневник" / "_реестр" / f"{iso} кадры.md"
        reg.parent.mkdir()
        reg.write_text(render_registry_note(self.day, "https://x", frames), encoding="utf-8")
        with mock.patch.object(self.m, "fetch_assets", return_value=frames), \
                mock.patch.object(self.m, "download_preview", side_effect=self.download_ok) as dl:
            self.m.main([])
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("📷 15 кадр", text)
        self.assertEqual(len(re.findall(r"\[!\[", text)), 9)
        self.assertEqual(dl.call_count, 0)

        old = self.frames(3, "g")
        cells = []
        for a in old:
            rel = f"_attachments/дневник/immich/{iso}_{a.id[:8]}.jpg"
            (self.vault / rel).write_bytes(b"jpg")
            cells.append(f"![[{rel}\\|150]]")
        old_block = (f"{MARK_OPEN}\n📷 3 кадр(ов) в Immich\n\n|   |   |   |\n|---|---|---|\n| "
                     + " | ".join(cells) + f" |\n{MARK_CLOSE}\n")
        self.day_path.write_text(replace_machine_block(DAY.replace("pending", "synced"), old_block), encoding="utf-8")
        reg.unlink()
        new = self.frames(1, "z")
        with mock.patch.object(self.m, "fetch_assets", return_value=old + new), \
                mock.patch.object(self.m, "download_preview", side_effect=self.download_ok):
            (self.m.PHOTOS_STATE / f"{iso}.json").unlink(missing_ok=True)
            self.m.main([])
        text = self.day_path.read_text(encoding="utf-8")
        self.assertIn("📷 4 кадр", text)
        self.assertEqual(text.count(".jpg"), 4)
        self.assertIn("/photos/z0000000-x", text)


class DayWriteSafetyTests(unittest.TestCase):
    """Утренний прогон: запись дня через diary.rewrite, сбой одного дня не обрывает остальные."""

    def setUp(self):
        import os
        from datetime import datetime, timedelta
        from unittest import mock
        from zoneinfo import ZoneInfo

        from domovoy.immich import __main__ as m

        self.m = m
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name)
        today = datetime.now(ZoneInfo("Asia/Dubai")).date()
        self.days = [today - timedelta(days=3), today - timedelta(days=2)]
        self.paths = []
        for d in self.days:
            iso = d.isoformat()
            path = self.vault / "Дневник" / iso[:4] / f"{iso}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"---\ntype: day\nфото: pending\n---\nрассказ {iso}\n", encoding="utf-8")
            self.paths.append(path)

        class Client:
            def __init__(self, **kw):
                pass

            def get(self, url):
                class R:
                    def raise_for_status(self):
                        pass
                return R()

            def close(self):
                pass

        def fake_download(client, base, asset, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"jpg")
            return True

        assets = [_asset("aaaa1111-x", "I1.jpg"), _asset("bbbb2222-x", "I2.jpg")]
        for patcher in (
            mock.patch.dict(os.environ, {"IMMICH_API_KEY": "k", "IMMICH_URL": "https://photos.example.com", "DOMOVOY_VAULT_DIR": str(self.vault)}),
            mock.patch.object(m, "load_env"),
            mock.patch.object(m.httpx, "Client", Client),
            mock.patch.object(m, "fetch_assets", return_value=assets),
            mock.patch.object(m, "download_preview", side_effect=fake_download),
            mock.patch.object(m.diary, "commit", return_value="нечего коммитить"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self):
        import io
        from unittest import mock

        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            rc = self.m.main([])
        return rc, err.getvalue()

    def test_quoted_marker_day_is_left_and_next_day_written(self):
        first = self.paths[0]
        quoted = first.read_text(encoding="utf-8") + f"Цитата {MARK_OPEN} в рассказе.\nВажное.\n\n{MARK_OPEN}\n📷 1 кадр(ов) в Immich\n{MARK_CLOSE}\n"
        first.write_text(quoted, encoding="utf-8")
        rc, err = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(first.read_text(encoding="utf-8"), quoted)
        self.assertIn("неоднозначен", err)
        self.assertIn("фото: synced", self.paths[1].read_text(encoding="utf-8"))

    def test_error_on_one_day_does_not_stop_the_rest(self):
        real = self.m.diary.rewrite

        def flaky(path, transform, **kw):
            if path == self.paths[0]:
                raise OSError(5, "Input/output error")
            return real(path, transform, **kw)

        from unittest import mock

        before = self.paths[0].read_text(encoding="utf-8")
        with mock.patch.object(self.m.diary, "rewrite", side_effect=flaky):
            rc, err = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("не записан", err)
        self.assertEqual(self.paths[0].read_text(encoding="utf-8"), before)
        self.assertIn("📷 2 кадр(ов) в Immich", self.paths[1].read_text(encoding="utf-8"))

    def test_bad_fsync_does_not_replace_day(self):
        """Ошибка fsync, кроме «не умею», не кончается подменой дня (как в diary).
        Сбой подставляется только на время записи дня: запись состояния при
        этом проходит, и проверка действительно доходит до diary.rewrite
        (ревью 11.10.2026)."""
        import errno
        from unittest import mock

        real_fsync = self.m.diary.os.fsync
        real_rewrite = self.m.diary.rewrite
        inside, calls = [False], []

        def fsync(fd):
            if inside[0]:
                raise OSError(errno.EIO, "EIO")
            return real_fsync(fd)

        def rewrite(path, transform, **kw):
            calls.append(path)
            inside[0] = True
            try:
                return real_rewrite(path, transform, **kw)
            finally:
                inside[0] = False

        before = [p.read_text(encoding="utf-8") for p in self.paths]
        with mock.patch.object(self.m.diary.os, "fsync", side_effect=fsync), \
                mock.patch.object(self.m.diary, "rewrite", side_effect=rewrite):
            rc, err = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, self.paths)
        self.assertIn("не записан", err)
        self.assertEqual([p.read_text(encoding="utf-8") for p in self.paths], before)
        self.assertEqual(sorted(f.name for f in self.paths[0].parent.iterdir()), sorted(p.name for p in self.paths))

    def test_state_write_failure_does_not_stop_the_day(self):
        """Состояние не пишется (каталог недоступен): день всё равно
        получает блок, но остаётся pending для перепроверок (ревью 11.10.2026)."""
        from unittest import mock

        with mock.patch.object(self.m, "atomic_state", side_effect=OSError(13, "Permission denied")):
            rc, err = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("состояние не записано", err)
        for p in self.paths:
            text = p.read_text(encoding="utf-8")
            self.assertIn("📷 2 кадр(ов) в Immich", text)
            self.assertIn("фото: pending", text)
            self.assertNotIn("closed_on", self.m.read_state(date.fromisoformat(p.stem)))

    def test_broken_utf8_day_is_skipped_not_fatal(self):
        self.paths[0].write_bytes("---\nфото: pending\n---\n".encode() + b"\xff\xfe\n")
        rc, err = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("не UTF-8", err)
        self.assertIn("фото: synced", self.paths[1].read_text(encoding="utf-8"))


class RelinkTests(unittest.TestCase):
    """Превью прежнего вида становятся ссылками, остальное в дне не меняется."""

    FULL = "3ca02d8c-f8b5-4335-a6f3-52035bff19de"
    OLD = (
        "---\ntype: day\nфото: synced\n---\n\n###### 22:34\n<!-- src:tg:1:2:3 sha:x kind:text -->\n"
        "Рассказ, в котором упомянут файл ![[_attachments/дневник/immich/2026-10-03_3ca02d8c.jpg\\|150]].\n\n"
        "<!-- machine:photos v1 -->\n📷 3 кадр(ов) в Immich · [[Дневник/_реестр/2026-10-03 кадры|все 3]]\n\n"
        # Таблицу уже переформатировал редактор Obsidian: ячейки с отбивкой.
        "|                                                               |     |\n"
        "| ------------------------------------------------------------- | --- |\n"
        "| ![[_attachments/дневник/immich/2026-10-03_3ca02d8c.jpg\\|150]] | ![[_attachments/дневник/immich/2026-10-03_deadbeef.jpg\\|150]] |\n"
        "<!-- /machine:photos -->\n"
    )

    def test_known_frames_become_links_and_the_rest_is_untouched(self):
        from domovoy.immich.core import old_cells, recorded_count, relink_block

        self.assertEqual(old_cells(self.OLD), ["3ca02d8c", "deadbeef"])
        new = relink_block(self.OLD, {"3ca02d8c": self.FULL}, "https://photos.example.com")
        self.assertIn(
            f"| [![\\|150](_attachments/дневник/immich/2026-10-03_3ca02d8c.jpg)](https://photos.example.com/photos/{self.FULL}) |", new
        )
        # Кадр удалён из Immich: превью остаётся вставкой без ссылки.
        self.assertIn("| ![[_attachments/дневник/immich/2026-10-03_deadbeef.jpg\\|150]] |", new)
        # Текст записи выше блока не тронут, хотя вставка в нём того же вида.
        head = self.OLD[: self.OLD.index("<!-- machine:photos")]
        self.assertTrue(new.startswith(head))
        self.assertEqual(recorded_count(new), 3)
        self.assertEqual(old_cells(new), ["deadbeef"])
        self.assertEqual(relink_block(new, {"3ca02d8c": self.FULL}, "https://photos.example.com"), new)

    def test_day_without_block_or_ids_is_unchanged(self):
        from domovoy.immich.core import relink_block

        self.assertEqual(relink_block(DAY, {"3ca02d8c": self.FULL}, "https://x"), DAY)
        self.assertEqual(relink_block(self.OLD, {}, "https://x"), self.OLD)

    def test_marker_quoted_in_human_text_makes_the_note_untouchable(self):
        """Маркер, процитированный в записи выше блока: править по регулярке нельзя."""
        from domovoy.immich.core import old_cells, photos_block_span, relink_block

        quoted = self.OLD.replace("Рассказ, в котором", "Блок начинается с `<!-- machine:photos v1 -->`. Рассказ, в котором")
        self.assertIsNone(photos_block_span(quoted))
        self.assertEqual(old_cells(quoted), [])
        self.assertEqual(relink_block(quoted, {"3ca02d8c": self.FULL}, "https://photos.example.com"), quoted)

    def test_two_blocks_are_skipped_not_half_converted(self):
        from domovoy.immich.core import photos_block_span, relink_block

        second = self.OLD[self.OLD.index("<!-- machine:photos v1 -->"):]
        doubled = self.OLD + "\n" + second
        self.assertIsNone(photos_block_span(doubled))
        self.assertEqual(relink_block(doubled, {"3ca02d8c": self.FULL}, "https://photos.example.com"), doubled)

    def test_shared_prefix_gives_no_link_rather_than_a_wrong_one(self):
        from domovoy.immich.core import relink_block, unique_prefixes

        other = "3ca02d8c-0000-4000-8000-000000000009"
        ids = unique_prefixes([self.FULL, other, "deadbeef-0000-4000-8000-000000000001"])
        self.assertEqual(ids, {"deadbeef": "deadbeef-0000-4000-8000-000000000001"})
        new = relink_block(self.OLD, ids, "https://photos.example.com")
        self.assertIn("| ![[_attachments/дневник/immich/2026-10-03_3ca02d8c.jpg\\|150]] |", new)
        self.assertIn("(https://photos.example.com/photos/deadbeef-0000-4000-8000-000000000001)", new)

    def test_new_blocks_are_already_links(self):
        from domovoy.immich.core import old_cells

        a = _asset("3ca02d8c-f8b5-4335-a6f3-52035bff19de")
        block = render_photos_block("https://photos.example.com", [a], {a.id: "_attachments/дневник/immich/2026-10-03_3ca02d8c.jpg"})
        self.assertEqual(old_cells(replace_machine_block(DAY, block)), [])


class RelinkRunTests(unittest.TestCase):
    """Разовая переделка всех дней: ссылку получает только кадр, подтверждённый сервером."""

    A = "aaaaaaaa-0000-4000-8000-000000000001"
    B = "bbbbbbbb-0000-4000-8000-000000000002"

    def setUp(self):
        import os
        from unittest import mock

        from domovoy.immich import __main__ as m

        self.m = m
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name)
        days = self.vault / "Дневник" / "2026"
        days.mkdir(parents=True)

        def day(iso, prefix):
            p = days / f"{iso}.md"
            p.write_text(
                f"---\ntype: day\nфото: synced\n---\nтекст\n\n{MARK_OPEN}\n📷 1 кадр(ов) в Immich\n\n"
                f"| ![[_attachments/дневник/immich/{iso}_{prefix}.jpg\\|150]] |\n{MARK_CLOSE}\n", encoding="utf-8")
            return p

        self.alive = day("2026-09-01", "aaaaaaaa")
        self.deleted = day("2026-09-02", "bbbbbbbb")
        # В реестре остался и удалённый с сервера кадр: ссылку по нему давать нельзя.
        reg = self.vault / "Дневник" / "_реестр"
        reg.mkdir()
        (reg / "2026-09-02 кадры.md").write_text(f"| 10:00 | [I.jpg](https://photos.example.com/photos/{self.B}) | `x` |\n", encoding="utf-8")
        for patcher in (
            mock.patch.dict(os.environ, {"IMMICH_API_KEY": "k", "IMMICH_URL": "https://photos.example.com", "DOMOVOY_VAULT_DIR": str(self.vault)}),
            mock.patch.object(m, "load_env"),
            mock.patch.object(m.diary, "commit", return_value="закоммичено"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, up: bool):
        class Client:
            def __init__(self, **kw):
                pass

            def get(self, url):
                if not up:
                    raise OSError("нет связи")

                class R:
                    def raise_for_status(self):
                        pass
                return R()

            def close(self):
                pass
        return Client

    def on_server(self, client, base, day, tz):
        return [asset_from_api({"id": self.A, "originalFileName": "I.jpg"})] if day.day == 1 else []

    def test_confirmed_frame_becomes_a_link_and_deleted_one_stays_plain(self):
        from unittest import mock

        deleted_before = self.deleted.read_text(encoding="utf-8")
        with mock.patch.object(self.m.httpx, "Client", self.client(up=True)), \
                mock.patch.object(self.m, "fetch_assets", side_effect=self.on_server):
            self.assertEqual(self.m.main(["--relink"]), 0)
            self.assertEqual(self.m.main(["--relink"]), 0)      # повтор ничего не ломает
        text = self.alive.read_text(encoding="utf-8")
        self.assertIn(f"[![\\|150](_attachments/дневник/immich/2026-09-01_aaaaaaaa.jpg)](https://photos.example.com/photos/{self.A})", text)
        self.assertIn("📷 1 кадр(ов) в Immich", text)
        self.assertTrue(text.startswith("---\ntype: day\nфото: synced\n---\nтекст\n"))
        self.assertEqual(self.deleted.read_text(encoding="utf-8"), deleted_before)

    def test_server_down_changes_nothing(self):
        from unittest import mock

        before = self.alive.read_text(encoding="utf-8")
        with mock.patch.object(self.m.httpx, "Client", self.client(up=False)):
            self.assertEqual(self.m.main(["--relink"]), 0)
        self.assertEqual(self.alive.read_text(encoding="utf-8"), before)

    def test_dry_run_changes_nothing(self):
        from unittest import mock

        before = self.alive.read_text(encoding="utf-8")
        with mock.patch.object(self.m.httpx, "Client", self.client(up=True)), \
                mock.patch.object(self.m, "fetch_assets", side_effect=self.on_server):
            self.assertEqual(self.m.main(["--relink", "--dry-run"]), 0)
        self.assertEqual(self.alive.read_text(encoding="utf-8"), before)
        self.m.diary.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
