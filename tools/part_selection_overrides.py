# -*- coding: utf-8 -*-
"""Shared config and override handling for the part-selection helper."""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from part_selection_fields import normalize_field_mapping


DATA_DIRECTORY = "BOM/选料工作台"
CONFIG_NAME = "工作台配置.json"
LOCAL_INVENTORY_NAME = "本地库存.json"
DATA_FILE_NAMES = {
    "output_overrides": "已选物料与字段修改记录.json",
    "stock_cache": "立创库存与参数缓存.json",
    "input_refs": "位号清单.txt",
}
SYNC_LOG_NAME = "原理图同步记录.csv"
RUNTIME_LOG_NAME = "原理图同步运行日志.jsonl"
EXPORT_PREFIXES = ("采购BOM表_", "选料变更记录_")
LEGACY_PATHS = {
    "bom_dir": "BOM", "input_refs": "BOM/part_selection_input.txt",
    "output_overrides": "BOM/part_selection_overrides.json", "stock_cache": "BOM/lcsc_stock_cache.json",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "project_name": "KiCad Project",
    "project_root": ".",
    "schematic_dir": "DOC",
    "bom_dir": DATA_DIRECTORY,
    "source_bom": "__schematic__",
    "lcsc_bom_pattern": "BOM/*LCSC*.csv",
    **{key: f"{DATA_DIRECTORY}/{name}" for key, name in DATA_FILE_NAMES.items()},
    "local_inventory": f"{DATA_DIRECTORY}/{LOCAL_INVENTORY_NAME}",
    "kicad_protocol": "kicad-sch-search",
    "jump_cursor_behavior": "return",
    "open_lcsc_search_on_replace": "true",
}

ALTERNATE_PART_FIELDS = ("c_code", "model", "package", "param")
MAX_ALTERNATE_PARTS = 5
OVERRIDES_UPDATE_LOCK = threading.RLock()
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
    path = Path(os.path.expandvars(str(value))).expanduser()
    if path.is_absolute():
        return path
    return project_root / path


def configuration_defaults(config_path: Path, loaded: dict) -> dict[str, Any]:
    # Explicit older/custom configurations retain their original implicit paths.
    legacy = config_path.name != CONFIG_NAME and loaded.get("data_layout_version", 0) < 2
    defaults = {**DEFAULT_CONFIG, **(LEGACY_PATHS if legacy else {})}
    if "local_inventory" not in loaded:
        defaults["local_inventory"] = str(Path(loaded.get("bom_dir") or defaults["bom_dir"]) / LOCAL_INVENTORY_NAME)
    return defaults


def load_config(config_path: Path) -> dict[str, Any]:
    config_path = Path(os.path.expandvars(str(config_path))).expanduser().resolve()
    loaded = {}
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
    config = {**configuration_defaults(config_path, loaded), **loaded}
    config['field_mapping'] = normalize_field_mapping(config.get('field_mapping'))

    project_root_value = Path(os.path.expandvars(str(config.get("project_root") or "."))).expanduser()
    if project_root_value.is_absolute():
        project_root = project_root_value
    else:
        project_root = (config_path.parent / project_root_value).resolve()
        if config_path.parent.name.upper() == "BOM" and project_root_value == Path("."):
            project_root = config_path.parent.parent.resolve()

    config["config_path"] = config_path
    config["project_root"] = project_root
    for key in ("schematic_dir", "bom_dir", "source_bom", "input_refs", "output_overrides", "stock_cache", "local_inventory"):
        config[key] = _resolve_path(project_root, config[key])
    if config.get("converter_executable"):
        config["converter_executable"] = _resolve_path(project_root, config["converter_executable"])
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
            if key == "workbench_undo" and isinstance(item, dict):
                if isinstance(item.get("record"), dict):
                    cleaned[key] = deepcopy(item)
            elif key == "footprint_change":
                if isinstance(item, dict):
                    cleaned[key] = deepcopy(item)
            elif key == "alternate_parts":
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
    data = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # Keep readers on the complete previous file until the new snapshot is ready.
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".overrides-", suffix=".tmp", delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def merge_override_record(current: dict[str, Any], update: dict[str, Any], now: str,
                          *, capture_undo: bool = False) -> dict[str, Any]:
    current = dict(current)
    if capture_undo and "workbench_undo" not in current:
        current["workbench_undo"] = {"record": deepcopy(current), "created_at": now}
    if update.get("clear_workbench_undo"):
        current.pop("workbench_undo", None)
    elif update.get("rebase_workbench_undo") and isinstance(current.get("workbench_undo"), dict):
        snapshot = deepcopy(current["workbench_undo"])
        baseline = snapshot.get("record", {})
        adopted = dict(update.get("field_overrides") or {})
        baseline["field_overrides"] = {**baseline.get("field_overrides", {}), **adopted}
        baseline["schematic_field_snapshot"] = {**baseline.get("schematic_field_snapshot", {}),
                                                 **update.get("schematic_field_snapshot", {})}
        for key in ("schematic_synced_at", "schematic_sync_summary"):
            if key in update:
                baseline[key] = update[key]
        snapshot["record"] = baseline
        current["workbench_undo"] = snapshot
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
        if capture_undo and 'footprint' in update['field_overrides'] and field_overrides.get('footprint') != existing_fields.get('footprint'):
            current['pcb_update_required'] = {'target': field_overrides['footprint'], 'since': now,
                                              'status': 'requires_live_check'}
            current.pop('footprint_change', None)
        current["field_overrides"] = field_overrides
        current["field_updated_at"] = now
    if "footprint_change" in update:
        if isinstance(update['footprint_change'], dict) and update['footprint_change']:
            current['footprint_change'] = deepcopy(update['footprint_change'])
        else:
            current.pop('footprint_change', None)
    if "alternate_parts" in update:
        current["alternate_parts"] = normalize_alternate_parts(update.get("alternate_parts"))
        current["alternate_parts_updated_at"] = now
    if "procurement_choice" in update:
        current["procurement_choice"] = normalize_procurement_choice(update.get("procurement_choice"))
        current["procurement_updated_at"] = now
    current["updated_at"] = now
    if str(update.get("review_status") or "").strip() == "Confirmed":
        current["review_confirmed_at"] = now
    return current


def save_override(path: Path, ref: str, update: dict[str, Any],
                  *, capture_undo: bool = False) -> dict[str, dict[str, Any]]:
    norm_ref = normalize_ref(ref)
    if not norm_ref:
        raise ValueError("ref is required")
    return save_override_updates(path, {norm_ref: update}, capture_undo=capture_undo)


def save_override_updates(path: Path, updates: dict[str, dict[str, Any]],
                          *, capture_undo: bool = False) -> dict[str, dict[str, Any]]:
    with OVERRIDES_UPDATE_LOCK:
        overrides = load_overrides(path)
        now = datetime.now().isoformat(timespec="seconds")
        for ref, update in updates.items():
            norm_ref = normalize_ref(ref)
            if not norm_ref:
                raise ValueError("ref is required")
            overrides[norm_ref] = merge_override_record(overrides.get(norm_ref, {}), update, now,
                                                       capture_undo=capture_undo)
        if updates:
            save_overrides(path, overrides)
        return overrides


def choose_effective_c_code(part: dict[str, str], override: dict[str, str] | None) -> str:
    override = override or {}
    decision = str(override.get("decision", "")).strip()
    if decision in {"Reject", "NoChange", "DNP", "ClearSelection"}:
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
