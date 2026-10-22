# -*- coding: utf-8 -*-
"""Editable local web UI for KiCad part-selection review."""

from __future__ import annotations

import argparse
import csv
import ctypes
import html
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import tempfile
from part_selection_runtime import runtime_directory
import traceback
import webbrowser
from copy import deepcopy
from dataclasses import asdict
from collections import OrderedDict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4
from functools import partial

from part_selection_search import CandidateSearchJobs
from part_selection_settings import SettingsGate, settings_snapshot, save_settings, choose_path, apply_runtime_settings, open_settings_directory, preview_field_mapping
from part_selection_library import SelectionJobs
from part_selection_footprints import FootprintChoices
from part_selection_inventory import (calculate_inventory_status, load_inventory,
                                      merge_inventory_records, parse_inventory_workbook, save_inventory,
                                      inventory_import_time, apply_inventory_matches, inventory_source_owners)
from part_selection_specs import validate_passive_specs

from part_selection_helper import (
    find_latest_lcsc_csv,
    kicad_jump_url,
    lcsc_search_url,
    load_schematic_parts,
    merge_part_data,
    read_schematic_symbol_field_values,
    read_refs_from_bom,
    read_target_refs,
    search_keyword,
    split_refs,
    update_schematic_symbol_fields,
    update_schematic_symbol_field_map,
)
from part_selection_overrides import (
    SYNC_LOG_NAME, RUNTIME_LOG_NAME, EXPORT_PREFIXES,
    MAX_ALTERNATE_PARTS,
    OVERRIDES_UPDATE_LOCK,
    choose_effective_c_code,
    load_config,
    load_overrides,
    normalize_alternate_parts,
    normalize_lcsc_c_code,
    merge_override_record,
    normalize_procurement_choice,
    normalize_schematic_field_snapshot,
    save_override,
    save_override_updates,
    save_overrides,
)
from part_stock_lookup import (
    candidate_stock_result,
    enrich_rows_with_cached_stock,
    enrich_rows_with_stock,
    exact_c_code_keyword,
    find_chrome_executable,
    fetch_szlcsc_overseas_channels,
    fetch_szlcsc_domestic_products,
    fetch_szlcsc_partner_products,
    load_stock_cache,
    lookup_stock_with_cache,
    save_stock_cache,
    search_lcsc_candidates,
    stock_cache_lock,
    stock_to_candidate_item,
    preserve_candidate_parameters,
)
EDITABLE_FIELDS = ("comment", "value", "footprint")
SCHEMATIC_SYNC_FIELDS = (
    *EDITABLE_FIELDS,
    "lcsc_c_code",
    "lcsc_url",
    "description",
    "lcsc_alt",
    "lcsc_alt_info",
    "procurement_url",
    "procurement_info",
)
GENERATED_REVIEW_BOM_PREFIXES = ("part_selection_change_log_", "part_selection_review_", EXPORT_PREFIXES[1])
SCHEMATIC_SYNC_LOG = SYNC_LOG_NAME
SCHEMATIC_SYNC_RUNTIME_LOG = RUNTIME_LOG_NAME
SCHEMATIC_SYNC_RUNTIME_LOG_LOCK = threading.Lock()
SCHEMATIC_SYNC_TRANSACTION_LOCK = threading.Lock()
SCHEMATIC_NOTIFY_SETTLE_SECONDS = 0.25
SCHEMATIC_SOURCE_BOM = "__schematic__"
DEFAULT_BULK_SYNC_REF_MAP = {}


def find_latest_by_pattern(project_root: Path, pattern: str) -> Path | None:
    path_pattern = Path(pattern)
    if path_pattern.is_absolute():
        search_root = path_pattern.parent
        glob_pattern = path_pattern.name
    else:
        search_root = project_root / path_pattern.parent
        glob_pattern = path_pattern.name
    files = sorted(search_root.glob(glob_pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None


def secondary_monitor_window_args() -> tuple[list[str], dict[str, int] | None]:
    """Return Chrome placement flags for the first non-primary Windows monitor."""
    if os.name != "nt":
        return [], None
    try:
        class Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", Rect),
                ("rcWork", Rect),
                ("dwFlags", ctypes.c_ulong),
            ]

        monitors: list[dict[str, int]] = []
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(Rect),
            ctypes.c_void_p,
        )

        def collect_monitor(handle, _device_context, _monitor_rect, _data):
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(MonitorInfo)
            if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                rect = info.rcMonitor
                monitors.append(
                    {
                        "left": int(rect.left),
                        "top": int(rect.top),
                        "width": int(rect.right - rect.left),
                        "height": int(rect.bottom - rect.top),
                        "primary": int(bool(info.dwFlags & 1)),
                    }
                )
            return 1

        callback = callback_type(collect_monitor)
        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, None)
        secondary = next((item for item in monitors if not item["primary"]), None)
        if not secondary:
            return [], None
        args = [
            f"--window-position={secondary['left']},{secondary['top']}",
        ]
        return args, secondary
    except (AttributeError, OSError, TypeError):
        return [], None


def chrome_window_handles() -> set[int]:
    """Return visible Chrome top-level window handles on Windows."""
    if os.name != "nt":
        return set()
    try:
        user32 = ctypes.windll.user32
        handles: set[int] = set()
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def collect_window(handle, _data):
            if not user32.IsWindowVisible(handle):
                return 1
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(handle, class_name, len(class_name))
            if class_name.value == "Chrome_WidgetWin_1":
                handles.add(int(handle))
            return 1

        callback = callback_type(collect_window)
        user32.EnumWindows(callback, None)
        return handles
    except (AttributeError, OSError, TypeError):
        return set()


def place_chrome_window_on_monitor(
    before_handles: set[int],
    monitor: dict[str, int] | None,
    preferred_pid: int | None = None,
) -> None:
    """Move the newly-created Chrome app window to the selected monitor."""
    if os.name != "nt" or not monitor:
        return
    try:
        user32 = ctypes.windll.user32
        deadline = time.monotonic() + 5.0
        target = None
        while time.monotonic() < deadline:
            new_handles = chrome_window_handles() - before_handles
            if new_handles:
                target = next(iter(new_handles))
                break
            time.sleep(0.05)
        if not target:
            return
        # Chrome may reuse its existing browser process, so launch flags alone
        # are not sufficient to place a new app window reliably.
        if preferred_pid:
            preferred_handles = []
            for handle in new_handles:
                process_id = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
                if int(process_id.value) == preferred_pid:
                    preferred_handles.append(handle)
            if preferred_handles:
                target = preferred_handles[0]
        time.sleep(0.75)
        swp_show_window = 0x0040
        user32.ShowWindow(target, 9)  # SW_RESTORE
        user32.SetWindowPos(
            target,
            0,
            monitor["left"] + 100,
            monitor["top"] + 100,
            max(320, min(1200, monitor["width"] - 200)),
            max(240, min(800, monitor["height"] - 200)),
            swp_show_window,
        )
        user32.ShowWindow(target, 3)  # SW_MAXIMIZE
        time.sleep(0.15)
        if not user32.IsZoomed(target):
            user32.ShowWindow(target, 3)
        user32.SetForegroundWindow(target)
    except (AttributeError, OSError, TypeError):
        return


def lcsc_code_url(c_code: object) -> str:
    match = re.search(r"\bC\s*(\d+)\b", str(c_code or "").upper())
    if not match:
        return ""
    code = f"C{match.group(1)}"
    return f"https://so.szlcsc.com/global.html?k={code}"


def stock_param_text(stock: object) -> str:
    if not isinstance(stock, dict):
        return ""
    primary = str(stock.get("param") or "")
    detail = str(stock.get("param_detail") or "")
    pieces: list[str] = []
    if primary:
        pieces.append(primary)
    if detail and detail != primary:
        pieces.append(detail)
    existing_text = "\n".join(pieces)
    package = str(stock.get("package") or "").strip()
    if package and not re.search(r"(?:封装|package)\s*[:：]", existing_text, re.I):
        pieces.append(f"封装: {package}")
    brand = str(stock.get("brand") or "").strip()
    normalized_text = re.sub(r"\s+", "", existing_text).casefold()
    if brand and re.sub(r"\s+", "", brand).casefold() not in normalized_text:
        pieces.append(f"品牌: {brand}")
    return "\n".join(pieces)


def alternate_part_summary_value(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("|", "/")).strip()


def alternate_parts_to_schematic_fields(parts: object) -> dict[str, str]:
    normalized = normalize_alternate_parts(parts)
    codes = ",".join(item["c_code"] for item in normalized)
    info = "\n".join(
        " | ".join(
            [
                item["c_code"],
                alternate_part_summary_value(item.get("model")),
                alternate_part_summary_value(item.get("package")),
                alternate_part_summary_value(item.get("param")),
            ]
        )
        for item in normalized
    )
    return {"lcsc_alt": codes, "lcsc_alt_info": info}


def alternate_parts_from_schematic(codes: object, info: object) -> list[dict[str, str]]:
    info_by_code: dict[str, dict[str, str]] = {}
    for line in str(info or "").splitlines():
        pieces = [piece.strip() for piece in line.split("|", 3)]
        c_code = normalize_lcsc_c_code(pieces[0] if pieces else "")
        if not c_code:
            continue
        padded = [*pieces, "", "", ""]
        info_by_code[c_code] = {
            "c_code": c_code,
            "model": padded[1],
            "package": padded[2],
            "param": padded[3],
        }
    result = []
    for raw_code in re.split(r"[,;\s]+", str(codes or "")):
        c_code = normalize_lcsc_c_code(raw_code)
        if not c_code or any(item["c_code"] == c_code for item in result):
            continue
        result.append(
            info_by_code.get(
                c_code,
                {"c_code": c_code, "model": "", "package": "", "param": ""},
            )
        )
        if len(result) >= MAX_ALTERNATE_PARTS:
            break
    return result


def part_alternate_parts(part, override: dict[str, object] | None) -> list[dict[str, str]]:
    override = override or {}
    if "alternate_parts" in override:
        return normalize_alternate_parts(override.get("alternate_parts"))
    return alternate_parts_from_schematic(
        getattr(part, "lcsc_alt", ""),
        getattr(part, "lcsc_alt_info", ""),
    )


def alternate_parts_signature(parts: object) -> str:
    return json.dumps(normalize_alternate_parts(parts), ensure_ascii=False, sort_keys=True)


def procurement_summary_value(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("|", "/")).strip()


def procurement_choice_to_schematic_fields(choice: object) -> dict[str, str]:
    normalized = normalize_procurement_choice(choice)
    if not normalized:
        return {"procurement_url": "", "procurement_info": ""}
    info = " | ".join(
        procurement_summary_value(normalized.get(key))
        for key in (
            "source",
            "supplier",
            "supplier_sku",
            "manufacturer_part_number",
            "package",
            "batch",
            "related_lcsc_code",
        )
    )
    return {"procurement_url": normalized["order_url"], "procurement_info": info}


def procurement_choice_from_schematic(url: object, info: object) -> dict[str, str]:
    pieces = [piece.strip() for piece in str(info or "").split("|", 6)]
    padded = [*pieces, "", "", "", "", "", "", ""]
    return normalize_procurement_choice(
        {
            "source": padded[0],
            "supplier": padded[1],
            "supplier_sku": padded[2],
            "manufacturer_part_number": padded[3],
            "package": padded[4],
            "batch": padded[5],
            "related_lcsc_code": padded[6],
            "order_url": url,
        }
    )


def part_procurement_choice(part, override: dict[str, object] | None) -> dict[str, str]:
    override = override or {}
    if "procurement_choice" in override:
        return normalize_procurement_choice(override.get("procurement_choice"))
    return procurement_choice_from_schematic(
        getattr(part, "procurement_url", ""),
        getattr(part, "procurement_info", ""),
    )


def procurement_choice_signature(choice: object) -> str:
    return json.dumps(normalize_procurement_choice(choice), ensure_ascii=False, sort_keys=True)


def enrich_alternate_parts_with_cached_stock(
    rows: list[dict[str, object]], cache_path: Path
) -> list[dict[str, object]]:
    cache = load_stock_cache(cache_path)
    for row in rows:
        enriched: list[dict[str, object]] = []
        for item in normalize_alternate_parts(row.get("alternate_parts")):
            alternate = dict(item)
            alternate["stock"] = dict(cache.get(item["c_code"], {}))
            enriched.append(alternate)
        row["alternate_parts"] = enriched
    return rows


def selection_codes_for_row(row: dict[str, object]) -> dict[str, str]:
    selections = row.get("per_ref_selection") or {}
    return {
        str(ref).upper(): normalize_lcsc_c_code(item.get("effective_c_code"))
        for ref, item in selections.items()
        if isinstance(item, dict) and normalize_lcsc_c_code(item.get("effective_c_code"))
    }


def enrich_rows_with_selection_stock(
    rows: list[dict[str, object]], cache_path: Path, fetch: bool
) -> list[dict[str, object]]:
    code_rows = [
        {"effective_c_code": code}
        for code in dict.fromkeys(code for row in rows for code in selection_codes_for_row(row).values())
    ]
    (enrich_rows_with_stock if fetch else enrich_rows_with_cached_stock)(code_rows, cache_path)
    stocks = {
        row["effective_c_code"]: row.get("stock", {"status": "待查询", "c_code": row["effective_c_code"]})
        for row in code_rows
    }
    for row in rows:
        codes = selection_codes_for_row(row)
        selections = row.get("per_ref_selection") or {}
        row["stock_by_ref"] = {
            ref: dict(stocks.get(code) or {"status": "NeedCCode", "c_code": ""})
            for ref, code in codes.items()
        }
        row["stock_by_ref"].update({
            str(ref).upper(): {"status": "NeedCCode", "c_code": ""}
            for ref, selection in selections.items()
            if isinstance(selection, dict) and not selection.get("effective_c_code")
        })
        all_codes = [normalize_lcsc_c_code(item.get("effective_c_code"))
                     for item in selections.values() if isinstance(item, dict)]
        unique_codes = list(dict.fromkeys(all_codes))
        codes_mixed = len([code for code in unique_codes if code]) > 1 or (
            any(all_codes) and any(not code for code in all_codes)
        )
        if not codes_mixed:
            row["stock"] = dict(stocks.get(unique_codes[0]) or {
                "status": "NeedCCode", "c_code": ""
            }) if unique_codes else {
                "status": "NeedCCode", "c_code": ""
            }
            row["stock_by_ref"] = {
                str(ref).upper(): dict(stocks.get(normalize_lcsc_c_code(item.get("effective_c_code"))) or {
                    "status": "NeedCCode", "c_code": ""
                })
                for ref, item in selections.items() if isinstance(item, dict)
            }
        else:
            row["stock"] = {
                "status": "多料号",
                "c_code": "",
                "entries": [
                    {"ref": ref, "c_code": code, "stock": dict(row["stock_by_ref"].get(ref) or {})}
                    for ref, selection in selections.items()
                    for code in [codes.get(str(ref).upper(), "")]
                ],
            }
        for ref, fields in (row.get("per_ref_sync_fields") or {}).items():
            code = codes.get(str(ref).upper(), "")
            stock = stocks.get(code) or {}
            if not fields.get("description") and stock:
                description = stock_param_text(stock)
                if description:
                    fields["description"] = description
                    fields["description_c_code"] = code
                    fields["lcsc_c_code"] = code
                    fields["lcsc_url"] = lcsc_code_url(code)
    return rows


def cache_candidate_stock(cache_path: Path, candidate: object) -> None:
    if not isinstance(candidate, dict):
        return
    c_code = normalize_lcsc_c_code(candidate.get("c_code"))
    if not c_code or not str(candidate.get("stock") or "").strip():
        return
    cached = {
        key: candidate.get(key, "")
        for key in (
            "model",
            "brand",
            "package",
            "param",
            "param_detail",
            "parameters",
            "product_id",
            "detail_parameter_status",
            "detail_url",
            "stock",
            "moq",
            "mpq",
            "price",
            "price_ladder",
            "delivery",
            "channel",
            "arrange",
            "category",
            "image_url",
            "image_url_big",
            "url",
            "order_channels",
        )
    }
    cached.update(
        {
            "ok": True,
            "status": "Found",
            "c_code": c_code,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    with stock_cache_lock(cache_path):
        cache = load_stock_cache(cache_path)
        cache[c_code] = preserve_candidate_parameters(cached, cache.get(c_code))
        save_stock_cache(cache_path, cache)


def row_effective_c_code(row: dict[str, object]) -> str:
    if "effective_c_code" in row:
        return str(row.get("effective_c_code") or "").strip()
    return str(
        row.get("stock_c_code")
        or row.get("effective_c_code")
        or row.get("accepted_c_code")
        or row.get("lcsc_match_c_code")
        or ""
    ).strip()


def row_spec_check(row: dict[str, object]) -> dict[str, object]:
    code = normalize_lcsc_c_code(row_effective_c_code(row))
    if not code or row.get('selection_codes_mixed') or row_is_nc(row):
        return {'status': 'not_applicable', 'issues': [], 'unknown_fields': [], 'c_code': code}
    stock = row.get('stock') or {}
    if normalize_lcsc_c_code(stock.get('c_code')) != code:
        stock = {}
    return {**validate_passive_specs(row, stock, actual_by_ref=row.get('schematic_fields_by_ref') or {}, explain=True), 'c_code': code}


def row_review_status(row: dict[str, object]) -> str:
    return str(row.get("review_status") or (row.get("override") or {}).get("review_status") or "Pending")


def row_code_status(row: dict[str, object]) -> str:
    selections = row.get("per_ref_selection") or {}
    if selections:
        statuses = {str(item.get("status") or "") for item in selections.values() if isinstance(item, dict)}
        decisions = {str(item.get("decision") or "") for item in selections.values() if isinstance(item, dict)}
        has_manual = "ChangedToNewCode" in statuses or "AcceptManual" in decisions
        has_rejected = "Rejected" in statuses or "Reject" in decisions
        if has_manual and has_rejected:
            return "Mixed"
        if has_rejected:
            return "Rejected"
        if has_manual:
            return "ChangedToNewCode"
        if row.get("selection_codes_mixed"):
            return "Mixed"
    override = row.get("override") or {}
    decision = str(override.get("decision") or row.get("decision") or "")
    status = str(override.get("status") or row.get("status") or "")
    if decision == "Reject" or status == "Rejected":
        return "Rejected"
    if decision == "AcceptManual" or status == "ChangedToNewCode":
        return "ChangedToNewCode"
    return "Original"


def row_is_nc(row: dict[str, object]) -> bool:
    value = str(row.get("value") or "").upper()
    comment = str(row.get("comment") or "").strip().upper()
    nc_pattern = r"(?<![A-Z0-9])(NC|DNP|DNF|NO\s*LOAD)(?![A-Z0-9])"
    return bool(re.search(nc_pattern, value) or comment in {"NC", "DNP", "DNF", "NO LOAD"})


def stock_number(stock: object) -> float | None:
    if not isinstance(stock, dict):
        return None
    try:
        return float(str(stock.get("stock", "")).replace(",", "").strip())
    except ValueError:
        return None


def row_stock_is_low(row: dict[str, object]) -> bool:
    stock = row.get("stock")
    if isinstance(stock, dict) and isinstance(stock.get("entries"), list):
        return bool(stock.get("entries")) and any(
            isinstance(item.get("stock"), dict)
            and item["stock"].get("ok")
            and (stock_number(item["stock"]) or 0) < 100
            for item in stock["entries"]
        )
    number = stock_number(stock)
    return bool(isinstance(stock, dict) and stock.get("ok") and number is not None and number < 100)


def row_stock_already_queried(row: dict[str, object]) -> bool:
    stock = row.get("stock")
    if not isinstance(stock, dict):
        return False
    if isinstance(stock.get("entries"), list):
        return bool(stock["entries"]) and all(
            row_stock_already_queried({"stock": item.get("stock")})
            for item in stock["entries"]
        )
    status = str(stock.get("status") or "")
    wait_status = {"待查询", "查询中", "请提供料号", "寰呮煡璇?", "鏌ヨ涓?", "璇锋彁渚涙枡鍙?"}
    return bool(stock.get("ok") or stock.get("fetched_at") or (stock.get("c_code") and status and status not in wait_status))


def row_stock_is_error(row: dict[str, object]) -> bool:
    stock = row.get("stock")
    if isinstance(stock, dict) and isinstance(stock.get("entries"), list):
        return any(row_stock_is_error({"stock": item.get("stock")}) for item in stock["entries"])
    return bool(row_stock_already_queried(row) and isinstance(stock, dict) and not stock.get("ok"))


def row_stock_is_issue(row: dict[str, object]) -> bool:
    return row_stock_is_low(row) or row_stock_is_error(row)


def row_stock_is_normal(row: dict[str, object]) -> bool:
    stock = row.get("stock")
    if isinstance(stock, dict) and isinstance(stock.get("entries"), list):
        return bool(stock["entries"]) and all(
            row_stock_is_normal({"stock": item.get("stock")}) for item in stock["entries"]
        )
    return bool(
        not row_is_nc(row)
        and row_stock_already_queried(row)
        and isinstance(stock, dict)
        and stock.get("ok")
        and not row_stock_is_low(row)
    )


def row_filter_keys(row: dict[str, object]) -> set[str]:
    keys = {"all"}
    is_nc = row_is_nc(row)
    if row.get("field_changed_refs"):
        keys.add("needs_sync")
    else:
        keys.add("sync_done")
    if row_review_status(row) != "Confirmed":
        keys.add("pending_review")
    else:
        keys.add("review_confirmed")
    if (row_code_status(row) in {"ChangedToNewCode", "Mixed"}
            or row.get("manual_c_code") or row.get("stock_c_code")
            or any(item.get("decision") == "AcceptManual" for item in (row.get("per_ref_selection") or {}).values())):
        keys.add("changed_code")
    else:
        keys.add("code_unchanged")
    if not is_nc:
        if row_stock_is_issue(row):
            keys.add("stock_issue")
        elif row_stock_is_normal(row):
            keys.add("stock_normal")
    if row.get("missing_schematic_refs"):
        keys.add("missing_ref")
    else:
        keys.add("ref_complete")
    if is_nc:
        keys.add("nc_device")
    else:
        keys.add("non_nc")
    return keys


def row_is_pump_exportable(row: dict[str, object]) -> bool:
    has_any_code = bool(row_effective_c_code(row)) or any(selection_codes_for_row(row).values())
    return bool(
        has_any_code
        and not row_is_nc(row)
        and not all(item.get("decision") == "Reject" or item.get("review_status") == "Rejected"
                    for item in (row.get("per_ref_selection") or {}).values())
        and (not row.get("per_ref_selection") or any(
            item.get("decision") != "Reject" and item.get("review_status") != "Rejected"
            for item in row["per_ref_selection"].values()
        ))
    )


def saved_candidate_description(row: dict[str, object]) -> str:
    fields = (row.get("override") or {}).get("field_overrides") or {}
    code = row_effective_c_code(row)
    if code and fields.get("description_c_code") == code:
        return str(fields.get("description") or "").strip()
    return ""


def candidate_selection_fields(item: dict[str, object], refs: list[str]) -> dict[str, str]:
    item = {**item}
    for key in ("model", "package", "param", "param_detail"):
        if key in item:
            item[key] = html.unescape(re.sub(r"</?span\b[^>]*>", "", str(item[key]), flags=re.I))
    model = str(item.get("model") or "").strip()
    fields = {"comment": model}
    prefix = re.match(r"[A-Z]+", refs[0].upper())
    kind = prefix.group() if prefix else ""
    fields["comment"] = {"R": "resistor", "C": "capacitor", "L": "inductor"}.get(kind, model)
    passive = {
        "R": (r"阻值|电阻值|Resistance", r"(?:RES|RESISTOR)\s+([^\s]+)", "Ω"),
        "C": (r"容值|电容量|电容值|Capacitance", r"(?:CAP|CAPACITOR)\s+([^\s]+)", "F"),
        "L": (r"感值|电感量|电感值|Inductance", r"(?:IND|INDUCTOR)\s+([^\s]+)", "H"),
    }.get(kind)
    if passive:
        names, summary_pattern, unit = passive
        # Prefer named attributes; never decode an electrical rating from MPN digits.
        detail = str(item.get("param_detail") or "")
        summary = str(item.get("param") or "")
        match = re.search(rf"(?:^|[;；、\n])\s*(?:{names})\s*[:：]\s*([^;；、\n]+)",
                          detail + ";" + summary, re.IGNORECASE)
        if match:
            fields["value"] = match.group(1).strip()
        else:
            match = re.search(summary_pattern, summary, re.IGNORECASE)
            if match and re.fullmatch(rf"[\d.]+[fpnumkKMGTµμ]?(?:{unit}|ohms?)", match.group(1), re.IGNORECASE):
                fields["value"] = match.group(1).strip()
    else:
        fields["value"] = model
    # Keep the selected MPN even when Comment is a category or stays unchanged.
    fields["description"] = "\n".join(piece for piece in ("型号: " + model, stock_param_text(item)) if piece)
    fields["description_c_code"] = normalize_lcsc_c_code(item.get("c_code"))
    return fields


def row_schematic_sync_fields(row: dict[str, object]) -> dict[str, str]:
    fields = {
        "comment": str(row.get("comment") or "").strip(),
        "value": str(row.get("value") or "").strip(),
        "footprint": str(row.get("footprint") or "").strip(),
    }
    c_code = row_effective_c_code(row)
    if c_code and not row.get("selection_codes_mixed"):
        fields["lcsc_c_code"] = c_code
        fields["lcsc_url"] = lcsc_code_url(c_code)
    description = saved_candidate_description(row) or stock_param_text(row.get("stock"))
    if description and not row.get("selection_codes_mixed"):
        fields["description"] = description
    if not row.get("selection_mixed"):
        fields.update(alternate_parts_to_schematic_fields(row.get("alternate_parts")))
        fields.update(procurement_choice_to_schematic_fields(row.get("procurement_choice")))
    return fields


def first_ref_prefix(row: dict[str, object]) -> str:
    refs = row.get("refs_list") or split_refs(row.get("refs", ""))
    first_ref = str(refs[0] if refs else row.get("ref", "")).strip().upper()
    match = re.match(r"^([A-Z]+)", first_ref)
    return match.group(1) if match else ""


def component_category_rank(row: dict[str, object]) -> int:
    comment = str(row.get("comment") or "").upper()
    value = str(row.get("value") or "").upper()
    ref_prefix = first_ref_prefix(row)
    haystack = f"{comment} {value}"
    if ref_prefix == "C" or "CAPACITOR" in haystack:
        return 0
    if ref_prefix == "R" or "RESISTOR" in haystack:
        return 1
    if ref_prefix == "L" or "INDUCTOR" in haystack:
        return 2
    chip_words = ("IC", "AMPLIFIER", "ADC", "DAC", "REGULATOR", "SWITCH", "DIODE", "TRANSISTOR")
    if ref_prefix in {"U", "Q", "D"} or any(word in haystack for word in chip_words):
        return 3
    return 4


def normalized_value_number(value: object, category_rank: int) -> float:
    text = (
        str(value or "")
        .upper()
        .replace("\u00b5", "U")
        .replace("\u03bc", "U")
        .replace("\u03a9", "R")
        .replace("OHM", "R")
    )
    text = re.sub(r"\s+", "", text)
    if not text:
        return float("inf")

    embedded = re.match(r"^(\d+)([RKM])(\d+)", text)
    if category_rank == 1 and embedded:
        number = float(f"{embedded.group(1)}.{embedded.group(3)}")
        unit = embedded.group(2)
    else:
        match = re.search(r"(\d+(?:\.\d+)?|\.\d+)", text)
        if not match:
            return float("inf")
        number = float(match.group(1))
        unit_match = re.match(r"([A-Z]+)", text[match.end() :])
        unit = unit_match.group(1) if unit_match else ""

    if category_rank == 0:
        multipliers = {"PF": 1e-12, "NF": 1e-9, "UF": 1e-6, "MF": 1e-3, "F": 1.0}
    elif category_rank == 1:
        multipliers = {"MR": 1e-3, "R": 1.0, "K": 1e3, "KOHM": 1e3, "M": 1e6, "MOHM": 1e6}
    elif category_rank == 2:
        multipliers = {"NH": 1e-9, "UH": 1e-6, "MH": 1e-3, "H": 1.0}
    else:
        multipliers = {}
    multiplier = next((scale for suffix, scale in multipliers.items() if unit.startswith(suffix)), 1.0)
    return number * multiplier


def footprint_sort_key(value: object) -> tuple[int, int, str]:
    text = str(value or "").strip().upper()
    match = re.fullmatch(r"0*(\d+)", text)
    if match:
        return (0, int(match.group(1)), text)
    first_number = re.search(r"\d+", text)
    if first_number:
        return (1, int(first_number.group(0)), text)
    return (2, 0, text)


def bom_row_sort_key(row: dict[str, object]) -> tuple[object, ...]:
    category = component_category_rank(row)
    return (
        category,
        normalized_value_number(row.get("value"), category),
        str(row.get("value") or "").upper(),
        footprint_sort_key(row.get("footprint")),
        ref_sort_key(str(row.get("ref") or "")),
    )


def sort_bom_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    rows.sort(key=bom_row_sort_key)
    return rows


def _timestamp_not_after(left: object, right: object) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not right_text:
        return False
    if not left_text:
        return True

    def timestamp_parts(value: str) -> tuple[int, ...] | None:
        pieces = [int(piece) for piece in re.findall(r"\d+", value)[:6]]
        if len(pieces) < 3:
            return None
        return tuple([*pieces, *([0] * (6 - len(pieces)))])

    left_parts = timestamp_parts(left_text)
    right_parts = timestamp_parts(right_text)
    if left_parts is None or right_parts is None:
        return False
    return left_parts <= right_parts


def reconcile_schematic_field_overrides(
    parts,
    overrides: dict[str, dict[str, object]],
    actual_by_ref: dict[str, dict[str, str]],
) -> tuple[bool, dict[str, dict[str, object]]]:
    """Adopt one-sided KiCad edits while preserving pending web edits and conflicts."""
    changed = False
    states: dict[str, dict[str, object]] = {}
    detected_at = datetime.now().isoformat(timespec="seconds")
    for part in parts:
        ref = str(getattr(part, "ref", "") or "").strip().upper()
        actual = actual_by_ref.get(ref)
        if not ref or actual is None:
            continue
        current = dict(overrides.get(ref, {}))
        desired = effective_part_fields(part, current)
        baseline = normalize_schematic_field_snapshot(current.get("schematic_field_snapshot"))
        existing_fields = current.get("field_overrides", {})
        field_overrides = dict(existing_fields) if isinstance(existing_fields, dict) else {}
        external_updates: dict[str, dict[str, str]] = {}
        conflicts: dict[str, dict[str, str]] = {}

        for field in EDITABLE_FIELDS:
            actual_value = str(actual.get(field, "") or "").strip()
            desired_value = desired[field]
            if actual_value == desired_value:
                continue

            if field in baseline:
                baseline_value = baseline[field]
                web_changed = desired_value != baseline_value
                schematic_changed = actual_value != baseline_value
                if schematic_changed and not web_changed:
                    external_updates[field] = {
                        "old": baseline_value,
                        "new": actual_value,
                    }
                elif schematic_changed and web_changed:
                    conflicts[field] = {
                        "baseline": baseline_value,
                        "web": desired_value,
                        "schematic": actual_value,
                    }
                continue

            # Compatibility for overrides written before snapshots existed. A field
            # override older than the last successful sync is already on disk, so a
            # later mismatch is treated as an external KiCad edit.
            if _timestamp_not_after(current.get("field_updated_at"), current.get("schematic_synced_at")):
                external_updates[field] = {
                    "old": desired_value,
                    "new": actual_value,
                }

        if external_updates:
            for field, values in external_updates.items():
                new_value = values["new"]
                original_value = str(getattr(part, field, "") or "").strip()
                if new_value == original_value:
                    field_overrides.pop(field, None)
                else:
                    field_overrides[field] = new_value
            if field_overrides:
                current["field_overrides"] = field_overrides
            else:
                current.pop("field_overrides", None)
            current["schematic_field_snapshot"] = {
                **baseline,
                **{field: values["new"] for field, values in external_updates.items()},
            }
            current["last_external_schematic_update"] = "; ".join(
                f"{field}: {values['old']} -> {values['new']}"
                for field, values in external_updates.items()
            )
            current["last_external_schematic_update_at"] = detected_at
            current["updated_at"] = detected_at
            overrides[ref] = current
            changed = True

        if external_updates or conflicts:
            states[ref] = {
                "external_updates": external_updates,
                "conflicts": conflicts,
            }
    return changed, states


def enrich_rows_with_schematic_sync_state(
    rows: list[dict[str, object]],
    schematic_dir: Path,
    actual_by_ref: dict[str, dict[str, str]] | None = None,
    reconciliation_by_ref: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    refs = sorted(
        {
            str(ref or "").strip().upper()
            for row in rows
            for ref in row.get("refs_list", [])
            if str(ref or "").strip()
        },
        key=ref_sort_key,
    )
    if actual_by_ref is None:
        actual_by_ref = read_schematic_symbol_field_values(schematic_dir, refs, SCHEMATIC_SYNC_FIELDS)
    reconciliation_by_ref = reconciliation_by_ref or {}
    if not actual_by_ref:
        return rows

    for row in rows:
        desired = row_schematic_sync_fields(row)
        row["schematic_fields_by_ref"] = {
            ref: actual_by_ref[ref] for ref in row.get("refs_list", []) if ref in actual_by_ref
        }
        per_ref_desired = row.get("per_ref_sync_fields") or {}
        changed_refs: set[str] = set()
        missing_refs: list[str] = []
        field_diff_refs: dict[str, list[str]] = {}
        field_old_values: dict[str, list[str]] = {}
        field_diffs_by_ref: dict[str, dict[str, dict[str, str]]] = {}
        external_updates: dict[str, dict[str, dict[str, str]]] = {}
        conflicts: dict[str, dict[str, dict[str, str]]] = {}
        for ref in [str(item or "").strip().upper() for item in row.get("refs_list", []) if str(item or "").strip()]:
            reconciliation = reconciliation_by_ref.get(ref, {})
            if reconciliation.get("external_updates"):
                external_updates[ref] = reconciliation["external_updates"]
            if reconciliation.get("conflicts"):
                conflicts[ref] = reconciliation["conflicts"]
            actual = actual_by_ref.get(ref)
            if actual is None:
                missing_refs.append(ref)
                continue
            ref_desired = dict(per_ref_desired.get(ref) or desired)
            for field, desired_value in ref_desired.items():
                if field not in SCHEMATIC_SYNC_FIELDS:
                    continue
                actual_value = str(actual.get(field, "") or "").strip()
                if actual_value == desired_value:
                    continue
                # A merged row must not invent a change to an already saved,
                # equivalent per-reference description just to unify its formatting.
                if (field == "description" and actual_value and desired_value
                        and actual_value == row.get("description_by_ref", {}).get(ref)
                        and actual.get("lcsc_c_code") == ref_desired.get("lcsc_c_code")
                        and saved_parameter_signature(actual_value, str(row.get("footprint") or ""))
                        == saved_parameter_signature(desired_value, str(row.get("footprint") or ""))):
                    continue
                changed_refs.add(ref)
                field_diff_refs.setdefault(field, []).append(ref)
                if actual_value not in field_old_values.setdefault(field, []):
                    field_old_values[field].append(actual_value)
                field_diffs_by_ref.setdefault(ref, {})[field] = {
                    "old": actual_value,
                    "new": desired_value,
                }

        if missing_refs:
            row["missing_schematic_refs"] = sorted_ref_text(missing_refs)
        if external_updates:
            row["external_schematic_updates"] = external_updates
            row["external_schematic_update_refs"] = sorted_ref_text(list(external_updates))
        if conflicts:
            row["field_conflicts"] = conflicts
            row["field_conflict_refs"] = sorted_ref_text(list(conflicts))

        if not changed_refs:
            continue
        row["field_changed_refs"] = sorted_ref_text(changed_refs)
        row["field_edit_status"] = "NeedsSchematicUpdate"
        field_diffs = dict(row.get("field_diffs") or {})
        for field, diff_refs in field_diff_refs.items():
            field_diffs[field] = {
                "old": "; ".join(field_old_values.get(field, [])),
                "new": desired.get(field, ""),
                "refs": sorted_ref_text(diff_refs),
            }
        row["field_diffs"] = field_diffs
        row["field_diffs_by_ref"] = field_diffs_by_ref
    return rows


def effective_part_fields(part, override: dict[str, object] | None) -> dict[str, str]:
    fields = {key: str(getattr(part, key, "") or "") for key in EDITABLE_FIELDS}
    override = override or {}
    field_overrides = override.get("field_overrides", {})
    if isinstance(field_overrides, dict):
        for key in EDITABLE_FIELDS:
            if key in field_overrides:
                fields[key] = str(field_overrides.get(key) or "").strip()
    return fields


def field_change_flags(part, override: dict[str, object] | None) -> dict[str, bool]:
    fields = effective_part_fields(part, override)
    return {key: fields[key] != str(getattr(part, key, "") or "") for key in EDITABLE_FIELDS}


def part_to_dict(part, overrides: dict[str, dict[str, object]]) -> dict[str, object]:
    row = asdict(part)
    override = overrides.get(part.ref, {})
    fields = effective_part_fields(part, override)
    row.update(fields)
    row["original_comment"] = part.comment
    row["original_value"] = part.value
    row["original_footprint"] = part.footprint
    row["keyword"] = " ".join(item for item in [fields["value"], fields["footprint"], fields["comment"]] if item)
    row["value_search_url"] = lcsc_search_url(fields["value"]) if fields["value"] else ""
    row["comment_search_url"] = lcsc_search_url(fields["comment"]) if fields["comment"] else ""
    vf_keyword = " ".join(item for item in [fields["value"].strip(), fields["footprint"].strip()] if item)
    row["value_footprint_search_url"] = lcsc_search_url(vf_keyword) if vf_keyword else ""
    c_keyword = part.accepted_c_code or part.lcsc_match_c_code
    row["c_code_search_url"] = lcsc_search_url(c_keyword) if c_keyword else ""
    row["kicad_jump_url"] = kicad_jump_url(part.ref, part.page)
    row["override"] = override
    row["effective_c_code"] = choose_effective_c_code(
        {"accepted_c_code": part.accepted_c_code, "lcsc_match_c_code": part.lcsc_match_c_code},
        override,
    )
    row["note"] = override.get("note", "")
    row["decision"] = override.get("decision", "")
    row["manual_c_code"] = override.get("manual_c_code", "")
    row["stock_c_code"] = override.get("stock_c_code", "")
    row["schematic_synced_at"] = override.get("schematic_synced_at", "")
    row["schematic_sync_summary"] = override.get("schematic_sync_summary", "")
    row["alternate_parts"] = part_alternate_parts(part, override)
    row["schematic_alternate_parts"] = alternate_parts_from_schematic(part.lcsc_alt, part.lcsc_alt_info)
    row["procurement_choice"] = part_procurement_choice(part, override)
    row["schematic_procurement_choice"] = procurement_choice_from_schematic(
        part.procurement_url, part.procurement_info
    )
    return row


def ref_sort_key(ref: str) -> tuple[str, int, str]:
    import re

    match = re.match(r"^([A-Z]+)(\d+)([A-Z]?)$", ref)
    if not match:
        return (ref, 0, "")
    return (match.group(1), int(match.group(2)), match.group(3))


def sorted_ref_text(refs: list[str]) -> str:
    return ",".join(sorted(set(refs), key=ref_sort_key))


def page_refs_text(page_refs: dict[str, list[str]]) -> str:
    return "; ".join(f"{page}: {sorted_ref_text(refs)}" for page, refs in sorted(page_refs.items()))


def saved_parameter_signature(description: str, footprint: str) -> str:
    """Ignore presentation differences, never unknown requirements or ratings."""
    attributes = []
    prose = []
    presentation_only = {
        "Tape & Reel (TR)", "圆盘", "贴片电容(MLCC)",
        "Multilayer Ceramic Capacitors MLCC - SMD/SMT",
    }
    for line in description.splitlines():
        for piece in re.split(r"[;；]", line):
            piece = piece.strip()
            if not piece or piece in presentation_only:
                continue
            pair = re.fullmatch(r"([^:：]+)[:：]\s*(.+)", piece)
            if pair:
                attributes.append((pair[1].strip(), pair[2].strip()))
            else:
                prose.append(piece)
    # Some sources repeat the named capacitor ratings in an English headline.
    # Discard it only when EVERY token is already represented by an attribute.
    ratings = {value for name, value in attributes if name in {
        "容值", "电容量", "电容值", "额定电压", "精度", "温度系数",
    }}
    package = footprint.rsplit(":", 1)[-1]
    known_tokens = ratings | ({package} if re.fullmatch(r"\d{4}", package) else set())
    residual = []
    for line in prose:
        if line.startswith("CAP CER ") and ratings and all(
            token in known_tokens for token in line[len("CAP CER "):].split()
        ):
            continue
        residual.append(line)
    return json.dumps([sorted(set(attributes)), sorted(set(residual))], ensure_ascii=False)


def grouped_note(refs: list[str], notes: dict[str, str]) -> str:
    distinct = set(notes.values())
    if len(distinct) == 1 and len(notes) == len(refs):
        return next(iter(distinct))
    return "\n".join(f"{ref}: {notes[ref]}" for ref in sorted(refs, key=ref_sort_key) if ref in notes)


def preserve_unchanged_notes(update: dict, refs: list[str], overrides: dict) -> None:
    if "note" not in update or len(refs) < 2:
        return
    notes = {ref: str(overrides.get(ref, {}).get("note"))
             for ref in refs if overrides.get(ref, {}).get("note")}
    if str(update["note"]).strip() == grouped_note(refs, notes).strip():
        update.pop("note")


def selection_code(part, override: dict) -> str:
    if override.get("decision") in {"ClearSelection", "Reject", "NoChange", "DNP"}:
        return ""
    return str(override.get("stock_c_code") or choose_effective_c_code(asdict(part), override)).strip()


def row_selection_updates(parts, overrides: dict[str, dict]) -> dict[str, dict]:
    """Resolve only row-level main codes and confirmation; retain per-ref metadata."""
    groups: dict[tuple[str, str, str], list] = {}
    for part in parts:
        fields = effective_part_fields(part, overrides.get(part.ref, {}))
        groups.setdefault(tuple(fields[key] for key in EDITABLE_FIELDS), []).append(part)
    updates = {}
    for members in groups.values():
        codes = {selection_code(part, overrides.get(part.ref, {})) for part in members}
        reviews = {row_review_status({"override": overrides.get(part.ref, {})}) for part in members}
        nonempty = codes - {""}
        code_update = None
        if len(nonempty) > 1:
            code_update = {"accepted_c_code": "", "manual_c_code": "", "stock_c_code": "",
                           "decision": "ClearSelection", "status": ""}
        elif len(codes) > 1:
            code = next(iter(nonempty))
            code_update = {"accepted_c_code": code, "manual_c_code": code, "stock_c_code": code,
                           "decision": "AcceptManual", "status": "ChangedToNewCode"}
        if code_update is not None or len(reviews) > 1:
            for part in members:
                updates[part.ref] = {**(code_update or {}), "review_status": "Pending"}
    return updates


def edited_row_merge_updates(parts, overrides: dict[str, dict], refs: list[str], fields: dict, stock_cache=None):
    """Inherit an unambiguous, pre-existing destination only on an explicit key edit."""
    selected = set(refs)
    destinations = {}
    incoming = {}
    for part in parts:
        before = effective_part_fields(part, overrides.get(part.ref, {}))
        key = tuple(before[name] for name in EDITABLE_FIELDS)
        if part.ref not in selected:
            destinations.setdefault(key, []).append(part)
        else:
            after = {**before, **fields}
            new_key = tuple(after[name] for name in EDITABLE_FIELDS)
            if new_key != key:
                incoming.setdefault(new_key, []).append(part)
    updates, merges = {}, []
    for key, members in incoming.items():
        targets = destinations.get(key)
        if not targets:
            continue
        target = group_parts_for_review(targets, overrides)[0]
        code = target["effective_c_code"]
        codes = {selection_code(part, overrides.get(part.ref, {})) for part in targets} - {""}
        if len(codes) > 1 or not (code or target["procurement_choice"]) or (not code and target["selection_mixed"]):
            continue
        descriptions = [saved_candidate_description({"override": overrides.get(part.ref, {}), "effective_c_code": code})
                        for part in targets]
        description = max(descriptions, key=len, default="") or stock_param_text((stock_cache or {}).get(code)) or target["description"]
        selection = {
            "accepted_c_code": code, "manual_c_code": code, "stock_c_code": code,
            "decision": "AcceptManual" if code else "ClearSelection",
            "status": target["status"], "review_status": target["review_status"],
            "alternate_parts": target["alternate_parts"], "procurement_choice": target["procurement_choice"],
            "field_overrides": {"description": description, "description_c_code": code},
        }
        if target["review_status"] == "Confirmed":
            selection["review_confirmed_at"] = max(
                (str(overrides.get(part.ref, {}).get("review_confirmed_at") or "") for part in targets), default="")
        for part in members:
            updates[part.ref] = selection
        merges.append({"refs": [part.ref for part in members], "target_refs": target["refs_list"], "c_code": code})
    return updates, merges


def group_parts_for_review(parts, overrides: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    parts = list(parts)
    overrides = dict(overrides)
    for ref, update in row_selection_updates(parts, overrides).items():
        overrides[ref] = {**overrides.get(ref, {}), **update}
    grouped: OrderedDict[tuple[str, str, str], dict[str, object]] = OrderedDict()
    for part in parts:
        override = overrides.get(part.ref, {})
        fields = effective_part_fields(part, override)
        changes = field_change_flags(part, override)
        alternate_parts = part_alternate_parts(part, override)
        schematic_alternate_parts = alternate_parts_from_schematic(part.lcsc_alt, part.lcsc_alt_info)
        procurement_choice = part_procurement_choice(part, override)
        schematic_procurement_choice = procurement_choice_from_schematic(
            part.procurement_url, part.procurement_info
        )
        effective_code = selection_code(part, override)
        key = (
            fields["comment"],
            fields["value"],
            fields["footprint"],
        )
        group = grouped.setdefault(
            key,
            {
                "comment": fields["comment"],
                "footprint": fields["footprint"],
                "value": fields["value"],
                "description_values": [],
                "description_by_ref": {},
                "notes_by_ref": {},
                "per_ref_selection": {},
                "per_ref_sync_fields": {},
                "original_comment_values": [],
                "original_value_values": [],
                "original_footprint_values": [],
                "field_changed_refs": [],
                "field_diff_data": {key: {"old_values": [], "refs": []} for key in EDITABLE_FIELDS},
                "refs_list": [],
                "page_ref_map": {},
                "ref_page_map": {},
                "accepted_codes": [],
                "lcsc_codes": [],
                "alternate_parts": alternate_parts,
                "schematic_alternate_parts": schematic_alternate_parts,
                "procurement_choice": procurement_choice,
                "schematic_procurement_choice": schematic_procurement_choice,
                "parts": [],
            },
        )
        group["refs_list"].append(part.ref)
        group["page_ref_map"].setdefault(part.page or "Unknown", []).append(part.ref)
        group["ref_page_map"][part.ref] = part.page
        description = saved_candidate_description({
            "override": override,
            "effective_c_code": effective_code,
        }) or part.description
        group["description_by_ref"][part.ref] = description
        group["per_ref_selection"][part.ref] = {
            "effective_c_code": effective_code,
            "accepted_c_code": part.accepted_c_code,
            "lcsc_match_c_code": part.lcsc_match_c_code,
            "decision": str(override.get("decision") or ""),
            "status": str(override.get("status") or ""),
            "review_status": row_review_status({"override": override}),
            "review_confirmed_at": str(override.get("review_confirmed_at") or ""),
            "description": description,
            "note": str(override.get("note") or ""),
            "alternate_parts": alternate_parts,
            "procurement_choice": procurement_choice,
        }
        per_ref_row = {
            "comment": fields["comment"], "value": fields["value"],
            "footprint": fields["footprint"], "effective_c_code": effective_code,
            "accepted_c_code": part.accepted_c_code, "lcsc_match_c_code": part.lcsc_match_c_code,
            "override": override, "stock": {},
            "alternate_parts": alternate_parts, "procurement_choice": procurement_choice,
        }
        group["per_ref_sync_fields"][part.ref] = row_schematic_sync_fields(per_ref_row)
        if override.get("note"):
            group["notes_by_ref"][part.ref] = str(override["note"])
        if description and description not in group["description_values"]:
            group["description_values"].append(description)
        for field_name, attr_name in (
            ("original_comment_values", "comment"),
            ("original_value_values", "value"),
            ("original_footprint_values", "footprint"),
        ):
            original_value = str(getattr(part, attr_name, "") or "")
            if original_value and original_value not in group[field_name]:
                group[field_name].append(original_value)
        if any(changes.values()):
            group["field_changed_refs"].append(part.ref)
        for field_name, changed in changes.items():
            if not changed:
                continue
            diff_data = group["field_diff_data"][field_name]
            original_value = str(getattr(part, field_name, "") or "")
            if original_value and original_value not in diff_data["old_values"]:
                diff_data["old_values"].append(original_value)
            diff_data["refs"].append(part.ref)
        if part.accepted_c_code and part.accepted_c_code not in group["accepted_codes"]:
            group["accepted_codes"].append(part.accepted_c_code)
        if part.lcsc_match_c_code and part.lcsc_match_c_code not in group["lcsc_codes"]:
            group["lcsc_codes"].append(part.lcsc_match_c_code)
        group["parts"].append(part)

    rows: list[dict[str, object]] = []
    for idx, group in enumerate(grouped.values(), start=1):
        parts_in_group = group["parts"]
        primary = parts_in_group[0]
        refs = sorted_ref_text(group["refs_list"])
        ref_list = refs.split(",") if refs else []
        ref_links = [
            {
                "ref": ref,
                "page": group["ref_page_map"].get(ref, ""),
                "url": kicad_jump_url(ref, group["ref_page_map"].get(ref, "")),
            }
            for ref in ref_list
        ]
        accepted_code = ",".join(group["accepted_codes"])
        lcsc_code = ",".join(group["lcsc_codes"])
        effective_codes = list(dict.fromkeys(
            item["effective_c_code"] for item in group["per_ref_selection"].values()
        ))
        has_partial_code = (
            any(item["effective_c_code"] for item in group["per_ref_selection"].values())
            and any(not item["effective_c_code"] for item in group["per_ref_selection"].values())
        )
        codes_disagree = len(effective_codes) > 1
        selection_signatures = {
            json.dumps({key: item.get(key) for key in (
                "effective_c_code", "review_status", "alternate_parts", "procurement_choice"
            )}, ensure_ascii=False, sort_keys=True)
            for item in group["per_ref_selection"].values()
        }
        effective_code = effective_codes[0] if len(effective_codes) == 1 else ""
        review_statuses = {item["review_status"] for item in group["per_ref_selection"].values()}
        review_status_mixed = len(review_statuses) > 1
        aggregate_review_status = (
            next(iter(review_statuses))
            if len(review_statuses) == 1 and not review_status_mixed and not codes_disagree and not has_partial_code
            else "Pending"
        )
        first_override = next((overrides.get(ref, {}) for ref in ref_list if overrides.get(ref)), {})
        notes = group["notes_by_ref"]
        note = grouped_note(ref_list, notes)
        # Equivalent parameter summaries share a row; preserve the originals for audit.
        descriptions = group["description_values"]
        description = max(descriptions, key=len, default="") if effective_code else "; ".join(descriptions)
        field_diffs = {
            key: {
                "old": "; ".join(data["old_values"]),
                "new": str(group[key]),
                "refs": sorted_ref_text(data["refs"]),
            }
            for key, data in group["field_diff_data"].items()
            if data["refs"]
        }
        row = {
            "group_id": f"G{idx}",
            "ref": primary.ref,
            "refs": refs,
            "refs_list": ref_list,
            "ref_links": ref_links,
            "page": primary.page,
            "page_refs": page_refs_text(group["page_ref_map"]),
            "quantity": len(ref_list),
            "comment": group["comment"],
            "value": group["value"],
            "footprint": group["footprint"],
            "description": description,
            "description_by_ref": dict(group["description_by_ref"]),
            "notes_by_ref": dict(notes),
            "original_comment_values": "; ".join(group["original_comment_values"]),
            "original_value_values": "; ".join(group["original_value_values"]),
            "original_footprint_values": "; ".join(group["original_footprint_values"]),
            "field_changed_refs": sorted_ref_text(group["field_changed_refs"]),
            "field_diffs": field_diffs,
            "field_edit_status": "NeedsSchematicUpdate" if group["field_changed_refs"] else "",
            "accepted_c_code": accepted_code,
            "lcsc_match_c_code": lcsc_code,
            "override": first_override,
            "note": note,
            "decision": first_override.get("decision", ""),
            "manual_c_code": first_override.get("manual_c_code", ""),
            "stock_c_code": first_override.get("stock_c_code", ""),
            "schematic_synced_at": first_override.get("schematic_synced_at", ""),
            "schematic_sync_summary": first_override.get("schematic_sync_summary", ""),
            "status": first_override.get("status", ""),
            "review_status": aggregate_review_status,
            "review_statuses": sorted(review_statuses),
            "review_status_mixed": review_status_mixed,
            "per_ref_selection": dict(group["per_ref_selection"]),
            "per_ref_sync_fields": dict(group["per_ref_sync_fields"]),
            "selection_mixed": len(selection_signatures) > 1 or review_status_mixed,
            "selection_codes_mixed": codes_disagree or has_partial_code,
            "selection_codes": list(dict.fromkeys(code for code in effective_codes if code)),
            "alternate_parts": normalize_alternate_parts(group["alternate_parts"]),
            "schematic_alternate_parts": normalize_alternate_parts(group["schematic_alternate_parts"]),
            "alternate_conflict": alternate_parts_signature(group["alternate_parts"])
            != alternate_parts_signature(group["schematic_alternate_parts"]),
            "procurement_choice": normalize_procurement_choice(group["procurement_choice"]),
            "schematic_procurement_choice": normalize_procurement_choice(
                group["schematic_procurement_choice"]
            ),
            "procurement_conflict": procurement_choice_signature(group["procurement_choice"])
            != procurement_choice_signature(group["schematic_procurement_choice"]),
            "keyword": " ".join(item for item in [str(group["value"]).strip(), str(group["footprint"]).strip()] if item),
            "value_search_url": lcsc_search_url(str(group["value"])) if group["value"] else "",
            "comment_search_url": lcsc_search_url(str(group["comment"])) if group["comment"] else "",
            "value_footprint_search_url": lcsc_search_url(
                " ".join(item for item in [str(group["value"]).strip(), str(group["footprint"]).strip()] if item)
            ),
            "c_code_search_url": lcsc_search_url(effective_code) if effective_code else "",
            "kicad_jump_url": kicad_jump_url(primary.ref, primary.page),
            "effective_c_code": effective_code,
        }
        if not row["selection_mixed"]:
            row.update(alternate_parts_to_schematic_fields(row["alternate_parts"]))
            row.update(procurement_choice_to_schematic_fields(row["procurement_choice"]))
        row["code_status"] = row_code_status(row)
        rows.append(row)
    return rows


class PartSelectionApp:
    def __init__(self, config_path: Path):
        self.config = load_config(config_path)
        apply_runtime_settings(self.config)
        self.settings_gate = SettingsGate()
        self.lifecycle = None
        self.project_root = self.config["project_root"]
        self.bom_dir = self.config["bom_dir"]
        self.overrides_path = self.config["output_overrides"]
        self.stock_cache_path = self.config["stock_cache"]
        self.local_inventory_path = self.config["local_inventory"]
        self.search_jobs = CandidateSearchJobs(self.lcsc_search, self.cached_search_seed)
        self.selection_jobs = SelectionJobs(self.select_candidate)
        self.footprint_choices = FootprintChoices(self.project_root, self.config.get("converter_executable"))
        self.selection_save_lock = threading.RLock()
        self.inventory_previews = OrderedDict()

    def save_path_settings(self, payload):
        with self.settings_gate.editing(), self.selection_save_lock, OVERRIDES_UPDATE_LOCK:
            if self.search_jobs.busy() or self.selection_jobs.busy():
                raise ValueError("搜索或转换尚未完成，请结束任务后保存路径配置")
            result = save_settings(self.config, payload)
            updated = result.pop('config')
            # Runtime processes keep their current cache/home until a fresh launch.
            for key in ('runtime_cache', 'desktop_home'):
                if key in self.config:
                    updated[key] = self.config[key]
                else:
                    updated.pop(key, None)
            self.config = updated
            self.bom_dir = updated['bom_dir']
            self.overrides_path = updated['output_overrides']
            self.stock_cache_path = updated['stock_cache']
            self.local_inventory_path = updated['local_inventory']
            self.inventory_previews.clear()
            self.footprint_choices = FootprintChoices(self.project_root, updated.get('converter_executable'))
            result['settings'] = settings_snapshot(self.config)
            return result

    def source_bom(self) -> Path:
        return self.config["source_bom"]

    def preview_local_inventory(self, payload: dict[str, object]) -> dict[str, object]:
        source = Path(str(payload.get("path") or "")).expanduser()
        if not source.is_absolute():
            raise ValueError("请选择库存 Excel 文件")
        source = source.resolve()
        if not source.is_file() or source.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('请选择不超过 20 MB 的库存 Excel 文件')
        fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
        preview = parse_inventory_workbook(source, str(payload.get("sheet") or ""))
        if hashlib.sha256(source.read_bytes()).hexdigest() != fingerprint:
            raise ValueError("文件已改变，请重新预览")
        with self.selection_save_lock:
            existing = load_inventory(self.local_inventory_path)
            explicit_matches = payload.get('matches', {})
            if not isinstance(explicit_matches, dict):
                raise ValueError('库存匹配必须为对象')
            owners = inventory_source_owners(existing)
            matches = {key: owners[key] for key in preview.get('unmatched', {}) if key in owners}
            for key, code in explicit_matches.items():
                if key in owners and str(code).upper() != owners[key]:
                    raise ValueError(f'该库存已绑定 {owners[key]}，不能重复归属其他 C 编码')
            matches.update(explicit_matches)
            preview = apply_inventory_matches(preview, matches)
            token = uuid4().hex
            counts = {"added": 0, "updated": 0, "repeated": 0}
            for code, item in preview['items'].items():
                old = existing['items'].get(code)
                item['previous_quantity'] = old.get('quantity') if old else None
                if item['status'] != 'ready':
                    item['action'] = '待核实'
                else:
                    action = 'added' if not old else 'repeated' if old.get('quantity') == item['quantity'] and old.get('status', 'ready') == 'ready' else 'updated'
                    item['action'] = {'added': '新增', 'updated': '更新', 'repeated': '重复'}[action]
                    counts[action] += 1
            preview['summary'].update(counts)
            self.inventory_previews[token] = {'preview': preview, 'source': source,
                'fingerprint': fingerprint, 'inventory': existing, 'created': time.monotonic()}
            while len(self.inventory_previews) > 8:
                self.inventory_previews.popitem(last=False)
        return {'ok': True, **preview, 'preview_id': token}

    def import_local_inventory(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("confirmed") is not True:
            raise ValueError("导入前请先在预览中确认")
        with self.selection_save_lock:
            saved = self.inventory_previews.get(str(payload.get('preview_id') or ''))
            if not saved or time.monotonic() - saved['created'] > 600:
                raise ValueError("预览已失效，请重新预览后确认")
            if hashlib.sha256(saved['source'].read_bytes()).hexdigest() != saved['fingerprint']:
                raise ValueError("源文件已改变，请重新预览")
            preview = saved['preview']
            if not preview['items'] and not preview.get('unmatched'):
                raise ValueError("没有可导入的库存记录")
            existing = load_inventory(self.local_inventory_path)
            if existing != saved['inventory']:
                raise ValueError("库存已被其他窗口修改，请重新预览")
            merged = merge_inventory_records(existing, preview)
            stamp = inventory_import_time()
            for code in preview['items']:
                merged['items'][code].update({'source_name': preview['source_name'],
                                             'source_sheet': preview['sheet'], 'imported_at': stamp})
            for key in preview.get('unmatched', {}):
                merged['unmatched'][key].update({'source_name': preview['source_name'],
                                                'source_sheet': preview['sheet'], 'imported_at': stamp})
            before = existing.get("items", {})
            added = updated = repeated = 0
            for code, item in preview["items"].items():
                if item.get("status") != "ready":
                    continue
                old = before.get(code)
                if not old:
                    added += 1
                elif old.get("quantity") == item.get("quantity"):
                    repeated += 1
                else:
                    updated += 1
            save_inventory(self.local_inventory_path, merged)
            self.inventory_previews.pop(str(payload['preview_id']), None)
        return {"ok": True, "path": str(self.local_inventory_path),
                "summary": {"added": added, "updated": updated, "repeated": repeated,
                            "unresolved": preview["summary"]["unresolved"], "invalid": preview["summary"]["invalid"],
                            "unmatched": preview['summary']['unmatched']}}

    def enrich_local_inventory(self, rows):
        try:
            saved = load_inventory(self.local_inventory_path)
            inventory, unmatched = saved['items'], saved.get('unmatched', {})
            issue = ''
        except (ValueError, OSError) as error:
            inventory, unmatched, issue = {}, {}, f'本地库存读取失败：{error}'
        totals = {}
        for row in rows:
            code = row_effective_c_code(row)
            if code and not row.get('selection_codes_mixed') and not row_is_nc(row):
                totals[code] = totals.get(code, 0) + int(row.get('quantity') or 0)
        for row in rows:
            code = row_effective_c_code(row)
            total = totals.get(code, int(row.get('quantity') or 0))
            result = calculate_inventory_status(inventory.get(code), total, 3)
            result.update({'c_code': code, 'row_quantity': int(row.get('quantity') or 0),
                           'issue': issue or inventory.get(code, {}).get('issue', '')})
            if issue or row.get('selection_codes_mixed'):
                result.update({'status': 'unresolved', 'inventory': None, 'difference': None})
            if row_is_nc(row):
                result['status'] = 'nc'
            model = str(row.get('comment') or '').strip().upper()
            result['model_suggestions'] = [key for key, item in inventory.items()
                if key != code and model and model == str(item.get('model') or '').strip().upper()]
            result['pending_model_inventory'] = [item for item in unmatched.values()
                if model and model == str(item.get('model') or '').strip().upper()]
            row['local_inventory'] = result

    def schematic_parts(self):
        options = {"field_mapping": self.config["field_mapping"]} if self.config.get("field_mapping") else {}
        return load_schematic_parts(self.config["schematic_dir"], **options)

    def read_schematic_fields(self, *args):
        options = {"field_mapping": self.config["field_mapping"]} if self.config.get("field_mapping") else {}
        return read_schematic_symbol_field_values(*args, **options)

    def write_schematic_fields(self, *args):
        options = {"field_mapping": self.config["field_mapping"]} if self.config.get("field_mapping") else {}
        return update_schematic_symbol_field_map(*args, **options)

    def is_schematic_source(self, selected_bom: str = "") -> bool:
        return (selected_bom == SCHEMATIC_SOURCE_BOM if selected_bom
                else self.source_bom().name == SCHEMATIC_SOURCE_BOM)

    def resolve_bom_path(self, selected_bom: str = "") -> Path:
        if self.is_schematic_source(selected_bom):
            return self.config["schematic_dir"]
        if not selected_bom:
            return self.source_bom()
        candidate = Path(selected_bom)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        candidate = candidate.resolve()
        roots = (self.project_root.resolve(), self.bom_dir.resolve())
        if candidate != self.source_bom().resolve() and not any(candidate.is_relative_to(root) for root in roots):
            raise ValueError("selected BOM must be inside the project directory or configured BOM directory")
        if not candidate.exists():
            raise FileNotFoundError(f"selected BOM not found: {candidate}")
        return candidate

    def available_boms(self) -> list[dict[str, str]]:
        files: list[Path] = []
        for pattern in ("*.csv", "*.xlsx", "*.xlsm"):
            files.extend(self.bom_dir.glob(pattern))
        if self.source_bom().is_file():
            files.append(self.source_bom())
        result: list[dict[str, str]] = [{"name": "原理图当前数据", "path": SCHEMATIC_SOURCE_BOM}]
        for path in sorted(set(files), key=lambda item: item.name.lower()):
            if path.name in (SCHEMATIC_SYNC_LOG, "part_selection_schematic_sync_log.csv") or path.name.lower().startswith(GENERATED_REVIEW_BOM_PREFIXES):
                continue
            try:
                if not read_refs_from_bom(path):
                    continue
            except Exception:
                continue
            relative = path.relative_to(self.project_root).as_posix() if path.is_relative_to(self.project_root) else str(path)
            result.append({"name": path.name, "path": relative})
        return result

    def lcsc_bom(self) -> Path | None:
        pattern = str(self.config.get("lcsc_bom_pattern") or "")
        if pattern:
            found = find_latest_by_pattern(self.project_root, pattern)
            if found:
                return found
        return find_latest_lcsc_csv(self.bom_dir)

    def refs(self, query_refs: str = "", selected_bom: str = "") -> list[str]:
        if query_refs.strip():
            return read_target_refs(self.project_root, query_refs, None)
        if self.is_schematic_source(selected_bom):
            return sorted(self.schematic_parts().keys(), key=ref_sort_key)
        bom_path = self.resolve_bom_path(selected_bom)
        refs = read_refs_from_bom(bom_path)
        if refs:
            return refs
        return read_target_refs(self.project_root, None, self.config.get("input_refs"))

    def reconcile_parts_with_schematic(
        self,
        parts,
        overrides: dict[str, dict[str, object]],
    ) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, object]]]:
        refs = [str(getattr(part, "ref", "") or "").strip().upper() for part in parts]
        actual_by_ref = self.read_schematic_fields(
            self.config["schematic_dir"], refs, SCHEMATIC_SYNC_FIELDS
        )
        changed, states = reconcile_schematic_field_overrides(parts, overrides, actual_by_ref)
        if changed:
            save_overrides(self.overrides_path, overrides)
        return actual_by_ref, states

    def normalize_row_selections(self, parts) -> dict[str, dict]:
        parts = list(parts)
        with OVERRIDES_UPDATE_LOCK:
            overrides = load_overrides(self.overrides_path)
            updates = row_selection_updates(parts, overrides)
            if updates:
                backup = runtime_directory(self.project_root) / (
                    f"row_selection_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
                ) / "overrides_before.json"
                save_overrides(backup, overrides)
                with backup.with_name("selection_changes.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["ref", *EDITABLE_FIELDS, "old_code", "new_code", "old_review", "new_review"])
                    for part in parts:
                        if part.ref not in updates:
                            continue
                        old = overrides.get(part.ref, {})
                        new = {**old, **updates[part.ref]}
                        fields = effective_part_fields(part, old)
                        writer.writerow([part.ref, *(fields[key] for key in EDITABLE_FIELDS),
                                         selection_code(part, old), selection_code(part, new),
                                         row_review_status({"override": old}), "Pending"])
                overrides = save_override_updates(self.overrides_path, updates)
            return overrides

    def rows(self, query_refs: str = "", selected_bom: str = "", include_stock: bool = True) -> list[dict[str, object]]:
        if self.is_schematic_source(selected_bom):
            schematic = self.schematic_parts()
            refs = self.refs(query_refs, selected_bom)
            with OVERRIDES_UPDATE_LOCK:
                overrides = load_overrides(self.overrides_path)
                actual_by_ref, reconciliation = self.reconcile_parts_with_schematic(list(schematic.values()), overrides)
                overrides = self.normalize_row_selections(list(schematic.values()))
            parts = [schematic[ref] for ref in refs if ref in schematic]
            rows = group_parts_for_review(parts, overrides)
            for row in rows:
                row["source_mode"] = "schematic"
            enrich_rows_with_selection_stock(rows, self.stock_cache_path, include_stock)
            enrich_alternate_parts_with_cached_stock(rows, self.stock_cache_path)
            enrich_rows_with_schematic_sync_state(
                rows,
                self.config["schematic_dir"],
                actual_by_ref=actual_by_ref,
                reconciliation_by_ref=reconciliation,
            )
            for row in rows:
                row['spec_check'] = row_spec_check(row)
            self.enrich_local_inventory(rows)
            return sort_bom_rows(rows)

        selected_path = self.resolve_bom_path(selected_bom)
        refs = self.refs(query_refs, selected_bom)
        lcsc_bom = selected_path if selected_path.suffix.lower() in {".xlsx", ".xlsm"} or "LCSC" in selected_path.name.upper() else self.lcsc_bom()
        parts = merge_part_data(refs, self.project_root, selected_path, lcsc_bom, self.config["schematic_dir"],
                                field_mapping=self.config.get("field_mapping"))
        overrides = load_overrides(self.overrides_path)
        actual_by_ref, reconciliation = self.reconcile_parts_with_schematic(parts, overrides)
        rows = group_parts_for_review(parts, overrides)
        enrich_rows_with_selection_stock(rows, self.stock_cache_path, include_stock)
        enrich_alternate_parts_with_cached_stock(rows, self.stock_cache_path)
        enrich_rows_with_schematic_sync_state(
            rows,
            self.config["schematic_dir"],
            actual_by_ref=actual_by_ref,
            reconciliation_by_ref=reconciliation,
        )
        for row in rows:
            row['spec_check'] = row_spec_check(row)
        self.enrich_local_inventory(rows)
        return sort_bom_rows(rows)

    def stock(self, c_code: str) -> dict[str, object]:
        return lookup_stock_with_cache(self.stock_cache_path, c_code)

    def check_specs(self, payload: dict[str, object]) -> dict[str, object]:
        requests = payload.get('rows', [])
        if not isinstance(requests, list) or len(requests) > 5000:
            raise ValueError('规格核对行数无效')
        cache = load_stock_cache(self.stock_cache_path)
        refs = sorted({ref for item in requests if isinstance(item, dict)
                       for ref in split_refs(str(item.get('refs') or ''))})
        if len(refs) > 50000:
            raise ValueError('规格核对位号过多')
        actual = self.read_schematic_fields(self.config['schematic_dir'], refs, EDITABLE_FIELDS)
        checks = []
        for item in requests:
            if not isinstance(item, dict):
                raise ValueError('规格核对请求无效')
            code = normalize_lcsc_c_code(item.get('c_code'))
            requested_refs = split_refs(str(item.get('refs') or ''))
            row = {'refs_list': requested_refs, 'refs': ','.join(requested_refs),
                   'effective_c_code': code, 'stock': cache.get(code) or {},
                   'schematic_fields_by_ref': {ref: actual[ref] for ref in requested_refs if ref in actual}}
            if requested_refs:
                row.update(actual.get(requested_refs[0]) or {})
            checks.append({'refs': row['refs'], 'c_code': code, 'check': row_spec_check(row)})
        return {'ok': True, 'checks': checks}

    def cached_search_seed(self, keyword: str) -> dict[str, object]:
        code = exact_c_code_keyword(keyword)
        if not code:
            return {}
        with stock_cache_lock(self.stock_cache_path):
            cached = load_stock_cache(self.stock_cache_path).get(code)
        if not cached or not cached.get("ok"):
            return {}
        return {"items": [stock_to_candidate_item(keyword, cached)],
                "checked_at": cached.get("fetched_at", ""), "mode": "cached-exact-stale"}

    def candidate_parameters(self, payload: dict[str, object]) -> dict[str, object]:
        code = str(payload.get("c_code") or "").strip().upper()
        product_id = str(payload.get("product_id") or "").strip()
        model = str(payload.get("model") or "").strip()
        if not re.fullmatch(r"C\d+", code) or (product_id and not re.fullmatch(r"\d+", product_id)) or len(model) > 200:
            raise ValueError("Invalid product identity")
        from part_selection_product_details import fetch_product_details as load_details
        return load_details(code, product_id=product_id, model=model,
                            force_refresh=payload.get("force_refresh") is True)

    def lcsc_search(self, keyword: str, include_substitutes: bool = True, *,
                    on_partial=None, cancelled=None, force_refresh=False) -> dict[str, object]:
        exact_code = exact_c_code_keyword(keyword)
        stale_cached: dict[str, object] | None = None
        with stock_cache_lock(self.stock_cache_path):
            cached_items = load_stock_cache(self.stock_cache_path)
        if exact_code:
            cached = cached_items.get(exact_code)
            if cached and cached.get("ok"):
                stale_cached = cached
        def protect_parameters(data, records=None):
            records = cached_items if records is None else records
            return {**data, "items": [preserve_candidate_parameters(item, records.get(item.get("c_code")))
                                       for item in data.get("items", [])]}
        options = {}
        if force_refresh:
            options = {"domestic_fetcher": partial(fetch_szlcsc_domestic_products, force_refresh=True),
                       "partner_fetcher": partial(fetch_szlcsc_partner_products, force_refresh=True, strict_errors=True)}
        if on_partial is not None:
            options["on_partial"] = lambda data: on_partial(protect_parameters(data))
        if cancelled is not None:
            options["cancelled"] = cancelled
        result = search_lcsc_candidates(
            keyword,
            limit=24,
            include_substitutes=include_substitutes,
            overseas_fetcher=fetch_szlcsc_overseas_channels,
            **options,
        )
        if cancelled and cancelled():
            from concurrent.futures import CancelledError
            raise CancelledError()
        if not result.get("ok") and stale_cached:
            if on_partial is not None:
                return result
            return {
                "ok": True,
                "error": "",
                "items": [stock_to_candidate_item(keyword, stale_cached)],
                "mode": "cached-exact-stale",
                "model_tokens": [exact_code],
            }
        if result.get("ok"):
            with stock_cache_lock(self.stock_cache_path):
                cache = load_stock_cache(self.stock_cache_path)
                result = protect_parameters(result, cache)
                item = next((item for item in result.get("items", []) if item.get("c_code") == exact_code), None)
                if item:
                    cache[exact_code] = candidate_stock_result(item)
                    save_stock_cache(self.stock_cache_path, cache)
        return result

    def undo_workbench_changes(self, payload: dict[str, object]) -> dict[str, object]:
        refs = sorted(set(split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))), key=ref_sort_key)
        if not refs:
            raise ValueError("请选择要撤回的行")
        if "confirm" in payload and not isinstance(payload["confirm"], bool):
            raise ValueError("confirm must be a boolean")
        with SCHEMATIC_SYNC_TRANSACTION_LOCK, OVERRIDES_UPDATE_LOCK:
            current = load_overrides(self.overrides_path)
            actual = self.read_schematic_fields(self.config["schematic_dir"], refs, SCHEMATIC_SYNC_FIELDS)
            if any(ref not in actual for ref in refs):
                raise ValueError("原理图中有位号缺失，未撤回任何工作台记录")
            targets, modes, changes = {}, set(), []

            def values(record, saved):
                fields = record.get("field_overrides", {})
                code = normalize_lcsc_c_code(record.get("stock_c_code") or record.get("accepted_c_code")
                                           or record.get("manual_c_code") or saved.get("lcsc_c_code"))
                if record.get("decision") in {"ClearSelection", "Reject", "NoChange", "DNP"}:
                    code = ""
                return {**{key: str(fields.get(key, saved.get(key, ""))) for key in (*EDITABLE_FIELDS, "description")},
                        "lcsc_c_code": code, "review_status": record.get("review_status", "Pending"),
                        "note": record.get("note", ""),
                        "alternate_parts": record.get("alternate_parts", alternate_parts_from_schematic(
                            saved.get("lcsc_alt", ""), saved.get("lcsc_alt_info", ""))),
                        "procurement_choice": record.get("procurement_choice", procurement_choice_from_schematic(
                            saved.get("procurement_url", ""), saved.get("procurement_info", "")))}

            for ref in refs:
                record = current.get(ref, {})
                snapshot = record.get("workbench_undo")
                if isinstance(snapshot, dict) and isinstance(snapshot.get("record"), dict):
                    target = deepcopy(snapshot["record"])
                    if target.get("schematic_synced_at", "") != record.get("schematic_synced_at", ""):
                        raise ValueError("该行已经同步过原理图，不能撤回此前的修改")
                    modes.add("snapshot")
                else:
                    # Legacy records have no pre-edit history: use only verifiable saved CAD fields.
                    modes.add("schematic")
                    saved = actual[ref]
                    code = normalize_lcsc_c_code(saved.get("lcsc_c_code"))
                    target = deepcopy(record)
                    target.update(accepted_c_code=code, manual_c_code=code, stock_c_code=code,
                                  decision="AcceptManual" if code else "ClearSelection", status="")
                    target["field_overrides"] = {key: str(saved.get(key, "")) for key in (*EDITABLE_FIELDS, "description")}
                    target["field_overrides"]["description_c_code"] = code
                    target["alternate_parts"] = alternate_parts_from_schematic(saved.get("lcsc_alt", ""), saved.get("lcsc_alt_info", ""))
                    target["procurement_choice"] = procurement_choice_from_schematic(saved.get("procurement_url", ""), saved.get("procurement_info", ""))
                targets[ref] = target
                before, after = values(record, actual[ref]), values(target, actual[ref])
                for key in before:
                    if before[key] != after[key]:
                        changes.append({"ref": ref, "field": key, "old": before[key], "new": after[key]})
            # Preview regrouping too: an undo must never clear a sibling's selected material.
            hypothetical = {**current, **targets}
            parts = list(self.schematic_parts().values())
            target_keys = {tuple(effective_part_fields(part, targets[part.ref])[key] for key in EDITABLE_FIELDS)
                           for part in parts if part.ref in targets}
            related = [part for part in parts if tuple(effective_part_fields(
                part, hypothetical.get(part.ref, {}))[key] for key in EDITABLE_FIELDS) in target_keys]
            normalizations = row_selection_updates(related, hypothetical)
            if normalizations:
                affected = sorted(normalizations, key=ref_sort_key)
                raise ValueError("撤回后将合并到选料或确认状态冲突的行，可能清空编码；请先处理这些位号：" + ",".join(affected))
            related_refs = set(refs) | {part.ref for part in related}
            revision = hashlib.sha256(json.dumps({"refs": refs,
                "records": {ref: current.get(ref) for ref in related_refs}, "schematic": actual,
                "related_parts": {part.ref: asdict(part) for part in related}},
                ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            result = {"ok": True, "refs": refs, "available": bool(changes), "changes": changes,
                      "revision": revision, "mode": next(iter(modes)) if len(modes) == 1 else "mixed"}
            if not payload.get("confirm"):
                return result
            if payload.get("revision") != revision:
                raise ValueError("预览后工作台或原理图发生变化，请重新预览后再撤回")
            if not changes:
                raise ValueError("该行没有可撤回的未同步修改")
            backup = runtime_directory(self.project_root) / f"workbench_undo_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}" / "overrides_before.json"
            save_overrides(backup, current)
            for ref, target in targets.items():
                if target:
                    current[ref] = target
                else:
                    current.pop(ref, None)
            save_overrides(self.overrides_path, current)
            return {**result, "restored": True, "backup": str(backup)}

    def save_override(self, payload: dict[str, object]) -> dict[str, object]:
        refs = split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))
        update = {
            key: payload.get(key, "")
            for key in (
                "accepted_c_code",
                "manual_c_code",
                "stock_c_code",
                "decision",
                "note",
                "status",
                "review_status",
                "field_overrides",
                "procurement_choice",
                "schematic_field_snapshot",
            )
            if key in payload
        }
        if not refs:
            raise ValueError("ref is required")
        primary_code = normalize_lcsc_c_code(
            payload.get("stock_c_code")
            or payload.get("accepted_c_code")
            or payload.get("manual_c_code")
        )
        current_overrides = load_overrides(self.overrides_path)
        preserve_unchanged_notes(update, refs, current_overrides)
        schematic_values = (
            self.read_schematic_fields(
                self.config["schematic_dir"], refs, ("lcsc_alt", "lcsc_alt_info")
            )
            if primary_code
            else {}
        )
        updates = {}
        for ref in refs:
            ref_update = dict(update)
            if primary_code:
                current = current_overrides.get(ref, {})
                if "alternate_parts" in current:
                    alternate_parts = normalize_alternate_parts(current.get("alternate_parts"))
                else:
                    values = schematic_values.get(ref, {})
                    alternate_parts = alternate_parts_from_schematic(
                        values.get("lcsc_alt", ""), values.get("lcsc_alt_info", "")
                    )
                filtered = [item for item in alternate_parts if item["c_code"] != primary_code]
                if len(filtered) != len(alternate_parts):
                    ref_update["alternate_parts"] = filtered
            updates[ref] = ref_update
        overrides = save_override_updates(self.overrides_path, updates, capture_undo=True)
        return {"ok": True, "refs": refs, "override": overrides.get(refs[0], {})}

    def select_candidate(self, payload: dict[str, object], progress=None) -> dict[str, object]:
        choice = payload.get("footprint_choice")
        context = None
        if choice is not None:
            if not isinstance(choice, dict):
                raise ValueError("footprint_choice must be an object")
            context = self.footprint_choices.context(choice.get("preview_id"))
            if not context:
                raise ValueError("该封装预览没有关联选料，请重新选择")
            payload = context["payload"]
        refs = split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))
        item = payload.get("candidate")
        if not refs or not isinstance(item, dict):
            raise ValueError("refs and candidate are required")
        code = normalize_lcsc_c_code(item.get("c_code"))
        if not re.fullmatch(r"C\d+", code) or not str(item.get("model") or "").strip():
            raise ValueError("candidate must have a valid C code and full model")
        mode = payload.get("footprint_mode", "keep")
        if mode not in ("keep", "library", "rename", "convert"):
            raise ValueError("footprint_mode must be keep, library, rename or convert")
        replacements = {field: payload.get("replace_" + field, False) for field in ("value", "comment")}
        for field, enabled in replacements.items():
            if not isinstance(enabled, bool):
                raise ValueError("replace_" + field + " must be a boolean")
        library = None
        initial = load_overrides(self.overrides_path)
        revisions = context["revisions"] if context else {ref: initial.get(ref, {}) for ref in refs}
        if mode in ("library", "rename", "convert") and choice is None:
            if payload.get("phase") == "preview":
                actual = self.read_schematic_fields(self.config["schematic_dir"], refs, ("footprint",))
                ids = list(dict.fromkeys(actual.get(ref, {}).get("footprint", "") for ref in refs))
                if mode == "library":
                    preview = self.footprint_choices.prepare_library(ids, str(item.get("package") or ""))
                elif mode == "rename":
                    preview = self.footprint_choices.prepare_rename(ids, str(item.get("package") or ""), code=code, refs=refs)
                else:
                    preview = self.footprint_choices.prepare(code, ids, str(item.get("package") or ""), progress, refs=refs)
                self.footprint_choices.bind(preview["preview_id"], {
                    "payload": payload, "revisions": revisions, "actual": actual})
                return {"ok": True, "preview": preview}
            else:
                raise ValueError("请先预览并确认封装后再保存，不能绕过配准与确认")
        item = {**item, "c_code": code}
        with stock_cache_lock(self.stock_cache_path):
            item = preserve_candidate_parameters(item, load_stock_cache(self.stock_cache_path).get(code))
        for key in ("model", "package", "param", "param_detail"):
            if key in item:
                item[key] = html.unescape(re.sub(r"</?span\b[^>]*>", "", str(item[key]), flags=re.I))
        fields = {}
        for ref in refs:
            fields[ref] = candidate_selection_fields(item, [ref])
            for field, enabled in replacements.items():
                if not enabled:
                    fields[ref].pop(field, None)
        if progress:
            progress('saving', 'Saving selected part and field overrides locally')
        with self.selection_save_lock, OVERRIDES_UPDATE_LOCK:
            if choice is not None:
                current = load_overrides(self.overrides_path)
                if any(current.get(ref, {}) != revisions[ref] for ref in refs):
                    raise ValueError("预览期间该行已被修改，请重新预览；没有写入封装库")
                actual = self.read_schematic_fields(self.config["schematic_dir"], refs, ("footprint",))
                if actual != context["actual"]:
                    raise ValueError("原理图封装发生变化，请重新预览")
                library = self.footprint_choices.commit(choice)
                if 'conflict' in library:
                    return {"ok": True, "footprint_conflict": library['conflict']}
            try:
                return self._save_candidate(refs, code, item, fields, library, mode, revisions)
            except OSError as exc:
                detail = ''
                if library:
                    detail = ('封装库已保存但选料记录未保存；库备份：' + library['backup'] + '。') if library.get('backup') else '选料记录未保存，所选现有库封装未修改。'
                raise RuntimeError(detail + '本地保存失败（不是网络转换失败），请检查目录权限或文件占用：' + str(exc)) from exc

    def _save_candidate(self, refs, code, item, fields, library, mode, revisions):
        current = load_overrides(self.overrides_path)
        if any(current.get(ref, {}) != revisions[ref] for ref in refs):
            raise ValueError("选料期间该行已被修改，请重新查看当前行后再选择；旧转换结果不会覆盖新选型")
        with stock_cache_lock(self.stock_cache_path):
            cache = load_stock_cache(self.stock_cache_path)
            cache[code] = candidate_stock_result(item)
            cache[code].setdefault("fetched_at", datetime.now().isoformat(timespec="seconds"))
            save_stock_cache(self.stock_cache_path, cache)
        actual = self.read_schematic_fields(self.config["schematic_dir"], refs, EDITABLE_FIELDS)
        schematic_alternates = self.read_schematic_fields(
            self.config["schematic_dir"], refs, ("lcsc_alt", "lcsc_alt_info")
        )
        now = datetime.now().isoformat(timespec="seconds")
        for ref in refs:
            # Establish the pre-edit baseline even for legacy records without snapshots.
            snapshot = {**actual.get(ref, {}), **normalize_schematic_field_snapshot(
                current.get(ref, {}).get("schematic_field_snapshot")
            )}
            previous = current.get(ref, {})
            ref_fields = {**fields[ref]}
            if library:
                ref_fields["footprint"] = library["footprint"]
            else:
                pending = previous.get("field_overrides", {})
                if "footprint" in pending:
                    ref_fields["footprint"] = pending["footprint"]
                elif ref in actual:
                    ref_fields["footprint"] = actual[ref].get("footprint", "")
            alternate_parts = (normalize_alternate_parts(previous["alternate_parts"])
                if "alternate_parts" in previous else alternate_parts_from_schematic(
                    schematic_alternates.get(ref, {}).get("lcsc_alt", ""),
                    schematic_alternates.get(ref, {}).get("lcsc_alt_info", "")))
            update = {
                "accepted_c_code": code, "manual_c_code": code, "stock_c_code": code,
                "decision": "AcceptManual", "status": "ChangedToNewCode",
                "field_overrides": ref_fields, "schematic_field_snapshot": snapshot,
                "procurement_choice": {},
            }
            filtered = [part for part in alternate_parts if part["c_code"] != code]
            if len(filtered) != len(alternate_parts):
                update["alternate_parts"] = filtered
            current[ref] = merge_override_record(previous, update, now, capture_undo=True)
        latest = load_overrides(self.overrides_path)
        if any(latest.get(ref, {}) != revisions[ref] for ref in refs):
            raise ValueError("保存前检测到该行发生变化，已保留较新的选料，请重试")
        latest.update({ref: current[ref] for ref in refs})
        save_overrides(self.overrides_path, latest)
        result = {"ok": True, "refs": refs, "override": current[refs[0]]}
        result["fields"] = current[refs[0]]["field_overrides"]
        result["footprint_mode"] = mode
        if library:
            result["library"] = library
        return result

    def save_field_override(self, payload: dict[str, object]) -> dict[str, object]:
        refs = split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))
        raw_fields = payload.get("field_overrides", {})
        if not refs:
            raise ValueError("ref is required")
        if not isinstance(raw_fields, dict):
            raise ValueError("field_overrides must be an object")
        update = {
            "field_overrides": {
                key: raw_fields.get(key, "")
                for key in EDITABLE_FIELDS
                if key in raw_fields
            },
        }
        if "note" in payload:
            update["note"] = payload["note"]
        if not update["field_overrides"]:
            raise ValueError("no editable fields were provided")
        with OVERRIDES_UPDATE_LOCK:
            overrides = load_overrides(self.overrides_path)
            preserve_unchanged_notes(update, refs, overrides)
            parts = []
            if hasattr(self, "config"):
                bom = str(payload.get("bom") or "")
                if self.is_schematic_source(bom):
                    parts = list(self.schematic_parts().values())
                elif bom or self.source_bom().is_file():
                    source = self.resolve_bom_path(bom)
                    lcsc = source if source.suffix.lower() in {".xlsx", ".xlsm"} or "LCSC" in source.name.upper() else self.lcsc_bom()
                    parts = merge_part_data(self.refs("", bom), self.project_root, source, lcsc, self.config["schematic_dir"],
                                            field_mapping=self.config.get("field_mapping"))
            fields = {key: str(value or "").strip() for key, value in update["field_overrides"].items()}
            cache = load_stock_cache(self.stock_cache_path) if parts else {}
            inherited, merges = edited_row_merge_updates(parts, overrides, refs, fields, cache)
            if merges:
                backup = runtime_directory(self.project_root) / f"row_merge_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
                save_overrides(backup / "overrides_before.json", overrides)
                (backup / "merge.json").write_text(json.dumps({"fields": fields, "merges": merges}, ensure_ascii=False, indent=2), encoding="utf-8")
            actual = self.read_schematic_fields(self.config["schematic_dir"], list(inherited), EDITABLE_FIELDS) if inherited else {}
            now = datetime.now().isoformat(timespec="seconds")
            for ref in refs:
                previous = overrides.get(ref, {})
                selection = inherited.get(ref, {})
                ref_update = {**selection, **update, "field_overrides": {**selection.get("field_overrides", {}), **fields}}
                if selection:
                    ref_update["schematic_field_snapshot"] = {
                        **actual.get(ref, {}), **normalize_schematic_field_snapshot(previous.get("schematic_field_snapshot"))}
                record = merge_override_record(previous, ref_update, now, capture_undo=True)
                if "review_confirmed_at" in selection:
                    record["review_confirmed_at"] = selection["review_confirmed_at"]
                overrides[ref] = record
            save_overrides(self.overrides_path, overrides)
        return {"ok": True, "refs": refs, "override": overrides.get(refs[0], {}), "merged_into": merges}

    def adopt_schematic_fields(self, payload: dict[str, object]) -> dict[str, object]:
        refs = split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))
        if not refs:
            raise ValueError("ref is required")
        requested_fields = payload.get("fields", EDITABLE_FIELDS)
        if not isinstance(requested_fields, (list, tuple)):
            raise ValueError("fields must be an array")
        fields = [str(key) for key in requested_fields if str(key) in EDITABLE_FIELDS]
        if not fields:
            raise ValueError("no editable fields were requested")
        actual_by_ref = self.read_schematic_fields(
            self.config["schematic_dir"], refs, EDITABLE_FIELDS
        )
        missing_refs: list[str] = []
        overrides = load_overrides(self.overrides_path)
        adopted: dict[str, dict[str, str]] = {}
        synced_at = datetime.now().isoformat(timespec="seconds")
        for ref in refs:
            actual = actual_by_ref.get(ref)
            if actual is None:
                missing_refs.append(ref)
                continue
            current = overrides.get(ref, {})
            existing_snapshot = normalize_schematic_field_snapshot(
                current.get("schematic_field_snapshot")
            )
            values = {key: str(actual.get(key, "") or "").strip() for key in fields}
            overrides = save_override(
                self.overrides_path,
                ref,
                {
                    "field_overrides": values,
                    "schematic_field_snapshot": {**existing_snapshot, **values},
                    "schematic_synced_at": synced_at,
                    "schematic_sync_summary": "adopted current KiCad fields",
                    "rebase_workbench_undo": True,
                },
            )
            adopted[ref] = values
        return {
            "ok": bool(adopted),
            "refs": sorted(adopted, key=ref_sort_key),
            "missing_refs": sorted(missing_refs, key=ref_sort_key),
            "adopted": adopted,
        }

    def save_alternate_parts(self, payload: dict[str, object]) -> dict[str, object]:
        refs = split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))
        if not refs:
            raise ValueError("ref is required")
        action = str(payload.get("action") or "").strip()
        if action not in {"add", "remove", "move", "adopt_schematic"}:
            raise ValueError("unsupported alternate-parts action")

        overrides = load_overrides(self.overrides_path)
        first_override = overrides.get(refs[0], {})
        schematic_values = self.read_schematic_fields(
            self.config["schematic_dir"], refs, ("lcsc_alt", "lcsc_alt_info")
        )

        def schematic_parts_for_ref(ref: str) -> list[dict[str, str]]:
            values = schematic_values.get(ref, {})
            return alternate_parts_from_schematic(
                values.get("lcsc_alt", ""), values.get("lcsc_alt_info", "")
            )

        if "alternate_parts" in first_override:
            parts = normalize_alternate_parts(first_override.get("alternate_parts"))
        else:
            parts = schematic_parts_for_ref(refs[0])

        if action == "adopt_schematic":
            variants = {
                alternate_parts_signature(schematic_parts_for_ref(ref))
                for ref in refs
                if ref in schematic_values
            }
            if len(variants) > 1:
                raise ValueError("selected refs contain different schematic alternate lists")
            parts = schematic_parts_for_ref(refs[0])
        else:
            c_code = normalize_lcsc_c_code(payload.get("c_code"))
            if not c_code:
                raise ValueError("valid alternate C code is required")
            index = next((idx for idx, item in enumerate(parts) if item["c_code"] == c_code), -1)
            if action == "add":
                primary_code = normalize_lcsc_c_code(payload.get("primary_c_code"))
                if primary_code and c_code == primary_code:
                    raise ValueError("current primary C code cannot be added as an alternate")
                if index >= 0:
                    raise ValueError(f"alternate {c_code} already exists")
                if len(parts) >= MAX_ALTERNATE_PARTS:
                    raise ValueError(f"a row can contain at most {MAX_ALTERNATE_PARTS} alternate parts")
                candidate = payload.get("part")
                if not isinstance(candidate, dict):
                    candidate = {}
                normalized = normalize_alternate_parts([{**candidate, "c_code": c_code}])
                if not normalized:
                    raise ValueError("alternate part data is invalid")
                parts.append(normalized[0])
                cache_candidate_stock(self.stock_cache_path, candidate)
            elif action == "remove":
                if index < 0:
                    raise ValueError(f"alternate {c_code} was not found")
                parts.pop(index)
            elif action == "move":
                if index < 0:
                    raise ValueError(f"alternate {c_code} was not found")
                direction = str(payload.get("direction") or "").lower()
                offset = -1 if direction == "up" else 1 if direction == "down" else 0
                if not offset:
                    raise ValueError("direction must be up or down")
                target = index + offset
                if 0 <= target < len(parts):
                    parts[index], parts[target] = parts[target], parts[index]

        normalized_parts = normalize_alternate_parts(parts)
        for ref in refs:
            save_override(self.overrides_path, ref, {"alternate_parts": normalized_parts}, capture_undo=True)
        return {
            "ok": True,
            "refs": refs,
            "alternate_parts": normalized_parts,
            "schematic_fields": alternate_parts_to_schematic_fields(normalized_parts),
            "needs_sync": action != "adopt_schematic",
        }

    def save_procurement_choice(self, payload: dict[str, object]) -> dict[str, object]:
        refs = split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))
        if not refs:
            raise ValueError("ref is required")
        action = str(payload.get("action") or "").strip()
        if action not in {"select", "clear", "adopt_schematic"}:
            raise ValueError("unsupported procurement action")

        if action == "adopt_schematic":
            schematic_values = self.read_schematic_fields(
                self.config["schematic_dir"], refs, ("procurement_url", "procurement_info")
            )

            def schematic_choice_for_ref(ref: str) -> dict[str, str]:
                values = schematic_values.get(ref, {})
                return procurement_choice_from_schematic(
                    values.get("procurement_url", ""),
                    values.get("procurement_info", ""),
                )

            variants = {
                procurement_choice_signature(schematic_choice_for_ref(ref))
                for ref in refs
                if ref in schematic_values
            }
            if len(variants) > 1:
                raise ValueError("selected refs contain different schematic procurement choices")
            choice = schematic_choice_for_ref(refs[0])
        elif action == "clear":
            choice = {}
        else:
            choice = normalize_procurement_choice(payload.get("choice"))
            if not choice:
                raise ValueError("valid procurement order URL is required")

        for ref in refs:
            save_override(self.overrides_path, ref, {"procurement_choice": choice}, capture_undo=True)
        return {
            "ok": True,
            "refs": refs,
            "procurement_choice": choice,
            "schematic_fields": procurement_choice_to_schematic_fields(choice),
            "needs_sync": action != "adopt_schematic",
        }

    def append_schematic_sync_log(self, result: dict[str, object]) -> Path:
        output_path = self.bom_dir / SCHEMATIC_SYNC_LOG
        output_path.parent.mkdir(parents=True, exist_ok=True)
        columns = ["SyncedAt", "Designator", "Page", "Field", "OldValue", "NewValue", "SchematicPath"]
        exists = output_path.exists()
        changes = result.get("changes", {})
        pages = result.get("pages", {})
        paths = result.get("paths", [])
        first_path = str(paths[0]) if isinstance(paths, list) and paths else ""
        with output_path.open("a", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            if not exists:
                writer.writeheader()
            for ref, field_changes in sorted(changes.items(), key=lambda item: ref_sort_key(item[0])):
                for field, values in field_changes.items():
                    writer.writerow(
                        {
                            "SyncedAt": datetime.now().isoformat(timespec="seconds"),
                            "Designator": ref,
                            "Page": pages.get(ref, ""),
                            "Field": field,
                            "OldValue": values.get("old", ""),
                            "NewValue": values.get("new", ""),
                            "SchematicPath": first_path,
                        }
                    )
        return output_path

    def append_schematic_sync_runtime_log(
        self,
        attempt_id: str,
        event: str,
        refs: list[str],
        started_at: float,
        details: dict[str, object] | None = None,
    ) -> Path:
        output_path = runtime_directory(self.project_root) / SCHEMATIC_SYNC_RUNTIME_LOG
        output_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "attempt_id": attempt_id,
            "event": event,
            "refs": refs,
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "details": details or {},
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with SCHEMATIC_SYNC_RUNTIME_LOG_LOCK:
            with output_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(line + "\n")
        return output_path

    def schematic_sync_summary_for_ref(self, result: dict[str, object], ref: str) -> str:
        changes = result.get("changes", {})
        if not isinstance(changes, dict):
            return ""
        field_changes = changes.get(ref, {})
        if not isinstance(field_changes, dict):
            return ""
        items: list[str] = []
        for field in SCHEMATIC_SYNC_FIELDS:
            values = field_changes.get(field)
            if not isinstance(values, dict):
                continue
            old_value = str(values.get("old", "") or "")
            new_value = str(values.get("new", "") or "")
            items.append(f"{field}: {old_value} -> {new_value}")
        return "; ".join(items)

    def record_schematic_sync_overrides(
        self,
        result: dict[str, object],
        expected_fields_by_ref: dict[str, dict[str, object]] | None = None,
    ) -> None:
        updated_refs = result.get("updated_refs", [])
        if not isinstance(updated_refs, list):
            updated_refs = []
        expected_fields_by_ref = expected_fields_by_ref or {}
        synced_at = datetime.now().isoformat(timespec="seconds")
        refs = {
            str(ref_value).strip().upper()
            for ref_value in [*updated_refs, *expected_fields_by_ref]
            if str(ref_value).strip()
        }
        updates: dict[str, dict[str, object]] = {}
        for ref in sorted(refs, key=ref_sort_key):
            summary = self.schematic_sync_summary_for_ref(result, ref)
            expected = expected_fields_by_ref.get(ref, {})
            snapshot = {
                key: str(expected.get(key) or "").strip()
                for key in EDITABLE_FIELDS
                if key in expected
            }
            if not summary and not snapshot and not expected:
                continue
            update: dict[str, object] = {
                "schematic_synced_at": synced_at,
                "schematic_sync_summary": summary,
                "clear_workbench_undo": True,
            }
            if snapshot:
                update["schematic_field_snapshot"] = snapshot
            updates[ref] = update
        if updates:
            save_override_updates(self.overrides_path, updates)

    def kicad_url_for_refs(self, refs: list[str], pages: dict[str, str] | None = None) -> str:
        if not refs:
            return ""
        first_ref = refs[0]
        page = str((pages or {}).get(first_ref, "") or "")
        if not page:
            parts = self.schematic_parts()
            page = parts.get(first_ref).page if first_ref in parts else ""
        return kicad_jump_url(first_ref, page)

    def run_kicad_script(self, url: str, switches: list[str]) -> dict[str, object]:
        script = Path(__file__).resolve().parent / "kicad_schematic_search.ps1"
        if not url:
            return {"ok": False, "message": "no KiCad ref URL"}
        if not script.exists():
            return {"ok": False, "message": f"missing KiCad search script: {script}"}
        try:
            process_options: dict[str, object] = {}
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                process_options = {
                    "creationflags": subprocess.CREATE_NO_WINDOW,
                    "startupinfo": startupinfo,
                }
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    url,
                    "-ProjectRoot",
                    str(self.project_root),
                    *switches,
                ],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                # Covers editor startup, a five-minute KiCad project backup,
                # dirty-state verification, and the two-minute close wait.
                timeout=480 if "-SaveOnly" in switches else 90,
                **process_options,
            )
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        return {
            "ok": completed.returncode == 0,
            "message": (completed.stdout or completed.stderr or "").strip(),
            "returncode": completed.returncode,
        }

    def jump_to_schematic(self, url: str) -> dict[str, object]:
        if urlparse(url).scheme != self.config.get("kicad_protocol", "kicad-sch-search"):
            raise ValueError("Not a KiCad schematic jump URL")
        switches = ["-Mode", "SearchPanel"]
        if self.config.get("jump_cursor_behavior", "return") == "stay":
            switches.append("-MoveCursorAfterJump")
        return self.run_kicad_script(url, switches)

    def prepare_kicad_for_external_schematic_write(self, refs: list[str]) -> dict[str, object]:
        url = self.kicad_url_for_refs(refs)
        return self.run_kicad_script(
            url,
            ["-Mode", "FindDialog", "-SaveActiveFrame", "-CloseAfterSave", "-SaveOnly"],
        )

    def notify_kicad_schematic_changed(
        self,
        result: dict[str, object],
        fallback_refs: list[str] | None = None,
    ) -> dict[str, object]:
        updated_refs = result.get("updated_refs", []) or fallback_refs or []
        pages = result.get("pages", {})
        if not updated_refs:
            return {"ok": False, "message": "no changed ref to show"}
        url = self.kicad_url_for_refs([str(updated_refs[0])], pages if isinstance(pages, dict) else {})
        return self.run_kicad_script(url, ["-Mode", "SearchPanel"])

    @staticmethod
    def require_kicad_prepare_success(result: dict[str, object]) -> None:
        if result.get("ok"):
            return
        message = str(result.get("message") or "KiCad schematic window did not close")
        if "KICAD_BUSY" in message:
            raise RuntimeError("KiCad 当前无响应，或工程窗口读取超时，已停止同步，本次未写入原理图，也不会继续排队保存/关闭。请等待 KiCad 恢复，检查是否有未处理的对话框；不要反复点击同步或强制终止有未保存修改的编辑器。")
        if "manual save required" in message:
            raise RuntimeError("原理图有未保存修改，当前自动保存接口不可用。为避免 KiCad 卡住，已停止自动保存和关闭，本次未写入原理图。请在 KiCad 按 Ctrl+S 保存，等待恢复响应后再同步。")
        raise RuntimeError(f"自动保存/关闭 KiCad 未完成，本次未写入原理图。请检查 KiCad 是否有未处理的对话框或保存错误，未保存改动不会被丢弃。详情：{message}")

    def verify_schematic_field_values(self, ref_field_map: dict[str, dict[str, object]], attempts: int = 1) -> dict[str, object]:
        expected: dict[str, dict[str, str]] = {}
        field_keys: set[str] = set()
        for ref, fields in ref_field_map.items():
            normalized_ref = str(ref or "").strip().upper()
            if not normalized_ref:
                continue
            expected[normalized_ref] = {
                key: str(value or "").strip()
                for key, value in fields.items()
                if key in SCHEMATIC_SYNC_FIELDS
                and (
                    str(value or "").strip()
                    or key in {"lcsc_alt", "lcsc_alt_info", "procurement_url", "procurement_info"}
                )
            }
            field_keys.update(expected[normalized_ref])
        actual = self.read_schematic_fields(
            self.config["schematic_dir"],
            sorted(expected, key=ref_sort_key),
            sorted(field_keys),
        )
        mirrored = [key for key, name in self.config.get('field_mapping', {}).items()
                    if key in ('value', 'footprint') and name != {'value': 'Value', 'footprint': 'Footprint'}[key]]
        builtin = read_schematic_symbol_field_values(
            self.config['schematic_dir'], sorted(expected, key=ref_sort_key), mirrored,
            field_mapping={**self.config.get('field_mapping', {}), 'value': 'Value', 'footprint': 'Footprint'},
        ) if mirrored else {}
        mismatches: dict[str, dict[str, dict[str, str]]] = {}
        missing_refs: list[str] = []
        for ref, fields in expected.items():
            actual_fields = actual.get(ref)
            if actual_fields is None:
                missing_refs.append(ref)
                continue
            for key, expected_value in fields.items():
                actual_value = str(actual_fields.get(key, "") or "").strip()
                if actual_value != expected_value:
                    mismatches.setdefault(ref, {})[key] = {
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                if key in mirrored and builtin.get(ref, {}).get(key, '') != expected_value:
                    mismatches.setdefault(ref, {})[key + '_builtin'] = {
                        'expected': expected_value, 'actual': builtin.get(ref, {}).get(key, ''),
                    }
        return {
            "ok": not mismatches,
            "attempts": attempts,
            "mismatches": mismatches,
            "missing_refs": sorted(missing_refs, key=ref_sort_key),
        }

    def verify_schematic_after_notify(
        self,
        ref_field_map: dict[str, dict[str, object]],
        attempts: int,
    ) -> dict[str, object]:
        time.sleep(SCHEMATIC_NOTIFY_SETTLE_SECONDS)
        verify = self.verify_schematic_field_values(ref_field_map, attempts=attempts)
        verify["phase"] = "after_notify"
        return verify

    def merge_schematic_update_results(self, first: dict[str, object], second: dict[str, object]) -> dict[str, object]:
        merged = dict(first)
        for key in ("updated_refs", "missing_refs", "paths"):
            values = []
            for result in (first, second):
                item = result.get(key, [])
                if isinstance(item, list):
                    values.extend(str(value) for value in item)
            if key in {"updated_refs", "missing_refs"}:
                merged[key] = sorted(set(values), key=ref_sort_key)
            else:
                merged[key] = sorted(set(values))
        for key in ("changes", "pages"):
            merged_item = {}
            for result in (first, second):
                item = result.get(key, {})
                if isinstance(item, dict):
                    merged_item.update(item)
            merged[key] = merged_item
        return merged

    def ensure_schematic_fields_verified(
        self,
        ref_field_map: dict[str, dict[str, object]],
        rewrite,
        phase: str,
        max_attempts: int = 3,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        retry_results: list[dict[str, object]] = []
        verify = self.verify_schematic_field_values(ref_field_map, attempts=1)
        for attempt in range(2, max_attempts + 1):
            if verify.get("ok"):
                break
            time.sleep(0.2 * attempt)
            retry_result = rewrite()
            retry_results.append(retry_result)
            verify = self.verify_schematic_field_values(ref_field_map, attempts=attempt)
        verify["phase"] = phase
        return verify, retry_results

    def recover_schematic_after_notify(
        self,
        refs: list[str],
        ref_field_map: dict[str, dict[str, object]],
        rewrite,
        result: dict[str, object],
        verify: dict[str, object],
        log_event,
        max_cycles: int = 2,
    ) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
        prepare_results: list[dict[str, object]] = []
        notify_results: list[dict[str, object]] = []
        cycle = 0
        total_attempts = int(verify.get("attempts", 1) or 1)
        while not verify.get("ok") and cycle < max_cycles:
            cycle += 1
            log_event(
                "recovery_started",
                cycle=cycle,
                mismatches=verify.get("mismatches", {}),
                missing_refs=verify.get("missing_refs", []),
            )
            prepare_result = self.prepare_kicad_for_external_schematic_write(refs)
            prepare_results.append(prepare_result)
            log_event("recovery_prepare_complete", cycle=cycle, result=prepare_result)
            if not prepare_result.get("ok"):
                verify = dict(verify)
                verify["ok"] = False
                verify["phase"] = "recovery_prepare_failed"
                verify["prepare_error"] = str(
                    prepare_result.get("message") or "KiCad schematic window did not close"
                )
                break

            rewrite_result = rewrite()
            result = self.merge_schematic_update_results(result, rewrite_result)
            log_event(
                "recovery_write_complete",
                cycle=cycle,
                updated_refs=rewrite_result.get("updated_refs", []),
                missing_refs=rewrite_result.get("missing_refs", []),
            )
            write_verify, retry_results = self.ensure_schematic_fields_verified(
                ref_field_map,
                rewrite,
                f"recovery_{cycle}_after_write",
            )
            for retry_result in retry_results:
                result = self.merge_schematic_update_results(result, retry_result)
            total_attempts += int(write_verify.get("attempts", 1) or 1)
            log_event("recovery_write_verified", cycle=cycle, verify=write_verify)
            if not write_verify.get("ok"):
                verify = write_verify
                break

            notify_target = result
            if not result.get("updated_refs") and refs:
                notify_target = dict(result)
                notify_target["updated_refs"] = refs
            notify_result = self.notify_kicad_schematic_changed(notify_target)
            notify_results.append(notify_result)
            log_event("recovery_notify_complete", cycle=cycle, result=notify_result)

            verify = self.verify_schematic_after_notify(ref_field_map, attempts=total_attempts + 1)
            total_attempts += 1
            verify["recovery_cycles"] = cycle
            log_event("recovery_notify_verified", cycle=cycle, verify=verify)
        verify["attempts"] = total_attempts
        verify["recovery_cycles"] = cycle
        return result, verify, prepare_results, notify_results

    def sync_schematic(self, payload: dict[str, object]) -> dict[str, object]:
        attempt_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        started_at = time.perf_counter()
        refs = split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))
        runtime_log_path = runtime_directory(self.project_root) / SCHEMATIC_SYNC_RUNTIME_LOG

        def log_event(event: str, **details: object) -> None:
            self.append_schematic_sync_runtime_log(attempt_id, event, refs, started_at, details)

        raw_fields = payload.get("field_overrides", {})
        log_event(
            "request_started",
            field_names=sorted(raw_fields) if isinstance(raw_fields, dict) else [],
            notify=payload.get("notify", True) is not False,
        )
        try:
            lock_started_at = time.perf_counter()
            with SCHEMATIC_SYNC_TRANSACTION_LOCK:
                log_event(
                    "transaction_lock_acquired",
                    wait_ms=round((time.perf_counter() - lock_started_at) * 1000, 1),
                )
                result = self._sync_schematic_impl(payload, log_event)
        except Exception as exc:
            log_event("request_failed", error=type(exc).__name__, message=str(exc))
            setattr(exc, "sync_attempt_id", attempt_id)
            setattr(exc, "runtime_log", str(runtime_log_path))
            raise
        log_event(
            "request_complete",
            ok=result.get("schematic_verify", {}).get("ok", result.get("ok", False)),
            updated_refs=result.get("updated_refs", []),
            missing_refs=result.get("missing_refs", []),
            recovery_cycles=result.get("schematic_verify", {}).get("recovery_cycles", 0),
        )
        result["sync_attempt_id"] = attempt_id
        result["runtime_log"] = str(runtime_log_path)
        return result

    def _sync_schematic_impl(self, payload: dict[str, object], log_event) -> dict[str, object]:
        refs = split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))
        raw_fields = payload.get("field_overrides", {})
        if not refs:
            raise ValueError("ref is required")
        if not isinstance(raw_fields, dict):
            raise ValueError("field_overrides must be an object")
        fields: dict[str, object] = {}
        lcsc_code = str(raw_fields.get("lcsc_c_code", "") or "").strip()
        for key in SCHEMATIC_SYNC_FIELDS:
            if key not in raw_fields:
                continue
            value = raw_fields.get(key, "")
            if key in {"lcsc_c_code", "lcsc_url", "description"} and not str(value or "").strip():
                continue
            fields[key] = value
        if lcsc_code and "lcsc_url" not in fields:
            fields["lcsc_url"] = lcsc_code_url(lcsc_code)
        if not fields:
            raise ValueError("no editable fields were provided")
        log_event("fields_normalized", fields=fields)

        raw_per_ref_fields = payload.get("per_ref_fields", {})
        if raw_per_ref_fields and not isinstance(raw_per_ref_fields, dict):
            raise ValueError("per_ref_fields must be an object")
        normalized_per_ref_fields = {
            str(ref).strip().upper(): value
            for ref, value in raw_per_ref_fields.items()
            if isinstance(raw_per_ref_fields, dict) and isinstance(value, dict)
        }
        mixed_per_ref_fields = {
            key for key in SCHEMATIC_SYNC_FIELDS
            if len({
                str(ref_fields.get(key, "") or "").strip()
                for ref_fields in normalized_per_ref_fields.values()
            }) > 1
        }
        ref_field_map: dict[str, dict[str, object]] = {}
        for ref in refs:
            normalized_ref = ref.strip().upper()
            ref_fields = normalized_per_ref_fields.get(normalized_ref, {})
            merged = {key: value for key, value in fields.items() if key in SCHEMATIC_SYNC_FIELDS}
            merged.update({
                key: ref_fields[key]
                for key in mixed_per_ref_fields
                if key in ref_fields
            })
            merged.update({key: value for key, value in fields.items() if key in EDITABLE_FIELDS})
            if merged.get("lcsc_c_code") and not merged.get("lcsc_url"):
                merged["lcsc_url"] = lcsc_code_url(str(merged["lcsc_c_code"]))
            if merged:
                ref_field_map[normalized_ref] = merged
        if not ref_field_map:
            raise ValueError("no editable fields were provided")
        preflight = self.verify_schematic_field_values(ref_field_map)
        if preflight.get("ok") and not preflight.get("missing_refs"):
            preflight["phase"] = "already_synced_on_disk"
            log_event("already_synced", verify=preflight)
            self.record_schematic_sync_overrides({}, ref_field_map)
            return {
                "ok": True, "already_synced": True, "updated_refs": [],
                "missing_refs": [], "changes": {}, "pages": {}, "paths": [],
                "schematic_verify": preflight,
                "kicad_prepare": {"ok": True, "skipped": True},
                "kicad_notify": {"ok": True, "skipped": True},
            }
        editable_fields = {key: fields[key] for key in EDITABLE_FIELDS if key in fields}
        if editable_fields:
            for ref, ref_fields in ref_field_map.items():
                self.save_field_override({
                    "ref": ref,
                    "field_overrides": {key: ref_fields[key] for key in EDITABLE_FIELDS if key in ref_fields},
                    "note": payload.get("note", ""),
                })
            log_event("field_overrides_saved", fields=editable_fields)
        kicad_prepare = self.prepare_kicad_for_external_schematic_write(refs)
        log_event("kicad_prepare_complete", result=kicad_prepare)
        self.require_kicad_prepare_success(kicad_prepare)
        result = self.write_schematic_fields(self.config["schematic_dir"], ref_field_map)
        log_event(
            "schematic_write_complete",
            updated_refs=result.get("updated_refs", []),
            missing_refs=result.get("missing_refs", []),
            paths=result.get("paths", []),
        )
        verify, retry_results = self.ensure_schematic_fields_verified(
            ref_field_map,
            lambda: self.write_schematic_fields(self.config["schematic_dir"], ref_field_map),
            "after_write",
        )
        max_verify_attempts = int(verify.get("attempts", 1) or 1)
        for retry_result in retry_results:
            result = self.merge_schematic_update_results(result, retry_result)
        log_event("schematic_write_verified", verify=verify, rewrite_count=len(retry_results))
        notify_result = result
        if not result.get("updated_refs") and refs:
            notify_result = dict(result)
            notify_result["updated_refs"] = refs
        kicad_notify = (
            self.notify_kicad_schematic_changed(notify_result)
            if payload.get("notify", True) is not False
            else {"ok": True, "message": "notification skipped"}
        )
        log_event("kicad_notify_complete", result=kicad_notify)
        recovery_prepares: list[dict[str, object]] = []
        recovery_notifies: list[dict[str, object]] = []
        if payload.get("notify", True) is not False:
            verify = self.verify_schematic_after_notify(
                ref_field_map,
                attempts=max_verify_attempts + 1,
            )
            log_event("kicad_notify_verified", verify=verify)
            if not verify.get("ok"):
                result, verify, recovery_prepares, recovery_notifies = self.recover_schematic_after_notify(
                    refs,
                    ref_field_map,
                    lambda: self.write_schematic_fields(self.config["schematic_dir"], ref_field_map),
                    result,
                    verify,
                    log_event,
                )
        self.record_schematic_sync_overrides(
            result,
            ref_field_map if verify.get("ok") else {},
        )
        log_path = self.append_schematic_sync_log(result) if result.get("changes") else self.bom_dir / SCHEMATIC_SYNC_LOG
        result["sync_log"] = str(log_path)
        result["kicad_prepare"] = kicad_prepare
        result["kicad_notify"] = kicad_notify
        result["kicad_recovery_prepares"] = recovery_prepares
        result["kicad_recovery_notifies"] = recovery_notifies
        result["schematic_verify"] = verify
        return result

    def bulk_sync_bom_state_to_schematic(
        self,
        selected_bom: str,
        ref_map: dict[str, str] | None = None,
        dry_run: bool = False,
        notify: bool = True,
        prepare: bool = True,
        selected_refs: list[str] | None = None,
    ) -> dict[str, object]:
        attempt_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        started_at = time.perf_counter()
        runtime_log_path = runtime_directory(self.project_root) / SCHEMATIC_SYNC_RUNTIME_LOG

        def log_event(event: str, **details: object) -> None:
            self.append_schematic_sync_runtime_log(attempt_id, event, selected_refs or [], started_at, details)

        log_event("bulk_request_started", bom=selected_bom, dry_run=dry_run, notify=notify)
        try:
            with SCHEMATIC_SYNC_TRANSACTION_LOCK:
                log_event("bulk_transaction_lock_acquired")
                result = self._bulk_sync_bom_state_to_schematic(
                    selected_bom,
                    ref_map=ref_map,
                    dry_run=dry_run,
                    notify=notify,
                    prepare=prepare,
                    selected_refs=selected_refs,
                    log_event=log_event,
                )
        except Exception as exc:
            log_event("bulk_request_failed", error=type(exc).__name__, message=str(exc), traceback=traceback.format_exc())
            setattr(exc, "sync_attempt_id", attempt_id)
            setattr(exc, "runtime_log", str(runtime_log_path))
            raise
        log_event("bulk_request_complete", updated_refs=result.get("updated_refs", []),
                  missing_refs=result.get("missing_refs", []), verify=result.get("schematic_verify", {}))
        result["sync_attempt_id"] = attempt_id
        result["runtime_log"] = str(runtime_log_path)
        return result

    def _bulk_sync_bom_state_to_schematic(
        self,
        selected_bom: str,
        ref_map: dict[str, str] | None = None,
        dry_run: bool = False,
        notify: bool = True,
        prepare: bool = True,
        selected_refs: list[str] | None = None,
        log_event=lambda *_args, **_kwargs: None,
    ) -> dict[str, object]:
        ref_map = {str(key).upper(): str(value).upper() for key, value in (ref_map or {}).items()}
        rows = self.rows("", selected_bom, include_stock=False)
        selection = None
        if selected_refs is not None:
            if not isinstance(selected_refs, list) or not selected_refs or any(
                not isinstance(ref, str) or not ref.strip() for ref in selected_refs
            ):
                raise ValueError("selected_refs must be a non-empty list of designators")
            selection = {ref.strip().upper() for ref in selected_refs}
            available = {str(ref).upper() for row in rows for ref in row.get("refs_list", [])}
            unknown = selection - available
            if unknown:
                raise ValueError("Selected designators are no longer in this BOM; reload before syncing: "
                                 + ",".join(sorted(unknown, key=ref_sort_key)))
            rows = [row for row in rows if selection.intersection(row.get("refs_list", []))]
        schematic_parts = self.schematic_parts()
        aggregate: dict[str, object] = {
            "ok": True,
            "rows": len(rows),
            "target_refs": 0,
            "updated_refs": [],
            "missing_refs": [],
            "remapped_refs": {},
            "changes": {},
            "pages": {},
            "paths": [],
            "dry_run": dry_run,
            "scope": "selected" if selection is not None else "all",
        }
        missing_seen: set[str] = set()
        target_seen: set[str] = set()
        remapped_refs: dict[str, str] = {}
        first_existing_refs: list[str] = []
        ref_field_map: dict[str, dict[str, object]] = {}

        for row in rows:
            source_refs = [str(ref or "").strip().upper() for ref in row.get("refs_list", []) if str(ref or "").strip()]
            target_refs: list[str] = []
            for source_ref in source_refs:
                if selection is not None and source_ref not in selection:
                    continue
                target_ref = ref_map.get(source_ref, source_ref)
                if target_ref != source_ref:
                    remapped_refs[source_ref] = target_ref
                if target_ref in target_seen:
                    continue
                target_seen.add(target_ref)
                aggregate["target_refs"] = int(aggregate["target_refs"]) + 1
                if target_ref not in schematic_parts:
                    if target_ref not in missing_seen:
                        missing_seen.add(target_ref)
                        aggregate["missing_refs"].append(target_ref)
                    continue
                target_refs.append(target_ref)
                if not first_existing_refs:
                    first_existing_refs.append(target_ref)

            if not target_refs:
                continue

            source_fields = row.get("per_ref_sync_fields") or {}
            common_fields = row_schematic_sync_fields(row)
            for source_ref, target_ref in zip(source_refs, [ref_map.get(ref, ref) for ref in source_refs]):
                if target_ref not in target_refs:
                    continue
                fields = {**common_fields, **dict(source_fields.get(source_ref, {}))}
                if not fields.get("description") and common_fields.get("description"):
                    fields["description"] = common_fields["description"]
                    if common_fields.get("description_c_code"):
                        fields["lcsc_c_code"] = common_fields["description_c_code"]
                        fields["lcsc_url"] = lcsc_code_url(common_fields["description_c_code"])
                ref_field_map[target_ref] = fields

        aggregate["remapped_refs"] = dict(sorted(remapped_refs.items(), key=lambda item: ref_sort_key(item[0])))
        aggregate["missing_refs"] = sorted(aggregate["missing_refs"], key=ref_sort_key)
        aggregate["updatable_refs"] = len(ref_field_map)
        log_event("bulk_targets_ready", rows=len(rows), target_refs=aggregate["target_refs"],
                  updatable_refs=len(ref_field_map), missing_refs=aggregate["missing_refs"])
        if dry_run:
            return aggregate

        if not ref_field_map:
            aggregate["schematic_verify"] = {"ok": True, "skipped": True, "message": "No existing targets to write"}
            aggregate["kicad_notify"] = {"ok": True, "skipped": True}
            return aggregate

        # A repeated request must not interrupt KiCad when the saved fields match.
        preflight = self.verify_schematic_field_values(ref_field_map)
        if preflight.get("ok") and not preflight.get("missing_refs"):
            preflight["phase"] = "already_synced_on_disk"
            aggregate["already_synced"] = True
            aggregate["schematic_verify"] = preflight
            aggregate["kicad_prepare"] = {"ok": True, "skipped": True}
            aggregate["kicad_notify"] = {"ok": True, "skipped": True}
            log_event("bulk_already_synced", verify=preflight)
            self.record_schematic_sync_overrides({}, ref_field_map)
            return aggregate

        log_event("bulk_prepare_started")
        kicad_prepare = self.prepare_kicad_for_external_schematic_write(first_existing_refs) if prepare and first_existing_refs else {
            "ok": False,
            "message": "preparation skipped" if not prepare else "no existing refs to prepare",
        }
        log_event("bulk_prepare_complete", result=kicad_prepare)
        if prepare and first_existing_refs:
            self.require_kicad_prepare_success(kicad_prepare)
        result = self.write_schematic_fields(self.config["schematic_dir"], ref_field_map)
        log_event("bulk_write_complete", updated_refs=result.get("updated_refs", []))
        verify, retry_results = self.ensure_schematic_fields_verified(
            ref_field_map,
            lambda: self.write_schematic_fields(self.config["schematic_dir"], ref_field_map),
            "after_write",
        )
        log_event("bulk_write_verified", verify=verify)
        max_verify_attempts = int(verify.get("attempts", 1) or 1)
        for retry_result in retry_results:
            result = self.merge_schematic_update_results(result, retry_result)
        changes = result.get("changes", {})
        pages = result.get("pages", {})
        paths = result.get("paths", [])
        if isinstance(changes, dict):
            aggregate["changes"].update(changes)
        if isinstance(pages, dict):
            aggregate["pages"].update(pages)
        if isinstance(paths, list):
            aggregate["paths"] = sorted(str(path) for path in paths)
        aggregate["updated_refs"] = sorted(aggregate["changes"], key=ref_sort_key)
        kicad_notify = self.notify_kicad_schematic_changed(aggregate, fallback_refs=first_existing_refs) if notify else {
            "ok": True,
            "message": "notification skipped",
        }
        log_event("bulk_notify_complete", result=kicad_notify)
        recovery_prepares: list[dict[str, object]] = []
        recovery_notifies: list[dict[str, object]] = []
        if notify:
            verify = self.verify_schematic_after_notify(
                ref_field_map,
                attempts=max_verify_attempts + 1,
            )
            if not verify.get("ok"):
                result, verify, recovery_prepares, recovery_notifies = self.recover_schematic_after_notify(
                    first_existing_refs,
                    ref_field_map,
                    lambda: self.write_schematic_fields(self.config["schematic_dir"], ref_field_map),
                    result,
                    verify,
                    log_event,
                )
                changes = result.get("changes", {})
                pages = result.get("pages", {})
                paths = result.get("paths", [])
                if isinstance(changes, dict):
                    aggregate["changes"].update(changes)
                if isinstance(pages, dict):
                    aggregate["pages"].update(pages)
                if isinstance(paths, list):
                    aggregate["paths"] = sorted(set([*aggregate["paths"], *[str(path) for path in paths]]))
                aggregate["updated_refs"] = sorted(aggregate["changes"], key=ref_sort_key)
        log_event("bulk_record_overrides_started", verify=verify)
        self.record_schematic_sync_overrides(
            aggregate,
            ref_field_map if verify.get("ok") else {},
        )
        log_event("bulk_record_overrides_complete")
        log_path = self.append_schematic_sync_log(aggregate) if aggregate["changes"] else self.bom_dir / SCHEMATIC_SYNC_LOG
        aggregate["sync_log"] = str(log_path)
        aggregate["kicad_prepare"] = kicad_prepare
        aggregate["kicad_notify"] = kicad_notify
        aggregate["kicad_recovery_prepares"] = recovery_prepares
        aggregate["kicad_recovery_notifies"] = recovery_notifies
        aggregate["schematic_verify"] = verify
        return aggregate

    def export_changes(self, payload: dict[str, object]) -> dict[str, object]:
        selected_bom = str(payload.get("bom", "") or "")
        query_refs = str(payload.get("refs", "") or "")
        refs = self.refs(query_refs, selected_bom)
        if self.is_schematic_source(selected_bom):
            schematic = self.schematic_parts()
            parts = [schematic[ref] for ref in refs if ref in schematic]
        else:
            selected_path = self.resolve_bom_path(selected_bom)
            lcsc_bom = selected_path if selected_path.suffix.lower() in {".xlsx", ".xlsm"} or "LCSC" in selected_path.name.upper() else self.lcsc_bom()
            parts = merge_part_data(refs, self.project_root, selected_path, lcsc_bom, self.config["schematic_dir"],
                                    field_mapping=self.config.get("field_mapping"))
        overrides = load_overrides(self.overrides_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = self.bom_dir / f"{EXPORT_PREFIXES[1]}{timestamp}.csv"
        columns = [
            "Designator",
            "Page",
            "OldComment",
            "NewComment",
            "OldValue",
            "NewValue",
            "OldFootprint",
            "NewFootprint",
            "OldCCode",
            "NewCCode",
            "Decision",
            "Status",
            "Note",
            "SchematicSyncStatus",
            "OverrideUpdatedAt",
        ]
        rows: list[dict[str, str]] = []
        for part in parts:
            override = overrides.get(part.ref, {})
            if not override:
                continue
            fields = effective_part_fields(part, override)
            field_changed = any(fields[key] != str(getattr(part, key, "") or "") for key in EDITABLE_FIELDS)
            old_code = str(part.accepted_c_code or part.lcsc_match_c_code or "").strip()
            new_code = choose_effective_c_code(
                {"accepted_c_code": part.accepted_c_code, "lcsc_match_c_code": part.lcsc_match_c_code},
                override,
            )
            code_changed = old_code != new_code
            if not field_changed and not code_changed and not override.get("note") and not override.get("status"):
                continue
            rows.append(
                {
                    "Designator": part.ref,
                    "Page": part.page,
                    "OldComment": part.comment,
                    "NewComment": fields["comment"],
                    "OldValue": part.value,
                    "NewValue": fields["value"],
                    "OldFootprint": part.footprint,
                    "NewFootprint": fields["footprint"],
                    "OldCCode": old_code,
                    "NewCCode": new_code,
                    "Decision": str(override.get("decision", "")),
                    "Status": str(override.get("status", "")),
                    "Note": str(override.get("note", "")),
                    "SchematicSyncStatus": "NeedsSchematicUpdate" if field_changed else "NoFieldChange",
                    "OverrideUpdatedAt": str(override.get("updated_at", "")),
                }
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        return {"ok": True, "path": str(output_path), "count": len(rows)}

    def export_pump_table(self, payload: dict[str, object]) -> dict[str, object]:
        selected_bom = str(payload.get("bom", "") or "")
        query_refs = str(payload.get("refs", "") or "")
        export_filter = str(payload.get("export_filter", "current_filter") or "current_filter")
        active_filters = {
            str(item)
            for item in payload.get("active_filters", [])
            if str(item) and str(item) != "all"
        }
        rows = self.rows(query_refs, selected_bom, include_stock=False)

        def include_row(row: dict[str, object]) -> bool:
            if not row_is_pump_exportable(row):
                return False
            keys = row_filter_keys(row)
            if export_filter == "current_filter":
                return not active_filters or active_filters.issubset(keys)
            if export_filter == "stock_issue":
                return row_stock_is_issue(row)
            if export_filter == "low_stock":
                return row_stock_is_low(row)
            if export_filter == "stock_error":
                return row_stock_is_error(row)
            if export_filter == "pending_review":
                return row_review_status(row) != "Confirmed"
            if export_filter == "needs_sync":
                return bool(row.get("field_changed_refs"))
            if export_filter == "all_non_nc":
                return True
            return True

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = self.bom_dir / f"{EXPORT_PREFIXES[0]}{timestamp}.csv"
        columns = [
            "Designator",
            "Quantity",
            "Comment",
            "Value",
            "Footprint",
            "LCSC",
            "LCSC_URL",
            "Stock",
            "Delivery",
            "MOQ",
            "MPQ",
            "Price",
            "Param",
            "ReviewStatus",
            "PerReferenceSelection",
            "SchematicSyncStatus",
            "Note",
        ]
        export_rows: list[dict[str, str]] = []
        for row in rows:
            if not include_row(row):
                continue
            stock = row.get("stock") if isinstance(row.get("stock"), dict) else {}
            c_code = row_effective_c_code(row)
            export_rows.append(
                {
                    "Designator": str(row.get("refs") or row.get("ref") or ""),
                    "Quantity": str(row.get("quantity") or ""),
                    "Comment": str(row.get("comment") or ""),
                    "Value": str(row.get("value") or ""),
                    "Footprint": str(row.get("footprint") or ""),
                    "LCSC": c_code,
                    "LCSC_URL": lcsc_code_url(c_code) if c_code else "",
                    "Stock": str(stock.get("stock", "")) if not stock.get("entries") else "; ".join(
                        f"{item['ref']}:{item['stock'].get('stock', item['stock'].get('status', ''))}"
                        for item in stock["entries"]
                    ),
                    "Delivery": str(stock.get("delivery", "")),
                    "MOQ": str(stock.get("moq", "")),
                    "MPQ": str(stock.get("mpq", "")),
                    "Price": str(stock.get("price_ladder") or stock.get("price") or ""),
                    "Param": stock_param_text(stock),
                    "ReviewStatus": row_review_status(row),
                    "PerReferenceSelection": "; ".join(
                        f"{ref}: C={item.get('effective_c_code') or '-'}, review={item.get('review_status')}, "
                        f"alternates={','.join(alt.get('c_code', '') for alt in item.get('alternate_parts', [])) or '-'}, "
                        f"order={item.get('procurement_choice', {}).get('supplier') or '-'}"
                        for ref, item in (row.get("per_ref_selection") or {}).items()
                    ),
                    "SchematicSyncStatus": "NeedsSchematicUpdate" if row.get("field_changed_refs") else "SyncedOrNoDiff",
                    "Note": str(row.get("note") or ""),
                }
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            writer.writerows(export_rows)
        return {"ok": True, "path": str(output_path), "count": len(export_rows), "filter": export_filter}

    def open_project_path(self) -> dict[str, object]:
        path = self.project_root
        if not path.exists():
            return {"ok": False, "error": f"工程路径不存在：{path}", "path": str(path)}
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": str(path)}
        return {"ok": True, "path": str(path)}

    def open_lcsc_search(self, keyword: str, url: str = "") -> dict[str, object]:
        keyword = str(keyword or "").strip()
        requested_url = str(url or "").strip()
        if not keyword and not requested_url:
            return {"ok": False, "error": "搜索关键词不能为空"}
        if not requested_url and re.match(r"^https?://", keyword, flags=re.IGNORECASE):
            requested_url = keyword
        if requested_url:
            parsed = urlparse(requested_url)
            host = parsed.hostname or ""
            allowed = host == "szlcsc.com" or host.endswith(".szlcsc.com") or host == "lcsc.com" or host.endswith(".lcsc.com")
            if parsed.scheme not in {"http", "https"} or not allowed:
                return {"ok": False, "error": f"不允许打开非立创链接：{requested_url}", "url": requested_url}
            url = requested_url
        else:
            url = f"https://so.szlcsc.com/global.html?k={quote(keyword)}"
        chrome = find_chrome_executable()
        try:
            if chrome:
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                monitor_args, monitor = secondary_monitor_window_args()
                before_handles = chrome_window_handles()
                chrome_profile = Path(tempfile.gettempdir()) / "part-selection-lcsc-chrome"
                process = subprocess.Popen(
                    [
                        chrome,
                        f"--user-data-dir={chrome_profile}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--app=" + url,
                        "--start-maximized",
                        "--new-window",
                        *monitor_args,
                    ],
                    creationflags=creationflags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if monitor:
                    threading.Thread(
                        target=place_chrome_window_on_monitor,
                        args=(before_handles, monitor, process.pid),
                        daemon=True,
                    ).start()
                return {
                    "ok": True,
                    "mode": "chrome-app",
                    "fullscreen": False,
                    "maximized": True,
                    "monitor": monitor or "primary",
                    "url": url,
                }
            webbrowser.open(url)
            return {"ok": True, "mode": "default-browser", "url": url}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": url}

    def save_refs(self, refs_text: str) -> None:
        input_refs = self.config.get("input_refs")
        input_refs.parent.mkdir(parents=True, exist_ok=True)
        input_refs.write_text(",".join(split_refs(refs_text)) + "\n", encoding="utf-8")


def json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def text_response(handler: BaseHTTPRequestHandler, text: str, content_type: str = "text/html; charset=utf-8") -> None:
    data = text.encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(app: PartSelectionApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        @app.settings_gate.request
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                text_response(self, render_index(app))
                return
            if parsed.path == "/api/selection_job":
                query = parse_qs(parsed.query)
                json_response(self, app.selection_jobs.status(query.get("id", [""])[0]))
                return
            if parsed.path == "/api/rows":
                query = parse_qs(parsed.query)
                refs = query.get("refs", [""])[0]
                selected_bom = query.get("bom", [""])[0]
                include_stock = query.get("stock", ["1"])[0] != "0"
                json_response(
                    self,
                    {
                        "rows": app.rows(refs, selected_bom, include_stock),
                        "overrides_path": str(app.overrides_path),
                        "stock_cache_path": str(app.stock_cache_path),
                    },
                )
                return
            if parsed.path == "/api/boms":
                json_response(self, {"boms": app.available_boms(), "default_bom": SCHEMATIC_SOURCE_BOM, "project_root": str(app.project_root)})
                return
            if parsed.path == "/api/local_inventory":
                try:
                    json_response(self, {'ok': True, **load_inventory(app.local_inventory_path)})
                except (ValueError, OSError) as error:
                    json_response(self, {'ok': False, 'error': str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/stock":
                c_code = parse_qs(parsed.query).get("code", [""])[0]
                json_response(self, {"stock": app.stock(c_code)})
                return
            if parsed.path == "/api/lcsc_search":
                query = parse_qs(parsed.query)
                keyword = query.get("keyword", [""])[0]
                include_substitutes = query.get("include_substitutes", ["1"])[0] == "1"
                if query.get("progressive", ["0"])[0] == "1":
                    job_id = query.get("job", [""])[0]
                    if job_id:
                        result = app.search_jobs.status(job_id)
                    else:
                        try:
                            sequence = int(query.get("sequence", ["0"])[0])
                        except ValueError:
                            json_response(self, {"ok": False, "done": True, "error": "Invalid sequence", "items": []})
                            return
                        result = app.search_jobs.start(keyword, include_substitutes,
                            query.get("client", [""])[0], sequence,
                            force_refresh=query.get("force", ["0"])[0] == "1")
                    json_response(self, result)
                else:
                    json_response(self, app.lcsc_search(keyword, include_substitutes))
                return
            if parsed.path == "/api/config":
                config = {key: str(value) if isinstance(value, Path) else value for key, value in app.config.items()}
                config["desktop_launcher_version"] = 3
                config["desktop_owner_id"] = app.lifecycle.identity if app.lifecycle else ''
                config["backend_directory"] = str(Path(__file__).resolve().parent)
                json_response(self, config)
                return
            if parsed.path == "/api/path_settings":
                json_response(self, settings_snapshot(app.config))
                return
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        @app.settings_gate.request
        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                payload = json.loads(body)
                if parsed.path == "/api/desktop_shutdown":
                    if not app.lifecycle:
                        raise ValueError('此后台不是由独立工作台启动的，不能从网页关闭')
                    json_response(self, app.lifecycle.shutdown(self.headers.get('X-Desktop-Token','')))
                    return
                if parsed.path == "/api/path_settings":
                    json_response(self, app.save_path_settings(payload))
                    return
                if parsed.path == "/api/local_inventory/preview":
                    json_response(self, app.preview_local_inventory(payload))
                    return
                if parsed.path == "/api/local_inventory/import":
                    json_response(self, app.import_local_inventory(payload))
                    return
                if parsed.path == "/api/spec_check":
                    json_response(self, app.check_specs(payload))
                    return
                if parsed.path == "/api/path_picker":
                    json_response(self, choose_path(app.config, payload))
                    return
                if parsed.path == "/api/field_mapping":
                    json_response(self, preview_field_mapping(app.config, payload))
                    return
                if parsed.path == "/api/open_settings_directory":
                    json_response(self, open_settings_directory(app.config, payload))
                    return
                if parsed.path == "/api/lcsc_search_cancel":
                    client = str(payload.get("client", ""))
                    if not client or len(client) > 100:
                        raise ValueError("Invalid search client")
                    json_response(self, app.search_jobs.cancel(client, int(payload.get("sequence", 0))))
                    return
                if parsed.path == "/api/kicad_jump":
                    json_response(self, app.jump_to_schematic(str(payload.get("url", ""))))
                    return
                if parsed.path == "/api/open_path_config":
                    if os.name != "nt":
                        raise ValueError("Open the configuration JSON in your text editor")
                    subprocess.Popen(["notepad.exe", str(app.config["config_path"])])
                    json_response(self, {"ok": True, "path": str(app.config["config_path"])})
                    return
                if parsed.path == "/api/override":
                    json_response(self, app.save_override(payload))
                    return
                if parsed.path == "/api/candidate_parameters":
                    json_response(self, app.candidate_parameters(payload))
                    return
                if parsed.path == "/api/undo_workbench":
                    json_response(self, app.undo_workbench_changes(payload))
                    return
                if parsed.path == "/api/field_override":
                    json_response(self, app.save_field_override(payload))
                    return
                if parsed.path == "/api/start_selection":
                    json_response(self, app.selection_jobs.start(payload))
                    return
                if parsed.path == "/api/select_candidate":
                    json_response(self, app.select_candidate(payload))
                    return
                if parsed.path == "/api/library_footprint":
                    json_response(self, {"ok": True, "detail": app.footprint_choices.library_detail(
                        str(payload.get("preview_id", "")), str(payload.get("id", "")),
                        render=payload.get("render") is True)})
                    return
                if parsed.path == "/api/footprint_comparison":
                    json_response(self, {"ok": True, "detail": app.footprint_choices.comparison(
                        payload.get("preview_id"), payload.get("id"))})
                    return
                if parsed.path == "/api/adopt_schematic_fields":
                    json_response(self, app.adopt_schematic_fields(payload))
                    return
                if parsed.path == "/api/alternate_parts":
                    json_response(self, app.save_alternate_parts(payload))
                    return
                if parsed.path == "/api/procurement_choice":
                    json_response(self, app.save_procurement_choice(payload))
                    return
                if parsed.path == "/api/sync_schematic":
                    json_response(self, app.sync_schematic(payload))
                    return
                if parsed.path == "/api/bulk_sync_schematic":
                    if "selected_refs" in payload and not isinstance(payload["selected_refs"], list):
                        raise ValueError("selected_refs must be a non-empty list of designators")
                    json_response(
                        self,
                        app.bulk_sync_bom_state_to_schematic(
                            str(payload.get("bom", "")),
                            ref_map={} if "selected_refs" in payload or payload.get("bom") == SCHEMATIC_SOURCE_BOM else app.config.get("bulk_sync_ref_map", DEFAULT_BULK_SYNC_REF_MAP),
                            notify=payload.get("notify", True) is not False,
                            selected_refs=payload.get("selected_refs"),
                        ),
                    )
                    return
                if parsed.path == "/api/export_changes":
                    json_response(self, app.export_changes(payload))
                    return
                if parsed.path == "/api/export_pump_table":
                    json_response(self, app.export_pump_table(payload))
                    return
                if parsed.path == "/api/open_project_path":
                    json_response(self, app.open_project_path())
                    return
                if parsed.path == "/api/open_lcsc_search":
                    json_response(
                        self,
                        app.open_lcsc_search(
                            str(payload.get("keyword", "")),
                            str(payload.get("url", "")),
                        ),
                    )
                    return
                if parsed.path == "/api/refs":
                    app.save_refs(str(payload.get("refs", "")))
                    json_response(self, {"ok": True})
                    return
            except Exception as exc:
                error_payload = {"ok": False, "error": str(exc)}
                sync_attempt_id = getattr(exc, "sync_attempt_id", "")
                runtime_log = getattr(exc, "runtime_log", "")
                if sync_attempt_id:
                    error_payload["sync_attempt_id"] = sync_attempt_id
                if runtime_log:
                    error_payload["runtime_log"] = runtime_log
                json_response(self, error_payload, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def render_index(app: PartSelectionApp) -> str:
    config = app.config
    icon_sprite = (Path(__file__).resolve().parent / "web" / "toolbar-icons.svg").read_text(encoding="utf-8")
    footprint_css = (Path(__file__).resolve().parent / "web" / "footprint-compare.css").read_text(encoding="utf-8")
    footprint_js = (Path(__file__).resolve().parent / "web" / "footprint-compare.js").read_text(encoding="utf-8")
    row_edits_js = (Path(__file__).resolve().parent / "web" / "row-edits.js").read_text(encoding="utf-8")
    row_filters_js = (Path(__file__).resolve().parent / "web" / "row-filters.js").read_text(encoding="utf-8")
    row_filters_css = (Path(__file__).resolve().parent / "web" / "row-filters.css").read_text(encoding="utf-8")
    path_settings_js = (Path(__file__).resolve().parent / "web" / "path-settings.js").read_text(encoding="utf-8")
    path_settings_css = (Path(__file__).resolve().parent / "web" / "path-settings.css").read_text(encoding="utf-8")
    desktop_lifecycle_js = (Path(__file__).resolve().parent / "web" / "desktop-lifecycle.js").read_text(encoding="utf-8")
    plugin_updates_js = (Path(__file__).resolve().parent / "web" / "plugin-updates.js").read_text(encoding="utf-8")
    candidate_desktop_js = (Path(__file__).resolve().parent / "web" / "candidate-desktop.js").read_text(encoding="utf-8")
    inventory_js = (Path(__file__).resolve().parent / "web" / "local-inventory.js").read_text(encoding="utf-8")
    inventory_css = (Path(__file__).resolve().parent / "web" / "local-inventory.css").read_text(encoding="utf-8")
    spec_warnings_js = (Path(__file__).resolve().parent / "web" / "spec-warnings.js").read_text(encoding="utf-8")
    spec_warnings_css = (Path(__file__).resolve().parent / "web" / "spec-warnings.css").read_text(encoding="utf-8")
    value_parameters_js = (Path(__file__).resolve().parent / "web" / "value-parameters.js").read_text(encoding="utf-8")
    value_parameters_css = (Path(__file__).resolve().parent / "web" / "value-parameters.css").read_text(encoding="utf-8")

    def toolbar_content(icon: str, label: str, count: bool = False) -> str:
        return (f'<svg class="toolbar-icon" aria-hidden="true"><use href="#toolbar-icon-{icon}"></use></svg>'
                f'<span data-button-label>{html.escape(label)}</span>'
                + ('<span class="toolbar-count" data-button-count aria-hidden="true">0</span>' if count else ''))

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BOM 器件选型工作台</title>
<script>const candidateOnly = new URLSearchParams(location.search).get('view') === 'candidate';
if (candidateOnly) document.documentElement.classList.add('candidate-only');</script>
<style>
html.candidate-only body > header, html.candidate-only body > main,
html.candidate-only .workbench-statusbar {{ display: none !important; }}
html.candidate-only body {{ padding: 0; }}
html.candidate-only #candidateModal {{ padding: 0; background: transparent; }}
html.candidate-only #candidateWindow {{ position: fixed !important; inset: 0 !important;
 width: 100% !important; height: 100% !important; max-width: none; max-height: none;
 min-width: 0; min-height: 0; border: 0; border-radius: 0; box-shadow: none; }}
html.candidate-only #candidateWindow > .modal-head,
html.candidate-only #candidateWindow > [data-resize],
html.candidate-only #candidateMinimizedBar {{ display: none !important; }}
</style>
<style>
body, input, textarea, [contenteditable], td, th, button {{ -webkit-user-select: text !important; user-select: text !important; }}
body {{ --sticky-toolbar-height: 0px; --sticky-filter-height: 0px; --statusbar-height: 28px; margin: 0; padding-bottom: var(--statusbar-height); font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif; color: #26323a; background: #fff; letter-spacing: 0; }}
.workbench-chrome {{ position: sticky; top: 0; left: 0; z-index: 8; background: #f5f5f5; }}
.toolbar {{ display: flex; flex-wrap: wrap; align-items: stretch; gap: 6px 0; min-height: 58px; padding: 4px 8px; box-sizing: border-box; background: #f1f2f3; border-bottom: 1px solid #bec4ca; position: relative; z-index: 5; }}
.app-menubar {{ display: flex; align-items: center; gap: 2px; min-height: 27px; padding: 0 8px; background: #f5f5f5; border-bottom: 1px solid #c8d1da; position: relative; z-index: 8; font-size: 13px; }}
.menubar-spacer {{ flex: 1 1 auto; min-width: 8px; }}
.project-path-link {{ display: inline-block; max-width: min(720px, 55vw); padding: 1px 5px; border: 0; background: transparent; color: #52606d; text-align: right; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }}
.project-path-link:hover {{ color: #1f4e78; text-decoration: underline; box-shadow: none; }}
.app-menu {{ position: relative; }}
.app-menu > button {{ min-height: 24px; padding: 2px 9px; border: 1px solid transparent; border-radius: 3px; background: transparent; }}
.app-menu > button:hover, .app-menu.open > button {{ background: #fff; border-color: #c8d1da; box-shadow: none; }}
.menu-popup {{ display: none; position: absolute; top: 27px; left: 0; min-width: 210px; padding: 4px; background: #fff; border: 1px solid #b8c7d6; border-radius: 3px; box-shadow: 0 8px 20px rgba(15, 35, 55, .18); }}
.app-menu.open .menu-popup {{ display: block; }}
.menu-popup > button, .menu-popup .view-wrap > button {{ display: block; width: 100%; border: 0; border-radius: 2px; padding: 7px 9px; text-align: left; background: #fff; box-shadow: none; white-space: nowrap; }}
.menu-popup > button:hover, .menu-popup .view-wrap > button:hover {{ background: #edf5fb; }}
.menu-popup > button:focus-visible, [data-menu-trigger]:focus-visible {{ outline: 2px solid #237bb5; outline-offset: -2px; }}
.rows-empty-state {{ padding: 28px 16px; text-align: center; color: #526375; }}
.rows-empty-state button {{ margin-left: 12px; }}
.menu-popup .view-wrap {{ display: block; }}
.menu-popup .view-panel {{ top: 0; left: calc(100% + 2px); right: auto; }}
.toolbar-section {{ display: flex; flex: 0 0 auto; flex-direction: column; gap: 2px; min-width: 0; margin: 0; padding: 0 12px; border: 0; border-right: 1px solid #cbd0d4; background: transparent; }}
.toolbar-section:first-child {{ padding-left: 2px; }}
.toolbar-section:last-child {{ border-right: 0; }}
.section-title {{ color: #6b7680; font-size: 11px; line-height: 13px; padding-left: 4px; }}
.section-actions {{ display: flex; align-items: center; gap: 3px; }}
.source-section select {{ box-sizing: border-box; width: 177px; height: 28px; min-width: 0; margin: 0 3px 0 0; padding: 0 5px; border: 1px solid #9da7b0; border-radius: 2px; background: #fff; font-size: 12px; }}
.toolbar .tool-button {{ display: inline-flex; align-items: center; justify-content: center; gap: 5px; height: 29px; padding: 3px 7px; border: 1px solid transparent; border-radius: 2px; color: #334651; background: transparent; box-shadow: none; font-size: 12px; font-weight: 400; white-space: nowrap; }}
.toolbar .tool-button:hover, .toolbar .tool-button:focus-visible {{ background: #dfedfa; border-color: #80a8cd; outline: none; box-shadow: none; }}
.toolbar .tool-button:disabled {{ opacity: .45; background: transparent; border-color: transparent; }}
.toolbar-icon {{ width: 18px; height: 18px; flex-shrink: 0; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; color: #3875a0; }}
.toolbar .tool-button.action-sync .toolbar-icon {{ color: #aa701d; }}
.export-section .toolbar-icon {{ color: #328366; }}
.view-section .toolbar-icon {{ color: #697680; }}
.toolbar .tool-button.selected-sync:not(:disabled) {{ border-color: #9cb9d0; background: #e5eef5; color: #245d87; }}
.toolbar-count {{ min-width: 17px; line-height: 17px; background: #e3e9ed; color: #526373; border-radius: 2px; font-size: 11px; text-align: center; }}
.toolbar-chevron {{ width: 12px; height: 12px; color: #6f7b85; }}
.export-popup {{ min-width: 230px; }}
.export-popup label {{ display: block; padding: 5px 7px; font-size: 12px; }}
.export-popup select {{ box-sizing: border-box; width: 100%; margin: 4px 0 0; padding: 4px; font-size: 12px; }}
.toolbar-tooltip {{ position: fixed; z-index: 100; box-sizing: border-box; max-width: min(310px, calc(100vw - 16px)); padding: 7px 10px; border: 1px solid #a59d75; border-radius: 2px; background: #fffde9; box-shadow: 1px 2px 4px #0002; font-size: 12px; line-height: 1.7; pointer-events: none; }}
.toolbar-tooltip strong, .toolbar-tooltip span {{ display: block; }}
.toolbar-tooltip span {{ color: #687078; font-size: 11px; }}
.workbench-statusbar {{ position: fixed; bottom: 0; left: 0; right: 0; z-index: 7; display: flex; flex-direction: column; gap: 4px; box-sizing: border-box; min-height: 28px; padding: 3px 10px; background: #eee; border-top: 1px solid #c9cdd1; font-size: 11px; }}
.status-line {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
.status-line > #state {{ flex: 1 1 auto; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.status-line > #state.error {{ white-space: normal; max-height: 100px; overflow: auto; overflow-wrap: anywhere; }}
#workbenchSummary {{ display: flex; flex: 0 0 auto; flex-wrap: wrap; gap: 12px; color: #687681; }}
#summaryPending {{ color: #a54239; }}
.workbench-statusbar .sync-progress, .workbench-statusbar .stock-progress {{ flex: 0 0 auto; min-width: 0; font-size: 11px; }}
.workbench-statusbar .sync-progress-text, .workbench-statusbar .stock-progress-text {{ min-width: 0; overflow-wrap: anywhere; }}
.workbench-statusbar .sync-progress-track, .workbench-statusbar .stock-progress-track {{ min-width: 60px; max-width: 260px; }}
body.toolbar-compact .toolbar {{ box-shadow: none; }}
textarea {{ width: 100%; min-height: 42px; resize: vertical; font-family: Consolas, monospace; }}
button, select, input {{ font: inherit; }}
button {{ border: 1px solid #9aa9b8; background: #fff; color: #1f2933; padding: 5px 9px; border-radius: 4px; cursor: pointer; transition: background .12s ease, border-color .12s ease, color .12s ease, box-shadow .12s ease; }}
button:hover {{ border-color: #5f7f9f; box-shadow: 0 1px 4px rgba(31, 78, 120, .18); }}
button:disabled {{ cursor: not-allowed; opacity: .48; box-shadow: none; }}
button.primary {{ background: #1f4e78; color: white; border-color: #1f4e78; }}
button.small {{ padding: 3px 7px; margin: 0 4px 4px 0; }}
button.stock-query {{ background: linear-gradient(#e9f7ff, #9bd7ff); border-color: #55a8df; color: #074d78; box-shadow: 0 2px 0 #3b8fc1; font-weight: 700; }}
button.stock-query:active {{ transform: translateY(1px); box-shadow: 0 1px 0 #3b8fc1; }}
button.action-save {{ background: #f0fff7; border-color: #53b780; color: #0b6b3a; font-weight: 700; }}
button.action-save:hover {{ background: #dff8ea; border-color: #259b5f; }}
button.action-link {{ border-color: #8ec4e8; color: #075985; background: #f4fbff; font-weight: 700; }}
button.action-sync {{ border-color: #f3b044; color: #7a4a00; background: #fff4d8; font-weight: 700; }}
button.action-sync:hover {{ background: #ffe7a8; border-color: #d99021; }}
button.action-sync-needed {{ border-color: #ef4444; color: #b42318; background: #fff1f2; font-weight: 800; box-shadow: 0 0 0 1px rgba(239, 68, 68, .14); }}
button.action-sync-needed:hover {{ background: #ffe4e6; border-color: #dc2626; }}
button.action-muted {{ color: #4b5563; background: #fafbfc; }}
main {{ padding: 0 0 24px; }}
table {{ border-collapse: collapse; width: max-content; min-width: 100%; table-layout: fixed; background: white; }}
th {{ box-sizing: border-box; background: #e8edf1; color: #344555; padding: 5px 7px; border: 1px solid #c6cdd3; font-size: 12px; position: sticky; top: calc(var(--sticky-toolbar-height) + var(--sticky-filter-height) - 1px); z-index: 1; overflow-wrap: anywhere; }}
th .resize-grip {{ position: absolute; right: 0; top: 0; width: 6px; height: 100%; cursor: col-resize; user-select: none; }}
td {{ padding: 6px; border: 1px solid #d6dde5; vertical-align: middle; font-size: 12px; word-break: break-word; overflow-wrap: anywhere; white-space: normal; }}
tr:nth-child(even) {{ background: #f8fbfd; }}
tr.row-original td:first-child {{ box-shadow: inset 4px 0 0 #21a366; }}
tr.row-changed {{ background: #fff6da; }}
tr.row-changed td:first-child {{ box-shadow: inset 4px 0 0 #f59e0b; }}
tr.row-needs-sync {{ background: #fff5f5; }}
tr.row-needs-sync td:first-child {{ box-shadow: inset 4px 0 0 #ef4444; }}
tr.row-warning {{ background: #fff9e6; }}
tr.row-warning td:first-child {{ box-shadow: inset 4px 0 0 #f59e0b; }}
tr.row-ok td:first-child {{ box-shadow: inset 4px 0 0 #21a366; }}
tr.row-muted {{ background: #eef2f6; color: #52606d; }}
tr.row-muted td:first-child {{ box-shadow: inset 4px 0 0 #94a3b8; }}
tr.hidden-row {{ display: none; }}
.mono {{ font-family: Consolas, monospace; }}
.ref-link {{ border: 0; padding: 0; color: #0563c1; background: transparent; text-decoration: underline; font-family: Consolas, monospace; }}
.page-refs div {{ margin-bottom: 3px; }}
.links a {{ display: inline-block; margin: 0 8px 4px 0; color: #0563c1; text-decoration: none; }}
.links button {{ display: inline-block; margin: 0 8px 4px 0; }}
.edit {{ width: 100%; box-sizing: border-box; min-height: 28px; }}
.field-edit {{ min-height: 34px; resize: vertical; }}
.footprint-library-summary {{ display: block; margin-top: 3px; font-size: 11px; font-weight: 400; color: #52606d; overflow-wrap: anywhere; }}
.footprint-library {{ display: block; margin-bottom: 3px; font-size: 11px; color: #52606d; overflow-wrap: anywhere; }}
.footprint-library[hidden] {{ display: none; }}
.field-changed {{ color: #9a5b00; font-size: 11px; margin-top: 3px; }}
.field-diff {{ color: #9a5b00; font-size: 11px; line-height: 1.35; margin-top: 3px; }}
.sync-summary {{ color: #0f6b3f; font-size: 11px; line-height: 1.35; margin: 4px 0; overflow-wrap: anywhere; }}
.sync-summary summary {{ width: fit-content; color: #52606d; cursor: pointer; }}
.sync-summary-detail {{ max-height: 110px; margin-top: 3px; padding: 4px 5px; overflow: auto; border-left: 2px solid #9ccfb3; background: #f3faf6; white-space: pre-wrap; }}
.schematic-sync-marker {{ display: block; margin: 3px 0 5px; padding: 4px 6px; border-radius: 3px; border: 1px solid #f3a7ad; background: #fff1f2; color: #9f1239; font-size: 11px; font-weight: 500; line-height: 1.45; white-space: normal; overflow-wrap: anywhere; }}
.sync-row-diff {{ display: grid; gap: 2px; max-height: 150px; margin-top: 3px; overflow: auto; font-weight: 400; }}
.review-status + .small {{ margin-top: 4px; }}
.schematic-field-notice {{ margin: 3px 0; padding: 4px 6px; border-left: 3px solid #3b82f6; background: #eff6ff; color: #1e4f86; font-size: 11px; line-height: 1.45; }}
.schematic-field-notice.conflict {{ border-left-color: #dc2626; background: #fff1f2; color: #991b1b; }}
.schematic-field-notice button {{ margin-top: 3px; }}
.note {{ min-height: 52px; }}
.status {{ color: #52606d; }}
.sync-progress {{ flex: 1 1 100%; display: none; align-items: center; gap: 8px; min-width: 280px; color: #35506b; font-size: 12px; }}
.sync-progress.open {{ display: flex; }}
.sync-progress-track {{ position: relative; flex: 1 1 auto; height: 8px; min-width: 140px; overflow: hidden; border-radius: 999px; background: #d9e2ec; }}
.sync-progress-fill {{ position: absolute; inset: 0 auto 0 0; width: 0%; border-radius: inherit; background: linear-gradient(90deg, #1f4e78, #38a3d1); transition: width .18s ease; }}
.sync-progress-text {{ flex: 0 1 auto; min-width: 220px; overflow-wrap: anywhere; }}
.stock-progress {{ flex: 1 1 100%; display: none; align-items: center; gap: 8px; min-width: 280px; color: #17606a; font-size: 12px; }}
.stock-progress.open {{ display: flex; }}
.stock-progress-track {{ position: relative; flex: 1 1 auto; height: 8px; min-width: 140px; overflow: hidden; border-radius: 999px; background: #d9e2ec; }}
.stock-progress-fill {{ position: absolute; inset: 0 auto 0 0; width: 0%; border-radius: inherit; background: linear-gradient(90deg, #087e8b, #22a06b); transition: width .18s ease; }}
.stock-progress-text {{ flex: 0 1 auto; min-width: 260px; overflow-wrap: anywhere; }}
.status-pill, .stock-pill {{ display: inline-flex; align-items: center; justify-content: center; gap: 4px; min-height: 18px; padding: 1px 7px; border-radius: 999px; border: 1px solid transparent; font-size: 11px; font-weight: 700; line-height: 1.4; white-space: nowrap; }}
.status-original {{ background: #e8f7ef; color: #0b6b3a; border-color: #a7d9bd; }}
.status-changed {{ background: #fff3cd; color: #8a5a00; border-color: #f3cf74; }}
.status-rejected {{ background: #ffe4e6; color: #b42318; border-color: #f3a7ad; }}
.review-status {{ display: grid; gap: 4px; min-width: 74px; }}
.review-status select {{ min-height: 28px; }}
.review-pill {{ display: inline-flex; align-items: center; justify-content: center; min-height: 18px; padding: 1px 7px; border-radius: 999px; border: 1px solid transparent; font-size: 11px; font-weight: 700; line-height: 1.4; white-space: nowrap; }}
.review-confirmed-at {{ color: #64748b; font-size: 10px; line-height: 1.35; overflow-wrap: anywhere; }}
.review-ok {{ background: #e8f7ef; color: #0b6b3a; border-color: #a7d9bd; }}
.review-warn {{ background: #fff3cd; color: #8a5a00; border-color: #f3cf74; }}
.review-bad {{ background: #ffe4e6; color: #b42318; border-color: #f3a7ad; }}
.stock-ok {{ background: #e8f7ef; color: #0b6b3a; border-color: #a7d9bd; }}
.stock-wait {{ background: #edf2f7; color: #4a5568; border-color: #cbd5e1; }}
.stock-low {{ background: #fff3cd; color: #8a5a00; border-color: #f3cf74; }}
.stock-bad {{ background: #ffe4e6; color: #b42318; border-color: #f3a7ad; }}
.stock-card {{ display: grid; gap: 3px; }}
.supply-primary {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin-bottom: 5px; }}
.supply-primary input.edit {{ flex: 1 1 95px; min-width: 80px; width: 95px; margin: 0; }}
.supply-icon {{ display: inline-flex; align-items: center; justify-content: center; flex: 0 0 24px; width: 24px; height: 26px; padding: 2px; box-sizing: border-box; }}
.supply-icon[hidden] {{ display: none; }}
.supply-order {{ margin: 4px 0; overflow-wrap: anywhere; }}
.stock-details {{ color: #52606d; font-size: 11px; }}
.stock-details summary {{ cursor: pointer; width: fit-content; }}
.alternate-cell {{ display: grid; gap: 5px; align-content: center; }}
.alternate-list {{ display: grid; gap: 4px; }}
.alternate-item {{ display: grid; grid-template-columns: 18px minmax(72px, auto) minmax(110px, 1fr) auto; gap: 4px 6px; align-items: center; padding: 4px 5px; border: 1px solid #d8e1ea; border-radius: 4px; background: #f8fbfd; }}
.alternate-rank {{ color: #52606d; font-weight: 700; text-align: center; }}
.alternate-code {{ color: #075985; font-weight: 700; }}
.alternate-summary {{ min-width: 0; line-height: 1.35; }}
.alternate-summary strong {{ display: block; }}
.alternate-summary small {{ display: block; color: #52606d; }}
.alternate-actions {{ display: inline-flex; gap: 2px; align-items: center; }}
.alternate-actions button {{ width: 24px; height: 24px; padding: 0; }}
.alternate-empty {{ color: #7b8794; }}
.alternate-conflict {{ padding: 5px; border: 1px solid #efb4b8; border-radius: 4px; background: #fff1f2; color: #9f1d16; line-height: 1.4; }}
.alternate-conflict-actions {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }}
.procurement-choice {{ display: grid; gap: 4px; padding: 5px 6px; border: 1px solid #9fc7df; border-radius: 4px; background: #f2f9fd; }}
.procurement-choice-head {{ display: flex; align-items: center; justify-content: space-between; gap: 6px; }}
.procurement-choice-title {{ color: #075985; font-weight: 700; }}
.procurement-choice-summary {{ color: #334e68; line-height: 1.4; }}
.procurement-choice-meta {{ color: #52606d; font-size: 11px; line-height: 1.4; }}
.procurement-choice-actions {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }}
.param-text {{ white-space: pre-line; line-height: 1.45; }}
.saved {{ color: #008000; font-weight: 600; }}
.error {{ color: #c00000; font-weight: 600; }}
.bad {{ color: #c00000; font-weight: 700; }}
.low-stock {{ color: #c00000; font-weight: 700; }}
.stock-time {{ color: #52606d; font-size: 11px; }}
.changed {{ background: #fff7d6; }}
.view-wrap {{ position: relative; display: inline-flex; align-items: center; gap: 5px; }}
.view-panel {{ display: none; position: absolute; right: 0; top: 36px; width: 250px; max-height: 70vh; overflow: auto; background: #fff; border: 1px solid #c8d1da; border-radius: 6px; box-shadow: 0 10px 28px rgba(15, 35, 55, .18); padding: 12px; z-index: 10; }}
.view-panel.open {{ display: block; }}
.view-panel h2 {{ font-size: 14px; margin: 0 0 8px; }}
.view-row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 5px 2px; }}
.view-row input {{ width: auto; }}
.hidden-col {{ display: none; }}
.modal-backdrop {{ display: none; position: fixed; inset: 0; background: rgba(15, 23, 42, .45); z-index: 20; align-items: center; justify-content: center; }}
.modal-backdrop.open {{ display: flex; }}
.modal {{ position: relative; width: min(1180px, calc(100vw - 56px)); height: min(720px, calc(100vh - 56px)); min-width: min(760px, calc(100vw - 24px)); min-height: min(480px, calc(100vh - 24px)); max-width: calc(100vw - 24px); max-height: calc(100vh - 24px); background: white; border-radius: 8px; box-shadow: 0 18px 48px rgba(15, 23, 42, .28); display: grid; grid-template-rows: auto auto 1fr; overflow: hidden; }}
.modal.fullscreen {{ position: fixed !important; inset: 12px !important; width: auto !important; height: auto !important; min-width: 0; min-height: 0; max-width: none; max-height: none; border-radius: 4px; }}
#candidateWindow {{ flex-direction: column; }}
#candidateModal:not(.minimized) #candidateWindow {{ display: flex; }}
#candidateWindow > .modal-head, #candidateWindow > .modal-query, #candidateWindow > .candidate-status-bar {{ flex: 0 0 auto; }}
#candidateWindow > .candidate-body-wrap {{ flex: 1 1 auto; min-height: 0; }}
.candidate-status-bar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; padding: 5px 12px; min-height: 28px; box-sizing: border-box; border-bottom: 1px solid #d9e2ec; }}
.candidate-search-progress {{ flex: 1 1 300px; font-size: 12px; line-height: 18px; color: #59636b; min-width: 0; overflow-wrap: anywhere; }}
.modal-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 18px; background: #eaf5ff; border-bottom: 1px solid #d4e5f5; font-weight: 700; min-width: 0; cursor: move; user-select: none; touch-action: none; }}
.modal-head:active {{ cursor: grabbing; }}
.modal-controls {{ display: inline-flex; align-items: center; gap: 2px; flex: 0 0 auto; }}
.modal-controls, .modal-control {{ cursor: default; }}
.modal-control {{ display: inline-grid; place-items: center; width: 30px; height: 30px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: #334e68; font: 21px/1 Arial, sans-serif; }}
.modal-control:hover {{ background: #d8e9f7; color: #102a43; }}
.modal-control.close {{ font-size: 24px; }}
.candidate-minimized-bar {{ display: none; position: fixed; right: 12px; bottom: 12px; z-index: 22; width: min(360px, calc(100vw - 24px)); min-height: 40px; align-items: center; justify-content: space-between; gap: 10px; padding: 7px 10px; border: 1px solid #8db7d4; border-radius: 6px; background: #eaf5ff; color: #193d57; box-shadow: 0 8px 24px rgba(15, 23, 42, .24); font-weight: 700; pointer-events: auto; }}
.candidate-minimized-title {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.candidate-minimized-icon {{ font-size: 18px; }}
.modal-backdrop.minimized {{ background: transparent; pointer-events: none; }}
.modal-backdrop.minimized .modal {{ display: none; }}
.modal-backdrop.minimized .candidate-minimized-bar {{ display: flex; }}
.modal-resize-handle {{ position: absolute; z-index: 5; user-select: none; touch-action: none; }}
.modal-resize-handle[data-resize="n"], .modal-resize-handle[data-resize="s"] {{ left: 18px; right: 18px; height: 9px; cursor: ns-resize; }}
.modal-resize-handle[data-resize="n"] {{ top: 0; }}
.modal-resize-handle[data-resize="s"] {{ bottom: 0; }}
.modal-resize-handle[data-resize="e"], .modal-resize-handle[data-resize="w"] {{ top: 18px; bottom: 18px; width: 9px; cursor: ew-resize; }}
.modal-resize-handle[data-resize="e"] {{ right: 0; }}
.modal-resize-handle[data-resize="w"] {{ left: 0; }}
.modal-resize-handle[data-resize="ne"], .modal-resize-handle[data-resize="nw"], .modal-resize-handle[data-resize="se"], .modal-resize-handle[data-resize="sw"] {{ width: 18px; height: 18px; }}
.modal-resize-handle[data-resize="ne"] {{ top: 0; right: 0; cursor: nesw-resize; }}
.modal-resize-handle[data-resize="nw"] {{ top: 0; left: 0; cursor: nwse-resize; }}
.modal-resize-handle[data-resize="se"] {{ right: 0; bottom: 0; cursor: nwse-resize; }}
.modal-resize-handle[data-resize="sw"] {{ left: 0; bottom: 0; cursor: nesw-resize; }}
.modal.fullscreen .modal-resize-handle {{ display: none; }}
.candidate-title {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.candidate-title {{ -webkit-user-select: text; user-select: text; cursor: text; }}
.modal-query {{ display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 18px; border-bottom: 1px solid #d9e2ec; align-items: center; }}
.modal-query input {{ width: 100%; min-height: 30px; box-sizing: border-box; }}
.modal-query > input {{ flex: 1 1 240px; min-width: 120px; }}
.modal-query > button, .modal-query label {{ flex: 0 0 auto; white-space: nowrap; }}
.modal-query label input {{ width: auto; min-height: 0; }}
.candidate-options {{ display: flex; flex: 0 1 auto; flex-wrap: wrap; align-items: center; gap: 8px 12px; min-width: 0; }}
.candidate-options label {{ display: flex; align-items: center; gap: 6px; white-space: nowrap; }}
.candidate-options select {{ min-width: 88px; }}
.filter-toggle {{ display: flex; align-items: center; justify-content: center; gap: 4px; min-height: 30px; }}
.candidate-table {{ width: max-content; min-width: 100%; table-layout: auto; font-size: 12px; }}
.candidate-table td {{ padding: 7px 8px; vertical-align: middle; }}
.candidate-table th {{ position: sticky; top: 0; }}
.candidate-table th[data-candidate-col], .candidate-table td[data-candidate-col] {{ min-width: 28px; max-width: none; white-space: normal; overflow-wrap: anywhere; word-break: break-word; }}
.candidate-table th[data-candidate-col] {{ cursor: grab; }}
.candidate-table th[data-candidate-col].dragging {{ opacity: .45; }}
.candidate-filter-trigger {{ display: inline-flex; align-items: center; justify-content: center; margin-left: 6px; padding: 1px 5px; min-width: 25px; color: inherit; border-color: #91abc2; background: transparent; font-size: 12px; }}
.candidate-filter-trigger[aria-pressed="true"] {{ color: #173b59; background: #d7f0ff; border-color: #fff; }}
.candidate-stock-filter {{ display: inline-flex; align-items: center; gap: 3px; margin-left: 7px; cursor: pointer; font-weight: normal; white-space: nowrap; }}
.candidate-filter-bar {{ display: flex; flex: 0 1 auto; flex-wrap: wrap; align-items: center; gap: 5px 8px; font-size: 12px; min-width: 0; max-width: 100%; }}
.candidate-filter-bar[hidden], .candidate-filter-panel[hidden] {{ display: none; }}
.candidate-filter-tags {{ display: flex; flex-wrap: wrap; gap: 4px; flex: 1 1 160px; min-width: 0; }}
.candidate-filter-tag {{ display: inline-flex; align-items: center; gap: 4px; color: #075985; border: 1px solid #b3d7ec; background: #f0f9ff; border-radius: 3px; max-width: 100%; padding-left: 5px; }}
.candidate-filter-tag span {{ overflow-wrap: anywhere; }}
.candidate-filter-tag button {{ padding: 0 4px; border: 0; background: transparent; color: inherit; font-size: 15px; }}
.candidate-filter-count {{ color: #52606d; margin-left: auto; white-space: nowrap; }}
.candidate-filter-clear {{ padding: 2px 6px; font-size: 12px; }}
.candidate-filter-panel {{ flex: 0 0 auto; min-width: 0; padding: 6px 12px 8px; background: #f8fafc; border-bottom: 1px solid #c8d6e3; font-size: 12px; }}
.candidate-filter-tools {{ display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-bottom: 5px; }}
.candidate-filter-tools input {{ width: 200px; max-width: 100%; min-width: 80px; box-sizing: border-box; padding: 3px 5px; }}
.candidate-filter-tools button {{ margin-left: auto; border: 0; background: transparent; padding: 0 5px; font-size: 19px; }}
.candidate-filter-groups {{ display: flex; flex-wrap: wrap; gap: 5px 16px; max-height: min(210px, 28vh); overflow: auto; }}
.candidate-filter-group {{ flex: 1 1 190px; min-width: 0; max-width: 100%; margin: 0; padding: 5px 0; border: 0; border-top: 1px solid #dbe4ee; }}
.candidate-filter-group legend {{ font-weight: 700; color: #334e68; }}
.candidate-filter-values {{ display: flex; flex-wrap: wrap; gap: 3px 10px; max-height: 130px; overflow: auto; align-content: flex-start; }}
.candidate-filter-option {{ display: inline-flex; align-items: center; gap: 3px; max-width: 100%; min-height: 22px; cursor: pointer; }}
.candidate-filter-option span {{ overflow-wrap: anywhere; }}
.candidate-filter-option small {{ color: #697d8e; }}
.candidate-filter-option:has(input:disabled) {{ opacity: .55; }}
.candidate-filter-empty {{ color: #697d8e; padding: 5px 0; }}
.candidate-table th[data-candidate-col="action"], .candidate-table td[data-candidate-col="action"] {{ min-width: 132px; }}
.candidate-table th[data-candidate-col="image"], .candidate-table td[data-candidate-col="image"] {{ min-width: 48px; }}
.candidate-table th[data-candidate-col="channel_stock"], .candidate-table td[data-candidate-col="channel_stock"] {{ min-width: 205px; }}
.candidate-body-wrap {{ overflow: auto; min-width: 0; }}
.candidate-section-row td {{ padding: 0; border-top: 2px solid #8dc6e8; border-bottom: 1px solid #b9dced; background: #e5f5fc; }}
.candidate-section-head {{ display: flex; align-items: center; gap: 14px; min-height: 38px; padding: 0 14px; color: #193d57; }}
.candidate-section-head strong {{ font-size: 14px; }}
.candidate-section-head span {{ color: #526d7d; font-size: 12px; }}
.candidate-partner-row td {{ background: #f7fbfd; border-bottom-color: #dce9f0; }}
.candidate-partner-row:hover td {{ background: #eef8fc; }}
.candidate-partner-supplier {{ color: #075985; font-weight: 700; }}
.candidate-partner-meta {{ color: #52606d; font-size: 11px; line-height: 1.45; }}
.candidate-stock-lines {{ display: grid; gap: 2px; margin-top: 3px; line-height: 1.35; }}
.candidate-stock-lines strong {{ color: #1f2933; }}
.candidate-partner-price {{ line-height: 1.45; }}
.candidate-partner-actions {{ display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }}
.candidate-param-lines, .candidate-order-lines, .candidate-price-lines {{ display: grid; gap: 3px; line-height: 1.35; }}
.candidate-param-line {{ display: grid; grid-template-columns: minmax(54px, max-content) 1fr; gap: 5px; align-items: baseline; }}
.candidate-param-key {{ color: #52606d; white-space: nowrap; }}
.candidate-param-value {{ min-width: 0; color: #1f2933; }}
.candidate-price-line {{ white-space: nowrap; }}
.candidate-order-link {{ color: #075985; font-weight: 700; text-decoration: none; }}
.thumb {{ width: 54px; height: 54px; object-fit: contain; background: #f6f8fb; border: 1px solid #d6dde5; border-radius: 4px; transition: transform .12s ease, box-shadow .12s ease; transform-origin: top left; }}
.thumb:hover {{ transform: scale(3.4); box-shadow: 0 12px 32px rgba(15, 23, 42, .28); z-index: 30; position: relative; background: white; }}
.drag-handle {{ color: #7b8794; cursor: grab; user-select: none; }}
.view-row[draggable="true"] {{ cursor: grab; }}
.view-row.dragging {{ opacity: .45; }}
.filter-bar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-height: 31px; box-sizing: border-box; margin: 0; padding: 3px 10px; border: 0; border-bottom: 1px solid #b6c0c9; border-radius: 0; background: #fafafa; position: relative; left: 0; z-index: 4; }}
.filter-title {{ color: #243b53; font-weight: 700; }}
.bom-search {{ display: flex; align-items: center; gap: 4px; margin-left: auto; min-width: 0; max-width: 100%; }}
.bom-search input {{ width: 230px; max-width: 100%; min-width: 100px; box-sizing: border-box; padding: 4px 6px; }}
.bom-search button {{ padding: 2px 6px; }}
.bom-search output {{ white-space: nowrap; color: #52606d; font-size: 12px; }}
.filter-hint {{ color: #7b8794; font-size: 12px; }}
.filter-tabs {{ display: flex; flex-wrap: wrap; gap: 4px; }}
.filter-tabs button {{ min-height: 23px; border-color: #bfc8d0; background: #fff; border-radius: 3px; padding: 1px 8px; font-size: 12px; }}
.filter-tabs button.active {{ background: #e1edf7; border-color: #699ac0; color: #205b87; font-weight: 400; box-shadow: none; }}
.filter-tabs button.filter-negative {{ background: #e7f2eb; border-color: #72a385; color: #246341; }}
.filter-hint, .filter-title {{ display: none; }}
@media (max-width: 1100px) {{ .toolbar-section {{ padding: 0 8px; }} .source-section select {{ width: 160px; }} }}
@media (max-width: 680px) {{
  .source-section {{ flex-basis: 100%; border-right: 0; }}
  .source-section select {{ width: 180px; }}
  .project-path-link {{ display: none; }}
  .bom-search {{ flex-basis: 100%; margin-left: 0; }}
  .bom-search input {{ width: auto; flex: 1; }}
  .status-line {{ flex-wrap: wrap; gap: 3px; }}
  #workbenchSummary {{ gap: 8px; }}
  .view-panel {{ width: min(300px, calc(100vw - 20px)); box-sizing: border-box; }}
}}
.split-marker {{ color: #7b4d00; font-size: 11px; margin-top: 3px; }}
.ref-multi-picker {{ display: grid; gap: 4px; padding: 4px; border: 1px solid #9cc5e2; border-radius: 4px; background: #f4fbff; font-family: inherit; }}
.ref-multi-option {{ display: flex; align-items: center; gap: 4px; white-space: nowrap; }}
.ref-multi-option input {{ width: auto; margin: 0; }}
.ref-multi-actions {{ display: flex; flex-wrap: wrap; gap: 4px; }}
.ref-multi-actions button {{ margin: 0; padding: 2px 6px; font-size: 11px; }}
.ref-context-menu {{ display: none; position: fixed; z-index: 50; min-width: 170px; background: white; border: 1px solid #b8c7d6; border-radius: 5px; box-shadow: 0 10px 26px rgba(15, 35, 55, .22); padding: 5px; }}
.ref-context-menu.open {{ display: block; }}
.ref-context-menu button {{ display: block; width: 100%; text-align: left; border: 0; background: white; padding: 7px 9px; }}
.ref-context-menu button:hover {{ background: #eaf5ff; }}
</style>
<style>{footprint_css}</style>
<style>{row_filters_css}</style>
<style>{path_settings_css}</style>
<style>{inventory_css}</style>
<style>{spec_warnings_css}</style>
<style>{value_parameters_css}</style>
</head>
<body>
{icon_sprite}
<header class="workbench-chrome">
<nav class="app-menubar" aria-label="工作台菜单">
  <div class="app-menu">
    <button type="button" data-menu-trigger aria-expanded="false">文件</button>
    <div class="menu-popup">
      <button type="button" data-command="exportPumpMenu">导出 BOM 表…</button>
      <button type="button" data-command="exportChanges">导出改动表</button>
      <button type="button" data-command="saveDefaultView">保存当前列表配置为默认</button>
      <button type="button" data-command="loadDefaultView">加载默认列表配置</button>
      <button type="button" id="restartWorkbench">重启工作台</button>
      <button type="button" id="exitWorkbench">退出工作台</button>
    </div>
  </div>
  <div class="app-menu">
    <button type="button" data-menu-trigger aria-expanded="false">数据</button>
    <div class="menu-popup">
      <button type="button" data-command="refreshSchematicRows">刷新原理图当前数据</button>
      <button type="button" data-command="importLocalInventory">导入本地库存…</button>
      <button type="button" data-command="queryMissingStock">查询未查库存</button>
      <button type="button" data-command="queryAllStock">刷新全部库存</button>
      <button type="button" data-command="syncSelectedSchematic">同步所选行</button>
      <button type="button" data-command="syncAllSchematic">同步全部原理图</button>
    </div>
  </div>
  <div class="app-menu">
    <button type="button" data-menu-trigger aria-expanded="false">视图</button>
    <div class="menu-popup">
      <button type="button" data-command="toggleColumns">列显示配置</button>
      <button type="button" data-command="loadDefaultView">加载默认列表配置</button>
    </div>
  </div>
  <div class="app-menu">
    <button type="button" data-menu-trigger aria-expanded="false">设置</button>
    <div class="menu-popup"><button type="button" id="openPathConfig">工程设置…</button><button type="button" id="openPluginUpdates">插件更新…</button></div>
  </div>
  <span class="menubar-spacer" aria-hidden="true"></span>
  <button type="button" class="project-path-link" data-action="open-project-path" title="点击打开工程目录">{html.escape(str(app.project_root))}</button>
</nav>
<div class="toolbar">
<section class="toolbar-section source-section" aria-label="原理图数据">
  <span class="section-title">原理图数据</span><div class="section-actions">
  <select id="bomSelect" aria-label="数据源" title="选择原理图当前数据或 BOM 文件"></select>
  <button type="button" id="refreshSchematicRows" class="tool-button" aria-label="读取原理图当前数据" data-tooltip="原理图 → 工作台：重新读取磁盘上已保存的器件字段，不写入原理图。">{toolbar_content('file-down', '重新读取')}</button>
  </div>
</section>
<section class="toolbar-section stock-section" aria-label="库存查询">
  <span class="section-title">库存查询</span><div class="section-actions">
  <button type="button" id="queryMissingStock" class="tool-button" disabled aria-label="查询未查库存" data-tooltip="仅查询没有库存缓存的主料及备选料，已有查询结果保留。">{toolbar_content('package-search', '查询未查')}</button>
  <button type="button" id="queryAllStock" class="tool-button" disabled aria-label="刷新全部库存" data-tooltip="重新联网查询全部主料及备选料库存，更新旧缓存。">{toolbar_content('refresh-cw', '刷新全部')}</button>
  <button type="button" id="importLocalInventory" class="tool-button" aria-label="导入本地库存" data-tooltip="从 Excel 预览并导入现存物料；同型号已购合计为库存量。">{toolbar_content('folder-open', '本地库存')}</button>
  <label class="inventory-set-count">套数 <input type="number" id="localInventorySetCount" min="1" max="1000000" step="1" value="3" aria-label="装配套数"></label>
  </div>
</section>
<section class="toolbar-section action-section" aria-label="写入原理图">
  <span class="section-title">写入原理图</span><div class="section-actions">
  <button type="button" id="syncSelectedSchematic" class="tool-button action-sync selected-sync" disabled aria-label="同步所选行（0）" data-tooltip="工作台 → 原理图：仅同步当前可见且已勾选的行，执行前确认。">{toolbar_content('list-checks', '同步所选', True)}</button>
  <button type="button" id="syncAllSchematic" class="tool-button action-sync" disabled aria-label="同步全部原理图" data-tooltip="工作台 → 原理图：处理整个 BOM，不受筛选或勾选限制，执行前确认。">{toolbar_content('layers', '同步全部')}</button>
  </div>
</section>
<section class="toolbar-section export-section" aria-label="表格导出">
  <span class="section-title">表格导出</span><div class="section-actions">
  <div class="app-menu">
    <button type="button" id="exportPumpMenu" class="tool-button" data-menu-trigger aria-expanded="false" aria-label="导出 BOM 表" data-tooltip="选择导出范围：当前筛选、低库存等；导出仅包含非 NC 器件。">{toolbar_content('table-2', 'BOM 导出')}<svg class="toolbar-icon toolbar-chevron" aria-hidden="true"><use href="#toolbar-icon-chevron-down"></use></svg></button>
    <div class="menu-popup export-popup">
    <label>导出范围<select id="pumpExportMode" title="选择导出泵表内容">
  <option value="current_filter">当前筛选结果</option>
  <option value="stock_issue">库存异常/低库存</option>
  <option value="low_stock">低库存器件</option>
  <option value="stock_error">库存异常器件</option>
  <option value="pending_review">未确认料</option>
  <option value="needs_sync">待同步原理图</option>
  <option value="all_non_nc">全部非NC器件</option>
</select></label>
    <button type="button" id="exportPumpTable">导出 BOM 表</button>
    </div>
  </div>
  <button type="button" id="exportChanges" class="tool-button" aria-label="导出改动表" data-tooltip="导出网页端的改动记录，不写入原理图。">{toolbar_content('file-diff', '改动表')}</button>
  </div>
</section>
<section class="toolbar-section view-section" aria-label="列表视图">
  <span class="section-title">列表视图</span><div class="section-actions">
  <div class="view-wrap">
    <button id="toggleColumns" type="button" class="tool-button" aria-expanded="false" aria-label="列显示配置" data-tooltip="选择显示列、调整列顺序及列宽。修改后自动记忆，下次打开继续使用。">{toolbar_content('columns-3', '列设置')}</button>
    <div id="columnPanel" class="view-panel"><h2>自定义视图配置</h2><button id="autoSizeColumns" type="button">自动调整列宽</button><div id="columnList"></div></div>
  </div>
  <div class="app-menu">
    <button type="button" id="defaultViewMenu" class="tool-button" data-menu-trigger aria-expanded="false" aria-label="默认视图配置" data-tooltip="保存当前列表配置为默认，或加载已保存的默认配置。">{toolbar_content('save', '默认配置')}<svg class="toolbar-icon toolbar-chevron" aria-hidden="true"><use href="#toolbar-icon-chevron-down"></use></svg></button>
    <div class="menu-popup">
      <button type="button" id="saveDefaultView">保存当前列表配置为默认</button>
      <button type="button" id="loadDefaultView">加载默认列表配置</button>
    </div>
  </div>
  </div>
</section>
</div>
</header>
<input type="hidden" id="refsInput" value="">
<div id="toolbarTooltip" class="toolbar-tooltip" role="tooltip" hidden></div>
<footer class="workbench-statusbar">
<div class="status-line">
<span id="state" class="status" role="status">正在读取数据源…</span>
<output id="workbenchSummary" aria-label="器件状态统计"><span id="summaryRows">0 组 / 0 器件</span><span id="summaryPending">待同步 0</span><span id="summaryReview">未确认 0</span></output>
</div>
<div id="syncProgress" class="sync-progress" aria-live="polite">
  <div class="sync-progress-track"><div id="syncProgressFill" class="sync-progress-fill"></div></div>
  <span id="syncProgressText" class="sync-progress-text"></span>
</div>
<div id="stockProgress" class="stock-progress" aria-live="polite">
  <div class="stock-progress-track"><div id="stockProgressFill" class="stock-progress-fill"></div></div>
  <span id="stockProgressText" class="stock-progress-text"></span>
</div>
</footer>
<main>
<nav class="filter-tabs" aria-label="筛选视图">
  <button type="button" class="active" data-filter="all">全部</button>
  <button type="button" data-filter="needs_sync">待同步原理图</button>
  <button type="button" data-filter="pending_review">未确认料</button>
  <button type="button" data-filter="changed_code">C 编码已改动</button>
  <button type="button" data-filter="stock_issue">库存异常/低库存</button>
  <button type="button" data-filter="missing_ref">原理图缺失位号</button>
  <button type="button" data-filter="nc_device">NC器件</button>
</nav>
<div class="bom-search" role="search" aria-label="搜索 BOM">
  <input id="bomSearch" type="search" placeholder="位号 / 型号 / C编码 / 参数" aria-label="搜索 BOM" spellcheck="false">
  <button id="clearBomSearch" type="button" aria-label="清空搜索" title="清空搜索">&times;</button>
  <output id="bomSearchCount" aria-live="polite"></output>
</div>
<table>
<thead>
<tr>
<th data-col="row_no" style="width:64px"><input type="checkbox" id="selectVisibleRows" aria-label="选择当前筛选结果" title="选择当前筛选结果">行号</th><th data-col="review" style="width:240px">确认料 / 同步差异</th><th data-col="refs" style="width:150px">位号</th><th data-col="page_refs" style="width:220px">页面位号</th><th data-col="quantity" style="width:70px">单套用量</th><th data-col="comment" style="width:110px">Comment</th><th data-col="value" style="width:110px">Value</th><th data-col="footprint" style="width:95px">Footprint<span id="footprintLibrarySummary" class="footprint-library-summary"></span></th>
<th data-col="stock" style="width:280px">选用物料 / 供货</th><th data-col="local_inventory" style="width:170px">本地库存</th><th data-col="order" style="width:90px">MOQ/MPQ</th><th data-col="price" style="width:100px">价格</th><th data-col="param" style="width:320px">物料参数</th><th data-col="alternates" style="width:360px">备选料</th><th data-col="note" style="width:220px">备注</th>
</tr>
</thead>
<tbody id="rows"></tbody>
</table>
<div id="rowsEmptyState" class="rows-empty-state" hidden>
  <span id="rowsEmptyMessage" role="status"></span><button id="resetRowFilters" type="button">清除筛选</button>
</div>
</main>
<div id="refContextMenu" class="ref-context-menu">
  <button type="button" data-action="split-ref-row">拆出为单独行编辑</button>
</div>
<div id="candidateModal" class="modal-backdrop">
  <div id="candidateWindow" class="modal">
    <div class="modal-head">
      <span id="candidateTitle" class="candidate-title">更换型号</span>
      <div class="modal-controls" aria-label="窗口控制">
        <button id="minimizeCandidate" class="modal-control" type="button" aria-label="最小化" title="最小化">&minus;</button>
        <button id="maximizeCandidate" class="modal-control" type="button" aria-label="全屏" title="全屏"><span id="candidateMaximizeIcon" aria-hidden="true">&#9633;</span></button>
        <button id="closeCandidate" class="modal-control close" type="button" aria-label="关闭" title="关闭">&times;</button>
      </div>
    </div>
    <div class="modal-query">
      <input id="candidateKeyword" spellcheck="false" placeholder="搜索型号、参数、C 编码，按回车搜索" aria-label="搜索器件">
      <div id="candidateReplacementOptions" class="candidate-options" role="group" aria-label="选用主料更新选项" title="物料参数默认更新，保存选料后仍需同步原理图">
        <label title="选择库封装、仅改名保留几何，或转换立创封装">封装 <select id="candidateFootprintMode" aria-label="封装更新方式"><option value="keep" selected>不替换</option><option value="library">替换库封装</option><option value="rename">仅改名保留几何</option><option value="convert">替换立创封装</option></select></label>
        <label title="芯片使用完整型号；阻容感使用对应数值">Value <select id="candidateReplaceValue" aria-label="Value 更新方式"><option value="false" selected>不替换</option><option value="true">替换</option></select></label>
        <label title="芯片使用完整型号；阻容感使用 resistor / capacitor / inductor，型号保留在物料参数中">Comment <select id="candidateReplaceComment" aria-label="Comment 更新方式"><option value="false" selected>不替换</option><option value="true">替换</option></select></label>
      </div>
      <button id="openOfficialSearch" type="button">网页搜索</button>
    </div>
    <div class="candidate-status-bar">
      <div id="candidateSearchProgress" class="candidate-search-progress" role="status"></div>
      <div id="candidateFilterBar" class="candidate-filter-bar" hidden>
        <div id="candidateFilterTags" class="candidate-filter-tags" aria-label="已选候选筛选条件"></div>
        <button id="clearCandidateFilters" class="candidate-filter-clear" type="button" hidden>清空筛选</button>
        <output id="candidateFilterCount" class="candidate-filter-count" aria-live="polite"></output>
      </div>
    </div>
    <section id="candidateFilterPanel" class="candidate-filter-panel" aria-label="候选结果筛选" hidden>
      <div class="candidate-filter-tools">
        <strong id="candidateFilterTitle">筛选</strong>
        <input id="candidateFilterOptionSearch" type="search" aria-label="查找筛选项" placeholder="查找筛选项" spellcheck="false">
        <button id="closeCandidateFilters" type="button" aria-label="关闭筛选面板" title="关闭筛选面板">&times;</button>
      </div>
      <div id="candidateFilterGroups" class="candidate-filter-groups"></div>
    </section>
    <div class="candidate-body-wrap">
      <table class="candidate-table">
        <thead>
          <tr>
            <th data-candidate-col="action" style="width:142px">操作</th>
            <th data-candidate-col="image" style="width:68px">图片</th>
            <th data-candidate-col="model" style="width:250px">型号/品牌/封装<button class="candidate-filter-trigger" data-candidate-filter="identity" type="button" aria-label="筛选品牌和封装" aria-expanded="false" aria-pressed="false" title="筛选品牌和封装">&#9661;</button></th>
            <th data-candidate-col="param" style="width:280px">参数详情<button class="candidate-filter-trigger" data-candidate-filter="parameters" type="button" aria-label="筛选参数详情" aria-expanded="false" aria-pressed="false" title="筛选参数详情">&#9661;</button></th>
            <th data-candidate-col="code" style="width:92px">C编码</th>
            <th data-candidate-col="channel_stock" style="width:215px">渠道 / 库存交期<label class="candidate-stock-filter"><input id="inStockOnly" type="checkbox">仅现货</label></th>
            <th data-candidate-col="order" style="width:112px">MOQ/MPQ</th>
            <th data-candidate-col="price" style="width:165px">价格梯度</th>
          </tr>
        </thead>
        <tbody id="candidateRows"></tbody>
      </table>
    </div>
    <span class="modal-resize-handle" data-resize="n"></span>
    <span class="modal-resize-handle" data-resize="ne"></span>
    <span class="modal-resize-handle" data-resize="e"></span>
    <span class="modal-resize-handle" data-resize="se"></span>
    <span class="modal-resize-handle" data-resize="s"></span>
    <span class="modal-resize-handle" data-resize="sw"></span>
    <span class="modal-resize-handle" data-resize="w"></span>
    <span class="modal-resize-handle" data-resize="nw"></span>
  </div>
  <button id="candidateMinimizedBar" class="candidate-minimized-bar" type="button" aria-label="恢复更换型号窗口" title="恢复窗口">
    <span id="candidateMinimizedTitle" class="candidate-minimized-title">更换型号</span>
    <span class="candidate-minimized-icon" aria-hidden="true">&#9633;</span>
  </button>
</div>
<script>{row_edits_js}</script>
<script>{row_filters_js}</script>
<script>
const state = document.getElementById("state");
const syncProgress = document.getElementById("syncProgress");
const syncProgressFill = document.getElementById("syncProgressFill");
const syncProgressText = document.getElementById("syncProgressText");
const stockProgress = document.getElementById("stockProgress");
const stockProgressFill = document.getElementById("stockProgressFill");
const stockProgressText = document.getElementById("stockProgressText");
const rowsEl = document.getElementById("rows");
const refsInput = document.getElementById("refsInput");
const bomSelect = document.getElementById("bomSelect");
const queryMissingButton = document.getElementById("queryMissingStock");
const queryAllButton = document.getElementById("queryAllStock");
const syncAllButton = document.getElementById("syncAllSchematic");
const syncSelectedButton = document.getElementById("syncSelectedSchematic");
const selectVisibleRows = document.getElementById("selectVisibleRows");
const selectedRowRefs = new Set();
let bulkSyncRunning = false;
let rowsLoading = false;
let rowsLoadVersion = 0;
let loadedRowsBom = null;
const columnPanel = document.getElementById("columnPanel");
const columnList = document.getElementById("columnList");
const candidateModal = document.getElementById("candidateModal");
const candidateWindow = document.getElementById("candidateWindow");
const candidateRows = document.getElementById("candidateRows");
const candidateKeyword = document.getElementById("candidateKeyword");
const candidateTitle = document.getElementById("candidateTitle");
const candidateMinimizedTitle = document.getElementById("candidateMinimizedTitle");
const maximizeCandidateButton = document.getElementById("maximizeCandidate");
const candidateMaximizeIcon = document.getElementById("candidateMaximizeIcon");
const inStockOnly = document.getElementById("inStockOnly");
const refContextMenu = document.getElementById("refContextMenu");
const filterTabs = document.querySelector(".filter-tabs");
const bomSearch = document.getElementById("bomSearch");
const bomSearchCount = document.getElementById("bomSearchCount");
let currentRows = [];
const FILTER_CYCLES = {{
  needs_sync: [
    {{key: "needs_sync", label: "待同步原理图"}},
    {{key: "sync_done", label: "已同步原理图"}}
  ],
  pending_review: [
    {{key: "pending_review", label: "未确认料"}},
    {{key: "review_confirmed", label: "已确认料"}}
  ],
  changed_code: [
    {{key: "changed_code", label: "C 编码已改动"}},
    {{key: "code_unchanged", label: "C 编码未改动"}}
  ],
  stock_issue: [
    {{key: "stock_issue", label: "库存异常/低库存"}},
    {{key: "stock_normal", label: "库存正常"}}
  ],
  missing_ref: [
    {{key: "missing_ref", label: "原理图缺失位号"}},
    {{key: "ref_complete", label: "原理图位号完整"}}
  ],
  nc_device: [
    {{key: "nc_device", label: "NC器件"}},
    {{key: "non_nc", label: "非NC器件"}}
  ]
}};
const activeFilters = new Map();
const FILTER_SCROLL_STORAGE_PREFIX = "partSelectionFilterScroll_v1:";
let activeFilterViewKey = "";
let candidateTargetRow = null;
let candidateTargetKey = "";
let currentCandidateItems = new Map();
let currentProcurementQuotes = new Map();
let candidateSearchToken = 0;
let candidateSearchController = null;
let candidateRawItems = [];
const candidateParameterRequests = new Map();
const candidateFilters = new Map();
let candidateFilterSection = "";
const candidateFilterPanel = document.getElementById("candidateFilterPanel");
const candidateFilterOptionSearch = document.getElementById("candidateFilterOptionSearch");
const candidateSearchClient = crypto.randomUUID ? crypto.randomUUID() : `search-${{Date.now()}}-${{Math.random()}}`;
const candidateSearchProgress = document.getElementById("candidateSearchProgress");
const candidateFootprintMode = document.getElementById("candidateFootprintMode");
const candidateReplaceValue = document.getElementById("candidateReplaceValue");
const candidateReplaceComment = document.getElementById("candidateReplaceComment");
let contextRefTarget = null;
let multiSplitGroupId = "";
let multiSplitRefs = new Set();
let multiSplitSource = "refs";
let stockQueryController = null;
let stockQueryRunId = 0;

function setToolbarLabel(button, text) {{
  button.querySelector("[data-button-label]").textContent = text;
}}

function workbenchCounts(rows) {{
  const refs = new Set();
  let needsSync = 0, unconfirmed = 0;
  rows.forEach(tr => {{
    String(tr.dataset.refs || tr.dataset.ref || "").split(",").filter(Boolean).forEach(ref => refs.add(ref.trim()));
    if ([tr.dataset.syncNeeded, tr.dataset.missingRef, tr.dataset.syncError].includes("1")) needsSync += 1;
    if (tr.dataset.reviewStatus !== "Confirmed") unconfirmed += 1;
  }});
  return {{groups: rows.length, refs: refs.size, needsSync, unconfirmed}};
}}

function updateWorkbenchSummary() {{
  const counts = workbenchCounts(Array.from(rowsEl.querySelectorAll("tr[data-ref]")));
  document.getElementById("summaryRows").textContent = `${{counts.groups}} 组 / ${{counts.refs}} 器件`;
  document.getElementById("summaryPending").textContent = `待同步 ${{counts.needsSync}}`;
  document.getElementById("summaryReview").textContent = `未确认 ${{counts.unconfirmed}}`;
}}

function organizeToolbar() {{
  const toolbar = document.querySelector(".toolbar");
  if (!toolbar || toolbar.classList.contains("organized")) return;
  toolbar.classList.add("organized");

  const filterTabsEl = document.querySelector(".filter-tabs");
  if (filterTabsEl && !filterTabsEl.closest(".filter-bar")) {{
    const bar = document.createElement("section");
    bar.className = "filter-bar";
    bar.setAttribute("aria-label", "筛选视图");
    const title = document.createElement("span");
    title.className = "filter-title";
    title.textContent = "筛选";
    const hint = document.createElement("span");
    hint.className = "filter-hint";
    hint.textContent = "多个筛选条件取交集";
    document.querySelector(".workbench-chrome").appendChild(bar);
    bar.appendChild(title);
    bar.appendChild(filterTabsEl);
    bar.appendChild(hint);
    bar.appendChild(document.querySelector(".bom-search"));
  }}
}}

function updateStickyChrome() {{
  const chrome = document.querySelector(".workbench-chrome");
  const toolbar = document.querySelector(".toolbar");
  const filterBar = document.querySelector(".filter-bar");
  document.body.classList.toggle("toolbar-compact", window.scrollY > 60);
  const horizontalOffset = `translateX(${{window.scrollX}}px)`;
  if (chrome) {{
    chrome.style.width = `${{document.documentElement.clientWidth}}px`;
    chrome.style.transform = horizontalOffset;
  }}
  const toolbarHeight = (toolbar ? Math.ceil(toolbar.getBoundingClientRect().height) : 0)
    + Math.ceil(document.querySelector(".app-menubar")?.getBoundingClientRect().height || 0);
  const filterHeight = filterBar ? Math.ceil(filterBar.getBoundingClientRect().height) : 0;
  document.body.style.setProperty("--sticky-toolbar-height", `${{toolbarHeight}}px`);
  document.body.style.setProperty("--sticky-filter-height", `${{filterHeight}}px`);
  const statusHeight = document.querySelector(".workbench-statusbar")?.getBoundingClientRect().height || 28;
  document.body.style.setProperty("--statusbar-height", `${{Math.ceil(statusHeight)}}px`);
  positionToolbarPopups();
}}

function scheduleStickyChromeUpdate() {{
  window.requestAnimationFrame(updateStickyChrome);
}}

function observeStickyChrome() {{
  if (!("ResizeObserver" in window)) return;
  const observer = new ResizeObserver(scheduleStickyChromeUpdate);
  const toolbar = document.querySelector(".workbench-chrome");
  const filterBar = document.querySelector(".filter-bar");
  if (toolbar) observer.observe(toolbar);
  if (filterBar) observer.observe(filterBar);
  observer.observe(document.querySelector(".workbench-statusbar"));
}}

function esc(text) {{
  return String(text ?? "").replace(/[&<>"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[ch]));
}}

function setState(text, cls="status") {{
  state.textContent = text;
  state.className = cls;
  state.title = text;
}}

function setSyncProgress(percent, text, visible = true) {{
  const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
  syncProgress.classList.toggle("open", !!visible);
  syncProgressFill.style.width = `${{safePercent}}%`;
  syncProgressText.textContent = text || "";
}}

function clearSyncProgress(delayMs = 900) {{
  window.setTimeout(() => setSyncProgress(0, "", false), delayMs);
}}

function stockProgressLocation(targetRows) {{
  const tr = Array.from(targetRows || [])[0];
  if (!tr) return "";
  const rowNumber = tr.querySelector("td[data-col='row_no']")?.textContent.trim() || "";
  const refs = compactRefsText(tr.dataset.refs || tr.dataset.ref || "");
  return [rowNumber ? `第${{rowNumber}}行` : "", refs].filter(Boolean).join("，");
}}

function setStockProgress(done, total, code = "", targetRows = null, complete = false, visible = true) {{
  const safeDone = Math.max(0, Number(done) || 0);
  const safeTotal = Math.max(0, Number(total) || 0);
  const percent = safeTotal ? (safeDone / safeTotal) * 100 : 0;
  const location = stockProgressLocation(targetRows);
  const detail = code ? `${{code}}${{location ? `（${{location}}）` : ""}}` : "";
  stockProgress.classList.toggle("open", !!visible);
  stockProgressFill.style.width = `${{Math.max(0, Math.min(100, percent))}}%`;
  stockProgressText.textContent = complete
    ? `库存查询完成 ${{safeDone}}/${{safeTotal}}`
    : `库存查询 ${{safeDone}}/${{safeTotal}}${{detail ? `：${{detail}}` : ""}}`;
}}

function clearStockProgress(delayMs = 1800) {{
  window.setTimeout(() => setStockProgress(0, 0, "", null, false, false), delayMs);
}}

function startSyncProgressTicker(steps) {{
  let index = 0;
  let timer = null;
  const tick = () => {{
    const step = steps[Math.min(index, steps.length - 1)];
    setSyncProgress(step.percent, step.text, true);
    if (index < steps.length - 1) {{
      index += 1;
      timer = window.setTimeout(tick, step.delay || 550);
    }}
  }};
  tick();
  return () => {{
    if (timer) window.clearTimeout(timer);
  }};
}}

function resetQueryAllButton() {{
  const unavailable = rowsLoading || loadedRowsBom === null || loadedRowsBom !== (bomSelect.value || "");
  queryMissingButton.disabled = unavailable;
  setToolbarLabel(queryMissingButton, "查询未查");
  queryAllButton.disabled = unavailable;
  setToolbarLabel(queryAllButton, "刷新全部");
}}

function cancelStockQueries(message = "") {{
  stockQueryRunId += 1;
  if (stockQueryController) stockQueryController.abort();
  stockQueryController = null;
  resetQueryAllButton();
  if (message) setState(message, "status");
}}

function beginStockQueryRun() {{
  cancelStockQueries();
  stockQueryController = new AbortController();
  stockQueryRunId += 1;
  return {{id: stockQueryRunId, signal: stockQueryController.signal}};
}}

function isActiveStockRun(run) {{
  return run && run.id === stockQueryRunId && !run.signal.aborted;
}}

const UI_STORAGE_KEY = "pxie4137_part_selection_ui_v2";
const DEFAULT_VIEW_STORAGE_KEY = `${{UI_STORAGE_KEY}}_default_v1`;
const UI_SCHEMA_VERSION = 7;
let defaultColumnWidthFrame = 0;
let candidateColumnWidthFrame = 0;
const DEFAULT_COLUMNS = [
  ["row_no", "行号"], ["review", "确认料"],
  ["refs", "位号"], ["page_refs", "页面位号"], ["quantity", "单套用量"],
  ["comment", "Comment"], ["value", "Value"], ["footprint", "Footprint"],
  ["stock", "选用物料 / 供货"], ["local_inventory", "本地库存"],
  ["order", "MOQ/MPQ"], ["price", "价格"], ["param", "物料参数"],
  ["alternates", "备选料"], ["note", "备注"]
];
const CANDIDATE_COLUMNS = [
  ["action", "操作"], ["image", "图片"], ["model", "型号/品牌/封装"],
  ["channel_stock", "渠道 / 库存交期"], ["param", "参数详情"], ["code", "C编码"],
  ["order", "MOQ/MPQ"], ["price", "价格梯度"]
];

function migrateSupplyColumns(view) {{
  if (Array.isArray(view.columnOrder)) {{
    view.columnOrder = [...new Set(view.columnOrder.map(key => key === "c_code" ? "stock" : key))];
  }}
  const hidden = view.hiddenColumns;
  if (hidden && Object.prototype.hasOwnProperty.call(hidden, "c_code")) {{
    hidden.stock = hidden.stock === true && hidden.c_code === true;
    delete hidden.c_code;
  }}
  const widths = view.columnWidths;
  if (widths && Object.prototype.hasOwnProperty.call(widths, "c_code")) {{
    widths.stock = Math.max(220, Number(widths.stock || widths.c_code) || 280);
    delete widths.c_code;
  }}
  return view;
}}

function loadUiSettings() {{
  try {{
    const settings = JSON.parse(localStorage.getItem(UI_STORAGE_KEY) || "{{}}");
    if (settings.schemaVersion !== UI_SCHEMA_VERSION) {{
      const savedOrder = Array.isArray(settings.columnOrder) ? [...settings.columnOrder] : [];
      const badV4Default = settings.schemaVersion === 4 && savedOrder.length === 1 && savedOrder[0] === "alternates";
      const order = (!savedOrder.length || badV4Default) ? DEFAULT_COLUMNS.map(([key]) => key) : savedOrder;
      if (!order.includes("alternates")) {{
        const noteIndex = order.indexOf("note");
        if (noteIndex >= 0) order.splice(noteIndex, 0, "alternates");
        else order.push("alternates");
      }}
      settings.columnOrder = order.filter(key => key !== "search");
      delete (settings.hiddenColumns || {{}}).search;
      delete (settings.columnWidths || {{}}).search;
      if (!settings.schemaVersion || settings.schemaVersion < 6) {{
        settings.hiddenColumns = Object.assign({{}}, settings.hiddenColumns || {{}}, {{note: true}});
      }}
      migrateSupplyColumns(settings);
      delete (settings.hiddenColumns || {{}}).search;
      if (settings.columnWidths) delete settings.columnWidths.search;
      settings.schemaVersion = UI_SCHEMA_VERSION;
      saveUiSettings(settings);
    }}
    return settings;
  }} catch (_err) {{
    return {{}};
  }}
}}

function saveUiSettings(settings) {{
  localStorage.setItem(UI_STORAGE_KEY, JSON.stringify(settings));
}}

function updateUiSettings(update) {{
  const settings = Object.assign({{}}, loadUiSettings(), update);
  saveUiSettings(settings);
  return settings;
}}

const DEFAULT_VIEW_FIELDS = [
  "columnOrder", "hiddenColumns", "columnWidths", "columnWidthMode",
  "candidateColumnOrder", "candidateHiddenColumns", "candidateColumnWidths", "candidateColumnWidthMode"
];

function saveDefaultViewConfig() {{
  const settings = loadUiSettings();
  const view = Object.fromEntries(DEFAULT_VIEW_FIELDS
    .filter(key => Object.prototype.hasOwnProperty.call(settings, key))
    .map(key => [key, settings[key]]));
  localStorage.setItem(DEFAULT_VIEW_STORAGE_KEY, JSON.stringify(view));
  setState("当前列表配置已保存为默认", "saved");
}}

function loadDefaultViewConfig() {{
  const saved = localStorage.getItem(DEFAULT_VIEW_STORAGE_KEY);
  if (!saved) {{
    setState("尚未保存默认列表配置", "status");
    return;
  }}
  const view = JSON.parse(saved);
  if (Array.isArray(view.columnOrder)) {{
    view.columnOrder = view.columnOrder.filter(key => key !== "search");
  }}
  migrateSupplyColumns(view);
  delete (view.hiddenColumns || {{}}).search;
  if (view.columnWidths) delete view.columnWidths.search;
  const update = Object.fromEntries(DEFAULT_VIEW_FIELDS
    .filter(key => Object.prototype.hasOwnProperty.call(view, key))
    .map(key => [key, view[key]]));
  updateUiSettings(update);
  applyColumnOrder();
  applySavedColumnWidths();
  applyColumnVisibility();
  applyCandidateColumnOrder();
  applyCandidateSavedWidths();
  makeCandidateColumnsConfigurable();
  renderColumnPanel();
  setState("已加载默认列表配置", "saved");
}}

function columnHiddenMap(settings = loadUiSettings()) {{
  return settings.hiddenColumns || {{}};
}}

function columnKeys() {{
  return DEFAULT_COLUMNS.map(([key]) => key);
}}

function columnLabel(key) {{
  const found = DEFAULT_COLUMNS.find(([itemKey]) => itemKey === key);
  return found ? found[1] : key;
}}

function orderedColumns(settings = loadUiSettings()) {{
  const keys = columnKeys();
  const saved = Array.isArray(settings.columnOrder) ? settings.columnOrder : [];
  const savedExisting = saved.filter(key => keys.includes(key));
  const missing = keys.filter(key => !saved.includes(key));
  const defaultFront = missing.filter(key => ["row_no", "review"].includes(key));
  const defaultRest = missing.filter(key => !defaultFront.includes(key));
  return [...defaultFront, ...savedExisting, ...defaultRest];
}}

function saveColumnOrder(order) {{
  updateUiSettings({{columnOrder: order.filter(key => columnKeys().includes(key))}});
}}

function applyColumnOrder() {{
  const order = orderedColumns();
  document.querySelectorAll("thead tr, tbody tr").forEach(row => {{
    for (const key of order) {{
      const cell = row.querySelector(`[data-col="${{key}}"]`);
      if (cell) row.appendChild(cell);
    }}
  }});
}}

function applyColumnVisibility() {{
  const hidden = columnHiddenMap();
  DEFAULT_COLUMNS.forEach(([key]) => {{
    document.querySelectorAll(`[data-col="${{key}}"]`).forEach(el => {{
      el.classList.toggle("hidden-col", !!hidden[key]);
    }});
  }});
  applySavedColumnWidths();
}}

function renderColumnPanel() {{
  const hidden = columnHiddenMap();
  columnList.innerHTML = orderedColumns().map(key => `
    <div class="view-row" draggable="true" data-column-row="${{esc(key)}}">
      <span class="drag-handle">⋮⋮</span>
      <span>${{esc(columnLabel(key))}}</span>
      <input type="checkbox" data-column-toggle="${{esc(key)}}" ${{hidden[key] ? "" : "checked"}}>
    </div>`).join("");
}}

function fitDefaultColumnWidths(candidate = false) {{
  const settings = loadUiSettings();
  const property = candidate ? "candidateColumnWidths" : "columnWidths";
  const modeProperty = candidate ? "candidateColumnWidthMode" : "columnWidthMode";
  // Legacy saved widths are user preferences; only auto layouts follow new content.
  if (settings[modeProperty] !== "auto" && Object.keys(settings[property] || {{}}).length) return;
  const pending = candidate ? candidateColumnWidthFrame : defaultColumnWidthFrame;
  if (pending) cancelAnimationFrame(pending);
  const frame = requestAnimationFrame(() => {{
    if (candidate) candidateColumnWidthFrame = 0;
    else defaultColumnWidthFrame = 0;
    const latestSettings = loadUiSettings();
    if (latestSettings[modeProperty] !== "auto" && Object.keys(latestSettings[property] || {{}}).length) {{
      return;
    }}
    autoSizeColumns(candidate);
  }});
  if (candidate) candidateColumnWidthFrame = frame;
  else defaultColumnWidthFrame = frame;
}}

function columnWidthLimits(key, candidate) {{
  const limits = candidate ? {{
    action: [168, 190], image: [64, 80], model: [180, 300], channel_stock: [205, 280],
    param: [220, 380], code: [100, 125], order: [100, 130], price: [140, 210]
  }} : {{
    row_no: [64, 80], review: [210, 300], refs: [100, 200], page_refs: [130, 240],
    quantity: [70, 95], comment: [120, 240], value: [100, 190], footprint: [130, 250],
    stock: [220, 310], local_inventory: [145, 210], order: [80, 115],
    price: [100, 160], param: [220, 360], alternates: [220, 380], note: [160, 280]
  }};
  return limits[key] || [80, 280];
}}

function fitColumnWidthBudget(columns, available) {{
  const sizes = columns.map(col => ({{...col, width: Math.max(col.min, Math.min(col.max, col.width))}}));
  const total = sizes.reduce((sum, col) => sum + col.width, 0);
  const minimum = sizes.reduce((sum, col) => sum + col.min, 0);
  const shrink = available < minimum ? 0 : Math.min(1, Math.max(0, (total - available) / (total - minimum || 1)));
  return Object.fromEntries(sizes.map(col => [col.key, Math.round(col.width - (col.width - col.min) * shrink)]));
}}

function measureColumnWidth(table, key, candidate) {{
  const attr = candidate ? "data-candidate-col" : "data-col";
  const rows = Array.from(table.tBodies[0]?.rows || []).filter(row => !row.hidden && !row.classList.contains("hidden-row"));
  // Sample throughout the visible result set, not just its first rows.
  const step = Math.max(1, Math.ceil(rows.length / 100));
  const cells = [table.querySelector(`th[${{attr}}="${{key}}"]`),
    ...rows.filter((_, index) => index % step === 0 || index === rows.length - 1)
      .map(row => row.querySelector(`td[${{attr}}="${{key}}"]`))].filter(Boolean);
  const probe = document.createElement("table");
  probe.className = table.className;
  probe.setAttribute("aria-hidden", "true");
  probe.style.cssText = "position:fixed;left:-100000px;top:0;visibility:hidden;pointer-events:none;width:max-content;min-width:0;max-width:none;table-layout:auto";
  const body = probe.createTBody();
  for (const cell of cells) {{
    const clone = cell.cloneNode(true);
    clone.removeAttribute("id");
    clone.querySelectorAll("[id]").forEach(el => el.removeAttribute("id"));
    const selector = "textarea, input:not([type='checkbox']), select";
    const originals = cell.querySelectorAll(selector);
    clone.querySelectorAll(selector).forEach((field, index) => {{
      const original = originals[index];
      const label = document.createElement("span");
      label.textContent = original.tagName === "SELECT" ? original.selectedOptions[0]?.textContent || "" : original.value || original.placeholder || "";
      const style = getComputedStyle(original);
      label.style.cssText = "display:inline-block;white-space:pre;min-width:0;max-width:none";
      label.style.font = style.font;
      label.style.padding = style.padding;
      if (original.tagName === "SELECT") label.style.paddingRight = "24px";
      if (original.type === "hidden") label.hidden = true;
      field.replaceWith(label);
    }});
    clone.querySelectorAll("details:not([open])").forEach(details => {{
      Array.from(details.children).filter(child => child.tagName !== "SUMMARY").forEach(child => child.remove());
    }});
    clone.querySelectorAll(".resize-grip, [hidden], .hidden-col").forEach(el => el.remove());
    Object.assign(clone.style, {{position: "static", width: "auto", minWidth: "0", maxWidth: "none", whiteSpace: "nowrap"}});
    body.insertRow().appendChild(clone);
  }}
  document.body.appendChild(probe);
  try {{ return Math.ceil(probe.getBoundingClientRect().width + 12); }}
  finally {{ probe.remove(); }}
}}

function autoSizeColumns(candidate = false, onlyKey = null) {{
  const pending = candidate ? candidateColumnWidthFrame : defaultColumnWidthFrame;
  if (pending) cancelAnimationFrame(pending);
  if (candidate) candidateColumnWidthFrame = 0;
  else defaultColumnWidthFrame = 0;
  const table = document.querySelector(candidate ? ".candidate-table" : "#rows");
  const target = candidate ? table : table?.closest("table");
  if (!target) return;
  const attr = candidate ? "data-candidate-col" : "data-col";
  const columns = Array.from(target.querySelectorAll(`th[${{attr}}]`))
    .filter(th => !th.classList.contains("hidden-col"))
    .map(th => ({{key: candidate ? th.dataset.candidateCol : th.dataset.col}}))
    .filter(col => !onlyKey || col.key === onlyKey)
    .map(col => {{
      const [min, max] = columnWidthLimits(col.key, candidate);
      return {{...col, min, max, width: measureColumnWidth(target, col.key, candidate)}};
    }});
  if (!columns.length) return;
  const available = onlyKey ? Infinity : (candidate ? target.parentElement.clientWidth : document.documentElement.clientWidth) - 2;
  const property = candidate ? "candidateColumnWidths" : "columnWidths";
  const modeProperty = candidate ? "candidateColumnWidthMode" : "columnWidthMode";
  updateUiSettings({{[property]: {{...(loadUiSettings()[property] || {{}}), ...fitColumnWidthBudget(columns, available)}}, [modeProperty]: onlyKey ? "manual" : "auto"}});
  if (candidate) applyCandidateSavedWidths();
  else applySavedColumnWidths();
}}

function applyTableColumnWidths(table, widths, candidate = false) {{
  if (!table) return;
  const attr = candidate ? "data-candidate-col" : "data-col";
  let total = 0;
  table.querySelectorAll(`th[${{attr}}]`).forEach(th => {{
    const key = candidate ? th.dataset.candidateCol : th.dataset.col;
    const width = Number(widths[key]);
    if (Number.isFinite(width) && width > 0) {{
      th.style.width = `${{width}}px`;
      th.style.minWidth = `${{width}}px`;
    }}
    if (!th.classList.contains("hidden-col")) total += parseFloat(th.style.width) || 100;
  }});
  if (Object.keys(widths).length) {{
    table.style.tableLayout = "fixed";
    table.style.width = `${{total + 1}}px`;
  }}
}}

function applySavedColumnWidths() {{
  const widths = loadUiSettings().columnWidths || {{}};
  applyTableColumnWidths(document.getElementById("rows").closest("table"), widths);
}}

function makeColumnsResizable() {{
  document.querySelectorAll("th[data-col]").forEach(th => {{
    if (th.querySelector(".resize-grip")) return;
    const grip = document.createElement("span");
    grip.className = "resize-grip";
    grip.title = "拖动调整列宽；双击自动适应内容";
    grip.addEventListener("dblclick", event => {{
      event.preventDefault();
      event.stopPropagation();
      autoSizeColumns(false, th.dataset.col);
    }});
    th.appendChild(grip);
    grip.addEventListener("mousedown", event => {{
      event.preventDefault();
      if (defaultColumnWidthFrame) {{
        cancelAnimationFrame(defaultColumnWidthFrame);
        defaultColumnWidthFrame = 0;
      }}
      const startX = event.clientX;
      const startWidth = th.offsetWidth;
      const dragWidths = {{...(loadUiSettings().columnWidths || {{}})}};
      function onMove(moveEvent) {{
        const width = Math.max(48, startWidth + moveEvent.clientX - startX);
        dragWidths[th.dataset.col] = width;
        applyTableColumnWidths(th.closest("table"), dragWidths);
      }}
      function onUp() {{
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        const settings = loadUiSettings();
        const widths = Object.assign({{}}, settings.columnWidths || {{}});
        widths[th.dataset.col] = Math.round(parseFloat(th.style.width));
        updateUiSettings({{columnWidths: widths, columnWidthMode: "manual"}});
        applySavedColumnWidths();
      }}
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    }});
  }});
}}

function candidateColumnKeys() {{
  return CANDIDATE_COLUMNS.map(([key]) => key);
}}

function orderedCandidateColumns(settings = loadUiSettings()) {{
  const keys = candidateColumnKeys();
  const saved = Array.isArray(settings.candidateColumnOrder) ? settings.candidateColumnOrder : [];
  const migrated = [];
  for (const key of saved) {{
    const normalized = key === "channel" || key === "stock" ? "channel_stock" : key;
    if (keys.includes(normalized) && !migrated.includes(normalized)) migrated.push(normalized);
  }}
  return [...migrated, ...keys.filter(key => !migrated.includes(key))];
}}

function saveCandidateColumnOrder(order) {{
  const keys = candidateColumnKeys();
  const migrated = [];
  for (const key of order) {{
    const normalized = key === "channel" || key === "stock" ? "channel_stock" : key;
    if (keys.includes(normalized) && !migrated.includes(normalized)) migrated.push(normalized);
  }}
  updateUiSettings({{candidateColumnOrder: migrated}});
}}

function applyCandidateColumnOrder() {{
  const order = orderedCandidateColumns();
  document.querySelectorAll(".candidate-table thead tr, .candidate-table tbody tr").forEach(row => {{
    for (const key of order) {{
      const cell = row.querySelector(`[data-candidate-col="${{key}}"]`);
      if (cell) row.appendChild(cell);
    }}
  }});
}}

function applyCandidateSavedWidths() {{
  const widths = loadUiSettings().candidateColumnWidths || {{}};
  applyTableColumnWidths(document.querySelector(".candidate-table"), widths, true);
}}

function makeCandidateColumnsConfigurable() {{
  document.querySelectorAll(".candidate-table th[data-candidate-col]").forEach(th => {{
    if (!th.dataset.configured) {{
      th.dataset.configured = "1";
      th.draggable = true;
      th.title = "拖动表头调整列顺序；拖右侧边缘调整列宽";
      th.addEventListener("dragstart", event => {{
        if (event.target.closest(".resize-grip, .candidate-filter-trigger, .candidate-stock-filter")) {{
          event.preventDefault();
          return;
        }}
        draggedCandidateColumnKey = th.dataset.candidateCol;
        th.classList.add("dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", draggedCandidateColumnKey);
      }});
      th.addEventListener("dragend", () => {{
        th.classList.remove("dragging");
        draggedCandidateColumnKey = "";
      }});
      th.addEventListener("dragover", event => {{
        if (!draggedCandidateColumnKey || draggedCandidateColumnKey === th.dataset.candidateCol) return;
        event.preventDefault();
      }});
      th.addEventListener("drop", event => {{
        if (!draggedCandidateColumnKey || draggedCandidateColumnKey === th.dataset.candidateCol) return;
        event.preventDefault();
        const order = orderedCandidateColumns();
        const from = order.indexOf(draggedCandidateColumnKey);
        const to = order.indexOf(th.dataset.candidateCol);
        if (from < 0 || to < 0) return;
        order.splice(to, 0, order.splice(from, 1)[0]);
        saveCandidateColumnOrder(order);
        applyCandidateColumnOrder();
        applyCandidateSavedWidths();
      }});
    }}
    if (th.querySelector(".resize-grip")) return;
    const grip = document.createElement("span");
    grip.className = "resize-grip";
    grip.title = "拖动调整列宽；双击自动适应内容";
    grip.addEventListener("dblclick", event => {{
      event.preventDefault();
      event.stopPropagation();
      autoSizeColumns(true, th.dataset.candidateCol);
    }});
    th.appendChild(grip);
    grip.addEventListener("mousedown", event => {{
      event.preventDefault();
      event.stopPropagation();
      const startX = event.clientX;
      const startWidth = th.offsetWidth;
      const dragWidths = {{...(loadUiSettings().candidateColumnWidths || {{}})}};
      function onMove(moveEvent) {{
        const width = Math.max(28, startWidth + moveEvent.clientX - startX);
        dragWidths[th.dataset.candidateCol] = width;
        applyTableColumnWidths(th.closest("table"), dragWidths, true);
      }}
      function onUp() {{
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        const settings = loadUiSettings();
        const widths = Object.assign({{}}, settings.candidateColumnWidths || {{}});
        widths[th.dataset.candidateCol] = Math.round(parseFloat(th.style.width));
        updateUiSettings({{candidateColumnWidths: widths, candidateColumnWidthMode: "manual"}});
        applyCandidateSavedWidths();
      }}
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    }});
  }});
}}

async function jump(url) {{
  try {{
    await flushRowEdits();
    const response = await fetch('/api/kicad_jump', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{url}})}});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || data.error || 'KiCad jump failed');
  }} catch (error) {{
    setState(String(error.message || error), 'status error');
  }}
}}

function refButton(item) {{
  return `<button type="button" class="ref-link" data-ref-link="${{esc(item.ref)}}" data-ref-page="${{esc(item.page || "")}}" title="${{esc(item.page || "")}}；右键可拆出单独编辑" onclick="jump('${{esc(item.url)}}')">${{esc(item.ref)}}</button>`;
}}

function renderMultiSplitPicker(row) {{
  const links = row.ref_links || [];
  const byPage = new Map();
  for (const item of links) {{
    const page = item.page || "Unknown";
    if (!byPage.has(page)) byPage.set(page, []);
    byPage.get(page).push(item);
  }}
  const options = Array.from(byPage, ([page, items]) => `<div class="ref-split-page"><label class="ref-multi-option"><input type="checkbox" data-multi-split-page="${{esc(page)}}" ${{items.every(item => multiSplitRefs.has(item.ref)) ? "checked" : ""}}><strong>${{esc(page)}}</strong></label><div>${{items.map(item => `<label class="ref-multi-option"><input type="checkbox" data-multi-split-ref="${{esc(item.ref)}}" ${{multiSplitRefs.has(item.ref) ? "checked" : ""}}><span class="mono">${{esc(item.ref)}}</span></label>`).join("")}}</div></div>`).join("");
  return `<div class="ref-multi-picker"><strong>选择要拆出的位号</strong>${{options}}<div class="ref-multi-actions"><button type="button" data-action="split-selected-refs" data-group-id="${{esc(row.group_id || row.ref)}}">拆出所选</button><button type="button" data-action="cancel-multi-split">取消</button></div></div>`;
}}

function renderRefButtons(row) {{
  const links = row.ref_links || [];
  if (links.length && multiSplitSource === "refs" && multiSplitGroupId === (row.group_id || row.ref)) return renderMultiSplitPicker(row);
  if (links.length) return `${{links.map(refButton).join(", ")}}${{links.length > 1 ? ` <button type="button" class="small action-link" data-action="start-multi-split" data-split-source="refs" title="选中的位号拆到同一行">多选拆行</button>` : ""}}`;
  return `<button type="button" class="ref-link" onclick="jump('${{esc(row.kicad_jump_url)}}')">${{esc(row.ref)}}</button>`;
}}

function renderPageRefButtons(row) {{
  const byPage = new Map();
  for (const item of (row.ref_links || [])) {{
    const page = item.page || "Unknown";
    if (!byPage.has(page)) byPage.set(page, []);
    byPage.get(page).push(item);
  }}
  const links = row.ref_links || [];
  if (links.length && multiSplitSource === "page" && multiSplitGroupId === (row.group_id || row.ref)) return renderMultiSplitPicker(row);
  const splitButton = links.length > 1 ? ` <button type="button" class="small action-link page-split-button" data-action="start-multi-split" data-split-source="page" title="按页面或位号勾选，所选器件拆到同一行">按页面拆分</button>` : "";
  if (!byPage.size) return `${{esc(row.page_refs || "")}}${{splitButton}}`;
  return `${{Array.from(byPage.entries())
    .map(([page, items]) => `<div><label class="ref-multi-option"><input type="checkbox" data-start-split-page="${{esc(page)}}" aria-label="拆分页面 ${{esc(page)}}"><span class="mono">${{esc(page)}}</span></label>: ${{items.map(refButton).join(", ")}}</div>`)
    .join("")}}${{splitButton}}`;
}}

function refsWithout(refs, ref) {{
  return (refs || []).filter(item => item !== ref);
}}

function buildPageRefs(refLinks) {{
  const byPage = new Map();
  for (const item of refLinks || []) {{
    const page = item.page || "Unknown";
    if (!byPage.has(page)) byPage.set(page, []);
    byPage.get(page).push(item.ref);
  }}
  return Array.from(byPage.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([page, refs]) => `${{page}}: ${{refs.join(",")}}`)
    .join("; ");
}}

function rowForRefs(row, refs) {{
  const selected = new Set(refs);
  const links = refs.map(ref => (row.ref_links || []).find(item => item.ref === ref) || {{ref, page: row.page || "", url: row.kicad_jump_url || ""}});
  const clone = Object.assign({{}}, row);
  clone.group_id = `${{row.group_id || row.ref || "G"}}_split_${{refs.join("_")}}`;
  clone.ref = refs[0];
  clone.refs = refs.join(",");
  clone.refs_list = [...refs];
  clone.ref_links = links;
  clone.page = links[0]?.page || row.page || "";
  clone.page_refs = buildPageRefs(clone.ref_links);
  clone.quantity = refs.length;
  clone.temp_split = true;
  if (row.spec_check) {{
    clone.spec_check = {{...row.spec_check, status: "unknown", issues: [], unknown_fields: ["value", "footprint"], unknown_details: [], error: ""}};
    clone.spec_check_needs_refresh = true;
  }}
  for (const key of ["per_ref_sync_fields", "schematic_fields_by_ref", "per_ref_selection", "description_by_ref", "notes_by_ref", "field_diffs_by_ref", "external_schematic_updates", "schematic_field_conflicts", "stock_by_ref"]) {{
    if (row[key]) clone[key] = Object.fromEntries(Object.entries(row[key]).filter(([ref]) => selected.has(ref)));
  }}
  if (row.notes_by_ref) {{
    const notes = refs.filter(ref => clone.notes_by_ref[ref]).map(ref => clone.notes_by_ref[ref]);
    clone.note = new Set(notes).size <= 1 ? notes[0] || "" : refs.filter(ref => clone.notes_by_ref[ref]).map(ref => `${{ref}}: ${{clone.notes_by_ref[ref]}}`).join("\\n");
  }}
  clone.missing_schematic_refs = String(row.missing_schematic_refs || "").split(",").filter(ref => selected.has(ref.trim())).join(",");
  clone.field_changed_refs = String(row.field_changed_refs || "").split(",").filter(ref => selected.has(ref.trim())).join(",");
  clone.field_diffs = Object.fromEntries(Object.entries(row.field_diffs || {{}}).flatMap(([key, diff]) => {{
    const affected = String(diff.refs || "").split(",").filter(ref => selected.has(ref.trim()));
    return affected.length ? [[key, {{...diff, refs: affected.join(",")}}]] : [];
  }}));
  return clone;
}}

function rowForSingleRef(row, ref) {{ return rowForRefs(row, [ref]); }}

function splitRefToStandaloneRow(ref) {{
  for (let index = 0; index < currentRows.length; index += 1) {{
    const row = currentRows[index];
    const refs = row.refs_list || [];
    if (!refs.includes(ref)) continue;
    if (refs.length <= 1) {{
      setState(`${{ref}} 已经是单独一行`, "status");
      return;
    }}
    const remainingRefs = refsWithout(refs, ref);
    currentRows.splice(index, 1, rowForRefs(row, remainingRefs), rowForSingleRef(row, ref));
    renderRows(currentRows);
    setState(`${{ref}} 已拆出临时单独行`, "saved");
    return;
  }}
  setState(`没有在当前表格中找到 ${{ref}}`, "error");
}}

function startMultiSplit(row, source = "refs") {{
  const refs = row?.refs_list || [];
  if (refs.length <= 1) {{
    setState(`${{row?.ref || "位号"}} 已经是单独一行`, "status");
    return;
  }}
  multiSplitGroupId = row.group_id || row.ref;
  multiSplitSource = source;
  multiSplitRefs = new Set();
  renderRows(currentRows);
  setState(`已进入多选拆行：请勾选 ${{row.refs}} 中需要独立编辑的位号`, "status");
}}

function cancelMultiSplit() {{
  multiSplitGroupId = "";
  multiSplitSource = "refs";
  multiSplitRefs = new Set();
  renderRows(currentRows);
}}

function splitSelectedRefs(groupId) {{
  const selected = new Set(multiSplitRefs);
  const index = currentRows.findIndex(row => (row.group_id || row.ref) === groupId);
  if (index < 0 || !selected.size) {{
    setState("请至少选择一个位号", "error");
    return;
  }}
  const row = currentRows[index];
  const refs = row.refs_list || [];
  const selectedRefs = refs.filter(ref => selected.has(ref));
  const remainingRefs = refs.filter(ref => !selected.has(ref));
  if (!selectedRefs.length) {{
    setState("请至少选择一个位号", "error");
    return;
  }}
  const originalRow = Object.assign({{}}, row, {{
    ref_links: [...(row.ref_links || [])],
    refs_list: [...refs]
  }});
  const splitRows = [rowForRefs(originalRow, selectedRefs)];
  if (remainingRefs.length) {{
    currentRows.splice(index, 1, rowForRefs(originalRow, remainingRefs), ...splitRows);
  }} else {{
    currentRows.splice(index, 1, ...splitRows);
  }}
  multiSplitGroupId = "";
  multiSplitSource = "refs";
  multiSplitRefs = new Set();
  renderRows(currentRows);
  setState(`已将 ${{selectedRefs.length}} 个位号拆到同一行`, "saved");
}}

function stockCode(row) {{
  if (row?.selection_codes_mixed) return "";
  const text = Object.hasOwn(row, "effective_c_code") ? row.effective_c_code
    : row.stock_c_code || row.accepted_c_code || row.lcsc_match_c_code || "";
  const match = String(text).toUpperCase().match(/\\bC\\d+\\b/);
  return match ? match[0] : "";
}}

function normalizeCCode(text) {{
  const compact = String(text || "").toUpperCase().replace(/\\bC\\s+(\\d+)\\b/g, "C$1");
  const match = compact.match(/\\bC\\d+\\b/);
  return match ? match[0] : "";
}}

function originalCode(row) {{
  return stockCode(row);
}}

function statusValue(row, ov) {{
  if (row?.code_status) return row.code_status;
  if (row?.selection_codes_mixed) return "Mixed";
  if ((ov.decision || row.decision) === "Reject" || ov.status === "Rejected") return "Rejected";
  if ((ov.decision || row.decision) === "AcceptManual" || ov.status === "ChangedToNewCode") return "ChangedToNewCode";
  return "Original";
}}

function statusText(value) {{
  if (value === "Mixed") return "未确认（位号选料不同）";
  if (value === "ChangedToNewCode") return "已改动为新编码";
  if (value === "Rejected") return "不接受/留空";
  return "当前主料";
}}

function statusClass(value) {{
  if (value === "Mixed") return "status-original";
  if (value === "ChangedToNewCode") return "status-changed";
  if (value === "Rejected") return "status-rejected";
  return "status-original";
}}

function reviewStatusText(value) {{
  if (value === "Confirmed") return "已确认";
  if (value === "Rejected") return "不确认";
  if (value === "Mixed") return "未确认（位号状态不同）";
  return "待确认";
}}

function reviewStatusClass(value) {{
  if (value === "Confirmed") return "review-ok";
  if (value === "Rejected") return "review-bad";
  return "review-warn";
}}

function stockNumber(stock) {{
  const number = Number(String(stock?.stock || "").replace(/,/g, ""));
  return Number.isFinite(number) ? number : Number.NaN;
}}

function stockIsLow(stock) {{
  if (Array.isArray(stock?.entries)) return stock.entries.some(item => stockIsLow(item.stock || {{}}));
  const number = stockNumber(stock);
  return !!(stock?.ok && Number.isFinite(number) && number < 100);
}}

function stockIsIssue(stock) {{
  if (Array.isArray(stock?.entries)) return stock.entries.some(item => stockIsIssue(item.stock || {{}}));
  if (stockIsLow(stock)) return true;
  return !!(stockAlreadyQueried(stock) && !stock?.ok);
}}

function rowIsNc(row) {{
  const value = String(row?.value || "").toUpperCase();
  const comment = String(row?.comment || "").trim().toUpperCase();
  const ncPattern = /(^|[^A-Z0-9])(NC|DNP|DNF|NO\s*LOAD)(?=$|[^A-Z0-9])/;
  return ncPattern.test(value) || ["NC", "DNP", "DNF", "NO LOAD"].includes(comment);
}}

function rowHasCode(row, stock) {{
  return !!(stockCode(row) || normalizeCCode(stock?.c_code || "") || (row?.selection_codes || []).some(Boolean));
}}

function rowHasMissingRef(row) {{
  return !!String(row?.missing_schematic_refs || "").trim();
}}

function rowVisualClass(row, selectedStatus, syncNeeded, stock, reviewStatus) {{
  if (syncNeeded || rowHasMissingRef(row) || row?.sync_error) return "row-needs-sync";
  if (selectedStatus === "Rejected" || reviewStatus === "Rejected" || rowIsNc(row) || !rowHasCode(row, stock)) return "row-muted";
  if (reviewStatus !== "Confirmed" || !stockAlreadyQueried(stock) || stockIsLow(stock)) return "row-warning";
  if (reviewStatus === "Confirmed" && stock?.ok) return "row-ok";
  return "row-warning";
}}

function rowFilterKeys(row, selectedStatus, syncNeeded, stock, reviewStatus) {{
  const keys = ["all"];
  const isNc = rowIsNc(row);
  keys.push(syncNeeded ? "needs_sync" : "sync_done");
  keys.push(reviewStatus !== "Confirmed" ? "pending_review" : "review_confirmed");
  const codeChanged = selectedStatus === "ChangedToNewCode" || row?.status === "ChangedToNewCode" || row?.manual_c_code || row?.stock_c_code || Object.values(row?.per_ref_selection || {{}}).some(item => item.decision === "AcceptManual");
  keys.push(codeChanged ? "changed_code" : "code_unchanged");
  if (!isNc) {{
    if (stockIsIssue(stock)) keys.push("stock_issue");
    else if (stockAlreadyQueried(stock) && stock?.ok && !stockIsLow(stock)) keys.push("stock_normal");
  }}
  keys.push(rowHasMissingRef(row) ? "missing_ref" : "ref_complete");
  keys.push(isNc ? "nc_device" : "non_nc");
  return keys;
}}

function initialStock(row) {{
  const code = stockCode(row);
  const stock = row.stock || (code ? {{status: "待查询", c_code: code}} : {{status: "请提供料号", c_code: "", error: "请提供料号"}});
  return stockWithSavedDescription(row, stock);
}}

function stockWithSavedDescription(row, stock) {{
  if (row?.selection_codes_mixed) return stock;
  const fields = row?.override?.field_overrides || {{}};
  if (fields.description && fields.description_c_code === stockCode(row)) {{
    return {{...stock, param: fields.description, param_detail: "", arrange: "", category: ""}};
  }}
  return stock;
}}

function stockVisual(stock) {{
  const status = stock.status || "";
  const stockNumber = Number(String(stock.stock || "").replace(/,/g, ""));
  const lowStock = stock.ok && Number.isFinite(stockNumber) && stockNumber < 100;
  if (lowStock) return {{pill: "stock-low", text: "低库存", detailClass: "low-stock"}};
  if (stock.ok) return {{pill: "stock-ok", text: "现货", detailClass: ""}};
  if (["待查询", "查询中"].includes(status)) return {{pill: "stock-wait", text: status, detailClass: ""}};
  return {{pill: "stock-bad", text: status || "异常", detailClass: "bad"}};
}}

function stockParamText(stock) {{
  const primary = stock.param || "";
  const detail = stock.param_detail || "";
  const pieces = [];
  if (primary) pieces.push(primary);
  if (detail && detail !== primary) pieces.push(detail);
  const existingText = pieces.join("\\n");
  const packageName = String(stock.package || "").trim();
  if (packageName && !/(?:封装|package)\\s*[:：]/i.test(existingText)) pieces.push(`封装: ${{packageName}}`);
  const brand = String(stock.brand || "").trim();
  const normalizedText = existingText.replace(/\\s+/g, "").toLocaleLowerCase();
  if (brand && !normalizedText.includes(brand.replace(/\\s+/g, "").toLocaleLowerCase())) pieces.push(`品牌: ${{brand}}`);
  return pieces.join("\\n");
}}

function renderStockParam(stock) {{
  return renderCandidateParam(stockParamText(stock));
}}

function stockAlreadyQueried(stock) {{
  if (Array.isArray(stock?.entries)) return stock.entries.length > 0 && stock.entries.every(item => stockAlreadyQueried(item.stock || {{}}));
  const status = stock.status || "";
  return !!(stock.ok || stock.fetched_at || (stock.c_code && status && !["待查询", "查询中", "请提供料号"].includes(status)));
}}

function renderStock(stock) {{
  const status = stock.status || "";
  const visual = stockVisual(stock);
  const detail = stock.ok ? `库存 ${{esc(stock.stock || "0")}}${{stock.delivery ? ` · ${{esc(stock.delivery)}}` : ""}}` : esc(stock.error || status);
  const extra = [stock.channel, stock.stock_sz != null ? `深圳 ${{stock.stock_sz}} / 江苏 ${{stock.stock_js || 0}} / 香港 ${{stock.stock_hk || 0}}` : "",
    stock.fetched_at ? `查询时间: ${{String(stock.fetched_at).replace("T", " ")}}` : ""].filter(Boolean);
  return `<div class="stock-card ${{visual.detailClass}}"><div><span class="stock-pill ${{visual.pill}}">${{esc(visual.text)}}</span> ${{detail}}</div>${{extra.length ? `<details class="stock-details"><summary>库存详情</summary>${{extra.map(esc).join("<br>")}}</details>` : ""}}</div>`;
}}

function renderStockCell(stock) {{
  if (Array.isArray(stock?.entries)) {{
    const entries = stock.entries.map(item => `<div class="stock-ref-entry"><strong>${{esc(item.ref)}}</strong> · ${{renderStock(item.stock || {{}})}}</div>`).join("");
  return `<div class="stock-mixed-list">${{entries}}</div><button type="button" class="small stock-query" data-action="query-stock">刷新组内库存</button>`;
  }}
  if (!stock.c_code) return '<div class="stock-wait">未选立创自营料</div>';
  return `${{renderStock(stock)}}<button type="button" class="small stock-query" data-action="query-stock">查询库存</button>`;
}}

function renderSupplyOrder(row) {{
  const order = row?.procurement_choice || {{}};
  if (!/^https?:/i.test(order.order_url || "")) return "";
  return `<div class="supply-order">${{esc(order.supplier || "订货渠道")}} · <a href="${{esc(order.order_url)}}" target="_blank" rel="noopener" data-secondary-url="${{esc(order.order_url)}}" data-secondary-label="订货料下单链接">订货链接</a></div>`;
}}

function rowDataForElement(tr) {{
  if (!tr) return null;
  return currentRows.find(row => row.refs === tr.dataset.refs || row.ref === tr.dataset.ref) || null;
}}

function alternateStockPill(stock) {{
  if (!stock || !Object.keys(stock).length || !stockAlreadyQueried(stock)) {{
    return '<span class="stock-pill stock-wait">未查</span>';
  }}
  const visual = stockVisual(stock);
  const count = stock.ok && stock.stock !== undefined ? ` ${{esc(stock.stock)}}` : "";
  return `<span class="stock-pill ${{visual.pill}}">${{esc(visual.text)}}${{count}}</span>`;
}}

function alternateCodes(parts) {{
  return (parts || []).map(item => normalizeCCode(item.c_code)).filter(Boolean);
}}

function renderAlternateParts(row) {{
  if (row?.selection_mixed) {{
    const records = Object.entries(row.per_ref_selection || {{}}).map(([ref, item]) => {{
      const alternates = (item.alternate_parts || []).map(part => part.c_code).join(", ") || "无";
      const procurement = item.procurement_choice || {{}};
      const order = procurement.supplier
        ? `${{procurement.supplier}} ${{procurement.order_url ? `<a target="_blank" rel="noopener" href="${{esc(procurement.order_url)}}">订货</a>` : ""}}`
        : "无";
      return `<div class="per-ref-selection"><strong>${{esc(ref)}}</strong><small>备选：${{esc(alternates)}} · 订货：${{order}}</small></div>`;
    }}).join("");
    return `<div class="per-ref-selection-list">${{records}}</div>`;
  }}
  const parts = row?.alternate_parts || [];
  const actualParts = row?.schematic_alternate_parts || [];
  const procurement = row?.procurement_choice || {{}};
  const actualProcurement = row?.schematic_procurement_choice || {{}};
  const list = parts.length ? `<div class="alternate-list">${{parts.map((item, index) => `
    <div class="alternate-item">
      <span class="alternate-rank">${{index + 1}}</span>
      <a class="alternate-code mono" target="_blank" rel="noopener" data-secondary-search="${{esc(item.c_code)}}" href="${{esc(lcscUrl(item.c_code))}}">${{esc(item.c_code)}}</a>
      <span class="alternate-summary">
        <strong>${{esc(item.model || "未记录型号")}}</strong>
        <small>${{esc([item.package, item.param].filter(Boolean).join(" · "))}}</small>
      </span>
      <span>${{alternateStockPill(item.stock)}}</span>
      <span></span><span></span><span></span>
      <span class="alternate-actions">
        <button type="button" title="提高优先级" data-action="move-alternate" data-code="${{esc(item.c_code)}}" data-direction="up" ${{index === 0 ? "disabled" : ""}}>↑</button>
        <button type="button" title="降低优先级" data-action="move-alternate" data-code="${{esc(item.c_code)}}" data-direction="down" ${{index === parts.length - 1 ? "disabled" : ""}}>↓</button>
        <button type="button" title="删除备选料" data-action="remove-alternate" data-code="${{esc(item.c_code)}}">×</button>
      </span>
    </div>`).join("")}}</div>` : '<div class="alternate-empty">暂无备选料</div>';
  const conflict = row?.alternate_conflict ? `
    <div class="alternate-conflict">
      原理图：${{esc(alternateCodes(actualParts).join(", ") || "空")}}
      <div class="alternate-conflict-actions">
        <button type="button" class="small" data-action="adopt-schematic-alternates">采用原理图</button>
        <button type="button" class="small action-sync-needed" data-action="sync-schematic">同步到原理图</button>
      </div>
    </div>` : "";
  const procurementBlock = procurement.order_url ? `
    <div class="procurement-choice" title="原理图属性：PROCUREMENT_URL / PROCUREMENT_INFO">
      <div class="procurement-choice-head">
        <span class="procurement-choice-title">订货料</span>
        <button type="button" class="small" data-action="clear-procurement">清除</button>
      </div>
      <div class="procurement-choice-summary"><strong>${{esc(procurement.manufacturer_part_number || "未记录型号")}}</strong> · ${{esc(procurement.package || "封装待确认")}}</div>
      <div class="procurement-choice-meta">${{esc([procurement.supplier, procurement.supplier_sku, procurement.batch].filter(Boolean).join(" · ") || "渠道信息待确认")}}</div>
      <div class="procurement-choice-actions">
        <a class="candidate-order-link" href="${{esc(procurement.order_url)}}" target="_blank" rel="noopener" data-secondary-url="${{esc(procurement.order_url)}}" data-secondary-label="订货料下单链接">打开订货链接</a>
        ${{procurement.related_lcsc_code ? `<span class="procurement-choice-meta">关联 ${{esc(procurement.related_lcsc_code)}}（仅参考）</span>` : ""}}
      </div>
    </div>` : '<div class="alternate-empty">暂无订货料</div>';
  const procurementConflict = row?.procurement_conflict ? `
    <div class="alternate-conflict">
      原理图订货料：${{esc(actualProcurement.manufacturer_part_number || actualProcurement.order_url || "空")}}
      <div class="alternate-conflict-actions">
        <button type="button" class="small" data-action="adopt-schematic-procurement">采用原理图</button>
        <button type="button" class="small action-sync-needed" data-action="sync-schematic">同步到原理图</button>
      </div>
    </div>` : "";
  return `<div class="alternate-cell" data-alternate-cell>${{list}}<div><button type="button" class="small action-link" data-action="open-candidates">添加备选/订货料</button></div>${{conflict}}${{procurementBlock}}${{procurementConflict}}</div>`;
}}

function rowClassFromDataset(tr) {{
  if (tr.dataset.syncNeeded === "1" || tr.dataset.missingRef === "1" || tr.dataset.syncError === "1") return "row-needs-sync";
  if (tr.dataset.codeStatus === "Rejected" || tr.dataset.reviewStatus === "Rejected" || tr.dataset.isNc === "1" || tr.dataset.hasCode !== "1") return "row-muted";
  if (tr.dataset.reviewStatus !== "Confirmed" || tr.dataset.stockQueried !== "1" || tr.dataset.stockLow === "1") return "row-warning";
  return "row-ok";
}}

function filterKeysFromDataset(tr) {{
  const keys = ["all"];
  const isNc = tr.dataset.isNc === "1";
  keys.push(tr.dataset.syncNeeded === "1" ? "needs_sync" : "sync_done");
  keys.push(tr.dataset.reviewStatus !== "Confirmed" ? "pending_review" : "review_confirmed");
  keys.push(tr.dataset.codeStatus === "ChangedToNewCode" || tr.dataset.manualCode === "1" ? "changed_code" : "code_unchanged");
  if (!isNc) {{
    if (tr.dataset.stockIssue === "1") keys.push("stock_issue");
    else if (tr.dataset.stockQueried === "1") keys.push("stock_normal");
  }}
  keys.push(tr.dataset.missingRef === "1" ? "missing_ref" : "ref_complete");
  keys.push(isNc ? "nc_device" : "non_nc");
  return keys.join(" ");
}}

function refreshRowState(tr) {{
  tr.classList.remove("row-original", "row-changed", "row-rejected", "row-needs-sync", "row-warning", "row-ok", "row-muted");
  tr.classList.add(rowClassFromDataset(tr));
  tr.dataset.filterKey = filterKeysFromDataset(tr);
  applyActiveFilter();
}}

function bomRowMatchesSearch(row, query) {{
  const terms = String(query || "").trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const values = [row.refs, row.ref, ...(row.refs_list || []), row.page_refs, row.comment,
    row.value, row.footprint, row.effective_c_code, row.accepted_c_code, row.lcsc_match_c_code,
    row.stock_c_code, row.manual_c_code, row.description, row.note,
    row.stock?.param, row.stock?.param_detail, row.stock?.model,
    ...(row.alternate_parts || []).flatMap(item => [item.c_code, item.model, item.package, item.param])];
  const text = values.filter(Boolean).join(" ").toLocaleLowerCase();
  return terms.every(term => /^[a-z]+\d+[a-z]?$/.test(term)
    ? new RegExp(`\\\\b${{term}}\\\\b`, "i").test(text) : text.includes(term));
}}

function filterViewKey() {{
  return JSON.stringify([
    bomSelect.value || "", refsInput.value.trim(),
    Array.from(activeFilters.entries()).sort(([a], [b]) => a.localeCompare(b)),
    bomSearch.value.trim().toLocaleLowerCase(),
    window.workbenchRowFilters?.key() || {{}}
  ]);
}}

function rememberFilterScroll() {{
  if (!activeFilterViewKey) return;
  try {{ sessionStorage.setItem(FILTER_SCROLL_STORAGE_PREFIX + activeFilterViewKey, String(window.scrollY)); }}
  catch (_error) {{}}
}}

function applyActiveFilter() {{
  const nextViewKey = filterViewKey();
  const changedView = Boolean(activeFilterViewKey && activeFilterViewKey !== nextViewKey);
  if (changedView) rememberFilterScroll();
  let visibleCount = 0;
  const filterContextRows = [];
  rowsEl.querySelectorAll("tr[data-filter-key]").forEach(tr => {{
    const keys = String(tr.dataset.filterKey || "all").split(/\s+/);
    const row = Object.assign({{}}, rowDataForElement(tr) || {{}}, {{
      comment: fieldValue(tr, "comment"), value: fieldValue(tr, "value"),
      footprint: fieldValue(tr, "footprint"), effective_c_code: currentLcscCode(tr)
    }});
    const contextMatch = (!activeFilters.size || Array.from(activeFilters.values()).every(filter => keys.includes(filter)))
      && bomRowMatchesSearch(row, bomSearch.value);
    if (contextMatch) filterContextRows.push(row);
    const visible = contextMatch && (window.workbenchRowFilters?.matches(row) ?? true);
    tr.classList.toggle("hidden-row", !visible);
    if (visible) visibleCount += 1;
  }});
  bomSearchCount.textContent = `${{visibleCount}} / ${{currentRows.length}} 行`;
  window.workbenchRowFilters?.refresh(filterContextRows);
  updateRowsEmptyState(visibleCount);
  if (changedView) {{
    let savedScroll = 0;
    try {{ savedScroll = Number(sessionStorage.getItem(FILTER_SCROLL_STORAGE_PREFIX + nextViewKey) || 0); }}
    catch (_error) {{}}
    window.scrollTo({{top: Number.isFinite(savedScroll) ? savedScroll : 0}});
  }}
  activeFilterViewKey = nextViewKey;
  updateSyncSelection();
  updateWorkbenchSummary();
}}

function updateRowsEmptyState(visibleCount = visibleSyncRows().length) {{
  const panel = document.getElementById("rowsEmptyState");
  panel.hidden = rowsLoading || loadedRowsBom === null || loadedRowsBom !== (bomSelect.value || "") || visibleCount > 0;
  const filtered = activeFilters.size > 0 || Boolean(bomSearch.value.trim()) || Boolean(window.workbenchRowFilters?.active());
  document.getElementById("rowsEmptyMessage").textContent = currentRows.length
    ? "没有匹配当前筛选的器件" : "当前数据源没有器件";
  document.getElementById("resetRowFilters").hidden = !filtered;
}}

document.getElementById("resetRowFilters").addEventListener("click", () => {{
  bomSearch.value = "";
  activeFilters.clear();
  window.workbenchRowFilters?.reset();
  updateFilterButtons();
  applyActiveFilter();
  bomSearch.focus();
}});

function visibleSyncRows() {{
  return Array.from(rowsEl.querySelectorAll("tr[data-ref]")).filter(tr => !tr.classList.contains("hidden-row"));
}}

function syncRowRefs(tr) {{
  return String(tr.dataset.refs || tr.dataset.ref || "").split(",").map(ref => ref.trim().toUpperCase()).filter(Boolean);
}}

function selectedSyncRefs() {{
  return [...new Set(visibleSyncRows().flatMap(syncRowRefs).filter(ref => selectedRowRefs.has(ref)))];
}}

function updateSyncSelection() {{
  const visible = visibleSyncRows();
  let checked = 0;
  let fullyChecked = 0;
  visible.forEach(tr => {{
    const refs = syncRowRefs(tr);
    const selected = refs.filter(ref => selectedRowRefs.has(ref)).length;
    const input = tr.querySelector("[data-select-row]");
    input.checked = selected === refs.length && selected > 0;
    input.indeterminate = selected > 0 && selected < refs.length;
    if (input.checked) fullyChecked += 1;
    if (selected) checked += 1;
  }});
  selectVisibleRows.checked = visible.length > 0 && fullyChecked === visible.length;
  selectVisibleRows.indeterminate = checked > 0 && fullyChecked < visible.length;
  syncSelectedButton.querySelector("[data-button-count]").textContent = checked;
  syncSelectedButton.setAttribute("aria-label", `同步所选行（${{checked}}）`);
  syncSelectedButton.disabled = bulkSyncRunning || rowsLoading || checked === 0;
}}

selectVisibleRows.addEventListener("change", () => {{
  visibleSyncRows().forEach(tr => syncRowRefs(tr).forEach(ref => {{
    if (selectVisibleRows.checked) selectedRowRefs.add(ref);
    else selectedRowRefs.delete(ref);
  }}));
  updateSyncSelection();
}});
rowsEl.addEventListener("change", event => {{
  if (!event.target.matches("[data-select-row]")) return;
  syncRowRefs(event.target.closest("tr")).forEach(ref => {{
    if (event.target.checked) selectedRowRefs.add(ref);
    else selectedRowRefs.delete(ref);
  }});
  updateSyncSelection();
}});

function setStockCells(tr, stock) {{
  if (normalizeCCode(stock.c_code) !== normalizeCCode(tr.dataset.stockCode)) return;
  const currentRow = rowDataForElement(tr);
  if (currentRow && !currentRow.selection_codes_mixed && stockCode(currentRow) === normalizeCCode(stock.c_code)) currentRow.stock = stock;
  tr.querySelector("[data-stock-cell]").innerHTML = renderStockCell(stock);
  tr.dataset.stockQueried = stockAlreadyQueried(stock) ? "1" : "0";
  tr.dataset.stockLow = stockIsLow(stock) ? "1" : "0";
  tr.dataset.stockIssue = stockIsIssue(stock) ? "1" : "0";
  tr.querySelector("[data-stock-order]").textContent = `${{stock.moq || ""}} / ${{stock.mpq || ""}}`;
  tr.querySelector("[data-stock-price]").textContent = stock.price_ladder || stock.price || "";
  const paramCell = tr.querySelector("[data-stock-param]");
  const material = stockWithSavedDescription(rowDataForElement(tr), stock);
  paramCell.dataset.stockParamText = stockParamText(material);
  paramCell.innerHTML = renderStockParam(material);
  refreshRowState(tr);
  window.queueLocalInventoryRefresh?.();
  window.refreshSpecificationWarnings?.(true);
}}

function setCodeState(tr, status, text = "") {{
  tr.dataset.codeStatus = status;
  const statusInput = tr.querySelector("input[type='hidden'][data-field='status']");
  if (statusInput) statusInput.value = statusInput.dataset.savedValue = status;
  tr.dataset.hasCode = currentLcscCode(tr) ? "1" : "0";
  refreshRowState(tr);
  const stateEl = tr.querySelector("[data-code-state]");
  if (stateEl) {{
    stateEl.className = `status-pill ${{statusClass(status)}}`;
    stateEl.textContent = text || statusText(status);
  }}
  const link = tr.querySelector("[data-primary-code-link]");
  if (link) {{
    const code = currentLcscCode(tr);
    link.hidden = !code;
    link.href = lcscUrl(code);
    link.dataset.secondarySearch = code;
  }}
}}

function rowPayload(tr, decision, acceptedCode) {{
  const replacementInput = tr.querySelector("[data-field='replacement_c_code']");
  const replacement = replacementInput ? normalizeCCode(replacementInput.value) : "";
  const stockCodeValue = normalizeCCode(acceptedCode ?? replacement);
  return {{
    ref: tr.dataset.ref,
    refs: tr.dataset.refs,
    decision,
    accepted_c_code: acceptedCode ?? replacement,
    manual_c_code: replacement,
    stock_c_code: stockCodeValue,
    note: tr.querySelector("[data-field='note']").value.trim(),
    status: tr.querySelector("[data-field='status']").value.trim(),
    review_status: tr.querySelector("[data-field='review_status']")?.value || ""
  }};
}}

function footprintParts(value) {{
  const full = String(value || "").trim(), separator = full.indexOf(":");
  return separator < 0 ? {{library: "", name: full}}
    : {{library: full.slice(0, separator), name: full.slice(separator + 1)}};
}}

function footprintFieldValue(input, raw = input?.value || "") {{
  const value = raw.trim(), library = input?.dataset.footprintLibrary || "";
  return value && !value.includes(":") && library ? `${{library}}:${{value}}` : value;
}}

function footprintEditor(row) {{
  const parts = footprintParts(row.footprint);
  return `<span class="footprint-library" data-footprint-library-label hidden></span><textarea class="edit field-edit mono" data-field="footprint" data-footprint-library="${{esc(parts.library)}}" title="完整封装: ${{esc(row.footprint || "")}}；可输入 库名:封装名 切换库">${{esc(parts.name)}}</textarea>`;
}}

function refreshFootprintLibraries() {{
  const inputs = Array.from(rowsEl.querySelectorAll("[data-field='footprint']"));
  const libraries = new Set(inputs.map(input => footprintParts(footprintFieldValue(input)).library));
  const common = libraries.size === 1 ? [...libraries][0] : "";
  const summary = document.getElementById("footprintLibrarySummary");
  if (summary) summary.textContent = libraries.size > 1 ? "封装库：多库（见各行）" : common ? `封装库：${{common}}` : "";
  for (const input of inputs) {{
    const label = input.parentElement.querySelector("[data-footprint-library-label]");
    if (!label) continue;
    label.hidden = libraries.size <= 1;
    label.textContent = footprintParts(footprintFieldValue(input)).library || "未指定库";
  }}
}}

function fieldValue(tr, field) {{
  const input = tr.querySelector(`[data-field="${{field}}"]`);
  return input ? (field === "footprint" ? footprintFieldValue(input) : input.value.trim()) : "";
}}

function currentLcscCode(tr) {{
  const inputCode = normalizeCCode(tr.querySelector("[data-field='replacement_c_code']")?.value || "");
  const effectiveCode = normalizeCCode(tr.querySelector("[data-effective-code]")?.textContent || "");
  const originalCodeValue = normalizeCCode(tr.dataset.originalCode || "");
  return inputCode || effectiveCode || originalCodeValue;
}}

function currentAlternateFields(tr) {{
  const row = rowDataForElement(tr) || {{}};
  return {{
    lcsc_alt: String(row.lcsc_alt || alternateCodes(row.alternate_parts).join(",")),
    lcsc_alt_info: String(row.lcsc_alt_info || "")
  }};
}}

function currentProcurementFields(tr) {{
  const row = rowDataForElement(tr) || {{}};
  return {{
    procurement_url: String(row.procurement_url || ""),
    procurement_info: String(row.procurement_info || "")
  }};
}}

function lcscUrl(code) {{
  const normalized = normalizeCCode(code);
  return normalized ? `https://so.szlcsc.com/global.html?k=${{encodeURIComponent(normalized)}}` : "";
}}

function schematicDescription(tr) {{
  if (rowDataForElement(tr)?.selection_mixed) return "";
  const cell = tr.querySelector("[data-stock-param]");
  return String(cell?.dataset.stockParamText || cell?.textContent || "").trim();
}}

function fieldPayload(tr) {{
  const field_overrides = {{
    comment: fieldValue(tr, "comment"),
    value: fieldValue(tr, "value"),
    footprint: fieldValue(tr, "footprint")
  }};
  const lcsc_c_code = currentLcscCode(tr);
  if (lcsc_c_code) {{
    field_overrides.lcsc_c_code = lcsc_c_code;
    field_overrides.lcsc_url = lcscUrl(lcsc_c_code);
  }}
  const description = schematicDescription(tr);
  if (description) field_overrides.description = description;
  Object.assign(field_overrides, currentAlternateFields(tr));
  Object.assign(field_overrides, currentProcurementFields(tr));
  const row = rowDataForElement(tr) || {{}};
  const per_ref_fields = {{}};
  for (const [ref, fields] of Object.entries(row.per_ref_sync_fields || {{}})) {{
    per_ref_fields[ref] = row.selection_mixed
      ? {{...fields, comment: field_overrides.comment, value: field_overrides.value, footprint: field_overrides.footprint}}
      : {{...fields, ...field_overrides, ...(fields.description ? {{description: fields.description}} : {{}})}};
  }}
  return {{
    ref: tr.dataset.ref,
    refs: tr.dataset.refs,
    field_overrides,
    per_ref_fields,
    note: tr.querySelector("[data-field='note']").value.trim()
  }};
}}

function fieldDiff(row, field) {{
  const diff = (row.field_diffs || {{}})[field];
  if (!diff) return "";
  let oldText = String(diff.old ?? "");
  let newText = String(diff.new ?? "");
  if (oldText === newText) return "";
  if (field === "footprint" && footprintParts(oldText).library === footprintParts(newText).library) {{
    oldText = footprintParts(oldText).name;
    newText = footprintParts(newText).name;
  }}
  const oldLabel = oldText || "(空)";
  const newLabel = newText || "(空)";
  return `<div class="field-diff" title="未同步原理图">${{esc(oldLabel)}} -&gt; ${{esc(newLabel)}}</div>`;
}}

function schematicSyncSummary(row) {{
  const summary = String(row.schematic_sync_summary || "").trim();
  if (!summary) return "";
  const syncedAt = String(row.schematic_synced_at || "").trim();
  const title = syncedAt ? `上次同步 ${{syncedAt}}` : "上次同步原理图";
  const count = summary.split(/;\s*/).filter(Boolean).length;
  const countText = count > 1 ? ` · ${{count}} 项` : "";
  return `<details class="sync-summary"><summary>${{esc(title)}}${{countText}}</summary><div class="sync-summary-detail">${{esc(summary)}}</div></details>`;
}}

const SYNC_FIELD_LABELS = {{
  comment: "Comment", value: "Value", footprint: "Footprint", description: "物料参数",
  lcsc_c_code: "C编码", lcsc_url: "C编码链接", lcsc_alt: "备选料", lcsc_alt_info: "备选料参数",
  procurement_url: "订货链接", procurement_info: "订货信息"
}};

function schematicSyncMarker(row, syncNeeded) {{
  if (!syncNeeded) return "";
  const changes = new Map();
  const perRef = Object.values(row.field_diffs_by_ref || {{}});
  for (const fields of perRef.length ? perRef : [row.field_diffs || {{}}]) {{
    for (const [field, values] of Object.entries(fields || {{}})) {{
      if (field === "lcsc_url" || field === "procurement_url") continue;
      if (String(values.old || "") === String(values.new || "")) continue;
      if (field === "description") {{
        changes.set(field, "物料参数更新");
        continue;
      }}
      const key = JSON.stringify([field, values.old, values.new]);
      changes.set(key, `${{SYNC_FIELD_LABELS[field] || field}}: ${{values.old || "(空)"}} → ${{values.new || "(空)"}}`);
    }}
  }}
  const details = [...changes.values()].map(change => `<div>${{esc(change)}}</div>`);
  const missing = String(row.missing_schematic_refs || "").split(",").map(value => value.trim()).filter(Boolean);
  if (missing.length) details.push(`<div>${{missing.length}} 个器件在原理图中缺失，未写入</div>`);
  return `<div class="schematic-sync-marker" data-sync-marker><strong>有改动，请同步原理图</strong><div class="sync-row-diff">${{details.join("") || "物料信息已更新"}}</div></div>`;
}}

function schematicFieldReconcileNotice(row) {{
  const conflicts = row.field_conflicts || {{}};
  const conflictFields = Array.from(new Set(Object.values(conflicts).flatMap(item => Object.keys(item || {{}}))));
  if (conflictFields.length) {{
    const refs = String(row.field_conflict_refs || row.refs || "");
    return `<div class="schematic-field-notice conflict">网页与原理图均有修改：${{esc(conflictFields.join("、"))}}<br><button type="button" class="small" data-action="adopt-schematic-fields" data-fields="${{esc(conflictFields.join(","))}}" title="放弃这些网页待同步值，采用 KiCad 当前值">采用原理图</button><span class="mono"> ${{esc(refs)}}</span></div>`;
  }}
  const updates = row.external_schematic_updates || {{}};
  const updateFields = Array.from(new Set(Object.values(updates).flatMap(item => Object.keys(item || {{}}))));
  if (!updateFields.length) return "";
  return `<div class="schematic-field-notice">已从原理图更新：${{esc(updateFields.join("、"))}}</div>`;
}}

function rowAnchor(tr) {{
  return {{refs: tr?.dataset.refs || tr?.dataset.ref || "", scrollY: window.scrollY, scrollX: window.scrollX, top: tr?.getBoundingClientRect().top}};
}}

function restoreRowAnchor(anchor) {{
  if (!anchor || !anchor.refs) return;
  const wanted = String(anchor.refs).split(",").map(item => item.trim()).filter(Boolean);
  const target = Array.from(document.querySelectorAll("tr[data-ref]")).find(tr => {{
    if (tr.dataset.refs === anchor.refs || tr.dataset.ref === anchor.refs) return true;
    const refs = String(tr.dataset.refs || tr.dataset.ref || "").split(",").map(item => item.trim());
    return wanted.some(ref => refs.includes(ref));
  }});
  if (target) {{
    window.scrollTo({{top: window.scrollY + target.getBoundingClientRect().top - (anchor.top ?? target.getBoundingClientRect().top), left: anchor.scrollX ?? window.scrollX}});
    target.classList.add("changed");
    window.setTimeout(() => target.classList.remove("changed"), 1200);
  }} else if (Number.isFinite(anchor.scrollY)) {{
    window.scrollTo({{top: anchor.scrollY}});
  }}
}}

function updateSavedFieldDiffs(row, fields) {{
  const byRef = row.field_diffs_by_ref ||= {{}};
  const diffs = row.field_diffs ||= {{}};
  for (const field of fields) {{
    const changed = [], oldValues = [];
    for (const ref of row.refs_list || String(row.refs).split(",")) {{
      const baseline = row.schematic_fields_by_ref?.[ref];
      if (!baseline) continue;
      const oldValue = String(baseline[field] || "").trim();
      const newValue = String(row.per_ref_sync_fields?.[ref]?.[field] ?? row[field] ?? "").trim();
      if (oldValue === newValue) {{
        if (byRef[ref]) delete byRef[ref][field];
      }} else {{
        (byRef[ref] ||= {{}})[field] = {{old: oldValue, new: newValue}};
        changed.push(ref);
        if (!oldValues.includes(oldValue)) oldValues.push(oldValue);
      }}
    }}
    if (changed.length) diffs[field] = {{old: oldValues.join("; "), new: row.per_ref_sync_fields?.[changed[0]]?.[field] ?? row[field] ?? "", refs: changed.join(",")}};
    else delete diffs[field];
  }}
  const changedRefs = Object.keys(byRef).filter(ref => Object.keys(byRef[ref]).length);
  row.field_changed_refs = [...new Set([...changedRefs, ...String(row.missing_schematic_refs || "").split(",").filter(Boolean)])].join(",");
  row.field_edit_status = row.field_changed_refs ? "NeedsSchematicUpdate" : "";
}}

function markRowNeedsSchematicSync(tr, refsText = "") {{
  if (!tr) return;
  const row = rowDataForElement(tr);
  const needed = row?.schematic_fields_by_ref ? !!row.field_changed_refs : true;
  tr.dataset.syncNeeded = needed ? "1" : "0";
  refreshRowState(tr);
  const button = tr.querySelector("[data-action='sync-schematic']");
  if (button) {{
    button.classList.remove("action-muted", "action-sync", "action-sync-needed");
    button.classList.add(needed ? "action-sync-needed" : "action-muted");
    button.title = needed ? "有改动，请同步原理图" : "当前字段和原理图一致";
  }}
  tr.querySelector("[data-sync-marker]")?.remove();
  if (needed) {{
    const marker = schematicSyncMarker(row || {{field_changed_refs: refsText || tr.dataset.refs || tr.dataset.ref}}, true);
    if (button) button.insertAdjacentHTML("afterend", marker);
  }}
  for (const field of ["comment", "value", "footprint"]) {{
    const cell = tr.querySelector(`[data-col='${{field}}']`);
    cell?.querySelector(".field-diff")?.remove();
    if (cell && row) cell.insertAdjacentHTML("beforeend", fieldDiff(row, field));
  }}
}}

async function saveRow(tr, decision, acceptedCode) {{
  const payload = rowPayload(tr, decision, acceptedCode);
  const res = await fetch("/api/override", {{ method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(payload) }});
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "保存失败");
  tr.classList.remove("changed");
  setState(`已保存 ${{payload.refs || payload.ref}}`, "saved");
}}

async function saveFieldEdits(tr) {{
  tr.querySelectorAll(".field-edit").forEach(queueRowInput);
  await flushRowEdits();
  const anchor = rowAnchor(tr);
  const version = rowsLoadVersion, bom = bomSelect.value || "";
  const res = await fetch(`/api/rows?refs=${{encodeURIComponent(refsInput.value.trim())}}&bom=${{encodeURIComponent(bom)}}&stock=0`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "加载字段失败");
  if (version !== rowsLoadVersion || bom !== (bomSelect.value || "") || rowEditQueue.pending) return;
  const signature = rows => rows.map(row => row.refs).sort().join("|");
  if (signature(data.rows) !== signature(currentRows)) {{
    renderRows(data.rows);
    restoreRowAnchor(anchor);
  }}
  setState(`字段已保存 ${{anchor.refs}}`, "saved");
}}

let rowsNeedRegroup = false;
let rowGroupRefreshAnchor = null;

async function regroupRowsAfterAutosave() {{
  if (!rowsNeedRegroup) return;
  if (rowEditQueue.pending) return;
  const anchor = rowGroupRefreshAnchor;
  rowsNeedRegroup = false;
  rowGroupRefreshAnchor = null;
  const version = rowsLoadVersion, bom = bomSelect.value || "";
  try {{
    const res = await fetch(`/api/rows?refs=${{encodeURIComponent(refsInput.value.trim())}}&bom=${{encodeURIComponent(bom)}}&stock=0`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "重新整理分组失败");
    if (version !== rowsLoadVersion || bom !== (bomSelect.value || "") || rowEditQueue.pending) {{
      if (rowEditQueue.pending) {{ rowsNeedRegroup = true; rowGroupRefreshAnchor ||= anchor; }}
      return;
    }}
    const signature = rows => rows.map(row => row.refs).sort().join("|");
    if (signature(data.rows) !== signature(currentRows)) {{
      renderRows(data.rows);
      restoreRowAnchor(anchor);
    }}
  }} catch (error) {{
    setState(`自动保存成功，但刷新分组失败：${{error.message || String(error)}}`, "error");
  }}
}}

async function adoptSchematicFields(tr, button) {{
  const anchor = rowAnchor(tr);
  const fields = String(button?.dataset.fields || "").split(",").map(item => item.trim()).filter(Boolean);
  const payload = {{ref: tr.dataset.ref, refs: tr.dataset.refs, fields}};
  const res = await fetch("/api/adopt_schematic_fields", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(payload)}});
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || "采用原理图字段失败");
  setState(`已采用原理图字段：${{fields.join("、")}}`, "saved");
  await loadRows(anchor);
}}

async function undoWorkbenchChanges(tr, button) {{
  const refs = tr.dataset.refs || tr.dataset.ref;
  const anchor = rowAnchor(tr);
  button.disabled = true;
  try {{
    await flushRowEdits();
    const request = async payload => {{
      const response = await fetch("/api/undo_workbench", {{method: "POST",
        headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{refs, ...payload}})}});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "撤回失败");
      return data;
    }};
    const preview = await request({{}});
    if (!preview.available) {{ setState("该行没有可撤回的未同步修改", "saved"); return; }}
    const label = preview.mode === "snapshot" ? "恢复为工作台本轮修改前的记录"
      : "部分旧记录没有修改前快照，将按原理图当前已保存的数据恢复";
    const labels = {{...SYNC_FIELD_LABELS, review_status: "确认状态", note: "备注",
      alternate_parts: "备选料", procurement_choice: "订货料"}};
    const lines = [...new Set(preview.changes.map(change => {{
      if (["description", "alternate_parts", "procurement_choice"].includes(change.field)) return `${{labels[change.field] || change.field}}恢复`;
      return `${{labels[change.field] || change.field}}: ${{change.old || "(空)"}} → ${{change.new || "(空)"}}`;
    }}))];
    if (!window.confirm(`撤回 ${{refs}} 的工作台修改？\\n${{label}}。\\n\\n${{lines.join("\\n")}}\\n\\n不会修改原理图、PCB 或已导入的封装库。`)) return;
    await request({{confirm: true, revision: preview.revision}});
    await loadRows(anchor);
    setState("已撤回本行工作台修改；原理图和 PCB 未改动", "saved");
  }} finally {{ button.disabled = false; }}
}}

async function saveReviewStatus(tr) {{
  const payload = {{
    ref: tr.dataset.ref,
    refs: tr.dataset.refs,
    review_status: tr.querySelector("[data-field='review_status']")?.value || "",
    note: tr.querySelector("[data-field='note']").value.trim()
  }};
  const res = await fetch("/api/override", {{ method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(payload) }});
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "保存确认状态失败");
  const pill = tr.querySelector("[data-review-state]");
  if (pill) {{
    pill.className = `review-pill ${{reviewStatusClass(payload.review_status)}}`;
    pill.textContent = reviewStatusText(payload.review_status);
    pill.title = "";
  }}
  const confirmedAt = String(data.override?.review_confirmed_at || "");
  const timeLabel = tr.querySelector("[data-review-confirmed-at]");
  if (timeLabel) {{
    timeLabel.textContent = confirmedAt
      ? `${{payload.review_status === "Confirmed" ? "确认于" : "上次确认于"}} ${{formatReviewTime(confirmedAt)}}`
      : "";
    timeLabel.title = confirmedAt;
  }}
  setState(`确认状态已保存 ${{payload.refs || payload.ref}}`, "saved");
  tr.dataset.reviewStatus = payload.review_status || "Pending";
  const row = rowDataForElement(tr);
  if (row) {{
    row.review_status = tr.dataset.reviewStatus;
    row.review_status_mixed = false;
    row.override = {{...row.override, review_status: row.review_status}};
    for (const selection of Object.values(row.per_ref_selection || {{}})) {{
      selection.review_status = row.review_status;
      if (confirmedAt) selection.review_confirmed_at = confirmedAt;
    }}
  }}
  refreshRowState(tr);
}}

async function postSchematicSync(path, payload) {{
  let res;
  try {{
    res = await fetch(path, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(payload)}});
  }} catch (error) {{
    throw new Error(`同步服务连接中断（${{window.location.origin}}）：${{error.message}}。结果尚不确定，请不要重复点击；先刷新核对状态或重新打开插件。`);
  }}
  let data;
  try {{
    data = await res.json();
  }} catch (error) {{
    throw new Error(`同步服务响应不完整（HTTP ${{res.status}}）。请不要重复点击，先刷新核对状态。`);
  }}
  if (!res.ok || !data.ok) {{
    const attemptText = data.sync_attempt_id ? `；日志编号 ${{data.sync_attempt_id}}` : "";
    throw new Error(`${{data.error || "同步原理图失败"}}${{attemptText}}`);
  }}
  return data;
}}

async function syncSchematicWithProgress(tr) {{
  if (bulkSyncRunning) return;
  if (rowsLoading || loadedRowsBom === null || loadedRowsBom !== (bomSelect.value || "")) {{
    setState("请先加载当前 BOM 后再同步", "error");
    return;
  }}
  const selectedRefs = syncRowRefs(tr);
  if (!selectedRefs.length) {{
    setState("当前行没有有效位号，无法同步", "error");
    return;
  }}
  const scopeLabel = `${{tr.dataset.refs || tr.dataset.ref}}（${{selectedRefs.length}} 个位号）`;
  if (!window.confirm(`将同步当前行 ${{scopeLabel}} 到原理图。已一致则直接完成；需要写入时自动关闭已保存的原理图，写入并校验后重新打开。若有未保存改动且自动保存不可用，会停止并提示先手动保存。是否继续？`)) return;
  bulkSyncRunning = true;
  syncAllButton.disabled = true;
  bomSelect.disabled = true;
  updateSyncSelection();
  const syncButton = tr.querySelector("[data-action='sync-schematic']");
  if (syncButton) syncButton.disabled = true;
  const stopProgress = startSyncProgressTicker([
    {{percent: 8, text: "检查已保存字段及编辑器保存状态；无需手动关闭", delay: 420}},
    {{percent: 24, text: "写入原理图文件", delay: 620}},
    {{percent: 54, text: "重新读取原理图校验字段", delay: 720}},
    {{percent: 76, text: "必要时重写被覆盖字段", delay: 720}},
    {{percent: 88, text: "通知 KiCad 并刷新网页状态", delay: 720}}
  ]);
  setState(`正在同步原理图 ${{scopeLabel}}...`, "status");
  let data;
  try {{
    data = await postSchematicSync("/api/bulk_sync_schematic", {{bom: loadedRowsBom, notify: true, selected_refs: selectedRefs}});
  }} finally {{
    stopProgress();
    if (syncButton) syncButton.disabled = false;
    bulkSyncRunning = false;
    bomSelect.disabled = false;
    syncAllButton.disabled = rowsLoading || loadedRowsBom !== (bomSelect.value || "");
    updateSyncSelection();
  }}
  if (!data.ok) {{
    const attemptText = data.sync_attempt_id ? `；日志编号 ${{data.sync_attempt_id}}` : "";
    throw new Error(`${{data.error || "同步原理图失败"}}${{attemptText}}`);
  }}
  const changedRefs = data.updated_refs || [];
  const missingRefs = data.missing_refs || [];
  const notify = data.kicad_notify || {{}};
  const verify = data.schematic_verify || {{}};
  if (verify.ok === false) {{
    const mismatchRefs = Object.keys(verify.mismatches || {{}});
    setSyncProgress(100, `校验失败：${{mismatchRefs.join(",") || "字段未落盘"}}`, true);
    throw new Error(`已写入但校验未通过，请勿重复同步；刷新核对字段并查看后台日志：${{mismatchRefs.join(",") || "字段未落盘"}}`);
  }}
  const verifyText = verify.attempts > 1 ? `；校验通过（${{verify.attempts}} 轮）` : "；校验通过";
  const showText = data.already_synced ? "；已保存字段一致，未操作 KiCad" : (notify.ok ? "；已通知 KiCad" : "；字段已保存，自动定位未完成，可手动打开原理图查看");
  const missingText = missingRefs.length ? `；未找到 ${{missingRefs.join(",")}}` : "";
  const message = changedRefs.length
    ? `已同步原理图 ${{changedRefs.join(",")}}${{showText}}${{verifyText}}${{missingText}}`
    : `当前行无需写入字段${{showText}}${{verifyText}}${{missingText}}`;
  setSyncProgress(100, message, true);
  setState(message, changedRefs.length ? "saved" : "status");
  await loadRows();
  clearSyncProgress();
}}

async function syncAllSchematicWithProgress(selectedOnly = false) {{
  if (bulkSyncRunning) return;
  const syncBom = loadedRowsBom;
  if (rowsLoading || syncBom === null || syncBom !== (bomSelect.value || "")) {{
    setState("请先加载当前 BOM 后再同步", "error");
    return;
  }}
  const selectedRefs = selectedOnly ? selectedSyncRefs() : null;
  const trs = Array.from(document.querySelectorAll("tr[data-ref]"));
  if (!trs.length || (selectedOnly && !selectedRefs.length)) {{
    setState("当前表格没有可同步的行", "error");
    return;
  }}
  const selectedCount = visibleSyncRows().filter(tr => syncRowRefs(tr).some(ref => selectedRowRefs.has(ref))).length;
  const scopeLabel = selectedOnly ? `所选 ${{selectedCount}} 行（${{selectedRefs.length}} 个位号）` : "全部 BOM";
  if (!window.confirm(`将同步${{scopeLabel}}到原理图。已一致则直接完成；需要写入时自动关闭已保存的原理图，写入并校验后重新打开。若有未保存改动且自动保存不可用，会停止并提示先手动保存。是否继续？`)) return;
  bulkSyncRunning = true;
  syncAllButton.disabled = true;
  bomSelect.disabled = true;
  updateSyncSelection();
  const stopProgress = startSyncProgressTicker([
    {{percent: 6, text: "准备批量同步", delay: 450}},
    {{percent: 18, text: "读取当前 BOM 合并状态", delay: 550}},
    {{percent: 36, text: "检查已保存字段及编辑器保存状态；无需手动关闭", delay: 650}},
    {{percent: 58, text: "批量写入所有原理图页面", delay: 900}},
    {{percent: 78, text: "校验写入字段", delay: 900}},
    {{percent: 90, text: "通知 KiCad 并刷新网页", delay: 650}}
  ]);
  try {{
    setState("正在批量同步原理图...", "status");
    const data = await postSchematicSync("/api/bulk_sync_schematic",
      Object.assign({{bom: syncBom, notify: true}}, selectedOnly ? {{selected_refs: selectedRefs}} : {{}}));
    const verify = data.schematic_verify || {{}};
    if (verify.ok === false) {{
      const mismatchRefs = Object.keys(verify.mismatches || {{}});
      setSyncProgress(100, `校验失败：${{mismatchRefs.join(",") || "字段未落盘"}}`, true);
      throw new Error(`批量写入后校验失败：${{mismatchRefs.join(",") || "字段未落盘"}}`);
    }}
    const changedCount = (data.updated_refs || []).length;
    const missingRefs = data.missing_refs || [];
    const missingText = missingRefs.length ? `；未找到 ${{missingRefs.join(",")}}` : "";
    const verifyText = verify.skipped ? "；无可写位号，未写入" : (verify.attempts > 1 ? `；校验通过（${{verify.attempts}} 轮）` : "；校验通过");
    const showText = data.already_synced ? "；已保存字段一致，未操作 KiCad" : (data.kicad_notify && data.kicad_notify.ok === false ? "；字段已保存，自动定位未完成，可手动打开原理图查看" : "");
    const message = `同步${{scopeLabel}}完成：${{changedCount}} 个位号有改动${{verifyText}}${{missingText}}${{showText}}`;
    setSyncProgress(100, message, true);
    setState(message, missingRefs.length ? "error" : "saved");
    if (selectedOnly) selectedRefs.filter(ref => !missingRefs.includes(ref)).forEach(ref => selectedRowRefs.delete(ref));
    await loadRows();
  }} finally {{
    stopProgress();
    bulkSyncRunning = false;
    syncAllButton.disabled = rowsLoading || loadedRowsBom !== (bomSelect.value || "");
    bomSelect.disabled = false;
    updateSyncSelection();
    clearSyncProgress();
  }}
}}

async function syncSchematic(tr) {{
  return syncSchematicWithProgress(tr);
}}

async function syncAllSchematic() {{
  return syncAllSchematicWithProgress();
}}

async function saveStockCode(tr, code) {{
  const normalized = normalizeCCode(code);
  const res = await fetch("/api/override", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{
      ref: tr.dataset.ref,
      refs: tr.dataset.refs,
      stock_c_code: normalized
    }})
  }});
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "保存查询料号失败");
  tr.dataset.stockCode = normalized;
}}

async function exportChanges() {{
  const res = await fetch("/api/export_changes", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{refs: refsInput.value.trim(), bom: bomSelect.value || ""}})
  }});
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "导出改动表失败");
  setState(`已导出改动表：${{data.count}} 条，${{data.path}}`, "saved");
}}

async function exportPumpTable() {{
  const res = await fetch("/api/export_pump_table", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{
      refs: refsInput.value.trim(),
      bom: bomSelect.value || "",
      export_filter: document.getElementById("pumpExportMode").value || "current_filter",
      active_filters: Array.from(activeFilters.values())
    }})
  }});
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "导出泵表失败");
  setState(`已导出泵表：${{data.count}} 条，${{data.path}}`, "saved");
}}

async function refreshRowStock(tr, code, run = beginStockQueryRun()) {{
  const normalized = normalizeCCode(code);
  tr.dataset.stockCode = normalized;
  if (!normalized) {{
    setStockCells(tr, {{status: "请提供料号", c_code: "", error: "请提供料号"}});
    if (isActiveStockRun(run)) stockQueryController = null;
    return;
  }}
  setStockCells(tr, {{status: "查询中", c_code: normalized}});
  try {{
    const res = await fetch(`/api/stock?code=${{encodeURIComponent(normalized)}}`, {{signal: run.signal}});
    const data = await res.json();
    if (!isActiveStockRun(run) || !document.body.contains(tr)) return;
    setStockCells(tr, data.stock || {{status: "查询失败", c_code: normalized}});
  }} catch (err) {{
    if (err.name === "AbortError") return;
    if (isActiveStockRun(run) && document.body.contains(tr)) {{
      setStockCells(tr, {{status: "查询失败", c_code: normalized, error: err.message || String(err)}});
    }}
    throw err;
  }}
}}

async function queryRowStock(tr) {{
  const row = rowDataForElement(tr);
  if (row?.selection_codes_mixed && Array.isArray(row.stock?.entries)) {{
    const run = beginStockQueryRun();
    const codes = [...new Set(row.stock.entries.map(item => normalizeCCode(item.c_code)).filter(Boolean))];
    await Promise.all(codes.map(async code => {{
      const res = await fetch(`/api/stock?code=${{encodeURIComponent(code)}}`, {{signal: run.signal}});
      const data = await res.json();
      const stock = data.stock || {{status: "查询失败", c_code: code}};
      for (const item of row.stock.entries) if (normalizeCCode(item.c_code) === code) item.stock = stock;
    }}));
    if (isActiveStockRun(run) && document.body.contains(tr)) {{
      tr.querySelector("[data-stock-cell]").innerHTML = renderStockCell(row.stock);
      refreshRowState(tr);
    }}
    return;
  }}
  const run = beginStockQueryRun();
  const inputCode = normalizeCCode(tr.querySelector("[data-field='replacement_c_code']").value);
  const effectiveCode = normalizeCCode(tr.querySelector("[data-effective-code]").textContent);
  const originalCodeValue = normalizeCCode(tr.dataset.originalCode);
  const code = inputCode || effectiveCode || originalCodeValue;
  if (inputCode) await saveStockCode(tr, inputCode);
  await refreshRowStock(tr, code, run);
}}

async function acceptReplacement(tr) {{
  const input = tr.querySelector("[data-field='replacement_c_code']");
  const code = normalizeCCode(input.value);
  if (!code) {{
    setState("请输入有效的立创 C 编码，例如 C49678", "error");
    input.focus();
    return;
  }}
  input.value = code;
  tr.querySelector("[data-field='status']").value = "ChangedToNewCode";
  await saveRow(tr, "AcceptManual", code);
  tr.querySelector("[data-effective-code]").textContent = code;
  setCodeState(tr, "ChangedToNewCode", `已改动为新编码：${{code}}`);
  markRowNeedsSchematicSync(tr, tr.dataset.refs || tr.dataset.ref);
  await refreshRowStock(tr, code);
  await loadRows(rowAnchor(tr));
}}

async function applyStatus(tr) {{
  const status = tr.querySelector("[data-field='status']").value;
  const input = tr.querySelector("[data-field='replacement_c_code']");
  if (status === "ChangedToNewCode") {{
    input.focus();
    if (normalizeCCode(input.value)) await acceptReplacement(tr);
    return;
  }}
  if (status === "Rejected") {{
    input.value = "";
    await saveRow(tr, "Reject", "");
    await saveStockCode(tr, "");
    tr.querySelector("[data-effective-code]").textContent = "";
    setCodeState(tr, status);
    markRowNeedsSchematicSync(tr, tr.dataset.refs || tr.dataset.ref);
    await refreshRowStock(tr, "");
    await loadRows(rowAnchor(tr));
    return;
  }}
  input.value = "";
  const code = tr.dataset.originalCode || "";
  await saveRow(tr, code ? "AcceptManual" : "ClearSelection", code);
  await saveStockCode(tr, code);
  tr.querySelector("[data-effective-code]").textContent = code;
  setCodeState(tr, status);
  markRowNeedsSchematicSync(tr, tr.dataset.refs || tr.dataset.ref);
  await refreshRowStock(tr, code);
  await loadRows(rowAnchor(tr));
}}

function renderRows(rows) {{
  currentRows = rows;
  const availableRefs = new Set(rows.flatMap(row => row.refs_list || [row.ref]));
  for (const ref of selectedRowRefs) if (!availableRefs.has(ref)) selectedRowRefs.delete(ref);
  refsInput.value = rows.flatMap(row => row.refs_list || [row.ref]).join(",");
  rowsEl.innerHTML = rows.map((row, index) => {{
    const ov = row.override || {{}};
    const effective = row.effective_c_code || "";
    const replacement = effective;
    const note = row.note || "";
    const selectedStatus = statusValue(row, ov);
    const reviewStatus = row.review_status || ov.review_status || "Pending";
    const confirmedAtValues = [...new Set(Object.values(row.per_ref_selection || {{}})
      .map(item => item.review_confirmed_at).filter(Boolean))];
    const confirmedRefs = Object.entries(row.per_ref_selection || {{}})
      .filter(([, item]) => item.review_confirmed_at);
    const confirmedAtTitle = confirmedRefs.map(([ref, item]) => `${{ref}}: ${{item.review_confirmed_at}}`).join("\\n");
    const confirmedAtText = confirmedAtValues.length === 1
      ? `${{reviewStatus === "Confirmed" ? "确认于" : "上次确认于"}} ${{formatReviewTime(confirmedAtValues[0])}}`
      : confirmedAtValues.length > 1 ? "各位号确认时间不同" : "";
    const reviewControl = `<select class="edit" data-field="review_status">${{[["Pending", "待确认"], ["Confirmed", "确认"], ["Rejected", "不确认"]].map(item => `<option value="${{item[0]}}" ${{item[0] === reviewStatus ? "selected" : ""}}>${{item[1]}}</option>`).join("")}}</select>`;
    const refButtons = renderRefButtons(row);
    const pageRefButtons = renderPageRefButtons(row);
    const code = stockCode(row);
    const original = originalCode(row);
    const stock = initialStock(row);
    const syncNeeded = !!row.field_changed_refs;
    const missingRef = rowHasMissingRef(row);
    const filterKeys = rowFilterKeys(row, selectedStatus, syncNeeded, stock, reviewStatus).join(" ");
    const syncButtonClass = syncNeeded ? "action-sync-needed" : "action-muted";
    const syncButton = `<button type="button" class="small ${{syncButtonClass}}" data-action="sync-schematic" title="${{syncNeeded ? "把当前行各位号的待同步字段写回 KiCad 原理图" : "当前字段和原理图一致；仍可手动写回"}}">同步原理图</button>`;
    const syncSummary = schematicSyncSummary(row);
    const syncMarker = schematicSyncMarker(row, syncNeeded);
    const fieldReconcileNotice = schematicFieldReconcileNotice(row);
    const splitMarker = row.temp_split ? `<div class="split-marker">临时拆行；字段不改会自动合回</div>` : "";
    return `<tr class="${{rowVisualClass(row, selectedStatus, syncNeeded, stock, reviewStatus)}}" data-ref="${{esc(row.ref)}}" data-refs="${{esc(row.refs)}}" data-sync-needed="${{syncNeeded ? "1" : "0"}}" data-missing-ref="${{missingRef ? "1" : "0"}}" data-review-status="${{esc(reviewStatus)}}" data-code-status="${{esc(selectedStatus)}}" data-manual-code="${{(row.manual_c_code || row.stock_c_code) ? "1" : "0"}}" data-has-code="${{code ? "1" : "0"}}" data-is-nc="${{rowIsNc(row) ? "1" : "0"}}" data-stock-low="${{stockIsLow(stock) ? "1" : "0"}}" data-stock-issue="${{stockIsIssue(stock) ? "1" : "0"}}" data-filter-key="${{esc(filterKeys)}}" data-stock-code="${{esc(code)}}" data-stock-queried="${{stockAlreadyQueried(stock) ? "1" : "0"}}" data-original-code="${{esc(original)}}" data-engineering="${{esc(row.accepted_c_code)}}" data-lcsc="${{esc(row.lcsc_match_c_code)}}">
      <td data-col="row_no" class="mono"><input type="checkbox" data-select-row aria-label="选择第 ${{index + 1}} 行">${{index + 1}}<span data-spec-warning></span></td>
      <td data-col="review">
        <div class="review-status">
          ${{reviewControl}}
          <span class="review-pill ${{reviewStatusClass(reviewStatus)}}" data-review-state title="${{row.review_status_mixed || row.selection_codes_mixed ? "位号确认状态或 C 编码不一致；整行按未确认处理" : ""}}">${{row.review_status_mixed || row.selection_codes_mixed ? "未确认" : esc(reviewStatusText(reviewStatus))}}</span>
          <small class="review-confirmed-at" data-review-confirmed-at title="${{esc(confirmedAtTitle)}}">${{esc(confirmedAtText)}}</small>
        </div>
        ${{syncButton}}<button type="button" class="small action-link" data-action="undo-workbench" title="预览并撤回本行工作台未同步的修改；不会修改原理图、PCB 或封装库">撤回修改</button>${{syncMarker}}${{fieldReconcileNotice}}${{syncSummary}}
      </td>
      <td data-col="refs" class="mono">${{refButtons}}${{splitMarker}}</td>
      <td data-col="page_refs" class="page-refs">${{pageRefButtons}}</td>
      <td data-col="quantity">${{esc(row.quantity || "")}}</td>
      <td data-col="comment"><textarea class="edit field-edit" data-field="comment" title="原始 Comment: ${{esc(row.original_comment_values || row.comment || "")}}">${{esc(row.comment)}}</textarea>${{fieldDiff(row, "comment")}}</td>
      <td data-col="value"><div class="value-edit-control"><textarea class="edit field-edit mono" data-field="value" title="原始 Value: ${{esc(row.original_value_values || row.value || "")}}">${{esc(row.value)}}</textarea><button type="button" class="value-parameters-button" data-action="value-parameters" title="从物料参数补充 Value" aria-label="从物料参数补充 Value"><svg class="toolbar-icon" aria-hidden="true"><use href="#toolbar-icon-arrow-left"></use></svg></button></div>${{fieldDiff(row, "value")}}</td>
      <td data-col="footprint">${{footprintEditor(row)}}${{fieldDiff(row, "footprint")}}</td>
      <td data-col="stock">
        <div class="supply-primary">
          <button type="button" class="small action-link" data-action="open-candidates">更换型号</button>
          <input class="edit mono" data-field="replacement_c_code" aria-label="主料 C 编码" placeholder="未选 C 编码" value="${{esc(replacement)}}" spellcheck="false">
          <a class="supply-icon" data-primary-code-link data-secondary-search="${{esc(effective)}}" href="${{esc(lcscUrl(effective))}}" target="_blank" rel="noopener" title="在立创查询此 C 编码" aria-label="在立创查询此 C 编码" ${{effective ? "" : "hidden"}}><svg class="toolbar-icon" aria-hidden="true"><use href="#toolbar-icon-search"></use></svg></a>
          <button type="button" class="small supply-icon" data-action="clear-primary" title="清空主料 C 编码" aria-label="清空主料 C 编码"><svg class="toolbar-icon" aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button>
        </div>
        <span data-effective-code hidden>${{esc(effective)}}</span>
        <input type="hidden" data-field="status" value="${{esc(selectedStatus)}}">
        ${{renderSupplyOrder(row)}}
        <div data-stock-cell>${{renderStockCell(stock)}}</div>
      </td>
      <td data-col="local_inventory" data-local-inventory></td>
      <td data-col="order" class="mono" data-stock-order>${{esc(stock.moq || "")}} / ${{esc(stock.mpq || "")}}</td>
      <td data-col="price" class="mono" data-stock-price>${{esc(stock.price || "")}}</td>
      <td data-col="param" class="param-text" data-stock-param data-stock-param-text="${{esc(stockParamText(stock))}}">${{renderStockParam(stock)}}</td>
      <td data-col="alternates">${{renderAlternateParts(row)}}</td>
      <td data-col="note"><textarea class="edit note" data-field="note">${{esc(note)}}</textarea></td>
    </tr>`;
  }}).join("");
  rowsEl.querySelectorAll("textarea[data-field], input[data-field]").forEach(input => {{ input.dataset.savedValue = input.value; }});
  refreshFootprintLibraries();
  updateSplitPageChecks();
  applyColumnOrder();
  applyColumnVisibility();
  applyActiveFilter();
  fitDefaultColumnWidths();
  window.refreshLocalInventory?.();
  window.refreshSpecificationWarnings?.(rows.some(row => row.spec_check_needs_refresh));
}}

function updateRowsForStock(code, stock) {{
  document.querySelectorAll("tr[data-stock-code]").forEach(tr => {{
    if (tr.dataset.stockCode === code) setStockCells(tr, stock);
    const row = rowDataForElement(tr);
    if (!row) return;
    if (Array.isArray(row.stock?.entries)) {{
      let changed = false;
      for (const entry of row.stock.entries) {{
        if (normalizeCCode(entry.c_code) !== code) continue;
        entry.stock = stock;
        row.stock_by_ref ||= {{}};
        row.stock_by_ref[entry.ref] = stock;
        changed = true;
      }}
      if (changed) {{
        tr.querySelector("[data-stock-cell]").innerHTML = renderStockCell(row.stock);
        refreshRowState(tr);
      }}
    }}
    let alternateChanged = false;
    for (const item of (row.alternate_parts || [])) {{
      if (normalizeCCode(item.c_code) !== code) continue;
      item.stock = stock;
      alternateChanged = true;
    }}
    if (alternateChanged) {{
      const cell = tr.querySelector("[data-alternate-cell]");
      if (cell) cell.outerHTML = renderAlternateParts(row);
    }}
  }});
}}

function uniqueSearchTerms(values) {{
  const seen = new Set();
  const result = [];
  for (const value of values) {{
    const text = String(value || "").trim();
    if (!text) continue;
    const key = text.toLocaleLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(text);
  }}
  return result;
}}

function passiveSearchType(fields) {{
  const text = `${{fields.comment || ""}} ${{fields.value || ""}}`.toLocaleLowerCase();
  const firstRef = String(fields.refs || "").split(",")[0].trim().toUpperCase();
  const types = [
    ["capacitor", /capacitors?|\bcap\b|电容/i, /^(C|CP)\d/],
    ["resistor", /resistors?|\bres\b|电阻/i, /^(R|RN|RP|RT|RV)\d/],
    ["inductor", /inductors?|\bchoke\b|ferrite\s*bead|电感|磁珠/i, /^(L|FB)\d/],
  ];
  for (const [name, textPattern, refPattern] of types) {{
    if (textPattern.test(text) || refPattern.test(firstRef)) return name;
  }}
  return "";
}}

function isExplicitPassiveComment(comment) {{
  const value = String(comment || "").trim().toLocaleLowerCase().replace(/\s+/g, " ");
  if (!value) return false;
  return !/^(?:chip\s+)?(?:resistor|capacitor|inductor)(?:\s*-\s*surface\s+mount)?$/.test(value)
    && !["电阻", "电容", "电感", "磁珠", "res", "cap"].includes(value);
}}

function candidateKeywordFromFields(fields) {{
  const passiveType = passiveSearchType(fields);
  if (passiveType) {{
    const explicit = isExplicitPassiveComment(fields.comment);
    const ferriteRef = /^FB\\d/i.test(String(fields.refs || "").trim());
    if (!explicit && !ferriteRef && ["resistor", "capacitor", "inductor"].includes(passiveType)) {{
      const value = String(fields.value || "").split(/[,，;；]/)[0]
        .replace(/\\s*[±+-]?\\s*\\d+(?:\\.\\d+)?\\s*%.*$/, "").trim();
      const nominal = value.match(/(\\S+)(?:\\s+([pnumkKMGµμ]?(?:[FfHh]|Ω|[Oo][Hh][Mm][Ss]?)|[RrkKM])(?=\\s|$))?/);
      const nominalValue = nominal ? nominal.slice(1).filter(Boolean).join(" ") : "";
      const footprint = String(fields.footprint || "").split(":").pop().trim();
      const packageMatch = footprint.match(/(?<!\\d)(?:01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\\d)/);
      return uniqueSearchTerms([nominalValue, packageMatch ? packageMatch[0] : footprint]).join(" ");
    }}
    const comment = explicit ? fields.comment : passiveType;
    return uniqueSearchTerms([comment, fields.value, fields.footprint]).join(" ");
  }}
  return String(fields.comment || fields.value || "").trim();
}}

function candidateKeywordForRow(tr) {{
  const fields = {{
    value: fieldValue(tr, "value"),
    footprint: fieldValue(tr, "footprint"),
    comment: fieldValue(tr, "comment"),
    refs: tr.dataset.refs || tr.dataset.ref || ""
  }};
  return candidateKeywordFromFields(fields);
}}

function compactRefsText(refsText, limit = 8) {{
  const refs = String(refsText || "").split(",").map(item => item.trim()).filter(Boolean);
  if (refs.length <= limit) return refs.join(",");
  return `${{refs.slice(0, limit).join(",")}} ... 共 ${{refs.length}} 个`;
}}

async function updateAlternateParts(tr, action, extra = {{}}) {{
  const anchor = rowAnchor(tr);
  const payload = Object.assign({{
    ref: tr.dataset.ref,
    refs: tr.dataset.refs,
    bom: bomSelect.value || "",
    action,
    primary_c_code: currentLcscCode(tr)
  }}, extra);
  const res = await fetch("/api/alternate_parts", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify(payload)
  }});
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "保存备选料失败");
  setState(`备选料已更新：${{payload.refs || payload.ref}}`, "saved");
  if (candidateOnly) await window.notifyNativeCandidateSaved();
  else await loadRows(anchor);
  return data;
}}

async function updateProcurementChoice(tr, action, choice = {{}}) {{
  const anchor = rowAnchor(tr);
  const payload = {{
    ref: tr.dataset.ref,
    refs: tr.dataset.refs,
    bom: bomSelect.value || "",
    action,
    choice
  }};
  const res = await fetch("/api/procurement_choice", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify(payload)
  }});
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "保存订货料失败");
  setState(`订货料已更新：${{payload.refs || payload.ref}}`, "saved");
  if (candidateOnly) await window.notifyNativeCandidateSaved();
  else await loadRows(anchor);
  return data;
}}

function candidateRowKey(tr) {{
  return String(tr?.dataset.refs || tr?.dataset.ref || "").trim();
}}

function updateCandidateWindowControls() {{
  const fullscreen = candidateWindow.classList.contains("fullscreen");
  maximizeCandidateButton.setAttribute("aria-label", fullscreen ? "还原" : "全屏");
  maximizeCandidateButton.title = fullscreen ? "还原" : "全屏";
  candidateMaximizeIcon.textContent = fullscreen ? "❐" : "□";
}}

function restoreCandidates() {{
  if (!candidateOnly && window.pywebview?.api?.candidate_window) {{ void window.restoreNativeCandidates(); return; }}
  if (!candidateModal.classList.contains("open")) return;
  candidateModal.classList.remove("minimized");
}}

function minimizeCandidates() {{
  if (window.pywebview?.api?.candidate_window) {{
    if (candidateOnly) void window.pywebview.api.candidate_window('minimize');
    else void window.minimizeNativeCandidates();
    return;
  }}
  if (!candidateModal.classList.contains("open")) return;
  candidateMinimizedTitle.textContent = candidateTitle.textContent || "更换型号";
  candidateModal.classList.add("minimized");
}}

function toggleCandidateFullscreen() {{
  restoreCandidates();
  candidateWindow.classList.toggle("fullscreen");
  updateCandidateWindowControls();
}}

function resetCandidateWindowGeometry() {{
  candidateWindow.classList.remove("fullscreen");
  for (const property of ["position", "left", "top", "width", "height"]) {{
    candidateWindow.style.removeProperty(property);
  }}
  updateCandidateWindowControls();
}}

function beginCandidateMove(event) {{
  if (event.button !== 0 || event.target.closest(".modal-controls, .candidate-title")) return;
  if (candidateWindow.classList.contains("fullscreen") || candidateModal.classList.contains("minimized")) return;
  event.preventDefault();
  const startX = event.clientX;
  const startY = event.clientY;
  const rect = candidateWindow.getBoundingClientRect();
  const viewportMargin = 12;

  function onMove(moveEvent) {{
    const maxLeft = Math.max(viewportMargin, window.innerWidth - viewportMargin - rect.width);
    const maxTop = Math.max(viewportMargin, window.innerHeight - viewportMargin - rect.height);
    const left = Math.min(Math.max(rect.left + moveEvent.clientX - startX, viewportMargin), maxLeft);
    const top = Math.min(Math.max(rect.top + moveEvent.clientY - startY, viewportMargin), maxTop);
    candidateWindow.style.position = "fixed";
    candidateWindow.style.left = `${{Math.round(left)}}px`;
    candidateWindow.style.top = `${{Math.round(top)}}px`;
    candidateWindow.style.width = `${{Math.round(rect.width)}}px`;
    candidateWindow.style.height = `${{Math.round(rect.height)}}px`;
  }}

  function onUp() {{
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", onUp);
  }}

  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
}}

function beginCandidateResize(event) {{
  if (candidateWindow.classList.contains("fullscreen") || candidateModal.classList.contains("minimized")) return;
  const direction = event.currentTarget.dataset.resize || "";
  if (!direction) return;
  event.preventDefault();
  event.stopPropagation();
  const startX = event.clientX;
  const startY = event.clientY;
  const rect = candidateWindow.getBoundingClientRect();
  const viewportMargin = 12;
  const minWidth = Math.min(760, window.innerWidth - viewportMargin * 2);
  const minHeight = Math.min(480, window.innerHeight - viewportMargin * 2);

  function clamp(value, minimum, maximum) {{
    return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
  }}

  function onMove(moveEvent) {{
    const dx = moveEvent.clientX - startX;
    const dy = moveEvent.clientY - startY;
    let left = rect.left;
    let top = rect.top;
    let width = rect.width;
    let height = rect.height;
    if (direction.includes("e")) width = clamp(rect.width + dx, minWidth, window.innerWidth - viewportMargin - rect.left);
    if (direction.includes("s")) height = clamp(rect.height + dy, minHeight, window.innerHeight - viewportMargin - rect.top);
    if (direction.includes("w")) {{
      width = clamp(rect.width - dx, minWidth, rect.right - viewportMargin);
      left = rect.right - width;
    }}
    if (direction.includes("n")) {{
      height = clamp(rect.height - dy, minHeight, rect.bottom - viewportMargin);
      top = rect.bottom - height;
    }}
    candidateWindow.style.position = "fixed";
    candidateWindow.style.left = `${{Math.round(left)}}px`;
    candidateWindow.style.top = `${{Math.round(top)}}px`;
    candidateWindow.style.width = `${{Math.round(width)}}px`;
    candidateWindow.style.height = `${{Math.round(height)}}px`;
  }}

  function onUp() {{
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", onUp);
  }}

  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
}}

async function openCandidates(tr) {{
  if (!candidateOnly && window.pywebview?.api?.candidate_window) return window.openNativeCandidates(tr);
  if (candidateTargetRow && candidateTargetRow.dataset.candidateSaving === "1") {{
    restoreCandidates();
    return;
  }}
  const targetKey = candidateRowKey(tr);
  if (candidateModal.classList.contains("open") && candidateModal.classList.contains("minimized") && targetKey && targetKey === candidateTargetKey) {{
    candidateTargetRow = tr;
    restoreCandidates();
    return;
  }}
  invalidateCandidateSearch();
  candidateModal.classList.remove("minimized");
  candidateTargetRow = tr;
  candidateTargetKey = targetKey;
  candidateFootprintMode.value = "keep";
  candidateReplaceValue.value = "false";
  candidateReplaceComment.value = "false";
  const refs = tr.dataset.refs || tr.dataset.ref || "";
  candidateTitle.textContent = `更换型号：${{compactRefsText(refs)}}`;
  candidateTitle.title = refs;
  candidateMinimizedTitle.textContent = candidateTitle.textContent;
  candidateKeyword.value = candidateKeywordForRow(tr);
  candidateRows.innerHTML = '<tr><td colspan="8">正在同步搜索：官网 + 结构化候选</td></tr>';
  candidateModal.classList.add("open");
  const autoSearch = await shouldOpenSearchOnReplace();
  if (autoSearch) void launchOfficialSearch(candidateKeyword.value.trim());
  void searchCandidates().catch(err => {{
    candidateRows.innerHTML = `<tr><td colspan="8" class="bad">结构化查询失败：${{esc(err.message || String(err))}}；网页搜索仍可继续使用</td></tr>`;
  }});
}}

async function shouldOpenSearchOnReplace() {{
  try {{
    const response = await fetch("/api/config");
    if (!response.ok) throw new Error("读取工程设置失败");
    const config = await response.json();
    return String(config.open_lcsc_search_on_replace ?? "true") !== "false";
  }} catch (_error) {{
    return true;
  }}
}}

function formatReviewTime(value) {{
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  return new Intl.DateTimeFormat("zh-CN", {{year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}}).format(date);
}}

function closeCandidates(afterSave = false) {{
  if (candidateOnly && afterSave !== true && !window.nativeCandidateCanExit()) {{
    candidateSearchProgress.textContent = "正在保存或转换，请完成后再关闭窗口";
    return;
  }}
  if (candidateTargetRow && candidateTargetRow.dataset.candidateSaving === "1") return;
  invalidateCandidateSearch();
  candidateModal.classList.remove("open", "minimized");
  resetCandidateWindowGeometry();
  candidateTargetRow = null;
  candidateTargetKey = "";
  currentCandidateItems = new Map();
  currentProcurementQuotes = new Map();
  if (candidateOnly) window.closeNativeCandidates();
}}

function officialSearchUrl(keyword) {{
  return `https://so.szlcsc.com/global.html?k=${{encodeURIComponent(keyword || "")}}`;
}}

async function launchLcscUrl(url, label = "立创页面") {{
  const value = String(url || "").trim();
  if (!value) return;
  try {{
    const res = await fetch("/api/open_lcsc_search", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{url: value}})
    }});
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "打开网页失败");
    setState(`已打开${{label}}`, "saved");
  }} catch (err) {{
    window.open(value, "_blank", "noopener");
    setState(`网页打开失败，已改用浏览器标签页：${{err.message || String(err)}}`, "status");
  }}
}}

async function launchOfficialSearch(keyword) {{
  const value = String(keyword || "").trim();
  if (!value) return;
  try {{
    const res = await fetch("/api/open_lcsc_search", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{keyword: value}})
    }});
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "打开网页搜索失败");
    setState(`已打开立创网页搜索：${{value}}`, "saved");
  }} catch (err) {{
    window.open(officialSearchUrl(value), "_blank", "noopener");
    setState(`网页搜索打开失败，已改用浏览器标签页：${{err.message || String(err)}}`, "status");
  }}
}}

document.addEventListener("click", event => {{
  const urlLink = event.target.closest("a[data-secondary-url]");
  if (urlLink) {{
    const url = String(urlLink.dataset.secondaryUrl || urlLink.href || "").trim();
    if (url) {{
      event.preventDefault();
      void launchLcscUrl(url, urlLink.dataset.secondaryLabel || "立创页面");
      return;
    }}
  }}
  const link = event.target.closest("a[data-secondary-search]");
  if (!link) return;
  const keyword = String(link.dataset.secondarySearch || "").trim();
  if (!keyword) return;
  event.preventDefault();
  void launchOfficialSearch(keyword);
}});

function normalizedCandidateParam(value) {{
  return String(value || "").toLocaleLowerCase().replace(/[\\s:：;；,，、]+/g, "");
}}

function uniqueCandidateParamSegments(...values) {{
  const seenBlocks = new Set();
  const seenSegments = new Set();
  const segments = [];
  for (const value of values) {{
    const text = String(value || "").trim();
    const blockKey = normalizedCandidateParam(text);
    if (!text || !blockKey || seenBlocks.has(blockKey)) continue;
    seenBlocks.add(blockKey);
    const parts = /[:：]/.test(text) ? text.split(/\s*[;；、]\s*/).filter(Boolean) : [text];
    for (const part of parts) {{
      const segment = part.trim();
      const segmentKey = normalizedCandidateParam(segment);
      if (!segmentKey || seenSegments.has(segmentKey)) continue;
      seenSegments.add(segmentKey);
      segments.push(segment);
    }}
  }}
  return segments;
}}

function renderCandidateParam(...values) {{
  const segments = values.flatMap(value => typeof value === "object" && value !== null
    ? [value] : uniqueCandidateParamSegments(value));
  const lines = segments.map(line => {{
    const match = typeof line === "object" ? [null, line.name, line.value]
      : line.match(/^([^:：]{{1,64}})\s*[:：]\s*(.+)$/);
    if (!match) return `<div class="candidate-param-value">${{esc(line)}}</div>`;
    return `<div class="candidate-param-line"><span class="candidate-param-key">${{esc(match[1])}}</span><span class="candidate-param-value">${{esc(match[2])}}</span></div>`;
  }});
  return lines.length
    ? `<div class="candidate-param-lines">${{lines.join("")}}</div>`
    : "";
}}

function renderCandidateEntryParam(entry, target = {{}}) {{
  const source = entry.channel || entry.item || {{}};
  const kind = candidateParameterKind(entry, target);
  const pairs = candidateParameterPairs(source, kind);
  const required = {{resistor: ["温度系数", "工作电压"], inductor: ["直流电阻(DCR)", "电感类型"]}}[kind] || [];
  const status = source.detail_parameter_status;
  const unknown = entry.channel ? "渠道未返回" : status === "ready" ? "详情页未列出"
    : status === "failed" ? "详情读取失败" : status === "pending" ? "正在补取详情" : "详情尚未读取";
  const lines = pairs.map(pair => ({{name: pair.label, value: candidateKnownParamValue(pair.value) ? pair.value : unknown}}));
  for (const name of required) {{
    if (!pairs.some(pair => pair.key === name)) lines.push({{name, value: unknown}});
  }}
  const summary = source.param && !/[:：]/.test(source.param) ? source.param : "";
  const origin = entry.channel ? "订货渠道" : status === "ready" ? "商品详情"
    : source.parameter_provenance === "cached-and-search" ? "搜索列表 / 历史缓存" : "搜索列表";
  const retry = !entry.channel && (status === "failed" || (!source.product_id && status !== "ready"))
    ? `<button type="button" class="small action-link" data-action="retry-candidate-parameters" data-code="${{esc(source.c_code)}}" title="读取商品详情参数">${{status === "failed" ? "重试详情" : "读取详情"}}</button>` : "";
  return renderCandidateParam(summary, ...lines) + `<div class="candidate-partner-meta">参数来源：${{origin}} ${{retry}}</div>`;
}}

function renderCandidateOrderLines(lines) {{
  const values = lines.filter(Boolean);
  return values.length
    ? `<div class="candidate-order-lines">${{values.map(line => `<div>${{esc(line)}}</div>`).join("")}}</div>`
    : "待核实";
}}

function renderCandidatePrice(value) {{
  const lines = String(value || "").split(/\s*;\s*/).map(line => line.trim()).filter(Boolean);
  return lines.length
    ? `<div class="candidate-price-lines">${{lines.map(line => `<div class="candidate-price-line">${{esc(line)}}</div>`).join("")}}</div>`
    : "价格待确认";
}}

function renderCandidatePartnerRows(items, selectedQuotes = null, target = {{}}) {{
  const quotes = [];
  const seen = new Set();
  currentProcurementQuotes = new Map();
  for (const item of items) {{
    for (const channel of (Array.isArray(item.order_channels) ? item.order_channels : [])) {{
      const key = channel.url || [channel.supplier, channel.vendor_code, channel.provider_model].join("|");
      if (!key || seen.has(key)) continue;
      seen.add(key);
      quotes.push({{item, channel}});
    }}
  }}
  if (selectedQuotes !== null) quotes.splice(0, quotes.length, ...selectedQuotes);
  if (!quotes.length) return "";
  const section = `<tr class="candidate-section-row"><td colspan="8">
    <div class="candidate-section-head"><strong>订货渠道</strong><span>合作库存/官网海外渠道报价，交期、价格、批次需再次核实</span></div>
  </td></tr>`;
  const rows = quotes.map(({{item, channel}}) => {{
    const image = item.image_url_big || item.image_url || "";
    const model = channel.model || item.model || "";
    const providerModel = channel.provider_model || model;
    const code = channel.c_code || item.c_code || "";
    const orderLines = [
      channel.moq ? `起订 ${{channel.moq}}` : "",
      channel.increment ? `递增 ${{channel.increment}}` : "",
      channel.mpq ? `最小包装 ${{channel.mpq}}` : "",
    ];
    const link = channel.url
      ? `<a class="candidate-order-link" href="${{esc(channel.url)}}" target="_blank" rel="noopener" data-secondary-url="${{esc(channel.url)}}" data-secondary-label="订货渠道链接">立创订货</a>`
      : `<span class="candidate-partner-meta">暂无订货链接</span>`;
    const quoteKey = String(channel.url || "");
    if (quoteKey) currentProcurementQuotes.set(quoteKey, {{item, channel}});
    const choose = quoteKey
      ? `<button type="button" class="small stock-query" data-action="select-procurement" data-quote-key="${{esc(quoteKey)}}" title="记录这条报价的订货链接，不改变主料 C 编码">选用订货料</button>`
      : `<button type="button" class="small stock-query" disabled title="该报价没有可记录的订货链接">选用订货料</button>`;
    return `<tr class="candidate-partner-row">
      <td data-candidate-col="action"><div class="candidate-partner-actions">${{choose}}${{link}}</div></td>
      <td data-candidate-col="image">${{image ? `<img class="thumb" loading="lazy" src="${{esc(image)}}" alt="">` : "暂无图片"}}</td>
      <td data-candidate-col="model">
        <div><strong>${{esc(providerModel)}}</strong></div>
        <div>${{esc(channel.brand || item.brand || "")}}</div>
        <div class="mono">${{esc(channel.package || item.package || "")}}</div>
        <div class="candidate-partner-meta">批次：${{esc(channel.batch_number || "待核实")}}</div>
      </td>
      <td data-candidate-col="param">${{renderCandidateEntryParam({{item, channel}}, target)}}</td>
      <td data-candidate-col="code" class="mono">${{code ? `无（关联 ${{esc(code)}}）` : "无"}}</td>
      <td data-candidate-col="channel_stock">
        <div class="candidate-partner-supplier">${{esc(channel.supplier || channel.name || "订货渠道")}}</div>
        <div class="candidate-partner-meta">编号：${{esc(channel.supplier_sku || channel.vendor_code || "待核实")}}</div>
        <div class="candidate-stock-lines mono">
          <div><strong>库存：${{esc(channel.stock || "待核实")}}</strong></div>
          <div>交期：${{esc(channel.lead_time || "待确认")}}</div>
        </div>
      </td>
      <td data-candidate-col="order" class="mono">${{renderCandidateOrderLines(orderLines)}}</td>
      <td data-candidate-col="price" class="mono candidate-partner-price">${{renderCandidatePrice(channel.price || "")}}</td>
    </tr>`;
  }}).join("");
  return section + rows;
}}

function renderCandidateRows(items, partnerEntries = null) {{
  currentCandidateItems = new Map(items.map(item => [normalizeCCode(item.c_code), item]));
  currentProcurementQuotes = new Map();
  if (!items.length && !(partnerEntries && partnerEntries.length)) {{
    candidateRows.innerHTML = `<tr><td colspan="8">${{candidateRawItems.length ? "没有符合筛选条件的结果，可移除条件或清空筛选" : "没有候选结果"}}</td></tr>`;
    return;
  }}
  const targetRow = rowDataForElement(candidateTargetRow) || {{}};
  const primaryCode = currentLcscCode(candidateTargetRow);
  const existingCodes = new Set(alternateCodes(targetRow.alternate_parts));
  const alternateCount = (targetRow.alternate_parts || []).length;
  const primaryRows = items.map(item => {{
    const code = normalizeCCode(item.c_code);
    const alternateReason = !code ? "候选料没有有效 C 编码"
      : code === primaryCode ? "当前主料不能作为备选料"
      : existingCodes.has(code) ? "已经在备选料列表中"
      : alternateCount >= 5 ? "每行最多 5 个备选料"
      : "";
    return `
    <tr>
      <td data-candidate-col="action">
        <button type="button" class="small stock-query" data-action="choose-candidate" data-code="${{esc(code)}}">选用主料</button>
        <button type="button" class="small action-link" data-action="add-candidate-alternate" data-code="${{esc(code)}}" title="${{esc(alternateReason || "加入备选料")}}" ${{alternateReason ? "disabled" : ""}}>加入备选</button>
      </td>
      <td data-candidate-col="image">${{(item.image_url_big || item.image_url) ? `<img class="thumb" loading="lazy" src="${{esc(item.image_url_big || item.image_url)}}" alt="">` : "暂无图片"}}</td>
      <td data-candidate-col="model">
        <div><strong>${{esc(item.model || "")}}</strong></div>
        <div>${{esc(item.brand || "")}}</div>
        <div class="mono">${{esc(item.package || "")}}</div>
        <div class="status">${{esc(item.match_type || "")}}</div>
      </td>
      <td data-candidate-col="param">${{renderCandidateEntryParam({{item}}, targetRow)}}</td>
      <td data-candidate-col="code" class="mono">${{item.url ? `<a target="_blank" rel="noopener" data-secondary-search="${{esc(item.c_code || "")}}" href="${{esc(item.url)}}">${{esc(item.c_code || "")}}</a>` : esc(item.c_code || "")}}</td>
      <td data-candidate-col="channel_stock">
        <div>${{esc(item.channel || "")}}</div>
        <div class="candidate-stock-lines">
          <div class="mono"><strong>库存：${{esc(item.stock || "待查询")}}</strong></div>
          <div>交期：${{esc(item.delivery || "待确认")}}</div>
        </div>
      </td>
      <td data-candidate-col="order" class="mono">${{renderCandidateOrderLines([
        item.moq ? `起订 ${{item.moq}}` : "",
        item.mpq ? `最小包装 ${{item.mpq}}` : ""
      ])}}</td>
      <td data-candidate-col="price" class="mono">${{renderCandidatePrice(item.price_ladder || item.price || "")}}</td>
    </tr>`;
  }}).join("");
  candidateRows.innerHTML = primaryRows + renderCandidatePartnerRows(items, partnerEntries, targetRow);
  applyCandidateColumnOrder();
  applyCandidateSavedWidths();
  makeCandidateColumnsConfigurable();
  fitDefaultColumnWidths(true);
}}

function candidateStockNumber(item) {{
  return candidateEntryStock({{item}});
}}

function candidateResultEntries(items) {{
  const entries = [];
  const seen = new Set();
  for (const item of items) {{
    entries.push({{item}});
    for (const channel of (Array.isArray(item.order_channels) ? item.order_channels : [])) {{
      const key = channel.url || [channel.supplier, channel.vendor_code, channel.provider_model].filter(Boolean).join("|");
      if (!key || seen.has(key)) continue;
      seen.add(key);
      entries.push({{item, channel}});
    }}
  }}
  return entries;
}}

function candidateKnownParamValue(value) {{
  const text = String(value ?? "").normalize("NFKC").trim();
  return !!text && !/^(?:[-—–]+|未提供|待核实|待确认|未知|暂无|无|n[/]?a|unknown|not specified|null|undefined)$/i.test(text);
}}

function candidateParameterKind(entry, target = {{}}) {{
  const category = String(entry.channel?.category || entry.item?.category || "").trim();
  const text = category || String(target.kind || target.component_kind || target.comment || "").trim();
  const kinds = [
    ["resistor", /\\bresistors?\\b|电阻|^R$/i],
    ["inductor", /\\binductors?\\b|\\bchokes?\\b|电感|^L$/i],
    ["capacitor", /\\bcapacitors?\\b|电容|^C$/i],
  ].filter(([, pattern]) => pattern.test(text));
  return kinds.length === 1 ? kinds[0][0] : "";
}}

function candidateParameterPairs(source, kind = candidateParameterKind({{item: source}})) {{
  const aliases = {{"容量": "容值", "电容量": "容值", "电容值": "容值", "电阻值": "阻值", "容差": "精度", "公差": "精度",
    "temperature coefficient": "温度系数", "temperature coefficient of resistance": "温度系数",
    "tcr": "温度系数", "电阻温度系数": "温度系数", "温度系数": "温度系数",
    "working voltage": "工作电压", "operating voltage": "工作电压", "工作电压": "工作电压",
    "最大工作电压": "工作电压", "maximum working voltage": "工作电压", "max working voltage": "工作电压",
    "dcr": "直流电阻(DCR)", "dc resistance": "直流电阻(DCR)", "直流电阻": "直流电阻(DCR)",
    "直流电阻(dcr)": "直流电阻(DCR)", "dc resistance (dcr)": "直流电阻(DCR)",
    "inductor type": "电感类型", "电感类型": "电感类型"}};
  if (kind === "inductor") Object.assign(aliases, {{"type": "电感类型", "类型": "电感类型"}});
  const raw = [];
  if (Array.isArray(source.parameters) && source.parameters.length) {{
    for (const pair of source.parameters) if (pair && pair.name) raw.push([pair.name, pair.value]);
  }} else {{
    for (const value of [source.param, source.param_detail, source.arrange]) {{
      const segments = String(value || "").split(/[;；、\\n\\r]+|[,，](?=\s*[^:：,，;；\\n]{{1,64}}[:：])/);
      for (const segment of segments) {{
        const match = segment.trim().match(/^([^:：]{{1,64}})\s*[:：]\s*(.+)$/);
        if (!match) continue;
        raw.push([match[1], match[2]]);
      }}
    }}
  }}
  const seen = new Set();
  return raw.flatMap(([name, value]) => {{
    const label = String(name).replace(/\\s+/g, " ").trim();
    const alias = label.replace(/\\s*[(（][^()（）]*[)）]\\s*$/, "").trim().toLowerCase();
    const key = aliases[alias] || label;
    const unit = label.match(/[(（]\\s*(ppm(?:\\/(?:°C|℃|K))?|[pmunµμkKM]?(?:F|H|A|V|W|Ω|Ω|[Oo][Hh][Mm][Ss]?)|%)\\s*[)）]$/);
    let paramValue = String(value ?? "").trim();
    if (aliases[alias] && unit && /^[±+-]?\\d+(?:\\.\\d+)?$/.test(paramValue)) paramValue += unit[1];
    const id = JSON.stringify([key, paramValue]);
    if (seen.has(id)) return [];
    seen.add(id);
    return [{{key, label: aliases[alias] ? key : label, value: paramValue}}];
  }});
}}

function candidateEntryFields(entry) {{
  const item = entry.item || {{}};
  const channel = entry.channel;
  const fields = new Map();
  const add = (key, value) => {{
    const text = String(value ?? "").normalize("NFKC").replace(/\\s+/g, " ").trim();
    if (!candidateKnownParamValue(text)) return;
    if (!fields.has(key)) fields.set(key, new Set());
    fields.get(key).add(text);
  }};
  add("brand", channel?.brand || item.brand);
  add("package", channel?.package || item.package);
  // Electrical parameters belong to this supplier, never to its associated parent row.
  for (const pair of candidateParameterPairs(channel || item, candidateParameterKind(entry))) {{
    if (!/^(型号|制造商型号|品牌|封装|model|mpn|brand|package)$/i.test(pair.key)) add("param:" + pair.key, pair.value);
  }}
  return fields;
}}

function candidateEntryStock(entry) {{
  const stock = entry.channel ? entry.channel.stock : entry.item?.stock;
  const match = String(stock ?? "").replace(/,/g, "").trim().match(/^(\d+(?:\.\d+)?)\s*([kKmM万]?)$/);
  if (!match) return 0;
  const multipliers = {{k: 1000, m: 1000000, "万": 10000}};
  return Number(match[1]) * (multipliers[match[2].toLowerCase()] || 1);
}}

function filterCandidateEntries(entries, filters, stockOnly, excludedKey = "") {{
  return entries.filter(entry => {{
    if (stockOnly && candidateEntryStock(entry) <= 0) return false;
    const fields = entry.fields || candidateEntryFields(entry);
    return Array.from(filters).every(([key, selected]) => key === excludedKey || !selected.size ||
      Array.from(selected).some(value => fields.get(key)?.has(value)));
  }});
}}

function buildCandidateFacets(entries, filters, stockOnly) {{
  const prepared = entries.map(entry => ({{...entry, fields: entry.fields || candidateEntryFields(entry)}}));
  const facets = new Map();
  for (const entry of prepared) for (const [key, values] of entry.fields) {{
    if (!facets.has(key)) facets.set(key, new Map());
    for (const value of values) facets.get(key).set(value, 0);
  }}
  for (const [key, selected] of filters) {{
    if (!facets.has(key)) facets.set(key, new Map());
    for (const value of selected) if (candidateKnownParamValue(value) && !facets.get(key).has(value)) facets.get(key).set(value, 0);
  }}
  for (const [key, options] of facets) {{
    for (const entry of filterCandidateEntries(prepared, filters, stockOnly, key)) {{
      for (const value of entry.fields.get(key) || []) options.set(value, (options.get(value) || 0) + 1);
    }}
  }}
  return facets;
}}

function renderCandidateFilterPanel() {{
  const entries = candidateResultEntries(candidateRawItems);
  const labelFor = key => ({{brand: "品牌", package: "封装"}}[key] || key.replace(/^param:/, ""));
  document.getElementById("candidateFilterBar").hidden = !entries.length && !candidateFilters.size && !inStockOnly.checked;
  document.getElementById("candidateFilterCount").textContent = "显示 " + filterCandidateEntries(entries, candidateFilters, inStockOnly.checked).length + " / 共 " + entries.length + " 条";
  const tags = [];
  for (const [key, values] of candidateFilters) for (const value of values) {{
    tags.push('<span class="candidate-filter-tag"><span>' + esc(labelFor(key)) + '：' + esc(value) + '</span><button type="button" data-remove-candidate-filter="' + esc(key) + '" data-filter-value="' + esc(value) + '" aria-label="' + esc('移除 ' + labelFor(key) + ' ' + value) + '" title="移除此条件">&times;</button></span>');
  }}
  if (inStockOnly.checked) tags.push('<span class="candidate-filter-tag"><span>现货</span><button type="button" data-remove-candidate-stock aria-label="移除现货筛选" title="移除此条件">&times;</button></span>');
  document.getElementById("candidateFilterTags").innerHTML = tags.join("");
  document.getElementById("clearCandidateFilters").hidden = !candidateFilters.size && !inStockOnly.checked;
  candidateWindow.querySelectorAll("[data-candidate-filter]").forEach(button => {{
    const section = button.dataset.candidateFilter;
    const active = Array.from(candidateFilters.keys()).some(key => section === "identity" ? key === "brand" || key === "package" : key.startsWith("param:"));
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute("aria-expanded", String(candidateFilterSection === section));
  }});
  candidateFilterPanel.hidden = !candidateFilterSection;
  if (!candidateFilterSection) return;
  const facets = buildCandidateFacets(entries, candidateFilters, inStockOnly.checked);
  document.getElementById("candidateFilterTitle").textContent = candidateFilterSection === "identity" ? "品牌 / 封装" : "参数详情";
  const search = candidateFilterOptionSearch.value.trim().toLocaleLowerCase();
  const groups = [];
  const keys = Array.from(facets.keys()).filter(key => candidateFilterSection === "identity" ? key === "brand" || key === "package" : key.startsWith("param:"));
  keys.sort((a,b) => a.localeCompare(b, "zh-CN"));
  for (const key of keys) {{
    const label = labelFor(key);
    const values = Array.from(facets.get(key)).filter(([value]) => !search || (label + " " + value).toLocaleLowerCase().includes(search));
    values.sort(([a],[b]) => a.localeCompare(b, "zh-CN", {{numeric: true}}));
    if (!values.length) continue;
    const options = values.map(([value,count]) => {{
      const checked = candidateFilters.get(key)?.has(value);
      return '<label class="candidate-filter-option"><input type="checkbox" data-candidate-facet="' + esc(key) + '" value="' + esc(value) + '" ' + (checked ? 'checked ' : '') + (!count && !checked ? 'disabled' : '') + '><span>' + esc(value) + '</span><small>(' + count + ')</small></label>';
    }}).join("");
    groups.push('<fieldset class="candidate-filter-group"><legend>' + esc(label) + '</legend><div class="candidate-filter-values">' + options + '</div></fieldset>');
  }}
  document.getElementById("candidateFilterGroups").innerHTML = groups.join("") || '<div class="candidate-filter-empty">当前结果没有可用的筛选项</div>';
}}

function resetCandidateFilters() {{
  candidateFilters.clear();
  inStockOnly.checked = false;
  candidateFilterSection = "";
  candidateFilterOptionSearch.value = "";
  renderCandidateFilterPanel();
}}

function invalidateCandidateSearch() {{
  const sequence = ++candidateSearchToken;
  if (candidateSearchController) candidateSearchController.abort();
  candidateSearchController = null;
  candidateRawItems = [];
  candidateParameterRequests.clear();
  resetCandidateFilters();
  currentCandidateItems = new Map();
  currentProcurementQuotes = new Map();
  candidateSearchProgress.textContent = "";
  candidateRows.innerHTML = '<tr><td colspan="8">请输入关键词后查询</td></tr>';
  void fetch("/api/lcsc_search_cancel", {{method: "POST", keepalive: true,
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{client: candidateSearchClient, sequence}})
  }}).catch(() => {{}});
}}

function renderCandidateResults() {{
  for (const item of candidateRawItems) {{
    const request = candidateParameterRequests.get(candidateParameterKey(item));
    if (request?.result?.ok) Object.assign(item, request.result,
      {{detail_parameter_status: "ready", parameter_provenance: "product-detail"}});
    else if (request?.done) item.detail_parameter_status = "failed";
  }}
  renderCandidateFilterPanel();
  const visible = filterCandidateEntries(candidateResultEntries(candidateRawItems), candidateFilters, inStockOnly.checked);
  renderCandidateRows(visible.filter(entry => !entry.channel).map(entry => entry.item), visible.filter(entry => entry.channel));
  supplementCandidateParameters(candidateSearchToken);
}}

function candidateParameterKey(item) {{
  return [normalizeCCode(item.c_code), String(item.model || "").trim().toUpperCase(), item.product_id || ""].join("|");
}}

function supplementCandidateParameters(token, retryCode = "") {{
  if (token !== candidateSearchToken) return;
  const queue = [];
  for (const item of candidateRawItems) {{
    const code = normalizeCCode(item.c_code), key = candidateParameterKey(item);
    if (!code || candidateParameterRequests.has(key) || (!/^\\d+$/.test(String(item.product_id || "")) && code !== retryCode)) continue;
    let resolve;
    const request = {{done: false, result: null, promise: new Promise(done => {{resolve = done;}})}};
    candidateParameterRequests.set(key, request);
    item.detail_parameter_status = "pending";
    queue.push({{item, key, request, resolve}});
  }}
  async function worker() {{
    while (queue.length) {{
      const job = queue.shift();
      try {{
        if (token !== candidateSearchToken) continue;
        const response = await fetch("/api/candidate_parameters", {{method: "POST", headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{c_code: job.item.c_code, product_id: job.item.product_id || "", model: job.item.model || "",
            force_refresh: Boolean(retryCode)}})}});
        const result = await response.json();
        if (token !== candidateSearchToken || candidateParameterRequests.get(job.key) !== job.request) continue;
        if (response.ok && result.ok && normalizeCCode(result.c_code) === normalizeCCode(job.item.c_code)
            && String(result.model || "").trim().toUpperCase() === String(job.item.model || "").trim().toUpperCase()) {{
          job.request.result = result;
          candidateParameterRequests.set(candidateParameterKey({{...job.item, ...result}}), job.request);
        }}
        job.request.done = true;
        renderCandidateResults();
      }} catch (error) {{
        if (token === candidateSearchToken && candidateParameterRequests.get(job.key) === job.request) {{
          job.request.done = true;
          renderCandidateResults();
        }}
      }} finally {{ job.resolve(); }}
    }}
  }}
  const workers = Math.min(4, queue.length);
  for (let i = 0; i < workers; i++) void worker();
}}

function candidateSearchStatusText(data) {{
  const names = {{domestic: "商城", partner: "订货渠道", overseas: "海外渠道", fallback: "补充检索"}};
  const statuses = {{querying: "查询中", ready: "已返回", empty: data.done ? "暂无匹配" : "暂未匹配", failed: "失败"}};
  const sources = Object.entries(data.sources || {{}}).filter(([,state]) => state !== "skipped")
    .map(([name,state]) => `${{names[name] || name}}：${{statuses[state] || state}}`).join(" · ");
  let summary = data.done ? (data.items?.length ? "查询完成" : "查询完成，未找到匹配器件") : "查询中";
  if (data.cached) summary = data.done ? "缓存结果" : "显示缓存，正在更新";
  else if (!data.done && data.items?.length) summary = "已返回部分结果，继续查询";
  const stamp = data.checked_at ? ` · 数据时间 ${{data.checked_at.replace("T", " ")}}` : "";
  return `${{summary}}${{sources ? " · " + sources : ""}}${{stamp}}${{data.error ? " · " + data.error : ""}}`;
}}

async function searchCandidates(forceRefresh = false) {{
  invalidateCandidateSearch();
  const requestToken = ++candidateSearchToken;
  const keyword = candidateKeyword.value.trim();
  if (!keyword) {{
    candidateRows.innerHTML = '<tr><td colspan="8">请输入搜索关键词</td></tr>';
    return;
  }}
  candidateRows.innerHTML = `<tr><td colspan="8">正在搜索：${{esc(keyword)}}</td></tr>`;
  candidateSearchProgress.textContent = "查询中…";
  const compactCode = String(keyword || "").replace(/\s+/g, "").toUpperCase();
  const exactCCode = /^C\d+$/.test(compactCode);
  const controller = new AbortController();
  candidateSearchController = controller;
  const current = () => requestToken === candidateSearchToken && candidateModal.classList.contains("open") && keyword === candidateKeyword.value.trim();
  let url = `/api/lcsc_search?progressive=1&keyword=${{encodeURIComponent(keyword)}}&include_substitutes=${{exactCCode ? "0" : "1"}}&client=${{encodeURIComponent(candidateSearchClient)}}&sequence=${{requestToken}}&force=${{forceRefresh === true ? "1" : "0"}}`;
  let revision = -1;
  const started = Date.now();
  try {{
    while (current()) {{
      const res = await fetch(url, {{signal: controller.signal, cache: "no-store"}});
      const data = await res.json();
      if (!current() || data.cancelled) return;
      candidateSearchProgress.textContent = candidateSearchStatusText(data);
      if (data.revision !== revision) {{
        revision = data.revision;
        candidateRawItems = data.items || [];
        if (candidateRawItems.length || data.done) renderCandidateResults();
      }}
      if (!res.ok || !data.ok) throw new Error(data.error || "搜索失败");
      if (data.done) return;
      if (Date.now() - started > 120000) throw new Error("查询超时，请重试");
      url = `/api/lcsc_search?progressive=1&job=${{encodeURIComponent(data.job_id)}}`;
      const pollDelay = Math.min(250, 80 + Math.floor((Date.now() - started) / 500) * 50);
      await new Promise(resolve => setTimeout(resolve, pollDelay));
    }}
  }} catch (err) {{
    if (!current() || err.name === "AbortError") return;
    candidateSearchProgress.textContent = `查询失败：${{err.message || String(err)}}${{candidateRawItems.length ? " · 保留已返回结果" : ""}}`;
    if (!candidateRawItems.length) candidateRows.innerHTML = `<tr><td colspan="8" class="bad">${{esc(err.message || String(err))}}</td></tr>`;
  }} finally {{
    if (candidateSearchController === controller) candidateSearchController = null;
  }}
}}

async function chooseCandidate(code) {{
  if (!candidateTargetRow || !code) return;
  const tr = candidateTargetRow;
  if (tr.dataset.candidateSaving === "1") return;
  let candidate = currentCandidateItems.get(normalizeCCode(code));
  if (!candidate) throw new Error("没有找到候选料数据，请重新搜索");
  const searchVersion = candidateSearchToken;
  const pendingParameters = candidateParameterRequests.get(candidateParameterKey(candidate));
  if (pendingParameters && !pendingParameters.done) {{
    await pendingParameters.promise;
    if (tr.dataset.candidateSaving === "1") return;
    if (searchVersion !== candidateSearchToken || candidateTargetRow !== tr) throw new Error("搜索已变化，请重新选择候选料");
    candidate = currentCandidateItems.get(normalizeCCode(code)) || candidate;
  }}
  const anchor = rowAnchor(tr);
  tr.dataset.candidateSaving = "1";
  candidateSearchToken += 1;
  if (candidateSearchController) candidateSearchController.abort();
  candidateSearchController = null;
  candidateSearchProgress.textContent = "正在保存主料，请稍候…";
  const buttons = candidateRows.querySelectorAll('button[data-action="choose-candidate"]');
  buttons.forEach(button => button.disabled = true);
  try {{
    const footprintMode = typeof candidateFootprintMode === "undefined" ? "keep" : candidateFootprintMode.value;
    const replaceValue = typeof candidateReplaceValue !== "undefined" && candidateReplaceValue.value === "true";
    const replaceComment = typeof candidateReplaceComment !== "undefined" && candidateReplaceComment.value === "true";
    const res = await fetch(footprintMode === "convert" ? "/api/start_selection" : "/api/select_candidate", {{
      method: "POST", headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{ref: tr.dataset.ref, refs: tr.dataset.refs, candidate, footprint_mode: footprintMode,
                            replace_value: replaceValue, replace_comment: replaceComment,
                            phase: footprintMode === "keep" ? "save" : "preview"}})
    }});
    let data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || "保存候选料失败");
    if (data.job_id) {{
      const jobId = data.job_id;
      tr.dataset.selectionJob = jobId;
      const stages = {{queued: "等待转换", converting: "下载并转换符号、封装", validating: "校验转换结果", previewing: "生成 KiCad 封装对比预览", registering: "注册工程库", saving: "保存选料与字段", complete: "保存完成", failed: "转换失败"}};
      while (!data.done) {{
        candidateSearchProgress.textContent = stages[data.stage] || data.message || "正在转换…";
        await new Promise(resolve => setTimeout(resolve, 500));
        try {{
          const status = await fetch("/api/selection_job?id=" + encodeURIComponent(jobId));
          if (!status.ok) throw new Error("状态服务暂时不可用");
          data = await status.json();
        }} catch (pollError) {{
          candidateSearchProgress.textContent = "连接中断，后台任务可能仍在执行；正在重连，请勿重复提交。任务：" + jobId;
          await new Promise(resolve => setTimeout(resolve, 1500));
          continue;
        }}
        if (!data.ok) throw new Error(data.error || "转换失败");
      }}
      data = data.result;
      if (!data || !data.ok) throw new Error("转换任务未保存选料");
    }}
    if (data.preview) {{
      candidateSearchProgress.textContent = "等待封装选择确认；取消不会修改选料记录";
      let choice = await (["library", "rename"].includes(data.preview.kind) ? window.chooseLibraryFootprint(data.preview) : window.chooseFootprint(data.preview));
      if (!choice) {{
        candidateSearchProgress.textContent = "已取消封装选择，原选料与工程库未修改";
        return;
      }}
      while (choice) {{
        const saved = await fetch("/api/select_candidate", {{
          method: "POST", headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{footprint_choice: choice}})
        }});
        data = await saved.json();
        if (!saved.ok || !data.ok) throw new Error(data.error || "保存封装选择失败");
        if (!data.footprint_conflict) break;
        candidateSearchProgress.textContent = "已有同名封装，等待选择覆盖或另存";
        choice = await window.resolveFootprintConflict(data.footprint_conflict, choice);
        if (!choice) {{
          candidateSearchProgress.textContent = "已取消，原选料与工程库未修改";
          return;
        }}
      }}
    }}
    delete tr.dataset.candidateSaving;
    if (candidateOnly) await window.notifyNativeCandidateSaved();
    closeCandidates(true);
    setState("主料已保存；正在刷新分组…", "saved");
    if (!candidateOnly) await loadRows(anchor);
    const fieldSummary = "Value：" + (replaceValue ? "按选型更新" : "保留原值") + "；Comment：" + (replaceComment ? "按器件类型更新" : "保留原值");
    const librarySummary = data.library && data.library.symbol ? "已保存转换符号与封装库" : "已采用现有库封装";
    const visualSummary = data.library && data.library.visual_differences?.length ? "；丝印/用户图形按基准封装统一" : "";
    setState(footprintMode !== "keep" ? librarySummary + visualSummary + "；" + fieldSummary + "。请同步原理图；已放置符号与 PCB 未自动更换" : "已保存主料及物料参数；" + fieldSummary + "；保留现有 KiCad 封装。请同步原理图", "saved");
  }} catch (err) {{
    candidateSearchProgress.textContent = "保存主料失败：" + (err.message || String(err));
    throw err;
  }} finally {{
    delete tr.dataset.selectionJob;
    delete tr.dataset.candidateSaving;
    buttons.forEach(button => button.disabled = false);
  }}
}}

async function selectProcurement(quoteKey) {{
  if (!candidateTargetRow || !quoteKey) return;
  const quote = currentProcurementQuotes.get(quoteKey);
  if (!quote) throw new Error("没有找到订货报价数据，请重新搜索");
  const {{item, channel}} = quote;
  const tr = candidateTargetRow;
  await updateProcurementChoice(tr, "select", {{
    source: channel.channel_type === "overseas" ? "lcsc_overseas" : "lcsc_partner",
    supplier: channel.supplier || channel.name || "",
    supplier_sku: channel.supplier_sku || channel.vendor_code || "",
    manufacturer_part_number: channel.provider_model || channel.model || item.model || "",
    package: channel.package || item.package || "",
    batch: channel.batch_number || "",
    order_url: channel.url || "",
    related_lcsc_code: channel.c_code || item.c_code || ""
  }});
  closeCandidates(true);
}}

async function addCandidateAsAlternate(code) {{
  if (!candidateTargetRow) return;
  const tr = candidateTargetRow;
  if (tr.dataset.candidateSaving === "1") return;
  const normalized = normalizeCCode(code);
  let item = currentCandidateItems.get(normalized);
  if (!item) throw new Error("没有找到候选料数据");
  const searchVersion = candidateSearchToken;
  const pendingParameters = candidateParameterRequests.get(candidateParameterKey(item));
  if (pendingParameters && !pendingParameters.done) {{
    await pendingParameters.promise;
    if (tr.dataset.candidateSaving === "1") return;
    if (searchVersion !== candidateSearchToken || candidateTargetRow !== tr) throw new Error("搜索已变化，请重新选择候选料");
    item = currentCandidateItems.get(normalized) || item;
  }}
  tr.dataset.candidateSaving = "1";
  try {{
    await updateAlternateParts(tr, "add", {{
    c_code: normalized,
    part: {{
      c_code: normalized,
      model: item.model || "",
      package: item.package || "",
      param: [item.param || "", item.param_detail || ""].filter((value, index, values) => value && values.indexOf(value) === index).join("; "),
      brand: item.brand || "",
      param_detail: item.param_detail || "",
      stock: item.stock || "",
      moq: item.moq || "",
      mpq: item.mpq || "",
      price: item.price || "",
      price_ladder: item.price_ladder || "",
      delivery: item.delivery || "",
      channel: item.channel || "",
      arrange: item.arrange || "",
      category: item.category || "",
      image_url: item.image_url || "",
      image_url_big: item.image_url_big || "",
      url: item.url || ""
    }}
    }});
  }} finally {{ delete tr.dataset.candidateSaving; }}
  closeCandidates(true);
}}

async function loadStockProgressively(rows) {{
  const run = beginStockQueryRun();
  const codes = Array.from(new Set(rows.flatMap(row => [stockCode(row), ...alternateCodes(row.alternate_parts)]).filter(Boolean)));
  if (!codes.length) {{
    if (isActiveStockRun(run)) stockQueryController = null;
    return;
  }}
  let done = 0;
  const queue = [...codes];
  const workerCount = Math.min(4, queue.length);
  setState(`表格已显示，正在查询库存 0/${{codes.length}}`, "status");
  setStockProgress(0, codes.length);
  async function worker() {{
    while (queue.length && isActiveStockRun(run)) {{
      const code = queue.shift();
      setState(`表格已显示，正在查询库存 ${{done}}/${{codes.length}}：${{code}}`, "status");
      setStockProgress(done, codes.length, code);
      updateRowsForStock(code, {{status: "查询中", c_code: code}});
      try {{
        const res = await fetch(`/api/stock?code=${{encodeURIComponent(code)}}`, {{signal: run.signal}});
        const data = await res.json();
        if (!isActiveStockRun(run)) return;
        updateRowsForStock(code, data.stock || {{status: "查询失败", c_code: code}});
      }} catch (err) {{
        if (err.name === "AbortError") return;
        if (!isActiveStockRun(run)) return;
        updateRowsForStock(code, {{status: "查询失败", c_code: code, error: err.message || String(err)}});
      }} finally {{
        if (!isActiveStockRun(run)) return;
        done += 1;
        setState(`表格已显示，正在查询库存 ${{done}}/${{codes.length}}${{done === codes.length ? "：完成" : ""}}`, done === codes.length ? "saved" : "status");
        setStockProgress(done, codes.length, "", null, done === codes.length);
      }}
    }}
  }}
  await Promise.all(Array.from({{length: workerCount}}, worker));
  if (isActiveStockRun(run)) clearStockProgress();
}}

async function queryAllStockFromTable(mode = "missing") {{
  const run = beginStockQueryRun();
  const refreshAll = mode === "all";
  const activeButton = refreshAll ? queryAllButton : queryMissingButton;
  const label = refreshAll ? "刷新全部库存" : "查询未查库存";
  setState(`${{label}}：正在整理待查询项`, "status");
  setStockProgress(0, 0, "正在整理待查询项");
  const trs = Array.from(document.querySelectorAll("tr[data-ref]"));
  const codeMap = new Map();
  let skipped = 0;
  for (const tr of trs) {{
    const row = rowDataForElement(tr) || {{}};
    if (Array.isArray(row.stock?.entries)) {{
      for (const entry of row.stock.entries) {{
        const code = normalizeCCode(entry.c_code);
        if (!code) continue;
        if (!refreshAll && stockAlreadyQueried(entry.stock || {{}})) {{ skipped += 1; continue; }}
        if (!codeMap.has(code)) codeMap.set(code, new Set());
        codeMap.get(code).add(tr);
      }}
    }}
    const inputCode = normalizeCCode(tr.querySelector("[data-field='replacement_c_code']")?.value || "");
    const effectiveCode = normalizeCCode(tr.querySelector("[data-effective-code]")?.textContent || "");
    const originalCodeValue = normalizeCCode(tr.dataset.originalCode);
    const code = inputCode || effectiveCode || originalCodeValue;
    if (!code) {{
      setStockCells(tr, {{status: "请提供料号", c_code: "", error: "请提供料号"}});
    }} else {{
      if (refreshAll || tr.dataset.stockQueried !== "1") {{
        if (!codeMap.has(code)) codeMap.set(code, new Set());
        codeMap.get(code).add(tr);
      }} else {{
        skipped += 1;
      }}
    }}
    for (const alternate of (row.alternate_parts || [])) {{
      const alternateCode = normalizeCCode(alternate.c_code);
      if (!alternateCode) continue;
      if (!refreshAll && stockAlreadyQueried(alternate.stock || {{}})) {{
        skipped += 1;
        continue;
      }}
      if (!codeMap.has(alternateCode)) codeMap.set(alternateCode, new Set());
      codeMap.get(alternateCode).add(tr);
    }}
  }}
  const codes = Array.from(codeMap.keys());
  if (!codes.length) {{
    const message = refreshAll ? "没有可查询的 C 编码" : `没有未查询库存的 C 编码；已跳过 ${{skipped}} 行`;
    setState(message, refreshAll ? "error" : "saved");
    stockQueryController = null;
    resetQueryAllButton();
    setStockProgress(0, 0, "", null, false, false);
    return;
  }}
  let done = 0;
  const queue = [...codes];
  const workerCount = Math.min(4, queue.length);
  queryMissingButton.disabled = true;
  queryAllButton.disabled = true;
  setToolbarLabel(activeButton, "查询中");
  const skippedText = skipped ? `，跳过已查 ${{skipped}} 行` : "";
  setState(`${{label}} 0/${{codes.length}}${{skippedText}}`, "status");
  setStockProgress(0, codes.length);
  async function worker() {{
    while (queue.length && isActiveStockRun(run)) {{
      const code = queue.shift();
      setState(`${{label}} ${{done}}/${{codes.length}}：${{code}}${{skippedText}}`, "status");
      setToolbarLabel(activeButton, "查询中");
      setStockProgress(done, codes.length, code, codeMap.get(code));
      updateRowsForStock(code, {{status: "查询中", c_code: code}});
      try {{
        const res = await fetch(`/api/stock?code=${{encodeURIComponent(code)}}`, {{signal: run.signal}});
        const data = await res.json();
        if (!isActiveStockRun(run)) return;
        updateRowsForStock(code, data.stock || {{status: "查询失败", c_code: code}});
      }} catch (err) {{
        if (err.name === "AbortError") return;
        if (!isActiveStockRun(run)) return;
        updateRowsForStock(code, {{status: "查询失败", c_code: code, error: err.message || String(err)}});
      }} finally {{
        if (!isActiveStockRun(run)) return;
        done += 1;
        setState(`${{label}} ${{done}}/${{codes.length}}${{done === codes.length ? "：完成" : ""}}${{skippedText}}`, done === codes.length ? "saved" : "status");
        setToolbarLabel(activeButton, done === codes.length ? (refreshAll ? "刷新全部" : "查询未查") : "查询中");
        setStockProgress(done, codes.length, "", null, done === codes.length);
      }}
    }}
  }}
  try {{
    await Promise.all(Array.from({{length: workerCount}}, worker));
  }} finally {{
    if (isActiveStockRun(run)) {{
      stockQueryController = null;
      resetQueryAllButton();
      if (done === codes.length) clearStockProgress();
    }}
  }}
}}

async function loadRows(anchor = null) {{
  const version = ++rowsLoadVersion;
  const bom = bomSelect.value || "";
  try {{ await flushRowEdits(); }} catch (error) {{ setState(error.message, "error"); return; }}
  if (version !== rowsLoadVersion || bom !== (bomSelect.value || "")) return;
  rowsLoading = true;
  updateRowsEmptyState();
  if (loadedRowsBom !== bom) {{
    rowsEl.innerHTML = "";
    currentRows = [];
  }}
  syncAllButton.disabled = true;
  updateSyncSelection();
  cancelStockQueries("已停止库存查询，正在加载 BOM...");
  queryMissingButton.disabled = true;
  queryAllButton.disabled = true;
  setState("加载表格中...");
  try {{
    const refs = refsInput.value.trim();
    const res = await fetch(`/api/rows?refs=${{encodeURIComponent(refs)}}&bom=${{encodeURIComponent(bom)}}&stock=0`);
    const data = await res.json();
    if (version !== rowsLoadVersion || bom !== (bomSelect.value || "")) return;
    if (!res.ok) throw new Error(data.error || "加载 BOM 失败");
    loadedRowsBom = bom;
    renderRows(data.rows);
    restoreRowAnchor(anchor);
    const refCount = data.rows.flatMap(row => row.refs_list || [row.ref]).length;
    setState(`已加载 ${{data.rows.length}} 个分组 / ${{refCount}} 个位号；库存需手动查询`, "saved");
  }} catch (err) {{
    if (version !== rowsLoadVersion) return;
    setState(`加载 BOM 失败：${{err.message || String(err)}}`, "error");
  }} finally {{
    if (version === rowsLoadVersion) {{
      rowsLoading = false;
      updateRowsEmptyState();
      syncAllButton.disabled = bulkSyncRunning || loadedRowsBom !== (bomSelect.value || "");
      updateSyncSelection();
      resetQueryAllButton();
    }}
  }}
}}

async function loadBomOptions() {{
  const res = await fetch("/api/boms");
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "读取数据源失败");
  if (!Array.isArray(data.boms) || !data.boms.length) throw new Error("没有可用的数据源");
  let selectedBom = data.default_bom;
  try {{
    const intent = JSON.parse(sessionStorage.getItem("partSelectionSourceOnce") || "null");
    sessionStorage.removeItem("partSelectionSourceOnce");
    if (intent?.project === data.project_root && data.boms.some(item => item.path === intent.source)) selectedBom = intent.source;
  }} catch (error) {{ /* A stale or unavailable session store must not change the startup source. */ }}
  bomSelect.innerHTML = data.boms.map(item => `<option value="${{esc(item.path)}}" ${{item.path === selectedBom ? "selected" : ""}}>${{esc(item.name)}}</option>`).join("");
  updateUiSettings({{selectedBom}});
}}

async function initializeWorkbenchRows() {{
  const refresh = document.getElementById("refreshSchematicRows");
  refresh.disabled = true;
  try {{
    setState("正在读取数据源…");
    await loadBomOptions();
    await loadRows();
  }} catch (error) {{
    setState(`读取数据源失败：${{error.message || String(error)}}；可点击“重新读取”重试`, "error");
  }} finally {{ refresh.disabled = false; }}
}}

const rowEditQueue = createRowEditQueue({{
  save: persistRowInput,
  onIdle: regroupRowsAfterAutosave,
  onError: error => setState(`自动保存失败，编辑内容已保留：${{error.message}}`, "error")
}});

function rowInputKey(input) {{
  return `${{loadedRowsBom}}|${{input.closest("tr")?.dataset.refs}}|${{input.dataset.field}}`;
}}

function queueRowInput(input) {{
  if (!input.matches("textarea[data-field], input[data-field]")) return;
  const tr = input.closest("tr");
  if (!tr || input.dataset.composing === "1" || (input.value === input.dataset.savedValue && !rowEditQueue.has(rowInputKey(input)))) return;
  tr.classList.add("changed");
  if (["comment", "value", "footprint"].includes(input.dataset.field)) {{
    rowsNeedRegroup = true;
    rowGroupRefreshAnchor ||= rowAnchor(tr);
  }}
  rowEditQueue.schedule(rowInputKey(input), {{tr, input, refs: tr.dataset.refs, field: input.dataset.field, value: input.value,
    resolvedValue: input.dataset.field === "footprint" ? footprintFieldValue(input) : input.value}});
}}

async function flushRowEdits() {{
  await rowEditQueue.flush();
  if (rowEditQueue.pending) throw new Error("请先完成输入法编辑，再继续操作");
}}

async function persistRowInput(entry) {{
  const {{tr, input, refs, field}} = entry;
  const value = (entry.resolvedValue ?? entry.value).trim();
  const payload = {{refs, bom: loadedRowsBom}};
  let endpoint = "/api/field_override";
  if (field === "replacement_c_code") {{
    if (value && !/^C[0-9]+$/i.test(value)) throw new Error("请输入完整 C 编码，例如 C49678");
    const code = value.toUpperCase();
    Object.assign(payload, {{decision: code ? "AcceptManual" : "ClearSelection", accepted_c_code: code,
      manual_c_code: code, stock_c_code: code, status: code ? "ChangedToNewCode" : "Rejected", review_status: "Pending"}});
    endpoint = "/api/override";
  }} else if (field === "note") {{
    payload.note = value;
    endpoint = "/api/override";
  }} else {{ payload.field_overrides = {{[field]: value}}; }}
  const response = await fetch(endpoint, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(payload)}});
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
  input.dataset.savedValue = entry.value;
  if (field === "footprint" && input.value === entry.value) {{
    const parts = footprintParts(value);
    input.dataset.footprintLibrary = parts.library;
    input.value = input.dataset.savedValue = parts.name;
    input.title = `完整封装: ${{value}}；可输入 库名:封装名 切换库`;
  }}
  const row = currentRows.find(row => row.refs === refs);
  if (row) {{
    if (field === "note") {{
      if (value !== String(row.note || "").trim()) {{
        row.notes_by_ref = Object.fromEntries(refs.split(",").map(ref => [ref, value]));
        for (const selection of Object.values(row.per_ref_selection || {{}})) selection.note = value;
      }}
      row.note = value;
    }}
    else if (field === "replacement_c_code") {{
      const code = value.toUpperCase();
      row.effective_c_code = row.stock_c_code = row.manual_c_code = code;
      row.override = {{...row.override, ...payload}};
      row.review_status = "Pending";
      row.review_status_mixed = row.selection_codes_mixed = false;
      for (const selection of Object.values(row.per_ref_selection || {{}})) {{
        Object.assign(selection, {{effective_c_code: code, decision: payload.decision, status: payload.status, review_status: "Pending"}});
      }}
      row.stock = {{c_code: code, status: "待查询"}};
      for (const fields of Object.values(row.per_ref_sync_fields || {{}})) {{ fields.lcsc_c_code = code; fields.lcsc_url = lcscUrl(code); }}
      if (document.body.contains(tr)) {{
        tr.dataset.originalCode = "";
        tr.dataset.stockCode = code;
        tr.dataset.stockQueried = "0";
        tr.querySelector("[data-effective-code]").textContent = code || "未选料";
        tr.querySelector("[data-field='status']").value = payload.status;
        tr.querySelector("[data-field='review_status']").value = "Pending";
        tr.querySelector("[data-stock-cell]").innerHTML = renderStockCell(row.stock);
        tr.querySelector("[data-stock-param]").innerHTML = "";
        tr.querySelector("[data-stock-param]").dataset.stockParamText = "";
        tr.querySelector("[data-stock-order]").textContent = "";
        tr.querySelector("[data-stock-price]").textContent = "";
        const pill = tr.querySelector("[data-review-state]");
        if (pill) {{ pill.className = `review-pill ${{reviewStatusClass("Pending")}}`; pill.textContent = reviewStatusText("Pending"); }}
        setCodeState(tr, payload.status);
      }}
    }} else {{
      row[field] = value;
      for (const fields of Object.values(row.per_ref_sync_fields || {{}})) fields[field] = value;
      row.override = {{...row.override, field_overrides: {{...row.override?.field_overrides, [field]: value}}}};
    }}
    if (field !== "note") {{
      if (row.schematic_fields_by_ref) updateSavedFieldDiffs(row, field === "replacement_c_code" ? ["lcsc_c_code", "lcsc_url"] : [field]);
      else {{ row.field_changed_refs = refs; row.field_edit_status = "NeedsSchematicUpdate"; }}
    }}
  }}
  if (field !== "note" && document.body.contains(tr)) markRowNeedsSchematicSync(tr, refs);
  if (field === "footprint") refreshFootprintLibraries();
  window.refreshLocalInventory?.();
  if (field === "replacement_c_code") window.refreshSpecificationWarnings?.(true);
  if (document.body.contains(tr) && !Array.from(tr.querySelectorAll("textarea[data-field], input[data-field]")).some(el => el.value !== el.dataset.savedValue)) tr.classList.remove("changed");
  const merged = (data.merged_into || []).map(item => item.target_refs.join(",") + (item.c_code ? "（" + item.c_code + "）" : "（订货料）")).join("、");
  setState(merged ? `已合并到 ${{merged}}，沿用目标行选型；请核对并同步原理图` : `已自动保存 ${{refs}} 的 ${{field}}`, "saved");
}}

rowsEl.addEventListener("input", event => {{
  if (!event.isComposing) queueRowInput(event.target);
}});
rowsEl.addEventListener("compositionstart", event => {{
  if (event.target.dataset.field) {{ event.target.dataset.composing = "1"; rowEditQueue.pause(rowInputKey(event.target)); }}
}});
rowsEl.addEventListener("compositionend", event => {{ delete event.target.dataset.composing; queueRowInput(event.target); }});
rowsEl.addEventListener("focusout", event => {{
  queueRowInput(event.target);
  rowEditQueue.flush().catch(error => setState(error.message, "error"));
}});
window.addEventListener("beforeunload", event => {{
  if (rowEditQueue.pending) {{ event.preventDefault(); event.returnValue = ""; }}
}});

bomSearch.addEventListener("input", applyActiveFilter);
document.getElementById("clearBomSearch").addEventListener("click", () => {{
  bomSearch.value = "";
  applyActiveFilter();
  bomSearch.focus();
}});
bomSearch.addEventListener("keydown", event => {{
  if (event.key === "Escape") {{
    bomSearch.value = "";
    applyActiveFilter();
  }}
}});
document.addEventListener("keydown", event => {{
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {{
    event.preventDefault();
    const target = candidateModal.classList.contains("open") && !candidateModal.classList.contains("minimized")
      ? candidateKeyword : bomSearch;
    target.focus();
    target.select();
  }}
}});

rowsEl.addEventListener("keydown", async event => {{
  if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;
  const tr = event.target.closest("tr");
  if (!tr) return;
  try {{
    if (event.target.matches("[data-field='replacement_c_code']")) {{
      event.preventDefault();
      queueRowInput(event.target);
      await flushRowEdits();
      return;
    }}
    if (event.target.matches(".field-edit") && !event.shiftKey) {{
      event.preventDefault();
      await saveFieldEdits(tr);
    }}
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});

rowsEl.addEventListener("change", async event => {{
  if (!event.target.matches("[data-field='status'], [data-field='review_status']")) return;
  const tr = event.target.closest("tr");
  const selectedValue = event.target.value;
  try {{
    await flushRowEdits();
    event.target.value = selectedValue;
    if (event.target.matches("[data-field='review_status']")) {{
      await saveReviewStatus(tr);
      return;
    }}
    await applyStatus(tr);
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});

rowsEl.addEventListener("click", async event => {{
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const tr = button.closest("tr");
  const action = button.dataset.action;
  try {{
    await flushRowEdits();
    if (action === "eng") await saveRow(tr, "AcceptEngineering", tr.dataset.engineering);
    if (action === "lcsc") await saveRow(tr, "AcceptLCSC", tr.dataset.lcsc);
    if (action === "query-stock") await queryRowStock(tr);
    if (action === "clear-primary") {{
      const input = tr.querySelector("[data-field='replacement_c_code']");
      input.value = "";
      queueRowInput(input);
      await flushRowEdits();
    }}
    if (action === "manual") {{
      const manual = tr.querySelector("[data-field='manual_c_code']").value.trim();
      await saveRow(tr, "AcceptManual", manual);
    }}
    if (action === "open-candidates") await openCandidates(tr);
    if (action === "value-parameters") await window.openValueParameters(tr.dataset.refs);
    if (action === "start-multi-split") startMultiSplit(currentRows.find(row => (row.group_id || row.ref) === tr.dataset.groupId) || rowDataForElement(tr), button.dataset.splitSource || "refs");
    if (action === "split-selected-refs") splitSelectedRefs(button.dataset.groupId || tr.dataset.groupId || "");
    if (action === "cancel-multi-split") cancelMultiSplit();
    if (action === "move-alternate") await updateAlternateParts(tr, "move", {{c_code: button.dataset.code || "", direction: button.dataset.direction || ""}});
    if (action === "remove-alternate") await updateAlternateParts(tr, "remove", {{c_code: button.dataset.code || ""}});
    if (action === "adopt-schematic-alternates") await updateAlternateParts(tr, "adopt_schematic");
    if (action === "clear-procurement") await updateProcurementChoice(tr, "clear");
    if (action === "adopt-schematic-procurement") await updateProcurementChoice(tr, "adopt_schematic");
    if (action === "sync-schematic") await syncSchematic(tr);
    if (action === "undo-workbench") await undoWorkbenchChanges(tr, button);
    if (action === "adopt-schematic-fields") await adoptSchematicFields(tr, button);
    if (action === "save-fields") await saveFieldEdits(tr);
    if (action === "reject") await saveRow(tr, "Reject", "");
    if (action === "save") await saveRow(tr, tr.querySelector("[data-field='status']").value || "NeedsReview", undefined);
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});

rowsEl.addEventListener("change", async event => {{
  const pageStart = event.target.closest("input[data-start-split-page]");
  if (pageStart) {{
    try {{ await flushRowEdits(); }} catch (error) {{ pageStart.checked = false; setState(error.message, "error"); return; }}
    const tr = pageStart.closest("tr");
    const row = rowDataForElement(tr);
    startMultiSplit(row, "page");
    for (const item of row.ref_links || []) if ((item.page || "Unknown") === pageStart.dataset.startSplitPage) multiSplitRefs.add(item.ref);
    renderRows(currentRows);
    return;
  }}
  const pageCheck = event.target.closest("input[data-multi-split-page]");
  if (pageCheck) {{
    const row = rowDataForElement(pageCheck.closest("tr"));
    for (const item of row?.ref_links || []) {{
      if ((item.page || "Unknown") !== pageCheck.dataset.multiSplitPage) continue;
      if (pageCheck.checked) multiSplitRefs.add(item.ref); else multiSplitRefs.delete(item.ref);
    }}
    pageCheck.closest("tr").querySelectorAll("[data-multi-split-ref]").forEach(input => {{ input.checked = multiSplitRefs.has(input.dataset.multiSplitRef); }});
    updateSplitPageChecks();
    return;
  }}
  const checkbox = event.target.closest("input[data-multi-split-ref]");
  if (!checkbox) return;
  const ref = checkbox.dataset.multiSplitRef || "";
  if (checkbox.checked) multiSplitRefs.add(ref);
  else multiSplitRefs.delete(ref);
  updateSplitPageChecks();
}});

function updateSplitPageChecks() {{
  rowsEl.querySelectorAll("[data-multi-split-page]").forEach(input => {{
    const row = rowDataForElement(input.closest("tr"));
    const refs = (row?.ref_links || []).filter(item => (item.page || "Unknown") === input.dataset.multiSplitPage).map(item => item.ref);
    const count = refs.filter(ref => multiSplitRefs.has(ref)).length;
    input.checked = count === refs.length && count > 0;
    input.indeterminate = count > 0 && count < refs.length;
  }});
}}

function closeRefContextMenu() {{
  refContextMenu.classList.remove("open");
  contextRefTarget = null;
}}

rowsEl.addEventListener("contextmenu", event => {{
  if (window.getSelection()?.toString().trim()) return;
  const refButtonEl = event.target.closest("[data-ref-link]");
  if (!refButtonEl) return;
  event.preventDefault();
  contextRefTarget = refButtonEl.dataset.refLink || "";
  refContextMenu.style.left = `${{event.clientX}}px`;
  refContextMenu.style.top = `${{event.clientY}}px`;
  refContextMenu.classList.add("open");
}});

refContextMenu.addEventListener("click", event => {{
  const button = event.target.closest("button[data-action='split-ref-row']");
  if (!button || !contextRefTarget) return;
  splitRefToStandaloneRow(contextRefTarget);
  closeRefContextMenu();
}});

document.addEventListener("click", event => {{
  if (!event.target.closest("#refContextMenu")) closeRefContextMenu();
}});

document.addEventListener("click", async event => {{
  const button = event.target.closest("[data-action='open-project-path']");
  if (!button) return;
  try {{
    const res = await fetch("/api/open_project_path", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: "{{}}"}});
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "打开工程目录失败");
    setState(`已打开工程目录：${{data.path || ""}}`, "saved");
  }} catch (err) {{
    setState(err.message || String(err), "error");
  }}
}});

candidateRows.addEventListener("click", async event => {{
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  if (button.dataset.action === "retry-candidate-parameters") {{
    const item = candidateRawItems.find(item => normalizeCCode(item.c_code) === button.dataset.code);
    if (item) {{
      candidateParameterRequests.delete(candidateParameterKey(item));
      delete item.detail_parameter_status;
      supplementCandidateParameters(candidateSearchToken, button.dataset.code);
      renderCandidateResults();
    }}
    return;
  }}
  try {{
    if (button.dataset.action === "choose-candidate") await chooseCandidate(button.dataset.code || "");
    if (button.dataset.action === "add-candidate-alternate") await addCandidateAsAlternate(button.dataset.code || "");
    if (button.dataset.action === "select-procurement") await selectProcurement(button.dataset.quoteKey || "");
  }} catch (err) {{
    setState(err.message, "error");
    if (candidateOnly) candidateSearchProgress.textContent = err.message;
  }} finally {{
    window.finishNativeCandidateAction?.();
  }}
}});

document.getElementById("openOfficialSearch").addEventListener("click", () => {{
  const keyword = candidateKeyword.value.trim();
  if (!keyword) {{
    candidateKeyword.focus();
    return;
  }}
  launchOfficialSearch(keyword);
}});
inStockOnly.addEventListener("change", renderCandidateResults);
candidateWindow.querySelectorAll("[data-candidate-filter]").forEach(button => {{
  button.addEventListener("click", () => {{
    candidateFilterSection = candidateFilterSection === button.dataset.candidateFilter ? "" : button.dataset.candidateFilter;
    candidateFilterOptionSearch.value = "";
    renderCandidateFilterPanel();
  }});
}});
candidateFilterOptionSearch.addEventListener("input", renderCandidateFilterPanel);
document.getElementById("closeCandidateFilters").addEventListener("click", () => {{
  candidateFilterSection = "";
  renderCandidateFilterPanel();
}});
candidateFilterPanel.addEventListener("change", event => {{
  const input = event.target.closest("[data-candidate-facet]");
  if (!input) return;
  const key = input.dataset.candidateFacet;
  const selected = candidateFilters.get(key) || new Set();
  if (input.checked) selected.add(input.value);
  else selected.delete(input.value);
  if (selected.size) candidateFilters.set(key, selected);
  else candidateFilters.delete(key);
  renderCandidateResults();
}});
document.getElementById("candidateFilterTags").addEventListener("click", event => {{
  const button = event.target.closest("button");
  if (!button) return;
  if (button.hasAttribute("data-remove-candidate-stock")) inStockOnly.checked = false;
  else {{
    const key = button.dataset.removeCandidateFilter;
    const selected = candidateFilters.get(key);
    selected?.delete(button.dataset.filterValue);
    if (!selected?.size) candidateFilters.delete(key);
  }}
  renderCandidateResults();
}});
document.getElementById("clearCandidateFilters").addEventListener("click", () => {{
  candidateFilters.clear();
  inStockOnly.checked = false;
  renderCandidateResults();
}});
candidateKeyword.addEventListener("input", invalidateCandidateSearch);
candidateKeyword.addEventListener("keydown", event => {{
  if (event.key === "Enter") {{
    event.preventDefault();
    searchCandidates();
  }}
}});
document.getElementById("minimizeCandidate").addEventListener("click", minimizeCandidates);
document.getElementById("maximizeCandidate").addEventListener("click", toggleCandidateFullscreen);
document.getElementById("closeCandidate").addEventListener("click", closeCandidates);
document.getElementById("candidateMinimizedBar").addEventListener("click", restoreCandidates);
candidateWindow.querySelector(".modal-head").addEventListener("pointerdown", beginCandidateMove);
candidateWindow.querySelectorAll("[data-resize]").forEach(handle => {{
  handle.addEventListener("pointerdown", beginCandidateResize);
}});
candidateModal.addEventListener("click", event => {{
  if (event.target === candidateModal) event.preventDefault();
}});
function updateFilterButtons() {{
  filterTabs.querySelectorAll("button[data-filter]").forEach(button => {{
    const group = button.dataset.filter || "all";
    if (group === "all") {{
      button.textContent = "全部";
      button.classList.toggle("active", activeFilters.size === 0);
      button.classList.remove("filter-negative");
      button.setAttribute("aria-pressed", activeFilters.size === 0 ? "true" : "false");
      button.title = "清除全部筛选";
      return;
    }}
    const cycle = FILTER_CYCLES[group];
    if (!cycle) return;
    const selectedKey = activeFilters.get(group) || "";
    const selected = cycle.find(item => item.key === selectedKey);
    button.textContent = selected ? selected.label : cycle[0].label;
    button.classList.toggle("active", Boolean(selected));
    button.classList.toggle("filter-negative", selectedKey === cycle[1].key);
    button.setAttribute("aria-pressed", selected ? "true" : "false");
    button.title = !selected
      ? `筛选：${{cycle[0].label}}`
      : selectedKey === cycle[0].key
        ? `切换为：${{cycle[1].label}}`
        : "取消此项筛选";
  }});
}}

filterTabs.addEventListener("click", event => {{
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  const group = button.dataset.filter || "all";
  if (group === "all") {{
    activeFilters.clear();
  }} else {{
    const cycle = FILTER_CYCLES[group];
    if (!cycle) return;
    const current = activeFilters.get(group) || "";
    if (!current) activeFilters.set(group, cycle[0].key);
    else if (current === cycle[0].key) activeFilters.set(group, cycle[1].key);
    else activeFilters.delete(group);
  }}
  updateFilterButtons();
  applyActiveFilter();
}});

updateFilterButtons();

document.getElementById("reloadRows")?.addEventListener("click", loadRows);
async function startStockQuery(mode) {{
  try {{
    await flushRowEdits();
    await queryAllStockFromTable(mode);
  }} catch (err) {{
    resetQueryAllButton();
    setStockProgress(0, 0, "", null, false, false);
    setState(`库存查询未启动：${{err.message || String(err)}}`, "error");
  }}
}}

document.getElementById("queryMissingStock").addEventListener("click", () => startStockQuery("missing"));
document.getElementById("queryAllStock").addEventListener("click", () => startStockQuery("all"));
syncAllButton.addEventListener("click", async () => {{
  try {{
    await flushRowEdits();
    await syncAllSchematic();
  }} catch (err) {{
    setState(err.message, "error");
    syncAllButton.disabled = false;
  }}
}});
syncSelectedButton.addEventListener("click", async () => {{
  try {{
    await flushRowEdits();
    await syncAllSchematicWithProgress(true);
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});
document.getElementById("exportChanges").addEventListener("click", async () => {{
  try {{
    await flushRowEdits();
    await exportChanges();
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});
document.getElementById("exportPumpTable").addEventListener("click", async () => {{
  closeToolbarMenus();
  try {{
    await flushRowEdits();
    await exportPumpTable();
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});
bomSelect.addEventListener("change", () => {{
  selectedRowRefs.clear();
  updateUiSettings({{selectedBom: bomSelect.value || ""}});
  refsInput.value = "";
  loadRows();
}});

document.getElementById("autoSizeColumns").addEventListener("click", () => {{
  autoSizeColumns(false);
}});
document.getElementById("toggleColumns").addEventListener("click", () => {{
  closeToolbarMenus();
  hideToolbarTooltip();
  renderColumnPanel();
  columnPanel.classList.toggle("open");
  document.getElementById("toggleColumns").setAttribute("aria-expanded", String(columnPanel.classList.contains("open")));
  positionToolbarPopups();
}});
function closeColumnPanel() {{
  columnPanel.classList.remove("open");
  document.getElementById("toggleColumns").setAttribute("aria-expanded", "false");
}}

function closeToolbarMenus() {{
  document.querySelectorAll(".app-menu.open").forEach(menu => {{
    const trigger = menu.querySelector("[data-menu-trigger]");
    const restoreFocus = menu.contains(document.activeElement) && document.activeElement !== trigger;
    menu.classList.remove("open");
    trigger?.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger?.focus();
  }});
}}

function positionToolbarPopups() {{
  function position(popup, trigger) {{
    if (!popup || !trigger) return;
    const rect = trigger.getBoundingClientRect();
    popup.style.position = "fixed";
    popup.style.right = "auto";
    popup.style.maxWidth = `${{Math.max(0, document.documentElement.clientWidth - 16)}}px`;
    popup.style.maxHeight = `${{Math.max(80, window.innerHeight - rect.bottom - 12)}}px`;
    popup.style.overflowY = "auto";
    popup.style.left = `${{Math.max(8, Math.min(rect.left, document.documentElement.clientWidth - popup.getBoundingClientRect().width - 8))}}px`;
    popup.style.top = `${{rect.bottom + 3}}px`;
  }}
  document.querySelectorAll(".app-menu.open").forEach(menu => position(menu.querySelector(".menu-popup"), menu.querySelector("[data-menu-trigger]")));
  if (columnPanel.classList.contains("open")) position(columnPanel, document.getElementById("toggleColumns"));
}}

let toolbarTooltipTimer = null;
let toolbarTooltipButton = null;
function hideToolbarTooltip() {{
  window.clearTimeout(toolbarTooltipTimer);
  document.getElementById("toolbarTooltip").hidden = true;
  toolbarTooltipButton?.removeAttribute("aria-describedby");
  toolbarTooltipButton = null;
}}

function installToolbarTooltips() {{
  const toolbar = document.querySelector(".toolbar");
  function show(button) {{
    if (!button || toolbarTooltipButton === button) return;
    hideToolbarTooltip();
    toolbarTooltipButton = button;
    toolbarTooltipTimer = window.setTimeout(() => {{
      const tooltip = document.getElementById("toolbarTooltip");
      const title = document.createElement("strong");
      title.textContent = button.getAttribute("aria-label");
      const detail = document.createElement("span");
      detail.textContent = button.dataset.tooltip;
      tooltip.replaceChildren(title, detail);
      tooltip.hidden = false;
      const rect = button.getBoundingClientRect();
      tooltip.style.left = `${{Math.max(8, Math.min(rect.left, document.documentElement.clientWidth - tooltip.offsetWidth - 8))}}px`;
      tooltip.style.top = `${{rect.bottom + 7}}px`;
      button.setAttribute("aria-describedby", "toolbarTooltip");
    }}, 350);
  }}
  toolbar.addEventListener("pointerover", event => show(event.target.closest("[data-tooltip]")));
  toolbar.addEventListener("focusin", event => show(event.target.closest("[data-tooltip]")));
  for (const eventName of ["pointerout", "focusout"]) toolbar.addEventListener(eventName, event => {{
    if (toolbarTooltipButton && !toolbarTooltipButton.contains(event.relatedTarget)) hideToolbarTooltip();
  }});
  toolbar.addEventListener("click", hideToolbarTooltip);
  window.addEventListener("scroll", hideToolbarTooltip, {{passive: true}});
}}

document.querySelectorAll("[data-menu-trigger]").forEach((trigger, index) => {{
  const popup = trigger.closest(".app-menu").querySelector(".menu-popup");
  popup.id = `workbenchMenu${{index}}`;
  trigger.setAttribute("aria-controls", popup.id);
  trigger.setAttribute("aria-haspopup", "true");
  const menu = trigger.closest(".app-menu");
  const commandMenu = !popup.querySelector("select, input");
  if (commandMenu) {{
    popup.setAttribute("role", "menu");
    popup.setAttribute("aria-label", trigger.getAttribute("aria-label") || trigger.textContent.trim());
    popup.querySelectorAll("button").forEach(item => {{ item.setAttribute("role", "menuitem"); item.tabIndex = -1; }});
  }}
  const controls = () => Array.from(popup.querySelectorAll("button, select, input, a[href]"))
    .filter(item => !item.disabled && item.getClientRects().length);
  function openMenu(last = false) {{
    closeToolbarMenus();
    closeColumnPanel();
    hideToolbarTooltip();
    menu.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    positionToolbarPopups();
    const items = controls();
    (last ? items.at(-1) : items[0])?.focus();
  }}
  menu.addEventListener("keydown", event => {{
    if (event.key === "Tab" && commandMenu && menu.classList.contains("open")) {{
      trigger.focus();
      closeToolbarMenus();
      return;
    }}
    const atTrigger = event.target === trigger;
    if (atTrigger && ["ArrowDown", "ArrowUp"].includes(event.key)) {{
      event.preventDefault();
      openMenu(event.key === "ArrowUp");
      return;
    }}
    // Keep native select/input editing keys; Escape is handled by the document.
    if (!atTrigger && !event.target.matches("button")) return;
    if (["ArrowLeft", "ArrowRight"].includes(event.key) && menu.closest(".app-menubar")) {{
      event.preventDefault();
      const triggers = Array.from(document.querySelectorAll(".app-menubar [data-menu-trigger]"));
      const next = triggers[(triggers.indexOf(trigger) + (event.key === "ArrowRight" ? 1 : triggers.length - 1)) % triggers.length];
      const wasOpen = menu.classList.contains("open");
      closeToolbarMenus();
      next.focus();
      if (wasOpen) next.dispatchEvent(new KeyboardEvent("keydown", {{key:"ArrowDown", bubbles:true, cancelable:true}}));
    }} else if (!atTrigger && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {{
      event.preventDefault();
      const items = controls();
      const current = items.indexOf(document.activeElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
        : (current + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
      items[next]?.focus();
    }}
  }});
  menu.addEventListener("focusout", event => {{
    if (event.relatedTarget && !menu.contains(event.relatedTarget)) {{
      menu.classList.remove("open");
      trigger.setAttribute("aria-expanded", "false");
    }}
  }});
  trigger.addEventListener("click", event => {{
    const menu = trigger.closest(".app-menu");
    const open = !menu.classList.contains("open");
    closeToolbarMenus();
    closeColumnPanel();
    hideToolbarTooltip();
    menu.classList.toggle("open", open);
    trigger.setAttribute("aria-expanded", String(open));
    positionToolbarPopups();
    if (open && event.detail === 0) controls()[0]?.focus();
  }});
}});
document.getElementById("saveDefaultView").addEventListener("click", () => {{
  saveDefaultViewConfig();
  closeToolbarMenus();
}});

document.getElementById("openPathConfig").addEventListener("click", async () => {{
  closeToolbarMenus();
  try {{
    await flushRowEdits();
    await window.openPathSettings();
  }} catch (error) {{ setState(error.message || String(error), "error"); }}
}});
document.getElementById("loadDefaultView").addEventListener("click", () => {{
  try {{ loadDefaultViewConfig(); }} catch (err) {{ setState(`加载默认配置失败：${{err.message || String(err)}}`, "error"); }}
  closeToolbarMenus();
}});
document.getElementById("refreshSchematicRows").addEventListener("click", async () => {{
  if (!bomSelect.options.length) {{ await initializeWorkbenchRows(); closeToolbarMenus(); return; }}
  selectedRowRefs.clear();
  refsInput.value = "";
  bomSearch.value = "";
  activeFilters.clear();
  updateFilterButtons();
  applyActiveFilter();
  if (bomSelect.value !== "__schematic__") {{
    bomSelect.value = "__schematic__";
    updateUiSettings({{selectedBom: "__schematic__"}});
  }}
  loadRows();
  closeToolbarMenus();
}});
columnList.addEventListener("change", event => {{
  const input = event.target.closest("[data-column-toggle]");
  if (!input) return;
  const settings = loadUiSettings();
  const hidden = Object.assign({{}}, settings.hiddenColumns || {{}});
  hidden[input.dataset.columnToggle] = !input.checked;
  updateUiSettings({{hiddenColumns: hidden}});
  applyColumnVisibility();
  fitDefaultColumnWidths();
}});

let draggedColumnKey = "";
let draggedCandidateColumnKey = "";
columnList.addEventListener("dragstart", event => {{
  const row = event.target.closest("[data-column-row]");
  if (!row) return;
  draggedColumnKey = row.dataset.columnRow;
  row.classList.add("dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", draggedColumnKey);
}});
columnList.addEventListener("dragend", event => {{
  const row = event.target.closest("[data-column-row]");
  if (row) row.classList.remove("dragging");
  draggedColumnKey = "";
}});
columnList.addEventListener("dragover", event => {{
  if (!draggedColumnKey) return;
  event.preventDefault();
  const target = event.target.closest("[data-column-row]");
  if (!target || target.dataset.columnRow === draggedColumnKey) return;
  const dragging = columnList.querySelector(`[data-column-row="${{draggedColumnKey}}"]`);
  if (!dragging) return;
  const rect = target.getBoundingClientRect();
  const after = event.clientY > rect.top + rect.height / 2;
  columnList.insertBefore(dragging, after ? target.nextSibling : target);
}});
columnList.addEventListener("drop", event => {{
  if (!draggedColumnKey) return;
  event.preventDefault();
  const order = Array.from(columnList.querySelectorAll("[data-column-row]")).map(row => row.dataset.columnRow);
  saveColumnOrder(order);
  applyColumnOrder();
  applyColumnVisibility();
}});
document.addEventListener("click", event => {{
  const command = event.target.closest("[data-command]");
  if (command) {{
    closeToolbarMenus();
    document.getElementById(command.dataset.command)?.click();
    return;
  }}
  if (!event.target.closest(".view-wrap")) closeColumnPanel();
  if (!event.target.closest(".app-menu")) {{
    closeToolbarMenus();
  }}
}});
document.addEventListener("keydown", event => {{
  if (event.key !== "Escape") return;
  const trigger = document.querySelector('.app-menu.open [data-menu-trigger]')
    || (columnPanel.classList.contains("open") ? document.getElementById("toggleColumns") : null);
  closeToolbarMenus();
  closeColumnPanel();
  hideToolbarTooltip();
  trigger?.focus();
}});
window.addEventListener("storage", event => {{
  if (event.key !== UI_STORAGE_KEY) return;
  applyColumnOrder();
  applySavedColumnWidths();
  applyColumnVisibility();
  renderColumnPanel();
}});
window.addEventListener("scroll", scheduleStickyChromeUpdate, {{passive: true}});
window.addEventListener("resize", scheduleStickyChromeUpdate);
window.addEventListener("resize", () => {{
  fitDefaultColumnWidths();
  if (candidateModal.classList.contains("open") && !candidateModal.classList.contains("minimized")) fitDefaultColumnWidths(true);
}});
if (typeof ResizeObserver !== "undefined") {{
  let candidateBodyWidth = 0;
  new ResizeObserver(entries => {{
    const width = Math.round(entries[0].contentRect.width);
    if (width <= 0 || width === candidateBodyWidth) return;
    candidateBodyWidth = width;
    if (candidateModal.classList.contains("open") && !candidateModal.classList.contains("minimized")) fitDefaultColumnWidths(true);
  }}).observe(candidateWindow.querySelector(".candidate-body-wrap"));
}}

organizeToolbar();
installToolbarTooltips();
updateStickyChrome();
observeStickyChrome();
applyColumnOrder();
applySavedColumnWidths();
makeColumnsResizable();
makeCandidateColumnsConfigurable();
applyCandidateColumnOrder();
applyCandidateSavedWidths();
renderColumnPanel();
applyColumnVisibility();
if (!candidateOnly) window.workbenchRowFilters = window.RowFilters?.createRowFilters({{
  document, project: document.querySelector(".project-path-link").textContent,
  getSettings: loadUiSettings, saveSettings: updateUiSettings, onChange: applyActiveFilter
}});
if (!candidateOnly) initializeWorkbenchRows();
</script>
<script>{candidate_desktop_js}</script>
<script>{inventory_js}</script>
<script>{spec_warnings_js}</script>
<script>{value_parameters_js}</script>
<script>{footprint_js}</script>
<script>{path_settings_js}</script>
<script>{desktop_lifecycle_js}</script>
<script>{plugin_updates_js}</script>
</body>
</html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run editable KiCad part-selection helper.")
    parser.add_argument("--config", type=Path, help="Project configuration JSON")
    parser.add_argument("--project", type=Path, help="KiCad project file or directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--desktop-owner-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--bulk-sync-bom", default="", help="Sync the merged web-state of a BOM into KiCad schematics, then exit.")
    parser.add_argument("--bulk-sync-dry-run", action="store_true", help="Only report what the bulk schematic sync would do.")
    parser.add_argument("--bulk-sync-no-notify", action="store_true", help="Do not notify or activate KiCad after bulk schematic sync.")
    parser.add_argument("--bulk-sync-no-prepare", action="store_true", help="Do not ask KiCad to save open schematic windows before bulk sync.")
    args = parser.parse_args()
    from part_selection_paths import resolve_config
    try:
        args.config = resolve_config(args.project, args.config)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return args


def main() -> int:
    args = parse_args()
    app = PartSelectionApp(args.config)
    if args.bulk_sync_bom:
        result = app.bulk_sync_bom_state_to_schematic(
            args.bulk_sync_bom,
            ref_map={} if args.bulk_sync_bom == SCHEMATIC_SOURCE_BOM else app.config.get("bulk_sync_ref_map", DEFAULT_BULK_SYNC_REF_MAP),
            dry_run=args.bulk_sync_dry_run,
            notify=not args.bulk_sync_no_notify,
            prepare=not args.bulk_sync_no_prepare,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    if args.desktop_owner_pid:
        from part_selection_lifecycle import ServiceLifecycle
        token = os.environ.pop('KICAD_PART_SELECTION_DESKTOP_TOKEN', '')
        if args.desktop_owner_pid <= 0 or not token:
            server.server_close()
            raise ValueError('Desktop service requires a valid owner and token')
        app.lifecycle = ServiceLifecycle(app, server, token)
        threading.Thread(target=app.lifecycle.watch_owner, args=(args.desktop_owner_pid,),
                         daemon=True, name='desktop-owner').start()
    url = f"http://{args.host}:{args.port}/"
    print(f"Part selection helper: {url}")
    print(f"Config: {app.config['config_path']}")
    print(f"Overrides: {app.overrides_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        server.server_close()
        app.selection_jobs.close()
        app.search_jobs.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
