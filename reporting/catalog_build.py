"""
Parse bq_inventory.txt → inventory.json

The inventory file is a flat text dump from BigQuery introspection:

    DATASET: data_mart_all

      TABLE: combined_campaign_stats | 6,869,225 rows
        date | DATE
        ad_source | STRING
        ...
        Date range: 2013-08-26 to 2043-05-01

This script turns that into structured JSON Jarvis can query at runtime.
Run from CLI:  python -m reporting.catalog_build [path-to-inventory.txt]

Output written to:  reporting/catalog_data/inventory.json
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional


# ─── Regexes ──────────────────────────────────────────────────
RE_DATASET = re.compile(r"^DATASET:\s+(\S+)\s*$")
RE_TABLE = re.compile(r"^\s{2}TABLE:\s+(\S+)\s+\|\s+([\d,]+)\s+rows\s*$")
RE_TABLE_ERROR = re.compile(r"^\s{2}TABLE:\s+(\S+)\s+\|\s+ERROR:\s+(.+)$")
RE_COLUMN = re.compile(r"^\s{4}(\S+)\s+\|\s+(\S+)\s*$")
RE_DATE_RANGE = re.compile(r"^\s{4}Date range:\s+(.+)$")


@dataclass
class Column:
    name: str
    type: str


@dataclass
class Table:
    dataset: str
    name: str
    full_name: str            # 'dataset.table'
    row_count: int
    columns: list[Column] = field(default_factory=list)
    date_range: Optional[str] = None
    error: Optional[str] = None  # if introspection failed for this table


def parse_inventory(path: Path) -> dict:
    """
    Parse the inventory text file into a structured dict.

    Returns:
        {
          "source_file": "...",
          "datasets": ["dataset1", "dataset2", ...],
          "tables": {
            "dataset.table_name": {
              "dataset": "...",
              "name": "...",
              "full_name": "dataset.table_name",
              "row_count": 12345,
              "columns": [{"name": "...", "type": "STRING"}, ...],
              "date_range": "2020-01-01 to 2026-05-10",
              "error": null
            },
            ...
          },
          "stats": {
            "dataset_count": 84,
            "table_count": 712,
            "total_rows": 8_500_000_000,
            "errored_tables": 3
          }
        }
    """
    current_dataset: Optional[str] = None
    current_table: Optional[Table] = None
    tables: dict[str, Table] = {}
    datasets: list[str] = []

    with path.open() as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            # Skip header/divider lines silently
            if not line.strip() or line.startswith("=") or line.startswith("ORLANDO HEALTH"):
                continue

            # DATASET: ...
            m = RE_DATASET.match(line)
            if m:
                current_dataset = m.group(1)
                datasets.append(current_dataset)
                current_table = None
                continue

            # TABLE: ... | N rows
            m = RE_TABLE.match(line)
            if m and current_dataset:
                name = m.group(1)
                rows = int(m.group(2).replace(",", ""))
                full_name = f"{current_dataset}.{name}"
                current_table = Table(
                    dataset=current_dataset,
                    name=name,
                    full_name=full_name,
                    row_count=rows,
                )
                # Note: some tables appear multiple times (e.g. 'leads' across marts)
                # The full_name is unique so this is fine.
                # Within one dataset, duplicate names get overwritten — that's
                # actually correct since the second appearance would be a redefinition.
                tables[full_name] = current_table
                continue

            # TABLE: ... | ERROR: ...  (tables we can't introspect — keep the record)
            m = RE_TABLE_ERROR.match(line)
            if m and current_dataset:
                name = m.group(1)
                err = m.group(2)
                full_name = f"{current_dataset}.{name}"
                current_table = Table(
                    dataset=current_dataset,
                    name=name,
                    full_name=full_name,
                    row_count=-1,  # sentinel for "unknown"
                    error=err,
                )
                tables[full_name] = current_table
                continue

            # Column: name | TYPE
            m = RE_COLUMN.match(line)
            if m and current_table is not None:
                col_name = m.group(1)
                col_type = m.group(2)
                # Filter false positives — "Date range: ..." matches this pattern
                # if we're not careful. Date range has a colon in the value side.
                if col_name == "Date" and "range" in col_type.lower():
                    continue
                current_table.columns.append(Column(name=col_name, type=col_type))
                continue

            # Date range
            m = RE_DATE_RANGE.match(line)
            if m and current_table is not None:
                current_table.date_range = m.group(1).strip()
                continue

    # Compute summary stats
    total_rows = sum(t.row_count for t in tables.values() if t.row_count >= 0)
    errored = sum(1 for t in tables.values() if t.error is not None)

    return {
        "source_file": str(path),
        "datasets": datasets,
        "tables": {fn: _table_to_dict(t) for fn, t in tables.items()},
        "stats": {
            "dataset_count": len(datasets),
            "table_count": len(tables),
            "total_rows": total_rows,
            "errored_tables": errored,
        },
    }


def _table_to_dict(t: Table) -> dict:
    return {
        "dataset": t.dataset,
        "name": t.name,
        "full_name": t.full_name,
        "row_count": t.row_count,
        "columns": [asdict(c) for c in t.columns],
        "date_range": t.date_range,
        "error": t.error,
    }


def main() -> int:
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
    else:
        # Default location next to this script
        src = Path(__file__).parent.parent / "bq_inventory.txt"

    if not src.exists():
        print(f"ERROR: inventory file not found: {src}", file=sys.stderr)
        return 1

    print(f"Parsing {src} ...")
    result = parse_inventory(src)

    out_path = Path(__file__).parent / "catalog_data" / "inventory.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(result, f, indent=2)

    # Summary
    stats = result["stats"]
    print(f"  Datasets:      {stats['dataset_count']}")
    print(f"  Tables:        {stats['table_count']:,}")
    print(f"  Total rows:    {stats['total_rows']:,}")
    print(f"  Error rows:    {stats['errored_tables']}")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
