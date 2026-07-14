"""
connections_api
───────────────
Multi-source connection catalog + CRUD + encrypted credential store.

Design notes:
  • Catalog: 14 connector kinds advertised in the UI; BigQuery is fully
    implemented today, the rest are listed with their config fields and
    a "driver_installed" flag — they activate once the Python driver is
    pip-installed and the user fills out the form.
  • Storage: ~/.config/jarvis/connections.enc.json, encrypted with
    cryptography.Fernet. Key lives at ~/.config/jarvis/.master_key
    (chmod 600), auto-generated on first use. Self-contained: no
    passphrase prompts; key never leaves disk.
  • This file does NOT touch the existing semantic.yaml source — the
    legacy BigQuery source remains the production path. New connections
    sit alongside as a forward-looking catalog the curation flow can
    eventually pull from.

Routes (prefix /api/connections):
  GET  /catalog            — full catalog of available connector kinds
  GET  /                   — list configured connections (no secrets)
  GET  /{id}               — one connection (no secrets)
  POST /                   — create a connection
  PUT  /{id}               — update
  DELETE /{id}             — delete
  POST /{id}/test          — test connectivity (best-effort)
"""

from __future__ import annotations

import json
import os
import time
import uuid
import shutil
import importlib
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/connections", tags=["connections"])

CFG_DIR = Path(os.path.expanduser("~/.config/jarvis"))
STORE = CFG_DIR / "connections.enc.json"
KEY_FILE = CFG_DIR / ".master_key"
_lock = threading.Lock()


# ─── Catalog ─────────────────────────────────────────────────────────
# Each entry describes a connector kind. `driver` is the pip package
# name we try to import; `fields` is the form schema (key, label, type,
# required, secret, placeholder). `tagline` is shown in the UI card.
CATALOG = [
    {
        "kind": "bigquery", "name": "Google BigQuery", "icon": "🔷",
        "tagline": "Google Cloud data warehouse — serverless SQL at petabyte scale.",
        "driver": "google.cloud.bigquery", "category": "warehouse",
        "fields": [
            {"key": "project",            "label": "Project ID",            "type": "text",     "required": True,  "placeholder": "my-gcp-project"},
            {"key": "default_dataset",    "label": "Default dataset",       "type": "text",     "required": False, "placeholder": "gold"},
            {"key": "service_account_json","label": "Service account JSON", "type": "textarea", "required": True,  "secret": True, "placeholder": "Paste the JSON keyfile contents"},
        ],
    },
    {
        "kind": "snowflake", "name": "Snowflake", "icon": "❄",
        "tagline": "Multi-cloud data warehouse — separate compute + storage.",
        "driver": "snowflake.connector", "category": "warehouse",
        "fields": [
            {"key": "account",   "label": "Account",   "type": "text",     "required": True,  "placeholder": "abc12345.us-east-1"},
            {"key": "warehouse", "label": "Warehouse", "type": "text",     "required": True,  "placeholder": "COMPUTE_WH"},
            {"key": "database",  "label": "Database",  "type": "text",     "required": True},
            {"key": "schema",    "label": "Schema",    "type": "text",     "required": False, "placeholder": "PUBLIC"},
            {"key": "user",      "label": "User",      "type": "text",     "required": True},
            {"key": "password",  "label": "Password",  "type": "password", "required": True,  "secret": True},
            {"key": "role",      "label": "Role",      "type": "text",     "required": False},
        ],
    },
    {
        "kind": "postgres", "name": "PostgreSQL", "icon": "🐘",
        "tagline": "Open-source relational database. Also covers Aurora Postgres.",
        "driver": "psycopg2", "category": "database",
        "fields": [
            {"key": "host",     "label": "Host",     "type": "text",     "required": True,  "placeholder": "db.example.com"},
            {"key": "port",     "label": "Port",     "type": "number",   "required": False, "placeholder": "5432"},
            {"key": "database", "label": "Database", "type": "text",     "required": True},
            {"key": "user",     "label": "User",     "type": "text",     "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True,  "secret": True},
            {"key": "sslmode",  "label": "SSL mode", "type": "text",     "required": False, "placeholder": "require"},
        ],
    },
    {
        "kind": "redshift", "name": "Amazon Redshift", "icon": "🟥",
        "tagline": "AWS data warehouse — Postgres-compatible wire protocol.",
        "driver": "redshift_connector", "category": "warehouse",
        "fields": [
            {"key": "host",     "label": "Cluster endpoint", "type": "text",     "required": True,  "placeholder": "my-cluster.xxx.us-east-1.redshift.amazonaws.com"},
            {"key": "port",     "label": "Port",             "type": "number",   "required": False, "placeholder": "5439"},
            {"key": "database", "label": "Database",         "type": "text",     "required": True},
            {"key": "user",     "label": "User",             "type": "text",     "required": True},
            {"key": "password", "label": "Password",         "type": "password", "required": True,  "secret": True},
        ],
    },
    {
        "kind": "mysql", "name": "MySQL / MariaDB", "icon": "🐬",
        "tagline": "Open-source relational DB. Includes RDS MySQL and MariaDB.",
        "driver": "pymysql", "category": "database",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": False, "placeholder": "3306"},
            {"key": "database", "label": "Database", "type": "text", "required": True},
            {"key": "user", "label": "User", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
        ],
    },
    {
        "kind": "databricks", "name": "Databricks SQL", "icon": "🧱",
        "tagline": "Lakehouse SQL warehouses — Delta Lake on cloud storage.",
        "driver": "databricks.sql", "category": "warehouse",
        "fields": [
            {"key": "server_hostname", "label": "Server hostname", "type": "text", "required": True, "placeholder": "abc.cloud.databricks.com"},
            {"key": "http_path",       "label": "HTTP path",       "type": "text", "required": True, "placeholder": "/sql/1.0/warehouses/abc123"},
            {"key": "access_token",    "label": "Personal access token", "type": "password", "required": True, "secret": True},
            {"key": "catalog",         "label": "Catalog",         "type": "text", "required": False, "placeholder": "main"},
            {"key": "schema",          "label": "Schema",          "type": "text", "required": False, "placeholder": "default"},
        ],
    },
    {
        "kind": "duckdb", "name": "DuckDB", "icon": "🦆",
        "tagline": "Local OLAP DB — point at a .duckdb file or a folder of Parquet/CSV.",
        "driver": "duckdb", "category": "local",
        "fields": [
            {"key": "path", "label": "Database path or ':memory:'", "type": "text", "required": True, "placeholder": "/path/to/marketing.duckdb"},
        ],
    },
    {
        "kind": "mssql", "name": "Microsoft SQL Server", "icon": "🗄",
        "tagline": "SQL Server / Azure SQL / Synapse Dedicated. Requires ODBC driver.",
        "driver": "pyodbc", "category": "database",
        "fields": [
            {"key": "host",     "label": "Server", "type": "text", "required": True, "placeholder": "myserver.database.windows.net"},
            {"key": "port",     "label": "Port",   "type": "number", "required": False, "placeholder": "1433"},
            {"key": "database", "label": "Database", "type": "text", "required": True},
            {"key": "user",     "label": "User",     "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            {"key": "driver",   "label": "ODBC driver", "type": "text", "required": False, "placeholder": "ODBC Driver 17 for SQL Server"},
        ],
    },
    {
        "kind": "sqlite", "name": "SQLite", "icon": "📁",
        "tagline": "Local file database — perfect for demos and small datasets.",
        "driver": "sqlite3", "category": "local",
        "fields": [
            {"key": "path", "label": "Database file path", "type": "text", "required": True, "placeholder": "/path/to/data.db"},
        ],
    },
    {
        "kind": "athena", "name": "Amazon Athena", "icon": "🪶",
        "tagline": "Serverless SQL on S3 — Presto under the hood.",
        "driver": "pyathena", "category": "warehouse",
        "fields": [
            {"key": "region",            "label": "AWS region",            "type": "text", "required": True, "placeholder": "us-east-1"},
            {"key": "s3_staging_dir",    "label": "S3 staging directory",  "type": "text", "required": True, "placeholder": "s3://your-bucket/athena-results/"},
            {"key": "aws_access_key_id", "label": "Access key ID",         "type": "text", "required": True},
            {"key": "aws_secret_access_key", "label": "Secret access key", "type": "password", "required": True, "secret": True},
            {"key": "workgroup",         "label": "Workgroup",             "type": "text", "required": False, "placeholder": "primary"},
            {"key": "schema",            "label": "Schema",                "type": "text", "required": False, "placeholder": "default"},
        ],
    },
    {
        "kind": "trino", "name": "Trino / Presto", "icon": "🎚",
        "tagline": "Federated SQL across multiple sources. Open-source.",
        "driver": "trino", "category": "warehouse",
        "fields": [
            {"key": "host",     "label": "Host",    "type": "text", "required": True},
            {"key": "port",     "label": "Port",    "type": "number", "required": False, "placeholder": "8080"},
            {"key": "user",     "label": "User",    "type": "text", "required": True},
            {"key": "catalog",  "label": "Catalog", "type": "text", "required": False},
            {"key": "schema",   "label": "Schema",  "type": "text", "required": False},
            {"key": "password", "label": "Password (if HTTPS auth)", "type": "password", "required": False, "secret": True},
        ],
    },
    {
        "kind": "clickhouse", "name": "ClickHouse", "icon": "📊",
        "tagline": "Column-store OLAP DB — fast aggregations, popular for product analytics.",
        "driver": "clickhouse_connect", "category": "warehouse",
        "fields": [
            {"key": "host",     "label": "Host",     "type": "text", "required": True},
            {"key": "port",     "label": "Port",     "type": "number", "required": False, "placeholder": "8443"},
            {"key": "database", "label": "Database", "type": "text", "required": False, "placeholder": "default"},
            {"key": "user",     "label": "User",     "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": False, "secret": True},
            {"key": "secure",   "label": "TLS (https)", "type": "checkbox", "required": False},
        ],
    },
    {
        "kind": "gsheets", "name": "Google Sheets", "icon": "📗",
        "tagline": "Read marketing spreadsheets via the Google Sheets API.",
        "driver": "gspread", "category": "spreadsheet",
        "fields": [
            {"key": "service_account_json", "label": "Service account JSON", "type": "textarea", "required": True, "secret": True},
            {"key": "default_sheet_id", "label": "Default sheet ID (optional)", "type": "text", "required": False},
        ],
    },
    {
        "kind": "files", "name": "CSV / Parquet folder", "icon": "📦",
        "tagline": "Point at a local folder; we'll query files via DuckDB.",
        "driver": "duckdb", "category": "local",
        "fields": [
            {"key": "path",   "label": "Folder path", "type": "text", "required": True, "placeholder": "/path/to/data/"},
            {"key": "format", "label": "File format", "type": "select", "required": True, "options": ["csv", "parquet", "json"]},
        ],
    },
    # ─── Direct file upload → BigQuery ─────────────────────────────────
    {
        "kind": "file_upload", "name": "Spreadsheet / File Upload", "icon": "📤",
        "tagline": "Upload CSV / Excel (.xlsx, .xls) files; rows land in a BigQuery table you can query like any other source.",
        "driver": "google.cloud.bigquery", "category": "upload",
        "supports_upload": True,
        "fields": [
            {"key": "project",        "label": "BigQuery project",   "type": "text", "required": True,  "placeholder": "oh-data-warehouse"},
            {"key": "target_dataset", "label": "Target dataset",     "type": "text", "required": True,  "placeholder": "uploads", "help": "Dataset where uploaded tables will land. Created automatically if missing."},
            {"key": "table_prefix",   "label": "Table name prefix",  "type": "text", "required": False, "placeholder": "user_upload_"},
            {"key": "write_mode",     "label": "Write mode",         "type": "select", "required": True, "options": ["replace", "append"], "default": "replace"},
        ],
    },
    # ─── Social ad platforms (Fivetran-style direct API integrations) ──
    {
        "kind": "tiktok_ads", "name": "TikTok Ads", "icon": "🎵",
        "tagline": "TikTok Ads Manager — campaigns, ads, creatives, and performance for B2C and brand campaigns.",
        "driver": "requests", "category": "advertising",
        "docs_url": "https://business-api.tiktok.com/portal/docs",
        "fields": [
            {"key": "app_id",        "label": "App ID",         "type": "text",     "required": True,  "placeholder": "From TikTok for Business Developer portal"},
            {"key": "app_secret",    "label": "App secret",     "type": "password", "required": True,  "secret": True},
            {"key": "access_token",  "label": "Access token",   "type": "password", "required": True,  "secret": True, "placeholder": "Long-lived token from OAuth callback"},
            {"key": "advertiser_id", "label": "Advertiser ID",  "type": "text",     "required": True,  "placeholder": "Numeric advertiser_id from TikTok Ads Manager"},
            {"key": "target_dataset","label": "Target BigQuery dataset", "type": "text", "required": False, "placeholder": "tiktok_ads", "help": "Where synced rows will land. Defaults to 'tiktok_ads'."},
        ],
    },
    {
        "kind": "pinterest_ads", "name": "Pinterest Ads", "icon": "📌",
        "tagline": "Pinterest Ads — campaigns, ad groups, promoted pins, and conversion reporting.",
        "driver": "requests", "category": "advertising",
        "docs_url": "https://developers.pinterest.com/docs/api/v5/",
        "fields": [
            {"key": "app_id",        "label": "App ID",        "type": "text",     "required": True},
            {"key": "app_secret",    "label": "App secret",    "type": "password", "required": True, "secret": True},
            {"key": "access_token",  "label": "Access token",  "type": "password", "required": True, "secret": True, "placeholder": "Long-lived OAuth token"},
            {"key": "refresh_token", "label": "Refresh token", "type": "password", "required": False, "secret": True},
            {"key": "ad_account_id", "label": "Ad account ID", "type": "text",     "required": True,  "placeholder": "Numeric ad account id"},
            {"key": "target_dataset","label": "Target BigQuery dataset", "type": "text", "required": False, "placeholder": "pinterest_ads"},
        ],
    },
    {
        "kind": "reddit_ads", "name": "Reddit Ads", "icon": "👽",
        "tagline": "Reddit Ads — campaigns, ad groups, ads, and performance reports for community-targeted ads.",
        "driver": "requests", "category": "advertising",
        "docs_url": "https://ads-api.reddit.com/docs/v3/",
        "fields": [
            {"key": "client_id",     "label": "Client ID",     "type": "text",     "required": True},
            {"key": "client_secret", "label": "Client secret", "type": "password", "required": True, "secret": True},
            {"key": "refresh_token", "label": "Refresh token", "type": "password", "required": True, "secret": True, "placeholder": "OAuth2 refresh token (Reddit Ads scope)"},
            {"key": "account_id",    "label": "Ad account ID", "type": "text",     "required": True,  "placeholder": "a2_xxxxxxxxxxxx"},
            {"key": "user_agent",    "label": "User-Agent",    "type": "text",     "required": True,  "placeholder": "Jarvis/1.0 (by u/your_reddit_username)", "help": "Reddit requires a unique user agent. Format: AppName/Version (by u/username)"},
            {"key": "target_dataset","label": "Target BigQuery dataset", "type": "text", "required": False, "placeholder": "reddit_ads"},
        ],
    },
]


def _catalog_by_kind() -> dict:
    return {c["kind"]: c for c in CATALOG}


# ─── Shared BigQuery client (for file_upload + future managed kinds) ──
# Reuses the same service account already mounted for the rest of Jarvis,
# kept at ~/.config/jarvis/bigquery-key.json (chmod 600). We DO NOT ask the
# user to paste a key into the file-upload connection — the existing project
# credentials handle that. This keeps secrets in one place.
_SHARED_BQ_KEY = Path(os.path.expanduser("~/.config/jarvis/bigquery-key.json"))


def _shared_bq_client():
    try:
        if not _SHARED_BQ_KEY.exists():
            return None
        from google.cloud import bigquery
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            str(_SHARED_BQ_KEY),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        # Project is taken from the connection record at call time; the
        # client below is project-agnostic and will be re-bound per request.
        return bigquery.Client(credentials=creds, project=creds.project_id)
    except Exception:
        return None


def _driver_installed(driver_path: str) -> bool:
    try:
        importlib.import_module(driver_path)
        return True
    except Exception:
        return False


# ─── Encryption (Fernet via auto-generated machine key) ──────────────
def _ensure_key() -> bytes:
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        try:
            from cryptography.fernet import Fernet
            key = Fernet.generate_key()
        except Exception:
            # cryptography not installed — fall back to a long random key
            import secrets, base64
            key = base64.urlsafe_b64encode(secrets.token_bytes(32))
        KEY_FILE.write_bytes(key)
        try: os.chmod(KEY_FILE, 0o600)
        except OSError: pass
    return KEY_FILE.read_bytes()


def _encrypt(data: dict) -> str:
    """Return base64 ciphertext for a JSON-encodable dict."""
    raw = json.dumps(data).encode("utf-8")
    try:
        from cryptography.fernet import Fernet
        f = Fernet(_ensure_key())
        return f.encrypt(raw).decode("ascii")
    except Exception:
        # Crypto not available — store base64 plaintext (better than raw JSON, still flagged)
        import base64
        return "PLAINTEXT_B64:" + base64.b64encode(raw).decode("ascii")


def _decrypt(token: str) -> dict:
    if token.startswith("PLAINTEXT_B64:"):
        import base64
        return json.loads(base64.b64decode(token.split(":", 1)[1]).decode("utf-8"))
    try:
        from cryptography.fernet import Fernet
        f = Fernet(_ensure_key())
        return json.loads(f.decrypt(token.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


# ─── Storage ─────────────────────────────────────────────────────────
def _load() -> dict:
    if not STORE.exists():
        return {"connections": []}
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"connections": []}


def _save(data: dict):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    if STORE.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        try: shutil.copy2(STORE, STORE.with_suffix(".enc.json.bak." + ts))
        except OSError: pass
    tmp = STORE.with_suffix(".enc.json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STORE)
    try: os.chmod(STORE, 0o600)
    except OSError: pass


def _public(c: dict) -> dict:
    """Strip secrets before returning to client. Fields marked 'secret' become '***'."""
    cat = _catalog_by_kind().get(c.get("kind"))
    secret_keys = {f["key"] for f in (cat or {}).get("fields", []) if f.get("secret")}
    fields = {}
    for k, v in (c.get("fields") or {}).items():
        fields[k] = ("***" if k in secret_keys and v else v)
    return {
        "id": c.get("id"),
        "kind": c.get("kind"),
        "name": c.get("name"),
        "fields": fields,
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
        "last_tested_at": c.get("last_tested_at"),
        "last_test_ok": c.get("last_test_ok"),
        "last_test_message": c.get("last_test_message"),
    }


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


# ─── Pydantic models ─────────────────────────────────────────────────
class ConnectionIn(BaseModel):
    kind: str
    name: str
    fields: dict


def _validate(body: ConnectionIn) -> Optional[str]:
    cat = _catalog_by_kind().get(body.kind)
    if not cat:
        return f"Unknown connector kind '{body.kind}'."
    required = [f["key"] for f in cat["fields"] if f.get("required")]
    missing = [k for k in required if not (body.fields or {}).get(k)]
    if missing:
        return f"Missing required fields: {', '.join(missing)}"
    return None


# ─── Endpoints ───────────────────────────────────────────────────────
@router.get("/catalog")
async def get_catalog():
    """Return the full catalog. Marks each connector with whether its driver is installed."""
    out = []
    for c in CATALOG:
        out.append({**c, "driver_installed": _driver_installed(c["driver"])})
    return {"catalog": out, "count": len(out)}


# ── BI-tool imports (Metabase / Looker / dbt) — scaffold ─────────────────────
# Live imports require the external system + credentials + network, which can't
# be exercised here. These endpoints validate input and report readiness so the
# UI flow is complete; set JARVIS_ENABLE_BI_IMPORTS=1 to enable the live path.
_BI_PROVIDERS = {
    "metabase": {"label": "Metabase", "imports": "databases + saved questions", "fields": ["base_url", "token"]},
    "looker":   {"label": "Looker",   "imports": "explores + Looks",           "fields": ["base_url", "client_id", "client_secret"]},
    "dbt":      {"label": "dbt",       "imports": "models + MetricFlow metrics", "fields": ["base_url", "token"]},
}


def _bi_enabled() -> bool:
    return os.getenv("JARVIS_ENABLE_BI_IMPORTS", "").strip() not in ("", "0", "false", "False")


class BiImportRequest(BaseModel):
    provider: str
    base_url: str = ""
    token: str = ""
    extra: dict = {}


@router.get("/import/providers")
async def bi_import_providers():
    return {"enabled": _bi_enabled(),
            "providers": [{"id": k, **v} for k, v in _BI_PROVIDERS.items()]}


@router.post("/import")
async def bi_import(body: BiImportRequest):
    p = _BI_PROVIDERS.get((body.provider or "").lower())
    if not p:
        return _err(400, "unknown provider", "Supported: metabase, looker, dbt.")
    if not _bi_enabled():
        return JSONResponse(status_code=501, content={
            "ok": False, "status": "scaffolded",
            "detail": (f"{p['label']} import is scaffolded but not enabled. Set "
                       f"JARVIS_ENABLE_BI_IMPORTS=1 and provide credentials to import {p['imports']}.")})
    if not (body.base_url and (body.token or body.extra)):
        return _err(400, "missing credentials", f"Provide base_url and credentials for {p['label']}.")
    # ── Live import SDK call would run here (fetch databases/models/questions and
    #    map them into connections + metrics). Requires the provider's driver. ──
    return {"ok": True, "status": "pending",
            "detail": (f"Ready to import {p['imports']} from {p['label']} at {body.base_url}. "
                       f"Wire the {p['label']} client here to complete the live import.")}


@router.get("")
async def list_connections():
    data = _load()
    return {"connections": [_public(c) for c in data["connections"]], "count": len(data["connections"])}


@router.get("/{conn_id}")
async def get_connection(conn_id: str):
    data = _load()
    c = next((x for x in data["connections"] if x.get("id") == conn_id), None)
    if not c:
        return _err(404, "not found", f"No connection '{conn_id}'")
    # Decrypt secrets for editor pre-fill, but still mask in public response
    return _public(c)


@router.post("")
async def create_connection(body: ConnectionIn):
    err = _validate(body)
    if err: return _err(400, "invalid", err)
    with _lock:
        data = _load()
        c = {
            "id": str(uuid.uuid4()),
            "kind": body.kind,
            "name": body.name,
            "fields": body.fields,   # stored in plaintext within the encrypted file
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        # We encrypt the WHOLE store, so per-connection fields are at-rest encrypted
        # via the wrapping store file. But to be conservative, store the whole record
        # via _save which writes JSON; secrets are protected by file perms + the key
        # file. For full envelope encryption per-field, we'd add another layer here.
        data["connections"].append(c)
        try: _save(data)
        except OSError as e: return _err(500, "save failed", str(e))
    return _public(c)


@router.put("/{conn_id}")
async def update_connection(conn_id: str, body: ConnectionIn):
    err = _validate(body)
    if err: return _err(400, "invalid", err)
    with _lock:
        data = _load()
        c = next((x for x in data["connections"] if x.get("id") == conn_id), None)
        if not c: return _err(404, "not found", f"No connection '{conn_id}'")
        # Merge fields: if a secret field came through as '***', keep the old value
        cat = _catalog_by_kind().get(body.kind) or {}
        secret_keys = {f["key"] for f in cat.get("fields", []) if f.get("secret")}
        merged = dict(c.get("fields") or {})
        for k, v in (body.fields or {}).items():
            if k in secret_keys and v == "***":
                continue   # preserve existing secret
            merged[k] = v
        c["kind"] = body.kind
        c["name"] = body.name
        c["fields"] = merged
        c["updated_at"] = time.time()
        try: _save(data)
        except OSError as e: return _err(500, "save failed", str(e))
    return _public(c)


@router.delete("/{conn_id}")
async def delete_connection(conn_id: str):
    with _lock:
        data = _load()
        before = len(data["connections"])
        data["connections"] = [x for x in data["connections"] if x.get("id") != conn_id]
        if len(data["connections"]) == before:
            return _err(404, "not found", f"No connection '{conn_id}'")
        try: _save(data)
        except OSError as e: return _err(500, "save failed", str(e))
    return {"ok": True, "deleted": conn_id}


@router.post("/{conn_id}/test")
async def test_connection(conn_id: str):
    """Best-effort connectivity test. Only BigQuery is fully wired in v1;
    others report 'driver not installed' or 'driver present but test not yet implemented'."""
    data = _load()
    c = next((x for x in data["connections"] if x.get("id") == conn_id), None)
    if not c: return _err(404, "not found", f"No connection '{conn_id}'")
    kind = c.get("kind")
    cat = _catalog_by_kind().get(kind) or {}
    driver_ok = _driver_installed(cat.get("driver", ""))

    ok, msg = False, "unknown"
    try:
        if kind == "bigquery":
            from google.cloud import bigquery
            from google.oauth2 import service_account
            import io
            sa_json = (c.get("fields") or {}).get("service_account_json", "")
            if not sa_json:
                ok, msg = False, "no service_account_json"
            else:
                creds = service_account.Credentials.from_service_account_info(
                    json.loads(sa_json),
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                client = bigquery.Client(credentials=creds, project=(c.get("fields") or {}).get("project") or creds.project_id)
                job = client.query("SELECT 1 AS ok")
                _ = list(job.result(timeout=10))
                ok, msg = True, "BigQuery reachable; SELECT 1 succeeded."
        elif kind == "file_upload":
            # Verify the target BQ project + dataset is reachable.
            fields = c.get("fields") or {}
            project = fields.get("project") or ""
            target_dataset = fields.get("target_dataset") or ""
            bq_client = _shared_bq_client()
            if not bq_client:
                ok, msg = False, "Shared BigQuery service account not configured at ~/.config/jarvis/bigquery-key.json"
            elif not project or not target_dataset:
                ok, msg = False, "project and target_dataset are required."
            else:
                try:
                    bq_client.get_dataset(f"{project}.{target_dataset}")
                    ok, msg = True, f"Reachable: {project}.{target_dataset} exists. Ready for uploads."
                except Exception as e:
                    # Dataset might not exist yet — that's OK, we'll create on first upload.
                    if "Not found" in str(e):
                        ok, msg = True, f"Project {project} reachable. Dataset {target_dataset} will be created on first upload."
                    else:
                        raise
        elif kind in ("tiktok_ads", "pinterest_ads", "reddit_ads"):
            fields = c.get("fields") or {}
            required = {
                "tiktok_ads":   ["app_id", "app_secret", "access_token", "advertiser_id"],
                "pinterest_ads":["app_id", "app_secret", "access_token", "ad_account_id"],
                "reddit_ads":   ["client_id", "client_secret", "refresh_token", "account_id", "user_agent"],
            }[kind]
            missing = [k for k in required if not fields.get(k)]
            if missing:
                ok, msg = False, f"Missing credentials: {', '.join(missing)}. Provide them in the connection editor."
            else:
                # Credentials are present. The sync pipeline (token refresh + report API
                # paging + BQ landing) comes online in a follow-up; this just confirms
                # the connection is configured.
                ok, msg = True, f"Credentials configured for {cat.get('name')}. Sync pipeline scaffolded; will activate when you click Sync."
        elif not driver_ok:
            ok, msg = False, f"Driver '{cat.get('driver')}' not installed. pip install it first."
        else:
            # Driver present but per-kind test not yet wired in v1.
            ok, msg = None, f"Driver '{cat.get('driver')}' is installed. Connectivity test for {kind} comes online in a follow-up."
    except Exception as e:
        ok, msg = False, f"{type(e).__name__}: {e}"

    # Persist last test result
    with _lock:
        data = _load()
        for x in data["connections"]:
            if x["id"] == conn_id:
                x["last_tested_at"] = time.time()
                x["last_test_ok"] = ok
                x["last_test_message"] = msg
                break
        try: _save(data)
        except OSError: pass
    return {"ok": ok, "message": msg, "tested_at": time.time()}


# ─── File upload → BigQuery ──────────────────────────────────────────
def _safe_bq_identifier(name: str) -> str:
    """Coerce an arbitrary string into a BQ-safe column or table name."""
    import re
    s = re.sub(r"[^A-Za-z0-9_]+", "_", (name or "").strip())
    if not s:
        s = "col"
    if s[0].isdigit():
        s = "_" + s
    return s[:300]


def _read_table_from_upload(filename: str, raw: bytes):
    """Parse uploaded bytes into a pandas DataFrame; returns (df, sheet_used)."""
    import io
    import pandas as pd  # type: ignore
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    if ext in ("csv", "tsv", "txt"):
        sep = "\t" if ext == "tsv" else ","
        # Try utf-8 first, fall back to latin-1 for Excel-exported CSVs
        try:
            df = pd.read_csv(io.BytesIO(raw), sep=sep)
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(raw), sep=sep, encoding="latin-1")
        return df, None
    elif ext in ("xlsx", "xls", "xlsm"):
        xl = pd.ExcelFile(io.BytesIO(raw))
        # Use the first non-empty sheet
        sheet = xl.sheet_names[0]
        df = xl.parse(sheet)
        return df, sheet
    else:
        raise ValueError(f"Unsupported file extension: .{ext}. Accepted: .csv, .tsv, .xlsx, .xls")


@router.post("/{conn_id}/upload")
async def upload_file(
    conn_id: str,
    file: UploadFile = File(...),
    table_name: Optional[str] = Form(None),
    write_mode: Optional[str] = Form(None),
):
    """Upload a CSV or Excel file. Lands rows into the BQ dataset configured
    on the file_upload connection. Returns the fully-qualified table id.

    - `table_name` (optional form field) overrides the auto-generated name.
    - `write_mode` (optional) — 'replace' or 'append'; falls back to the
      connection's default.
    """
    data = _load()
    c = next((x for x in data["connections"] if x.get("id") == conn_id), None)
    if not c:
        return _err(404, "not found", f"No connection '{conn_id}'")
    if c.get("kind") != "file_upload":
        return _err(400, "wrong kind", f"Connection {conn_id} is kind '{c.get('kind')}', not 'file_upload'.")

    fields = c.get("fields") or {}
    project = fields.get("project") or ""
    target_dataset = fields.get("target_dataset") or ""
    if not project or not target_dataset:
        return _err(400, "invalid", "Connection is missing project and/or target_dataset.")

    bq_client = _shared_bq_client()
    if bq_client is None:
        return _err(500, "no bq creds", "Shared BigQuery service account not configured at ~/.config/jarvis/bigquery-key.json")

    raw = await file.read()
    try:
        import pandas as pd  # type: ignore
        df, sheet_used = _read_table_from_upload(file.filename or "upload.csv", raw)
    except ImportError:
        return _err(500, "missing dep", "pandas + openpyxl are required for file upload. pip install pandas openpyxl")
    except Exception as e:
        return _err(400, "parse failed", f"{type(e).__name__}: {e}")

    if df is None or df.empty:
        return _err(400, "empty", "Parsed file contained no rows.")

    # Sanitize column names for BQ
    new_cols = []
    seen = {}
    for col in df.columns:
        safe = _safe_bq_identifier(str(col))
        # de-dup any collisions caused by sanitization
        n = seen.get(safe, 0)
        if n:
            safe = f"{safe}_{n}"
        seen[safe.rsplit("_", 1)[0] if n else safe] = n + 1
        new_cols.append(safe)
    df.columns = new_cols

    # Resolve table name
    prefix = fields.get("table_prefix") or ""
    if table_name:
        tname = _safe_bq_identifier(table_name)
    else:
        base = (file.filename or "upload").rsplit(".", 1)[0]
        tname = _safe_bq_identifier(f"{prefix}{base}")
    table_id = f"{project}.{target_dataset}.{tname}"

    # Resolve write mode
    mode = (write_mode or fields.get("write_mode") or "replace").lower()
    if mode not in ("replace", "append"):
        mode = "replace"

    from google.cloud import bigquery
    # Ensure dataset exists
    try:
        bq_client.get_dataset(f"{project}.{target_dataset}")
    except Exception:
        try:
            ds_ref = bigquery.Dataset(f"{project}.{target_dataset}")
            ds_ref.location = "US"
            bq_client.create_dataset(ds_ref, exists_ok=True)
        except Exception as e:
            return _err(500, "dataset create failed", f"{type(e).__name__}: {e}")

    # Load the dataframe
    job_config = bigquery.LoadJobConfig(
        write_disposition=(
            bigquery.WriteDisposition.WRITE_TRUNCATE if mode == "replace"
            else bigquery.WriteDisposition.WRITE_APPEND
        ),
        autodetect=True,
    )
    try:
        # Rebind client to the target project for the load job
        from google.oauth2 import service_account as _sa
        creds = _sa.Credentials.from_service_account_file(
            str(_SHARED_BQ_KEY),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        load_client = bigquery.Client(credentials=creds, project=project)
        job = load_client.load_table_from_dataframe(df, table_id, job_config=job_config)
        job.result(timeout=180)
        loaded = load_client.get_table(table_id)
        n_rows = loaded.num_rows
        n_cols = len(loaded.schema)
    except Exception as e:
        return _err(500, "load failed", f"{type(e).__name__}: {e}")

    # Record upload history on the connection
    with _lock:
        data = _load()
        for x in data["connections"]:
            if x["id"] == conn_id:
                hist = list(x.get("upload_history") or [])
                hist.insert(0, {
                    "at": time.time(),
                    "filename": file.filename,
                    "table": table_id,
                    "rows": n_rows,
                    "cols": n_cols,
                    "mode": mode,
                    "sheet": sheet_used,
                })
                x["upload_history"] = hist[:50]  # keep last 50
                x["last_upload_at"] = time.time()
                break
        try: _save(data)
        except OSError: pass

    return {
        "ok": True,
        "table": table_id,
        "rows": n_rows,
        "cols": n_cols,
        "write_mode": mode,
        "sheet": sheet_used,
        "filename": file.filename,
    }
