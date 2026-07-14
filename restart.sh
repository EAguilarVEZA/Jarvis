#!/bin/bash
# Restart the JARVIS martin_app backend.
# The frontend (martin_app.html) probes ONLY port 8000, so we MUST bind there.
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "🔴 Stopping any process on :8000…"
lsof -ti :8000 | xargs kill -9 2>/dev/null || true
# Also clear the stale pid file
rm -f .pids/backend.pid 2>/dev/null || true
sleep 1

echo "🟡 Starting JARVIS server on https://localhost:8000…"
# Use the venv if it exists, otherwise system python
if [ -d "venv" ]; then
  source venv/bin/activate
fi

mkdir -p logs .pids
# Run detached, capture output, record pid
nohup python server.py --port 8000 > logs/backend.log 2>&1 &
echo $! > .pids/backend.pid
SRV_PID=$!
echo "   PID $SRV_PID"

# Wait up to 12s for the health endpoint to answer
echo "⏳ Waiting for /api/health…"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 1
  if curl -sk --max-time 1 https://localhost:8000/api/health >/dev/null 2>&1; then
    echo "✅ Backend is up on https://localhost:8000"
    curl -sk https://localhost:8000/api/health | python3 -m json.tool || true
    echo ""
    echo "🟢 Open martin_app.html in your browser. The orange 'API Connecting…' pill should go green."
    exit 0
  fi
done

echo "❌ Backend did not respond on /api/health within 12s. Last log lines:"
tail -30 logs/backend.log
exit 1
