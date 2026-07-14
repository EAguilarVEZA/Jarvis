"""
Service line catalog for Orlando Health data warehouse reporting.

Two layers:
  1. ServiceLine objects (the existing 14 marts + rollups) — used by the
     existing reporting module's intent parser. UNCHANGED.
  2. WarehouseCatalog — the new layer. Loads inventory.json (auto-discovered)
     and curated.yaml (hand-written semantics) so the report builder can ask
     intelligent questions about ANY of the 17K tables, not just the 14 marts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


# ═════════════════════════════════════════════════════════════════════
# Original ServiceLine catalog — preserved for backward compatibility
# ═════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ServiceLine:
    key: str                     # canonical key used in code
    label: str                   # human label for dashboards
    dataset: str                 # BigQuery dataset id
    aliases: tuple = field(default=())  # voice/text matches (lowercased compare)


SERVICE_LINES = [
    ServiceLine(
        key="all", label="All Service Lines", dataset="data_mart_all",
        aliases=("all", "everything", "total", "company-wide", "enterprise"),
    ),
    ServiceLine(
        key="cardiology", label="Cardiology", dataset="data_mart_cardiology",
        aliases=("cardiology", "cardiac", "heart", "cardio"),
    ),
    ServiceLine(
        key="cancer", label="Cancer", dataset="data_mart_cancer",
        aliases=("cancer", "oncology", "tumor"),
    ),
    ServiceLine(
        key="orthopedics", label="Orthopedics", dataset="data_mart_orthopedics",
        aliases=("orthopedics", "ortho", "orthopedic", "bone", "joint"),
    ),
    ServiceLine(
        key="neuroscience", label="Neuroscience", dataset="data_mart_neuroscience",
        aliases=("neuroscience", "neuro", "stroke", "brain", "neurology"),
    ),
    ServiceLine(
        key="womens", label="Women's Health", dataset="data_mart_womens",
        aliases=("women", "womens", "women's", "ob", "obgyn", "obstetrics"),
    ),
    ServiceLine(
        key="pediatrics", label="Pediatrics", dataset="data_mart_pediatrics",
        aliases=("pediatrics", "peds", "kids", "children"),
    ),
    ServiceLine(
        key="primary", label="Primary Care", dataset="data_mart_primary",
        aliases=("primary", "primary care", "pcp", "family medicine"),
    ),
    ServiceLine(
        key="surgery", label="Surgery", dataset="data_mart_surgery",
        aliases=("surgery", "surgical", "general surgery"),
    ),
    ServiceLine(
        key="bariatrics", label="Bariatrics", dataset="data_mart_bariatrics",
        aliases=("bariatrics", "bariatric", "weight loss"),
    ),
    ServiceLine(
        key="urology", label="Urology", dataset="data_mart_urology",
        aliases=("urology", "urologic"),
    ),
    ServiceLine(
        key="digestive", label="Digestive Health", dataset="data_mart_digestive",
        aliases=("digestive", "gi", "gastro", "gastroenterology"),
    ),
    ServiceLine(
        key="colon", label="Colon & Rectal", dataset="data_mart_colon",
        aliases=("colon", "colorectal", "rectal"),
    ),
    ServiceLine(
        key="ancillary", label="Ancillary Services", dataset="data_mart_ancillary",
        aliases=("ancillary", "imaging", "lab"),
    ),
    ServiceLine(
        key="facilities", label="Facilities", dataset="data_mart_facilities",
        aliases=("facilities", "facility", "hospital", "hospitals", "ER", "emergency"),
    ),
]


def resolve(name: str) -> Optional[ServiceLine]:
    """Find a service line by key OR by any alias. Case-insensitive.

    Loose match uses word boundaries so 'weather' doesn't match the alias 'ER'
    (substring match was too greedy and caused a class of false positives).
    """
    import re

    if not name:
        return None
    needle = name.strip().lower()
    # Exact key or exact alias match first
    for sl in SERVICE_LINES:
        if sl.key == needle:
            return sl
        if needle in (a.lower() for a in sl.aliases):
            return sl
    # Word-boundary match for multi-word phrases ("the cardiology stuff")
    for sl in SERVICE_LINES:
        for alias in sl.aliases:
            if re.search(rf"\b{re.escape(alias.lower())}\b", needle):
                return sl
    return None


# Backward-compatibility alias — older modules import as find_service_line
find_service_line = resolve


def list_keys() -> list[str]:
    """Return the canonical keys of every service line."""
    return [sl.key for sl in SERVICE_LINES]


# Dict view of SERVICE_LINES — keyed by ServiceLine.key for O(1) lookup
BY_KEY: dict[str, ServiceLine] = {sl.key: sl for sl in SERVICE_LINES}


# Standard table set every service-line dataset exposes.
# Maps logical name → physical table name in the dataset.
CORE_TABLES: dict[str, str] = {
    "leads": "leads",                                  # one row per lead with attribution
    "digital_ads": "digital_ads_w_leads",              # ad performance + leads joined
    "paid_keywords": "paid_keywords_w_leads",          # keyword-level perf + leads
    "campaigns": "combined_campaign_stats",            # all media (digital + traditional)
    "media_efficiency": "efficiency_media_campaigns",  # owned-budget campaign expense
}


# ═════════════════════════════════════════════════════════════════════
# Warehouse catalog — Phase 1 of the report builder
# ═════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Column:
    name: str
    type: str                        # STRING, INTEGER, FLOAT, etc. (from BQ)
    role: Optional[str] = None       # 'dimension'|'metric'|'filter'|'time'|'key'|'noise'
    label: Optional[str] = None
    agg: Optional[str] = None        # 'SUM'|'AVG'|'COUNT'|'COUNT_DISTINCT' for metrics
    unit: Optional[str] = None       # 'USD'|'ratio'|... for metrics
    examples: tuple = field(default=())
    note: Optional[str] = None


@dataclass(frozen=True)
class DerivedMetric:
    name: str
    expr: str                        # SQL expression — assumes appropriate grouping
    label: Optional[str] = None
    unit: Optional[str] = None


@dataclass(frozen=True)
class TableMeta:
    full_name: str                   # 'dataset.table'
    dataset: str
    name: str
    layer: str                       # 'data_mart'|'gold'|'silver'|'bronze'|'unknown'
    source: str                      # 'service_lines'|'cross_channel'|'ga4'|'gsc'|'cross_platform_ads'|'other'
    label: str
    purpose: str
    row_count: int
    time_column: Optional[str]
    columns: tuple = field(default=())
    derived_metrics: tuple = field(default=())
    default_metrics: tuple = field(default=())
    warnings: tuple = field(default=())
    require_date_filter: bool = False
    service_line_key: Optional[str] = None  # which mart (if any)

    # Convenience accessors
    def dimensions(self) -> list[Column]:
        return [c for c in self.columns if c.role == "dimension"]

    def metrics(self) -> list[Column]:
        return [c for c in self.columns if c.role == "metric"]

    def filters(self) -> list[Column]:
        return [c for c in self.columns if c.role == "filter"]

    def time_col(self) -> Optional[Column]:
        return next((c for c in self.columns if c.role == "time"), None)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ─── Internal helpers ───────────────────────────────────────

def _layer_from_dataset(dataset: str) -> str:
    if dataset.startswith("data_mart_"):
        return "data_mart"
    if dataset in ("gold",):
        return "gold"
    if dataset in ("silver",):
        return "silver"
    if dataset.startswith(("fivetran_", "airbyte_", "adwords_", "searchconsole_",
                           "analytics_", "google_ads_", "supermetrics_")):
        return "bronze"
    return "unknown"


def _column_from_inventory(inv_col: dict, override: Optional[dict] = None) -> Column:
    role = label = agg = unit = note = None
    examples: tuple = ()
    if override:
        role = override.get("role")
        label = override.get("label")
        agg = override.get("agg")
        unit = override.get("unit")
        note = override.get("note")
        ex = override.get("examples")
        if ex:
            examples = tuple(ex)
    return Column(
        name=inv_col["name"],
        type=inv_col["type"],
        role=role, label=label, agg=agg, unit=unit, note=note, examples=examples,
    )


def _columns_from_template(template_def: dict, inv_cols: list[dict]) -> tuple[Column, ...]:
    """
    Given a template's column definitions and the actual columns from inventory,
    produce the merged Column list. Inventory provides the type; the template
    provides role/label/etc. Columns present in inventory but not in template
    are included with no role (Jarvis treats them as unknown).
    """
    overrides = template_def.get("columns", {})
    out: list[Column] = []
    seen = set()
    for ic in inv_cols:
        ov = overrides.get(ic["name"])
        out.append(_column_from_inventory(ic, ov))
        seen.add(ic["name"])
    return tuple(out)


def _columns_with_overrides(inv_cols: list[dict], overrides: dict) -> tuple[Column, ...]:
    out = []
    for ic in inv_cols:
        ov = overrides.get(ic["name"])
        out.append(_column_from_inventory(ic, ov))
    return tuple(out)


# ─── The Catalog class ───────────────────────────────────────

class WarehouseCatalog:
    """
    Load inventory + curated YAML and expose a queryable catalog.

    Usage:
        cat = WarehouseCatalog.load()
        cat.get("data_mart_cardiology.combined_campaign_stats")
        cat.search("leads cardiology")
        cat.sources()
        cat.validate({...})
    """

    def __init__(self, inventory: dict, curated: dict):
        self._inventory = inventory
        self._curated = curated
        self._tables: dict[str, TableMeta] = {}
        self._build()

    # ─── Construction ───
    @classmethod
    def load(cls, data_dir: Optional[Path] = None) -> "WarehouseCatalog":
        if data_dir is None:
            data_dir = Path(__file__).parent / "catalog_data"

        with (data_dir / "inventory.json").open() as f:
            inv = json.load(f)

        curated_path = data_dir / "curated.yaml"
        try:
            import yaml  # lazy import — only required when loading curated layer
        except ImportError as e:
            raise RuntimeError(
                "PyYAML required to load curated catalog. `pip install pyyaml`"
            ) from e
        with curated_path.open() as f:
            curated = yaml.safe_load(f)

        return cls(inv, curated)

    def _build(self) -> None:
        """Materialize curated TableMeta objects from inventory + YAML templates."""
        inv_tables = self._inventory["tables"]
        templates = self._curated.get("mart_table_templates", {})

        # 1) Service-line marts — apply templates uniformly across all marts
        sl_block = self._curated.get("service_line_marts", {})
        for mart in sl_block.get("marts", []):
            dataset = mart["dataset"]
            mart_key = mart["key"]
            for tname, tdef in templates.items():
                full_name = f"{dataset}.{tname}"
                if full_name not in inv_tables:
                    continue  # this mart doesn't have this table (e.g., ancillary)
                inv_t = inv_tables[full_name]
                cols = _columns_from_template(tdef, inv_t["columns"])
                derived = tuple(
                    DerivedMetric(**{k: v for k, v in dm.items() if k in DerivedMetric.__dataclass_fields__})
                    for dm in tdef.get("derived_metrics", [])
                )
                warnings = tuple(tdef.get("warnings", []))
                meta = TableMeta(
                    full_name=full_name,
                    dataset=dataset,
                    name=tname,
                    layer="data_mart",
                    source="service_lines",
                    label=f"{mart['label']} — {tdef['label']}",
                    purpose=tdef["purpose"],
                    row_count=inv_t["row_count"],
                    time_column=tdef.get("time_column"),
                    columns=cols,
                    derived_metrics=derived,
                    default_metrics=tuple(tdef.get("default_metrics", [])),
                    warnings=warnings,
                    require_date_filter=bool(tdef.get("hard_rules", {}).get("require_date_filter", False)),
                    service_line_key=mart_key,
                )
                self._tables[full_name] = meta

        # 2) Cross-channel (data_mart_all) — mix of templated tables and one special
        xc = self._curated.get("cross_channel", {})
        for full_name, tdef in xc.get("tables", {}).items():
            if full_name not in inv_tables:
                continue
            inv_t = inv_tables[full_name]
            if "template" in tdef:
                # Apply named template + per-table overrides
                template = templates[tdef["template"]]
                cols = _columns_from_template(template, inv_t["columns"])
                derived = tuple(
                    DerivedMetric(**{k: v for k, v in dm.items() if k in DerivedMetric.__dataclass_fields__})
                    for dm in template.get("derived_metrics", [])
                )
                warnings = tuple(template.get("warnings", [])) + tuple(tdef.get("additional_warnings", []))
                purpose = tdef.get("override_purpose") or template["purpose"]
                label = template["label"]
                time_col = template.get("time_column")
                default_metrics = tuple(template.get("default_metrics", []))
                require_date = bool(template.get("hard_rules", {}).get("require_date_filter", False))
            else:
                # Standalone definition — has its own columns/aggregation
                cols = _columns_with_overrides(inv_t["columns"], tdef.get("column_overrides", {}))
                derived_list = tdef.get("derived_metrics", [])
                derived = tuple(
                    DerivedMetric(**{k: v for k, v in dm.items() if k in DerivedMetric.__dataclass_fields__})
                    for dm in derived_list
                )
                # Handle special_aggregation (data_mart_all.leads case)
                sa = tdef.get("special_aggregation")
                if sa and "lead_count_expr" in sa:
                    derived = derived + (DerivedMetric(name="lead_count", expr=sa["lead_count_expr"], label="Lead count"),)
                warnings = tuple(tdef.get("warnings", []))
                purpose = tdef.get("purpose", "")
                label = tdef.get("label", full_name)
                time_col = tdef.get("time_column")
                default_metrics = tuple(tdef.get("default_metrics", []))
                require_date = bool(tdef.get("hard_rules", {}).get("require_date_filter", False))

            meta = TableMeta(
                full_name=full_name,
                dataset=full_name.split(".")[0],
                name=full_name.split(".")[1],
                layer="data_mart",
                source="cross_channel",
                label=label,
                purpose=purpose,
                row_count=inv_t["row_count"],
                time_column=time_col,
                columns=cols,
                derived_metrics=derived,
                default_metrics=default_metrics,
                warnings=warnings,
                require_date_filter=require_date,
            )
            self._tables[full_name] = meta

        # 3) GA4, GSC, cross-platform ads — all follow the same pattern
        for src_key, src_label in [
            ("ga4", "ga4"),
            ("search_console", "gsc"),
            ("cross_platform_ads", "cross_platform_ads"),
        ]:
            block = self._curated.get(src_key, {})
            for full_name, tdef in block.get("tables", {}).items():
                if full_name not in inv_tables:
                    continue
                inv_t = inv_tables[full_name]
                cols = _columns_with_overrides(inv_t["columns"], tdef.get("column_overrides", {}))
                derived = tuple(
                    DerivedMetric(**{k: v for k, v in dm.items() if k in DerivedMetric.__dataclass_fields__})
                    for dm in tdef.get("derived_metrics", [])
                )
                meta = TableMeta(
                    full_name=full_name,
                    dataset=full_name.split(".")[0],
                    name=full_name.split(".")[1],
                    layer=_layer_from_dataset(full_name.split(".")[0]),
                    source=src_label,
                    label=tdef.get("label", full_name),
                    purpose=tdef.get("purpose", ""),
                    row_count=inv_t["row_count"],
                    time_column=tdef.get("time_column"),
                    columns=cols,
                    derived_metrics=derived,
                    default_metrics=tuple(tdef.get("default_metrics", [])),
                    warnings=tuple(tdef.get("warnings", [])),
                    require_date_filter=bool(tdef.get("hard_rules", {}).get("require_date_filter", False)),
                )
                self._tables[full_name] = meta

    # ─── Query API ───

    def get(self, full_name: str) -> Optional[TableMeta]:
        """Look up a fully-curated table by 'dataset.name'."""
        return self._tables.get(full_name)

    def all_tables(self) -> list[TableMeta]:
        return list(self._tables.values())

    def sources(self) -> list[str]:
        """Distinct logical sources currently surfaced (e.g., 'service_lines', 'ga4')."""
        return sorted({t.source for t in self._tables.values()})

    def tables_by_source(self, source: str) -> list[TableMeta]:
        return [t for t in self._tables.values() if t.source == source]

    def search(self, query: str, limit: int = 10) -> list[TableMeta]:
        """
        Free-text search across curated tables.
        Ranks by: source preference (data_mart > gold), label match, column match.
        """
        q = query.lower()
        scored: list[tuple[int, TableMeta]] = []
        for t in self._tables.values():
            score = 0
            # Layer preference (data_mart wins)
            if t.layer == "data_mart":
                score += 30
            elif t.layer == "gold":
                score += 20
            # Label / purpose match
            if q in t.label.lower():
                score += 40
            if q in t.purpose.lower():
                score += 20
            # Service line match
            if t.service_line_key and t.service_line_key in q:
                score += 50
            for sl in SERVICE_LINES:
                if t.service_line_key == sl.key:
                    if any(a in q for a in sl.aliases):
                        score += 50
                        break
            # Column name match
            col_matches = sum(1 for c in t.columns if c.role and q in c.name.lower())
            score += col_matches * 5
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored[:limit]]

    def validate(self, intent: dict) -> ValidationResult:
        """
        Sanity-check a proposed report intent BEFORE running it.

        intent = {
          "table":    "dataset.table",
          "metrics":  ["leads", "cost"],
          "dimensions": ["dashboard_campaign"],     # optional
          "filters":  {"is_brand_lead": False},     # optional
          "time_window": {"column": "date", "start": "...", "end": "..."},  # optional
        }
        """
        errors: list[str] = []
        warnings: list[str] = []

        tbl_name = intent.get("table")
        if not tbl_name:
            errors.append("No table specified.")
            return ValidationResult(ok=False, errors=errors)

        meta = self.get(tbl_name)
        if meta is None:
            errors.append(f"Table '{tbl_name}' is not in the curated catalog.")
            # Try to suggest something close
            suggestions = self.search(tbl_name.split(".")[-1], limit=3)
            if suggestions:
                names = ", ".join(s.full_name for s in suggestions)
                errors.append(f"Did you mean: {names}?")
            return ValidationResult(ok=False, errors=errors)

        col_names_with_roles = {c.name: c.role for c in meta.columns}
        metric_names = {c.name for c in meta.metrics()}
        derived_names = {dm.name for dm in meta.derived_metrics}
        all_metric_names = metric_names | derived_names
        dimension_names = {c.name for c in meta.dimensions()}

        # Metrics requested must exist
        for m in intent.get("metrics", []):
            if m not in all_metric_names:
                errors.append(
                    f"Metric '{m}' not available on {tbl_name}. "
                    f"Available: {sorted(all_metric_names)}"
                )

        # Dimensions requested must exist
        for d in intent.get("dimensions", []):
            if d not in dimension_names:
                errors.append(
                    f"Dimension '{d}' not available on {tbl_name}. "
                    f"Available: {sorted(dimension_names)}"
                )

        # Filters requested must be valid filter columns OR dimensions
        for fcol in intent.get("filters", {}).keys():
            if fcol not in col_names_with_roles:
                errors.append(f"Filter column '{fcol}' not in {tbl_name}.")

        # Hard rule: date filter required for large tables
        if meta.require_date_filter:
            tw = intent.get("time_window")
            if not tw or not (tw.get("start") and tw.get("end")):
                errors.append(
                    f"{tbl_name} has {meta.row_count:,} rows. A date filter is REQUIRED."
                )

        # Sanity check: tiny vs huge tables
        if meta.row_count == 0:
            warnings.append(f"{tbl_name} is empty (0 rows). Query will return nothing.")
        elif meta.row_count > 100_000_000:
            warnings.append(
                f"{tbl_name} has {meta.row_count:,} rows. Make sure the date range is tight."
            )

        # Surface table-level warnings
        warnings.extend(meta.warnings)

        ok = len(errors) == 0
        return ValidationResult(ok=ok, errors=errors, warnings=warnings)

    # ─── Convenience for the report builder ───

    def summarize(self) -> dict:
        """Quick summary stats — used for inspection / smoke tests."""
        return {
            "curated_tables": len(self._tables),
            "sources": {
                src: len(self.tables_by_source(src))
                for src in self.sources()
            },
            "total_warehouse_tables": self._inventory["stats"]["table_count"],
            "total_warehouse_datasets": self._inventory["stats"]["dataset_count"],
        }
