"""Agent conversation memory — Postgres-backed, with a local SQLite fallback.

This is the "Memory" attachment for an AI Agent node in Studio (the n8n "Postgres
Chat Memory" pattern). Each agent turn can load the last N messages for a session
key and append the new exchange, so the agent remembers across runs.

Backends
--------
* Postgres (preferred): used when the node's Memory is backed by a Postgres
  credential (host/port/database/user/password) or a raw DSN. Table is created on
  first use. Requires the `psycopg` (v3) or `psycopg2` driver in your environment.
* SQLite fallback: if no Postgres is configured (or the driver isn't installed),
  memory is stored locally in agent_memory.sqlite so the feature still works for
  development and demos. `backend_kind()` reports which is active.

A "session key" scopes a conversation — e.g. the workflow id, a user id, or a
form-field value. Different keys keep separate memories.
"""
from __future__ import annotations

import os
import json
import time
import sqlite3
import logging

log = logging.getLogger("agent_memory")

_DIR = os.path.dirname(os.path.abspath(__file__))
_SQLITE_PATH = os.path.join(_DIR, "agent_memory.sqlite")
_TABLE = "agent_memory"


# ---------------------------------------------------------------------------
# DSN / config
# ---------------------------------------------------------------------------
def dsn_from_cred(data: dict) -> str:
    """Build a libpq DSN from a Postgres credential's fields, or use a raw dsn."""
    data = data or {}
    if (data.get("dsn") or "").strip():
        return data["dsn"].strip()
    host = data.get("host") or "localhost"
    port = data.get("port") or "5432"
    db = data.get("database") or data.get("dbname") or "postgres"
    user = data.get("user") or "postgres"
    pw = data.get("password") or ""
    parts = [f"host={host}", f"port={port}", f"dbname={db}", f"user={user}"]
    if pw:
        parts.append(f"password={pw}")
    return " ".join(parts)


def _pg_connect(dsn: str):
    """Return a live Postgres connection using whichever driver is installed, or
    None if neither driver is available."""
    if not dsn:
        return None
    try:
        import psycopg  # psycopg 3
        return psycopg.connect(dsn, autocommit=True)
    except Exception as e:
        log.info(f"psycopg(3) unavailable: {e}")
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception as e:
        log.info(f"psycopg2 unavailable: {e}")
    return None


def backend_kind(dsn: str) -> str:
    """Which backend a given DSN would actually use right now."""
    if dsn:
        c = _pg_connect(dsn)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            return "postgres"
    return "sqlite"


# ---------------------------------------------------------------------------
# SQLite backend (fallback)
# ---------------------------------------------------------------------------
def _sqlite():
    try:
        conn = sqlite3.connect(_SQLITE_PATH)
        conn.execute("SELECT 1")
    except Exception:
        import tempfile
        conn = sqlite3.connect(os.path.join(tempfile.gettempdir(), "agent_memory.sqlite"))
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ts REAL NOT NULL
    )""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_sk ON {_TABLE}(session_key, ts)")
    return conn


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------
def _pg_ensure(conn):
    with conn.cursor() as cur:
        cur.execute(f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            session_key TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts DOUBLE PRECISION NOT NULL
        )""")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_sk ON {_TABLE}(session_key, ts)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load(dsn: str, session_key: str, window: int = 10) -> list:
    """Return the last `window` messages for the session as [{role, content}]."""
    session_key = session_key or "default"
    conn = _pg_connect(dsn) if dsn else None
    if conn is not None:
        try:
            _pg_ensure(conn)
            with conn.cursor() as cur:
                cur.execute(f"SELECT role, content FROM {_TABLE} WHERE session_key=%s "
                            f"ORDER BY ts DESC LIMIT %s", (session_key, int(window)))
                rows = cur.fetchall()
            return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
        except Exception as e:
            log.warning(f"pg load failed, falling back: {e}")
        finally:
            try: conn.close()
            except Exception: pass
    conn = _sqlite()
    try:
        rows = conn.execute(f"SELECT role, content FROM {_TABLE} WHERE session_key=? "
                            f"ORDER BY ts DESC LIMIT ?", (session_key, int(window))).fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    finally:
        conn.close()


def append(dsn: str, session_key: str, role: str, content: str) -> None:
    """Append one message to the session's memory."""
    session_key = session_key or "default"
    content = content or ""
    now = time.time()
    conn = _pg_connect(dsn) if dsn else None
    if conn is not None:
        try:
            _pg_ensure(conn)
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO {_TABLE}(session_key, role, content, ts) VALUES (%s,%s,%s,%s)",
                            (session_key, role, content, now))
            return
        except Exception as e:
            log.warning(f"pg append failed, falling back: {e}")
        finally:
            try: conn.close()
            except Exception: pass
    conn = _sqlite()
    try:
        conn.execute(f"INSERT INTO {_TABLE}(session_key, role, content, ts) VALUES (?,?,?,?)",
                     (session_key, role, content, now))
        conn.commit()
    finally:
        conn.close()


def clear(dsn: str, session_key: str) -> int:
    """Delete a session's memory. Returns rows removed (best-effort)."""
    session_key = session_key or "default"
    conn = _pg_connect(dsn) if dsn else None
    if conn is not None:
        try:
            _pg_ensure(conn)
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {_TABLE} WHERE session_key=%s", (session_key,))
                return cur.rowcount or 0
        except Exception as e:
            log.warning(f"pg clear failed, falling back: {e}")
        finally:
            try: conn.close()
            except Exception: pass
    conn = _sqlite()
    try:
        cur = conn.execute(f"DELETE FROM {_TABLE} WHERE session_key=?", (session_key,))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def test_connection(dsn: str) -> dict:
    """Probe the backend for the Memory 'Test' button in the inspector."""
    if dsn:
        conn = _pg_connect(dsn)
        if conn is not None:
            try:
                _pg_ensure(conn)
                return {"ok": True, "backend": "postgres", "detail": "Connected to Postgres; memory table ready."}
            except Exception as e:
                return {"ok": False, "backend": "postgres", "detail": f"Connected but schema failed: {e}"}
            finally:
                try: conn.close()
                except Exception: pass
        return {"ok": False, "backend": "sqlite",
                "detail": "Could not reach Postgres (driver missing or connection refused) — "
                          "using local SQLite fallback. Install psycopg and check the credential."}
    return {"ok": True, "backend": "sqlite", "detail": "No Postgres credential set — using local SQLite memory."}
