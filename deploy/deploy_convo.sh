#!/usr/bin/env bash
# One-command enablement for MARTIN's conversational infrastructure.
# Run:  cd ~/Jarvis && bash deploy/deploy_convo.sh
# Idempotent + safe: it only ADDS; it never edits your existing app code.
set -euo pipefail
cd "$(dirname "$0")/.."   # ~/Jarvis

echo "==> 1/5  Python deps (pgvector client)"
if [ -x "venv/bin/pip" ]; then PIP="venv/bin/pip"; else PIP="pip3"; fi
"$PIP" install --quiet "psycopg[binary]>=3.1" pgvector >/dev/null 2>&1 || \
  echo "    (pip step skipped — install psycopg[binary] + pgvector manually if needed)"

echo "==> 2/5  Postgres + pgvector (Docker)"
if command -v docker >/dev/null 2>&1; then
  (cd deploy && docker compose up -d)
  echo "    waiting for Postgres to be healthy…"
  for i in $(seq 1 20); do
    if docker exec martin-convo-pg pg_isready -U martin -d martin_convo >/dev/null 2>&1; then
      echo "    Postgres ready."; break; fi; sleep 2; done
else
  echo "    Docker not found — skipping. (Install Docker Desktop to enable pgvector; the"
  echo "    engine falls back to JSON persistence until then.)"
fi

echo "==> 3/5  Enable DATABASE_URL in .env (if not present)"
grep -q "^DATABASE_URL=" .env 2>/dev/null || \
  echo "DATABASE_URL=postgresql://martin:martin_local_dev@localhost:5433/martin_convo" >> .env
echo "    .env has DATABASE_URL."

echo "==> 4/5  Self-check (engine acceptance suites)"
if [ -x "venv/bin/python3" ]; then PY="venv/bin/python3"; else PY="python3"; fi
for d in demo_interruptions demo_phase2 demo_memory demo_scopes demo_realtime demo_evals; do
  if "$PY" -m jarvis_convo.$d >/dev/null 2>&1; then echo "    PASS  $d"; else echo "    FAIL  $d"; fi
done

echo "==> 5/5  Restart the server to load the conversational engine + realtime WS"
./restart.sh || echo "    Run ./restart.sh manually if that didn't start it."

echo
echo "Done. Open https://localhost:8000/app (hard-refresh)."
echo "  • /api/convo/turn   — conversational engine (memory + tools + artifacts)"
echo "  • /ws/realtime      — realtime event transport (WebRTC-ready)"
echo "  • Durable memory is now on Postgres/pgvector if Docker started; JSON otherwise."
