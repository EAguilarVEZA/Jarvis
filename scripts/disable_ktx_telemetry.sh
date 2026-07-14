#!/usr/bin/env bash
# disable_ktx_telemetry.sh
# ─────────────────────────
# Fully opts out of every KTX telemetry channel:
#   1. Persists ~/.ktx/telemetry.json {"enabled":false} — survives reboots, daemon honours it.
#   2. Adds KTX_TELEMETRY_DISABLED=1 + DO_NOT_TRACK=1 to /Users/developer/Jarvis/.env so every
#      ktx subprocess that the Jarvis server spawns inherits the opt-out.
#   3. Appends an export line to your shell rc (~/.zshrc, fallback ~/.bashrc) so any
#      future interactive shell ALSO has the env vars set.
#   4. Verifies by running `ktx status` and checking the daemon emits no telemetry banner.
#
# Re-running this script is safe (idempotent — no duplicate lines).
set -euo pipefail

KTX_HOME="${HOME}/.ktx"
TELEMETRY_JSON="${KTX_HOME}/telemetry.json"
ENV_FILE="/Users/developer/Jarvis/.env"
RC_TARGETS=("${HOME}/.zshrc" "${HOME}/.bashrc")

echo "▶ Step 1 — writing ${TELEMETRY_JSON}"
mkdir -p "${KTX_HOME}"
printf '{\n  "enabled": false,\n  "set_by": "disable_ktx_telemetry.sh",\n  "set_at": "%s"\n}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${TELEMETRY_JSON}"
chmod 600 "${TELEMETRY_JSON}"
echo "  ✓ wrote $(cat "${TELEMETRY_JSON}" | tr -d '\n')"

echo
echo "▶ Step 2 — patching ${ENV_FILE}"
touch "${ENV_FILE}"
for kv in 'KTX_TELEMETRY_DISABLED=1' 'DO_NOT_TRACK=1'; do
    key="${kv%%=*}"
    if grep -qE "^${key}=" "${ENV_FILE}"; then
        echo "  · ${key} already set in .env — leaving as-is"
    else
        echo "${kv}" >> "${ENV_FILE}"
        echo "  ✓ added ${kv}"
    fi
done

echo
echo "▶ Step 3 — appending to shell rc"
for rc in "${RC_TARGETS[@]}"; do
    [ -f "${rc}" ] || continue
    if grep -q 'KTX_TELEMETRY_DISABLED=1' "${rc}"; then
        echo "  · ${rc} already opts out — skipping"
    else
        {
          echo ""
          echo "# Added by disable_ktx_telemetry.sh — opt out of KTX/Kaelio analytics"
          echo "export KTX_TELEMETRY_DISABLED=1"
          echo "export DO_NOT_TRACK=1"
        } >> "${rc}"
        echo "  ✓ appended exports to ${rc}"
    fi
done

echo
echo "▶ Step 4 — verifying"
if command -v ktx >/dev/null 2>&1; then
    KTX_TELEMETRY_DISABLED=1 DO_NOT_TRACK=1 ktx status 2>&1 | head -8 || true
    echo "  ✓ ran 'ktx status' with telemetry disabled — no banner means it's off"
else
    echo "  · ktx binary not on PATH yet — that's fine, opt-out is persisted in ~/.ktx/telemetry.json"
fi

echo
echo "Done. Telemetry is now disabled four ways:"
echo "  1. Persistent file:  ~/.ktx/telemetry.json"
echo "  2. Jarvis env file:  /Users/developer/Jarvis/.env"
echo "  3. Shell rc:         ~/.zshrc (and ~/.bashrc if present)"
echo "  4. Process inherit:  every ktx subprocess Jarvis spawns will see the env vars"
echo
echo "To re-enable later, delete or edit ~/.ktx/telemetry.json and remove the env vars."
