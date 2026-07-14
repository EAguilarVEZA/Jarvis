"""
jarvis_engine
─────────────
Selects which engine compiles/executes analytics queries for Jarvis.

    JARVIS_SQL_ENGINE = "bigquery"  (default)  |  "ktx"

"bigquery"  Jarvis's built-in semantic layer compiles SQL (semantic.build_sql)
            and executes it directly on BigQuery (semantic.executor.run_query).
            This is the legacy behaviour — nothing changes unless you opt in.

"ktx"       Execution is routed through the ktx context layer (parser-validated,
            read-only) via ktx_client. Compilation still uses the built-in
            semantic layer until ktx measures are seeded (Phase 2).

This is a feature flag so making ktx canonical is reversible and can be
A/B compared on real reports. To try ktx for a single process:

    JARVIS_SQL_ENGINE=ktx python server.py

To make it the default later, set it in .env.
"""
from __future__ import annotations

import os

_VALID = {"bigquery", "ktx"}


def engine() -> str:
    """Return the active engine name, normalised. Unknown values fall back to 'bigquery'."""
    val = os.environ.get("JARVIS_SQL_ENGINE", "bigquery").strip().lower()
    return val if val in _VALID else "bigquery"


def kaelio_lockdown() -> bool:
    """
    Hard kill-switch for *anything* that could touch Kaelio's infrastructure
    (KTX subprocess, telemetry, MCP daemon). When set, use_ktx() is forced
    False regardless of JARVIS_SQL_ENGINE, and ktx_client / ktx_sync refuse
    to spawn the binary.

    Enable via:
        export KAELIO_LOCKDOWN=1
    in the server's environment (systemd EnvironmentFile, Docker, etc.).

    The audit module kaelio_audit.py also runs on server startup and refuses
    to boot if Kaelio domains resolve to non-loopback addresses while this
    flag is set.
    """
    return os.environ.get("KAELIO_LOCKDOWN", "").strip() in ("1", "true", "yes", "on")


def use_ktx() -> bool:
    """True when execution should be routed through the ktx context layer.
    Always False when KAELIO_LOCKDOWN is set — no exceptions, no overrides."""
    if kaelio_lockdown():
        return False
    return engine() == "ktx"
