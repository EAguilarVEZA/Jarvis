"""
semantic.loader
───────────────
Parses ~/jarvis/semantic/semantic.yaml into a validated, in-memory model.

The loader is the single entry point for reading the semantic config.
All other modules in this package consume the dataclasses returned here.

Key responsibilities:
  • Read the YAML file
  • Validate structure (required keys present, types correct)
  • Convert into typed dataclasses for ergonomic use
  • Cache the parsed model so repeat reads are cheap
  • Hot-reload when the YAML file changes on disk (mtime check)
"""

from __future__ import annotations

import os
import yaml
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─── Paths ─────────────────────────────────────────────────────────
_JARVIS_DIR = Path(os.environ.get("JARVIS_HOME", Path.home() / "jarvis"))
SEMANTIC_DIR = _JARVIS_DIR / "semantic"
SEMANTIC_YAML = SEMANTIC_DIR / "semantic.yaml"           # published / live
SEMANTIC_DRAFT_YAML = SEMANTIC_DIR / "semantic.draft.yaml"  # working copy curators edit


def _path_for(use_draft: bool) -> Path:
    """Return the YAML to read from. Draft falls back to live if no draft exists yet."""
    if use_draft and SEMANTIC_DRAFT_YAML.exists():
        return SEMANTIC_DRAFT_YAML
    return SEMANTIC_YAML


# ─── Exceptions ────────────────────────────────────────────────────
class SemanticLoadError(Exception):
    """Raised when the semantic.yaml cannot be loaded or is structurally invalid."""
    pass


# ─── Typed model ───────────────────────────────────────────────────
@dataclass
class FieldDef:
    """A single field in a table (dimension, metric, or date)."""
    key: str
    column: str
    display_name: str
    field_kind: str            # 'dimension' | 'metric' | 'date'
    type: Optional[str] = None
    description: Optional[str] = None
    # Metric-only:
    aggregation: Optional[str] = None     # 'SUM' | 'AVG' | 'COUNT' | 'COUNT_DISTINCT' | 'SUM_BOOL'
    format: Optional[str] = None          # 'currency' | 'number' | 'percent' | 'ratio'
    divide_by: Optional[float] = None     # e.g. 1000000 for micros


@dataclass
class TableDef:
    """A curated table with curated fields."""
    key: str                              # user-facing name, e.g. 'campaigns'
    display_name: str
    description: str
    source: str                           # which data source
    raw_table: str                        # actual BigQuery table name
    cluster: str
    kind: str                             # 'dim' | 'fact' | 'summary'
    primary_key: Optional[str] = None
    join_keys: list[str] = field(default_factory=list)
    dimensions: list[FieldDef] = field(default_factory=list)
    metrics: list[FieldDef] = field(default_factory=list)
    dates: list[FieldDef] = field(default_factory=list)

    def all_fields(self) -> list[FieldDef]:
        return self.dimensions + self.metrics + self.dates

    def field_by_key(self, key: str) -> Optional[FieldDef]:
        for f in self.all_fields():
            if f.key == key:
                return f
        return None


@dataclass
class CalculatedMetric:
    """A formula-based metric. e.g. ROAS = {conversion_value} / NULLIF({spend}, 0)"""
    key: str
    display_name: str
    description: str
    formula: str
    format: str
    available_in: list[str]               # which table keys it applies to
    prefix: str = ""                      # display unit before the value, e.g. "$"
    suffix: str = ""                      # display unit after the value, e.g. "%", "×", " leads"


@dataclass
class CombinedDimensionRule:
    """One row inside a combined dimension's rule list.

    A rule is either a value-match rule (``matches`` → label) or a numeric
    range rule (``range_min`` ≤ value < ``range_max`` → label) used for bins.
    """
    matches: list[str] = field(default_factory=list)
    label: Optional[str] = None
    is_default: bool = False
    range_min: Optional[float] = None
    range_max: Optional[float] = None


@dataclass
class CombinedDimension:
    """A user-defined grouping over an existing dimension's values."""
    key: str
    display_name: str
    description: str
    base_field: str                       # which raw dimension to group
    available_in: list[str]
    rules: list[CombinedDimensionRule] = field(default_factory=list)


@dataclass
class BusinessRule:
    """A named, reusable filter expressed over a curated field.

    Example: "Active Campaigns Only" → campaigns.campaign_status == 'ENABLED'.
    Maps directly onto the query engine's Filter construct (table/field/op/value),
    so a rule can be applied to any query as a WHERE condition.
    """
    key: str
    display_name: str
    description: str
    table: str                            # curated table key the field belongs to
    field: str                            # curated field key
    op: str                               # 'eq' | 'neq' | 'in' | 'gt' | 'lt' | 'gte' | 'lte' | 'like' | 'is_null' | 'not_null'
    value: object = None                  # type depends on op (scalar, list, or None for null ops)


@dataclass
class JoinDef:
    """How two tables connect.

    join_type controls SQL JOIN semantics. Always normalized to lowercase
    on load. Valid values: 'inner' | 'left' | 'right' | 'full' | 'cross'.
    Defaults to 'inner' for backward compat with older YAML.
    """
    left: str
    right: str
    on: str                               # join column name
    join_type: str = "inner"


@dataclass
class ClusterDef:
    key: str
    display_name: str
    color: str


@dataclass
class SourceDef:
    key: str
    type: str                             # 'bigquery'
    project: Optional[str] = None
    dataset: Optional[str] = None


@dataclass
class Hierarchy:
    """An ordered drill path of dimension fields, e.g. service_line → campaign → ad_group → keyword."""
    key: str
    display_name: str
    levels: list[str] = field(default_factory=list)   # ordered dimension field keys
    available_in: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class SemanticModel:
    """Top-level container — the entire parsed semantic.yaml."""
    version: int
    sources: dict[str, SourceDef]
    clusters: dict[str, ClusterDef]
    tables: dict[str, TableDef]
    calculated_metrics: dict[str, CalculatedMetric]
    combined_dimensions: dict[str, CombinedDimension]
    joins: list[JoinDef]
    business_rules: dict[str, BusinessRule] = field(default_factory=dict)
    hierarchies: dict[str, "Hierarchy"] = field(default_factory=dict)

    # ── Convenience lookups ──
    def table(self, key: str) -> Optional[TableDef]:
        return self.tables.get(key)

    def cluster(self, key: str) -> Optional[ClusterDef]:
        return self.clusters.get(key)

    def calc_metrics_for(self, table_key: str) -> list[CalculatedMetric]:
        return [m for m in self.calculated_metrics.values() if table_key in m.available_in]

    def combined_dims_for(self, table_key: str) -> list[CombinedDimension]:
        return [d for d in self.combined_dimensions.values() if table_key in d.available_in]

    def hierarchies_for(self, table_key: str) -> list["Hierarchy"]:
        return [h for h in self.hierarchies.values() if table_key in h.available_in]

    def join_path(self, left: str, right: str) -> Optional[JoinDef]:
        """Find a direct join between two tables (in either direction)."""
        for j in self.joins:
            if (j.left == left and j.right == right) or (j.left == right and j.right == left):
                return j
        return None


# ─── Parsing helpers ───────────────────────────────────────────────
def _require(d: dict, key: str, ctx: str):
    if key not in d:
        raise SemanticLoadError(f"Missing required key '{key}' in {ctx}")
    return d[key]


def _parse_field(raw: dict, kind: str, table_key: str) -> FieldDef:
    """Parse a single field entry."""
    ctx = f"{table_key}.{kind}"
    return FieldDef(
        key=_require(raw, "key", ctx),
        column=_require(raw, "column", ctx),
        display_name=_require(raw, "display_name", ctx),
        field_kind=kind,
        type=raw.get("type"),
        description=raw.get("description"),
        aggregation=raw.get("aggregation"),
        format=raw.get("format"),
        divide_by=raw.get("divide_by"),
    )


def _parse_table(table_key: str, raw: dict) -> TableDef:
    fields_raw = raw.get("fields", {})

    dims = [_parse_field(f, "dimension", table_key) for f in fields_raw.get("dimensions", [])]
    mets = [_parse_field(f, "metric", table_key) for f in fields_raw.get("metrics", [])]
    dts  = [_parse_field(f, "date", table_key) for f in fields_raw.get("dates", [])]

    return TableDef(
        key=table_key,
        display_name=_require(raw, "display_name", table_key),
        description=raw.get("description", ""),
        source=_require(raw, "source", table_key),
        raw_table=_require(raw, "table", table_key),
        cluster=_require(raw, "cluster", table_key),
        kind=_require(raw, "kind", table_key),
        primary_key=raw.get("primary_key"),
        join_keys=raw.get("join_keys", []),
        dimensions=dims,
        metrics=mets,
        dates=dts,
    )


def _parse_calculated_metric(key: str, raw: dict) -> CalculatedMetric:
    return CalculatedMetric(
        key=key,
        display_name=_require(raw, "display_name", f"calculated_metrics.{key}"),
        description=raw.get("description", ""),
        formula=_require(raw, "formula", f"calculated_metrics.{key}"),
        format=raw.get("format", "number"),
        available_in=raw.get("available_in", []),
        prefix=raw.get("prefix", ""),
        suffix=raw.get("suffix", ""),
    )


def _parse_combined_dim(key: str, raw: dict) -> CombinedDimension:
    rules = []
    for r in raw.get("rules", []):
        if "default" in r:
            rules.append(CombinedDimensionRule(label=r["default"], is_default=True))
        elif "range_min" in r or "range_max" in r:
            rules.append(CombinedDimensionRule(
                label=r.get("label"),
                range_min=r.get("range_min"),
                range_max=r.get("range_max"),
            ))
        else:
            rules.append(CombinedDimensionRule(
                matches=r.get("matches", []),
                label=r.get("label"),
            ))

    return CombinedDimension(
        key=key,
        display_name=_require(raw, "display_name", f"combined_dimensions.{key}"),
        description=raw.get("description", ""),
        base_field=_require(raw, "base_field", f"combined_dimensions.{key}"),
        available_in=raw.get("available_in", []),
        rules=rules,
    )


def _parse_business_rule(key: str, raw: dict) -> BusinessRule:
    ctx = f"business_rules.{key}"
    return BusinessRule(
        key=key,
        display_name=_require(raw, "display_name", ctx),
        description=raw.get("description", ""),
        table=_require(raw, "table", ctx),
        field=_require(raw, "field", ctx),
        op=_require(raw, "op", ctx),
        value=raw.get("value"),
    )


def _parse_join(raw: dict, idx: int) -> JoinDef:
    ctx = f"joins[{idx}]"
    on = _require(raw, "on", ctx)
    if not isinstance(on, str):
        raise SemanticLoadError(
            f"In {ctx}, 'on' must be a string (got {type(on).__name__}). "
            f"Likely YAML parsed 'on' as boolean — ensure it's quoted in the YAML."
        )
    # join_type defaults to 'inner' when missing (back-compat). Normalize case.
    jt = (raw.get("join_type") or raw.get("type") or "inner")
    jt = str(jt).strip().lower()
    if jt not in ("inner", "left", "right", "full", "cross"):
        jt = "inner"
    return JoinDef(
        left=_require(raw, "left", ctx),
        right=_require(raw, "right", ctx),
        on=on,
        join_type=jt,
    )


# ─── Main entry point ──────────────────────────────────────────────
def _parse_full(raw_doc: dict) -> SemanticModel:
    version = raw_doc.get("version", 1)

    sources = {
        k: SourceDef(key=k, type=v["type"], project=v.get("project"), dataset=v.get("dataset"))
        for k, v in raw_doc.get("sources", {}).items()
    }
    clusters = {
        k: ClusterDef(key=k, display_name=v.get("display_name", k), color=v.get("color", "gray"))
        for k, v in raw_doc.get("clusters", {}).items()
    }
    tables = {k: _parse_table(k, v) for k, v in raw_doc.get("tables", {}).items()}
    calc = {k: _parse_calculated_metric(k, v) for k, v in raw_doc.get("calculated_metrics", {}).items()}
    combined = {k: _parse_combined_dim(k, v) for k, v in raw_doc.get("combined_dimensions", {}).items()}
    joins = [_parse_join(j, i) for i, j in enumerate(raw_doc.get("joins", []))]
    rules = {k: _parse_business_rule(k, v) for k, v in raw_doc.get("business_rules", {}).items()}
    hierarchies = {
        k: Hierarchy(key=k, display_name=v.get("display_name", k),
                     levels=v.get("levels", []), available_in=v.get("available_in", []),
                     description=v.get("description", ""))
        for k, v in raw_doc.get("hierarchies", {}).items()
    }

    return SemanticModel(
        version=version,
        sources=sources,
        clusters=clusters,
        tables=tables,
        calculated_metrics=calc,
        combined_dimensions=combined,
        joins=joins,
        business_rules=rules,
        hierarchies=hierarchies,
    )


# ─── Caching with mtime hot-reload ─────────────────────────────────
# One cache slot per path (live vs draft) so they don't blow each other away.
_cache_lock = threading.Lock()
_cache_by_path: dict = {}    # str(path) → {"model", "mtime"}
_cache: dict = {"model": None, "mtime": 0.0, "path": None}   # legacy shim, unused now


def load(path: Optional[Path] = None, force: bool = False, use_draft: bool = False) -> SemanticModel:
    """Load and parse a semantic.yaml. Cached per-path; hot-reloads on mtime change.

    Args:
        path: Override the default path. For testing.
        force: Bypass cache and re-read from disk.
        use_draft: Read the draft layer if it exists (else fall back to live).

    Returns:
        SemanticModel
    """
    p = Path(path) if path else _path_for(use_draft)

    if not p.exists():
        raise SemanticLoadError(f"semantic.yaml not found at {p}")

    key = str(p)
    with _cache_lock:
        current_mtime = p.stat().st_mtime
        slot = _cache_by_path.get(key)
        if (not force and slot is not None
                and slot["model"] is not None
                and slot["mtime"] >= current_mtime):
            return slot["model"]

        try:
            with open(p, "r") as f:
                raw_doc = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise SemanticLoadError(f"YAML parse error in {p}: {e}") from e

        if not isinstance(raw_doc, dict):
            raise SemanticLoadError(f"Top-level of {p} must be a dict, got {type(raw_doc).__name__}")

        model = _parse_full(raw_doc)
        _cache_by_path[key] = {"model": model, "mtime": current_mtime}
        return model


def invalidate_cache():
    """Force the next load() to re-read from disk (all paths)."""
    with _cache_lock:
        _cache_by_path.clear()
        _cache["path"] = None
