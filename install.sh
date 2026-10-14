#!/usr/bin/env bash
# Domovoy install on the machine with the timers: venv, dependencies, .env,
# systemd user units. Safe to run again.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VENV="$REPO/.venv"

echo "-> venv in $VENV"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$REPO[dev]"

if [ ! -f "$REPO/.env" ]; then
    (umask 077 && cp "$REPO/.env.example" "$REPO/.env")
    echo "   created .env from the example: fill in DOMOVOY_CHAT_ID (and the rest you need), then run ./install.sh again"
    exit 1
fi
chmod 600 "$REPO/.env"
mkdir -p "$REPO/journal" "$REPO/data"
[ -f "$REPO/prompt.md" ] || cp "$REPO/prompt.example.md" "$REPO/prompt.md"
[ -f "$REPO/diary.md" ] || cp "$REPO/diary.example.md" "$REPO/diary.md"

echo "-> systemd user units"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
for f in "$REPO"/systemd/*; do
    sed "s#@REPO@#${REPO}#g" "$f" > "$unit_dir/$(basename "$f")"
    echo "   $(basename "$f")"
done
systemctl --user daemon-reload
systemctl --user enable --now \
    domovoy-morning.timer domovoy-evening.timer domovoy-sync.timer domovoy-sync.path

echo
echo "-> check"
"$VENV/bin/python" -m domovoy.wake --morning --dry-run --force
systemctl --user list-timers 'domovoy-*' --no-pager
