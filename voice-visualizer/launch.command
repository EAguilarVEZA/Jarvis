#!/usr/bin/env bash
# Martin Voice Visualizer launcher. Double-click, or run ./launch.command
# Pass --mock to run the scripted demo (no voice line needed): ./launch.command --mock
cd "$(dirname "$0")"
PORT=8777; MODE=""
if [ "$1" = "--mock" ]; then PORT=8778; MODE="--mock"; fi
if ! curl -s "http://127.0.0.1:$PORT/state" >/dev/null 2>&1; then
  nohup python3 server.py $MODE > /tmp/martin-visualizer.log 2>&1 &
  sleep 1
fi
URL="http://127.0.0.1:$PORT/"
if [ -d "/Applications/Google Chrome.app" ]; then
  open -na "Google Chrome" --args --new-window --user-data-dir="/tmp/martin-viz-profile" "$URL"
else
  open "$URL"
  echo "Opened your default browser. Press F11 (or Ctrl/Cmd+Shift+F) for fullscreen."
fi
echo "Visualizer at $URL  (log: /tmp/martin-visualizer.log)"
