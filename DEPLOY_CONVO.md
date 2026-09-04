# Deploying the Conversational Engine

The engine (`jarvis_convo/`) + endpoints (`convo_api.py`, `realtime_api.py`) are already wired
into the app and work with **zero extra infrastructure** — durable-semantic memory persists to
JSON, vectors run in-process, and the pill has live barge-in. This guide turns on the production
infrastructure when you want scale.

## One command
```bash
cd ~/Jarvis && bash deploy/deploy_convo.sh
```
It installs the pgvector client, starts Postgres+pgvector in Docker (if Docker is present),
adds `DATABASE_URL` to `.env`, runs the engine self-check, and restarts the server. Safe and
idempotent — it only adds; it never edits your app code.

## What activates what
| Capability | Works today | To scale it |
|---|---|---|
| Conversational engine (memory + tools + artifacts) | ✅ `/api/convo/turn` | — |
| Durable memory across restarts | ✅ JSON in `convo_state/` | set `DATABASE_URL` → pgvector mirror |
| Semantic retrieval | ✅ in-process (hashed embeddings) | swap `EmbeddingClient` for a real model |
| Barge-in | ✅ pill browser-speech | `/ws/realtime` + WebRTC/STT for <150ms |
| Connector writes | ✅ scoped + confirmed | run OAuth to populate real scopes |
| Audit trail | ✅ JSONL `convo_state/audit.jsonl` | `convo_audit` table (pgvector schema) |

## Manual steps (if you skip the script)
1. `cd ~/Jarvis/deploy && docker compose up -d`   # Postgres + pgvector on host port 5433
2. Add to `~/Jarvis/.env`:
   `DATABASE_URL=postgresql://martin:martin_local_dev@localhost:5433/martin_convo`
3. `venv/bin/pip install "psycopg[binary]" pgvector`
4. `./restart.sh` and hard-refresh `https://localhost:8000/app`

## Self-check (any time)
```bash
for d in demo_interruptions demo_phase2 demo_memory demo_scopes demo_realtime demo_evals; do
  python3 -m jarvis_convo.$d; done
```
All six should print PASS; `demo_evals` prints the §18 quality scorecard (target 1.0).

## What remains (needs credentials, not code)
- **Real embeddings**: implement `EmbeddingClient.embed()` against a local/remote embedder.
- **<150ms voice**: a TURN/STUN server + a streaming-ASR provider feeding `/ws/realtime`
  (adapter documented at the bottom of `realtime_api.py`).
- **Live connector scopes**: OAuth flows to grant `gmail:write` etc. into the `ScopeStore`.
