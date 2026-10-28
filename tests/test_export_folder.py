"""Export HTTP actions open completed output folders, never real desktop windows."""
import csv
import json
import threading
import re
import shutil
import subprocess
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from part_selection_server import PartSelectionApp, make_handler, render_index


@pytest.fixture
def export_app(scratch_project):
    (scratch_project / "test.kicad_sch").write_text('(kicad_sch)', encoding="utf-8")
    config = scratch_project / "config.json"
    config.write_text(json.dumps({"schematic_dir": ".", "source_bom": "__schematic__",
                                  "bom_dir": "自定义输出 目录"}), encoding="utf-8")
    app = PartSelectionApp(config)
    yield app
    app.search_jobs.close()
    app.selection_jobs.close()


def post_export(app, endpoint, payload=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(f"http://127.0.0.1:{server.server_port}{endpoint}",
                          data=json.dumps(payload or {"export_filter": "all_non_nc"}).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("endpoint,header", [
    ("/api/export_pump_table", "序号"),
    ("/api/export_changes", "Designator"),
])
def test_each_export_opens_actual_parent_only_after_file_is_closed(export_app, endpoint, header):
    def open_folder(path):
        assert path == export_app.bom_dir
        outputs = list(path.glob("*.csv"))
        assert len(outputs) == 1
        # Windows rename requires the writer to be closed before the shell opens.
        moved = outputs[0].with_suffix(".check")
        outputs[0].rename(moved)
        moved.rename(outputs[0])
        with outputs[0].open(encoding="utf-8-sig", newline="") as file:
            assert next(csv.reader(file))[0] == header
        return {"ok": True, "path": str(path)}

    with patch.object(export_app, "_open_directory", side_effect=open_folder) as opened:
        status, result = post_export(export_app, endpoint)
    assert status == 200 and result["ok"] and result["folder_opened"]
    assert Path(result["path"]).parent == export_app.bom_dir
    opened.assert_called_once_with(export_app.bom_dir)


@pytest.mark.parametrize("endpoint", ["/api/export_pump_table", "/api/export_changes"])
def test_shell_failure_does_not_undo_successful_export(export_app, endpoint):
    with patch.object(export_app, "_open_directory", return_value={
        "ok": False, "error": "shell unavailable", "path": str(export_app.bom_dir),
    }):
        status, result = post_export(export_app, endpoint)
    assert status == 200 and result["ok"] and not result["folder_opened"]
    assert result["folder_error"] == "shell unavailable"
    assert Path(result["path"]).is_file()


@pytest.mark.parametrize("endpoint,method", [
    ("/api/export_pump_table", "export_pump_table"),
    ("/api/export_changes", "export_changes"),
])
def test_failed_export_never_opens_folder(export_app, endpoint, method):
    with patch.object(export_app, method, side_effect=OSError("disk full")), \
            patch.object(export_app, "_open_directory") as opened:
        status, result = post_export(export_app, endpoint)
    assert status >= 400 and not result["ok"]
    opened.assert_not_called()


def test_unexpected_folder_access_error_preserves_success(export_app):
    with patch.object(export_app, "_open_directory", side_effect=PermissionError("access denied")):
        status, result = post_export(export_app, "/api/export_pump_table")
    assert status == 200 and result["ok"] and not result["folder_opened"]
    assert result["folder_error"] == "access denied"
    assert Path(result["path"]).is_file()


def test_unsuccessful_or_missing_output_never_opens_directory(export_app):
    with patch.object(export_app, "_open_directory") as opened:
        failed = {"ok": False, "error": "export failed"}
        assert export_app.open_export_folder(failed) == failed
        result = export_app.open_export_folder({"ok": True, "path": str(export_app.bom_dir / "missing.csv")})
    assert not result["folder_opened"] and result["folder_error"]
    opened.assert_not_called()


def test_windows_opens_directory_not_exported_file_or_shell_command(export_app):
    export_app.bom_dir.mkdir(parents=True, exist_ok=True)
    opened = Mock()
    with patch("part_selection_server.os", SimpleNamespace(name="nt", startfile=opened)):
        assert export_app._open_directory(export_app.bom_dir)["ok"]
    opened.assert_called_once_with(export_app.bom_dir)


@pytest.mark.parametrize("platform,command", [("darwin", "open"), ("linux", "xdg-open")])
def test_other_platforms_pass_directory_as_one_argument(export_app, platform, command):
    export_app.bom_dir.mkdir(parents=True, exist_ok=True)
    with patch("part_selection_server.os", SimpleNamespace(name="posix")), \
            patch("part_selection_server.sys.platform", platform), \
            patch("part_selection_server.subprocess.Popen") as opened:
        assert export_app._open_directory(export_app.bom_dir)["ok"]
    opened.assert_called_once_with([command, str(export_app.bom_dir)])


def test_shell_error_and_missing_directory_are_reported_without_creation(export_app):
    missing = export_app.bom_dir / "missing"
    with patch("part_selection_server.os", SimpleNamespace(name="nt", startfile=Mock())) as shell:
        assert not export_app._open_directory(missing)["ok"]
        shell.startfile.assert_not_called()
        assert not missing.exists()
        export_app.bom_dir.mkdir(parents=True, exist_ok=True)
        shell.startfile.side_effect = OSError("shell unavailable")
        result = export_app._open_directory(export_app.bom_dir)
    assert not result["ok"] and result["error"] == "shell unavailable"


def test_both_frontend_exports_keep_success_path_when_folder_cannot_open(export_app):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    html = render_index(export_app)
    code = re.search(r"function showExportResult\(.*?(?=const pendingStockRequests)", html, re.S).group()
    script = """
const assert = require('node:assert/strict');
const states = [];
const refsInput = {value:''}, bomSelect = {value:''}, activeFilters = new Map();
const document = {getElementById:()=>({value:'all_non_nc'})};
const setState = (text, style)=>states.push({text,style});
let result = {ok:true,count:3,path:'custom/BOM.csv',folder_opened:true};
global.fetch = async ()=>({json:async ()=>result});
""" + code + """
(async ()=>{
  for (const action of [exportChanges, exportPumpTable]) {
    await action();
    assert.equal(states.at(-1).style,'saved');
    assert.ok(states.at(-1).text.includes('custom/BOM.csv'));
    result = {...result,folder_opened:false,folder_error:'shell unavailable'};
    await action();
    assert.ok(states.at(-1).text.includes('已导出'));
    assert.ok(states.at(-1).text.includes('custom/BOM.csv'));
    assert.ok(states.at(-1).text.includes('shell unavailable'));
    result = {...result,ok:false,error:'disk full'};
    const count = states.length;
    await assert.rejects(action(), /disk full/);
    assert.equal(states.length,count);
    result = {...result,ok:true,folder_opened:true};
  }
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
