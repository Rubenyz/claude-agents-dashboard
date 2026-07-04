#!/bin/bash
# Installs the Claude Agents Dashboard: a launcher (pinnable in the taskbar),
# an autostart entry (background process in the system tray) and (re)starts
# the app from this project directory. Idempotent.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY=/usr/bin/python3
APP=claude-agents-dashboard

mkdir -p "$HOME/.local/share/applications" "$HOME/.config/autostart"

cat > "$HOME/.local/share/applications/$APP.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Claude Agents
GenericName=Active Claude agents
Comment=Shows all active Claude Code agents
Exec=$PY $DIR/dashboard.py --show
Icon=$DIR/icon.svg
Terminal=false
Categories=Utility;
StartupWMClass=$APP
StartupNotify=false
EOF

cat > "$HOME/.config/autostart/$APP.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Claude Agents Dashboard
Comment=Background process (system tray) with active Claude agents
Exec=$PY $DIR/dashboard.py
Icon=$DIR/icon.svg
Terminal=false
X-KDE-autostart-after=panel
X-GNOME-Autostart-enabled=true
StartupWMClass=$APP
StartupNotify=false
EOF

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

# Stop a possibly running instance (real python processes only).
for p in $(pgrep -f 'dashboard\.py' 2>/dev/null || true); do
  c=$(cat "/proc/$p/comm" 2>/dev/null || true)
  case "$c" in python*) kill "$p" 2>/dev/null || true ;; esac
done
sleep 1

# Start hidden in the tray from this directory.
setsid "$PY" "$DIR/dashboard.py" >/dev/null 2>&1 &
disown 2>/dev/null || true

echo "Installed. App runs from: $DIR"
echo "Optionally pin the 'Claude Agents' icon to the taskbar via right-click -> Pin to Task Manager."
