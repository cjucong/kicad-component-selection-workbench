# -*- coding: utf-8 -*-
"""Editable local web UI for KiCad part-selection review."""

from __future__ import annotations

import argparse
import csv
import ctypes
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import tempfile
import webbrowser
from dataclasses import asdict
from collections import OrderedDict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4

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
    MAX_ALTERNATE_PARTS,
    choose_effective_c_code,
    load_config,
    load_overrides,
    normalize_alternate_parts,
    normalize_lcsc_c_code,
    normalize_procurement_choice,
    normalize_schematic_field_snapshot,
    save_override,
    save_overrides,
)
from part_stock_lookup import (
    candidate_stock_result,
    enrich_rows_with_cached_stock,
    enrich_rows_with_stock,
    exact_c_code_keyword,
    find_chrome_executable,
    fetch_szlcsc_overseas_channels,
    load_stock_cache,
    lookup_stock_with_cache,
    save_stock_cache,
    search_lcsc_candidates,
    stock_cache_lock,
    stock_to_candidate_item,
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
GENERATED_REVIEW_BOM_PREFIXES = ("part_selection_change_log_", "part_selection_review_")
SCHEMATIC_SYNC_LOG = "part_selection_schematic_sync_log.csv"
SCHEMATIC_SYNC_RUNTIME_LOG = "part_selection_schematic_sync_runtime.jsonl"
SCHEMATIC_SYNC_RUNTIME_LOG_LOCK = threading.Lock()
SCHEMATIC_SYNC_TRANSACTION_LOCK = threading.Lock()
SCHEMATIC_NOTIFY_SETTLE_SECONDS = 0.25
SCHEMATIC_SOURCE_BOM = "__schematic__"
DEFAULT_BULK_SYNC_REF_MAP: dict[str, str] = {}


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
    for key in ("arrange", "category"):
        value = str(stock.get(key) or "")
        if value:
            pieces.append(value)
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
        cache[c_code] = cached
        save_stock_cache(cache_path, cache)


def row_effective_c_code(row: dict[str, object]) -> str:
    return str(
        row.get("stock_c_code")
        or row.get("effective_c_code")
        or row.get("accepted_c_code")
        or row.get("lcsc_match_c_code")
        or ""
    ).strip()


def row_review_status(row: dict[str, object]) -> str:
    return str(row.get("review_status") or (row.get("override") or {}).get("review_status") or "Pending")


def row_code_status(row: dict[str, object]) -> str:
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
    number = stock_number(stock)
    return bool(isinstance(stock, dict) and stock.get("ok") and number is not None and number < 100)


def row_stock_already_queried(row: dict[str, object]) -> bool:
    stock = row.get("stock")
    if not isinstance(stock, dict):
        return False
    status = str(stock.get("status") or "")
    wait_status = {"待查询", "查询中", "请提供料号", "寰呮煡璇?", "鏌ヨ涓?", "璇锋彁渚涙枡鍙?"}
    return bool(stock.get("ok") or stock.get("fetched_at") or (stock.get("c_code") and status and status not in wait_status))


def row_stock_is_error(row: dict[str, object]) -> bool:
    stock = row.get("stock")
    return bool(row_stock_already_queried(row) and isinstance(stock, dict) and not stock.get("ok"))


def row_stock_is_issue(row: dict[str, object]) -> bool:
    return row_stock_is_low(row) or row_stock_is_error(row)


def row_stock_is_normal(row: dict[str, object]) -> bool:
    stock = row.get("stock")
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
    if row_code_status(row) == "ChangedToNewCode" or row.get("manual_c_code") or row.get("stock_c_code"):
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
    return bool(
        row_effective_c_code(row)
        and not row_is_nc(row)
        and row_code_status(row) != "Rejected"
        and row_review_status(row) != "Rejected"
    )


def saved_candidate_description(row: dict[str, object]) -> str:
    fields = (row.get("override") or {}).get("field_overrides") or {}
    code = row_effective_c_code(row)
    if code and fields.get("description_c_code") == code:
        return str(fields.get("description") or "").strip()
    return ""


def candidate_selection_fields(item: dict[str, object], refs: list[str]) -> dict[str, str]:
    model = str(item.get("model") or "").strip()
    fields = {"comment": model}
    package = str(item.get("package") or "").strip()
    if package:
        fields["footprint"] = package
    prefix = re.match(r"[A-Z]+", refs[0].upper())
    kind = prefix.group() if prefix else ""
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
    # A packaging/category label alone is not an electrical description.
    if item.get("param") or item.get("param_detail"):
        fields["description"] = stock_param_text(item)
        fields["description_c_code"] = normalize_lcsc_c_code(item.get("c_code"))
    return fields


def row_schematic_sync_fields(row: dict[str, object]) -> dict[str, str]:
    fields = {
        "comment": str(row.get("comment") or "").strip(),
        "value": str(row.get("value") or "").strip(),
        "footprint": str(row.get("footprint") or "").strip(),
    }
    c_code = str(
        row.get("stock_c_code")
        or row.get("effective_c_code")
        or row.get("accepted_c_code")
        or row.get("lcsc_match_c_code")
        or ""
    ).strip()
    if c_code:
        fields["lcsc_c_code"] = c_code
        fields["lcsc_url"] = lcsc_code_url(c_code)
    description = saved_candidate_description(row) or stock_param_text(row.get("stock"))
    if description:
        fields["description"] = description
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
        changed_refs = set(split_refs(row.get("field_changed_refs", "")))
        missing_refs: list[str] = []
        field_diff_refs: dict[str, list[str]] = {}
        field_old_values: dict[str, list[str]] = {}
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
            for field, desired_value in desired.items():
                actual_value = str(actual.get(field, "") or "").strip()
                if actual_value == desired_value:
                    continue
                changed_refs.add(ref)
                field_diff_refs.setdefault(field, []).append(ref)
                if actual_value not in field_old_values.setdefault(field, []):
                    field_old_values[field].append(actual_value)

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


def group_parts_for_review(parts, overrides: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    grouped: OrderedDict[tuple[str, ...], dict[str, object]] = OrderedDict()
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
        key = (
            fields["comment"],
            fields["footprint"],
            fields["value"],
            alternate_parts_signature(alternate_parts),
            alternate_parts_signature(schematic_alternate_parts),
            procurement_choice_signature(procurement_choice),
            procurement_choice_signature(schematic_procurement_choice),
            saved_candidate_description({
                "override": override,
                "effective_c_code": choose_effective_c_code(asdict(part), override),
            }),
        )
        group = grouped.setdefault(
            key,
            {
                "comment": fields["comment"],
                "footprint": fields["footprint"],
                "value": fields["value"],
                "description_values": [],
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
            "effective_c_code": choose_effective_c_code(asdict(part), override),
        }) or part.description
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
        first_override = next((overrides.get(ref, {}) for ref in ref_list if overrides.get(ref)), {})
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
            "description": "; ".join(group["description_values"]),
            "original_comment_values": "; ".join(group["original_comment_values"]),
            "original_value_values": "; ".join(group["original_value_values"]),
            "original_footprint_values": "; ".join(group["original_footprint_values"]),
            "field_changed_refs": sorted_ref_text(group["field_changed_refs"]),
            "field_diffs": field_diffs,
            "field_edit_status": "NeedsSchematicUpdate" if group["field_changed_refs"] else "",
            "accepted_c_code": accepted_code,
            "lcsc_match_c_code": lcsc_code,
            "override": first_override,
            "note": first_override.get("note", ""),
            "decision": first_override.get("decision", ""),
            "manual_c_code": first_override.get("manual_c_code", ""),
            "stock_c_code": first_override.get("stock_c_code", ""),
            "schematic_synced_at": first_override.get("schematic_synced_at", ""),
            "schematic_sync_summary": first_override.get("schematic_sync_summary", ""),
            "status": first_override.get("status", ""),
            "review_status": first_override.get("review_status", ""),
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
            "c_code_search_url": lcsc_search_url(accepted_code or lcsc_code) if (accepted_code or lcsc_code) else "",
            "kicad_jump_url": kicad_jump_url(primary.ref, primary.page),
            "effective_c_code": choose_effective_c_code(
                {"accepted_c_code": accepted_code, "lcsc_match_c_code": lcsc_code},
                first_override,
            ),
        }
        row.update(alternate_parts_to_schematic_fields(row["alternate_parts"]))
        row.update(procurement_choice_to_schematic_fields(row["procurement_choice"]))
        rows.append(row)
    return rows


class PartSelectionApp:
    def __init__(self, config_path: Path):
        self.config = load_config(config_path)
        self.project_root = self.config["project_root"]
        self.bom_dir = self.project_root / "BOM"
        self.overrides_path = self.config["output_overrides"]
        self.stock_cache_path = self.config["stock_cache"]

    def source_bom(self) -> Path:
        return self.config["source_bom"]

    def resolve_bom_path(self, selected_bom: str = "") -> Path:
        if not selected_bom:
            return self.source_bom()
        if selected_bom == SCHEMATIC_SOURCE_BOM:
            return self.config["schematic_dir"]
        candidate = Path(selected_bom)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        candidate = candidate.resolve()
        project_root = self.project_root.resolve()
        if project_root not in [candidate, *candidate.parents]:
            raise ValueError("selected BOM must be inside the project directory")
        if not candidate.exists():
            raise FileNotFoundError(f"selected BOM not found: {candidate}")
        return candidate

    def available_boms(self) -> list[dict[str, str]]:
        files: list[Path] = []
        for pattern in ("*.csv", "*.xlsx", "*.xlsm"):
            files.extend(self.bom_dir.glob(pattern))
        result: list[dict[str, str]] = [{"name": "原理图当前数据", "path": SCHEMATIC_SOURCE_BOM}]
        for path in sorted(set(files), key=lambda item: item.name.lower()):
            if path.name == SCHEMATIC_SYNC_LOG or path.name.lower().startswith(GENERATED_REVIEW_BOM_PREFIXES):
                continue
            try:
                if not read_refs_from_bom(path):
                    continue
            except Exception:
                continue
            relative = path.relative_to(self.project_root).as_posix()
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
        if selected_bom == SCHEMATIC_SOURCE_BOM:
            return sorted(load_schematic_parts(self.config["schematic_dir"]).keys(), key=ref_sort_key)
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
        actual_by_ref = read_schematic_symbol_field_values(
            self.config["schematic_dir"], refs, SCHEMATIC_SYNC_FIELDS
        )
        changed, states = reconcile_schematic_field_overrides(parts, overrides, actual_by_ref)
        if changed:
            save_overrides(self.overrides_path, overrides)
        return actual_by_ref, states

    def rows(self, query_refs: str = "", selected_bom: str = "", include_stock: bool = True) -> list[dict[str, object]]:
        if selected_bom == SCHEMATIC_SOURCE_BOM:
            schematic = load_schematic_parts(self.config["schematic_dir"])
            refs = self.refs(query_refs, selected_bom)
            parts = [schematic[ref] for ref in refs if ref in schematic]
            overrides = load_overrides(self.overrides_path)
            actual_by_ref, reconciliation = self.reconcile_parts_with_schematic(parts, overrides)
            rows = group_parts_for_review(parts, overrides)
            for row in rows:
                row["source_mode"] = "schematic"
            if include_stock:
                enrich_rows_with_stock(rows, self.stock_cache_path)
            else:
                enrich_rows_with_cached_stock(rows, self.stock_cache_path)
            enrich_alternate_parts_with_cached_stock(rows, self.stock_cache_path)
            enrich_rows_with_schematic_sync_state(
                rows,
                self.config["schematic_dir"],
                actual_by_ref=actual_by_ref,
                reconciliation_by_ref=reconciliation,
            )
            return sort_bom_rows(rows)

        selected_path = self.resolve_bom_path(selected_bom)
        refs = self.refs(query_refs, selected_bom)
        lcsc_bom = selected_path if selected_path.suffix.lower() in {".xlsx", ".xlsm"} or "LCSC" in selected_path.name.upper() else self.lcsc_bom()
        parts = merge_part_data(refs, self.project_root, selected_path, lcsc_bom, self.config["schematic_dir"])
        overrides = load_overrides(self.overrides_path)
        actual_by_ref, reconciliation = self.reconcile_parts_with_schematic(parts, overrides)
        rows = group_parts_for_review(parts, overrides)
        if include_stock:
            enrich_rows_with_stock(rows, self.stock_cache_path)
        else:
            enrich_rows_with_cached_stock(rows, self.stock_cache_path)
        enrich_alternate_parts_with_cached_stock(rows, self.stock_cache_path)
        enrich_rows_with_schematic_sync_state(
            rows,
            self.config["schematic_dir"],
            actual_by_ref=actual_by_ref,
            reconciliation_by_ref=reconciliation,
        )
        return sort_bom_rows(rows)

    def stock(self, c_code: str) -> dict[str, object]:
        return lookup_stock_with_cache(self.stock_cache_path, c_code)

    def lcsc_search(self, keyword: str, include_substitutes: bool = True) -> dict[str, object]:
        exact_code = exact_c_code_keyword(keyword)
        stale_cached: dict[str, object] | None = None
        if exact_code:
            with stock_cache_lock(self.stock_cache_path):
                cached = load_stock_cache(self.stock_cache_path).get(exact_code)
            if cached and cached.get("ok"):
                stale_cached = cached
        result = search_lcsc_candidates(
            keyword,
            limit=24,
            include_substitutes=include_substitutes,
            overseas_fetcher=fetch_szlcsc_overseas_channels,
        )
        if not result.get("ok") and stale_cached:
            return {
                "ok": True,
                "error": "",
                "items": [stock_to_candidate_item(keyword, stale_cached)],
                "mode": "cached-exact-stale",
                "model_tokens": [exact_code],
            }
        if result.get("ok") and exact_code:
            item = next((item for item in result.get("items", []) if item.get("c_code") == exact_code), None)
            if item:
                with stock_cache_lock(self.stock_cache_path):
                    cache = load_stock_cache(self.stock_cache_path)
                    cache[exact_code] = candidate_stock_result(item)
                    save_stock_cache(self.stock_cache_path, cache)
        return result

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
        schematic_values = (
            read_schematic_symbol_field_values(
                self.config["schematic_dir"], refs, ("lcsc_alt", "lcsc_alt_info")
            )
            if primary_code
            else {}
        )
        overrides = {}
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
            overrides = save_override(self.overrides_path, ref, ref_update)
        return {"ok": True, "refs": refs, "override": overrides.get(refs[0], {})}

    def select_candidate(self, payload: dict[str, object]) -> dict[str, object]:
        refs = split_refs(payload.get("refs", "")) or split_refs(payload.get("ref", ""))
        item = payload.get("candidate")
        if not refs or not isinstance(item, dict):
            raise ValueError("refs and candidate are required")
        code = normalize_lcsc_c_code(item.get("c_code"))
        if not re.fullmatch(r"C\d+", code) or not str(item.get("model") or "").strip():
            raise ValueError("candidate must have a valid C code and full model")
        item = {**item, "c_code": code}
        fields = candidate_selection_fields(item, refs)
        with stock_cache_lock(self.stock_cache_path):
            cache = load_stock_cache(self.stock_cache_path)
            cache[code] = candidate_stock_result(item)
            cache[code].setdefault("fetched_at", datetime.now().isoformat(timespec="seconds"))
            save_stock_cache(self.stock_cache_path, cache)
        current = load_overrides(self.overrides_path)
        actual = read_schematic_symbol_field_values(self.config["schematic_dir"], refs, EDITABLE_FIELDS)
        for ref in refs:
            # Establish the pre-edit baseline even for legacy records without snapshots.
            snapshot = {**actual.get(ref, {}), **normalize_schematic_field_snapshot(
                current.get(ref, {}).get("schematic_field_snapshot")
            )}
            result = self.save_override({
                "ref": ref,
                "accepted_c_code": code, "manual_c_code": code, "stock_c_code": code,
                "decision": "AcceptManual", "status": "ChangedToNewCode",
                "field_overrides": fields, "schematic_field_snapshot": snapshot,
                "procurement_choice": {},
            })
        result["refs"] = refs
        result["fields"] = fields
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
            "note": payload.get("note", ""),
        }
        if not update["field_overrides"]:
            raise ValueError("no editable fields were provided")
        overrides = {}
        for ref in refs:
            overrides = save_override(self.overrides_path, ref, update)
        return {"ok": True, "refs": refs, "override": overrides.get(refs[0], {})}

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
        actual_by_ref = read_schematic_symbol_field_values(
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
        schematic_values = read_schematic_symbol_field_values(
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
            save_override(self.overrides_path, ref, {"alternate_parts": normalized_parts})
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
            schematic_values = read_schematic_symbol_field_values(
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
            save_override(self.overrides_path, ref, {"procurement_choice": choice})
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
        output_path = self.bom_dir / SCHEMATIC_SYNC_RUNTIME_LOG
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
        for ref in sorted(refs, key=ref_sort_key):
            summary = self.schematic_sync_summary_for_ref(result, ref)
            expected = expected_fields_by_ref.get(ref, {})
            snapshot = {
                key: str(expected.get(key) or "").strip()
                for key in EDITABLE_FIELDS
                if key in expected
            }
            if not summary and not snapshot:
                continue
            update: dict[str, object] = {
                "schematic_synced_at": synced_at,
                "schematic_sync_summary": summary,
            }
            if snapshot:
                update["schematic_field_snapshot"] = snapshot
            save_override(
                self.overrides_path,
                ref,
                update,
            )

    def kicad_url_for_refs(self, refs: list[str], pages: dict[str, str] | None = None) -> str:
        if not refs:
            return ""
        first_ref = refs[0]
        page = str((pages or {}).get(first_ref, "") or "")
        if not page:
            parts = load_schematic_parts(self.config["schematic_dir"])
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
                timeout=20,
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
        return self.run_kicad_script(url, ["-Mode", "SearchPanel"])

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
        raise RuntimeError(f"KiCad schematic save/close failed: {message}")

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
        actual = read_schematic_symbol_field_values(
            self.config["schematic_dir"],
            sorted(expected, key=ref_sort_key),
            sorted(field_keys),
        )
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
        runtime_log_path = self.bom_dir / SCHEMATIC_SYNC_RUNTIME_LOG

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

        ref_field_map = {ref.strip().upper(): fields for ref in refs if ref and ref.strip()}
        editable_fields = {key: fields[key] for key in EDITABLE_FIELDS if key in fields}
        if editable_fields:
            self.save_field_override(
                {"refs": ",".join(refs), "field_overrides": editable_fields, "note": payload.get("note", "")}
            )
            log_event("field_overrides_saved", fields=editable_fields)
        kicad_prepare = self.prepare_kicad_for_external_schematic_write(refs)
        log_event("kicad_prepare_complete", result=kicad_prepare)
        self.require_kicad_prepare_success(kicad_prepare)
        result = update_schematic_symbol_fields(self.config["schematic_dir"], refs, fields)
        log_event(
            "schematic_write_complete",
            updated_refs=result.get("updated_refs", []),
            missing_refs=result.get("missing_refs", []),
            paths=result.get("paths", []),
        )
        verify, retry_results = self.ensure_schematic_fields_verified(
            ref_field_map,
            lambda: update_schematic_symbol_fields(self.config["schematic_dir"], refs, fields),
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
                    lambda: update_schematic_symbol_fields(self.config["schematic_dir"], refs, fields),
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
    ) -> dict[str, object]:
        with SCHEMATIC_SYNC_TRANSACTION_LOCK:
            return self._bulk_sync_bom_state_to_schematic(
                selected_bom,
                ref_map=ref_map,
                dry_run=dry_run,
                notify=notify,
                prepare=prepare,
            )

    def _bulk_sync_bom_state_to_schematic(
        self,
        selected_bom: str,
        ref_map: dict[str, str] | None = None,
        dry_run: bool = False,
        notify: bool = True,
        prepare: bool = True,
    ) -> dict[str, object]:
        ref_map = {str(key).upper(): str(value).upper() for key, value in (ref_map or {}).items()}
        rows = self.rows("", selected_bom, include_stock=False)
        schematic_parts = load_schematic_parts(self.config["schematic_dir"])
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

            c_code = str(
                row.get("stock_c_code")
                or row.get("effective_c_code")
                or row.get("accepted_c_code")
                or row.get("lcsc_match_c_code")
                or ""
            ).strip()
            fields: dict[str, object] = row_schematic_sync_fields(row)
            for target_ref in target_refs:
                ref_field_map[target_ref] = fields

        aggregate["remapped_refs"] = dict(sorted(remapped_refs.items(), key=lambda item: ref_sort_key(item[0])))
        aggregate["missing_refs"] = sorted(aggregate["missing_refs"], key=ref_sort_key)
        aggregate["updatable_refs"] = len(ref_field_map)
        if dry_run:
            return aggregate

        kicad_prepare = self.prepare_kicad_for_external_schematic_write(first_existing_refs) if prepare and first_existing_refs else {
            "ok": False,
            "message": "preparation skipped" if not prepare else "no existing refs to prepare",
        }
        if prepare and first_existing_refs:
            self.require_kicad_prepare_success(kicad_prepare)
        result = update_schematic_symbol_field_map(self.config["schematic_dir"], ref_field_map)
        verify, retry_results = self.ensure_schematic_fields_verified(
            ref_field_map,
            lambda: update_schematic_symbol_field_map(self.config["schematic_dir"], ref_field_map),
            "after_write",
        )
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
        kicad_notify = self.notify_kicad_schematic_changed(aggregate) if notify else {
            "ok": True,
            "message": "notification skipped",
        }
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
                    lambda: update_schematic_symbol_field_map(self.config["schematic_dir"], ref_field_map),
                    result,
                    verify,
                    lambda *_args, **_kwargs: None,
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
        self.record_schematic_sync_overrides(
            aggregate,
            ref_field_map if verify.get("ok") else {},
        )
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
        if selected_bom == SCHEMATIC_SOURCE_BOM:
            schematic = load_schematic_parts(self.config["schematic_dir"])
            parts = [schematic[ref] for ref in refs if ref in schematic]
        else:
            selected_path = self.resolve_bom_path(selected_bom)
            lcsc_bom = selected_path if selected_path.suffix.lower() in {".xlsx", ".xlsm"} or "LCSC" in selected_path.name.upper() else self.lcsc_bom()
            parts = merge_part_data(refs, self.project_root, selected_path, lcsc_bom, self.config["schematic_dir"])
        overrides = load_overrides(self.overrides_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = self.bom_dir / f"part_selection_change_log_{timestamp}.csv"
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
        output_path = self.bom_dir / f"part_selection_pump_table_{timestamp}.csv"
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
                    "LCSC_URL": lcsc_code_url(c_code),
                    "Stock": str(stock.get("stock", "")),
                    "Delivery": str(stock.get("delivery", "")),
                    "MOQ": str(stock.get("moq", "")),
                    "MPQ": str(stock.get("mpq", "")),
                    "Price": str(stock.get("price_ladder") or stock.get("price") or ""),
                    "Param": stock_param_text(stock),
                    "ReviewStatus": row_review_status(row),
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

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                text_response(self, render_index(app))
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
                json_response(self, {"boms": app.available_boms(), "default_bom": SCHEMATIC_SOURCE_BOM})
                return
            if parsed.path == "/api/stock":
                c_code = parse_qs(parsed.query).get("code", [""])[0]
                json_response(self, {"stock": app.stock(c_code)})
                return
            if parsed.path == "/api/lcsc_search":
                query = parse_qs(parsed.query)
                keyword = query.get("keyword", [""])[0]
                include_substitutes = query.get("include_substitutes", ["1"])[0] == "1"
                json_response(self, app.lcsc_search(keyword, include_substitutes))
                return
            if parsed.path == "/api/config":
                config = {key: str(value) if isinstance(value, Path) else value for key, value in app.config.items()}
                config["desktop_launcher_version"] = 2
                json_response(self, config)
                return
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                payload = json.loads(body)
                if parsed.path == "/api/kicad_jump":
                    json_response(self, app.jump_to_schematic(str(payload.get("url", ""))))
                    return
                if parsed.path == "/api/override":
                    json_response(self, app.save_override(payload))
                    return
                if parsed.path == "/api/field_override":
                    json_response(self, app.save_field_override(payload))
                    return
                if parsed.path == "/api/select_candidate":
                    json_response(self, app.select_candidate(payload))
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
                    json_response(
                        self,
                        app.bulk_sync_bom_state_to_schematic(
                            str(payload.get("bom", "")),
                            ref_map=DEFAULT_BULK_SYNC_REF_MAP,
                            notify=payload.get("notify", True) is not False,
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
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BOM 器件选型工作台</title>
<style>
body {{ --sticky-toolbar-height: 0px; --sticky-filter-height: 0px; margin: 0; font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif; color: #1f2933; background: #f4f7fb; }}
header {{ display: flex; align-items: center; gap: 14px; background: #1f4e78; color: white; padding: 4px 10px; min-height: 26px; }}
h1 {{ flex: 0 0 auto; font-size: 15px; margin: 0; line-height: 1.2; }}
.meta {{ display: flex; align-items: center; min-width: 0; font-size: 12px; opacity: .95; }}
.project-path-link {{ display: inline-block; max-width: min(980px, calc(100vw - 44px)); padding: 0; border: 0; background: transparent; color: #eaf5ff; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }}
.project-path-link:hover {{ color: #fff; text-decoration: underline; box-shadow: none; }}
.toolbar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 5px 9px; min-height: 40px; padding: 3px 8px; box-sizing: border-box; background: white; border-bottom: 1px solid #d9e2ec; position: sticky; top: 0; left: 0; z-index: 5; transition: padding .14s ease, box-shadow .14s ease; }}
.toolbar-section {{ display: flex; flex-wrap: nowrap; align-items: center; gap: 5px; min-width: 0; padding: 0; border: 0; background: transparent; }}
.toolbar-section .section-title {{ display: none; }}
.source-section {{ flex: 0 1 330px; }}
.source-section select {{ flex: 1 1 240px; min-width: 190px; max-width: 330px; }}
.action-section {{ flex: 1 1 auto; }}
.export-section {{ flex: 0 1 auto; }}
.action-section button, .export-section button, .export-section select {{ white-space: nowrap; }}
.export-section select {{ flex: 1 1 180px; min-width: 160px; }}
body.toolbar-compact .toolbar {{ min-height: 36px; gap: 4px 7px; padding: 2px 6px; box-shadow: 0 2px 10px rgba(15, 35, 55, .12); }}
body.toolbar-compact .toolbar-section {{ gap: 4px; }}
body.toolbar-compact .source-section select {{ flex: 0 1 220px; min-width: 170px; }}
body.toolbar-compact button {{ padding: 3px 7px; }}
body.toolbar-compact .action-section .status {{ max-width: 420px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
body.toolbar-compact .export-section select {{ flex: 1 1 160px; min-width: 150px; }}
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
th {{ background: #274b6d; color: white; padding: 7px; border: 1px solid #c8d1da; font-size: 12px; position: sticky; top: calc(var(--sticky-toolbar-height) + var(--sticky-filter-height) - 1px); z-index: 1; overflow-wrap: anywhere; }}
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
.field-changed {{ color: #9a5b00; font-size: 11px; margin-top: 3px; }}
.field-diff {{ color: #9a5b00; font-size: 11px; line-height: 1.35; margin-top: 3px; }}
.sync-summary {{ color: #0f6b3f; font-size: 11px; line-height: 1.35; margin: 4px 0; overflow-wrap: anywhere; }}
.sync-summary summary {{ width: fit-content; color: #52606d; cursor: pointer; user-select: none; }}
.sync-summary-detail {{ max-height: 110px; margin-top: 3px; padding: 4px 5px; overflow: auto; border-left: 2px solid #9ccfb3; background: #f3faf6; white-space: pre-wrap; }}
.schematic-sync-marker {{ display: inline-flex; align-items: center; margin: 2px 0 5px; padding: 1px 7px; border-radius: 999px; border: 1px solid #f3a7ad; background: #ffe4e6; color: #b42318; font-size: 11px; font-weight: 700; line-height: 1.45; }}
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
.review-ok {{ background: #e8f7ef; color: #0b6b3a; border-color: #a7d9bd; }}
.review-warn {{ background: #fff3cd; color: #8a5a00; border-color: #f3cf74; }}
.review-bad {{ background: #ffe4e6; color: #b42318; border-color: #f3a7ad; }}
.stock-ok {{ background: #e8f7ef; color: #0b6b3a; border-color: #a7d9bd; }}
.stock-wait {{ background: #edf2f7; color: #4a5568; border-color: #cbd5e1; }}
.stock-low {{ background: #fff3cd; color: #8a5a00; border-color: #f3cf74; }}
.stock-bad {{ background: #ffe4e6; color: #b42318; border-color: #f3a7ad; }}
.stock-card {{ display: grid; gap: 3px; }}
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
.view-wrap {{ position: relative; }}
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
.modal-query {{ display: grid; grid-template-columns: minmax(360px, 1fr) 76px 72px 118px; gap: 8px; padding: 10px 18px; border-bottom: 1px solid #d9e2ec; align-items: center; }}
.modal-query input {{ width: 100%; min-height: 30px; box-sizing: border-box; }}
.modal-query label {{ white-space: nowrap; }}
.filter-toggle {{ display: flex; align-items: center; justify-content: center; gap: 4px; min-height: 30px; }}
.candidate-table {{ width: max-content; min-width: 100%; table-layout: auto; font-size: 12px; }}
.candidate-table td {{ padding: 7px 8px; vertical-align: middle; }}
.candidate-table th {{ position: sticky; top: 0; user-select: none; }}
.candidate-table th[data-candidate-col], .candidate-table td[data-candidate-col] {{ min-width: 28px; max-width: none; white-space: normal; overflow-wrap: anywhere; word-break: break-word; }}
.candidate-table th[data-candidate-col] {{ cursor: grab; }}
.candidate-table th[data-candidate-col].dragging {{ opacity: .45; }}
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
.filter-bar {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-height: 34px; box-sizing: border-box; margin: 0; padding: 3px 8px; border: 1px solid #dbe4ee; border-left: 0; border-right: 0; border-radius: 0; background: #fff; position: sticky; top: calc(var(--sticky-toolbar-height) - 1px); left: 0; z-index: 4; box-shadow: 0 2px 7px rgba(15, 35, 55, .10); }}
.filter-title {{ color: #243b53; font-weight: 700; }}
.bom-search {{ display: flex; align-items: center; gap: 4px; margin-left: auto; min-width: 0; max-width: 100%; }}
.bom-search input {{ width: 230px; max-width: 100%; min-width: 100px; box-sizing: border-box; padding: 4px 6px; }}
.bom-search button {{ padding: 2px 6px; }}
.bom-search output {{ white-space: nowrap; color: #52606d; font-size: 12px; }}
.filter-hint {{ color: #7b8794; font-size: 12px; }}
.filter-tabs {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.filter-tabs button {{ border-color: #b8c7d6; background: #fff; border-radius: 999px; padding: 2px 9px; }}
.filter-tabs button.active {{ background: #1f4e78; border-color: #1f4e78; color: #fff; font-weight: 700; box-shadow: 0 0 0 2px rgba(31, 78, 120, .12); }}
.filter-tabs button.filter-negative {{ background: #26734d; border-color: #26734d; color: #fff; }}
body.toolbar-compact .filter-bar {{ gap: 4px; min-height: 30px; padding: 2px 6px; }}
body.toolbar-compact .filter-tabs button {{ padding: 1px 8px; }}
body.toolbar-compact .filter-hint {{ display: none; }}
@media (max-width: 1280px) {{ .toolbar {{ align-items: flex-start; }} .toolbar-section {{ flex-wrap: wrap; }} }}
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
</head>
<body>
<header>
<h1>BOM 器件选型工作台</h1>
<div class="meta"><button type="button" class="project-path-link" data-action="open-project-path" title="点击打开工程目录">工程：{html.escape(str(app.project_root))}</button></div>
</header>
<div class="toolbar">
<select id="bomSelect" title="选择要转换到网页的 BOM 表"></select>
<input type="hidden" id="refsInput" value="">
<button id="queryMissingStock" class="stock-query">查询未查库存</button>
<button id="queryAllStock" class="action-muted">刷新全部库存</button>
<button id="syncAllSchematic" class="action-sync">同步全部原理图</button>
<button id="exportChanges">导出改动表</button>
<select id="pumpExportMode" title="选择导出泵表内容">
  <option value="current_filter">当前筛选结果</option>
  <option value="stock_issue">库存异常/低库存</option>
  <option value="low_stock">低库存器件</option>
  <option value="stock_error">库存异常器件</option>
  <option value="pending_review">未确认料</option>
  <option value="needs_sync">待同步原理图</option>
  <option value="all_non_nc">全部非NC器件</option>
</select>
<button id="exportPumpTable">导出泵表</button>
<div class="view-wrap">
  <button id="toggleColumns" type="button">列显示</button>
  <div id="columnPanel" class="view-panel">
    <h2>自定义视图配置</h2>
    <div id="columnList"></div>
  </div>
</div>
<span id="state" class="status">准备就绪</span>
<div id="syncProgress" class="sync-progress" aria-live="polite">
  <div class="sync-progress-track"><div id="syncProgressFill" class="sync-progress-fill"></div></div>
  <span id="syncProgressText" class="sync-progress-text"></span>
</div>
<div id="stockProgress" class="stock-progress" aria-live="polite">
  <div class="stock-progress-track"><div id="stockProgressFill" class="stock-progress-fill"></div></div>
  <span id="stockProgressText" class="stock-progress-text"></span>
</div>
</div>
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
<th data-col="row_no" style="width:56px">行号</th><th data-col="review" style="width:88px">确认料</th><th data-col="refs" style="width:150px">位号</th><th data-col="page_refs" style="width:220px">页面位号</th><th data-col="quantity" style="width:70px">单套用量</th><th data-col="comment" style="width:110px">Comment</th><th data-col="value" style="width:110px">Value</th><th data-col="footprint" style="width:95px">Footprint</th>
<th data-col="c_code" style="width:180px">C编码</th><th data-col="search" style="width:180px">搜索</th><th data-col="stock" style="width:170px">库存</th><th data-col="order" style="width:90px">MOQ/MPQ</th><th data-col="price" style="width:100px">价格</th><th data-col="param" style="width:320px">物料参数</th><th data-col="alternates" style="width:360px">备选料</th><th data-col="note" style="width:220px">备注</th>
</tr>
</thead>
<tbody id="rows"></tbody>
</table>
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
      <input id="candidateKeyword" spellcheck="false" placeholder="搜索型号、品牌、参数、C 编码">
      <label class="filter-toggle"><input id="inStockOnly" type="checkbox"> 现货</label>
      <button id="runCandidateSearch" class="stock-query" type="button">结构化查询</button>
      <button id="openOfficialSearch" type="button">Chrome快速搜索</button>
    </div>
    <div class="candidate-body-wrap">
      <table class="candidate-table">
        <thead>
          <tr>
            <th data-candidate-col="action" style="width:142px">操作</th>
            <th data-candidate-col="image" style="width:68px">图片</th>
            <th data-candidate-col="model" style="width:250px">型号/品牌/封装</th>
            <th data-candidate-col="param" style="width:280px">参数详情</th>
            <th data-candidate-col="code" style="width:92px">C编码</th>
            <th data-candidate-col="channel_stock" style="width:215px">渠道 / 库存交期</th>
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
let candidateTargetRow = null;
let candidateTargetKey = "";
let currentCandidateItems = new Map();
let currentProcurementQuotes = new Map();
let candidateSearchToken = 0;
let contextRefTarget = null;
let multiSplitGroupId = "";
let multiSplitRefs = new Set();
let multiSplitSource = "refs";
let stockQueryController = null;
let stockQueryRunId = 0;

function organizeToolbar() {{
  const toolbar = document.querySelector(".toolbar");
  if (!toolbar || toolbar.classList.contains("organized")) return;
  function makeSection(className, title, ids) {{
    const section = document.createElement("div");
    section.className = `toolbar-section ${{className}}`;
    const label = document.createElement("span");
    label.className = "section-title";
    label.textContent = title;
    section.appendChild(label);
    ids.forEach(id => {{
      const el = document.getElementById(id);
      if (el) section.appendChild(el);
    }});
    return section;
  }}
  const source = makeSection("source-section", "数据源", ["bomSelect"]);
  const actions = makeSection("action-section", "查询 / 同步", ["queryMissingStock", "queryAllStock", "syncAllSchematic", "state", "stockProgress", "syncProgress"]);
  const exports = makeSection("export-section", "导出 / 视图", ["exportChanges", "pumpExportMode", "exportPumpTable"]);
  const viewWrap = document.querySelector(".view-wrap");
  if (viewWrap) exports.appendChild(viewWrap);
  toolbar.replaceChildren(source, actions, exports);
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
    filterTabsEl.parentNode.insertBefore(bar, filterTabsEl);
    bar.appendChild(title);
    bar.appendChild(filterTabsEl);
    bar.appendChild(hint);
    bar.appendChild(document.querySelector(".bom-search"));
  }}
}}

function updateStickyChrome() {{
  const toolbar = document.querySelector(".toolbar");
  const filterBar = document.querySelector(".filter-bar");
  document.body.classList.toggle("toolbar-compact", window.scrollY > 60);
  const horizontalOffset = `translateX(${{window.scrollX}}px)`;
  if (toolbar) toolbar.style.transform = horizontalOffset;
  if (filterBar) filterBar.style.transform = horizontalOffset;
  const toolbarHeight = toolbar ? Math.ceil(toolbar.getBoundingClientRect().height) : 0;
  const filterHeight = filterBar ? Math.ceil(filterBar.getBoundingClientRect().height) : 0;
  document.body.style.setProperty("--sticky-toolbar-height", `${{toolbarHeight}}px`);
  document.body.style.setProperty("--sticky-filter-height", `${{filterHeight}}px`);
}}

function scheduleStickyChromeUpdate() {{
  window.requestAnimationFrame(updateStickyChrome);
}}

function observeStickyChrome() {{
  if (!("ResizeObserver" in window)) return;
  const observer = new ResizeObserver(scheduleStickyChromeUpdate);
  const toolbar = document.querySelector(".toolbar");
  const filterBar = document.querySelector(".filter-bar");
  if (toolbar) observer.observe(toolbar);
  if (filterBar) observer.observe(filterBar);
}}

function esc(text) {{
  return String(text ?? "").replace(/[&<>"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[ch]));
}}

function setState(text, cls="status") {{
  state.textContent = text;
  state.className = cls;
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
  queryMissingButton.disabled = false;
  queryMissingButton.textContent = "查询未查库存";
  queryAllButton.disabled = false;
  queryAllButton.textContent = "刷新全部库存";
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
const UI_SCHEMA_VERSION = 5;
let defaultColumnWidthFrame = 0;
const DEFAULT_COLUMNS = [
  ["row_no", "行号"], ["review", "确认料"],
  ["refs", "位号"], ["page_refs", "页面位号"], ["quantity", "单套用量"],
  ["comment", "Comment"], ["value", "Value"], ["footprint", "Footprint"],
  ["c_code", "C编码"], ["search", "搜索"], ["stock", "库存"],
  ["order", "MOQ/MPQ"], ["price", "价格"], ["param", "物料参数"],
  ["alternates", "备选料"], ["note", "备注"]
];
const CANDIDATE_COLUMNS = [
  ["action", "操作"], ["image", "图片"], ["model", "型号/品牌/封装"],
  ["channel_stock", "渠道 / 库存交期"], ["param", "参数详情"], ["code", "C编码"],
  ["order", "MOQ/MPQ"], ["price", "价格梯度"]
];

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
      settings.columnOrder = order;
      settings.hiddenColumns = Object.assign({{}}, settings.hiddenColumns || {{}}, {{note: true}});
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

function fitDefaultColumnWidths() {{
  const settings = loadUiSettings();
  if (settings.columnWidths && Object.keys(settings.columnWidths).length) return;
  if (defaultColumnWidthFrame) cancelAnimationFrame(defaultColumnWidthFrame);
  defaultColumnWidthFrame = requestAnimationFrame(() => {{
    defaultColumnWidthFrame = 0;
    const latestSettings = loadUiSettings();
    if (latestSettings.columnWidths && Object.keys(latestSettings.columnWidths).length) {{
      applySavedColumnWidths();
      return;
    }}
    const widths = {{}};
    document.querySelectorAll("th[data-col]").forEach(th => {{
      const key = th.dataset.col;
      const cells = [th, ...Array.from(document.querySelectorAll(`td[data-col="${{key}}"]`)).slice(0, 80)];
      let width = th.offsetWidth || 80;
      for (const cell of cells) {{
        width = Math.max(width, Math.min(320, Math.ceil(cell.scrollWidth + 18)));
      }}
      widths[key] = Math.max(48, width);
      th.style.width = `${{widths[key]}}px`;
      th.style.minWidth = `${{widths[key]}}px`;
    }});
    const currentSettings = loadUiSettings();
    if (!currentSettings.columnWidths || !Object.keys(currentSettings.columnWidths).length) {{
      updateUiSettings({{columnWidths: widths}});
    }}
  }});
}}

function applySavedColumnWidths() {{
  const widths = loadUiSettings().columnWidths || {{}};
  document.querySelectorAll("th[data-col]").forEach(th => {{
    const width = widths[th.dataset.col];
    if (width) {{
      th.style.width = `${{width}}px`;
      th.style.minWidth = `${{width}}px`;
    }}
  }});
}}

function makeColumnsResizable() {{
  document.querySelectorAll("th[data-col]").forEach(th => {{
    if (th.querySelector(".resize-grip")) return;
    const grip = document.createElement("span");
    grip.className = "resize-grip";
    th.appendChild(grip);
    grip.addEventListener("mousedown", event => {{
      event.preventDefault();
      if (defaultColumnWidthFrame) {{
        cancelAnimationFrame(defaultColumnWidthFrame);
        defaultColumnWidthFrame = 0;
      }}
      const startX = event.clientX;
      const startWidth = th.offsetWidth;
      function onMove(moveEvent) {{
        const width = Math.max(48, startWidth + moveEvent.clientX - startX);
        th.style.width = `${{width}}px`;
        th.style.minWidth = `${{width}}px`;
      }}
      function onUp() {{
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        const settings = loadUiSettings();
        const widths = Object.assign({{}}, settings.columnWidths || {{}});
        widths[th.dataset.col] = Math.round(th.offsetWidth);
        updateUiSettings({{columnWidths: widths}});
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
  document.querySelectorAll(".candidate-table th[data-candidate-col]").forEach(th => {{
    const width = widths[th.dataset.candidateCol];
    if (width) {{
      th.style.width = `${{width}}px`;
      th.style.minWidth = `${{width}}px`;
    }}
  }});
}}

function makeCandidateColumnsConfigurable() {{
  document.querySelectorAll(".candidate-table th[data-candidate-col]").forEach(th => {{
    if (!th.dataset.configured) {{
      th.dataset.configured = "1";
      th.draggable = true;
      th.title = "拖动表头调整列顺序；拖右侧边缘调整列宽";
      th.addEventListener("dragstart", event => {{
        if (event.target.closest(".resize-grip")) {{
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
    th.appendChild(grip);
    grip.addEventListener("mousedown", event => {{
      event.preventDefault();
      event.stopPropagation();
      const startX = event.clientX;
      const startWidth = th.offsetWidth;
      function onMove(moveEvent) {{
        const width = Math.max(28, startWidth + moveEvent.clientX - startX);
        th.style.width = `${{width}}px`;
        th.style.minWidth = `${{width}}px`;
      }}
      function onUp() {{
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        const settings = loadUiSettings();
        const widths = Object.assign({{}}, settings.candidateColumnWidths || {{}});
        widths[th.dataset.candidateCol] = Math.round(th.offsetWidth);
        updateUiSettings({{candidateColumnWidths: widths}});
      }}
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    }});
  }});
}}

async function jump(url) {{
  try {{
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
  const options = links.map(item => `<label class="ref-multi-option"><input type="checkbox" data-multi-split-ref="${{esc(item.ref)}}" ${{multiSplitRefs.has(item.ref) ? "checked" : ""}}><span class="mono">${{esc(item.ref)}}</span></label>`).join("");
  return `<div class="ref-multi-picker"><strong>选择要拆出的位号</strong>${{options}}<div class="ref-multi-actions"><button type="button" data-action="split-selected-refs" data-group-id="${{esc(row.group_id || row.ref)}}">拆出所选</button><button type="button" data-action="cancel-multi-split">取消</button></div></div>`;
}}

function renderRefButtons(row) {{
  const links = row.ref_links || [];
  if (links.length && multiSplitSource === "refs" && multiSplitGroupId === (row.group_id || row.ref)) return renderMultiSplitPicker(row);
  if (links.length) return `${{links.map(refButton).join(", ")}}${{links.length > 1 ? ` <button type="button" class="small action-link" data-action="start-multi-split" data-split-source="refs" title="选择多个位号并分别拆成独立行">多选拆行</button>` : ""}}`;
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
  const splitButton = links.length > 1 ? ` <button type="button" class="small action-link page-split-button" data-action="start-multi-split" data-split-source="page" title="按页面位号选择多个器件并分别拆成独立行">多选拆行</button>` : "";
  if (!byPage.size) return `${{esc(row.page_refs || "")}}${{splitButton}}`;
  return `${{Array.from(byPage.entries())
    .map(([page, items]) => `<div><span class="mono">${{esc(page)}}</span>: ${{items.map(refButton).join(", ")}}</div>`)
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

function rowForSingleRef(row, ref) {{
  const link = (row.ref_links || []).find(item => item.ref === ref) || {{ref, page: row.page || "", url: row.kicad_jump_url || ""}};
  const clone = Object.assign({{}}, row);
  clone.group_id = `${{row.group_id || row.ref || "G"}}_split_${{ref}}`;
  clone.ref = ref;
  clone.refs = ref;
  clone.refs_list = [ref];
  clone.ref_links = [link];
  clone.page = link.page || row.page || "";
  clone.page_refs = buildPageRefs(clone.ref_links);
  clone.quantity = 1;
  clone.temp_split = true;
  clone.field_changed_refs = "";
  clone.field_edit_status = "";
  clone.field_diffs = {{}};
  return clone;
}}

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
    const remainingLinks = (row.ref_links || []).filter(item => item.ref !== ref);
    const originalLinks = row.ref_links || [];
    row.refs_list = remainingRefs;
    row.refs = remainingRefs.join(",");
    row.ref = remainingRefs[0] || row.ref;
    row.ref_links = remainingLinks;
    row.quantity = remainingRefs.length;
    row.page_refs = buildPageRefs(remainingLinks);
    const splitRow = rowForSingleRef(Object.assign({{}}, row, {{ref_links: originalLinks}}), ref);
    currentRows.splice(index + 1, 0, splitRow);
    renderRows(currentRows);
    setState(`${{ref}} 已拆出临时单独行；修改 Comment/Value/Footprint 后按 Enter 保存并合并`, "saved");
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
  const splitRows = selectedRefs.map(ref => rowForSingleRef(originalRow, ref));
  if (remainingRefs.length) {{
    const remainingLinks = (row.ref_links || []).filter(item => remainingRefs.includes(item.ref));
    row.refs_list = remainingRefs;
    row.refs = remainingRefs.join(",");
    row.ref = remainingRefs[0] || row.ref;
    row.ref_links = remainingLinks;
    row.quantity = remainingRefs.length;
    row.page_refs = buildPageRefs(remainingLinks);
    currentRows.splice(index + 1, 0, ...splitRows);
  }} else {{
    currentRows.splice(index, 1, ...splitRows);
  }}
  multiSplitGroupId = "";
  multiSplitSource = "refs";
  multiSplitRefs = new Set();
  renderRows(currentRows);
  setState(`已拆出 ${{selectedRefs.length}} 个独立位号；修改字段后按 Enter 保存`, "saved");
}}

function stockCode(row) {{
  const text = row.stock_c_code || row.effective_c_code || row.accepted_c_code || row.lcsc_match_c_code || "";
  const match = String(text).toUpperCase().match(/\\bC\\d+\\b/);
  return match ? match[0] : "";
}}

function normalizeCCode(text) {{
  const compact = String(text || "").toUpperCase().replace(/\\bC\\s+(\\d+)\\b/g, "C$1");
  const match = compact.match(/\\bC\\d+\\b/);
  return match ? match[0] : "";
}}

function originalCode(row) {{
  return normalizeCCode(row.accepted_c_code || row.lcsc_match_c_code || "");
}}

function statusValue(row, ov) {{
  if ((ov.decision || row.decision) === "Reject" || ov.status === "Rejected") return "Rejected";
  if ((ov.decision || row.decision) === "AcceptManual" || ov.status === "ChangedToNewCode") return "ChangedToNewCode";
  return "Original";
}}

function statusText(value) {{
  if (value === "ChangedToNewCode") return "已改动为新编码";
  if (value === "Rejected") return "不接受/留空";
  return "接受原始C编码";
}}

function statusClass(value) {{
  if (value === "ChangedToNewCode") return "status-changed";
  if (value === "Rejected") return "status-rejected";
  return "status-original";
}}

function reviewStatusText(value) {{
  if (value === "Confirmed") return "已确认";
  if (value === "Rejected") return "不确认";
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
  const number = stockNumber(stock);
  return !!(stock?.ok && Number.isFinite(number) && number < 100);
}}

function stockIsIssue(stock) {{
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
  return !!(stockCode(row) || normalizeCCode(stock?.c_code || ""));
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
  const codeChanged = selectedStatus === "ChangedToNewCode" || row?.status === "ChangedToNewCode" || row?.manual_c_code || row?.stock_c_code;
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
  if (stock.arrange) pieces.push(stock.arrange);
  if (stock.category) pieces.push(stock.category);
  return pieces.join("\\n");
}}

function renderStockParam(stock) {{
  return renderCandidateParam(
    stock.param || "",
    stock.param_detail || "",
    stock.arrange || "",
    stock.category || ""
  );
}}

function stockAlreadyQueried(stock) {{
  const status = stock.status || "";
  return !!(stock.ok || stock.fetched_at || (stock.c_code && status && !["待查询", "查询中", "请提供料号"].includes(status)));
}}

function renderStock(stock) {{
  const status = stock.status || "";
  const visual = stockVisual(stock);
  const url = stock.url ? `<a target="_blank" rel="noopener" data-secondary-search="${{esc(stock.c_code || "")}}" href="${{esc(stock.url)}}">${{esc(stock.c_code || "")}}</a>` : esc(stock.c_code || "");
  const delivery = stock.delivery ? `<br>${{esc(stock.delivery)}}` : "";
  const channel = stock.channel ? `<br>${{esc(stock.channel)}}` : "";
  const detail = stock.ok ? `总: <span class="${{visual.detailClass}}">${{esc(stock.stock)}}</span><br>SZ: ${{esc(stock.stock_sz)}} / JS: ${{esc(stock.stock_js)}} / HK: ${{esc(stock.stock_hk)}}${{delivery}}${{channel}}` : esc(stock.error || status);
  const fetchedAt = stock.fetched_at ? `<br><span class="stock-time">查询时间: ${{esc(String(stock.fetched_at).replace("T", " "))}}</span>` : "";
  return `<div class="stock-card ${{visual.detailClass}}"><span class="stock-pill ${{visual.pill}}">${{esc(visual.text)}}</span><div>${{url}}<br>${{detail}}${{fetchedAt}}</div></div>`;
}}

function renderStockCell(stock) {{
  return `${{renderStock(stock)}}<button type="button" class="small stock-query" data-action="query-stock">查询库存</button>`;
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

function applyActiveFilter() {{
  let visibleCount = 0;
  rowsEl.querySelectorAll("tr[data-filter-key]").forEach(tr => {{
    const keys = String(tr.dataset.filterKey || "all").split(/\s+/);
    const row = Object.assign({{}}, rowDataForElement(tr) || {{}}, {{
      comment: fieldValue(tr, "comment"), value: fieldValue(tr, "value"),
      footprint: fieldValue(tr, "footprint"), effective_c_code: currentLcscCode(tr)
    }});
    const visible = (!activeFilters.size || Array.from(activeFilters.values()).every(filter => keys.includes(filter)))
      && bomRowMatchesSearch(row, bomSearch.value);
    tr.classList.toggle("hidden-row", !visible);
    if (visible) visibleCount += 1;
  }});
  bomSearchCount.textContent = `${{visibleCount}} / ${{currentRows.length}} 行`;
}}

function setStockCells(tr, stock) {{
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
}}

function setCodeState(tr, status, text = "") {{
  tr.dataset.codeStatus = status;
  tr.dataset.hasCode = currentLcscCode(tr) ? "1" : "0";
  refreshRowState(tr);
  const stateEl = tr.querySelector("[data-code-state]");
  stateEl.className = `status-pill ${{statusClass(status)}}`;
  stateEl.textContent = text || statusText(status);
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

function fieldValue(tr, field) {{
  const input = tr.querySelector(`[data-field="${{field}}"]`);
  return input ? input.value.trim() : "";
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
  return {{
    ref: tr.dataset.ref,
    refs: tr.dataset.refs,
    field_overrides,
    note: tr.querySelector("[data-field='note']").value.trim()
  }};
}}

function fieldDiff(row, field) {{
  const diff = (row.field_diffs || {{}})[field];
  if (!diff) return "";
  const oldText = String(diff.old ?? "");
  const newText = String(diff.new ?? "");
  if (oldText === newText) return "";
  const refs = diff.refs ? ` ${{diff.refs}}` : "";
  const oldLabel = oldText || "(空)";
  const newLabel = newText || "(空)";
  return `<div class="field-diff" title="${{esc(diff.refs || "")}} 未同步原理图">${{esc(oldLabel)}} -&gt; ${{esc(newLabel)}}${{esc(refs)}}</div>`;
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

function schematicSyncMarker(row, syncNeeded) {{
  if (!syncNeeded) return "";
  const refs = String(row.field_changed_refs || row.refs || "").trim();
  const label = refs ? `有改动，请同步原理图：${{refs}}` : "有改动，请同步原理图";
  return `<div class="schematic-sync-marker" data-sync-marker title="${{esc(refs)}}">${{esc(label)}}</div>`;
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
  return {{refs: tr?.dataset.refs || tr?.dataset.ref || "", scrollY: window.scrollY}};
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
    target.scrollIntoView({{block: "center", inline: "nearest"}});
    target.classList.add("changed");
    window.setTimeout(() => target.classList.remove("changed"), 1200);
  }} else if (Number.isFinite(anchor.scrollY)) {{
    window.scrollTo({{top: anchor.scrollY}});
  }}
}}

function markRowNeedsSchematicSync(tr, refsText = "") {{
  if (!tr) return;
  tr.dataset.syncNeeded = "1";
  refreshRowState(tr);
  const button = tr.querySelector("[data-action='sync-schematic']");
  if (button) {{
    button.classList.remove("action-muted", "action-sync");
    button.classList.add("action-sync-needed");
    button.title = "有改动，请同步原理图";
  }}
  if (!tr.querySelector("[data-sync-marker]")) {{
    const marker = schematicSyncMarker({{field_changed_refs: refsText || tr.dataset.refs || tr.dataset.ref}}, true);
    if (button) button.insertAdjacentHTML("afterend", marker);
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
  const anchor = rowAnchor(tr);
  const payload = fieldPayload(tr);
  const res = await fetch("/api/field_override", {{ method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(payload) }});
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "保存字段失败");
  setState(`字段已保存，正在重新合并 ${{payload.refs || payload.ref}}`, "saved");
  await loadRows(anchor);
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
  }}
  setState(`确认状态已保存 ${{payload.refs || payload.ref}}`, "saved");
  tr.dataset.reviewStatus = payload.review_status || "Pending";
  refreshRowState(tr);
}}

async function syncSchematicWithProgress(tr) {{
  const payload = fieldPayload(tr);
  const syncButton = tr.querySelector("[data-action='sync-schematic']");
  if (syncButton) syncButton.disabled = true;
  const stopProgress = startSyncProgressTicker([
    {{percent: 8, text: "保存并关闭 KiCad 当前原理图页", delay: 420}},
    {{percent: 24, text: "写入原理图文件", delay: 620}},
    {{percent: 54, text: "重新读取原理图校验字段", delay: 720}},
    {{percent: 76, text: "必要时重写被覆盖字段", delay: 720}},
    {{percent: 88, text: "通知 KiCad 并刷新网页状态", delay: 720}}
  ]);
  setState(`正在同步原理图 ${{payload.refs || payload.ref}}...`, "status");
  let data;
  try {{
    const res = await fetch("/api/sync_schematic", {{ method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(payload) }});
    data = await res.json();
  }} finally {{
    stopProgress();
    if (syncButton) syncButton.disabled = false;
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
    throw new Error(`已写入但校验失败，请先保存/关闭 KiCad 后重试：${{mismatchRefs.join(",") || "字段未落盘"}}`);
  }}
  const verifyText = verify.attempts > 1 ? `；检测到覆盖并已重写 ${{verify.attempts}} 次校验` : "；校验通过";
  const showText = notify.ok ? "；已通知 KiCad" : "；已写入文件";
  const missingText = missingRefs.length ? `；未找到 ${{missingRefs.join(",")}}` : "";
  const message = changedRefs.length
    ? `已同步原理图 ${{changedRefs.join(",")}}${{showText}}${{verifyText}}${{missingText}}`
    : `没有需要同步的字段${{verifyText}}${{missingText}}`;
  setSyncProgress(100, message, true);
  setState(message, changedRefs.length ? "saved" : "status");
  await loadRows();
  clearSyncProgress();
}}

async function syncAllSchematicWithProgress() {{
  const trs = Array.from(document.querySelectorAll("tr[data-ref]"));
  if (!trs.length) {{
    setState("当前表格没有可同步的行", "error");
    return;
  }}
  syncAllButton.disabled = true;
  const stopProgress = startSyncProgressTicker([
    {{percent: 6, text: "准备批量同步", delay: 450}},
    {{percent: 18, text: "读取当前 BOM 合并状态", delay: 550}},
    {{percent: 36, text: "保存并关闭 KiCad 当前原理图页", delay: 650}},
    {{percent: 58, text: "批量写入所有原理图页面", delay: 900}},
    {{percent: 78, text: "校验写入字段", delay: 900}},
    {{percent: 90, text: "通知 KiCad 并刷新网页", delay: 650}}
  ]);
  try {{
    setState("正在批量同步原理图...", "status");
    const res = await fetch("/api/bulk_sync_schematic", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{bom: bomSelect.value || "", notify: true}})
    }});
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "批量同步原理图失败");
    const verify = data.schematic_verify || {{}};
    if (verify.ok === false) {{
      const mismatchRefs = Object.keys(verify.mismatches || {{}});
      setSyncProgress(100, `校验失败：${{mismatchRefs.join(",") || "字段未落盘"}}`, true);
      throw new Error(`批量写入后校验失败：${{mismatchRefs.join(",") || "字段未落盘"}}`);
    }}
    const changedCount = (data.updated_refs || []).length;
    const missingRefs = data.missing_refs || [];
    const missingText = missingRefs.length ? `；未找到 ${{missingRefs.join(",")}}` : "";
    const verifyText = verify.attempts > 1 ? `；检测到覆盖并已重写 ${{verify.attempts}} 次校验` : "；校验通过";
    const message = `同步全部原理图完成：${{changedCount}} 个位号有改动${{verifyText}}${{missingText}}`;
    setSyncProgress(100, message, true);
    setState(message, "saved");
    await loadRows();
  }} finally {{
    stopProgress();
    syncAllButton.disabled = false;
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
    return;
  }}
  input.value = "";
  await saveRow(tr, "", "");
  await saveStockCode(tr, "");
  const code = tr.dataset.originalCode || "";
  tr.querySelector("[data-effective-code]").textContent = code;
  setCodeState(tr, status);
  markRowNeedsSchematicSync(tr, tr.dataset.refs || tr.dataset.ref);
  await refreshRowStock(tr, code);
}}

function renderRows(rows) {{
  currentRows = rows;
  refsInput.value = rows.flatMap(row => row.refs_list || [row.ref]).join(",");
  rowsEl.innerHTML = rows.map((row, index) => {{
    const ov = row.override || {{}};
    const effective = row.effective_c_code || "";
    const replacement = (ov.decision === "AcceptManual" || ov.status === "ChangedToNewCode") ? (ov.manual_c_code || ov.accepted_c_code || effective) : (row.stock_c_code || "");
    const note = row.note || "";
    const selectedStatus = statusValue(row, ov);
    const reviewStatus = row.review_status || ov.review_status || "Pending";
    const codeMismatch = (row.accepted_c_code || row.lcsc_match_c_code) && row.accepted_c_code !== row.lcsc_match_c_code;
    const codeClass = codeMismatch ? "bad mono" : "mono";
    const refButtons = renderRefButtons(row);
    const pageRefButtons = renderPageRefButtons(row);
    const code = stockCode(row);
    const original = originalCode(row);
    const stock = initialStock(row);
    const syncNeeded = !!row.field_changed_refs;
    const missingRef = rowHasMissingRef(row);
    const filterKeys = rowFilterKeys(row, selectedStatus, syncNeeded, stock, reviewStatus).join(" ");
    const syncButtonClass = syncNeeded ? "action-sync-needed" : "action-muted";
    const syncSummary = schematicSyncSummary(row);
    const syncMarker = schematicSyncMarker(row, syncNeeded);
    const fieldReconcileNotice = schematicFieldReconcileNotice(row);
    const splitMarker = row.temp_split ? `<div class="split-marker">临时拆行；字段不改会自动合回</div>` : "";
    return `<tr class="${{rowVisualClass(row, selectedStatus, syncNeeded, stock, reviewStatus)}}" data-ref="${{esc(row.ref)}}" data-refs="${{esc(row.refs)}}" data-sync-needed="${{syncNeeded ? "1" : "0"}}" data-missing-ref="${{missingRef ? "1" : "0"}}" data-review-status="${{esc(reviewStatus)}}" data-code-status="${{esc(selectedStatus)}}" data-manual-code="${{(row.manual_c_code || row.stock_c_code) ? "1" : "0"}}" data-has-code="${{code ? "1" : "0"}}" data-is-nc="${{rowIsNc(row) ? "1" : "0"}}" data-stock-low="${{stockIsLow(stock) ? "1" : "0"}}" data-stock-issue="${{stockIsIssue(stock) ? "1" : "0"}}" data-filter-key="${{esc(filterKeys)}}" data-stock-code="${{esc(code)}}" data-stock-queried="${{stockAlreadyQueried(stock) ? "1" : "0"}}" data-original-code="${{esc(original)}}" data-engineering="${{esc(row.accepted_c_code)}}" data-lcsc="${{esc(row.lcsc_match_c_code)}}">
      <td data-col="row_no" class="mono">${{index + 1}}</td>
      <td data-col="review">
        <div class="review-status">
          <select class="edit" data-field="review_status">
            ${{[
              ["Pending", "待确认"],
              ["Confirmed", "确认"],
              ["Rejected", "不确认"]
            ].map(item => `<option value="${{item[0]}}" ${{item[0] === reviewStatus ? "selected" : ""}}>${{item[1]}}</option>`).join("")}}
          </select>
          <span class="review-pill ${{reviewStatusClass(reviewStatus)}}" data-review-state>${{esc(reviewStatusText(reviewStatus))}}</span>
        </div>
      </td>
      <td data-col="refs" class="mono">${{refButtons}}${{splitMarker}}</td>
      <td data-col="page_refs" class="page-refs">${{pageRefButtons}}</td>
      <td data-col="quantity">${{esc(row.quantity || "")}}</td>
      <td data-col="comment"><textarea class="edit field-edit" data-field="comment" title="原始 Comment: ${{esc(row.original_comment_values || row.comment || "")}}">${{esc(row.comment)}}</textarea>${{fieldDiff(row, "comment")}}</td>
      <td data-col="value"><textarea class="edit field-edit mono" data-field="value" title="原始 Value: ${{esc(row.original_value_values || row.value || "")}}">${{esc(row.value)}}</textarea>${{fieldDiff(row, "value")}}</td>
      <td data-col="footprint"><textarea class="edit field-edit mono" data-field="footprint" title="原始 Footprint: ${{esc(row.original_footprint_values || row.footprint || "")}}">${{esc(row.footprint)}}</textarea>${{fieldDiff(row, "footprint")}}</td>
      <td data-col="c_code">
        <div>工程: <span class="${{codeClass}}">${{esc(row.accepted_c_code)}}</span></div>
        <div>立创: <span class="${{codeClass}}">${{esc(row.lcsc_match_c_code)}}</span></div>
        <div>生效: <span class="mono" data-effective-code>${{esc(effective)}}</span></div>
        <input class="edit mono" data-field="replacement_c_code" placeholder="替换C编码，回车确认" value="${{esc(replacement)}}">
        <select class="edit" data-field="status">
          ${{[
            ["Original", "接受原始C编码"],
            ["ChangedToNewCode", "已改动为新编码"],
            ["Rejected", "不接受/留空"]
          ].map(item => `<option value="${{item[0]}}" ${{item[0] === selectedStatus ? "selected" : ""}}>${{item[1]}}</option>`).join("")}}
        </select>
        <div><span class="status-pill ${{statusClass(selectedStatus)}}" data-code-state>${{esc(statusText(selectedStatus))}}</span></div>
      </td>
      <td data-col="search" class="links">
        <button type="button" class="small action-link" data-action="open-candidates">更换型号</button>
        <button type="button" class="small ${{syncButtonClass}}" data-action="sync-schematic" title="${{syncNeeded ? "把当前 Comment/Value/Footprint 写回 KiCad 原理图" : "当前字段和原理图一致；仍可手动写回"}}">同步原理图</button>
        ${{syncMarker}}
        ${{fieldReconcileNotice}}
        ${{syncSummary}}
        ${{row.value_search_url ? `<a target="_blank" rel="noopener" data-secondary-search="${{esc(row.value || "")}}" href="${{esc(row.value_search_url)}}">Value</a>` : ""}}
        ${{row.comment_search_url ? `<a target="_blank" rel="noopener" data-secondary-search="${{esc(row.comment || "")}}" href="${{esc(row.comment_search_url)}}">Comment</a>` : ""}}
        ${{row.value_footprint_search_url ? `<a target="_blank" rel="noopener" data-secondary-search="${{esc([row.value, row.footprint].filter(Boolean).join(" "))}}" href="${{esc(row.value_footprint_search_url)}}">Value+封装</a>` : ""}}
        ${{row.c_code_search_url ? `<a target="_blank" rel="noopener" data-secondary-search="${{esc(code)}}" href="${{esc(row.c_code_search_url)}}">C编码</a>` : ""}}
      </td>
      <td data-col="stock" data-stock-cell>${{renderStockCell(stock)}}</td>
      <td data-col="order" class="mono" data-stock-order>${{esc(stock.moq || "")}} / ${{esc(stock.mpq || "")}}</td>
      <td data-col="price" class="mono" data-stock-price>${{esc(stock.price || "")}}</td>
      <td data-col="param" class="param-text" data-stock-param data-stock-param-text="${{esc(stockParamText(stock))}}">${{renderStockParam(stock)}}</td>
      <td data-col="alternates">${{renderAlternateParts(row)}}</td>
      <td data-col="note"><textarea class="edit note" data-field="note">${{esc(note)}}</textarea></td>
    </tr>`;
  }}).join("");
  applyColumnOrder();
  applyColumnVisibility();
  applyActiveFilter();
  fitDefaultColumnWidths();
}}

function updateRowsForStock(code, stock) {{
  document.querySelectorAll("tr[data-stock-code]").forEach(tr => {{
    if (tr.dataset.stockCode === code) setStockCells(tr, stock);
    const row = rowDataForElement(tr);
    if (!row) return;
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
    const comment = isExplicitPassiveComment(fields.comment) ? fields.comment : passiveType;
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
  await loadRows(anchor);
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
  await loadRows(anchor);
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
  if (!candidateModal.classList.contains("open")) return;
  candidateModal.classList.remove("minimized");
}}

function minimizeCandidates() {{
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
  if (event.button !== 0 || event.target.closest(".modal-controls")) return;
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
  const targetKey = candidateRowKey(tr);
  if (candidateModal.classList.contains("open") && candidateModal.classList.contains("minimized") && targetKey && targetKey === candidateTargetKey) {{
    candidateTargetRow = tr;
    restoreCandidates();
    return;
  }}
  candidateSearchToken += 1;
  candidateModal.classList.remove("minimized");
  candidateTargetRow = tr;
  candidateTargetKey = targetKey;
  const refs = tr.dataset.refs || tr.dataset.ref || "";
  candidateTitle.textContent = `更换型号：${{compactRefsText(refs)}}`;
  candidateTitle.title = refs;
  candidateMinimizedTitle.textContent = candidateTitle.textContent;
  candidateKeyword.value = candidateKeywordForRow(tr);
  candidateRows.innerHTML = '<tr><td colspan="8">正在同步搜索：Chrome 官网 + 结构化候选</td></tr>';
  candidateModal.classList.add("open");
  void launchOfficialSearch(candidateKeyword.value.trim());
  void searchCandidates().catch(err => {{
    candidateRows.innerHTML = `<tr><td colspan="8" class="bad">结构化查询失败：${{esc(err.message || String(err))}}；Chrome 官网仍可继续使用</td></tr>`;
  }});
}}

function closeCandidates() {{
  candidateSearchToken += 1;
  candidateModal.classList.remove("open", "minimized");
  resetCandidateWindowGeometry();
  candidateTargetRow = null;
  candidateTargetKey = "";
  currentCandidateItems = new Map();
  currentProcurementQuotes = new Map();
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
    if (!data.ok) throw new Error(data.error || "启动 Chrome 页面失败");
    setState(`已用 Chrome 打开${{label}}`, "saved");
  }} catch (err) {{
    window.open(value, "_blank", "noopener");
    setState(`Chrome 启动失败，已改用浏览器标签页：${{err.message || String(err)}}`, "status");
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
    if (!data.ok) throw new Error(data.error || "启动 Chrome 搜索失败");
    setState(`已用 Chrome 打开立创搜索：${{value}}`, "saved");
  }} catch (err) {{
    window.open(officialSearchUrl(value), "_blank", "noopener");
    setState(`Chrome 启动失败，已改用浏览器标签页：${{err.message || String(err)}}`, "status");
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
  const lines = uniqueCandidateParamSegments(...values).map(line => {{
    const match = line.match(/^([^:：]{{1,24}})\s*[:：]\s*(.+)$/);
    if (!match) return `<div class="candidate-param-value">${{esc(line)}}</div>`;
    return `<div class="candidate-param-line"><span class="candidate-param-key">${{esc(match[1])}}</span><span class="candidate-param-value">${{esc(match[2])}}</span></div>`;
  }});
  return lines.length
    ? `<div class="candidate-param-lines">${{lines.join("")}}</div>`
    : "";
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

function renderCandidatePartnerRows(items) {{
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
      <td data-candidate-col="param">${{renderCandidateParam(channel.param || item.param || "", item.param_detail || "")}}</td>
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

function renderCandidateRows(items) {{
  if (!items.length) {{
    candidateRows.innerHTML = '<tr><td colspan="8">没有候选结果</td></tr>';
    return;
  }}
  currentCandidateItems = new Map(items.map(item => [normalizeCCode(item.c_code), item]));
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
        <div>${{esc(item.category || "")}}</div>
        <div class="status">${{esc(item.match_type || "")}}</div>
      </td>
      <td data-candidate-col="param">${{renderCandidateParam(item.param || "", item.param_detail || "", item.arrange || "")}}</td>
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
  candidateRows.innerHTML = primaryRows + renderCandidatePartnerRows(items);
  applyCandidateColumnOrder();
  applyCandidateSavedWidths();
  makeCandidateColumnsConfigurable();
}}

function candidateStockNumber(item) {{
  const value = String(item?.stock ?? "").replace(/,/g, "").trim();
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}}

async function searchCandidates() {{
  const requestToken = ++candidateSearchToken;
  const keyword = candidateKeyword.value.trim();
  if (!keyword) {{
    candidateRows.innerHTML = '<tr><td colspan="8">请输入搜索关键词</td></tr>';
    return;
  }}
  candidateRows.innerHTML = `<tr><td colspan="8">正在搜索：${{esc(keyword)}}</td></tr>`;
  const compactCode = String(keyword || "").replace(/\s+/g, "").toUpperCase();
  const exactCCode = /^C\d+$/.test(compactCode);
  let res;
  let data;
  try {{
    res = await fetch(`/api/lcsc_search?keyword=${{encodeURIComponent(keyword)}}&include_substitutes=${{exactCCode ? "0" : "1"}}`);
    if (requestToken !== candidateSearchToken || !candidateModal.classList.contains("open")) return;
    data = await res.json();
  }} catch (err) {{
    if (requestToken !== candidateSearchToken || !candidateModal.classList.contains("open")) return;
    throw err;
  }}
  if (requestToken !== candidateSearchToken || !candidateModal.classList.contains("open")) return;
  if (!data.ok) {{
    candidateRows.innerHTML = `<tr><td colspan="8" class="bad">搜索失败：${{esc(data.error || "")}}</td></tr>`;
    return;
  }}
  const rawItems = data.items || [];
  const items = inStockOnly.checked ? rawItems.filter(item => candidateStockNumber(item) > 0) : rawItems;
  if (inStockOnly.checked && rawItems.length && !items.length) {{
    candidateRows.innerHTML = '<tr><td colspan="8">当前搜索结果里没有现货候选料</td></tr>';
    return;
  }}
  renderCandidateRows(items);
}}

async function chooseCandidate(code) {{
  if (!candidateTargetRow || !code) return;
  const tr = candidateTargetRow;
  const candidate = currentCandidateItems.get(normalizeCCode(code));
  if (!candidate) throw new Error("没有找到候选料数据，请重新搜索");
  const anchor = rowAnchor(tr);
  const res = await fetch("/api/select_candidate", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{ref: tr.dataset.ref, refs: tr.dataset.refs, candidate}})
  }});
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || "保存候选料失败");
  closeCandidates();
  await loadRows(anchor);
  setState("已保存型号、数值、封装及物料参数；请核对 KiCad 封装库名后同步原理图", "saved");
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
  closeCandidates();
}}

async function addCandidateAsAlternate(code) {{
  if (!candidateTargetRow) return;
  const normalized = normalizeCCode(code);
  const item = currentCandidateItems.get(normalized);
  if (!item) throw new Error("没有找到候选料数据");
  const tr = candidateTargetRow;
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
  closeCandidates();
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
    const inputCode = normalizeCCode(tr.querySelector("[data-field='replacement_c_code']").value);
    const effectiveCode = normalizeCCode(tr.querySelector("[data-effective-code]").textContent);
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
    const row = rowDataForElement(tr) || {{}};
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
  activeButton.textContent = `查询中 0/${{codes.length}}`;
  const skippedText = skipped ? `，跳过已查 ${{skipped}} 行` : "";
  setState(`${{label}} 0/${{codes.length}}${{skippedText}}`, "status");
  setStockProgress(0, codes.length);
  async function worker() {{
    while (queue.length && isActiveStockRun(run)) {{
      const code = queue.shift();
      setState(`${{label}} ${{done}}/${{codes.length}}：${{code}}${{skippedText}}`, "status");
      activeButton.textContent = `查询中：${{code}}`;
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
        activeButton.textContent = done === codes.length ? label : `查询中 ${{done}}/${{codes.length}}`;
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
  cancelStockQueries("已停止库存查询，正在加载 BOM...");
  queryMissingButton.disabled = true;
  queryAllButton.disabled = true;
  setState("加载表格中...");
  try {{
    const refs = refsInput.value.trim();
    const bom = bomSelect.value || "";
    const res = await fetch(`/api/rows?refs=${{encodeURIComponent(refs)}}&bom=${{encodeURIComponent(bom)}}&stock=0`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "加载 BOM 失败");
    renderRows(data.rows);
    restoreRowAnchor(anchor);
    const refCount = data.rows.flatMap(row => row.refs_list || [row.ref]).length;
    setState(`已加载 ${{data.rows.length}} 个分组 / ${{refCount}} 个位号；库存需手动查询`, "saved");
  }} catch (err) {{
    setState(`加载 BOM 失败：${{err.message || String(err)}}`, "error");
  }} finally {{
    resetQueryAllButton();
  }}
}}

async function loadBomOptions() {{
  const res = await fetch("/api/boms");
  const data = await res.json();
  bomSelect.innerHTML = data.boms.map(item => `<option value="${{esc(item.path)}}" ${{item.path === data.default_bom ? "selected" : ""}}>${{esc(item.name)}}</option>`).join("");
}}

rowsEl.addEventListener("input", event => {{
  const tr = event.target.closest("tr");
  if (tr) tr.classList.add("changed");
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
  if (event.key !== "Enter") return;
  const tr = event.target.closest("tr");
  if (!tr) return;
  try {{
    if (event.target.matches("[data-field='replacement_c_code']")) {{
      event.preventDefault();
      await acceptReplacement(tr);
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
  try {{
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
    if (action === "eng") await saveRow(tr, "AcceptEngineering", tr.dataset.engineering);
    if (action === "lcsc") await saveRow(tr, "AcceptLCSC", tr.dataset.lcsc);
    if (action === "query-stock") await queryRowStock(tr);
    if (action === "manual") {{
      const manual = tr.querySelector("[data-field='manual_c_code']").value.trim();
      await saveRow(tr, "AcceptManual", manual);
    }}
    if (action === "open-candidates") await openCandidates(tr);
    if (action === "start-multi-split") startMultiSplit(currentRows.find(row => (row.group_id || row.ref) === tr.dataset.groupId) || rowDataForElement(tr), button.dataset.splitSource || "refs");
    if (action === "split-selected-refs") splitSelectedRefs(button.dataset.groupId || tr.dataset.groupId || "");
    if (action === "cancel-multi-split") cancelMultiSplit();
    if (action === "move-alternate") await updateAlternateParts(tr, "move", {{c_code: button.dataset.code || "", direction: button.dataset.direction || ""}});
    if (action === "remove-alternate") await updateAlternateParts(tr, "remove", {{c_code: button.dataset.code || ""}});
    if (action === "adopt-schematic-alternates") await updateAlternateParts(tr, "adopt_schematic");
    if (action === "clear-procurement") await updateProcurementChoice(tr, "clear");
    if (action === "adopt-schematic-procurement") await updateProcurementChoice(tr, "adopt_schematic");
    if (action === "sync-schematic") await syncSchematic(tr);
    if (action === "adopt-schematic-fields") await adoptSchematicFields(tr, button);
    if (action === "save-fields") await saveFieldEdits(tr);
    if (action === "reject") await saveRow(tr, "Reject", "");
    if (action === "save") await saveRow(tr, tr.querySelector("[data-field='status']").value || "NeedsReview", undefined);
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});

rowsEl.addEventListener("change", event => {{
  const checkbox = event.target.closest("input[data-multi-split-ref]");
  if (!checkbox) return;
  const ref = checkbox.dataset.multiSplitRef || "";
  if (checkbox.checked) multiSplitRefs.add(ref);
  else multiSplitRefs.delete(ref);
}});

function closeRefContextMenu() {{
  refContextMenu.classList.remove("open");
  contextRefTarget = null;
}}

rowsEl.addEventListener("contextmenu", event => {{
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
  try {{
    if (button.dataset.action === "choose-candidate") await chooseCandidate(button.dataset.code || "");
    if (button.dataset.action === "add-candidate-alternate") await addCandidateAsAlternate(button.dataset.code || "");
    if (button.dataset.action === "select-procurement") await selectProcurement(button.dataset.quoteKey || "");
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});

document.getElementById("runCandidateSearch").addEventListener("click", searchCandidates);
document.getElementById("openOfficialSearch").addEventListener("click", () => {{
  const keyword = candidateKeyword.value.trim();
  if (!keyword) {{
    candidateKeyword.focus();
    return;
  }}
  launchOfficialSearch(keyword);
}});
inStockOnly.addEventListener("change", searchCandidates);
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
    await syncAllSchematic();
  }} catch (err) {{
    setState(err.message, "error");
    syncAllButton.disabled = false;
  }}
}});
document.getElementById("exportChanges").addEventListener("click", async () => {{
  try {{
    await exportChanges();
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});
document.getElementById("exportPumpTable").addEventListener("click", async () => {{
  try {{
    await exportPumpTable();
  }} catch (err) {{
    setState(err.message, "error");
  }}
}});
bomSelect.addEventListener("change", () => {{
  updateUiSettings({{selectedBom: bomSelect.value || ""}});
  refsInput.value = "";
  loadRows();
}});

document.getElementById("toggleColumns").addEventListener("click", () => {{
  renderColumnPanel();
  columnPanel.classList.toggle("open");
}});
columnList.addEventListener("change", event => {{
  const input = event.target.closest("[data-column-toggle]");
  if (!input) return;
  const settings = loadUiSettings();
  const hidden = Object.assign({{}}, settings.hiddenColumns || {{}});
  hidden[input.dataset.columnToggle] = !input.checked;
  updateUiSettings({{hiddenColumns: hidden}});
  applyColumnVisibility();
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
  if (!event.target.closest(".view-wrap")) columnPanel.classList.remove("open");
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

organizeToolbar();
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
loadBomOptions().then(() => {{
  loadRows();
}});
</script>
</body>
</html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run editable KiCad part-selection helper.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "BOM" / "part_selection_config.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the UI in the default browser.")
    parser.add_argument("--bulk-sync-bom", default="", help="Sync the merged web-state of a BOM into KiCad schematics, then exit.")
    parser.add_argument("--bulk-sync-dry-run", action="store_true", help="Only report what the bulk schematic sync would do.")
    parser.add_argument("--bulk-sync-no-notify", action="store_true", help="Do not notify or activate KiCad after bulk schematic sync.")
    parser.add_argument("--bulk-sync-no-prepare", action="store_true", help="Do not ask KiCad to save open schematic windows before bulk sync.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = PartSelectionApp(args.config)
    if args.bulk_sync_bom:
        result = app.bulk_sync_bom_state_to_schematic(
            args.bulk_sync_bom,
            ref_map=DEFAULT_BULK_SYNC_REF_MAP,
            dry_run=args.bulk_sync_dry_run,
            notify=not args.bulk_sync_no_notify,
            prepare=not args.bulk_sync_no_prepare,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    url = f"http://{args.host}:{args.port}/"
    print(f"Part selection helper: {url}")
    print(f"Config: {app.config['config_path']}")
    print(f"Overrides: {app.overrides_path}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
