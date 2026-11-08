"""Журнал месяца (ревизия повторов 07.11.2026, шаг 3): разделы дня по дате,
запись в последний раздел дня, один модуль на всех писателей и читателей."""
import re
from datetime import date, datetime
from pathlib import Path

from domovoy import data, journal
from domovoy.common import TZ

DAY = date(2026, 11, 3)
# Как 03.11.2026: сторож и утро поставили по заголовку дня.
TWO = ("## 2026-11-02 (пн)\n\nВечер\n\n## 2026-11-03 (вт)\n\nСторож 08:32: Просмотр\n\n"
       "## 2026-11-03 (вт)\n\nПлан: 1) зарядка\nСебе: старое\n")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "journal").mkdir(parents=True)
    return repo


def test_append_to_day_writes_into_last_section(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "journal" / "2026-11.md"
    path.write_text(TWO, encoding="utf-8")
    assert journal.append_to_day(DAY, ["Сверка прогноза: 2 из 3"], root=repo, journal_dir=repo / "journal")
    assert path.read_text(encoding="utf-8") == TWO + "Сверка прогноза: 2 из 3\n"


def test_append_to_day_does_not_repeat_line_from_any_section(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "journal" / "2026-11.md"
    path.write_text(TWO, encoding="utf-8")
    assert not journal.append_to_day(DAY, ["Сторож 08:32: Просмотр", "Себе: старое"], root=repo,
                                     journal_dir=repo / "journal")
    assert path.read_text(encoding="utf-8") == TWO


def test_day_edit_two_empty_headings_widens_anchor():
    """Два пустых заголовка одного дня: якорь расширяется, запись находит его один раз."""
    text = "## 2026-11-03 (вт)\n\n## 2026-11-03 (вт)\n"
    block = journal.day_edit(text, DAY, ["Себе: а"])
    out = data.apply_edit(text, data.parse_blocks(block))
    assert out == "## 2026-11-03 (вт)\n\n## 2026-11-03 (вт)\nСебе: а\n"


def test_day_edit_last_section_before_next_day():
    text = "## 2026-11-03 (вт)\nУтро\n\n## 2026-11-03 (вт)\nВечер\n\n## 2026-11-04 (ср)\nСторож 00:10: x\n"
    block = journal.day_edit(text, DAY, ["Себе: а"])
    out = data.apply_edit(text, data.parse_blocks(block))
    assert out == ("## 2026-11-03 (вт)\nУтро\n\n## 2026-11-03 (вт)\nВечер\nСебе: а\n\n"
                   "## 2026-11-04 (ср)\nСторож 00:10: x\n")


def test_day_edit_marker_with_two_equal_headings():
    """«=======» в последнем разделе и два одинаковых заголовка: якорь
    заголовок плюс строки до него, запись находит его один раз (ревью 08.11.2026)."""
    text = "## 2026-11-03 (вт)\nУтро\n\n## 2026-11-03 (вт)\n=======\nВечер\n"
    block = journal.day_edit(text, DAY, ["Себе: а"])
    out = data.apply_edit(text, data.parse_blocks(block))
    assert out == "## 2026-11-03 (вт)\nУтро\n\n## 2026-11-03 (вт)\nСебе: а\n=======\nВечер\n"


def test_day_sections_only_split_on_newline():
    text = "Строка\x0c## 2026-11-03 (вт)\n## 2026-11-03 (вт)\nУтро\n"
    block = journal.day_edit(text, DAY, ["Себе: а"])
    out = data.apply_edit(text, data.parse_blocks(block))
    assert out == text + "Себе: а\n"


def test_line_does_not_add_heading_when_weekday_differs(tmp_path):
    """Заголовок агента с другим днём недели (опечатка) тот же день: второго
    заголовка сторож не ставит."""
    path = tmp_path / "2026-11.md"
    path.write_text("## 2026-11-03 (вторник)\n\nПлан\n", encoding="utf-8")
    journal.append_or_raise("Сторож", ["Почта: A"], datetime(2026, 11, 3, 9, 10, tzinfo=TZ), tmp_path)
    assert path.read_text(encoding="utf-8") == "## 2026-11-03 (вторник)\n\nПлан\nСторож 09:10: Почта: A\n"


def test_line_repeat_checked_in_all_sections_of_day(tmp_path):
    path = tmp_path / "2026-11.md"
    path.write_text("## 2026-11-03 (вт)\n\nСторож 09:10: Почта: A\n\n## 2026-11-03 (вт)\n\nПлан\n",
                    encoding="utf-8")
    journal.append_or_raise("Сторож", ["Почта: A"], datetime(2026, 11, 3, 9, 10, tzinfo=TZ), tmp_path)
    assert path.read_text(encoding="utf-8").count("Почта: A") == 1


def test_days_merges_sections_of_one_day(tmp_path):
    (tmp_path / "2026-11.md").write_text(TWO, encoding="utf-8")
    got = journal.days(DAY, DAY, tmp_path)
    assert got == {DAY: ["Сторож 08:32: Просмотр", "План: 1) зарядка", "Себе: старое"]}


def test_month_file_and_heading():
    assert journal.month_file(DAY, Path("/j")) == Path("/j/2026-11.md")
    assert journal.heading(DAY) == "## 2026-11-03 (вт)"
    assert journal.heading_day("## 2026-11-03 (пт)") == DAY
    assert journal.heading_day("## 2026-13-03") is None
    assert journal.heading_day("## Заметки") is None


def test_no_own_month_file_or_day_heading_outside_journal():
    """Тест-преграда: файл месяца и разделы дня журнала только через
    domovoy.journal. `startswith("## ")` вне журнала разрешён только там, где
    режут не журнал (промт, BACKLOG, ответ разбора)."""
    pkg = Path(journal.__file__).parent
    allowed_heads = {"promptparts.py": 1, "review.py": 2, "selfcheck.py": 1}
    month = re.compile(r"%Y-%m\}\.md|%Y-%m['\"]\)\s*(\}|\+)\s*['\"]?\.md|\}-\{\w+:02d\}\.md|month:02d")
    own_day = re.compile(r"## \(\\d\{4\}|\^## |\[:3\] == ['\"]## |startswith\(['\"]##['\"]\)")
    bad = []
    files = sorted(pkg.rglob("*.py")) + sorted((pkg.parent / "tools").glob("*.py"))
    for path in files:
        if path.name == "journal.py":
            continue
        text = path.read_text(encoding="utf-8")
        if month.search(text):
            bad.append(f"{path.name}: свой файл месяца, звать journal.month_file")
        if own_day.search(text):
            bad.append(f"{path.name}: свой разбор заголовка дня, звать journal.DAY_RE и journal.heading_day")
        heads = text.count('startswith("## ")') + text.count("startswith('## ')")
        if heads > allowed_heads.get(path.name, 0):
            bad.append(f"{path.name}: свой startswith(\"## \"), разделы дня звать из journal")
    assert not bad, bad
