"""
Taxonomy service (spec §29 step 7, used here so the canonical layer can reference topics).
Loads the versioned healthcare experience taxonomy (taxonomy_v1.yaml) into the
`taxonomy_version` + `topic` tables. Taxonomy is DATA, never hard-coded (spec §28.7).

No hard yaml dependency: falls back to a tiny parser if PyYAML is absent (dev sandbox).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

_TAXO = os.path.join(os.path.dirname(__file__), "taxonomy_v1.yaml")


def _load_yaml(path: str) -> dict:
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        return _mini_parse(path)


def _mini_parse(path: str) -> dict:
    """Minimal parser for our specific taxonomy file shape (version + domains → topics).
    Only used when PyYAML isn't installed; prod uses PyYAML."""
    import re
    version, domains, cur_domain = "taxonomy-1.0", {}, None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.startswith("version:"):
            version = line.split(":", 1)[1].strip().strip('"')
        elif re.match(r"^  [A-Z_]+:$", line):
            cur_domain = line.strip().rstrip(":"); domains[cur_domain] = {"topics": []}
        elif line.strip().startswith("- {") and cur_domain:
            body = line.strip()[3:].rstrip("}")
            d = {}
            for part in re.split(r",\s*(?=[a-z_]+:)", body):
                if ":" in part:
                    k, v = part.split(":", 1); d[k.strip()] = v.strip()
            if d.get("id"):
                domains[cur_domain]["topics"].append(d)
    return {"version": version, "domains": domains}


def load_taxonomy(store, path: str = _TAXO) -> int:
    data = _load_yaml(path)
    version = data["version"]
    now = datetime.now(timezone.utc).isoformat()
    store.upsert("taxonomy_version",
                 {"taxonomy_version": version, "effective_from": now, "effective_to": None,
                  "notes": "loaded from taxonomy_v1.yaml"},
                 conflict_cols=["taxonomy_version"])
    n = 0
    for domain_key, domain in (data.get("domains") or {}).items():
        for t in domain.get("topics", []):
            store.upsert("topic", {
                "topic_id": t["id"], "taxonomy_version": version,
                "parent_topic_id": None, "domain": domain_key,
                "name": t.get("name", t["id"]), "description": t.get("description"),
                "inclusion_examples": None, "exclusion_examples": None,
                "hcahps_mapping": t.get("hcahps_mapping"),
                "regulatory_mapping": t.get("regulatory_mapping"),
            }, conflict_cols=["topic_id", "taxonomy_version"])
            n += 1
    store.commit()
    return n
