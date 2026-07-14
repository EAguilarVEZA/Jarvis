"""
kaelio_audit
────────────
Startup-time check that verifies the Jarvis server is *not* able to reach any
Kaelio / KTX-telemetry endpoint when KAELIO_LOCKDOWN=1.

This is defense-in-depth on top of:
  • KTX_TELEMETRY_DISABLED / DO_NOT_TRACK env vars
  • ~/.ktx/telemetry.json {"enabled": false}
  • jarvis_engine.kaelio_lockdown() forcing use_ktx() False
  • ktx_client / ktx_sync refusing to spawn the binary when locked down

The audit runs DNS resolution against a small set of domains. If any of them
resolves to a non-loopback address while lockdown is requested, we refuse to
start the server — that means /etc/hosts blackholing failed and the server
could phone home if some code path slipped past the other guards.

Wire from server.py startup:

    from kaelio_audit import audit_or_die
    audit_or_die()
"""
from __future__ import annotations
import logging
import os
import socket
import sys

log = logging.getLogger("kaelio_audit")

# Domains that MUST be blackholed when lockdown is on. Kaelio's own infra plus
# the PostHog endpoints KTX uses for telemetry.
SENTINEL_DOMAINS = (
    "kaelio.com",
    "www.kaelio.com",
    "oh-next.kaelio.com",
    "docs.kaelio.com",
    "app.posthog.com",
    "us.posthog.com",
    "eu.posthog.com",
    "i.posthog.com",
)

# Addresses that count as "blackholed" — loopback or IETF-reserved.
_LOOPBACK_PREFIXES = ("127.", "0.0.0.0", "::1", "::")


def _is_blackholed(ip: str) -> bool:
    return any(ip == "0.0.0.0" or ip == "::" or ip.startswith(p) for p in _LOOPBACK_PREFIXES)


def _resolve(domain: str, timeout: float = 1.5) -> list[str]:
    """Return all IPs `domain` resolves to, or [] on failure / timeout."""
    socket.setdefaulttimeout(timeout)
    try:
        info = socket.getaddrinfo(domain, None)
        return list({ai[4][0] for ai in info})
    except (socket.gaierror, socket.herror, OSError):
        return []
    finally:
        socket.setdefaulttimeout(None)


def audit() -> dict:
    """Run the audit. Returns a structured report — does NOT exit the process."""
    locked = os.environ.get("KAELIO_LOCKDOWN", "").strip() in ("1", "true", "yes", "on")
    rows: list[dict] = []
    breaches: list[dict] = []
    for d in SENTINEL_DOMAINS:
        ips = _resolve(d)
        row = {"domain": d, "ips": ips, "blackholed": True}
        if not ips:
            row["status"] = "unresolved"          # DNS blocked at network layer — best
        elif all(_is_blackholed(ip) for ip in ips):
            row["status"] = "blackholed"           # /etc/hosts entry returns 127.0.0.1
        else:
            row["status"] = "REACHABLE"            # could phone home
            row["blackholed"] = False
            if locked:
                breaches.append(row)
        rows.append(row)
    return {"lockdown_requested": locked, "rows": rows, "breaches": breaches}


def audit_or_die():
    """Run the audit and call sys.exit(1) if lockdown is requested and any
    sentinel domain is still reachable. Logs a summary either way."""
    report = audit()
    if not report["lockdown_requested"]:
        log.info("kaelio_audit: KAELIO_LOCKDOWN not set — skipping enforcement. "
                 "Set KAELIO_LOCKDOWN=1 in production to enforce.")
        return report

    if report["breaches"]:
        log.error("kaelio_audit: ❌ LOCKDOWN BREACH — these Kaelio/PostHog domains "
                  "are still reachable from this host:")
        for b in report["breaches"]:
            log.error("  %s → %s", b["domain"], ", ".join(b["ips"]))
        log.error("kaelio_audit: refusing to start. Run "
                  "scripts/server_kaelio_lockdown.sh to add /etc/hosts blackholes "
                  "and firewall rules, then retry.")
        sys.exit(1)

    log.info("kaelio_audit: ✓ lockdown enforced — %d sentinel domains all "
             "unresolved or blackholed.", len(report["rows"]))
    return report


if __name__ == "__main__":
    # CLI usage: `python kaelio_audit.py` prints the report; exits non-zero if a breach.
    import json as _j
    r = audit()
    print(_j.dumps(r, indent=2))
    if r["lockdown_requested"] and r["breaches"]:
        sys.exit(1)
