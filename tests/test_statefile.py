"""domovoy.statefile и его вызывающие (ревизия повторов 07.11.2026, шаг 2):
битый UTF-8 и битый JSON не роняют чтение и откладываются рядом, следующая
запись их не теряет, обрыв записи оставляет прежний файл целым."""
from __future__ import annotations

import ast
import errno
import importlib
import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from domovoy import statefile
from domovoy.common import TZ

ROOT = Path(__file__).resolve().parents[1]
BAD_UTF8 = b'{"a": "\xd0'          # оборван посреди русской буквы
BAD_JSON = b'{"a": 1, "b": '


def _private(name: str):
    """Модуль, которого нет в открытой копии: тест там пропускается."""
    try:
        return importlib.import_module(f"domovoy.{name}")
    except ImportError:
        pytest.skip(f"domovoy.{name} нет в этой копии")


def _corrupt(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


def _no_tmp(folder: Path) -> None:
    assert [p.name for p in folder.iterdir() if p.name.endswith(".tmp")] == []


# --- сам модуль -------------------------------------------------------------

def test_write_atomic_mode_and_text(tmp_path):
    p = tmp_path / "s.json"
    assert statefile.write_atomic(p, "привет")
    assert p.read_text(encoding="utf-8") == "привет"
    assert p.stat().st_mode & 0o777 == 0o600
    _no_tmp(tmp_path)


def test_write_atomic_mode_none_keeps_old_mode(tmp_path):
    p = tmp_path / "data.md"
    p.write_text("старое", encoding="utf-8")
    os.chmod(p, 0o644)
    statefile.write_atomic(p, "новое", mode=None)
    assert p.stat().st_mode & 0o777 == 0o644
    fresh = tmp_path / "fresh.md"
    statefile.write_atomic(fresh, "x", mode=None)
    umask = os.umask(0)
    os.umask(umask)
    assert fresh.stat().st_mode & 0o777 == 0o666 & ~umask


@pytest.mark.parametrize("exc", [OSError("диск"), KeyboardInterrupt()])
def test_write_atomic_interrupted_keeps_old_and_cleans(tmp_path, monkeypatch, exc):
    p = tmp_path / "s.json"
    p.write_text("целое", encoding="utf-8")

    def broken(src, dst):
        raise exc

    monkeypatch.setattr(statefile.os, "replace", broken)
    with pytest.raises(type(exc)):
        statefile.write_atomic(p, "новое")
    assert p.read_text(encoding="utf-8") == "целое"
    _no_tmp(tmp_path)


def test_write_atomic_fsync_strict_and_lenient(tmp_path, monkeypatch):
    p = tmp_path / "day.md"
    p.write_text("целое", encoding="utf-8")
    monkeypatch.setattr(statefile.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(errno.EINVAL, "drvfs")))
    with pytest.raises(OSError):
        statefile.write_atomic(p, "новое")
    assert p.read_text(encoding="utf-8") == "целое"
    assert statefile.write_atomic(p, "новое", fsync="lenient")
    assert p.read_text(encoding="utf-8") == "новое"
    monkeypatch.setattr(statefile.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "диск")))
    with pytest.raises(OSError):
        statefile.write_atomic(p, "третье", fsync="lenient")
    assert p.read_text(encoding="utf-8") == "новое"
    _no_tmp(tmp_path)


def test_write_atomic_check_false_leaves_file(tmp_path):
    p = tmp_path / "day.md"
    p.write_text("с телефона", encoding="utf-8")
    assert not statefile.write_atomic(p, "наше", check=lambda: False)
    assert p.read_text(encoding="utf-8") == "с телефона"
    _no_tmp(tmp_path)


@pytest.mark.parametrize("raw", [BAD_UTF8, BAD_JSON, b"[1, 2]"])
def test_read_json_sets_bad_aside(tmp_path, capsys, raw):
    p = tmp_path / "state.json"
    p.write_bytes(raw)
    assert statefile.read_json(p, {"x": 1}) == {"x": 1}
    assert not p.exists()
    [aside] = _corrupt(p)
    assert aside.read_bytes() == raw
    assert "битый" in capsys.readouterr().err
    statefile.write_json(p, {"y": 2})
    assert statefile.read_json(p, {}) == {"y": 2}
    assert _corrupt(p) == [aside]


def test_read_json_missing_and_empty_are_quiet(tmp_path, capsys):
    default = {"list": []}
    got = statefile.read_json(tmp_path / "nope.json", default)
    got["list"].append(1)
    assert default == {"list": []}        # копия, а не тот же объект
    (tmp_path / "empty.json").write_text("  \n", encoding="utf-8")
    assert statefile.read_json(tmp_path / "empty.json", {}) == {}
    assert (tmp_path / "empty.json").exists()
    assert capsys.readouterr().err == ""


def test_read_json_unreadable_is_not_set_aside(tmp_path, capsys):
    p = tmp_path / "dir.json"
    p.mkdir()                             # IsADirectoryError: это OSError
    assert statefile.read_json(p, {}) == {}
    assert p.exists() and not _corrupt(p)
    assert "не читается" in capsys.readouterr().err


def test_read_json_strict_raises_and_keeps(tmp_path):
    p = tmp_path / "state.json"
    p.write_bytes(BAD_UTF8)
    with pytest.raises(statefile.StateError):
        statefile.read_json(p, {}, strict=True)
    assert p.read_bytes() == BAD_UTF8 and not _corrupt(p)
    assert statefile.read_json(tmp_path / "nope.json", None, strict=True) is None


def test_corrupt_copies_are_capped(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    stamps = iter(range(10))

    class Clock:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 11, 8, 12, 0, next(stamps), tzinfo=tz)

    monkeypatch.setattr(statefile, "datetime", Clock)
    for _ in range(statefile.CORRUPT_KEEP + 2):
        p.write_bytes(BAD_JSON)
        statefile.read_json(p, {})
    assert len(_corrupt(p)) == statefile.CORRUPT_KEEP


def test_jsonl_append_and_iter_skip_broken(tmp_path):
    p = tmp_path / "log.jsonl"
    statefile.append_jsonl(p, {"n": 1})
    with p.open("ab") as f:
        f.write(b'{"n": "\xd0\n{"n": 2\n\n')
    statefile.append_jsonl(p, {"n": "три"})
    assert p.stat().st_mode & 0o777 == 0o600
    assert [r.get("n") for r in statefile.iter_jsonl(p)] == [1, "три"]
    assert list(statefile.iter_jsonl(tmp_path / "nope.jsonl")) == []


def test_flock_wait_and_timeout(tmp_path):
    lock = tmp_path / "sub" / "x.lock"
    with statefile.flock(lock):
        with pytest.raises(TimeoutError):
            with statefile.flock(lock, 0):
                pass
        with pytest.raises(TimeoutError):
            with statefile.flock(lock, 0.2, poll=0.05):
                pass
    with statefile.flock(lock, 0):
        pass


# --- файлы состояния вызывающих ----------------------------------------------
# Каждый случай: (путь, прочитать -> значение, записать нечто, ожидаемое после
# чтения битого). Битый файл отложен, следующая запись ложится рядом с ним.

def _case_watch_state(tmp, mp):
    watch = _private("watch")
    mp.setattr(watch, "STATE_DIR", tmp)
    mp.setattr(watch, "STATE_FILE", tmp / "state.json")
    return (tmp / "state.json", lambda: watch.read_state()["sent_keys"],
            lambda: watch.write_state({**watch.read_state(), "sent_keys": ["k"]}), [])


def _case_watch_notices(tmp, mp):
    watch = _private("watch")
    mp.setattr(watch, "STATE_DIR", tmp)
    mp.setattr(watch, "send_notices", lambda *a, **kw: True)
    return (tmp / "notices.json", watch._read_notices,
            lambda: watch.notice_once("k", "v", "текст", 1, None, False), {})


def _case_watch_srochno(tmp, mp):
    watch = _private("watch")
    mp.setattr(watch, "STATE_DIR", tmp)
    mp.setattr(watch, "send_notices", lambda *a, **kw: True)
    src = tmp / "srochno.md"
    src.write_text("- 2026-11-08 09:00 | ответить\n", encoding="utf-8")
    now = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)
    return (tmp / "srochno.json", lambda: statefile.read_json(tmp / "srochno.json", {}),
            lambda: watch.srochno_notice(now, 1, None, False, src), {})


def _case_wakefail(tmp, mp):
    from domovoy import wakefail
    mp.setattr(wakefail, "FAILED_FILE", tmp / "failed.json")
    return (tmp / "failed.json", wakefail.wake_failures,
            lambda: wakefail.note_wake_failure("morning", "Утро", "нет", datetime(2026, 11, 8, tzinfo=TZ)), {})


def _case_interview(tmp, mp):
    interview = _private("interview")
    now = datetime(2026, 11, 8, tzinfo=TZ)
    p = tmp / "interview.json"
    return (p, lambda: interview._load_done(p, now),
            lambda: interview._save_done(p, {"k": now.isoformat()}), {})


def _case_reflect(tmp, mp):
    reflect = _private("reflect")
    p = tmp / "reflect.json"
    return p, lambda: reflect._load(p), lambda: reflect._save(p, {"day": "2026-11-08"}), {}


def _case_immich(tmp, mp):
    from domovoy.immich import __main__ as m
    mp.setattr(m, "PHOTOS_STATE", tmp)
    mp.setattr(m.diary, "LOCK_FILE", tmp / "diary.lock")
    day = date(2026, 11, 8)
    return (tmp / f"{day}.json", lambda: m.read_state(day),
            lambda: m.save_state(day, {"closed_on": "2026-11-08"}), {})


def _case_sync_stop(tmp, mp):
    from domovoy import sync
    mp.setattr(sync, "STATE_DIR", tmp)
    return (tmp / "stop.json", lambda: sync.saved_stop(0),
            lambda: sync.save_stop(sync.Stop(sync.BLOCKED, "x")), sync.Stop())


def _case_sync_failures(tmp, mp):
    from domovoy import sync
    mp.setattr(sync, "STATE_DIR", tmp)
    return (tmp / "failures.json", lambda: statefile.read_json(tmp / "failures.json", {}),
            lambda: sync.count_failure(sync.ERROR, "сеть", False), {})


def _case_sync_notify(tmp, mp):
    from domovoy import sync
    mp.setattr(sync, "STATE_DIR", tmp)
    mp.setattr(sync, "read_target", lambda: (1, None))
    mp.setattr(sync, "post_event", lambda *a, **kw: (True, "ok"))
    return (tmp / "dirty.json", lambda: statefile.read_json(tmp / "dirty.json", {}),
            lambda: sync._notify_once("dirty.json", "ключ", "заголовок", "текст"), {})


CASES = [_case_watch_state, _case_watch_notices, _case_watch_srochno, _case_wakefail, _case_interview,
         _case_reflect, _case_immich, _case_sync_stop, _case_sync_failures, _case_sync_notify]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__[6:])
@pytest.mark.parametrize("raw", [BAD_UTF8, BAD_JSON], ids=["utf8", "json"])
def test_bad_state_file_is_set_aside(tmp_path, monkeypatch, case, raw):
    path, read, write, empty = case(tmp_path, monkeypatch)
    path.write_bytes(raw)
    assert read() == empty
    [aside] = _corrupt(path)
    assert aside.read_bytes() == raw
    write()
    json.loads(path.read_text(encoding="utf-8"))       # новая запись цела
    assert _corrupt(path) == [aside]                     # и отложенное не потеряно
    _no_tmp(path.parent)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__[6:])
def test_interrupted_state_write_keeps_old(tmp_path, monkeypatch, case):
    path, read, write, _empty = case(tmp_path, monkeypatch)
    old = '{"старое": 1}'
    path.write_text(old, encoding="utf-8")

    tried = []

    def broken(src, dst):
        tried.append(dst)
        raise OSError("диск")

    monkeypatch.setattr(statefile.os, "replace", broken)
    try:
        write()
    except OSError:
        pass
    monkeypatch.undo()
    assert tried, "запись не дошла до подмены: тест прошёл бы вхолостую"
    assert path.read_text(encoding="utf-8") == old
    _no_tmp(path.parent)


def test_avia_bad_state_is_loud_and_kept(tmp_path):
    avia = _private("avia")
    conf = {"origin": "TBS", "destination": "MOW", "adults": 1, "children": 1}
    p = tmp_path / "state.json"
    for raw in (BAD_UTF8, BAD_JSON, b"[]", b"", b"  \n"):
        p.write_bytes(raw)
        with pytest.raises(avia.AviaError):
            avia.load_state(conf, p)
        assert p.read_bytes() == raw and not _corrupt(p)
    p.unlink()
    assert avia.load_state(conf, p)["days"] == {}


def test_woken_jsonl_survives_broken_utf8(tmp_path, monkeypatch):
    watch = _private("watch")
    listen = _private("listen")
    now = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)
    path = watch.LISTEN_DIR / "woken.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"at": "2026-11-08T07:59:00+00:00", "keys": ["a"]}\n{"at": "\xd0\n')
    assert watch.woken_keys(now) == {"a"}
    listen.record_woken(["b"], now.timestamp())
    assert watch.woken_keys(now) == {"a", "b"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_weekstats_records_skip_torn_utf8(tmp_path):
    weekstats = _private("weekstats")
    p = tmp_path / "s.jsonl"
    p.write_bytes(b'{"type": "user"}\n{"type": "\xd0\x9f')     # живая сессия, обрыв посреди буквы
    assert [r["type"] for r in weekstats._records(p)] == ["user"]


def test_journal_queue_survives_broken_utf8(tmp_path):
    from domovoy import journal, sync
    when = datetime(2026, 11, 8, 10, 0, tzinfo=TZ)
    entry = {"path": str(journal.month_file(when.date())), "at": when.isoformat(), "lines": ["Почта: A"]}
    journal.QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    journal.QUEUE_FILE.write_bytes(b'{"path": "\xd0\n' + (json.dumps(entry, ensure_ascii=False) + "\n").encode())
    with sync.data_lock() as locked:
        assert locked
        journal.flush_queue(when.date())
    assert "Почта: A" in journal.month_file(when.date()).read_text(encoding="utf-8")
    assert not journal.QUEUE_FILE.exists()


# --- тест-преграда ------------------------------------------------------------

def _raw_writes(source: str) -> list[int]:
    """Строки с подменой файла или записью JSON поверх мимо statefile:
    os.replace/os.rename, Path.replace(x)/Path.rename(x) с одним аргументом
    (у str.replace их два), .write_text(json.dumps(...)), json.dump(...),
    os.fsync. Временный файл для подпроцесса (tempfile.mkstemp) не в счёт."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
        if owner == "os" and name in ("replace", "rename", "fsync"):
            hits.append(node.lineno)
        elif owner == "json" and name == "dump":
            hits.append(node.lineno)
        elif owner != "os" and name in ("replace", "rename") and len(node.args) == 1 and not node.keywords:
            hits.append(node.lineno)
        elif name == "write_text" and node.args and isinstance(node.args[0], ast.Call) \
                and isinstance(node.args[0].func, ast.Attribute) and node.args[0].func.attr == "dumps":
            hits.append(node.lineno)
    return hits


def test_barrier_catches_raw_writes():
    assert _raw_writes("import os\nos.replace(a, b)\n")
    assert _raw_writes("tmp.replace(path)\n")
    assert _raw_writes("p.write_text(json.dumps(x), encoding='utf-8')\n")
    assert _raw_writes("json.dump(state, f)\n")
    assert not _raw_writes("s.replace('a', 'b')\nd.replace(tzinfo=tz)\np.write_text(text)\n")


def test_no_raw_state_writes_outside_statefile():
    """Подмена файла и запись JSON поверх только через domovoy.statefile
    (ревизия повторов 07.11.2026): десять копий расходились fsync, правами и
    уборкой временного. Дневник держит свою сверку с телефоном, но пишет тоже
    через statefile (check=)."""
    allowed = {ROOT / "domovoy" / "statefile.py"}
    found = []
    for path in sorted((ROOT / "domovoy").rglob("*.py")):
        if path in allowed:
            continue
        for line in _raw_writes(path.read_text(encoding="utf-8")):
            found.append(f"{path.relative_to(ROOT)}:{line}")
    assert found == [], "запись файла мимо domovoy.statefile"


def test_watch_state_unreadable_is_loud(tmp_path, monkeypatch):
    """Нечитаемое (не битое) состояние сторожа роняет прогон: пустое затёрло бы
    ключи «уже слал» и очередь недоставленного."""
    watch = _private("watch")
    monkeypatch.setattr(watch, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "state.json").mkdir()
    with pytest.raises(OSError):
        watch.read_state()


@pytest.mark.parametrize("raw", [BAD_UTF8, BAD_JSON], ids=["utf8", "json"])
def test_listen_batch_and_health_set_aside(tmp_path, monkeypatch, raw):
    import types
    watch = _private("watch")
    listen = _private("listen")
    stub = types.SimpleNamespace(dry_run=False, now=lambda: 1_000_000.0)
    for name, load, empty in (("batch.json", listen.Listener._load_batch, ([], set())),
                              ("health.json", listen.Listener._load_health, None),
                              ("watch-alert.json", None, None)):
        path = watch.LISTEN_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        if load is None:
            assert statefile.read_json(path, {}).get("pulse") is None
        elif empty is not None:
            assert load(stub) == empty
        else:
            assert set(load(stub)) == set(listen.SOURCES)
        assert _corrupt(path), name


def test_review_run_lock_is_single(tmp_path):
    review = _private("review")
    first = review._run_lock(tmp_path)
    assert first is not None
    assert review._run_lock(tmp_path) is None
    first.close()
    again = review._run_lock(tmp_path)
    assert again is not None
    again.close()
