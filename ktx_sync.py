"""
ktx_sync
────────
Sync the editable semantic layer (semantic.yaml) INTO ktx so the canonical
engine sees your curated metrics, dimensions, and filters.

The flow is: you edit the semantic layer (rename fields, add a metric, add a
filter) via the Semantic Layer screen → semantic.yaml is updated → call
sync() → this regenerates ktx's authored measure files + the Jarvis↔ktx field
map, then runs `ktx admin reindex`. After that every dashboard and brief that
runs through ktx uses the new definition.

semantic.yaml is the source of truth you edit; ktx is regenerated from it.

    from ktx_sync import sync
    result = sync()   # {"sources": N, "measures": M, "reindex": "...", "ok": True}
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import yaml

_BASE = Path(__file__).parent
_SEMANTIC_YAML = _BASE / "semantic" / "semantic.yaml"
_KTX_PROJECT = Path(os.environ.get("KTX_PROJECT_DIR", _BASE / "ktx-project"))
_KTX_SL = _KTX_PROJECT / "semantic-layer" / "bigquery-warehouse"
_SCHEMA_DIR = _KTX_SL / "_schema"
_MEASURE_MAP = _BASE / "jarvis_ktx_measure_map.json"
_FIELD_MAP = _BASE / "jarvis_ktx_field_map.json"


def _agg_expr(col: str, aggregation: Optional[str], divide_by) -> str:
    a = (aggregation or "SUM").upper()
    if a == "COUNT_DISTINCT":
        base = f"COUNT(DISTINCT {col})"
    elif a == "SUM_BOOL":
        base = f"COUNTIF({col})"            # BigQuery-native; avoids CAST..INT64
    elif a in ("SUM", "AVG", "MIN", "MAX", "COUNT"):
        base = f"{a}({col})"
    else:
        base = f"SUM({col})"
    if divide_by and str(divide_by) not in ("None", "null", ""):
        base = f"{base} / {divide_by}"
    return base


def _ktx_bin() -> str:
    return (os.environ.get("KTX_BIN") or shutil.which("ktx")
            or ("/opt/homebrew/bin/ktx" if Path("/opt/homebrew/bin/ktx").exists() else "ktx"))


def sync(reindex: bool = True) -> dict:
    """Regenerate ktx authored measures + field map from semantic.yaml, then reindex."""
    sm = yaml.safe_load(open(_SEMANTIC_YAML))
    tables = sm.get("tables") or {}
    sources = sm.get("sources") or {}
    calculated = sm.get("calculated_metrics") or {}
    combined = sm.get("combined_dimensions") or {}

    # discovered schema (raw columns + joins per ktx source)
    # Iterate over every *.yaml in _SCHEMA_DIR so newly-added datasets are
    # picked up without code changes (previously hardcoded gold + adwords).
    discovered = {}
    if _SCHEMA_DIR.exists():
        for p in sorted(_SCHEMA_DIR.glob("*.yaml")):
            try:
                doc = yaml.safe_load(open(p)) or {}
            except Exception:
                continue
            for name, body in (doc.get("tables") or {}).items():
                discovered[name] = body

    # jarvis table key -> raw source name (== ktx source) and per-table column expr map
    pk = {tk: t.get("primary_key") for tk, t in tables.items()}
    key_expr = {}   # jarvis_table_key -> {metric_key: column-level aggregate sql}
    for tk, t in tables.items():
        fields = t.get("fields") or {}
        key_expr[tk] = {m["key"]: _agg_expr(m["column"], m.get("aggregation"), m.get("divide_by"))
                        for m in (fields.get("metrics") or [])}

    def expand(formula: str, tk: str) -> str:
        return re.sub(r"\{([a-zA-Z0-9_]+)\}",
                      lambda x: f"({key_expr.get(tk, {}).get(x.group(1))})"
                      if key_expr.get(tk, {}).get(x.group(1)) else x.group(0),
                      formula)

    name_map = {}       # raw_source -> {jarvis_key: ktx_measure_name}
    field_map = {"sources": {}, "dimensions": {}, "metrics": {}, "columns": {},
                 "combined_dimensions": list(combined.keys()), "calculated_metrics": {}}
    written = 0
    total_measures = 0

    for tk, t in tables.items():
        raw = t.get("table")
        if not raw:
            continue
        disc = discovered.get(raw)
        field_map["sources"][tk] = raw
        fields = t.get("fields") or {}
        field_map["dimensions"][tk] = {d["key"]: d.get("column", d["key"])
                                       for d in (fields.get("dimensions") or [])}
        field_map["columns"][tk] = [c["name"] for c in (disc or {}).get("columns", [])] if disc else []
        if not disc:
            # no discovered schema → can't author measures; keep mapping for fallback path
            field_map["metrics"][tk] = {m["key"]: m["key"] for m in (fields.get("metrics") or [])}
            continue

        colnames = {c["name"] for c in disc["columns"]}
        cols = []
        for c in disc["columns"]:
            col = {"name": c["name"], "type": c["type"]}
            if c.get("descriptions"):
                col["descriptions"] = c["descriptions"]
            if c["type"] == "time" and "role" not in col:
                col["role"] = "time"
            cols.append(col)

        name_map[raw] = {}
        field_map["metrics"][tk] = {}
        measures = []
        for m in (fields.get("metrics") or []):
            nm = m["key"] if m["key"] not in colnames else f"total_{m['key']}"
            name_map[raw][m["key"]] = nm
            field_map["metrics"][tk][m["key"]] = nm
            measures.append({"name": nm,
                             "expr": _agg_expr(m["column"], m.get("aggregation"), m.get("divide_by")),
                             "description": f"{m.get('display_name', m['key'])} "
                                            f"({m.get('aggregation', 'SUM').lower()} of {m['column']})."})
        for cname, cinfo in calculated.items():
            if tk in (cinfo.get("available_in") or []):
                exp = expand(cinfo["formula"], tk)
                # Window/LOD metrics (OVER ...) aren't ktx-native measures — leave them out
                # of the ktx layer so queries using them fall back to the BigQuery builder.
                if "OVER" in exp.upper():
                    continue
                nm = cname if cname not in colnames else f"metric_{cname}"
                name_map[raw][cname] = nm
                field_map["metrics"][tk][cname] = nm
                measures.append({"name": nm, "expr": exp,
                                 "description": f"{cinfo.get('display_name', cname)} = {cinfo['formula']}"})

        grain = [pk.get(tk)] if pk.get(tk) else ([cols[0]["name"]] if cols else ["id"])
        authored = {"name": raw,
                    "descriptions": {"user": (disc.get("descriptions", {}) or {}).get("ai", f"{raw} source.")},
                    "table": disc["table"], "grain": grain, "columns": cols, "measures": measures}
        yaml.safe_dump(authored, open(_KTX_SL / f"{raw}.yaml", "w"),
                       sort_keys=False, default_flow_style=False, width=140)
        written += 1
        total_measures += len(measures)

    for cname, cinfo in calculated.items():
        field_map["calculated_metrics"][cname] = cinfo.get("available_in", [])

    json.dump(name_map, open(_MEASURE_MAP, "w"), indent=2)
    json.dump(field_map, open(_FIELD_MAP, "w"), indent=2)

    result = {"ok": True, "sources": written, "measures": total_measures,
              "field_map": str(_FIELD_MAP), "reindex": None}

    if reindex:
        # Lockdown gate — refuse to spawn the ktx binary when KAELIO_LOCKDOWN=1.
        try:
            from jarvis_engine import kaelio_lockdown
            if kaelio_lockdown():
                result["reindex"] = "KAELIO_LOCKDOWN=1 — reindex skipped (binary not spawned)"
                result["reindex_ok"] = False
                return result
        except ImportError:
            pass
        # Force telemetry-off into the child process
        child_env = dict(os.environ)
        child_env.setdefault("KTX_TELEMETRY_DISABLED", "1")
        child_env.setdefault("DO_NOT_TRACK", "1")
        try:
            proc = subprocess.run([_ktx_bin(), "--project-dir", str(_KTX_PROJECT), "admin", "reindex"],
                                  capture_output=True, text=True, timeout=300, env=child_env)
            result["reindex"] = (proc.stdout or proc.stderr or "").strip()[-400:]
            result["reindex_ok"] = proc.returncode == 0
        except Exception as e:  # noqa: BLE001
            result["reindex"] = f"reindex failed: {e}"
            result["reindex_ok"] = False

    return result


if __name__ == "__main__":
    print(json.dumps(sync(), indent=2))
