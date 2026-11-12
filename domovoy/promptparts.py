"""Части prompt.md и предел текстов правил.

Ядро (всё до заголовка «# Разделы по случаю») сессия читает целиком одним
Read, раздел по случаю тоже одним Read. Пределы это стена, а не сигнал: у
предела правка убирает лишнее или не вносится, подъём предела решает только
Иван своими словами, сам он в меню не предлагается. Почему стена: за шесть
недель до 12.11.2026 файл вырос с 32 до 241 КБ, части заполнялись до байта, а
рост уходил в соседние тексты; прежний потолок 130 КБ сняли через девять дней.
Поэтому предел стоит и на сумме всех текстов, которые ран читает как указания
(`rules_total`). Размеры в байтах UTF-8.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

CORE_END = "\n# Разделы по случаю"
CORE_MAX_BYTES = 18 * 1024        # ядро читается в каждом ране
SECTION_MAX_BYTES = 8 * 1024
FILE_MAX_BYTES = 45 * 1024        # prompt.md целиком
LINE_MAX_CHARS = 1500
POPRAVKI_MAX_BYTES = 3 * 1024     # data/popravki.md
RULES_MAX_BYTES = 118 * 1024      # сумма rules_total (107 КБ на 12.11.2026)
GROWTH_SIGNAL = 0.10              # рост части за 30 дней, о котором говорит разбор
TABLE_HEAD = "| Раздел по случаю |"
_DATED_DECISION = re.compile(r"решени[еяю] Ивана \d\d\.\d\d")
_DASHES = re.compile("[\u2013\u2014]")
_ROW = re.compile(r"[^ |\n]+ \| ")              # строка данных «ключ | …» без ведущей черты
_KEY = re.compile(r"[^\s|#`-][^:|`\n]{0,30}: \S+\s*$")   # строка данных «ключ: значение»

# Тексты правил кроме prompt.md: целые файлы, затем шапки data/*.md.
RULE_FILES = ("data/popravki.md", "workshop.md", "diary.md", "domovoy/watch_rules.md")
# Тексты событий для модели: функции, чьи строки уходят в событие, или (None)
# все строки в _() модуля.
EVENT_FUNCS = {"domovoy/wake.py": ("build_prompt", "workshop_note", "app_note", "sync_warning"),
               "domovoy/watch.py": ("build_agent_prompt",),
               "domovoy/listen.py": ("bare_prompt",),
               "domovoy/interview.py": ("payload",),
               "domovoy/wakenotes.py": None}
_WATCH_CONSTS = ("LOUD_RULE", "NO_ACTION")


@dataclass
class Layout:
    core: int                                   # байт
    sections: dict[str, int] = field(default_factory=dict)
    table: list[str] = field(default_factory=list)
    total: int = 0                              # весь файл, байт
    text: str = ""


def section_name(heading: str) -> str:
    """«## Утро и вечер (было в …)» -> «Утро и вечер»."""
    return heading[3:].split(" (", 1)[0].strip()


def split(text: str) -> Layout | None:
    """Ядро и разделы; None, если в файле нет раскладки."""
    i = text.find(CORE_END)
    if i < 0:
        return None
    core_text, rest = text[:i + 1], text[i + 1:]
    lay = Layout(core=len(core_text.encode("utf-8")), total=len(text.encode("utf-8")), text=text)
    in_table = False
    for line in core_text.splitlines():
        if line.startswith(TABLE_HEAD):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                in_table = False
            elif not line.startswith("|---"):
                lay.table.append(line.strip("|").split("|", 1)[0].strip())
    cur, size, fenced = None, 0, False
    for line in rest.splitlines(keepends=True):
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            if cur is not None:
                lay.sections[cur] = lay.sections.get(cur, 0) + size
            cur, size = section_name(line.rstrip("\n")), 0
        size += len(line.encode("utf-8"))
    if cur is not None:
        lay.sections[cur] = lay.sections.get(cur, 0) + size
    return lay


def problems(lay: Layout | None) -> list[str]:
    """Что нарушает раскладку: пределы, таблица против заголовков, история и тире в тексте."""
    if lay is None:
        return ["нет раскладки: заголовка «# Разделы по случаю» нет"]
    out = []
    if lay.core > CORE_MAX_BYTES:
        out.append(f"ядро {kb(lay.core)} КБ больше {kb(CORE_MAX_BYTES)} КБ")
    if lay.total > FILE_MAX_BYTES:
        out.append(f"prompt.md {kb(lay.total)} КБ больше {kb(FILE_MAX_BYTES)} КБ")
    for name, size in lay.sections.items():
        if size > SECTION_MAX_BYTES:
            out.append(f"раздел «{name}» {kb(size)} КБ больше {kb(SECTION_MAX_BYTES)} КБ")
    for name in lay.table:
        if name not in lay.sections:
            out.append(f"в таблице раздел «{name}», а заголовка нет")
    for name in lay.sections:
        if name not in lay.table:
            out.append(f"раздел «{name}» не назван в таблице «Как читать»")
    if not lay.table:
        out.append("в ядре нет таблицы разделов")
    for n, line in enumerate(lay.text.splitlines(), 1):
        if len(line) > LINE_MAX_CHARS:
            out.append(f"строка {n}: {len(line)} знаков больше {LINE_MAX_CHARS}")
    if _DATED_DECISION.search(lay.text):
        out.append("в тексте «решение Ивана ДД.ММ»: история живёт в git и журнале")
    if _DASHES.search(lay.text):
        out.append("в тексте длинное тире")
    return out


def kb(n: int) -> str:
    return f"{n / 1024:.1f}"


def core_after_edit(text: str, old: str, new: str) -> int | None:
    """Размер ядра после замены old на new; None, если old нет или раскладки нет."""
    if not old or old not in text:
        return None
    lay = split(text.replace(old, new, 1))
    return lay.core if lay else None


def data_header(text: str) -> str:
    """Шапка таблицы data/*.md: от заголовка «# » до первой строки данных
    («|», «## » или «- »). Файл без заголовка шапки не имеет."""
    if not text.startswith("# "):
        return ""
    out = []
    for line in text.splitlines(keepends=True):
        if line.startswith(("|", "## ", "- ", "```")) or _ROW.match(line) or _KEY.match(line):
            break
        out.append(line)
    return "".join(out)


def _event_strings(path: Path, funcs: tuple[str, ...] | None) -> int:
    """Байты строковых литералов текстов событий: внутри названных функций
    (без докстринга) или, при None, в вызовах _() и N_() по всему модулю."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes: list[ast.AST] = []
    for n in ast.walk(tree):
        if funcs is None:
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("_", "N_") and n.args:
                nodes.append(n.args[0])
        elif isinstance(n, ast.FunctionDef) and n.name in funcs:
            nodes += n.body[1:] if ast.get_docstring(n) else n.body
        elif (isinstance(n, ast.Assign) and path.name == "watch.py"
              and any(isinstance(t, ast.Name) and t.id in _WATCH_CONSTS for t in n.targets)):
            nodes.append(n.value)
    return sum(len(c.value.encode("utf-8")) for node in nodes for c in ast.walk(node)
               if isinstance(c, ast.Constant) and isinstance(c.value, str))


def rules_total(root: Path, override: dict[str, str] | None = None) -> dict[str, int]:
    """Байты всех текстов правил по частям; override подменяет текст файла
    (проверка записи до записи). Нет файла: 0."""
    override = override or {}

    def read(rel: str) -> str:
        if rel in override:
            return override[rel]
        p = root / rel
        if not p.exists():
            return ""
        with p.open(encoding="utf-8", newline="") as f:     # концы строк как у записи
            return f.read()

    parts = {"prompt.md": len(read("prompt.md").encode("utf-8"))}
    for rel in RULE_FILES:
        parts[rel] = len(read(rel).encode("utf-8"))
    names = {f"data/{p.name}" for p in (root / "data").glob("*.md")} | {k for k in override if k.startswith("data/")}
    parts["шапки data/*.md"] = sum(len(data_header(read(rel)).encode("utf-8"))
                                   for rel in sorted(names - set(RULE_FILES)))
    parts["тексты событий в коде"] = sum(_event_strings(root / rel, funcs)
                                         for rel, funcs in EVENT_FUNCS.items() if (root / rel).exists())
    return parts


def rules_problems(parts: dict[str, int]) -> list[str]:
    out = []
    total = sum(parts.values())
    if total > RULES_MAX_BYTES:
        out.append(f"тексты правил вместе {kb(total)} КБ больше {kb(RULES_MAX_BYTES)} КБ: "
                   "убери лишнее или не вноси")
    if parts.get("data/popravki.md", 0) > POPRAVKI_MAX_BYTES:
        out.append(f"data/popravki.md больше {kb(POPRAVKI_MAX_BYTES)} КБ: убери истёкшие поправки")
    return out


def growth(now: Layout | None, before: Layout | None) -> list[str]:
    """Строки о размере частей против версии 30 дней назад. Перенос текста из
    одной части в другую не выдаётся за рост: часть, выросшая не больше, чем
    сжалась другая, помечается «похоже на перенос»."""
    if now is None:
        return ["- раскладки нет: размер частей не посчитан"]
    parts = [("ядро", now.core, before.core if before else None, CORE_MAX_BYTES)]
    parts += [(name, size, (before.sections.get(name) if before else None), SECTION_MAX_BYTES)
              for name, size in now.sections.items()]
    shrunk = {}
    if before:
        old_parts = {"ядро": before.core, **before.sections}
        new_parts = {"ядро": now.core, **now.sections}
        shrunk = {n: old_parts[n] - new_parts.get(n, 0) for n in old_parts if new_parts.get(n, 0) < old_parts[n]}
    out = []
    for name, size, old, limit in parts:
        line = f"- {name}: {kb(size)} КБ (предел {kb(limit)})"
        if before is None:
            line += ", 30 дней назад: нет данных"
        elif old is None:
            line += ", новый"
        else:
            delta = size - old
            pct = delta / old if old else 0.0
            line += f", 30 дней назад {kb(old)} КБ ({pct:+.0%})"
            if pct > GROWTH_SIGNAL:
                src = next((n for n, d in shrunk.items() if n != name and d >= 0.8 * delta), None)
                line += f"; похоже на перенос из «{src}»" if src else "; рост больше 10%: что слить или убрать"
        out.append(line)
    total_before = f", 30 дней назад {kb(before.total)} КБ" if before else ""
    out.append(f"- весь файл {kb(now.total)} КБ (предел {kb(FILE_MAX_BYTES)}){total_before}")
    return out
