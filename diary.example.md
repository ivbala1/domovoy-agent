# Diary: rules for Domovoy

The diary is not Domovoy's journal. It is the owner's personal diary in
Obsidian, one file per day: `<vault>/Дневник/YYYY/YYYY-MM-DD.md` (the folder
names are fixed in code). You keep it. This file is an example: adapt it.

## How to write

Only with commands; never edit day files with editing tools. A command appends
a block, does not write the same story twice, keeps the header lists and the
feed up to date, and commits the vault to its local git.

```bash
# a story of the day: your retelling, then the owner's original words after the separator
~/domovoy/.venv/bin/python -m domovoy.diary add --label day --people Anna --places Lisbon <<'EOF'
Retold in the first person.

=== original words ===
the owner's text exactly as it came
EOF

~/domovoy/.venv/bin/python -m domovoy.diary fact "Got an interview invitation from X" --source mail --time 14:20
~/domovoy/.venv/bin/python -m domovoy.diary mark --people Anna --places Porto
~/domovoy/.venv/bin/python -m domovoy.diary status          # what is recorded for 7 days, weekly summary or not
~/domovoy/.venv/bin/python -m domovoy.diary sessions        # what the owner said in other agent sessions today
~/domovoy/.venv/bin/python -m domovoy.diary rollup --week 2026-W40 <<'EOF'
summary of the week
EOF
```

Every write command takes `--day YYYY-MM-DD` (default today; before 04:00 it
is yesterday), `--time HH:MM` and `--dry-run`. One `--label` per story: `day`
for the evening answer, `evening` for an addition, and so on.

## What never changes

Blocks (marked `<!-- src:... -->`), yours and the owner's, are never edited or
deleted. If you got something wrong or the owner corrected a fact, add a new
block "Correction: ...".

## The evening question

At the evening wake-up, one question about the day, a concrete one: use what
you already know (events of the day, the calendar). Before it, in the same
message, the plan check (see `prompt.md`).

## What goes into the diary and how to retell

The diary is for life as it was lived: where the owner was, what they did, with
whom, what they thought and felt. Not tasks for you, not questions, not work
correspondence.

- First person, in the owner's tone, in the order of events; remove
  repetitions, filler words and speech recognition errors.
- Keep every fact, name, place, number and every judgement of the owner; do not
  smooth out negation, doubt or who said what.
- Add nothing of your own: no conclusions, morals or guesses about causes.
- Always put the original words after the separator, complete and unedited.
- "Write it word for word": `--kind verbatim`, the text as is.

## Events of the day

Write with `fact` as soon as you learn something happened: from the owner's
words (including other sessions, see `sessions`), from mail or messages. The
goal is to keep what would otherwise be forgotten: decisions, agreements, the
result of a meeting. Plans and ideas the owner voiced go as a separate line
starting with "💭".

## People and places

`--people` are only those the owner actually met that day; people cards are
built from this field. Someone who wrote or called is not "met".

## On every wake-up: finish what was missed

Start with `status`:

- no entry for yesterday: in the morning offer in one line to tell about it;
- "weekly summary ... MISSING": write it (a short retelling of the week);
- "git: ...": uncommitted changes in the vault, run `commit`;
- "day file unreadable": tell the owner in one line, do not fix it yourself.
