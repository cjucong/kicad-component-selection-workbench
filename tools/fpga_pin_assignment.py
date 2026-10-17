"""Local, dependency-free U46 pin assignment workbench.

The editor reads the current schematic on every refresh. Apply moves local
labels, no-connect markers, and isolated one-segment FPGA label stubs at U46.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SCHEMATIC = ROOT / "DOC" / "XC7A75T.kicad_sch"
LABEL_LIST = ROOT / "Temp" / "需要分配的网络标签.md"
HTML = Path(__file__).with_suffix(".html")
CHANGELOG = ROOT / "CHANGELOG.md"
ADC_LABELS = frozenset({
    "SDO0", "SDO1", "SDO2", "SDO3", "SDO4", "SDO5", "SDO6", "SDO7",
    "BUSY", "SDI", "SCK", "/CS", "CNV", "/RESET",
})
CONFIG_LABEL_PIN_NAMES = {
    "BOOT_PROGRAM_B": "PROGRAM_B_0",
    "BOOT_INIT_B": "INIT_B_0",
    "BOOT_CCLK": "CCLK_0",
    "BOOT_DIN": "IO_L1N_T0_D01_DIN_14",
}
IO_BANKS_3V3 = frozenset({"14", "15", "16", "35"})
WRITE_LOCK = threading.Lock()


@dataclass
class Node:
    value: str | None
    start: int
    end: int
    children: list["Node"] = field(default_factory=list)

    @property
    def head(self) -> str:
        return self.children[0].value if self.children and self.children[0].value else ""

    def child(self, head: str) -> "Node | None":
        return next((part for part in self.children if part.head == head), None)

    def children_named(self, head: str) -> list["Node"]:
        return [part for part in self.children if part.head == head]

    def arg(self, index: int = 1) -> str:
        if index < len(self.children):
            return self.children[index].value or ""
        return ""


def parse_sexpr(source: str) -> Node:
    """Parse KiCad S-expressions while retaining exact source offsets."""
    length = len(source)
    index = 0

    def parse() -> Node:
        nonlocal index
        while index < length and source[index].isspace():
            index += 1
        if index >= length:
            raise ValueError("Unexpected end of schematic")
        start = index
        if source[index] == "(":
            index += 1
            children: list[Node] = []
            while True:
                while index < length and source[index].isspace():
                    index += 1
                if index >= length:
                    raise ValueError("Unclosed KiCad expression")
                if source[index] == ")":
                    index += 1
                    return Node(None, start, index, children)
                children.append(parse())
        if source[index] == '"':
            index += 1
            chars: list[str] = []
            while index < length:
                char = source[index]
                if char == '"':
                    index += 1
                    return Node("".join(chars), start, index)
                if char == "\\" and index + 1 < length:
                    index += 1
                    escape = source[index]
                    chars.append({"n": "\n", "r": "\r", "t": "\t"}.get(escape, escape))
                else:
                    chars.append(char)
                index += 1
            raise ValueError("Unclosed KiCad string")
        while index < length and not source[index].isspace() and source[index] not in "()":
            index += 1
        if index == start:
            raise ValueError(f"Unexpected character at offset {index}")
        return Node(source[start:index], start, index)

    result = parse()
    if result.head != "kicad_sch":
        raise ValueError("Not a KiCad schematic")
    return result


def point(x: float, y: float) -> tuple[int, int]:
    return round(x * 10000), round(y * 10000)


def at(node: Node) -> tuple[float, float, int]:
    value = node.child("at")
    if value is None:
        raise ValueError("Object has no position")
    return float(value.arg(1)), float(value.arg(2)), int(float(value.arg(3) or 0))


def text_arg(node: Node, head: str) -> str:
    child = node.child(head)
    return child.arg() if child else ""


def format_number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


@dataclass
class Pin:
    ball: str
    name: str
    bank: str
    unit: int
    x: float
    y: float
    angle: int
    labels: list[Node] = field(default_factory=list)
    wire_names: list[str] = field(default_factory=list)
    no_connects: list[Node] = field(default_factory=list)
    wired: bool = False
    stub_wire: Node | None = None
    stub_label: Node | None = None

    @property
    def movable_stub(self) -> bool:
        return self.stub_wire is not None and self.stub_label is not None

    @property
    def location(self) -> tuple[int, int]:
        return point(self.x, self.y)


@dataclass
class State:
    source: str
    digest: str
    labels: list[str]
    pins: dict[str, Pin]
    assignments: dict[str, str]
    duplicate_labels: list[str]
    newline: str


def read_label_list(path: Path | None = None) -> tuple[list[str], list[str]]:
    path = path or LABEL_LIST
    if not path.exists():
        raise FileNotFoundError(f"标签清单不存在：{path}")
    labels: list[str] = []
    duplicates: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if name in labels:
            duplicates.append(name)
        else:
            labels.append(name)
    return labels, duplicates


def load_state(schematic: Path | None = None, label_list: Path | None = None) -> State:
    schematic = schematic or SCHEMATIC
    label_list = label_list or LABEL_LIST
    source = schematic.read_bytes().decode("utf-8-sig")
    document = parse_sexpr(source)
    requested, duplicates = read_label_list(label_list)
    library = document.child("lib_symbols")
    if library is None:
        raise ValueError("原理图缺少内嵌符号定义")
    definition = next((item for item in library.children_named("symbol")
                       if item.arg() == "DSMU:XC7A75T-1CSG324C"), None)
    if definition is None:
        raise ValueError("找不到 U46 的 XC7A75T 符号定义")
    units = {int(match.group(1)): item for item in definition.children_named("symbol")
             if (match := re.search(r"_(\d+)_\d+$", item.arg()))}
    pins: dict[str, Pin] = {}
    for placed in document.children_named("symbol"):
        if text_arg(placed, "lib_id") != definition.arg():
            continue
        if not any(prop.arg(1) == "Reference" and prop.arg(2) == "U46"
                   for prop in placed.children_named("property")):
            continue
        unit = int(text_arg(placed, "unit"))
        sx, sy, rotation = at(placed)
        if rotation != 0 or placed.child("mirror") is not None:
            raise ValueError("U46 符号发生旋转/镜像，暂不支持自动定位引脚")
        if unit not in units:
            raise ValueError(f"U46 缺少符号单元 {unit}")
        for pin_node in units[unit].children_named("pin"):
            name = text_arg(pin_node, "name")
            ball = text_arg(pin_node, "number")
            if not (name.startswith("IO_") or name in CONFIG_LABEL_PIN_NAMES.values()) or not ball:
                continue
            bank = name.rsplit("_", 1)[-1]
            if bank not in IO_BANKS_3V3 | {"34", "0"}:
                continue
            px, py, angle = at(pin_node)
            if ball in pins:
                raise ValueError(f"U46 球位重复：{ball}")
            pins[ball] = Pin(ball, name, bank, unit, round(sx + px, 4),
                             round(sy - py, 4), angle)
    by_location: dict[tuple[int, int], Pin] = {}
    for pin in pins.values():
        if pin.location in by_location:
            raise ValueError(f"U46 引脚坐标重叠：{pin.ball}")
        by_location[pin.location] = pin
    all_labels = [label for kind in ("label", "global_label", "hierarchical_label")
                  for label in document.children_named(kind)]
    for label in document.children_named("label"):
        x, y, _ = at(label)
        if pin := by_location.get(point(x, y)):
            pin.labels.append(label)
    for marker in document.children_named("no_connect"):
        x, y, _ = at(marker)
        if pin := by_location.get(point(x, y)):
            pin.no_connects.append(marker)
    parents: dict[tuple[int, int], tuple[int, int]] = {}

    def root_of(location: tuple[int, int]) -> tuple[int, int]:
        parents.setdefault(location, location)
        if parents[location] != location:
            parents[location] = root_of(parents[location])
        return parents[location]

    def join(first: tuple[int, int], second: tuple[int, int]) -> None:
        parents[root_of(first)] = root_of(second)

    segments: list[tuple[tuple[int, int], tuple[int, int]]] = []
    wire_records: list[tuple[Node, list[tuple[int, int]]]] = []
    for wire in document.children_named("wire"):
        pts = wire.child("pts")
        if pts is None:
            continue
        endpoints = [point(float(xy.arg(1)), float(xy.arg(2)))
                     for xy in pts.children_named("xy")]
        wire_records.append((wire, endpoints))
        for first, second in zip(endpoints, endpoints[1:]):
            join(first, second)
            segments.append((first, second))

    def on_segment(location: tuple[int, int], first: tuple[int, int],
                   second: tuple[int, int]) -> bool:
        x, y = location
        x1, y1 = first
        x2, y2 = second
        return ((x - x1) * (y2 - y1) == (y - y1) * (x2 - x1)
                and min(x1, x2) <= x <= max(x1, x2)
                and min(y1, y2) <= y <= max(y1, y2))

    label_locations = [(label, point(*at(label)[:2])) for label in all_labels]
    for location in set(by_location) | {location for _, location in label_locations}:
        for first, second in segments:
            if on_segment(location, first, second):
                join(location, first)
                if pin := by_location.get(location):
                    pin.wired = True
    wire_names: dict[tuple[int, int], set[str]] = {}
    for label, location in label_locations:
        if location in parents:
            wire_names.setdefault(root_of(location), set()).add(label.arg())
    for pin in pins.values():
        if pin.wired:
            pin.wire_names = sorted(wire_names.get(root_of(pin.location), set()))
    junction_locations = {point(*at(node)[:2]) for node in document.children_named("junction")}
    for pin in pins.values():
        if not pin.wired or pin.labels or pin.no_connects or len(pin.wire_names) != 1:
            continue
        touching = [(wire, endpoints) for wire, endpoints in wire_records
                    if any(on_segment(pin.location, first, second)
                           for first, second in zip(endpoints, endpoints[1:]))]
        if len(touching) != 1:
            continue
        wire, endpoints = touching[0]
        if len(endpoints) != 2 or pin.location not in endpoints:
            continue
        remote = endpoints[1] if endpoints[0] == pin.location else endpoints[0]
        if remote == pin.location or (abs(remote[0] - pin.location[0]) +
                                      abs(remote[1] - pin.location[1])) > 100000:
            continue  # Limit automatic edits to short, isolated label stubs (10 mm).
        remote_labels = [label for label, location in label_locations if location == remote]
        if (len(remote_labels) != 1 or remote_labels[0].head != "label" or
                remote_labels[0].arg() != pin.wire_names[0]):
            continue
        if any(location not in {pin.location, remote} and
               on_segment(location, pin.location, remote)
               for _, location in label_locations):
            continue
        if any(on_segment(location, pin.location, remote) for location in junction_locations):
            continue
        if any(other.location != pin.location and
               on_segment(other.location, pin.location, remote)
               for other in pins.values()):
            continue
        if any(other_wire is not wire and
               any(on_segment(location, pin.location, remote) for location in other_endpoints)
               for other_wire, other_endpoints in wire_records):
            continue
        pin.stub_wire, pin.stub_label = wire, remote_labels[0]
    assignments: dict[str, str] = {}
    for pin in pins.values():
        for name in {label.arg() for label in pin.labels} | set(pin.wire_names):
            if name in requested:
                if name in assignments:
                    raise ValueError(f"标签 {name} 已连接 U46 多个球位")
                assignments[name] = pin.ball
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return State(source, digest, requested, pins, assignments, duplicates,
                 "\r\n" if "\r\n" in source else "\n")


def state_payload(state: State) -> dict:
    used_by = {ball: name for name, ball in state.assignments.items()}

    def issue_for(name: str) -> str:
        required = CONFIG_LABEL_PIN_NAMES.get(name)
        if not required:
            return ""
        pin = next((item for item in state.pins.values() if item.name == required), None)
        if pin is None:
            return f"找不到配置引脚 {required}"
        occupants = sorted(({label.arg() for label in pin.labels} | set(pin.wire_names)) - {name})
        if occupants:
            return f"配置引脚 {pin.ball} 已连接 {', '.join(occupants)}"
        if pin.wired:
            return f"配置引脚 {pin.ball} 已接走线"
        return ""

    return {
        "revision": state.digest,
        "labels": [{"name": name, "adc": name in ADC_LABELS,
                    "config_pin": CONFIG_LABEL_PIN_NAMES.get(name, ""),
                    "issue": issue_for(name),
                    "ball": state.assignments.get(name, ""),
                    "fixed": bool(state.assignments.get(name) and
                                  state.pins[state.assignments[name]].wired and
                                  not state.pins[state.assignments[name]].movable_stub),
                    "short_stub": bool(state.assignments.get(name) and
                                       state.pins[state.assignments[name]].movable_stub)}
                   for name in state.labels],
        "duplicates": state.duplicate_labels,
        "pins": [{"ball": pin.ball, "name": pin.name, "bank": pin.bank,
                  "unit": pin.unit, "x": pin.x, "y": pin.y,
                  "voltage": "1.8 V" if pin.bank == "34" else "3.3 V",
                  "used_by": used_by.get(pin.ball, ""),
                  "locked": bool((pin.wired and not pin.movable_stub) or
                                 any(label.arg() not in state.labels
                                                   for label in pin.labels)),
                  "occupants": sorted({label.arg() for label in pin.labels} | set(pin.wire_names)),
                  "wired": pin.wired}
                 for pin in sorted(state.pins.values(), key=lambda pin: (int(pin.bank), pin.ball))],
    }


def validate_assignments(state: State, assignments: dict[str, str]) -> dict[str, str]:
    if set(assignments) != set(state.labels):
        raise ValueError("标签清单已变化，请刷新页面")
    normalized: dict[str, str] = {}
    for name, ball in assignments.items():
        if not isinstance(ball, str):
            raise ValueError(f"{name} 的球位格式错误")
        normalized[name] = "" if ball == "0" else ball
    assignments = normalized
    selected: dict[str, str] = {}
    for name, ball in assignments.items():
        if not ball:
            continue
        pin = state.pins.get(ball)
        if pin is None:
            raise ValueError(f"{name} 选择了不存在的球位 {ball}")
        if (name in ADC_LABELS) != (pin.bank == "34"):
            raise ValueError(f"{name} 与 {ball} 的 1.8 V / 3.3 V Bank 不匹配")
        required = CONFIG_LABEL_PIN_NAMES.get(name)
        if required and pin.name != required:
            raise ValueError(f"{name} 必须使用 FPGA 配置引脚 {required}")
        if not required and pin.name in CONFIG_LABEL_PIN_NAMES.values():
            raise ValueError(f"球位 {ball} 保留给 FPGA 配置接口")
        if ball in selected:
            raise ValueError(f"球位 {ball} 同时分配给 {selected[ball]} 与 {name}")
        selected[ball] = name
        if pin.wired and state.assignments.get(name) != ball:
            occupant = pin.stub_label.arg() if pin.movable_stub else ""
            if not occupant or assignments.get(occupant) == ball:
                raise ValueError(f"球位 {ball} 已连接走线，不能覆盖")
        if any(label.arg() not in state.labels for label in pin.labels) or any(
                name not in state.labels for name in pin.wire_names):
            raise ValueError(f"球位 {ball} 已被其他网络占用")
    for name, old_ball in state.assignments.items():
        if assignments[name] != old_ball and state.pins[old_ball].wired and not state.pins[old_ball].movable_stub:
            raise ValueError(f"{name} 原球位 {old_ball} 有走线，不能直接移走标签")
    return assignments


def node_line_span(source: str, node: Node) -> tuple[int, int]:
    start = source.rfind("\n", 0, node.start) + 1
    if source[start:node.start].strip():
        return node.start, node.end
    end = source.find("\n", node.end)
    if end < 0 or source[node.end:end].strip():
        return start, node.end
    return start, end + 1


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_label(pin: Pin, name: str, newline: str) -> str:
    # Match the direction used by existing labels on U46: text faces away from the body.
    angle = 180 if pin.angle == 0 else 0
    justify = "right" if angle == 180 else "left"
    return (f'\t(label {quote(name)}{newline}'
            f'\t\t(at {format_number(pin.x)} {format_number(pin.y)} {angle}){newline}'
            f'\t\t(effects (font (size 1.27 1.27)) (justify {justify})){newline}'
            f'\t\t(uuid "{uuid4()}"){newline}\t){newline}')


def render_no_connect(pin: Pin, newline: str) -> str:
    return (f'\t(no_connect{newline}'
            f'\t\t(at {format_number(pin.x)} {format_number(pin.y)}){newline}'
            f'\t\t(uuid "{uuid4()}"){newline}\t){newline}')


def updated_source(state: State, assignments: dict[str, str]) -> tuple[str, list[str]]:
    assignments = validate_assignments(state, assignments)
    changed = [name for name in state.labels if assignments[name] != state.assignments.get(name, "")]
    if not changed:
        return state.source, []
    old_balls = {state.assignments[name] for name in changed if name in state.assignments}
    new_balls = {assignments[name] for name in changed if assignments[name]}
    splices: list[tuple[int, int, str]] = []
    for ball in old_balls:
        pin = state.pins[ball]
        for label in pin.labels:
            if label.arg() in changed:
                start, end = node_line_span(state.source, label)
                splices.append((start, end, ""))
        if pin.movable_stub and pin.stub_label.arg() in changed:
            for node in (pin.stub_wire, pin.stub_label):
                start, end = node_line_span(state.source, node)
                splices.append((start, end, ""))
    for ball in new_balls:
        for marker in state.pins[ball].no_connects:
            start, end = node_line_span(state.source, marker)
            splices.append((start, end, ""))
    additions = []
    vacated = old_balls - new_balls
    for ball in sorted(vacated):
        pin = state.pins[ball]
        if not pin.no_connects and (not pin.wired or pin.movable_stub):
            additions.append(render_no_connect(pin, state.newline))
    for name in changed:
        if assignments[name]:
            additions.append(render_label(state.pins[assignments[name]], name, state.newline))
    insertion = state.source.rfind(state.newline + ")")
    if insertion < 0:
        raise ValueError("原理图结尾格式无法识别")
    splices.append((insertion + len(state.newline), insertion + len(state.newline), "".join(additions)))
    result = state.source
    for start, end, replacement in sorted(splices, key=lambda item: item[0], reverse=True):
        result = result[:start] + replacement + result[end:]
    parse_sexpr(result)
    return result, changed


def schematic_editor_open() -> bool:
    if os.name != "nt":
        return False
    import ctypes

    found = False
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    def inspect(window, _data):
        nonlocal found
        if not user32.IsWindowVisible(window):
            return True
        size = user32.GetWindowTextLengthW(window)
        if not size:
            return True
        buffer = ctypes.create_unicode_buffer(size + 1)
        user32.GetWindowTextW(window, buffer, size + 1)
        title = buffer.value.lower()
        if "原理图编辑器" in title or "schematic editor" in title or "eeschema" in title:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
            process = kernel32.OpenProcess(0x1000, False, pid.value)
            if process:
                executable = ctypes.create_unicode_buffer(1024)
                capacity = ctypes.c_ulong(len(executable))
                if kernel32.QueryFullProcessImageNameW(process, 0, executable, ctypes.byref(capacity)):
                    if Path(executable.value).name.lower() in {"kicad.exe", "eeschema.exe"}:
                        found = True
                kernel32.CloseHandle(process)
            if found:
                return False
        return True

    user32.EnumWindows(callback_type(inspect), None)
    return found


def make_git_backup() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    created = subprocess.run(["git", "stash", "create"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
    if created:
        subprocess.run(["git", "stash", "store", "-m", f"{stamp} before FPGA pin tool apply", created],
                       cwd=ROOT, capture_output=True, text=True, check=True)
    return stamp


def apply_assignments(assignments: dict[str, str], revision: str) -> dict:
    with WRITE_LOCK:
        if schematic_editor_open():
            raise ValueError("请先保存并关闭 KiCad 原理图编辑器，再点击保存；PCB 编辑器可保持打开")
        state = load_state()
        if revision != state.digest:
            raise ValueError("原理图已被其他程序改动，请刷新页面后重试")
        result, changed = updated_source(state, assignments)
        if not changed:
            return {"changed": [], "message": "分配没有变化"}
        stamp = make_git_backup()
        backup_dir = ROOT / "Temp" / "fpga_pin_assignment_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"Spartan6_interface_{stamp}.kicad_sch"
        shutil.copy2(SCHEMATIC, backup)
        temp = backup_dir / f"writing_{uuid4().hex}.tmp"
        temp.write_bytes(result.encode("utf-8"))
        line = ", ".join(f"{name}={assignments[name] or '未分配'}" for name in changed)
        entry = (f"\n### FPGA 球位手动分配（{datetime.now():%Y-%m-%d %H:%M:%S}）\n\n"
                 f"- 修改 `DOC/XC7A75T.kicad_sch`：{line}。\n"
                 f"- 原因：通过 FPGA 球位分配工具按 1.8 V ADC Bank 34 / 3.3 V 其他 Bank 的规则保存。\n"
                 f"- 验证：球位唯一、未覆盖其他网络或已有走线，原理图 S 表达式可解析；"
                 f"保存前备份位于 `Temp/fpga_pin_assignment_backups/`。\n")
        try:
            os.replace(temp, SCHEMATIC)
            with CHANGELOG.open("a", encoding="utf-8") as handle:
                handle.write(entry)
        except OSError:
            shutil.copy2(backup, SCHEMATIC)
            raise
        return {"changed": changed, "message": f"已写入 {len(changed)} 项；备份：{backup.name}"}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/":
            body = HTML.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            try:
                self.send_json(state_payload(load_state()))
            except (OSError, ValueError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path != "/api/apply":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("只接受 JSON 请求")
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                        or parsed.port != self.server.server_port):
                    raise ValueError("不接受其他网站的写入请求")
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 100000:
                raise ValueError("请求大小无效")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data.get("assignments"), dict):
                raise ValueError("分配数据格式错误")
            result = apply_assignments(data["assignments"], data.get("revision", ""))
            self.send_json(result)
        except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)

    def log_message(self, _format: str, *_args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="U46 FPGA pin assignment workbench")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", default=8766, type=int)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"FPGA 球位分配工具：{url}", flush=True)
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
