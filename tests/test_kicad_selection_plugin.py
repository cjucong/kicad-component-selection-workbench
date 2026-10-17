import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "tools" / "kicad_plugin"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(PLUGIN))


class SelectionPluginTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "Temp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "Temp", prefix="selection_plugin_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def project(self, name="project with spaces"):
        root = self.root / name
        root.mkdir()
        (root / "board.kicad_pro").write_text("{}", encoding="utf-8")
        (root / "board.kicad_sch").write_text('(kicad_sch (version 20250114))', encoding="utf-8")
        return root

    def test_explicit_project_and_ipc_project_are_validated_without_cwd_guess(self):
        import desktop
        root = self.project()
        self.assertEqual(desktop.validate_project(root / "board.kicad_pro"), root)
        board = SimpleNamespace(get_project=lambda: SimpleNamespace(path=str(root)))
        self.assertEqual(desktop.project_from_ipc(SimpleNamespace(get_board=lambda: board)), root)
        with self.assertRaises(ValueError):
            desktop.validate_project(self.root)
        board.get_project = lambda: SimpleNamespace(path="")
        with self.assertRaises(ValueError):
            desktop.project_from_ipc(SimpleNamespace(get_board=lambda: board))

    def test_existing_project_config_is_never_overwritten_or_redirected(self):
        import desktop
        root = self.project()
        config = desktop.ensure_config(root)
        first = config.read_bytes()
        self.assertEqual(json.loads(first)["source_bom"], "__schematic__")
        self.assertEqual(desktop.ensure_config(root).read_bytes(), first)
        data = json.loads(first)
        data["project_root"] = str(self.root)
        config.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            desktop.ensure_config(root)
        self.assertEqual(json.loads(config.read_text())["project_root"], str(self.root))

    def test_schematic_entry_resolves_project_without_open_pcb(self):
        import desktop
        root = self.project()
        document = SimpleNamespace(name="board.kicad_sch")
        client = SimpleNamespace(
            get_open_documents=lambda kind: [document],
            get_project=lambda doc: SimpleNamespace(path=str(root)),
            get_board=lambda: (_ for _ in ()).throw(RuntimeError("No PCB open")),
        )
        with patch.dict(sys.modules, {"kipy.proto.common.types.base_types_pb2": SimpleNamespace(
                DocumentType=SimpleNamespace(DOCTYPE_SCHEMATIC=1))}):
            self.assertEqual(desktop.project_from_ipc(client), root)

    def test_native_window_enables_find_shortcut_without_debug_mode(self):
        import desktop
        settings = SimpleNamespace(AreBrowserAcceleratorKeysEnabled=False, AreDevToolsEnabled=False)
        window = SimpleNamespace(native=SimpleNamespace(
            webview=SimpleNamespace(CoreWebView2=SimpleNamespace(Settings=settings)),
            Invoke=lambda action: action(),
        ))
        with patch.dict(sys.modules, {"System": SimpleNamespace(Action=lambda action: action)}):
            desktop.enable_browser_shortcuts(window)
        self.assertTrue(settings.AreBrowserAcceleratorKeysEnabled)
        self.assertFalse(settings.AreDevToolsEnabled)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend behavior checks")
    def test_bom_search_and_chip_keyword_behavior(self):
        import desktop
        from part_selection_server import PartSelectionApp, render_index
        root = self.project()
        page = render_index(PartSelectionApp(desktop.ensure_config(root)))
        names = ["uniqueSearchTerms", "passiveSearchType", "isExplicitPassiveComment",
                 "candidateKeywordFromFields", "bomRowMatchesSearch"]
        functions = []
        for name in names:
            match = re.search(r"function " + name + r"\([^)]*\) \{.*?\n\}", page, re.S)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        js = "\n".join(functions) + r'''
const assert = require('node:assert/strict');
assert.equal(candidateKeywordFromFields({refs:'U1',comment:'AD4630-24BBCZ',value:'AD4630-24BBCZ',footprint:'BGA-64'}), 'AD4630-24BBCZ');
assert.equal(candidateKeywordFromFields({refs:'U1',comment:'C4283613',footprint:'0805'}), 'C4283613');
assert.equal(candidateKeywordFromFields({refs:'R85',comment:'P2TC0805D5002LNTA',value:'50k',footprint:'0805'}), 'P2TC0805D5002LNTA 50k 0805');
assert.equal(candidateKeywordFromFields({refs:'C1',comment:'Capacitor',value:'100nF',footprint:'0402'}), 'capacitor 100nF 0402');
const row = {refs:'R85,R86',comment:'P2TC0805D5002LNTA',value:'50k',footprint:'0805',effective_c_code:'C4283613',page_refs:'POWER: R85,R86',description:'precision resistor'};
assert.ok(bomRowMatchesSearch(row,'r85'));
assert.ok(!bomRowMatchesSearch(row,'R8'));
assert.ok(bomRowMatchesSearch(row,'50K 0805'));
assert.ok(bomRowMatchesSearch(row,'c4283613'));
assert.ok(!bomRowMatchesSearch(row,'C428361'));
assert.ok(bomRowMatchesSearch(row,'power precision'));
assert.ok(bomRowMatchesSearch({...row,alternate_parts:[{c_code:'C1234',model:'Alternate'}]},'C1234'));
assert.ok(bomRowMatchesSearch(row,''));
assert.ok(!bomRowMatchesSearch(row,'missing'));
'''
        result = subprocess.run([shutil.which("node"), "-e", js], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('id="bomSearch"', page)
        self.assertNotIn('const selectedBom = loadUiSettings().selectedBom', page)

    def test_config_uses_doc_when_present_and_does_not_touch_cad_or_state(self):
        import desktop
        root = self.project()
        (root / "DOC").mkdir()
        sentinel = root / "BOM"
        sentinel.mkdir()
        state = sentinel / "part_selection_overrides.json"
        state.write_text('{"R1":{"note":"keep"}}', encoding="utf-8")
        before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        config = desktop.ensure_config(root)
        self.assertEqual(json.loads(config.read_text())["schematic_dir"], "DOC")
        self.assertTrue(all(p.read_bytes() == content for p, content in before.items()))

    def test_running_service_must_match_project_and_config(self):
        import desktop
        root = self.project()
        config = desktop.ensure_config(root)
        payload = {"project_root": str(root), "config_path": str(config), "desktop_launcher_version": 2}
        self.assertTrue(desktop.matches_service(payload, root, config))
        self.assertFalse(desktop.matches_service({**payload, "project_root": str(self.root)}, root, config))
        self.assertFalse(desktop.matches_service({"project_root": str(root)}, root, config))
        self.assertFalse(desktop.matches_service([], root, config))
        self.assertFalse(desktop.matches_service({**payload, "config_path": 99}, root, config))
        self.assertFalse(desktop.matches_service({**payload, "desktop_launcher_version": 0}, root, config))

    def test_managed_plugin_requirements_exclude_source_only_webview_dependency(self):
        managed = (PLUGIN / "requirements.txt").read_text()
        self.assertNotIn("pywebview", managed)
        self.assertIn("pywebview", (PLUGIN / "desktop-requirements.txt").read_text())

    def test_desktop_runtime_installs_in_private_environment_with_hidden_processes(self):
        import desktop
        with patch.object(desktop.subprocess, "run") as run:
            runtime = desktop.ensure_desktop_runtime(self.root)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any("venv" in command for command in commands))
            self.assertTrue(any("pip" in command and "install" in command for command in commands))
            self.assertTrue(all(str(PLUGIN / "desktop-requirements.txt") in command
                                for command in commands if "install" in command))
            self.assertNotEqual(runtime, Path(sys.executable))
            self.assertEqual(runtime.parent.parent, self.root / "desktop-runtime")
            if os.name == "nt":
                self.assertTrue(all(call.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
                                    for call in run.call_args_list))

    def test_new_project_defaults_to_schematic_in_real_api(self):
        import desktop
        from part_selection_server import PartSelectionApp, make_handler
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        root = self.project()
        app = PartSelectionApp(desktop.ensure_config(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            with urlopen(url + "/api/boms") as response:
                self.assertEqual(json.load(response)["default_bom"], "__schematic__")
            with urlopen(url + "/api/rows?bom=__schematic__&stock=0") as response:
                self.assertEqual(json.load(response)["rows"], [])
            with urlopen(url + "/api/config") as response:
                self.assertTrue(desktop.matches_service(json.load(response), root, app.config["config_path"]))
            with patch.object(app, "run_kicad_script", return_value={"ok": True}) as run:
                request = Request(url + "/api/kicad_jump", data=json.dumps(
                    {"url": "kicad-sch-search://R1"}).encode(), headers={"Content-Type": "application/json"})
                with urlopen(request) as response:
                    self.assertTrue(json.load(response)["ok"])
                run.assert_called_once_with("kicad-sch-search://R1", ["-Mode", "SearchPanel"])
                bad = Request(url + "/api/kicad_jump", data=b'{"url":"https://example.com"}',
                              headers={"Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(bad)
                self.assertEqual(error.exception.code, 400)
                run.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_pcm_bundle_is_self_contained_and_has_no_project_data(self):
        import package_plugin
        output = self.root / "plugin.zip"
        package_plugin.build_package(output)
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            self.assertIn("metadata.json", names)
            metadata = json.loads(archive.read("metadata.json"))
            self.assertEqual(metadata["versions"][0]["runtime"], "ipc")
            self.assertIn("resources", metadata)
            manifest = json.loads(archive.read("plugins/plugin.json"))
            self.assertEqual(manifest["actions"][0]["scopes"], ["pcb", "schematic"])
            self.assertIn("plugins/" + manifest["actions"][0]["entrypoint"], names)
            self.assertIn("plugins/app/part_selection_server.py", names)
            self.assertIn("plugins/app/fpga_pin_assignment.py", names)
            self.assertIn("plugins/app/kicad_schematic_search.ps1", names)
            self.assertIn("plugins/desktop-requirements.txt", names)
            self.assertFalse(any(n.startswith(("BOM/", "DOC/", "Libraries/")) for n in names))
            self.assertFalse(any("__pycache__" in n or n.endswith(".kicad_sch") for n in names))
            archive.extractall(self.root / "extracted")
        server = self.root / "extracted/plugins/app/part_selection_server.py"
        self.assertTrue(server.is_file())

    def test_installer_only_updates_plugin_and_preserves_previous_version(self):
        import package_plugin
        directory = self.root / "user_plugins"
        target = package_plugin.install_plugin(directory, self.root / "backups")
        (target / "local_marker").write_text("previous", encoding="utf-8")
        package_plugin.install_plugin(directory, self.root / "backups")
        self.assertFalse((target / "local_marker").exists())
        self.assertEqual(len(list((self.root / "backups").glob("*/local_marker"))), 1)
        self.assertTrue((target / "plugin.json").exists())

    def test_failed_install_restores_previous_version_after_partial_copy(self):
        import package_plugin
        directory = self.root / "user_plugins"
        target = package_plugin.install_plugin(directory, self.root / "backups")
        (target / "local_marker").write_text("previous", encoding="utf-8")
        copytree = shutil.copytree

        def fail_new_payload(source, destination, *args, **kwargs):
            if Path(source).name.startswith(".selection-install-"):
                Path(destination).mkdir()
                (Path(destination) / "partial").write_text("incomplete", encoding="utf-8")
                raise OSError("Simulated installation failure")
            return copytree(source, destination, *args, **kwargs)

        with patch.object(package_plugin.shutil, "copytree", side_effect=fail_new_payload):
            with self.assertRaisesRegex(OSError, "Simulated"):
                package_plugin.install_plugin(directory, self.root / "backups")
        self.assertEqual((target / "local_marker").read_text(), "previous")
        self.assertFalse((target / "partial").exists())

    def test_jump_passes_project_path_to_powershell_not_plugin_directory(self):
        import desktop
        from part_selection_server import PartSelectionApp
        root = self.project()
        app = PartSelectionApp(desktop.ensure_config(root))
        with patch("part_selection_server.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="OK", stderr="")
            self.assertTrue(app.run_kicad_script("kicad-sch-search://R1", ["-DryRun"])["ok"])
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-ProjectRoot") + 1], str(root))
        with self.assertRaises(ValueError):
            app.jump_to_schematic("https://example.com")

    @unittest.skipUnless(os.name == "nt", "Windows batch entrypoints")
    def test_chinese_launchers_forward_to_existing_entrypoints(self):
        cases = (("启动器件选料助手.bat", "start_part_selection_tool.bat", "--desktop"),)
        for wrapper_name, entry_name, argument in cases:
            with self.subTest(wrapper=wrapper_name):
                wrapper = self.root / wrapper_name
                shutil.copyfile(ROOT / "tools" / wrapper_name, wrapper)
                (self.root / entry_name).write_bytes(b"@echo off\r\necho forwarded:%~1\r\n")
                result = subprocess.run(
                    f'cmd.exe /d /c call "{wrapper}"', capture_output=True, text=True,
                    timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("forwarded:" + argument, result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell project ownership")
    def test_hosted_schematic_is_found_and_cached_by_window_not_process(self):
        command = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$tree = [System.Management.Automation.Language.Parser]::ParseFile($env:PLUGIN_SEARCH_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Get-KiCadCandidateWindows','Get-CachedKiCadWindow','Save-KiCadWindowCache','Test-SchematicEditorWindowName','Ensure-KiCadProjectOpen','Wait-KiCadEditorFrameClosed','Set-KiCadForeground')) {
    $node = $tree.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    . ([scriptblock]::Create($node.Extent.Text))
}
function Write-Log { param($Message) }
function Test-KiCadProcessBelongsToProject { param($Window) return $true }
function Get-Process { return [pscustomobject]@{Id=123;ProcessName='kicad';MainWindowHandle=[IntPtr]44;MainWindowTitle='board - PCB Editor'} }
$script:window = [pscustomobject]@{Id=123;ProcessName='kicad';MainWindowHandle=[IntPtr]55;MainWindowTitle='board - Schematic Editor'}
function Get-KiCadTopLevelWindows { return $script:window }
$script:window.MainWindowTitle = -join ([char]0x52A0,[char]0x8F7D,[char]0x539F,[char]0x7406,[char]0x56FE)
if (@(Get-KiCadCandidateWindows).Count -ne 0) { throw 'Loading dialog mistaken for schematic editor' }
$script:window.MainWindowTitle = 'board - Schematic Editor'
$windows = @(Get-KiCadCandidateWindows)
if ($windows.Count -ne 1 -or $windows[0].MainWindowHandle -ne 55) { throw 'Hosted schematic hidden behind PCB main window' }
$WindowCachePath = Join-Path $env:PLUGIN_TEST_ROOT 'window.json'
Save-KiCadWindowCache -Window $windows[0]
$cached = Get-CachedKiCadWindow
if ($null -eq $cached -or $cached.MainWindowHandle -ne 55) { throw 'Cache must preserve HWND, not just PID' }
$saved = Get-Content -LiteralPath $WindowCachePath -Raw | ConvertFrom-Json
if ($saved.hwnd -ne 55 -or $saved.pid -ne 123) { throw 'Cache lost window identity' }
$saved.hwnd = 66
$saved | ConvertTo-Json | Set-Content -LiteralPath $WindowCachePath
if ($null -ne (Get-CachedKiCadWindow)) { throw 'Stale HWND reused' }
function Get-KiCadProjectFile { return (Join-Path $env:PLUGIN_TEST_ROOT 'board.kicad_pro') }
function Start-Process { throw 'Existing project must not be launched again' }
Ensure-KiCadProjectOpen
$close = $tree.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Wait-KiCadEditorFrameClosed'}, $true).Extent.Text
if ($close -match 'Get-Process') { throw 'Frame close must check the HWND, not the PCB process main window' }
$foreground = $tree.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Set-KiCadForeground'}, $true).Extent.Text
if ($foreground -notmatch 'GetForegroundWindow\(\)\s*-eq\s*\$Target.MainWindowHandle') { throw 'Same process is not the same foreground window' }
'''
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            env={**os.environ, "PLUGIN_SEARCH_SCRIPT": str(ROOT / "tools/kicad_schematic_search.ps1"),
                 "PLUGIN_TEST_ROOT": str(self.root)},
            capture_output=True, text=True, errors="replace", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell project ownership")
    def test_schematic_window_ownership_rejects_other_and_unknown_projects(self):
        command = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$tree = [System.Management.Automation.Language.Parser]::ParseFile($env:PLUGIN_SEARCH_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Get-CommandLineProjectStatus','Test-KiCadProcessBelongsToProject','Get-HostedProjectStatus')) {
    $node = $tree.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    if (-not $node) { throw "Missing project isolation function: $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
$root = $env:PLUGIN_TEST_ROOT
function Get-ProjectRoot { return $env:PLUGIN_TEST_ROOT }
function Get-KiCadProjectFile { return (Join-Path $env:PLUGIN_TEST_ROOT 'board.kicad_pro') }
function Get-HostedProjectPaths { param($ProcessId) return $script:paths }
function Get-CimInstance { param($ClassName, $Filter, $ErrorAction) return [pscustomobject]@{CommandLine=$script:commandLine} }
$script:commandLine = '"C:\KiCad\eeschema.exe" "' + $root + '\DOC\Power.kicad_sch"'
if ((Get-CommandLineProjectStatus -CommandLine $script:commandLine -Root $root) -ne 'match') { throw 'Own child page not recognized' }
if (-not (Test-KiCadProcessBelongsToProject -Window ([pscustomobject]@{Id=123}))) { throw 'Own process rejected' }
$script:commandLine = '"C:\KiCad\eeschema.exe" "' + $root + '-other\Power.kicad_sch"'
if ((Get-CommandLineProjectStatus -CommandLine $script:commandLine -Root $root) -ne 'other') { throw 'Prefix collision accepted' }
if (Test-KiCadProcessBelongsToProject -Window ([pscustomobject]@{Id=456})) { throw 'Other project accepted' }
$script:commandLine = '"C:\KiCad\kicad.exe"'
if ((Get-CommandLineProjectStatus -CommandLine $script:commandLine -Root $root) -ne 'unknown') { throw 'Unknown association accepted' }
$rejected = $false
try { [void](Test-KiCadProcessBelongsToProject -Window ([pscustomobject]@{Id=789})) } catch { $rejected = $true }
if (-not $rejected) { throw 'Ambiguous window must stop synchronization' }
$script:paths = @((Join-Path $root 'board.kicad_pro'))
$window = [pscustomobject]@{Id=789;MainWindowTitle='board - Schematic Editor'}
if (-not (Test-KiCadProcessBelongsToProject -Window $window)) { throw 'Verified hosted schematic rejected' }
$window.MainWindowTitle = 'Power [board/Top/Power] - Schematic Editor'
if (-not (Test-KiCadProcessBelongsToProject -Window $window)) { throw 'Hosted hierarchy page rejected after navigation' }
$window.MainWindowTitle = 'Power [board-other/Top/Power] - Schematic Editor'
$rejected = $false
try { [void](Test-KiCadProcessBelongsToProject -Window $window) } catch { $rejected = $true }
if (-not $rejected) { throw 'Other hierarchy accepted' }
$window.MainWindowTitle = 'board - Schematic Editor'
$script:paths = @((Join-Path ($root + '-other') 'board.kicad_pro'))
if (Test-KiCadProcessBelongsToProject -Window $window) { throw 'Same basename in different directory accepted' }
$script:paths = @((Join-Path $root 'board.kicad_pro'), (Join-Path $root 'second.kicad_pro'))
$rejected = $false
try { [void](Test-KiCadProcessBelongsToProject -Window $window) } catch { $rejected = $true }
if (-not $rejected) { throw 'Multiple hosted projects must remain ambiguous' }
'''
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            env={**os.environ, "PLUGIN_SEARCH_SCRIPT": str(ROOT / "tools/kicad_schematic_search.ps1"),
                 "PLUGIN_TEST_ROOT": str(self.root / "project with spaces")},
            capture_output=True, text=True, errors="replace", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
