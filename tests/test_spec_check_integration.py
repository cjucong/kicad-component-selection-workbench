import json
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen

from part_selection_server import PartSelectionApp, make_handler


def test_schematic_baseline_and_matching_code_drive_warning_without_writes(scratch_project):
    root = scratch_project
    schematic = root / 'test.kicad_sch'
    original = '(kicad_sch (symbol (property "Reference" "C1") (property "Comment" "capacitor") (property "Value" "100nF, 50V") (property "Footprint" "DSMU:0603") (property "LCSC" "C100")))'
    schematic.write_text(original, encoding='utf-8')
    config = root / 'config.json'
    config.write_text(json.dumps({'schematic_dir': '.', 'source_bom': '__schematic__', 'bom_dir': 'data'}), encoding='utf-8')
    app = PartSelectionApp(config)
    stock = {'c_code': 'C100', 'ok': True, 'stock': '500', 'param_detail': '容值: 10nF', 'package': '0402'}
    app.stock_cache_path.parent.mkdir(parents=True, exist_ok=True)
    app.stock_cache_path.write_text(json.dumps({'C100': stock}), encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch('part_selection_server.lookup_stock_with_cache', side_effect=AssertionError('No network')):
            row = app.rows(include_stock=False)[0]
            assert row['spec_check']['status'] == 'mismatch', row
            assert {issue['field'] for issue in row['spec_check']['issues']} == {'value', 'footprint'}
            before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
            req = Request(f'http://127.0.0.1:{server.server_port}/api/spec_check',
                          data=json.dumps({'rows': [{'refs': 'C1', 'c_code': 'C100'}]}).encode(),
                          headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=10) as response:
                result = json.load(response)
            assert result['checks'][0]['check']['status'] == 'mismatch'
            assert app.check_specs({'rows': [{'refs': 'C1', 'c_code': 'C999'}]})['checks'][0]['check']['status'] == 'unknown'
            assert {path: path.read_bytes() for path in root.rglob('*') if path.is_file()} == before
        assert schematic.read_text(encoding='utf-8') == original
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.search_jobs.close()
        app.selection_jobs.close()
