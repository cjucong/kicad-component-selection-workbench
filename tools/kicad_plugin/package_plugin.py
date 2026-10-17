"""Build a PCM ZIP or install its payload into a Windows user plugin directory."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import tempfile
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
ROOT = TOOLS.parent
IDENTIFIER = "com.smu.part-selection"
VERSION = "0.1.1"
CORE_FILES = (
    "part_selection_server.py", "part_selection_helper.py", "part_selection_overrides.py",
    "part_stock_lookup.py", "fpga_pin_assignment.py", "kicad_schematic_search.ps1",
    "kicad_ipc_action.py", "kicad_schematic_search_launcher.vbs",
    "install_kicad_schematic_search_protocol.ps1",
)


def icon_png(size=24) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            body = 6 <= x < 18 and 5 <= y < 19
            pins = ((3 <= x < 21 and x % 3 == 0 and (2 <= y < 5 or 19 <= y < 22))
                    or (3 <= y < 21 and y % 3 == 0 and (3 <= x < 6 or 18 <= x < 21)))
            row.extend((26, 124, 99, 255) if body else (90, 98, 105, 255) if pins else (0, 0, 0, 0))
        rows.append(bytes(row))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def package_files() -> dict[str, bytes]:
    files = {"plugins/" + name: (HERE / name).read_bytes()
             for name in ("desktop.py", "plugin.json", "requirements.txt", "desktop-requirements.txt")}
    files.update({"plugins/app/" + name: (TOOLS / name).read_bytes() for name in CORE_FILES})
    files["plugins/icon.png"] = icon_png()
    files["resources/icon.png"] = icon_png(24)
    files["plugins/INSTALL.md"] = (TOOLS / "docs" / "kicad-plugin.md").read_bytes()
    metadata = {
        "$schema": "https://go.kicad.org/pcm/schemas/v2", "identifier": IDENTIFIER,
        "name": "器件选料助手", "description": "Project-aware BOM selection desktop workbench for Windows",
        "description_full": "KiCad PCB and schematic toolbar launcher. Uses WebView2; keeps selection data in each project. "
                            "Schematic synchronization retains the existing file-write workflow and its limitations.",
        "type": "plugin", "author": {"name": "SMU engineering tools", "contact": {}},
        "license": "proprietary",
        "resources": {},
        "versions": [{"version": VERSION, "status": "testing", "kicad_version": "10.0",
                      "runtime": "ipc", "platforms": ["windows"],
                      "install_size": sum(map(len, files.values()))}],
    }
    files["metadata.json"] = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return files


def build_package(output: Path) -> Path:
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(package_files().items()):
            info = zipfile.ZipInfo(name, (2026, 10, 17, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output


def install_plugin(plugin_directory: Path, backup_directory: Path) -> Path:
    directory = Path(plugin_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / IDENTIFIER
    if target.is_symlink() or target.resolve().parent != directory:
        raise ValueError("Refusing to replace a linked or out-of-directory plugin")
    files = {name.removeprefix("plugins/"): data for name, data in package_files().items()
             if name.startswith("plugins/")}
    with tempfile.TemporaryDirectory(dir=directory, prefix=".selection-install-") as td:
        staged = Path(td)
        for name, data in files.items():
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        backup = None
        if target.exists():
            backups = Path(backup_directory).resolve()
            backups.mkdir(parents=True, exist_ok=True)
            backup = backups / (IDENTIFIER + "_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
            shutil.copytree(target, backup)
            # The resolved target was checked against the explicitly selected plugin directory above.
            shutil.rmtree(target)
        try:
            shutil.copytree(staged, target)
        except Exception:
            if target.exists():
                if target.is_symlink() or target.resolve().parent != directory:
                    raise RuntimeError("Installation target changed; backup retained for manual recovery")
                shutil.rmtree(target)
            if backup is not None:
                shutil.copytree(backup, target)
            raise
    return target


def user_plugin_directory(version: str) -> Path:
    if os.name != "nt":
        raise RuntimeError("This package currently supports Windows only")
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer):
        raise RuntimeError("Cannot resolve the Windows Documents folder")
    return Path(buffer.value) / "KiCad" / version / "plugins"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "Temp" / f"part-selection-{VERSION}.zip")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--kicad-version", choices=["10.0"], default="10.0")
    args = parser.parse_args()
    print(build_package(args.output))
    if args.install:
        print(install_plugin(user_plugin_directory(args.kicad_version), ROOT / "Temp/plugin_install_backups"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
