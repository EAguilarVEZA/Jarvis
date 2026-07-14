#!/usr/bin/env bash
# server_kaelio_lockdown.sh
# ─────────────────────────
# Run this ONCE on the Jarvis production server to make Kaelio + PostHog
# unreachable at the OS level, regardless of any code path inside Jarvis or
# KTX.
#
# What it does:
#   1. Adds blackhole entries to /etc/hosts (127.0.0.1 for every Kaelio /
#      PostHog domain we know about). This is enough to stop the daemon from
#      sending telemetry even if env vars are forgotten.
#   2. If UFW (Linux) is installed and enabled, adds explicit outbound deny
#      rules for the same domains.
#   3. Sets KAELIO_LOCKDOWN=1 + KTX_TELEMETRY_DISABLED=1 + DO_NOT_TRACK=1 in
#      /etc/environment (system-wide) and /etc/systemd/system/jarvis.service.d/
#      (if Jarvis is running under systemd).
#   4. Runs the Python audit module to verify the lockdown took effect.
#
# Idempotent — safe to re-run. Requires sudo.
#
# Linux server only (Debian/Ubuntu/RHEL). For your local Mac dev box, the
# /etc/hosts edit works the same way but the firewall step is skipped.
set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "This script must run as root (sudo)." >&2
    exit 1
fi

DOMAINS=(
    "kaelio.com" "www.kaelio.com" "oh-next.kaelio.com" "docs.kaelio.com"
    "app.posthog.com" "us.posthog.com" "eu.posthog.com" "i.posthog.com"
)

# ─── Step 1: /etc/hosts blackholes ─────────────────────────────────
echo "▶ Step 1 — appending blackholes to /etc/hosts"
MARKER="# JARVIS-KAELIO-LOCKDOWN — managed by server_kaelio_lockdown.sh, do not edit by hand"
if grep -qF "${MARKER}" /etc/hosts; then
    echo "  · already present — leaving as-is"
else
    {
        echo ""
        echo "${MARKER}"
        for d in "${DOMAINS[@]}"; do
            printf "127.0.0.1 %s\n" "${d}"
            printf "::1       %s\n" "${d}"
        done
    } >> /etc/hosts
    echo "  ✓ added ${#DOMAINS[@]} blackholes"
fi

# ─── Step 2: outbound deny rules (UFW if present) ─────────────────
echo
echo "▶ Step 2 — firewall rules"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    for d in "${DOMAINS[@]}"; do
        # ufw needs an IP, not a hostname. Resolve via getent first (will hit
        # the new /etc/hosts entry and return 127.0.0.1 — so we use the
        # public-DNS dig output instead).
        ips=$(dig +short "${d}" @1.1.1.1 2>/dev/null | grep -E '^[0-9]+\.' || true)
        for ip in ${ips}; do
            if ufw status numbered | grep -q "${ip}"; then
                echo "  · ufw already denies ${ip} (${d}) — skipping"
            else
                ufw deny out to "${ip}" comment "kaelio-lockdown:${d}" >/dev/null
                echo "  ✓ ufw deny out to ${ip} (${d})"
            fi
        done
    done
else
    echo "  · ufw not active — relying on /etc/hosts blackholes only."
    echo "    (If you're on iptables/nftables, the audit step below will still verify reachability.)"
fi

# ─── Step 3: env vars persisted system-wide ────────────────────────
echo
echo "▶ Step 3 — persisting env vars"
for kv in 'KAELIO_LOCKDOWN=1' 'KTX_TELEMETRY_DISABLED=1' 'DO_NOT_TRACK=1'; do
    key="${kv%%=*}"
    if grep -qE "^${key}=" /etc/environment; then
        echo "  · ${key} already in /etc/environment — leaving as-is"
    else
        echo "${kv}" >> /etc/environment
        echo "  ✓ added ${kv} to /etc/environment"
    fi
done

# systemd drop-in (only if jarvis.service exists)
if systemctl list-unit-files 2>/dev/null | grep -q '^jarvis\.service'; then
    DROPIN_DIR="/etc/systemd/system/jarvis.service.d"
    mkdir -p "${DROPIN_DIR}"
    cat > "${DROPIN_DIR}/kaelio-lockdown.conf" <<'EOF'
[Service]
Environment="KAELIO_LOCKDOWN=1"
Environment="KTX_TELEMETRY_DISABLED=1"
Environment="DO_NOT_TRACK=1"
EOF
    systemctl daemon-reload
    echo "  ✓ systemd drop-in written; jarvis.service will inherit the vars on next start"
fi

# ─── Step 4: verify ─────────────────────────────────────────────────
echo
echo "▶ Step 4 — verifying with kaelio_audit.py"
cd "$(dirname "$0")/.."   # /Users/developer/Jarvis
if command -v python3 >/dev/null 2>&1; then
    KAELIO_LOCKDOWN=1 python3 kaelio_audit.py | tail -30 || {
        echo
        echo "❌ Audit failed — see breaches above. Lockdown is incomplete."
        echo "   Common causes: DNS cache (try 'systemd-resolve --flush-caches' or reboot),"
        echo "   or another /etc/hosts override file (e.g. /etc/cloud/templates)."
        exit 1
    }
fi

echo
echo "✓ Lockdown applied successfully."
echo "  After the next deploy/restart, the Jarvis server will refuse to boot"
echo "  if any Kaelio/PostHog domain becomes reachable again."
