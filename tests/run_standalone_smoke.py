"""Exercise an extracted plugin package with a disposable project and HTTP server."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import zipfile

from _scratch import temporary_project

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/kicad_plugin'))
from package_plugin import build_package


def main():
    with temporary_project(prefix='standalone-smoke-') as directory:
        work = Path(directory)
        package = build_package(work / 'plugin.zip')
        with zipfile.ZipFile(package) as archive:
            assert archive.testzip() is None
            assert not any(name.startswith(('BOM/', 'DOC/', 'Libraries/')) for name in archive.namelist())
            archive.extractall(work / 'installed')
        project = work / 'board with spaces'
        project.mkdir()
        (project / 'board.kicad_pro').write_text('{}')
        (project / 'board.kicad_sch').write_text(
            '(kicad_sch (symbol (property "Reference" "R1") '
            '(property "Comment" "resistor") (property "Value" "10k") '
            '(property "Footprint" "MyLibrary:0603")))')
        config = work / 'settings.json'
        config.write_text(json.dumps({'project_root': str(project), 'schematic_dir': '.',
            'bom_dir': str(work / 'records'), 'output_overrides': str(work / 'records/selected.json'),
            'stock_cache': str(work / 'records/stock.json')}))
        with socket.socket() as socket_:
            socket_.bind(('127.0.0.1', 0))
            port = socket_.getsockname()[1]
        entry = work / 'installed/plugins/app/part_selection_server.py'
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        with (work / 'server.log').open('wb') as output:
            process = subprocess.Popen([sys.executable, '-B', str(entry), '--config', str(config),
                '--port', str(port)], cwd=work, stdout=output, stderr=output, **flags)
            try:
                base = f'http://127.0.0.1:{port}'
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urlopen(base + '/api/config', timeout=1) as response:
                            info = json.load(response)
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise AssertionError((work / 'server.log').read_text(errors='replace'))
                        time.sleep(0.1)
                assert Path(info['project_root']) == project
                assert Path(info['backend_directory']) == entry.parent
                with urlopen(base + '/', timeout=5) as response:
                    html = response.read().decode('utf-8')
                assert 'id="openPathConfig"' in html
                with urlopen(base + '/api/rows?bom=__schematic__&stock=0', timeout=5) as response:
                    rows = json.load(response)['rows']
                assert len(rows) == 1 and rows[0]['refs'] == 'R1'
                request = Request(base + '/api/override', data=json.dumps({'refs': 'R1',
                    'decision': 'AcceptManual', 'manual_c_code': 'C123'}).encode(),
                    headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    assert json.load(response)['ok']
                assert json.loads((work / 'records/selected.json').read_text())['R1']['manual_c_code'] == 'C123'
                print(json.dumps({'zip': 'valid', 'isolated_package': True, 'project': 'explicit',
                    'http_config': 'passed', 'html': 'passed', 'rows': len(rows),
                    'external_record_write': 'passed', 'real_cad_written': False}))
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()
