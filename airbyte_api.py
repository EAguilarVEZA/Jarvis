"""
airbyte_api — Jarvis ↔ Airbyte (open-source data movement) bridge
─────────────────────────────────────────────────────────────────
Mirrors the "/admin/data-sources/create" experience Kaelio exposes via
Fivetran, but routed to a self-hosted Airbyte instance instead.

Designed to be **portable** — your Mac dev box today, a cloud VM tomorrow.
Only the `url` field in the encrypted config has to change.

Endpoints (prefix /api/airbyte):
  • GET   /config                — show the current Airbyte URL + workspace (no secrets)
  • POST  /config                — set/update Airbyte URL + auth + workspace
  • GET   /health                — round-trip ping to the Airbyte instance
  • GET   /workspace             — current workspace (auto-create if missing)
  • GET   /catalog               — source definitions (cached 24h, ~600 connectors)
  • GET   /catalog/categories    — distinct category list for the UI sidebar
  • GET   /sources               — sources active in this workspace
  • POST  /sources               — create a source (returns Airbyte source id + redirect URL)
  • DELETE /sources/{sid}        — delete a source
  • POST  /sources/{sid}/check   — run the connection-check against this source
  • GET   /destinations          — list destinations (BigQuery, GCS, etc.)
  • POST  /destinations          — create a destination
  • GET   /connections           — source→destination pipelines
  • POST  /connections           — create a pipeline
  • POST  /connections/{cid}/sync— trigger a sync now
  • GET   /jobs/{jid}            — sync job status

Storage:
  ~/.config/jarvis/airbyte.enc.json   ← URL + client_id/secret (Fernet-encrypted)
  ~/.config/jarvis/airbyte_catalog.json ← cached connector metadata (plain text;
                                          it's the same as Airbyte's public catalog)
"""
from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Reuse the same encryption helpers connections_api.py uses so we only ever
# have ONE master key on the box.
from connections_api import _encrypt, _decrypt, CFG_DIR  # type: ignore

router = APIRouter(prefix="/api/airbyte", tags=["airbyte"])

# ─── Storage paths ──────────────────────────────────────────────────
STORE          = CFG_DIR / "airbyte.enc.json"
CATALOG_CACHE  = CFG_DIR / "airbyte_catalog.json"
CATALOG_TTL_S  = 24 * 3600
_lock          = threading.Lock()

# Default to localhost — abctl runs Airbyte at port 8000 on the Mac. The
# Jarvis server itself ALSO runs at 8000; we recommend running Airbyte on a
# different port (e.g. abctl local install --port 8001) and capturing that in
# install_airbyte_local.sh.
DEFAULT_URL = os.environ.get("AIRBYTE_API_URL", "http://localhost:8001")


# ─── Config envelope ────────────────────────────────────────────────
class AirbyteConfigIn(BaseModel):
    url: str                           # e.g. http://localhost:8001
    auth_kind: str = "none"            # "none" | "client_credentials" | "bearer"
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    bearer_token: Optional[str] = None
    workspace_id: Optional[str] = None # filled in after first /workspace call


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _load_config() -> dict:
    """Read + decrypt the Airbyte config. Returns {} when unconfigured."""
    if not STORE.exists():
        # Allow zero-config local mode when AIRBYTE_API_URL is set
        if os.environ.get("AIRBYTE_API_URL"):
            return {"url": os.environ["AIRBYTE_API_URL"], "auth_kind": "none"}
        return {}
    try:
        token = STORE.read_text(encoding="utf-8").strip()
        return _decrypt(token)
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(_encrypt(cfg), encoding="utf-8")
    tmp.replace(STORE)
    try:
        os.chmod(STORE, 0o600)
    except OSError:
        pass


def _redact(cfg: dict) -> dict:
    """Strip secrets before sending the config back to the UI."""
    safe = dict(cfg)
    for k in ("client_secret", "bearer_token"):
        if safe.get(k):
            safe[k] = "***redacted***"
    return safe


# ─── HTTP client to Airbyte ────────────────────────────────────────
def _airbyte_url() -> str:
    cfg = _load_config()
    return (cfg.get("url") or DEFAULT_URL).rstrip("/")


def _bearer() -> Optional[str]:
    """Return a Bearer token if configured / minted via client_credentials."""
    cfg = _load_config()
    kind = cfg.get("auth_kind", "none")
    if kind == "bearer":
        return cfg.get("bearer_token")
    if kind == "client_credentials":
        return _mint_bearer(cfg)
    return None


_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}


def _mint_bearer(cfg: dict) -> Optional[str]:
    """Exchange client_id/secret for a short-lived JWT via Airbyte's
    POST /v1/applications/token endpoint. Cached in memory until expiry."""
    if _token_cache["token"] and _token_cache["expires_at"] > time.time() + 30:
        return _token_cache["token"]
    try:
        import requests
        r = requests.post(
            f"{_airbyte_url()}/api/public/v1/applications/token",
            json={"client_id": cfg.get("client_id"),
                  "client_secret": cfg.get("client_secret")},
            timeout=10,
        )
        if r.status_code >= 300:
            return None
        data = r.json()
        _token_cache["token"]      = data.get("access_token")
        _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 900))
        return _token_cache["token"]
    except Exception:
        return None


def _http(method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None, timeout: float = 30.0):
    """Call Airbyte and return (status, parsed_json_or_text)."""
    import requests
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    tok = _bearer()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    url = f"{_airbyte_url()}/api/public/v1{path}"
    try:
        r = requests.request(method, url, json=body, params=params,
                             headers=headers, timeout=timeout)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.status_code, r.json()
        return r.status_code, r.text
    except requests.RequestException as e:
        return 599, {"error": "request_failed", "detail": str(e)}


# ─── Config + health endpoints ──────────────────────────────────────
@router.get("/config")
async def get_config():
    cfg = _load_config()
    if not cfg:
        return {"configured": False, "url_default": DEFAULT_URL,
                "hint": "Run scripts/install_airbyte_local.sh to install Airbyte locally, "
                        "then POST to /api/airbyte/config with the captured URL + credentials."}
    return {"configured": True, **_redact(cfg)}


@router.post("/config")
async def set_config(body: AirbyteConfigIn):
    if body.auth_kind not in ("none", "client_credentials", "bearer"):
        return _err(400, "invalid auth_kind")
    with _lock:
        cfg = _load_config()
        cfg.update({k: v for k, v in body.model_dump().items() if v is not None})
        _save_config(cfg)
    _token_cache["token"] = None    # force re-mint next call
    return {"ok": True, "config": _redact(cfg)}


@router.get("/health")
async def health():
    """Round-trip the Airbyte instance. Returns reachable=True only when we get
    a workspace list back (auth + URL both correct)."""
    code, data = _http("GET", "/workspaces", params={"limit": 1})
    return {
        "url": _airbyte_url(),
        "reachable": 200 <= code < 300,
        "status": code,
        "detail": (data if isinstance(data, dict) else {"raw": str(data)[:300]}),
    }


# ─── Workspace ──────────────────────────────────────────────────────
@router.get("/workspace")
async def workspace():
    cfg = _load_config()
    wid = cfg.get("workspace_id")
    if wid:
        return {"workspace_id": wid, "from_config": True}
    code, data = _http("GET", "/workspaces", params={"limit": 50})
    if code != 200:
        return _err(code, "could not list workspaces", str(data)[:300])
    items = (data.get("data") if isinstance(data, dict) else None) or []
    if not items:
        return _err(404, "no workspaces", "Create one in Airbyte first.")
    wid = items[0].get("workspaceId") or items[0].get("id")
    with _lock:
        cfg = _load_config(); cfg["workspace_id"] = wid; _save_config(cfg)
    return {"workspace_id": wid, "from_config": False, "available": items}


# ─── Catalog (cached 24h) ──────────────────────────────────────────
def _read_catalog_cache() -> Optional[dict]:
    if not CATALOG_CACHE.exists():
        return None
    try:
        data = json.loads(CATALOG_CACHE.read_text(encoding="utf-8"))
        if data.get("fetched_at", 0) + CATALOG_TTL_S < time.time():
            return None
        return data
    except Exception:
        return None


def _write_catalog_cache(payload: dict):
    CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_CACHE.write_text(json.dumps(payload), encoding="utf-8")


def _normalize_source_def(sd: dict) -> dict:
    """Normalize the Airbyte source-definition shape into something the UI
    can render as a Kaelio-style "Activate" card."""
    name = sd.get("name") or sd.get("sourceDefinitionId") or "(unnamed)"
    category = (sd.get("category") or sd.get("sourceType") or "Other").title()
    return {
        "id":             sd.get("sourceDefinitionId") or sd.get("id"),
        "name":           name,
        "icon":           sd.get("icon") or sd.get("iconUrl") or "",
        "category":       category,
        "release_stage":  sd.get("releaseStage") or sd.get("supportLevel") or "unknown",
        "docs_url":       sd.get("documentationUrl") or "",
        "support_level":  sd.get("supportLevel") or "",
        "tags":           sd.get("tags") or [],
    }


# ─── Curated fallback catalog ──────────────────────────────────────
# Used when Airbyte's live catalog API isn't reachable / returns nothing.
# Each entry deep-links into Airbyte's own source-creation flow which always
# works — clicking lands you on the right connector card inside Airbyte.
# Order = popularity for marketing-intelligence buyers.
_FALLBACK = [
    # Marketing & ads
    ("Google Ads",                    "Marketing",   "google-ads"),
    ("Google Analytics 4 (GA4)",      "Marketing",   "google-analytics-data-api"),
    ("Facebook Marketing",            "Marketing",   "facebook-marketing"),
    ("LinkedIn Ads",                  "Marketing",   "linkedin-ads"),
    ("TikTok Marketing",              "Marketing",   "tiktok-marketing"),
    ("Snapchat Marketing",            "Marketing",   "snapchat-marketing"),
    ("Pinterest",                     "Marketing",   "pinterest"),
    ("Bing Ads",                      "Marketing",   "bing-ads"),
    ("Mailchimp",                     "Marketing",   "mailchimp"),
    ("Klaviyo",                       "Marketing",   "klaviyo"),
    ("HubSpot",                       "Marketing",   "hubspot"),
    ("Google Search Console",         "Marketing",   "google-search-console"),
    # Sales & CRM
    ("Salesforce",                    "Sales",       "salesforce"),
    ("Pipedrive",                     "Sales",       "pipedrive"),
    ("Zendesk Support",               "Support",     "zendesk-support"),
    ("Freshdesk",                     "Support",     "freshdesk"),
    # E-commerce & payments
    ("Shopify",                       "E-commerce",  "shopify"),
    ("Stripe",                        "Finance",     "stripe"),
    ("Square",                        "Finance",     "square"),
    # Productivity
    ("Google Sheets",                 "Productivity","google-sheets"),
    ("Airtable",                      "Productivity","airtable"),
    ("Notion",                        "Productivity","notion"),
    ("Slack",                         "Productivity","slack"),
    ("Asana",                         "Productivity","asana"),
    ("Jira",                          "Productivity","jira"),
    ("GitHub",                        "Engineering", "github"),
    # Files & databases
    ("File (CSV, JSON, Excel)",       "Files",       "file"),
    ("Amazon S3",                     "Files",       "s3"),
    ("Google Cloud Storage",          "Files",       "gcs"),
    ("PostgreSQL",                    "Databases",   "postgres"),
    ("MySQL",                         "Databases",   "mysql"),
    ("MongoDB",                       "Databases",   "mongodb-v2"),
    ("BigQuery",                      "Databases",   "bigquery"),
    ("Snowflake",                     "Databases",   "snowflake"),
]


def _build_fallback_catalog() -> list[dict]:
    """Build a fallback list shaped like the live catalog response."""
    return [
        {
            "id":            slug,    # use the slug; UI deep-links use it
            "name":          name,
            "icon":          f"https://connectors.airbyte.com/files/metadata/airbyte/source-{slug}/latest/icon.svg",
            "category":      category,
            "release_stage": "popular",
            "docs_url":      f"https://docs.airbyte.com/integrations/sources/{slug}",
            "support_level": "",
            "tags":          [],
            "is_fallback":   True,
        }
        for name, category, slug in _FALLBACK
    ]


# Keyword → category map so the 600+ public registry connectors get useful facets.
_CAT_KEYWORDS = [
    ("Marketing", ("ads", "adwords", "marketing", "analytics", "mailchimp", "klaviyo", "hubspot",
                   "sendgrid", "iterable", "braze", "customer-io", "marketo", "pardot", "sailthru",
                   "outbrain", "taboola", "criteo", "tiktok", "snapchat", "pinterest", "reddit",
                   "twitter", "instagram", "facebook", "linkedin", "google-search-console", "seo",
                   "mixpanel", "amplitude", "posthog", "matomo", "segment", "gtm", "tag-manager")),
    ("Sales", ("salesforce", "pipedrive", "hubspot", "close", "outreach", "salesloft", "copper",
               "zoho-crm", "dynamics", "sugar", "insightly", "crm")),
    ("Support", ("zendesk", "freshdesk", "intercom", "front", "gorgias", "kustomer", "helpscout",
                 "gladly", "dixa", "aircall", "talkdesk", "support", "ticket")),
    ("E-commerce", ("shopify", "woocommerce", "magento", "bigcommerce", "commerce", "amazon-seller",
                    "ebay", "etsy", "recharge", "shipstation", "aftership", "cart")),
    ("Finance", ("stripe", "square", "paypal", "quickbooks", "xero", "netsuite", "chargebee",
                 "recurly", "braintree", "plaid", "brex", "ramp", "invoice", "billing", "payments")),
    ("Productivity", ("notion", "airtable", "slack", "asana", "trello", "clickup", "monday", "jira",
                      "confluence", "google-sheets", "smartsheet", "basecamp", "wrike", "todoist",
                      "calendar", "gmail", "gcal", "sheets", "drive", "dropbox", "box", "onedrive")),
    ("Engineering", ("github", "gitlab", "bitbucket", "jira", "sentry", "datadog", "pagerduty",
                     "opsgenie", "launchdarkly", "circleci", "harness", "sonar", "jenkins")),
    ("Databases", ("postgres", "mysql", "mssql", "sql-server", "mongodb", "bigquery", "snowflake",
                   "redshift", "clickhouse", "cassandra", "dynamodb", "oracle", "db2", "cockroach",
                   "elasticsearch", "redis", "databricks", "firestore", "firebase", "sqlite")),
    ("Files", ("file", "s3", "gcs", "azure-blob", "sftp", "ftp", "local-json", "local-csv", "excel",
               "spreadsheet", "parquet")),
    ("Advertising", ("google-ads", "bing-ads", "amazon-ads", "apple-search-ads")),
]


def _guess_category(sd: dict) -> str:
    hay = (str(sd.get("dockerRepository", "")) + " " + str(sd.get("name", "")) + " " +
           " ".join(sd.get("tags", []) if isinstance(sd.get("tags"), list) else [])).lower()
    for cat, kws in _CAT_KEYWORDS:
        if any(k in hay for k in kws):
            return cat
    return "Other"


def _fetch_public_registry() -> list[dict]:
    """Fetch Airbyte's PUBLIC OSS connector registry (no account/auth required) and
    normalize the ~600 source connectors into catalog cards. This is what lets the
    catalog show the full connector list even with no live Airbyte workspace."""
    import requests
    url = "https://connectors.airbyte.com/files/registries/v0/oss_registry.json"
    r = requests.get(url, timeout=25, headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    out, seen = [], set()
    for sd in (data.get("sources") or []):
        repo = sd.get("dockerRepository", "") or ""
        slug = repo.split("source-", 1)[1] if "source-" in repo else \
            (sd.get("name", "") or "").lower().strip().replace(" ", "-")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        icon = sd.get("iconUrl") or (f"https://connectors.airbyte.com/files/metadata/{repo}/latest/icon.svg"
                                     if repo else "")
        out.append({
            "id":            slug,
            "name":          sd.get("name") or slug,
            "icon":          icon,
            "category":      _guess_category(sd),
            "release_stage": sd.get("supportLevel") or sd.get("releaseStage") or "",
            "docs_url":      sd.get("documentationUrl") or f"https://docs.airbyte.com/integrations/sources/{slug}",
            "support_level": sd.get("supportLevel") or "",
            "tags":          [t for t in (sd.get("tags") or []) if isinstance(t, str)][:6],
        })
    out.sort(key=lambda x: x["name"].lower())
    return out


@router.get("/catalog")
async def catalog(force_refresh: bool = False):
    """Return all source definitions. Cached 24h on disk.

    Airbyte's Public API (v1) deliberately omits a source_definitions
    listing endpoint, so we have to hit the internal Configuration API at
    /api/v1/source_definition_specifications/list_latest which IS what the
    Airbyte UI itself uses to render its catalog browser."""
    if not force_refresh:
        cached = _read_catalog_cache()
        if cached:
            return {"cached": True, "fetched_at": cached["fetched_at"],
                    "sources": cached["sources"]}

    # Try every known catalog endpoint in order, stop on first success.
    attempts = [
        ("GET",  "/api/public/v1/source_definitions", None, {"limit": 1000}),
        ("POST", "/api/v1/source_definitions/list_latest", {}, None),
        ("POST", "/api/v1/source_definitions/list", {}, None),
        ("GET",  "/api/public/v1/sources/definitions", None, {"limit": 1000}),
    ]
    import requests
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    tok = _bearer()
    if tok: headers["Authorization"] = f"Bearer {tok}"

    last_err = "no endpoints attempted"
    for method, path, body, params in attempts:
        try:
            url = f"{_airbyte_url()}{path}"
            r = requests.request(method, url, json=body, params=params, headers=headers, timeout=20)
            if 200 <= r.status_code < 300:
                data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
                items = (data.get("data") or data.get("sourceDefinitions") or [])
                if items:
                    sources = [_normalize_source_def(x) for x in items]
                    cfg = _load_config()
                    payload = {"fetched_at": time.time(), "sources": sources, "endpoint": path}
                    _write_catalog_cache(payload)
                    return {"cached": False, "fetched_at": payload["fetched_at"],
                            "sources": sources, "endpoint_used": path,
                            "is_fallback": False,
                            "workspace_id": cfg.get("workspace_id") or "",
                            "airbyte_url": _airbyte_url()}
            last_err = f"{path} → HTTP {r.status_code}: {(r.text or '')[:200]}"
        except Exception as e:
            last_err = f"{path} → {e}"

    # No live Airbyte workspace configured (or it's unreachable). Fall back to the
    # PUBLIC Airbyte OSS registry — the full ~600 connectors, no account needed.
    try:
        sources = _fetch_public_registry()
        if sources:
            cfg = _load_config()
            payload = {"fetched_at": time.time(), "sources": sources, "endpoint": "public_oss_registry"}
            _write_catalog_cache(payload)
            return {"cached": False, "fetched_at": payload["fetched_at"], "sources": sources,
                    "endpoint_used": "public_oss_registry", "is_fallback": False,
                    "count": len(sources),
                    "workspace_id": cfg.get("workspace_id") or "", "airbyte_url": _airbyte_url(),
                    "diagnostic": f"Showing Airbyte's public connector registry ({len(sources)} connectors). "
                                  "Connect a live Airbyte workspace to sync them."}
    except Exception as e:
        last_err = f"public registry → {e}"

    # Last resort — curated offline list so the UI is never empty.
    sources = _build_fallback_catalog()
    cfg = _load_config()
    wid = cfg.get("workspace_id") or ""
    payload = {
        "cached": False,
        "fetched_at": time.time(),
        "sources": sources,
        "is_fallback": True,
        "count": len(sources),
        "workspace_id": wid,
        "airbyte_url": _airbyte_url(),
        "diagnostic": f"Offline — showing {len(sources)} popular connectors (no internet to reach the "
                      f"public registry). Last response: {last_err[:200]}",
    }
    return payload


@router.get("/catalog/categories")
async def catalog_categories():
    cached = _read_catalog_cache() or {"sources": []}
    cats: dict[str, int] = {}
    for s in cached.get("sources", []):
        c = s.get("category", "Other")
        cats[c] = cats.get(c, 0) + 1
    return {"categories": sorted(cats.items(), key=lambda x: -x[1])}


# ─── Sources ────────────────────────────────────────────────────────
class SourceIn(BaseModel):
    name: str                       # display name for the source instance
    source_definition_id: str       # which connector type
    configuration: dict = {}         # connector-specific config blob
    # OAuth and credential entry should happen IN Airbyte, not here.


@router.get("/sources")
async def list_sources():
    cfg = _load_config()
    wid = cfg.get("workspace_id")
    if not wid:
        return _err(400, "workspace not set", "Call /api/airbyte/workspace first.")
    code, data = _http("GET", "/sources", params={"workspaceId": wid, "limit": 200})
    if code != 200:
        return _err(code, "could not list sources", str(data)[:300])
    return data


@router.post("/sources")
async def create_source(body: SourceIn):
    cfg = _load_config()
    wid = cfg.get("workspace_id")
    if not wid:
        return _err(400, "workspace not set", "Call /api/airbyte/workspace first.")
    payload = {
        "name": body.name,
        "workspaceId": wid,
        "sourceDefinitionId": body.source_definition_id,
        "connectionConfiguration": body.configuration,
    }
    code, data = _http("POST", "/sources", body=payload)
    if code >= 300:
        return _err(code, "create_failed", str(data)[:400])
    # Build a "finish setup in Airbyte" URL so the admin can complete OAuth
    sid = (data.get("sourceId") or data.get("id")) if isinstance(data, dict) else None
    finish_url = f"{_airbyte_url()}/workspaces/{wid}/source/{sid}" if sid else None
    return {"ok": True, "source": data, "finish_in_airbyte_url": finish_url}


@router.delete("/sources/{sid}")
async def delete_source(sid: str):
    code, data = _http("DELETE", f"/sources/{sid}")
    if code >= 300:
        return _err(code, "delete_failed", str(data)[:300])
    return {"ok": True, "deleted": sid}


@router.post("/sources/{sid}/check")
async def check_source(sid: str):
    code, data = _http("POST", f"/sources/{sid}/check_connection")
    return {"status": code, "result": data}


# ─── Destinations ───────────────────────────────────────────────────
class DestinationIn(BaseModel):
    name: str
    destination_definition_id: str
    configuration: dict = {}


@router.get("/destinations")
async def list_destinations():
    cfg = _load_config()
    wid = cfg.get("workspace_id")
    if not wid:
        return _err(400, "workspace not set")
    code, data = _http("GET", "/destinations", params={"workspaceId": wid, "limit": 100})
    return data if code == 200 else _err(code, "list_failed", str(data)[:300])


@router.post("/destinations")
async def create_destination(body: DestinationIn):
    cfg = _load_config()
    wid = cfg.get("workspace_id")
    if not wid:
        return _err(400, "workspace not set")
    payload = {
        "name": body.name,
        "workspaceId": wid,
        "destinationDefinitionId": body.destination_definition_id,
        "connectionConfiguration": body.configuration,
    }
    code, data = _http("POST", "/destinations", body=payload)
    return data if code < 300 else _err(code, "create_failed", str(data)[:400])


# ─── Connections + jobs ────────────────────────────────────────────
class ConnectionIn(BaseModel):
    name: str
    source_id: str
    destination_id: str
    schedule_type: str = "manual"   # manual | cron | basic
    cron_expression: Optional[str] = None
    namespace_format: Optional[str] = None
    streams: Optional[list[dict]] = None   # leave None to sync all default streams


@router.get("/connections")
async def list_connections():
    cfg = _load_config()
    wid = cfg.get("workspace_id")
    if not wid: return _err(400, "workspace not set")
    code, data = _http("GET", "/connections", params={"workspaceId": wid, "limit": 200})
    return data if code == 200 else _err(code, "list_failed", str(data)[:300])


@router.post("/connections")
async def create_connection(body: ConnectionIn):
    payload = {
        "name":          body.name,
        "sourceId":      body.source_id,
        "destinationId": body.destination_id,
        "scheduleType":  body.schedule_type,
    }
    if body.cron_expression:
        payload["scheduleData"] = {"cron": {"cronExpression": body.cron_expression,
                                            "cronTimeZone": "UTC"}}
    if body.namespace_format:
        payload["namespaceFormat"] = body.namespace_format
    if body.streams:
        payload["configurations"] = {"streams": body.streams}
    code, data = _http("POST", "/connections", body=payload)
    return data if code < 300 else _err(code, "create_failed", str(data)[:400])


@router.post("/connections/{cid}/sync")
async def sync_now(cid: str):
    code, data = _http("POST", "/jobs", body={"connectionId": cid, "jobType": "sync"})
    return data if code < 300 else _err(code, "sync_failed", str(data)[:300])


@router.get("/jobs/{jid}")
async def get_job(jid: str):
    code, data = _http("GET", f"/jobs/{jid}")
    return data if code == 200 else _err(code, "job_lookup_failed", str(data)[:300])
