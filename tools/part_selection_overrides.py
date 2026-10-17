# -*- coding: utf-8 -*-
"""Shared config and override handling for the part-selection helper."""

from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_CONFIG: dict[str, Any] = {
    "project_name": "KiCad Project",
    "project_root": ".",
    "schematic_dir": "DOC",
    "source_bom": "__schematic__",
    "lcsc_bom_pattern": "BOM/*LCSC*.csv",
    "input_refs": "BOM/part_selection_input.txt",
    "output_overrides": "BOM/part_selection_overrides.json",
    "stock_cache": "BOM/lcsc_stock_cache.json",
    "kicad_protocol": "kicad-sch-search",
}

ALTERNATE_PART_FIELDS = ("c_code", "model", "package", "param")
MAX_ALTERNATE_PARTS = 5
SCHEMATIC_FIELD_SNAPSHOT_FIELDS = ("comment", "value", "footprint")
PROCUREMENT_CHOICE_FIELDS = (
    "source",
    "supplier",
    "supplier_sku",
    "manufacturer_part_number",
    "package",
    "batch",
    "order_url",
    "related_lcsc_code",
)


def normalize_ref(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9_?+-]", "", str(ref or "")).upper()


def normalize_lcsc_c_code(value: object) -> str:
    match = re.search(r"\bC\s*(\d+)\b", str(value or "").upper())
    return f"C{match.group(1)}" if match else ""


def normalize_alternate_parts(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        c_code = normalize_lcsc_c_code(item.get("c_code"))
        if not c_code or c_code in seen:
            continue
        normalized = {
            key: str(item.get(key) or "").strip()
            for key in ALTERNATE_PART_FIELDS
        }
        normalized["c_code"] = c_code
        result.append(normalized)
        seen.add(c_code)
        if len(result) >= MAX_ALTERNATE_PARTS:
            break
    return result


def normalize_procurement_choice(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    order_url = str(value.get("order_url") or "").strip()
    parsed = urlparse(order_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {}
    normalized = {
        key: str(value.get(key) or "").strip()
        for key in PROCUREMENT_CHOICE_FIELDS
    }
    normalized["order_url"] = order_url
    normalized["related_lcsc_code"] = normalize_lcsc_c_code(normalized["related_lcsc_code"])
    normalized["id"] = "order_" + hashlib.sha256(order_url.encode("utf-8")).hexdigest()[:16]
    return normalized


def normalize_schematic_field_snapshot(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: str(value.get(key) or "").strip()
        for key in SCHEMATIC_FIELD_SNAPSHOT_FIELDS
        if key in value
    }


def _resolve_path(project_root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_root / path


def load_config(config_path: Path) -> dict[str, Any]:
    config_path = Path(config_path).resolve()
    config = dict(DEFAULT_CONFIG)
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
        config.update(loaded)

    project_root_value = Path(config.get("project_root") or ".")
    if project_root_value.is_absolute():
        project_root = project_root_value
    else:
        project_root = (config_path.parent / project_root_value).resolve()
        if config_path.parent.name.upper() == "BOM" and project_root_value == Path("."):
            project_root = config_path.parent.parent.resolve()

    config["config_path"] = config_path
    config["project_root"] = project_root
    for key in ("schematic_dir", "source_bom", "input_refs", "output_overrides", "stock_cache"):
        config[key] = _resolve_path(project_root, config[key])
    return config


def load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    result: dict[str, dict[str, Any]] = {}
    for ref, value in raw.items():
        norm_ref = normalize_ref(ref)
        if not norm_ref or not isinstance(value, dict):
            continue
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            if key == "alternate_parts":
                cleaned[key] = normalize_alternate_parts(item)
            elif key == "procurement_choice":
                cleaned[key] = normalize_procurement_choice(item)
            elif key == "schematic_field_snapshot":
                cleaned[key] = normalize_schematic_field_snapshot(item)
            elif isinstance(item, dict):
                cleaned[str(key)] = {str(k): str(v) for k, v in item.items() if v is not None}
            else:
                cleaned[str(key)] = str(item)
        result[norm_ref] = cleaned
    return result


def save_overrides(path: Path, overrides: dict[str, dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {normalize_ref(ref): value for ref, value in overrides.items() if normalize_ref(ref)}
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_override(path: Path, ref: str, update: dict[str, Any]) -> dict[str, dict[str, Any]]:
    overrides = load_overrides(path)
    norm_ref = normalize_ref(ref)
    if not norm_ref:
        raise ValueError("ref is required")
    current = dict(overrides.get(norm_ref, {}))
    now = datetime.now().isoformat(timespec="seconds")
    for key in (
        "accepted_c_code",
        "manual_c_code",
        "decision",
        "note",
        "status",
        "review_status",
        "stock_c_code",
        "schematic_synced_at",
        "schematic_sync_summary",
    ):
        if key in update:
            current[key] = str(update.get(key) or "").strip()
    if "schematic_field_snapshot" in update:
        current["schematic_field_snapshot"] = normalize_schematic_field_snapshot(
            update.get("schematic_field_snapshot")
        )
    if "field_overrides" in update and isinstance(update["field_overrides"], dict):
        existing_fields = current.get("field_overrides", {})
        if not isinstance(existing_fields, dict):
            existing_fields = {}
        field_overrides = dict(existing_fields)
        for key in ("comment", "value", "footprint", "description", "description_c_code"):
            if key in update["field_overrides"]:
                field_overrides[key] = str(update["field_overrides"].get(key) or "").strip()
        current["field_overrides"] = field_overrides
        current["field_updated_at"] = now
    if "alternate_parts" in update:
        current["alternate_parts"] = normalize_alternate_parts(update.get("alternate_parts"))
        current["alternate_parts_updated_at"] = now
    if "procurement_choice" in update:
        current["procurement_choice"] = normalize_procurement_choice(update.get("procurement_choice"))
        current["procurement_updated_at"] = now
    current["updated_at"] = now
    overrides[norm_ref] = current
    save_overrides(path, overrides)
    return overrides


def choose_effective_c_code(part: dict[str, str], override: dict[str, str] | None) -> str:
    override = override or {}
    decision = str(override.get("decision", "")).strip()
    if decision in {"Reject", "NoChange", "DNP"}:
        return ""
    if decision == "AcceptManual" and override.get("manual_c_code"):
        return override["manual_c_code"].strip()
    if override.get("accepted_c_code"):
        return override["accepted_c_code"].strip()
    if decision == "AcceptEngineering":
        return str(part.get("accepted_c_code", "")).strip()
    if decision == "AcceptLCSC":
        return str(part.get("lcsc_match_c_code", "")).strip()
    return str(part.get("accepted_c_code", "") or part.get("lcsc_match_c_code", "")).strip()
