#!/bin/bash
# Usage: ./install.sh            install (or update) and start the agent
#        ./install.sh --uninstall   stop and remove the agent (data is kept)
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
DATA="$REPO/data"
VENV="$REPO/.venv"
LEGACY="$HOME/Library/Application Support/worktracker"
LABEL="com.worktracker.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Agent removed. Your data is still in: $DATA"
  exit 0
fi

# pyobjc needs Python 3.10+; an older default python3 fails to build it.
find_python() {
  for py in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$py" >/dev/null &&
       "$py" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      command -v "$py"
      return
    fi
  done
  echo "Python 3.10 or newer is required (e.g. brew install python)." >&2
  exit 1
}

if [[ ! -x "$VENV/bin/python" ]] || ! "$VENV/bin/python" -c "import AppKit, Quartz" 2>/dev/null; then
  echo "Creating Python environment..."
  rm -rf "$VENV"
  PYTHON="$(find_python)"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet pyobjc-framework-Cocoa pyobjc-framework-Quartz
fi

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
mkdir -p "$DATA/logs" "$HOME/Library/LaunchAgents"

# Earlier versions kept data in ~/Library/Application Support/worktracker.
if [[ -f "$LEGACY/tracker.db" && ! -f "$DATA/tracker.db" ]]; then
  echo "Moving existing data from $LEGACY"
  for f in tracker.db tracker.db-wal tracker.db-shm config.json; do
    if [[ -f "$LEGACY/$f" ]]; then mv "$LEGACY/$f" "$DATA/"; fi
  done
  if [[ -d "$LEGACY/logs" ]]; then
    find "$LEGACY/logs" -mindepth 1 -maxdepth 1 -exec mv {} "$DATA/logs/" \;
  fi
  echo "The old folder now only holds a stale copy of the code; you can delete it: $LEGACY"
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$REPO/tracker.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WORKTRACKER_HOME</key><string>$DATA</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>Nice</key><integer>10</integer>
  <key>LowPriorityBackgroundIO</key><true/>
  <key>StandardOutPath</key><string>$DATA/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$DATA/logs/launchd.err</string>
</dict>
</plist>
EOF

launchctl bootstrap "$DOMAIN" "$PLIST"

PORT=$("$VENV/bin/python" -c "import json,sys;print(json.load(open(sys.argv[1])).get('dashboard_port',8765))" "$DATA/config.json" 2>/dev/null || echo 8765)
echo "Running. Dashboard: http://127.0.0.1:$PORT"
echo "Config:  $DATA/config.json  (edits apply automatically, except sample_seconds and port)"
echo "Logs:    $DATA/logs/"
echo "First run: allow Automation access for your browser when macOS asks."
echo "Goals, work window and alerts: open the dashboard and use Goals & settings."
echo "After editing the code, run ./install.sh again to restart the agent."
