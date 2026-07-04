#!/bin/bash
# Mirror the Claude Code session name into the KDE Konsole tab/window title
# (so it shows up in the window switcher), and cache it in
# $XDG_RUNTIME_DIR/claude-konsole-title-<sessionId> where the dashboard picks
# it up as a name fallback.
#
# Wire it up in ~/.claude/settings.json twice:
#   - call it from your statusline script:  printf '%s' "$input" | /path/to/konsole-title.sh
#   - as a Stop hook, so the title doesn't fall back to the Konsole default
#     ("%d : %n") once the session goes idle:
#       "hooks": { "Stop": [ { "hooks": [ { "type": "command",
#         "command": "/path/to/konsole-title.sh" } ] } ] }
#
# Reads the statusline/hook JSON on stdin. Safe outside Konsole; fails silently.
input=$(cat)
[ -n "$KONSOLE_DBUS_SERVICE" ] && [ -n "$KONSOLE_DBUS_SESSION" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

SESSION_ID=$(printf '%s' "$input" | jq -r '.session_id // "x"')
NAME=$(printf '%s' "$input" | jq -r '.session_name // empty')

# The statusline JSON has .session_name; the Stop-hook JSON does not. The
# statusline therefore caches the name in a file the Stop hook reads back.
CACHE="${XDG_RUNTIME_DIR:-/tmp}/claude-konsole-title-${SESSION_ID}"
if [ -n "$NAME" ]; then
  printf '%s' "$NAME" >"$CACHE"
else
  NAME=$(cat "$CACHE" 2>/dev/null)
fi

if [ -n "$NAME" ]; then
  FMT=${NAME//%/%%}        # escape % against Konsole format placeholders
else
  FMT='%d : %n'            # nothing known: Konsole default (dir : program)
fi

QD=$(command -v qdbus6 || command -v qdbus || command -v qdbus-qt6 || true)
[ -n "$QD" ] && "$QD" "$KONSOLE_DBUS_SERVICE" "$KONSOLE_DBUS_SESSION" \
  org.kde.konsole.Session.setTabTitleFormat 0 "$FMT" >/dev/null 2>&1
exit 0
