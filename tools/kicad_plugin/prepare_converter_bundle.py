"""Prepare pinned converter sources in external project-specific scratch storage."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.request import urlopen

from package_plugin import ROOT, CONVERTER_COMMIT, CONVERTER_RELEASE_URL, CONVERTER_RELEASE_SHA256, converter_version
from part_selection_runtime import runtime_directory

SOURCES = {
    'Qt-GPL-3.0.txt': 'https://raw.githubusercontent.com/qt/qtbase/v6.10.2/LICENSES/GPL-3.0-only.txt',
    'Qt-LGPL-3.0.txt': 'https://raw.githubusercontent.com/qt/qtbase/v6.10.2/LICENSES/LGPL-3.0-only.txt',
    'Qt-GPL-2.0.txt': 'https://raw.githubusercontent.com/qt/qtbase/v6.10.2/LICENSES/GPL-2.0-only.txt',
    'QXlsx-LICENSE': 'https://raw.githubusercontent.com/QtExcel/QXlsx/9f545935a1effa0144ba76598e95a7597210553a/LICENSE',
    'zlib-LICENSE': 'https://raw.githubusercontent.com/madler/zlib/v1.3.1/LICENSE',
    'source/QXlsx-9f545935.zip': 'https://codeload.github.com/QtExcel/QXlsx/zip/9f545935a1effa0144ba76598e95a7597210553a',
    'source/zlib-1.3.1.zip': 'https://codeload.github.com/madler/zlib/zip/refs/tags/v1.3.1',
}


def run(args):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    subprocess.run(args, check=True, timeout=240, **options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    runtime = runtime_directory(ROOT)
    output = (args.output or runtime / 'easyki-bundle').resolve()
    if not output.is_relative_to(runtime):
        raise ValueError('Bundle preparation output must stay in external runtime storage')
    if converter_version(args.runtime / 'easykiconverter.exe') != '3.1.11':
        raise ValueError('Expected converter 3.1.11')
    source = output / 'source'
    output.mkdir(parents=True, exist_ok=True)
    release = output / 'upstream-release.zip'
    if not release.exists():
        with urlopen(CONVERTER_RELEASE_URL, timeout=120) as response:
            release.write_bytes(response.read())
    if hashlib.sha256(release.read_bytes()).hexdigest() != CONVERTER_RELEASE_SHA256:
        raise ValueError('Pinned upstream release SHA-256 mismatch')
    if not source.exists():
        run(['git', 'clone', '--depth', '1', '--branch', 'v3.1.11', 'https://github.com/EasyKiconverter/EasyKiConverter.git', str(source)])
    notices = output / 'notices'
    notices.mkdir(exist_ok=True)
    provenance = {}
    for name, url in SOURCES.items():
        target = notices / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with urlopen(url, timeout=90) as response:
                data = response.read()
            target.write_bytes(data)
        provenance[name] = {'url': url, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
        print(name, flush=True)
    (notices / 'sources.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')
    (notices / 'THIRD_PARTY.md').write_text(
        '# Independent converter dependency materials\n\n'
        'EasyKiConverter 3.1.11 / ' + CONVERTER_COMMIT + ': GPL-3.0; full matching source is included next to this directory.\n'
        'Windows portable runtime: upstream 3.1.11 build, Qt DLLs 6.10.2 and zlib 1.3.1. Dynamic DLLs remain replaceable.\n'
        'Qt 6.10.2: https://download.qt.io/archive/qt/6.10/6.10.2/single/qt-everywhere-src-6.10.2.tar.xz\n'
        'Qt module and bundled dependency notices: https://code.qt.io/cgit/qt/qtbase.git/tree/LICENSES?h=v6.10.2 and module qt_attribution.json files in matching Qt sources.\n'
        'QXlsx commit 9f545935 (MIT) and zlib 1.3.1 source archives and licenses are included here.\n'
        'No converter GUI/QML assets, DirectX redistributables, MSVC redistributable installer or developer static libraries are shipped.\n'
        'Windows may still require Microsoft Visual C++ 2015-2022 runtime; install it from Microsoft if missing.\n'
        'The converter runs as an independent process, not a linked plugin library. Plugin licensing is unchanged.\n'
        'Retain all source archives, notices and manifests when redistributing. Review licensing for your exact binary before public distribution.\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
