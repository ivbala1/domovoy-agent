"""Выжимка сессий Ивана с агентами за день: его собственные реплики.

Домовой видит только свой диалог, а большая часть решений дня звучит в
других сессиях: настольных и в темах бота. Блок «За агентом» прежнего
дневника вынимал из них то, что иначе забывается; Иван попросил сохранить
именно это (04.10.2026). Здесь только отбор и нарезка, без модели: читает
и решает, что записать в дневник, сам Домовой.

Граница (решение Ивана 04.10.2026): из личного контекста доступно всё,
включая сессии по проектам заказчиков; из сессии заказчика не должно быть
доступно ничего, кроме его собственной памяти. Поэтому выжимка отдаётся
целиком, но отказывает сессии, закреплённой за заказчиком. Проект сессии
решает её первое сообщение, а не каталог, поэтому главная проверка идёт по
записи закрепления роутера памяти; каталог только вторая линия.

Источник: журналы Claude Code в ~/.claude/projects/*/*.jsonl на этой машине.
Сессии с других машин здесь не видны. Отбор перенесён из claude-tg-bot
(skills/diary_agent/core.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from .common import TZ, load_env
from .i18n import SOURCE_LANG, _, N_, language

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CLIENTS_ROOT = Path.home() / ".agent-memory" / "clients"
WORK_ROOT = Path.home() / "src-work"
PERSONAL_SCOPE = "personal"
NIGHT_CUTOFF_HOUR = 4          # сутки дневника: с 04:00 до 04:00
MAX_MSG_CHARS = 400            # реплики режем: нужен смысл, не стенограмма
MAX_TOTAL_CHARS = 30000        # потолок всей выжимки по умолчанию
MIN_TOTAL_CHARS = 500          # меньше не влезут шапка, один заголовок и строка о скрытом
# Пауза, после которой считаем, что человек отошёл: без неё сессия, открытая
# утром и закрытая ночью, давала бы «12 часов работы».
IDLE_GAP_MINUTES = 15
# Собственный диалог Домового: его он знает и так. Узнаётся по событию
# пробуждения (wake.py, watch.py), а не по одной фразе: разговор О Домовом,
# где эту фразу процитировали, чужим от этого не становится.
OWN_DIALOG_MARK = N_("Действуй по промту Домового")
# Событие узнаётся по началу реплики: перед заголовком дня допустимы только
# пометки бота в квадратных скобках (ран по чужому тексту), предупреждение
# синхронизации, которое wake.py ставит первым абзацем (ревью Codex
# 14.10.2026: сессию поиска потом никто не помечает своей), и баннер пробного
# запуска. Строка с датой в середине вставленного текста не в счёт: вставка
# вывода `wake --dry-run` выкидывала всю сессию (ревью 11.10.2026).
_WAKE_RE_RU = re.compile(
    r"(?:\[[^\]]*\]\s*)*(?:⚠\ufe0f? Синхронизация репозитория Домового[^\n]*\s*)?"
    r"(?:⚠\ufe0f? ПРОБНЫЙ ЗАПУСК[^\n]*\s*)?"
    r"(Утро|Поиск|День|Вечер|Сторож)(?: \(пробный запуск\))?, \d{4}-\d{2}-\d{2}"
)
# Пробуждение приходит только через бота. Поле entrypoint необязательное,
# поэтому без него тоже; в настольной сессии и терминале это вставка Ивана.
# Сверяется поле самой записи с пробуждением, а не сессии.
# Не «первая реплика файла»: вечернее пробуждение продолжает сессию темы,
# которую мог начать сам Иван (ревью 11.10.2026).
_WAKE_ENTRIES = ("", "sdk-cli")
# Одноразовый прогон программы узнаётся по известному шаблону, а не по длине:
# длина авторства не доказывает, и короткое «да» в теме бота это ответ Ивана.
_MACHINE_FIRST = ("# Роль",)   # промты прежних шагов дневника
_MACHINE_EXACT = ("ok",)       # проверка связи бота
# Каталоги проектов, где людей не бывает: служебные прогоны сторожа и судьи,
# черновые каталоги подагентов. Не «любой /tmp»: в /tmp Иван тоже работает.
_MACHINE_DIR_MARKS = ("-tmp-domovoy-llm-", "-tmp-claude-", "-local-state-", "wf_")
# Служебные вставки харнесса в записи user. Не «любой <»: реплика Ивана может
# начинаться с вставленного `<config>`.
_NOISE_PREFIXES = (
    "Base directory for this skill:",
    "<local-command", "<command-name>", "<command-message>", "<command-args>",
    "<system-reminder>", "<task-notification>", "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "<user-prompt-submit-hook>",
    "[SYSTEM NOTIFICATION",
    "[Request interrupted by user",   # пометка прерывания, не слова (ревью 11.10.2026)
    "Caveat:",
)
# Слеш-команда Ивана: служебные теги, но в аргументах его слова (ревью 11.10.2026).
_COMMAND_PREFIXES = ("<command-name>", "<command-message>", "<command-args>")
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_ENTRY_TITLES = {"claude-desktop": N_("настольная"), "cli": N_("терминал"), "sdk-cli": N_("через бота")}


@dataclass
class Session:
    path: Path
    cwd: str = ""
    entry: str = ""
    own_dialog: bool = False
    file_humans: int = 0       # реплик человека во всём файле, не только в окне
    file_first: str = ""       # первая из них целиком
    stamps: list[datetime] = field(default_factory=list)
    replies: list[tuple[datetime, str]] = field(default_factory=list)

    @property
    def minutes(self) -> int:
        """Активные минуты: сумма промежутков короче IDLE_GAP_MINUTES."""
        ordered = sorted(self.stamps)
        total = 0.0
        for a, b in zip(ordered, ordered[1:]):
            gap = (b - a).total_seconds() / 60
            if gap <= IDLE_GAP_MINUTES:
                total += gap
        return int(total)

    @property
    def machine(self) -> bool:
        """Одноразовый прогон программы: за всю сессию одна «реплика», и она
        известного машинного вида. Считается по всему файлу: вчерашний
        разговор в теме бота с сегодняшним единственным «да» остаётся."""
        if self.entry != "sdk-cli" or self.file_humans != 1:
            return False
        first = self.file_first.strip()
        return first.casefold() in _MACHINE_EXACT or first.startswith(_MACHINE_FIRST)


def day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, dtime(NIGHT_CUTOFF_HOUR), tzinfo=TZ)
    return start, start + timedelta(days=1)


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
    return []


def is_human_message(text: str) -> bool:
    """Реплика человека, а не служебная вставка. Длину не фильтруем: «ок» и
    «да» часто и есть момент решения."""
    t = text.strip()
    return bool(t) and not t.startswith(_NOISE_PREFIXES)


def command_text(text: str) -> str:
    """Слеш-команда с аргументами: «команда /имя: аргументы». '' если это не
    команда или аргументов нет: без них слов Ивана в записи нет, а вывод
    команд (<local-command-*>) и так отсекается (ревью 11.10.2026)."""
    t = text.strip()
    if not t.startswith(_COMMAND_PREFIXES):
        return ""
    name, args = _COMMAND_NAME_RE.search(t), _COMMAND_ARGS_RE.search(t)
    if not name or not args or not args.group(1).strip():
        return ""
    return _("команда {name}: {args}").format(name=name.group(1).strip(), args=args.group(1).strip())


def human_texts(d: dict) -> list[tuple[str, str]]:
    """Слова Ивана в одной записи журнала: (вид, текст). Видов три:

    - user: обычная реплика, запись user с текстовыми частями, в том числе
      слеш-команда с аргументами;
    - queued: реплика, отправленная посреди работы агента, вложение
      queued_command с origin human (в таких часто и звучит решение);
    - choice: выбор в вопросе с вариантами, toolUseResult.answers.
    """
    if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        # isSidechain: промт подагенту от самого агента; isMeta: вставка
        # харнесса (тело скилла, оговорка); isCompactSummary: пересказ
        # разговора после сжатия контекста (ревью 11.10.2026). Всё это не
        # слова Ивана.
        return []
    kind = d.get("type")
    if kind == "attachment":
        att = d.get("attachment")
        if (
            isinstance(att, dict)
            and att.get("type") == "queued_command"
            and (att.get("origin") or {}).get("kind") == "human"
            and str(att.get("prompt") or "").strip()
        ):
            return [("queued", str(att["prompt"]))]
        return []
    if kind != "user":
        return []
    out = []
    for t in _texts((d.get("message") or {}).get("content")):
        if is_human_message(t):
            out.append(("user", t))
        elif command := command_text(t):
            out.append(("user", command))
    result = d.get("toolUseResult")
    answers = result.get("answers") if isinstance(result, dict) else None
    if isinstance(answers, dict):
        for question, answer in answers.items():
            out.append(("choice", _("выбрал «{answer}» на вопрос агента: {question}").format(
                answer=answer, question=' '.join(str(question).split())[:200])))
    return out


def is_wake_event(text: str) -> bool:
    """Событие пробуждения Домового: начало его собственного диалога. Только
    по формату текста: поле entrypoint в записи необязательное, и без него
    машинный промт пробуждения иначе вышел бы словами Ивана."""
    return _(OWN_DIALOG_MARK) in text and bool(_wake_re().match(text.lstrip()))


def _wake_re() -> re.Pattern:
    """Начало события пробуждения на языке сообщений. По-русски прежнее точное
    выражение; на другом языке заголовки дня из перевода, а пометки и
    предупреждения перед ним любой строкой со значком ⚠️."""
    if language() == SOURCE_LANG:
        return _WAKE_RE_RU
    titles = "|".join(re.escape(_(t)) for t in ("Утро", "Поиск", "День", "Вечер")) + "|Сторож"
    return re.compile(r"(?:\[[^\]]*\]\s*)*(?:⚠\ufe0f?[^\n]*\s*)*"
                      r"(" + titles + r")(?: \([^)\n]*\))?, \d{4}-\d{2}-\d{2}")


def read_session(path: Path, start: datetime, end: datetime, seen: set[str] | None = None) -> Session | None:
    """Срез сессии за окно. None: в окне активности не было.

    seen: идентификаторы уже учтённых записей. Возобновлённая или ответвлённая
    сессия переносит прежние записи в новый файл с теми же uuid, и без сверки
    одна реплика считалась бы дважды.
    """
    seen = seen if seen is not None else set()
    s = Session(path=path)
    try:
        fh = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            s.entry = s.entry or str(d.get("entrypoint") or "")
            try:
                pairs = human_texts(d)
            except (AttributeError, TypeError):
                # Запись не той структуры ("text": null, message строкой):
                # пропускается целиком, без её текста; прежде она обрывала
                # всю выжимку (ревью 11.10.2026).
                continue
            # Признаки сессии считаем по всему файлу, не только в окне:
            # диалог мог начаться пробуждением ещё вчера.
            for _kind, text in pairs:
                # entrypoint самой записи, не первой в файле: /resume продолжает
                # настольную сессию в теме бота, /handback сессию бота в
                # терминале (ревью 11.10.2026).
                if str(d.get("entrypoint") or "") in _WAKE_ENTRIES and is_wake_event(text):
                    s.own_dialog = True
                s.file_humans += 1
                s.file_first = s.file_first or text
            raw = d.get("timestamp") or ""
            if not raw:
                continue
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(TZ)
            except (AttributeError, TypeError, ValueError, OverflowError):
                # Не строка, не дата или выход за пределы datetime при смене
                # часового пояса (ревью 11.10.2026).
                continue
            if not start <= ts < end:
                continue
            uuid = str(d.get("uuid") or "")
            if uuid:
                if uuid in seen:
                    continue
                seen.add(uuid)
            s.stamps.append(ts)
            s.cwd = s.cwd or str(d.get("cwd") or "")
            # Одинаковые тексты не сводим: два «да» это два ответа. Реплика из
            # очереди и обычная запись это разные сообщения: на 318 реплик из
            # очереди в журналах за неделю совпадение текста нашлось одно.
            for _kind, text in pairs:
                s.replies.append((ts, " ".join(text.split())[:MAX_MSG_CHARS]))
    return s if s.stamps else None


def collect(projects_root: Path, day: date) -> list[Session]:
    """Сессии с репликами человека за сутки дневника, по времени начала."""
    start, end = day_window(day)
    out: list[Session] = []
    if not projects_root.is_dir():
        return out
    seen: set[str] = set()
    # Только файлы самих сессий: в подкаталогах лежат журналы подагентов.
    for f in sorted(projects_root.glob("*/*.jsonl")):
        if any(mark in f.parent.name for mark in _MACHINE_DIR_MARKS):
            continue
        try:
            # Не тронутый с начала окна файл в этот день не писался: журналы
            # копятся месяцами, читать их все на каждый запрос незачем.
            if f.stat().st_mtime < start.timestamp():
                continue
        except OSError:
            continue
        s = read_session(f, start, end, seen)
        if s is None or not s.replies or s.own_dialog or s.machine:
            continue
        out.append(s)
    return sorted(out, key=lambda s: min(s.stamps))


def fmt_minutes(m: int) -> str:
    h, mm = divmod(m, 60)
    if h and mm:
        return _("{h} ч {mm} мин").format(h=h, mm=mm)
    return _("{h} ч").format(h=h) if h else _("{mm} мин").format(mm=mm)


def _short_cwd(cwd: str) -> str:
    home = str(Path.home())
    return "~" + cwd[len(home):] if cwd.startswith(home) else (cwd or _("каталог неизвестен"))


def render(sessions: list[Session], day: date, limit: int = MAX_TOTAL_CHARS) -> str:
    """Выжимка не длиннее limit знаков: в счёт идут и заголовки сессий, и
    переводы строк, и строка о том, сколько не поместилось."""
    if not sessions:
        return _("За {day} реплик владельца в других сессиях на этой машине нет.").format(day=day.isoformat())
    limit = max(limit, MIN_TOTAL_CHARS)
    head = _(
        "Реплики владельца в сессиях с агентами за {day} (сутки с 04:00), "
        "без диалога с Домовым. Реплики обрезаны, это подсказка, а не готовые события."
    ).format(day=day.isoformat())
    reserve = 120                      # место под строку о непоместившемся
    budget = max(limit - len(head) - reserve, 0)
    lines = [head]
    hidden_replies = hidden_sessions = 0
    for s in sessions:
        first, last = min(s.stamps), max(s.stamps)
        title = (
            _("\n== {cwd} · {entry} · {first:%H:%M}-{last:%H:%M} · активно {active} · реплик {count}").format(
                cwd=_short_cwd(s.cwd),
                entry=_(_ENTRY_TITLES[s.entry]) if s.entry in _ENTRY_TITLES else (s.entry or _("сессия")),
                first=first, last=last, active=fmt_minutes(s.minutes), count=len(s.replies))
        )
        rows = [f"- {ts:%H:%M} {text}" for ts, text in s.replies]
        # Заголовок без единой реплики под ним бесполезен: сессия либо
        # входит хотя бы первой репликой, либо считается непоказанной.
        if len(title) + 1 + len(rows[0]) + 1 > budget:
            hidden_sessions += 1
            hidden_replies += len(rows)
            continue
        lines.append(title)
        budget -= len(title) + 1
        for row in rows:
            if len(row) + 1 > budget:
                hidden_replies += 1
                continue
            lines.append(row)
            budget -= len(row) + 1
    if hidden_replies:
        tail = _("\n… не показано реплик: {count}").format(count=hidden_replies)
        if hidden_sessions:
            tail += _(", из них целых сессий: {count}").format(count=hidden_sessions)
        lines.append(tail + _(" (потолок {limit}, поднять: --limit)").format(limit=limit))
    # Одиночный суррогат из журнала (\ud83d) печать в UTF-8 роняла целиком;
    # замена знак на знак, длина та же (ревью 11.10.2026).
    return "\n".join(lines).encode("utf-8", "replace").decode("utf-8")


# --- граница заказчика ---------------------------------------------------


def _router_state_dir() -> Path:
    """Каталог состояния роутера памяти, те же правила, что у него самого."""
    configured = os.environ.get("MEMORY_CONTEXT_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    memoryctl = os.environ.get("MEMORYCTL_STATE_DIR")
    if memoryctl:
        return Path(memoryctl).expanduser() / "claude"
    xdg = Path(os.environ.get("XDG_STATE_HOME", "").strip() or Path.home() / ".local" / "state")
    return xdg / "agent-memory" / "claude"


def pin_record(session_id: str, state_dir: Path | None = None) -> dict:
    """Запись закрепления сессии роутером памяти ({"scope", "source", ...}).
    {} если записи нет или она не читается."""
    if not session_id:
        return {}
    key = hashlib.sha256(session_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    path = (state_dir or _router_state_dir()) / "pins" / f"{key}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def pinned_scope(session_id: str, state_dir: Path | None = None) -> str:
    """За каким проектом роутер памяти закрепил сессию. '' если записи нет
    или она не читается."""
    scope = pin_record(session_id, state_dir).get("scope")
    return scope if isinstance(scope, str) else ""


# Откуда агент передаёт идентификатор своей сессии командам. У Codex это
# CODEX_THREAD_ID (и CODEX_SESSION_ID с тем же значением), его же хук роутера
# получает как session_id (проверено 11.10.2026 на Codex 0.162.0). Без них
# сессия Codex, закреплённая за заказчиком, получала выжимку (ревью 11.10.2026).
SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")


def session_ids(environ=os.environ) -> list[str]:
    """Идентификаторы сессий агентов, из которых запущена команда. Codex,
    запущенный из сессии Claude, видит оба, и проверяются оба."""
    out: list[str] = []
    for name in SESSION_ENV:
        val = environ.get(name, "")
        if val and val not in out:
            out.append(val)
    return out


def env_refusal(environ=os.environ) -> str:
    """Признаки агента без идентификатора сессии. Отсутствие идентификатора само
    по себе не доказывает запуск руками или таймером: Claude Code ставит
    CLAUDECODE вместе с CLAUDE_CODE_SESSION_ID, и одно без другого это
    неизвестный контекст, а неизвестное здесь отказ."""
    if not session_ids(environ) and ("CLAUDECODE" in environ or any(n in environ for n in SESSION_ENV)):
        return _("команду запустил агент без идентификатора сессии")
    return ""


# Виды личного закрепления, которые роутер памяти пишет и отдаёт личную память
# (resolve_pin и handle_session в ~/src/svod/lib/memorycontext.py). forked там
# получает только контракт. Список разрешающий: новый вид у роутера здесь
# будет отказом, пока его не впишут.
PERSONAL_PIN_SOURCES = ("default-personal", "ambiguous-first-message", "resumed-without-pin")


def _under(path: str, root: Path) -> bool:
    """path лежит в root или равен ему. Регистр не различается, как и в
    файловой системе мака; на Linux это лишь лишний отказ."""
    base = str(root).casefold().rstrip(os.sep)
    here = path.casefold()
    return here == base or here.startswith(base + os.sep)


def client_context(cwd: Path, clients_root: Path | None = None, work_root: Path | None = None) -> str:
    """Имя заказчика, если команду запустили из его каталога, иначе ''.

    OSError (каталог не разрешается, корни не читаются, корня заказчиков нет)
    уходит вызывающему: неизвестный каталог там отказ, а не разрешение.
    """
    clients_root = clients_root or CLIENTS_ROOT
    work_root = work_root or WORK_ROOT
    cwd = cwd.resolve()
    entries = list(clients_root.iterdir())          # нет корня: OSError, а не «заказчиков нет»
    names = {p.name.casefold() for p in entries if p.is_dir()}
    for part in cwd.parts:
        if part.casefold() in names:
            return part
    # Корни разрешаются так же, как cwd: иначе путь через символическую ссылку
    # (на маке /var ведёт в /private/var) каталог под src-work не узнавал (ревью
    # 11.10.2026). Ссылки внутри корней ведут наружу, их цели тоже корни.
    roots = [(work_root.resolve(), work_root.name)]
    roots += [(p.resolve(), p.name) for p in entries if p.is_symlink()]
    if work_root.is_dir():
        for p in work_root.iterdir():
            kids = list(p.iterdir()) if p.is_dir() and not p.is_symlink() else []
            roots += [(q.resolve(), work_root.name) for q in [p, *kids] if q.is_symlink()]
    here = str(cwd)
    for root, name in roots:
        if _under(here, root):
            return name
    return ""


def refusal(session_id: str | list[str], cwd: Path, state_dir: Path | None = None) -> str:
    """Почему выжимку отдавать нельзя ('' если можно)."""
    for sid in [session_id] if isinstance(session_id, str) else session_id:
        if not sid:
            continue
        # Команду запустил агент из сессии. Разрешаем только подтверждённо
        # личную: нет записи закрепления, она повреждена или это подагент со
        # своим идентификатором, значит неизвестно, чья сессия, а неизвестное
        # здесь отказ. Без идентификатора команду запускают руками или таймер.
        record = pin_record(sid, state_dir)
        scope = record.get("scope") if isinstance(record.get("scope"), str) else ""
        if scope != PERSONAL_SCOPE:
            return (_("сессия закреплена за проектом {scope}").format(scope=scope) if scope
                    else _("закрепление сессии не подтверждено (нет записи роутера памяти)"))
        source = record.get("source")
        if source == "forked":
            # Ответвление несёт историю родителя, а чья она, роутер не знает и
            # отдаёт такой сессии только контракт; выжимка тоже не положена.
            return _("сессия ответвлена от другого разговора (роутер памяти отдаёт ей только контракт)")
        if source not in PERSONAL_PIN_SOURCES:
            return _("вид закрепления сессии не подтверждён как личный ({source})").format(source=source or _("не указан"))
    try:
        client = client_context(cwd)
    except (OSError, RuntimeError):         # RuntimeError: петля ссылок в Python до 3.13
        return _("граница не проверяется: рабочий каталог или список заказчиков не читается")
    if client:
        return _("команда запущена из каталога заказчика ({client})").format(client=client)
    return ""


# Граница заказчиков опирается на закрепления роутера памяти Свода и каталоги
# заказчиков. Без Свода её проверить нечем, и выжимка отказывала бы всегда:
# DOMOVOY_CLIENT_BOUNDARY=off выключает проверку явно (открытая копия, машина
# без заказчиков). По умолчанию включена: неизвестное здесь отказ.
BOUNDARY_ENV = "DOMOVOY_CLIENT_BOUNDARY"


def boundary_enabled() -> bool:
    load_env()
    return os.environ.get(BOUNDARY_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def digest(day: date, limit: int = MAX_TOTAL_CHARS) -> tuple[int, str]:
    """(код выхода, текст) для команды `domovoy.diary sessions`."""
    if not boundary_enabled():
        return 0, render(collect(PROJECTS_ROOT, day), day, limit)
    why = env_refusal()
    if not why:
        try:
            cwd = Path(os.getcwd())
        except OSError:                     # рабочий каталог удалён
            why = _("граница не проверяется: рабочий каталог не определяется")
        else:
            why = refusal(session_ids(), cwd)
    if why:
        return 1, _(
            "отказ: {why}. Выжимка сессий отдаётся только личному контексту: "
            "сессия заказчика чужие сессии не читает."
        ).format(why=why)
    return 0, render(collect(PROJECTS_ROOT, day), day, limit)
