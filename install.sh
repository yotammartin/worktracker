#!/bin/bash
# Usage: ./install.sh            install (or update) and start the agent
#        ./install.sh --uninstall   stop and remove the agent (data is kept)
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
DATA="$REPO/data"
VENV="$REPO/.venv"
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
fi
# Reinstall dependencies whenever requirements.txt changes (e.g. after a git pull).
if ! cmp -s "$REPO/requirements.txt" "$VENV/.requirements.txt"; then
  "$VENV/bin/pip" install --quiet -r "$REPO/requirements.txt"
  cp "$REPO/requirements.txt" "$VENV/.requirements.txt"
fi

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# bootout returns before the service is fully gone; bootstrapping too early fails
# with "Bootstrap failed: 5: Input/output error".
for _ in $(seq 1 50); do
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 0.2
done
mkdir -p "$DATA/logs" "$HOME/Library/LaunchAgents"

# plistlib escapes paths, so folders with &, < or non-ASCII characters work.
"$VENV/bin/python" - "$PLIST" "$LABEL" "$VENV/bin/python" "$REPO/tracker.py" "$DATA" <<'PLIST_PY'
import plistlib, sys
plist, label, python, tracker, data = sys.argv[1:]
with open(plist, "wb") as f:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": [python, tracker],
        "EnvironmentVariables": {"WORKTRACKER_HOME": data},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "Nice": 10,
        "LowPriorityBackgroundIO": True,
        "StandardOutPath": f"{data}/logs/launchd.out",
        "StandardErrorPath": f"{data}/logs/launchd.err",
    }, f)
PLIST_PY

launchctl bootstrap "$DOMAIN" "$PLIST"

PORT=$("$VENV/bin/python" -c "import json,sys;print(json.load(open(sys.argv[1])).get('dashboard_port',8765))" "$DATA/config.json" 2>/dev/null || echo 8765)
# Wait until this install's dashboard answers; a port owned by something else must not look like success.
ready=""
for _ in $(seq 1 40); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/api/stats" 2>/dev/null | grep -q '"settings"'; then
    ready=1
    break
  fi
  sleep 0.25
done
if [[ -z "$ready" ]]; then
  echo "The agent started, but the dashboard isn't answering on port $PORT." >&2
  echo "See $DATA/logs/worktracker.log. If the port is taken, set \"dashboard_port\" in $DATA/config.json and run ./install.sh again." >&2
  exit 1
fi
echo "Running. Dashboard: http://127.0.0.1:$PORT"
echo "Config:  $DATA/config.json  (edits apply automatically, except sample_seconds and dashboard_port)"
echo "Logs:    $DATA/logs/"
echo "First run: allow Automation access for your browser when macOS asks."
echo "Goals, work window and alerts: open the dashboard and use Goals & settings."
echo "After updating the code (e.g. git pull), run ./install.sh again to restart the agent."
