"""
semantic.writer
───────────────
Safely persists changes to semantic.yaml.

When the Curation Mode UI lets a user rename a field, add a calculated metric,
or create a combined dimension, those changes need to flow back to disk. This
module is the only safe path for that.

Safety guarantees:
  • Atomic writes (temp file + rename, never partial writes)
  • Auto-backup before every save (timestamped, kept indefinitely until cleanup)
  • Validate the YAML structure before committing
  • Cache invalidation triggers a reload on next semantic.load()
  • Lockfile prevents concurrent writers
"""

from __future__ import annotations

import os
import shutil
import time
import yaml
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

from .loader import (
    SemanticModel, SEMANTIC_YAML, SEMANTIC_DRAFT_YAML, SEMANTIC_DIR,
    invalidate_cache, load,
)


class WriterError(Exception):
    pass


# ─── Lockfile ──────────────────────────────────────────────────────
_LOCK_PATH = SEMANTIC_DIR / ".write_lock"


@contextmanager
def _acquire_lock(timeout_seconds: float = 5.0):
    """Acquire a simple file-based write lock. Best-effort, not for high concurrency."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            # O_EXCL fails if the file already exists — atomic check-and-create.
            fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            time.sleep(0.1)
    else:
        raise WriterError(f"Could not acquire write lock at {_LOCK_PATH} within {timeout_seconds}s")

    try:
        yield
    finally:
        try:
            _LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


# ─── Backup ────────────────────────────────────────────────────────
def _backup_current(yaml_path: Path = SEMANTIC_YAML) -> Optional[Path]:
    """Copy the current YAML to a timestamped backup. Returns the backup path."""
    if not yaml_path.exists():
        return None
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup = yaml_path.parent / f"semantic.yaml.bak.{timestamp}"
    shutil.copy2(yaml_path, backup)
    return backup


# ─── Atomic write ──────────────────────────────────────────────────
def _atomic_write(content: str, target: Path):
    """Write content to a temp file in the same directory, then rename.

    This is atomic on POSIX filesystems — readers will see either the old or
    new content, never a partial write.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # rename is atomic on POSIX
        tmp.replace(target)
    except Exception:
        # Clean up temp on failure
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


# ─── YAML serialization ────────────────────────────────────────────
def _model_to_yaml_dict(model: SemanticModel) -> dict:
    """Convert a SemanticModel back into the dict structure that YAML expects.

    Inverse of loader._parse_full. Preserves the structure we read in.
    """
    doc = {"version": model.version}

    # Sources
    doc["sources"] = {}
    for key, s in model.sources.items():
        entry = {"type": s.type}
        if s.project:
            entry["project"] = s.project
        if s.dataset:
            entry["dataset"] = s.dataset
        doc["sources"][key] = entry

    # Clusters
    doc["clusters"] = {
        key: {"display_name": c.display_name, "color": c.color}
        for key, c in model.clusters.items()
    }

    # Tables
    doc["tables"] = {}
    for key, t in model.tables.items():
        table_doc = {
            "display_name": t.display_name,
            "description": t.description,
            "source": t.source,
            "table": t.raw_table,
            "cluster": t.cluster,
            "kind": t.kind,
        }
        if t.primary_key:
            table_doc["primary_key"] = t.primary_key
        if t.join_keys:
            table_doc["join_keys"] = t.join_keys

        fields_doc = {}
        if t.dimensions:
            fields_doc["dimensions"] = [_field_to_dict(f) for f in t.dimensions]
        if t.metrics:
            fields_doc["metrics"] = [_field_to_dict(f) for f in t.metrics]
        if t.dates:
            fields_doc["dates"] = [_field_to_dict(f) for f in t.dates]
        if fields_doc:
            table_doc["fields"] = fields_doc

        doc["tables"][key] = table_doc

    # Calculated metrics
    doc["calculated_metrics"] = {}
    for key, m in model.calculated_metrics.items():
        entry = {
            "display_name": m.display_name,
            "description": m.description,
            "formula": m.formula,
            "format": m.format,
            "available_in": m.available_in,
        }
        if getattr(m, "prefix", ""):
            entry["prefix"] = m.prefix
        if getattr(m, "suffix", ""):
            entry["suffix"] = m.suffix
        doc["calculated_metrics"][key] = entry

    # Combined dimensions
    doc["combined_dimensions"] = {}
    for key, d in model.combined_dimensions.items():
        rules_out = []
        for rule in d.rules:
            if rule.is_default:
                rules_out.append({"default": rule.label})
            elif rule.range_min is not None or rule.range_max is not None:
                rules_out.append({"range_min": rule.range_min, "range_max": rule.range_max, "label": rule.label})
            else:
                rules_out.append({"matches": rule.matches, "label": rule.label})

        doc["combined_dimensions"][key] = {
            "display_name": d.display_name,
            "description": d.description,
            "base_field": d.base_field,
            "available_in": d.available_in,
            "rules": rules_out,
        }

    # Joins. Note 'on' must be quoted in YAML — handled by the dumper config.
    # Only emit join_type when it's NOT the default 'inner' so older configs
    # (and pure-inner setups) round-trip without growing a new field.
    def _join_dict(j):
        d = {"left": j.left, "right": j.right, "on": j.on}
        jt = getattr(j, "join_type", "inner") or "inner"
        if jt != "inner":
            d["join_type"] = jt
        return d
    doc["joins"] = [_join_dict(j) for j in model.joins]

    # Business rules (named reusable filters). Only emit the section if any exist,
    # so older configs without rules round-trip without growing an empty block.
    rules = getattr(model, "business_rules", None) or {}
    if rules:
        doc["business_rules"] = {}
        for key, r in rules.items():
            entry = {
                "display_name": r.display_name,
                "description": r.description,
                "table": r.table,
                "field": r.field,
                "op": r.op,
            }
            # Null-style ops carry no value; everything else does.
            if r.op not in ("is_null", "not_null"):
                entry["value"] = r.value
            doc["business_rules"][key] = entry

    # Hierarchies (ordered drill paths). Only emit the section if any exist.
    hierarchies = getattr(model, "hierarchies", None) or {}
    if hierarchies:
        doc["hierarchies"] = {}
        for key, h in hierarchies.items():
            entry = {"display_name": h.display_name, "levels": list(h.levels),
                     "available_in": list(h.available_in)}
            if getattr(h, "description", ""):
                entry["description"] = h.description
            doc["hierarchies"][key] = entry

    return doc


def _field_to_dict(f) -> dict:
    """Serialize a FieldDef back to dict form."""
    out = {
        "key": f.key,
        "column": f.column,
        "display_name": f.display_name,
    }
    if f.type:
        out["type"] = f.type
    if f.description:
        out["description"] = f.description
    if f.aggregation:
        out["aggregation"] = f.aggregation
    if f.format:
        out["format"] = f.format
    if f.divide_by:
        out["divide_by"] = f.divide_by
    return out


# ─── YAML dumper config ────────────────────────────────────────────
class _SemanticDumper(yaml.SafeDumper):
    """Custom dumper that:
      • Forces 'on' keys to be quoted (otherwise YAML thinks it's boolean True)
      • Indents nested structures for readability
    """
    pass


def _represent_str(dumper, data):
    """Strings that look like YAML reserved words get quoted."""
    if data in ("on", "off", "yes", "no", "true", "false") or data.startswith("\n"):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="'")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_SemanticDumper.add_representer(str, _represent_str)


def _serialize(doc: dict) -> str:
    """Render the doc dict to YAML text."""
    return yaml.dump(
        doc,
        Dumper=_SemanticDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )


# ─── Main API ──────────────────────────────────────────────────────
def save(model: SemanticModel, *, yaml_path: Path = SEMANTIC_DRAFT_YAML, make_backup: bool = True) -> dict:
    """Persist the SemanticModel back to disk.

    Returns:
        dict with 'backup_path' (if made), 'bytes_written', 'path'

    Raises:
        WriterError on any failure.
    """
    with _acquire_lock():
        # 1. Serialize the model
        doc = _model_to_yaml_dict(model)
        yaml_text = _serialize(doc)

        # 2. Validate it parses back
        try:
            reparsed = yaml.safe_load(yaml_text)
        except yaml.YAMLError as e:
            raise WriterError(f"Generated YAML is unparseable: {e}")

        # Sanity: top-level keys we expect
        for required in ("version", "sources", "tables"):
            if required not in reparsed:
                raise WriterError(f"Serialized YAML is missing required key: {required}")

        # 3. Backup the existing file
        backup_path = _backup_current(yaml_path) if make_backup else None

        # 4. Atomic write
        try:
            _atomic_write(yaml_text, yaml_path)
        except Exception as e:
            raise WriterError(f"Failed to write {yaml_path}: {e}")

        # 5. Invalidate the loader cache so next load() re-reads
        invalidate_cache()

        return {
            "path": str(yaml_path),
            "bytes_written": len(yaml_text.encode("utf-8")),
            "backup_path": str(backup_path) if backup_path else None,
        }


def publish() -> dict:
    """Promote the draft semantic.yaml to the live (published) layer.

    Behaviour:
      • If no draft exists, returns ok=False with a clear message.
      • Backs up the current live file before overwriting.
      • Atomic rename to keep readers consistent.
      • Invalidates loader cache so the next read sees the new live model.
      • Returns sizes + backup path.
    """
    if not SEMANTIC_DRAFT_YAML.exists():
        return {"ok": False, "error": "no draft to publish"}
    with _acquire_lock():
        # Read draft text (don't re-serialize — preserves exact bytes)
        draft_text = SEMANTIC_DRAFT_YAML.read_text(encoding="utf-8")
        # Validate it parses + has required top-level keys
        try:
            doc = yaml.safe_load(draft_text)
        except yaml.YAMLError as e:
            raise WriterError(f"Draft YAML is unparseable: {e}")
        if not isinstance(doc, dict):
            raise WriterError("Draft top-level must be a dict")
        for required in ("version", "sources", "tables"):
            if required not in doc:
                raise WriterError(f"Draft missing required key: {required}")

        backup_path = _backup_current(SEMANTIC_YAML)
        try:
            _atomic_write(draft_text, SEMANTIC_YAML)
        except Exception as e:
            raise WriterError(f"Failed to publish to {SEMANTIC_YAML}: {e}")

        # Record publish timestamp + clear the draft so "draft has changes" goes false
        published_at = time.time()
        try:
            SEMANTIC_DRAFT_YAML.unlink()
        except OSError:
            pass
        # Track last publish time
        try:
            (SEMANTIC_DIR / ".last_published").write_text(str(published_at), encoding="utf-8")
        except OSError:
            pass

        invalidate_cache()
        return {
            "ok": True,
            "path": str(SEMANTIC_YAML),
            "bytes_written": len(draft_text.encode("utf-8")),
            "backup_path": str(backup_path) if backup_path else None,
            "published_at": published_at,
        }


def publish_status() -> dict:
    """Return current draft/published state for the UI."""
    out = {
        "live_exists": SEMANTIC_YAML.exists(),
        "draft_exists": SEMANTIC_DRAFT_YAML.exists(),
        "live_path": str(SEMANTIC_YAML),
        "draft_path": str(SEMANTIC_DRAFT_YAML),
    }
    if SEMANTIC_YAML.exists():
        out["live_size"] = SEMANTIC_YAML.stat().st_size
        out["live_mtime"] = SEMANTIC_YAML.stat().st_mtime
    if SEMANTIC_DRAFT_YAML.exists():
        out["draft_size"] = SEMANTIC_DRAFT_YAML.stat().st_size
        out["draft_mtime"] = SEMANTIC_DRAFT_YAML.stat().st_mtime
        # Cheap "has changes" check: byte size differs or draft newer
        if SEMANTIC_YAML.exists():
            same_size = out["live_size"] == out["draft_size"]
            same_text = same_size and SEMANTIC_YAML.read_bytes() == SEMANTIC_DRAFT_YAML.read_bytes()
            out["has_changes"] = not same_text
        else:
            out["has_changes"] = True
    else:
        out["has_changes"] = False
    last_file = SEMANTIC_DIR / ".last_published"
    if last_file.exists():
        try:
            out["last_published_at"] = float(last_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass
    return out


def save_raw_yaml(yaml_text: str, *, yaml_path: Path = SEMANTIC_DRAFT_YAML, make_backup: bool = True) -> dict:
    """Save a YAML string directly to disk. Validates it parses first.

    Useful when the UI lets the user edit raw YAML (advanced mode).
    """
    # Validate before touching disk
    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise WriterError(f"YAML is unparseable: {e}")

    if not isinstance(doc, dict):
        raise WriterError("Top-level YAML must be a dict")

    for required in ("version", "sources", "tables"):
        if required not in doc:
            raise WriterError(f"YAML is missing required key: {required}")

    with _acquire_lock():
        backup_path = _backup_current(yaml_path) if make_backup else None
        _atomic_write(yaml_text, yaml_path)
        invalidate_cache()

        return {
            "path": str(yaml_path),
            "bytes_written": len(yaml_text.encode("utf-8")),
            "backup_path": str(backup_path) if backup_path else None,
        }
