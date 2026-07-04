#!/bin/bash
# Example Claude Code statusline: shows model, a context-usage bar and the
# 5-hour rate limit with its reset time, e.g.
#   [Fable (agent)] context: ▓▓▓░░░░░░░ 34%  |  5h limit: 12% resets 14:00
#
# It also feeds the two optional dashboard integrations:
#   - writes the exact context percentage to
#     $XDG_RUNTIME_DIR/claude-agents-ctx-<sessionId>.json
#   - forwards the JSON to konsole-title.sh (KDE Konsole window titles)
#
# Wire it up in ~/.claude/settings.json:
#   "statusLine": { "type": "command", "command": "/path/to/statusline.sh" }
input=$(cat)

MODEL=$(echo "$input" | jq -r '.model.display_name')
AGENT=$(echo "$input" | jq -r '.agent.name // empty')
PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)

# Write the real context percentage per session so the dashboard can show
# exactly the same number as this statusline.
SID=$(echo "$input" | jq -r '.session_id // empty')
if [ -n "$SID" ]; then
  echo "$input" \
    | jq -c '{pct: (.context_window.used_percentage // null), model: .model.display_name}' \
    > "${XDG_RUNTIME_DIR:-/tmp}/claude-agents-ctx-$SID.json" 2>/dev/null
fi

# KDE Konsole: mirror the session name into the window title.
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
[ -x "$SCRIPT_DIR/konsole-title.sh" ] \
  && printf '%s' "$input" | "$SCRIPT_DIR/konsole-title.sh" >/dev/null 2>&1

# Build progress bar: printf -v creates a run of spaces, then
# ${var// /▓} replaces each space with a block character
BAR_WIDTH=10
FILLED=$((PCT * BAR_WIDTH / 100))
EMPTY=$((BAR_WIDTH - FILLED))
BAR=""
[ "$FILLED" -gt 0 ] && printf -v FILL "%${FILLED}s" && BAR="${FILL// /▓}"
[ "$EMPTY" -gt 0 ] && printf -v PAD "%${EMPTY}s" && BAR="${BAR}${PAD// /░}"

MODEL_LABEL="$MODEL"
[ -n "$AGENT" ] && MODEL_LABEL="$MODEL_LABEL ($AGENT)"
OUTPUT="[$MODEL_LABEL] context: $BAR $PCT%"

# Append 5-hour rate limit usage and reset time if available
FIVE_PCT=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
if [ -n "$FIVE_PCT" ]; then
  FIVE_INT=$(LC_NUMERIC=C printf '%.0f' "$FIVE_PCT")
  FIVE_RESETS_AT=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
  RESETS_STR=""
  if [ -n "$FIVE_RESETS_AT" ]; then
    RESETS_STR=" resets $(date -d "@${FIVE_RESETS_AT}" +%H:%M)"
  fi
  OUTPUT="$OUTPUT  |  5h limit: ${FIVE_INT}%${RESETS_STR}"
fi

echo "$OUTPUT"
