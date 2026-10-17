# -*- coding: utf-8 -*-
"""
Generate a small part-selection review page for a list of KiCad designators.

The tool reads the current schematic pages, the source BOM CSV, and the latest
LCSC-export BOM CSV when present. It then writes:
  - an HTML page with LCSC search links
  - an XLSX workbook for manual C-code review
"""

from __future__ import annotations

import argparse
import csv
import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill


DEFAULT_REFS = ""
BOM_SOURCE = "source.csv"
GENERIC_COMMENTS = {"", "CAPACITOR", "RESISTOR", "INDUCTOR", "DIODE", "MOSFET", "TRANSISTOR", "NC"}


@dataclass
class PartInfo:
    ref: str
    page: str = ""
    comment: str = ""
    value: str = ""
    footprint: str = ""
    description: str = ""
    lcsc_alt: str = ""
    lcsc_alt_info: str = ""
    procurement_url: str = ""
    procurement_info: str = ""
    accepted_c_code: str = ""
    lcsc_match_c_code: str = ""
    bom_refs: str = ""
    page_refs: str = ""
    quantity: str = ""
    status: str = "Found"
    notes: list[str] = field(default_factory=list)


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def split_refs(text: object) -> list[str]:
    result: list[str] = []
    for part in re.split(r"[,;，；\s]+", str(text or "")):
        ref = part.strip().upper()
        if ref:
            result.append(ref)
    return result


def read_target_refs(project_root: Path, refs_arg: str | None, input_path: Path | None) -> list[str]:
    raw = refs_arg or ""
    if input_path and input_path.exists():
        raw = input_path.read_text(encoding="utf-8-sig")
    if not raw:
        default_file = project_root / "BOM" / "part_selection_input.txt"
        raw = default_file.read_text(encoding="utf-8-sig") if default_file.exists() else DEFAULT_REFS

    refs: list[str] = []
    for ref in split_refs(raw):
        if ref not in refs:
            refs.append(ref)
    return refs


def unescape_kicad_string(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        ch = value[index]
        if ch != "\\" or index + 1 >= len(value):
            result.append(ch)
            index += 1
            continue
        nxt = value[index + 1]
        if nxt == "n":
            result.append("\n")
        elif nxt == "r":
            result.append("\r")
        elif nxt == "t":
            result.append("\t")
        elif nxt == '"':
            result.append('"')
        elif nxt == "\\":
            result.append("\\")
        else:
            result.extend([ch, nxt])
        index += 2
    return "".join(result).strip()


def escape_kicad_string(value: object) -> str:
    return (
        str(value or "")
        .replace("\\", r"\\")
        .replace("\r", r"\r")
        .replace("\n", r"\n")
        .replace("\t", r"\t")
        .replace('"', r"\"")
    )


def iter_symbol_block_spans(text: str):
    start = 0
    needle = "(symbol"
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            return
        depth = 0
        in_string = False
        escaped = False
        for pos in range(idx, len(text)):
            ch = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    yield idx, pos + 1, text[idx : pos + 1]
                    start = pos + 1
                    break
        else:
            return


def iter_symbol_blocks(text: str):
    for _start, _end, block in iter_symbol_block_spans(text):
        yield block


def parse_symbol_properties(block: str) -> dict[str, str]:
    props: dict[str, str] = {}
    pattern = re.compile(r'\(property\s+"((?:\\.|[^"])*)"\s+"((?:\\.|[^"])*)"')
    for key, value in pattern.findall(block):
        props[unescape_kicad_string(key)] = unescape_kicad_string(value)
    return props


SCHEMATIC_FIELD_PROPERTIES = {
    "comment": "Comment",
    "value": "Value",
    "footprint": "Footprint",
    "lcsc_c_code": "LCSC",
    "lcsc_url": "LCSC_URL",
    "description": "Description",
    "lcsc_alt": "LCSC_ALT",
    "lcsc_alt_info": "LCSC_ALT_INFO",
    "procurement_url": "PROCUREMENT_URL",
    "procurement_info": "PROCUREMENT_INFO",
}


def replace_symbol_property_value(block: str, property_name: str, value: object) -> tuple[str, str | None]:
    escaped_name = re.escape(escape_kicad_string(property_name))
    pattern = re.compile(rf'(\(property\s+"{escaped_name}"\s+")((?:\\.|[^"])*)(")')
    match = pattern.search(block)
    if not match:
        return block, None
    old_value = unescape_kicad_string(match.group(2))
    new_block = block[: match.start(2)] + escape_kicad_string(value) + block[match.end(2) :]
    return new_block, old_value


def add_symbol_property(block: str, property_name: str, value: object) -> str:
    property_line = (
        f'\n    (property "{escape_kicad_string(property_name)}" "{escape_kicad_string(value)}" '
        '(at 0 0 0) (effects (font (size 1.27 1.27)) hide))'
    )
    close_index = block.rfind(")")
    if close_index < 0:
        return block + property_line
    return block[:close_index] + property_line + "\n  " + block[close_index:]


def update_schematic_symbol_fields(doc_dir: Path, refs: list[str], fields: dict[str, object]) -> dict[str, object]:
    target_refs = {ref.strip().upper() for ref in refs if ref and ref.strip()}
    field_updates = {
        key: str(fields.get(key) or "").strip()
        for key in SCHEMATIC_FIELD_PROPERTIES
        if key in fields
    }
    if not target_refs:
        raise ValueError("ref is required")
    if not field_updates:
        raise ValueError("no schematic fields were provided")
    if not doc_dir.exists():
        raise FileNotFoundError(f"schematic directory not found: {doc_dir}")

    changes: dict[str, dict[str, dict[str, str]]] = {}
    pages: dict[str, str] = {}
    found_refs: set[str] = set()
    changed_paths: list[Path] = []

    for path in sorted(doc_dir.glob("*.kicad_sch")):
        text = path.read_text(encoding="utf-8", errors="replace")
        pieces: list[str] = []
        last = 0
        file_changed = False
        for start, end, block in iter_symbol_block_spans(text):
            props = parse_symbol_properties(block)
            ref = props.get("Reference", "").strip().upper()
            if ref not in target_refs:
                continue
            found_refs.add(ref)
            pages[ref] = path.stem
            new_block = block
            ref_changes: dict[str, dict[str, str]] = {}
            for field_key, field_value in field_updates.items():
                property_name = SCHEMATIC_FIELD_PROPERTIES[field_key]
                new_block, old_value = replace_symbol_property_value(new_block, property_name, field_value)
                if old_value is None and not field_value:
                    continue
                if old_value is None:
                    new_block = add_symbol_property(new_block, property_name, field_value)
                    old_value = ""
                if old_value == field_value:
                    continue
                ref_changes[field_key] = {"old": old_value, "new": field_value}
            if ref_changes:
                pieces.append(text[last:start])
                pieces.append(new_block)
                last = end
                changes[ref] = ref_changes
                file_changed = True
        if file_changed:
            pieces.append(text[last:])
            path.write_text("".join(pieces), encoding="utf-8")
            changed_paths.append(path)

    missing_refs = sorted(target_refs - found_refs)
    return {
        "ok": True,
        "updated_refs": sorted(changes, key=ref_sort_key_for_text),
        "missing_refs": missing_refs,
        "changes": changes,
        "pages": pages,
        "paths": [str(path) for path in changed_paths],
    }


def update_schematic_symbol_field_map(doc_dir: Path, ref_fields: dict[str, dict[str, object]]) -> dict[str, object]:
    target_updates: dict[str, dict[str, str]] = {}
    for ref, fields in ref_fields.items():
        normalized_ref = str(ref or "").strip().upper()
        if not normalized_ref:
            continue
        field_updates = {
            key: str(fields.get(key) or "").strip()
            for key in SCHEMATIC_FIELD_PROPERTIES
            if key in fields
        }
        if field_updates:
            target_updates[normalized_ref] = field_updates

    if not target_updates:
        raise ValueError("no schematic field updates were provided")
    if not doc_dir.exists():
        raise FileNotFoundError(f"schematic directory not found: {doc_dir}")

    changes: dict[str, dict[str, dict[str, str]]] = {}
    pages: dict[str, str] = {}
    found_refs: set[str] = set()
    changed_paths: list[Path] = []

    for path in sorted(doc_dir.glob("*.kicad_sch")):
        text = path.read_text(encoding="utf-8", errors="replace")
        pieces: list[str] = []
        last = 0
        file_changed = False
        for start, end, block in iter_symbol_block_spans(text):
            props = parse_symbol_properties(block)
            ref = props.get("Reference", "").strip().upper()
            field_updates = target_updates.get(ref)
            if field_updates is None:
                continue
            found_refs.add(ref)
            pages[ref] = path.stem
            new_block = block
            ref_changes: dict[str, dict[str, str]] = {}
            for field_key, field_value in field_updates.items():
                property_name = SCHEMATIC_FIELD_PROPERTIES[field_key]
                new_block, old_value = replace_symbol_property_value(new_block, property_name, field_value)
                if old_value is None and not field_value:
                    continue
                if old_value is None:
                    new_block = add_symbol_property(new_block, property_name, field_value)
                    old_value = ""
                if old_value == field_value:
                    continue
                ref_changes[field_key] = {"old": old_value, "new": field_value}
            if ref_changes:
                pieces.append(text[last:start])
                pieces.append(new_block)
                last = end
                existing_changes = changes.setdefault(ref, {})
                existing_changes.update(ref_changes)
                file_changed = True
        if file_changed:
            pieces.append(text[last:])
            path.write_text("".join(pieces), encoding="utf-8")
            changed_paths.append(path)

    missing_refs = sorted(set(target_updates) - found_refs, key=ref_sort_key_for_text)
    return {
        "ok": True,
        "updated_refs": sorted(changes, key=ref_sort_key_for_text),
        "missing_refs": missing_refs,
        "changes": changes,
        "pages": pages,
        "paths": [str(path) for path in changed_paths],
    }


def read_schematic_symbol_field_values(
    doc_dir: Path,
    refs: list[str],
    field_keys: list[str] | tuple[str, ...],
) -> dict[str, dict[str, str]]:
    target_refs = {ref.strip().upper() for ref in refs if ref and ref.strip()}
    wanted_fields = [key for key in field_keys if key in SCHEMATIC_FIELD_PROPERTIES]
    values: dict[str, dict[str, str]] = {}
    if not target_refs or not wanted_fields or not doc_dir.exists():
        return values

    property_to_key = {SCHEMATIC_FIELD_PROPERTIES[key]: key for key in wanted_fields}
    for path in sorted(doc_dir.glob("*.kicad_sch")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for _start, _end, block in iter_symbol_block_spans(text):
            props = parse_symbol_properties(block)
            ref = props.get("Reference", "").strip().upper()
            if ref not in target_refs:
                continue
            values[ref] = {
                field_key: props.get(property_name, "")
                for property_name, field_key in property_to_key.items()
            }
    return values


def ref_sort_key_for_text(ref: str) -> tuple[str, int, str]:
    match = re.match(r"^([A-Z]+)(\d+)([A-Z]?)$", ref)
    if not match:
        return (ref, 0, "")
    return (match.group(1), int(match.group(2)), match.group(3))


def load_schematic_sheet_names(doc_dir: Path) -> dict[Path, str]:
    from fpga_pin_assignment import parse_sexpr

    names: dict[Path, set[str]] = {}
    hierarchy_files = set(doc_dir.glob("*.kicad_sch")) | set(doc_dir.parent.glob("*.kicad_sch"))
    for path in sorted(hierarchy_files):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"\(sheet\s", text):
            continue
        for sheet in parse_sexpr(text).children_named("sheet"):
            props = {prop.arg(1): prop.arg(2) for prop in sheet.children_named("property")}
            filename = props.get("Sheetfile", "").strip()
            name = props.get("Sheetname", "").strip()
            if filename and name:
                target = (path.parent / filename).resolve()
                names.setdefault(target, set()).add(name)
    # A multiply instantiated file needs instance-aware references to choose a name.
    return {path: next(iter(values)) for path, values in names.items() if len(values) == 1}


def load_schematic_parts(doc_dir: Path) -> dict[str, PartInfo]:
    parts: dict[str, PartInfo] = {}
    if not doc_dir.exists():
        return parts

    sheet_names = load_schematic_sheet_names(doc_dir)
    for path in sorted(doc_dir.glob("*.kicad_sch")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for block in iter_symbol_blocks(text):
            props = parse_symbol_properties(block)
            ref = props.get("Reference", "").strip().upper()
            if not re.match(r"^[A-Z]+\d+[A-Z]?$", ref):
                continue
            parts[ref] = PartInfo(
                ref=ref,
                page=sheet_names.get(path.resolve(), path.stem),
                comment=props.get("Comment", ""),
                value=props.get("Value", ""),
                footprint=props.get("Footprint", ""),
                description=props.get("Description", ""),
                lcsc_alt=props.get("LCSC_ALT", ""),
                lcsc_alt_info=props.get("LCSC_ALT_INFO", ""),
                procurement_url=props.get("PROCUREMENT_URL", ""),
                procurement_info=props.get("PROCUREMENT_INFO", ""),
                accepted_c_code=props.get("LCSC", ""),
            )
    return parts


def find_latest_lcsc_csv(bom_dir: Path) -> Path | None:
    files = sorted(
        bom_dir.glob("*LCSC*.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def make_unique_headers(headers: list[object]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for idx, header in enumerate(headers, start=1):
        name = str(header or "").strip() or f"Column{idx}"
        count = seen.get(name, 0) + 1
        seen[name] = count
        result.append(name if count == 1 else f"{name}#{count}")
    return result


def iter_bom_rows(bom_path: Path):
    if not bom_path.exists():
        return

    suffix = bom_path.suffix.lower()
    if suffix == ".csv":
        with bom_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                headers = make_unique_headers(next(reader))
            except StopIteration:
                return
            for values in reader:
                row = {header: str(values[idx]).strip() if idx < len(values) and values[idx] is not None else "" for idx, header in enumerate(headers)}
                if any(row.values()):
                    yield row
        return

    if suffix in {".xlsx", ".xlsm"}:
        workbook = openpyxl.load_workbook(bom_path, read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[0]
            rows = sheet.iter_rows(values_only=True)
            try:
                headers = make_unique_headers(list(next(rows)))
            except StopIteration:
                return
            for values in rows:
                row = {header: str(values[idx]).strip() if idx < len(values) and values[idx] is not None else "" for idx, header in enumerate(headers)}
                if any(row.values()):
                    yield row
        finally:
            workbook.close()
        return

    raise ValueError(f"Unsupported BOM file type: {bom_path}")


def pick_field(row: dict[str, str], names: list[str]) -> str:
    for name in names:
        value = row.get(name, "")
        if value:
            return value
    return ""


def read_refs_from_bom(bom_path: Path) -> list[str]:
    refs: list[str] = []
    for row in iter_bom_rows(bom_path):
        refs_text = pick_field(row, ["Designator", "位号", "BOM分组位号", "KiCad搜索位号"])
        for ref in split_refs(refs_text):
            if ref not in refs:
                refs.append(ref)
    return refs


def load_source_bom(bom_csv: Path) -> dict[str, dict[str, str]]:
    by_ref: dict[str, dict[str, str]] = {}
    for row in iter_bom_rows(bom_csv):
        refs_text = pick_field(row, ["Designator", "位号", "BOM分组位号", "KiCad搜索位号"])
        refs = split_refs(refs_text)
        for ref in refs:
            by_ref[ref] = {
                "Comment": pick_field(row, ["Comment", "原始型号"]),
                "Description": pick_field(row, ["Description", "商品名称", "参数"]),
                "Footprint": pick_field(row, ["Footprint", "封装"]),
                "Value": pick_field(row, ["Value", "参数/规格", "参数"]),
                "BOMRefs": refs_text,
                "Quantity": pick_field(row, ["单套用量", "Quantity"]) or (str(len(refs)) if refs else ""),
            }
    return by_ref


def load_lcsc_bom(lcsc_csv: Path | None) -> dict[str, dict[str, str]]:
    by_ref: dict[str, dict[str, str]] = {}
    if not lcsc_csv:
        return by_ref

    for row in iter_bom_rows(lcsc_csv):
        refs_text = pick_field(row, ["位号", "Designator", "BOM分组位号", "KiCad搜索位号"])
        refs = split_refs(refs_text)
        for ref in refs:
            by_ref[ref] = {
                "AcceptedCCode": pick_field(row, ["工程/已接受C编码", "当前工程C编码"]),
                "LCSCMatchCCode": pick_field(row, ["立创匹配C编码", "当前立创匹配C编码", "商品编号"]),
                "BOMRefs": refs_text,
                "PageRefs": pick_field(row, ["页面位号"]),
                "Quantity": pick_field(row, ["单套用量", "Quantity"]),
            }
    return by_ref


def merge_part_data(
    refs: list[str],
    project_root: Path,
    bom_csv: Path,
    lcsc_csv: Path | None,
    schematic_dir: Path | None = None,
) -> list[PartInfo]:
    schematic = load_schematic_parts(schematic_dir or (project_root / "DOC"))
    source_bom = load_source_bom(bom_csv)
    lcsc_bom = load_lcsc_bom(lcsc_csv)

    rows: list[PartInfo] = []
    for ref in refs:
        part = schematic.get(ref, PartInfo(ref=ref, status="NotFound"))
        source = source_bom.get(ref, {})
        lcsc = lcsc_bom.get(ref, {})

        if not part.comment:
            part.comment = source.get("Comment", "")
        if not part.value:
            part.value = source.get("Value", "")
        if not part.footprint:
            part.footprint = source.get("Footprint", "")
        if not part.description:
            part.description = source.get("Description", "")

        part.accepted_c_code = lcsc.get("AcceptedCCode", "")
        part.lcsc_match_c_code = lcsc.get("LCSCMatchCCode", "")
        part.bom_refs = lcsc.get("BOMRefs", "") or source.get("BOMRefs", "")
        part.page_refs = lcsc.get("PageRefs", "")
        part.quantity = lcsc.get("Quantity", "") or source.get("Quantity", "")

        if part.status == "NotFound":
            part.notes.append("not found in DOC/*.kicad_sch")
        if not part.accepted_c_code and not part.lcsc_match_c_code:
            part.notes.append("no current C-code")
        rows.append(part)
    return rows


def lcsc_search_url(keyword: str) -> str:
    return f"https://so.szlcsc.com/global.html?k={quote_plus(keyword.strip())}"


def kicad_jump_url(ref: str, page: str = "") -> str:
    url = f"kicad-sch-search://{quote_plus(ref.strip().upper())}"
    if page:
        url += f"?page={quote_plus(page.strip())}"
    return url


def page_map_from_page_refs(page_refs: str) -> dict[str, str]:
    ref_to_page: dict[str, str] = {}
    for chunk in str(page_refs or "").split(";"):
        if ":" not in chunk:
            continue
        page, refs_text = chunk.split(":", 1)
        page = page.strip()
        for ref in split_refs(refs_text):
            ref_to_page[ref] = page
    return ref_to_page


def render_ref_jump(ref: str, page: str = "") -> str:
    ref = ref.strip().upper()
    if not ref:
        return ""
    safe_ref = html.escape(ref)
    safe_url = html.escape(kicad_jump_url(ref, page))
    safe_title = html.escape(f"跳转 KiCad {ref}")
    return (
        f'<button type="button" class="ref-link" data-jump="{safe_url}" '
        f'data-ref-value="{safe_ref}" title="{safe_title}">{safe_ref}</button>'
    )


def render_ref_list(refs_text: str, default_page: str = "", ref_to_page: dict[str, str] | None = None) -> str:
    refs = split_refs(refs_text)
    if not refs:
        return html.escape(refs_text or "")
    links = []
    for ref in refs:
        page = ref_to_page.get(ref, default_page) if ref_to_page else default_page
        links.append(render_ref_jump(ref, page))
    return "<span class='ref-list'>" + "<span class='sep'>,</span>".join(links) + "</span>"


def render_page_refs(page_refs: str) -> str:
    chunks = []
    for chunk in str(page_refs or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            chunks.append(render_ref_list(chunk))
            continue
        page, refs_text = chunk.split(":", 1)
        page = page.strip()
        chunks.append(f"{html.escape(page)}: {render_ref_list(refs_text, page)}")
    return "<br>".join(chunks)


def search_keyword(part: PartInfo) -> str:
    comment = part.comment.strip()
    if comment.upper() not in GENERIC_COMMENTS:
        base = comment
    else:
        base = part.value.strip()
    pieces = [base, part.value.strip() if base != part.value.strip() else "", part.footprint.strip()]
    return " ".join(piece for piece in pieces if piece).strip()


def write_excel(rows: list[PartInfo], output_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PartSelection"

    headers = [
        "位号",
        "KiCad搜索位号",
        "页面",
        "Comment",
        "Value",
        "Footprint",
        "Description",
        "当前工程C编码",
        "当前立创匹配C编码",
        "BOM分组位号",
        "页面位号",
        "单套用量",
        "搜索关键词",
        "立创搜Value",
        "立创搜Comment",
        "立创搜Value+Footprint",
        "搜当前C编码",
        "推荐C编码",
        "是否接受",
        "备注",
        "状态",
    ]
    ws.append(headers)

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    link_font = Font(color="0563C1", underline="single")
    red_font = Font(color="C00000")

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for part in rows:
        keyword = search_keyword(part)
        value_keyword = part.value.strip()
        comment_keyword = part.comment.strip()
        vf_keyword = " ".join(x for x in [part.value.strip(), part.footprint.strip()] if x)
        c_keyword = part.accepted_c_code or part.lcsc_match_c_code

        ws.append(
            [
                part.ref,
                part.ref,
                part.page,
                part.comment,
                part.value,
                part.footprint,
                part.description,
                part.accepted_c_code,
                part.lcsc_match_c_code,
                part.bom_refs,
                part.page_refs,
                part.quantity,
                keyword,
                "搜索" if value_keyword else "",
                "搜索" if comment_keyword else "",
                "搜索" if vf_keyword else "",
                "搜索" if c_keyword else "",
                "",
                "",
                "; ".join(part.notes),
                part.status,
            ]
        )
        row_idx = ws.max_row
        ws.cell(row=row_idx, column=2).hyperlink = kicad_jump_url(part.ref, part.page)
        ws.cell(row=row_idx, column=2).font = link_font
        link_specs = {
            14: value_keyword,
            15: comment_keyword,
            16: vf_keyword,
            17: c_keyword,
        }
        for col_idx, link_keyword in link_specs.items():
            if not link_keyword:
                continue
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.hyperlink = lcsc_search_url(link_keyword)
            cell.font = link_font

        if part.accepted_c_code and part.lcsc_match_c_code and part.accepted_c_code != part.lcsc_match_c_code:
            ws.cell(row=row_idx, column=8).font = red_font
            ws.cell(row=row_idx, column=9).font = red_font

    widths = {
        "A": 12,
        "B": 14,
        "C": 18,
        "D": 24,
        "E": 18,
        "F": 18,
        "G": 44,
        "H": 18,
        "I": 18,
        "J": 44,
        "K": 56,
        "L": 12,
        "M": 32,
        "N": 12,
        "O": 14,
        "P": 18,
        "Q": 14,
        "R": 16,
        "S": 12,
        "T": 30,
        "U": 12,
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    wb.save(output_path)


def write_html(rows: list[PartInfo], output_path: Path, source_bom: Path, lcsc_bom: Path | None) -> None:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    style = """
body { font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif; margin: 24px; color: #222; }
h1 { font-size: 22px; margin-bottom: 6px; }
.meta { color: #666; margin-bottom: 18px; line-height: 1.6; }
table { border-collapse: collapse; width: 100%; table-layout: fixed; }
th { background: #1f4e78; color: white; padding: 8px; border: 1px solid #c8d1da; font-size: 13px; }
td { padding: 7px; border: 1px solid #d6dde5; vertical-align: top; font-size: 13px; word-break: break-word; }
tr:nth-child(even) { background: #f7fafc; }
a { color: #0563c1; text-decoration: none; }
.links a { display: inline-block; margin: 0 8px 4px 0; }
.toolbar { display: flex; gap: 8px; align-items: center; margin: 12px 0 18px; flex-wrap: wrap; }
button { border: 1px solid #9aa9b8; background: #f7fafc; color: #1f2933; padding: 5px 10px; border-radius: 4px; cursor: pointer; }
button:hover { background: #edf2f7; }
.jump { border-color: #1f4e78; background: #1f4e78; color: white; margin-right: 6px; }
.jump:hover { background: #173957; color: white; }
.ref-link { border: 0; background: transparent; color: #0563c1; padding: 0 2px; margin: 0; font: inherit; font-family: Consolas, monospace; text-decoration: underline; }
.ref-link:hover { background: #e7f0fb; color: #034b87; }
.ref-list .sep { color: #777; margin: 0 2px; }
.hint { color: #666; }
.copied { color: #008000; font-weight: 600; }
.bad { color: #c00000; font-weight: 600; }
.mono { font-family: Consolas, monospace; }
"""
    table_rows: list[str] = []
    for part in rows:
        keyword = search_keyword(part)
        links = []
        if part.value:
            links.append(f'<a href="{html.escape(lcsc_search_url(part.value))}">搜Value</a>')
        if part.comment:
            links.append(f'<a href="{html.escape(lcsc_search_url(part.comment))}">搜Comment</a>')
        vf_keyword = " ".join(x for x in [part.value.strip(), part.footprint.strip()] if x)
        if vf_keyword:
            links.append(f'<a href="{html.escape(lcsc_search_url(vf_keyword))}">搜Value+封装</a>')
        code_keyword = part.accepted_c_code or part.lcsc_match_c_code
        if code_keyword:
            links.append(f'<a href="{html.escape(lcsc_search_url(code_keyword))}">搜当前C编码</a>')

        code_class = "bad" if part.accepted_c_code and part.lcsc_match_c_code and part.accepted_c_code != part.lcsc_match_c_code else ""
        ref_to_page = page_map_from_page_refs(part.page_refs)
        table_rows.append(
            f'<tr data-ref="{html.escape(part.ref)}">'
            f"<td class='mono'>{render_ref_jump(part.ref, part.page)}</td>"
            f"<td><button type='button' class='jump' data-jump='{html.escape(kicad_jump_url(part.ref, part.page))}' data-ref-value='{html.escape(part.ref)}'>跳转KiCad</button><button type='button' data-copy='{html.escape(part.ref)}'>复制</button></td>"
            f"<td>{html.escape(part.page)}</td>"
            f"<td>{html.escape(part.comment)}</td>"
            f"<td>{html.escape(part.value)}</td>"
            f"<td>{html.escape(part.footprint)}</td>"
            f"<td>{html.escape(part.description)}</td>"
            f"<td class='{code_class}'>{html.escape(part.accepted_c_code)}</td>"
            f"<td class='{code_class}'>{html.escape(part.lcsc_match_c_code)}</td>"
            f"<td>{html.escape(keyword)}</td>"
            f"<td class='links'>{''.join(links)}</td>"
            f"<td>{render_ref_list(part.bom_refs, part.page, ref_to_page)}</td>"
            f"<td>{render_page_refs(part.page_refs)}</td>"
            f"<td>{html.escape('; '.join(part.notes))}</td>"
            "</tr>"
        )

    source_line = html.escape(str(source_bom))
    lcsc_line = html.escape(str(lcsc_bom)) if lcsc_bom else "未找到"
    refs_js = ", ".join(f'"{html.escape(part.ref)}"' for part in rows)
    output_path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>器件选型搜索助手</title>
<style>{style}</style>
</head>
<body>
<h1>器件选型搜索助手</h1>
<div class="meta">
生成时间：{html.escape(generated_at)}<br>
源 BOM：{source_line}<br>
当前 LCSC BOM：{lcsc_line}
</div>
<div class="toolbar">
<button type="button" id="copyNext">复制下一个 KiCad 搜索位号</button>
<button type="button" id="jumpNext">跳转下一个 KiCad 位号</button>
<span id="copyState" class="hint">首次使用先运行 tools/install_kicad_schematic_search_protocol.ps1。KiCad 原理图编辑器需要已打开；跳转会使用 KiCad 搜索面板定位第一条结果。</span>
</div>
<table>
<thead>
<tr>
<th>位号</th><th>KiCad搜索</th><th>页面</th><th>Comment</th><th>Value</th><th>Footprint</th><th>Description</th>
<th>工程C编码</th><th>立创匹配C编码</th><th>搜索关键词</th><th>链接</th><th>BOM分组位号</th><th>页面位号</th><th>备注</th>
</tr>
</thead>
<tbody>
{''.join(table_rows)}
</tbody>
</table>
<script>
const refs = [{refs_js}];
let cursor = 0;
const state = document.getElementById("copyState");

async function copyText(text) {{
  await navigator.clipboard.writeText(text);
  state.textContent = `已复制 ${{text}}，在 KiCad 原理图搜索框粘贴即可。`;
  state.className = "copied";
}}

document.querySelectorAll("[data-copy]").forEach(button => {{
  button.addEventListener("click", () => copyText(button.dataset.copy));
}});

document.querySelectorAll("[data-jump]").forEach(button => {{
  button.addEventListener("click", () => {{
    const ref = button.dataset.refValue || "";
    state.textContent = `已请求跳转 ${{ref}}；如果无反应，检查浏览器是否拦截外部协议。`;
    state.className = "hint";
    window.location.href = button.dataset.jump;
  }});
}});

document.getElementById("copyNext").addEventListener("click", () => {{
  if (!refs.length) return;
  const ref = refs[cursor % refs.length];
  cursor += 1;
  copyText(ref);
}});

document.getElementById("jumpNext").addEventListener("click", () => {{
  if (!refs.length) return;
  const ref = refs[cursor % refs.length];
  cursor += 1;
  const row = document.querySelector(`[data-ref="${{CSS.escape(ref)}}"]`);
  const button = row ? row.querySelector("[data-jump]") : null;
  if (button) {{
    button.click();
  }} else {{
    window.location.href = `kicad-sch-search://${{encodeURIComponent(ref)}}`;
  }}
}});
</script>
</body>
</html>
""",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate LCSC search helper files for KiCad designators.")
    parser.add_argument("--refs", help="Comma/space separated refs. Overrides --input.")
    parser.add_argument("--input", type=Path, help="Text file containing refs. Defaults to BOM/part_selection_input.txt.")
    parser.add_argument("--bom", type=Path, help=f"Source BOM CSV. Defaults to BOM/{BOM_SOURCE}.")
    parser.add_argument("--lcsc-bom", type=Path, help="Latest exported LCSC BOM CSV. Defaults to newest BOM/*LCSC*.csv.")
    parser.add_argument("--out-dir", type=Path, help="Output directory. Defaults to BOM/.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = project_root_from_script()
    bom_dir = project_root / "BOM"
    refs = read_target_refs(project_root, args.refs, args.input)
    source_bom = args.bom or (bom_dir / BOM_SOURCE)
    lcsc_bom = args.lcsc_bom if args.lcsc_bom else find_latest_lcsc_csv(bom_dir)
    out_dir = args.out_dir or bom_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = merge_part_data(refs, project_root, source_bom, lcsc_bom)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx_path = out_dir / f"part_selection_review_{stamp}.xlsx"
    html_path = out_dir / f"part_selection_review_{stamp}.html"
    write_excel(rows, xlsx_path)
    write_html(rows, html_path, source_bom, lcsc_bom)

    print(f"Refs: {len(refs)}")
    print(f"Found: {sum(1 for row in rows if row.status == 'Found')}")
    print(f"Missing: {sum(1 for row in rows if row.status != 'Found')}")
    print(f"HTML: {html_path}")
    print(f"XLSX: {xlsx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
