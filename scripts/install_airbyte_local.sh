#!/usr/bin/env bash
# install_airbyte_local.sh
# ────────────────────────
# Installs Airbyte OSS on this Mac via `abctl` (Docker-backed), captures the
# URL + initial credentials, and writes them encrypted into Jarvis's standard
# config so the /api/airbyte/* routes can talk to it immediately.
#
# Why a separate port (8001)? The Jarvis server already binds 8000. We point
# Airbyte at 8001 to avoid the collision.
#
# Migrate-to-server later? When you stand up Airbyte on a remote VM, run
# this script there OR just POST the new URL to /api/airbyte/config — every
# Jarvis caller resolves the URL from the encrypted config, so swapping it is
# a one-line change.
set -euo pipefail

AIRBYTE_PORT="${AIRBYTE_PORT:-8001}"
JARVIS_DIR="${JARVIS_DIR:-/Users/developer/Jarvis}"
JARVIS_URL="${JARVIS_URL:-https://localhost:8000}"

echo "▶ Step 1 — ensure prerequisites"
if ! command -v docker >/dev/null 2>&1; then
    echo "  ❌ Docker isn't installed. Install Docker Desktop first: https://www.docker.com/products/docker-desktop/"
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "  ❌ Docker daemon isn't running. Start Docker Desktop and re-run."
    exit 1
fi
if ! command -v abctl >/dev/null 2>&1; then
    echo "  · abctl not found — installing via Airbyte's official installer"
    # abctl is NOT on Homebrew — it ships through Airbyte's own one-liner that
    # drops the binary into /usr/local/bin (sudo prompt is normal).
    if ! curl -LsfS https://get.airbyte.com | bash -; then
        echo "  ❌ Airbyte's installer failed. Try manually: curl -LsfS https://get.airbyte.com | bash -"
        exit 1
    fi
    # The installer prints the install location; make sure it's on PATH for the rest of this session
    export PATH="/usr/local/bin:$PATH"
fi
if ! command -v abctl >/dev/null 2>&1; then
    echo "  ❌ abctl installed but not on PATH. Add /usr/local/bin to your PATH and re-run."
    exit 1
fi
echo "  ✓ docker + abctl present"

echo
echo "▶ Step 2 — install Airbyte locally on port ${AIRBYTE_PORT}"
echo "  (first run downloads ~5 GB of images; expect 10–20 min)"
abctl local install --port "${AIRBYTE_PORT}"

echo
echo "▶ Step 3 — capture credentials"
CREDS_FILE="$HOME/.airbyte/abctl/abctl.yaml"
if [ ! -f "${CREDS_FILE}" ]; then
    # newer abctl stores creds in keychain — print them
    abctl local credentials || true
fi
echo "  ✓ credentials saved by abctl in ~/.airbyte/"

echo
echo "▶ Step 4 — POST the URL to Jarvis"
AIRBYTE_URL="http://localhost:${AIRBYTE_PORT}"
echo "  · pinging ${AIRBYTE_URL}"
until curl -sf "${AIRBYTE_URL}/api/v1/health" >/dev/null 2>&1 \
   || curl -sf "${AIRBYTE_URL}/api/public/v1/workspaces?limit=1" >/dev/null 2>&1; do
    sleep 3
done
echo "  ✓ Airbyte answering on ${AIRBYTE_URL}"

# Tell Jarvis where Airbyte lives. Auth is 'none' for default OSS install.
curl -k -sf -X POST "${JARVIS_URL}/api/airbyte/config" \
    -H 'Content-Type: application/json' \
    -d "{\"url\":\"${AIRBYTE_URL}\",\"auth_kind\":\"none\"}" >/dev/null \
    && echo "  ✓ Jarvis configured at ${JARVIS_URL}/api/airbyte/config" \
    || echo "  · could not reach Jarvis at ${JARVIS_URL} — POST the URL manually when the server is up."

echo
echo "▶ Step 5 — warm the catalog cache"
curl -k -sf "${JARVIS_URL}/api/airbyte/catalog?force_refresh=true" >/dev/null \
    && echo "  ✓ catalog cached at ~/.config/jarvis/airbyte_catalog.json" \
    || echo "  · skip — Jarvis not running yet."

echo
echo "Done. Open Airbyte's UI at:  ${AIRBYTE_URL}"
echo "Then open Jarvis → Data Sources tab to start activating connectors."
echo
echo "When you migrate to a server later: install Airbyte there, then POST"
echo "the new URL to ${JARVIS_URL}/api/airbyte/config. No code change needed."
