"""Build a PCM ZIP or install its payload into a Windows user plugin directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from part_selection_runtime import runtime_directory
IDENTIFIER = "com.smu.part-selection"
if __package__:
    from .updater import VERSION, REPOSITORY
else:
    from updater import VERSION, REPOSITORY
CORE_FILES = (
    "part_selection_stock_refresh.py", "web/stock-refresh.js",
    "part_selection_footprint_unify.py", "unify_footprints.py",
    "web/edit-menu.js", "web/footprint-adjust.js",
    "part_selection_design_sync.py", "part_selection_design_status.py",
    "web/design-sync.js", "web/design-sync.css",
    "web/tool-pages.js", "web/tool-pages.css",
    "part_selection_design_source.py",
    "part_selection_pcb_connection.py",
    "part_selection_tool_windows.py", "web/tool-desktop.js",
    "part_selection_footprint_changes.py",
    "part_selection_pad_mapping.py",
    "web/row-filters.js", "web/row-filters.css",
    "part_selection_specs.py", "web/spec-warnings.js", "web/spec-warnings.css",
    "web/value-parameters.js", "web/value-parameters.css",
    "part_selection_inventory.py", "web/local-inventory.js", "web/local-inventory.css",
    "part_selection_candidate_window.py", "web/candidate-desktop.js",
    "part_selection_pcb.py", "part_selection_pcb_geometry.py", "part_selection_pcb_session.py",
    "part_selection_pcb_library.py", "web/pcb-updates.js", "web/pcb-updates.css",
    "part_selection_server.py", "part_selection_helper.py", "part_selection_overrides.py", "part_selection_fields.py",
    "part_selection_library.py", "part_selection_footprints.py", "part_selection_footprint_geometry.py", "footprint_geometry_worker.py", "part_selection_runtime.py", "part_selection_paths.py", "part_selection_settings.py", "part_selection_lifecycle.py",
    "part_stock_lookup.py", "part_selection_search.py", "part_selection_product_details.py", "kicad_sexpr.py", "kicad_schematic_search.ps1",
    "kicad_ipc_action.py", "kicad_schematic_search_launcher.vbs",
    "install_kicad_schematic_search_protocol.ps1",
    "web/toolbar-icons.svg", "web/LUCIDE-LICENSE", "web/footprint-compare.js", "web/footprint-compare.css", "web/row-edits.js", "web/path-settings.js", "web/path-settings.css", "web/desktop-lifecycle.js", "web/plugin-updates.js",
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


CONVERTER_VERSION = "3.1.11"
CONVERTER_COMMIT = "1acc47c028ed1f75066ba134f6a2f327c0751c0e"
CONVERTER_RELEASE_URL = "https://github.com/EasyKiconverter/EasyKiConverter/releases/download/v3.1.11/EasyKiConverter-3.1.11-g1acc47c-win64.zip"
CONVERTER_RELEASE_SHA256 = "5b33cf78cf7ae1742a93d201ae32bee5345d2a9556f18bab440ae83630cfdc0e"


def verify_converter_runtime(release, files):
    release = Path(release)
    if not release.is_file() or hashlib.sha256(release.read_bytes()).hexdigest() != CONVERTER_RELEASE_SHA256:
        raise ValueError("Pinned upstream release archive missing or SHA-256 mismatch; rerun prepare_converter_bundle.py")
    with zipfile.ZipFile(release) as archive:
        for name, data in files.items():
            if not name.startswith("runtime/"):
                continue
            relative = name.removeprefix("runtime/")
            if relative not in archive.namelist() or archive.read(relative) != data:
                raise ValueError("Runtime does not match pinned upstream release: " + relative)


def run_hidden(args):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return subprocess.run(args, capture_output=True, check=True, timeout=60, **options).stdout


def converter_source_commit(source):
    return run_hidden(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()


def converter_version(executable):
    output = run_hidden([str(executable), "--version"]).decode("ascii", errors="replace")
    import re
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", output)
    return match.group(1) if match else ""


def source_archive(source):
    if run_hidden(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"]).strip():
        raise ValueError("Converter source has local changes; use the pinned clean checkout")
    return run_hidden(["git", "-C", str(source), "archive", "--format=zip", "HEAD"])


def converter_bundle(runtime, source, notices):
    if not runtime or not source or not notices:
        raise ValueError("Full bundle requires --converter-runtime, --converter-source and --converter-notices")
    runtime, source, notices = (Path(path).resolve() for path in (runtime, source, notices))
    executable = runtime / "easykiconverter.exe"
    if converter_source_commit(source) != CONVERTER_COMMIT or converter_version(executable) != CONVERTER_VERSION:
        raise ValueError("Converter binary/source must match pinned v3.1.11 (1acc47c)")
    for name in ("LICENSE", "CMakeLists.txt"):
        if not (source / name).is_file():
            raise ValueError("Converter source is incomplete")
    for name in ("Qt-GPL-3.0.txt", "Qt-LGPL-3.0.txt", "QXlsx-LICENSE", "zlib-LICENSE", "THIRD_PARTY.md"):
        if not (notices / name).is_file() or not (notices / name).stat().st_size:
            raise ValueError("Missing dependency notice: " + name)
    files = {}
    # Headless conversion does not need QML assets, GUI plugins, caches or development libraries.
    paths = [executable, *runtime.glob("Qt6*.dll"), *runtime.glob("zlib1.dll")]
    for folder in ("tls", "networkinformation"):
        paths.extend((runtime / folder).glob("*.dll"))
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(runtime):
            raise ValueError("Unsafe converter runtime path")
        files["runtime/" + path.relative_to(runtime).as_posix()] = path.read_bytes()
    for name in ("Qt6Core.dll", "Qt6Network.dll"):
        if "runtime/" + name not in files:
            raise ValueError("Converter runtime missing " + name)
    verify_converter_runtime(source.parent / "upstream-release.zip", files)
    for path in notices.rglob("*"):
        if path.is_file():
            if path.is_symlink() or not path.resolve().is_relative_to(notices):
                raise ValueError("Unsafe dependency notice path")
            files["licenses/" + path.relative_to(notices).as_posix()] = path.read_bytes()
    files["LICENSE"] = (source / "LICENSE").read_bytes()
    files["source/EasyKiConverter-v3.1.11.zip"] = source_archive(source)
    files["bundle-manifest.json"] = json.dumps({
        "version": CONVERTER_VERSION, "commit": CONVERTER_COMMIT,
        "upstream_release": {"url": CONVERTER_RELEASE_URL, "sha256": CONVERTER_RELEASE_SHA256},
        "execution": "independent hidden CLI subprocess; imported symbols are not placed automatically",
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }, indent=2).encode()
    return {"plugins/third_party/EasyKiConverter/" + name: data for name, data in files.items()}


def package_files(*, converter_runtime=None, converter_source=None, converter_notices=None) -> dict[str, bytes]:
    files = {"plugins/" + name: (HERE / name).read_bytes()
             for name in ("__init__.py", "desktop.py", "startup_progress.py", "startup_progress.manifest", "updater.py", "plugin.json", "requirements.txt", "desktop-requirements.txt")}
    files["plugins/build-info.json"] = (json.dumps({"version": VERSION}) + "\n").encode()
    files.update({"plugins/app/" + name: (TOOLS / name).read_bytes() for name in CORE_FILES})
    files["plugins/icon.png"] = icon_png()
    files["resources/icon.png"] = icon_png(24)
    files["plugins/INSTALL.md"] = (TOOLS / "docs" / "kicad-plugin.md").read_bytes()
    files["plugins/EASYKICONVERTER.md"] = (TOOLS / "docs" / "easykiconverter.md").read_bytes()
    if any(path is not None for path in (converter_runtime, converter_source, converter_notices)):
        files.update(converter_bundle(converter_runtime, converter_source, converter_notices))
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


def build_package(output: Path, **bundle_options) -> Path:
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(package_files(**bundle_options).items()):
            info = zipfile.ZipInfo(name, (2026, 10, 17, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output


def build_update_manifest(archive_path: Path) -> Path:
    archive_path = Path(archive_path).resolve()
    filename = f'kicad-component-selection-workbench-v{VERSION}.zip'
    if archive_path.name != filename:
        raise ValueError('Release ZIP name must match the current version')
    with zipfile.ZipFile(archive_path) as archive:
        if json.loads(archive.read('plugins/build-info.json'))['version'] != VERSION:
            raise ValueError('Release ZIP version differs from the manifest')
        if any(name.startswith('plugins/third_party/') for name in archive.namelist()):
            raise ValueError('Online update ZIP must not contain the converter')
    data = archive_path.read_bytes()
    manifest = {'identifier':IDENTIFIER, 'tag_name':'v'+VERSION, 'draft':False, 'prerelease':False,
        'body':'KiCad Component Selection Workbench '+VERSION,
        'assets':[{'name':filename, 'size':len(data), 'digest':'sha256:'+hashlib.sha256(data).hexdigest(),
            'browser_download_url':f'https://github.com/{REPOSITORY}/releases/download/v{VERSION}/{filename}'}]}
    output = archive_path.parent / 'workbench-update.json'
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return output


def install_plugin(plugin_directory: Path, backup_directory: Path, **bundle_options) -> Path:
    directory = Path(plugin_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Match PCM's flat Python package naming; dotted directory names cannot be imported.
    target = directory / IDENTIFIER.replace(".", "_")
    previous = [target, directory / IDENTIFIER]
    for path in previous:
        if path.is_symlink() or path.resolve().parent != directory:
            raise ValueError("Refusing to replace a linked or out-of-directory plugin")
    files = {name.removeprefix("plugins/"): data for name, data in package_files(**bundle_options).items()
             if name.startswith("plugins/")}
    with tempfile.TemporaryDirectory(dir=directory, prefix=".selection-install-") as td:
        staged = Path(td)
        # A code-only upgrade must not remove an already installed converter.
        if not bundle_options.get("converter_runtime"):
            bundled = next((path / "third_party" for path in previous if (path / "third_party").is_dir()), None)
            if bundled is not None:
                if any(path.is_symlink() for path in (bundled, *bundled.rglob("*"))):
                    raise ValueError("Refusing to copy linked converter files")
                shutil.copytree(bundled, staged / "third_party")
        for name, data in files.items():
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        saved = {}
        backups = Path(backup_directory).resolve()
        for path in previous:
            if path.exists():
                backups.mkdir(parents=True, exist_ok=True)
                backup = backups / (path.name + "_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
                shutil.copytree(path, backup)
                saved[path] = backup
        try:
            # All deletion targets were resolved and checked above; back up both before migration.
            for path in saved:
                shutil.rmtree(path)
            shutil.copytree(staged, target)
        except Exception:
            for path in previous:
                if path.exists():
                    if path.is_symlink() or path.resolve().parent != directory:
                        raise RuntimeError("Installation target changed; backup retained for manual recovery")
                    shutil.rmtree(path)
                if path in saved:
                    shutil.copytree(saved[path], path)
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
    parser.add_argument("--output", type=Path)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--update-manifest", action="store_true", help="Write the official release update manifest alongside --output")
    parser.add_argument("--plugin-dir", type=Path, help="Custom KiCad user plugin directory for --install")
    parser.add_argument("--kicad-version", choices=["10.0"], default="10.0")
    parser.add_argument("--converter-runtime", type=Path, help="Pinned Windows converter runtime directory (full bundle)")
    parser.add_argument("--converter-source", type=Path, help="Clean pinned converter Git checkout")
    parser.add_argument("--converter-notices", type=Path, help="Dependency licenses, source materials and notices")
    args = parser.parse_args()
    bundle = {"converter_runtime": args.converter_runtime, "converter_source": args.converter_source, "converter_notices": args.converter_notices}
    output = args.output or runtime_directory(ROOT) / f"part-selection-{VERSION}.zip"
    print(build_package(output, **bundle))
    if args.update_manifest:
        print(build_update_manifest(output))
    if args.install:
        print(install_plugin(args.plugin_dir or user_plugin_directory(args.kicad_version), runtime_directory(ROOT) / "plugin_install_backups", **bundle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
