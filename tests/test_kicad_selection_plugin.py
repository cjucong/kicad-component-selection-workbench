import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from _scratch import temporary_project
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "tools" / "kicad_plugin"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(PLUGIN))


class SelectionPluginTests(unittest.TestCase):
    def test_non_ascii_powershell_payload_has_utf8_bom_for_english_windows(self):
        for name, data in importlib.import_module('package_plugin').package_files().items():
            if name.endswith('.ps1') and not data.isascii():
                self.assertTrue(data.startswith(b'\xef\xbb\xbf'), name)
                data.decode('utf-8-sig')

    def setUp(self):
        self.temp = temporary_project(prefix="selection_plugin_")
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

    def test_invalid_pcb_project_path_falls_back_to_schematic_document(self):
        import desktop
        root = self.project()
        for path in ("", "relative-project", str(self.root / "missing")):
            with self.subTest(path=path):
                client = SimpleNamespace(
                    get_board=lambda: SimpleNamespace(get_project=lambda: SimpleNamespace(path=path)),
                    get_open_documents=lambda kind: [SimpleNamespace()],
                    get_project=lambda doc: SimpleNamespace(path=str(root)),
                )
                with patch.dict(sys.modules, {"kipy.proto.common.types.base_types_pb2": SimpleNamespace(
                        DocumentType=SimpleNamespace(DOCTYPE_SCHEMATIC=1))}):
                    self.assertEqual(desktop.project_from_ipc(client), root)

    def test_schematic_projects_are_compared_by_validated_root_not_raw_path(self):
        import desktop
        root = self.project()
        docs = [SimpleNamespace(path=str(root)), SimpleNamespace(path=str(root / "board.kicad_pro"))]
        client = SimpleNamespace(
            get_board=lambda: (_ for _ in ()).throw(RuntimeError("No PCB open")),
            get_open_documents=lambda kind: docs,
            get_project=lambda doc: doc,
        )
        with patch.dict(sys.modules, {"kipy.proto.common.types.base_types_pb2": SimpleNamespace(
                DocumentType=SimpleNamespace(DOCTYPE_SCHEMATIC=1))}):
            self.assertEqual(desktop.project_from_ipc(client), root)

    def test_empty_schematic_metadata_uses_matching_kicad_project_environment(self):
        import desktop
        root = self.project()
        client = SimpleNamespace(
            get_board=lambda: (_ for _ in ()).throw(RuntimeError("No PCB open")),
            get_open_documents=lambda kind: [SimpleNamespace(board_filename="board.kicad_sch")],
            get_project=lambda doc: SimpleNamespace(path=""),
        )
        with patch.dict(sys.modules, {"kipy.proto.common.types.base_types_pb2": SimpleNamespace(
                DocumentType=SimpleNamespace(DOCTYPE_SCHEMATIC=1))}), patch.dict(
                os.environ, {"KIPRJMOD": str(root)}):
            self.assertEqual(desktop.project_from_ipc(client), root)

    def test_environment_fallback_refuses_unverified_or_stale_document(self):
        import desktop
        root = self.project()
        for path, filename in (("", "board.kicad_sch"), ("relative", "board.kicad_sch"),
                               (str(root), "other.kicad_sch"), (str(root), "../board.kicad_sch"),
                               (str(root), ""), (str(root), str(self.root / "board.kicad_sch"))):
            with self.subTest(path=path, filename=filename):
                client = SimpleNamespace(
                    get_board=lambda: (_ for _ in ()).throw(RuntimeError("No PCB open")),
                    get_open_documents=lambda kind: [SimpleNamespace(board_filename=filename)],
                    get_project=lambda doc: SimpleNamespace(path=""),
                )
                with patch.dict(sys.modules, {"kipy.proto.common.types.base_types_pb2": SimpleNamespace(
                        DocumentType=SimpleNamespace(DOCTYPE_SCHEMATIC=1))}), patch.dict(
                        os.environ, {"KIPRJMOD": path}):
                    with self.assertRaises(ValueError):
                        desktop.project_from_ipc(client)

    def test_multiple_schematic_projects_are_not_silently_selected(self):
        import desktop
        first, second = self.project("first"), self.project("second")
        client = SimpleNamespace(
            get_board=lambda: (_ for _ in ()).throw(RuntimeError("No PCB open")),
            get_open_documents=lambda kind: [first, second],
            get_project=lambda doc: SimpleNamespace(path=str(doc)),
        )
        with patch.dict(sys.modules, {"kipy.proto.common.types.base_types_pb2": SimpleNamespace(
                DocumentType=SimpleNamespace(DOCTYPE_SCHEMATIC=1))}):
            with self.assertRaises(ValueError):
                desktop.project_from_ipc(client)

    def test_schematic_launch_does_not_open_picker_when_pcb_path_is_empty(self):
        import desktop
        from part_selection_lifecycle import DesktopLifecycle
        view = MagicMock()
        root = self.project()
        client = SimpleNamespace(
            get_board=lambda: SimpleNamespace(get_project=lambda: SimpleNamespace(path="")),
            get_open_documents=lambda kind: [SimpleNamespace(board_filename="board.kicad_sch")],
            get_project=lambda doc: SimpleNamespace(path=""),
        )
        with patch.dict(sys.modules, {
                "webview":view,
                "kipy": SimpleNamespace(KiCad=lambda: client),
                "kipy.proto.common.types.base_types_pb2": SimpleNamespace(
                    DocumentType=SimpleNamespace(DOCTYPE_SCHEMATIC=1))}), patch.dict(
                os.environ, {"KICAD_API_SOCKET": "test-socket", "KIPRJMOD": str(root)}), patch.object(
                desktop, "cache_directory", return_value=self.root), patch.object(
                desktop.logging, "basicConfig"), patch.object(desktop, "choose_project") as picker, patch.object(
                desktop, "acquire_window", return_value=(None, True)), patch.object(
                desktop, "release_window"), patch.object(desktop, "ensure_config", return_value=root / "config.json"), patch.object(
                desktop, "ensure_backend", return_value="http://127.0.0.1:1234/") as backend, patch.object(
                DesktopLifecycle, "_stop_backend"):
            self.assertEqual(desktop.main([]), 0)
            picker.assert_not_called()
            self.assertEqual(backend.call_args.args[0], root)
            self.assertEqual(view.create_window.call_args.args[1],"http://127.0.0.1:1234/")
            self.assertIn('js_api',view.create_window.call_args.kwargs)

    def test_entrypoint_records_startup_before_argument_parser_exits(self):
        for argument, status in (("--help", 0), ("--invalid-dispatch-argument", 2)):
            with self.subTest(argument=argument):
                cache = self.root / str(status)
                result = subprocess.run(
                    [sys.executable, "-B", str(PLUGIN / "desktop.py"), argument],
                    env={**os.environ, "LOCALAPPDATA": str(cache),
                         "KICAD_API_SOCKET": "test-socket", "KICAD_API_TOKEN": "secret-token"},
                    capture_output=True, text=True, timeout=15,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
                )
                self.assertEqual(result.returncode, status, result.stderr)
                log = cache / "KiCadPartSelection" / "desktop.log"
                self.assertTrue(log.is_file(), "Early entrypoint exits must leave a dispatch witness")
                content = log.read_text(encoding="utf-8")
                self.assertIn("Desktop entry started", content)
                self.assertIn(str(PLUGIN / "desktop.py"), content)
                self.assertIn(sys.executable, content)
                self.assertIn("ipc_socket_present=True", content)
                self.assertIn(f"Argument parser exited status={status}", content)
                self.assertNotIn("secret-token", content)
                self.assertNotIn("test-socket", content)
                self.assertEqual(list(cache.rglob("*")), [log.parent, log])

    def test_native_window_enables_find_shortcut_without_debug_mode(self):
        import desktop
        settings = SimpleNamespace(AreBrowserAcceleratorKeysEnabled=False, AreDefaultContextMenusEnabled=False, AreDevToolsEnabled=False)
        window = SimpleNamespace(native=SimpleNamespace(
            webview=SimpleNamespace(CoreWebView2=SimpleNamespace(Settings=settings)),
            Invoke=lambda action: action(),
        ))
        with patch.dict(sys.modules, {"System": SimpleNamespace(Action=lambda action: action)}):
            desktop.enable_browser_shortcuts(window)
        self.assertTrue(settings.AreBrowserAcceleratorKeysEnabled)
        self.assertTrue(settings.AreDefaultContextMenusEnabled)
        self.assertFalse(settings.AreDevToolsEnabled)

    def test_native_window_explicitly_allows_text_selection(self):
        tree = ast.parse((PLUGIN / 'desktop.py').read_text(encoding='utf-8'))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'create_window']
        self.assertTrue(calls)
        for call in calls:
            options = {kw.arg: kw.value for kw in call.keywords}
            self.assertIn('text_select', options)
            self.assertIs(ast.literal_eval(options['text_select']), True)

    def legacy_plugin(self):
        import package_plugin
        registry = []

        class ActionPlugin:
            def __init__(self):
                self.defaults()

            def register(self):
                registry.append(self)

        native = SimpleNamespace(ActionPlugin=ActionPlugin, GetBoard=lambda: None)
        errors = []
        wx = SimpleNamespace(MessageBox=lambda *args: errors.append(args), OK=1, ICON_ERROR=2)
        target = package_plugin.install_plugin(self.root / "plugins", self.root / "backups")
        self.addCleanup(sys.modules.pop, target.name, None)
        with patch.dict(sys.modules, {"pcbnew": native, "wx": wx}), patch.object(
                sys, "path", [str(target.parent), *sys.path]):
            module = importlib.import_module(target.name)
        self.assertEqual(len(registry), 1, "The installed package must register one legacy PCB action")
        return target, module, native, registry[0], wx, errors

    def test_installed_legacy_pcb_action_launches_hidden_with_explicit_active_board(self):
        target, module, native, action, wx, errors = self.legacy_plugin()
        self.assertIn("PCB \u517c\u5bb9\u5165\u53e3", action.name)
        self.assertTrue(action.show_toolbar_button)
        self.assertEqual(Path(action.icon_file_name), target / "icon.png")
        board = self.project() / "board.kicad_pcb"
        board.write_text("(kicad_pcb)", encoding="utf-8")
        native.GetBoard = lambda: SimpleNamespace(GetFileName=lambda: str(board))
        pythonw = self.root / "pythonw.exe"
        pythonw.touch()
        before = {path: path.read_bytes() for path in board.parent.iterdir() if path.is_file()}
        with patch.dict(sys.modules, {"wx": wx}), patch.dict(os.environ, {
                "LOCALAPPDATA": str(self.root / "cache"), "KICAD_API_SOCKET": "wrong-editor",
                "KICAD_API_TOKEN": "secret", "PYTHONHOME": "wrong-python"}), patch.object(
                module.sys, "executable", str(self.root / "kicad.exe")), patch.object(
                module.subprocess, "Popen") as launch:
            action.Run()
            action.Run()
        self.assertEqual(errors, [])
        self.assertEqual(launch.call_count, 2, "Retained GUI must not block subsequent PCB launches")
        command = launch.call_args.args[0]
        self.assertEqual(command, [str(pythonw), "-B", str(target / "desktop.py"), "--project", str(board)])
        options = launch.call_args.kwargs
        self.assertEqual(options["cwd"], board.parent)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertIs(options["stdout"], options["stderr"])
        for variable in ("KICAD_API_SOCKET", "KICAD_API_TOKEN", "PYTHONHOME"):
            self.assertNotIn(variable, options["env"])
        if os.name == "nt":
            self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertTrue(all(path.read_bytes() == content for path, content in before.items()))

    def test_legacy_pcb_action_rejects_unsaved_or_relative_board_without_launch(self):
        target, module, native, action, wx, errors = self.legacy_plugin()
        for filename in (None, "", "board.kicad_pcb", str(self.root / "missing.kicad_pcb")):
            with self.subTest(filename=filename), patch.dict(sys.modules, {"wx": wx}), patch.object(
                    module.subprocess, "Popen") as launch:
                native.GetBoard = lambda: (None if filename is None else SimpleNamespace(
                    GetFileName=lambda: filename))
                action.Run()
                launch.assert_not_called()
        self.assertEqual(len(errors), 4)

    @unittest.skipUnless(os.name == "nt", "Windows hidden desktop host")
    def test_legacy_pcb_action_hands_project_to_real_hidden_python_process(self):
        target, module, native, action, wx, errors = self.legacy_plugin()
        if not Path(sys.executable).with_name("pythonw.exe").is_file():
            self.skipTest("Pythonw is unavailable beside the test interpreter")
        board = self.project() / "board.kicad_pcb"
        board.write_text("(kicad_pcb)", encoding="utf-8")
        native.GetBoard = lambda: SimpleNamespace(GetFileName=lambda: str(board))
        marker = board.parent / "child.json"
        (target / "desktop.py").write_text(
            "import argparse,json,os\nfrom pathlib import Path\n"
            "parser=argparse.ArgumentParser()\nparser.add_argument('--project')\n"
            "args=parser.parse_args()\n"
            "Path('child.json').write_text(json.dumps({'project':args.project,'cwd':os.getcwd()}))\n",
            encoding="utf-8",
        )
        children = []
        popen = subprocess.Popen

        def start(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        with patch.dict(sys.modules, {"wx": wx}), patch.dict(os.environ, {
                "LOCALAPPDATA": str(self.root / "cache")}), patch.object(
                module.subprocess, "Popen", side_effect=start):
            action.Run()
        try:
            self.assertEqual(errors, [])
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0].wait(timeout=10), 0)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
        self.assertTrue(marker.is_file(), "Hidden child did not receive the board path")
        payload = json.loads(marker.read_text())
        self.assertEqual(Path(payload["project"]), board)
        self.assertEqual(Path(payload["cwd"]), board.parent)

    def test_missing_swig_does_not_prevent_package_import_or_remove_ipc_pcb_entry(self):
        import package_plugin
        target = package_plugin.install_plugin(self.root / "plugins", self.root / "backups")
        self.addCleanup(sys.modules.pop, target.name, None)
        with patch.dict(sys.modules, {"pcbnew": None}), patch.object(
                sys, "path", [str(target.parent), *sys.path]):
            importlib.import_module(target.name)
        manifest = json.loads((target / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["actions"][0]["scopes"], ["pcb", "schematic"])

    def test_installed_kicad_legacy_loader_accepts_package_with_ipc_manifest(self):
        source = Path("C:/Program Files/KiCad/10.0/bin/Lib/site-packages/pcbnew.py")
        if not source.is_file():
            self.skipTest("Installed KiCad legacy loader is unavailable")
        import package_plugin
        target = package_plugin.install_plugin(self.root / "plugins", self.root / "backups")
        registered = []

        class ActionPlugin:
            def __init__(self):
                self.defaults()

            def register(self):
                registered.append(self)

        native = SimpleNamespace(ActionPlugin=ActionPlugin, SETTINGS_MANAGER=SimpleNamespace(
            GetUserSettingsPath=lambda: ""))
        tree = ast.parse(source.read_text(encoding="utf-8"))
        functions = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                                    and node.name in {"LoadPlugins", "LoadPluginModule"}], type_ignores=[])
        namespace = {"KICAD_PLUGINS": {}, "__builtins__": __builtins__}
        exec(compile(functions, str(source), "exec"), namespace)
        self.addCleanup(sys.modules.pop, target.name, None)
        with patch.dict(sys.modules, {"pcbnew": native}), patch.object(sys, "path", list(sys.path)):
            namespace["LoadPlugins"](userpath=str(target.parent))
        self.assertEqual(namespace["NOT_LOADED_WIZARDS"], "", namespace["FULL_BACK_TRACE"])
        self.assertEqual(len(registered), 1)
        self.assertTrue((target / "plugin.json").is_file())

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
        payload = {"project_root": str(root), "config_path": str(config), "desktop_launcher_version": 3,
                   "backend_directory": str(desktop.CORE)}
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
            self.assertEqual(metadata["versions"][0]["version"], "0.1.5")
            self.assertIn("plugins/__init__.py", names)
            self.assertIn("plugins/" + manifest["actions"][0]["entrypoint"], names)
            self.assertIn("plugins/app/part_selection_server.py", names)
            self.assertIn("plugins/app/kicad_sexpr.py", names)
            self.assertNotIn("plugins/app/fpga_pin_assignment.py", names)
            self.assertIn("plugins/app/kicad_schematic_search.ps1", names)
            self.assertIn("plugins/desktop-requirements.txt", names)
            self.assertFalse(any(n.startswith(("BOM/", "DOC/", "Libraries/")) for n in names))
            self.assertFalse(any("__pycache__" in n or n.endswith(".kicad_sch") for n in names))
            archive.extractall(self.root / "extracted")
        server = self.root / "extracted/plugins/app/part_selection_server.py"
        self.assertTrue(server.is_file())

    def test_converter_bundle_requires_matching_source_and_licensing_inputs(self):
        import package_plugin
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (runtime / 'easykiconverter.exe').write_bytes(b'fixture executable')
        with self.assertRaises(ValueError):
            package_plugin.package_files(converter_runtime=runtime)

    def test_full_bundle_contains_sources_notices_and_hashes_without_private_data(self):
        import hashlib
        import package_plugin
        runtime = self.root / 'runtime'
        runtime.mkdir()
        for name in ('easykiconverter.exe', 'Qt6Core.dll', 'Qt6Network.dll'):
            (runtime / name).write_bytes(b'fixture ' + name.encode())
        (runtime / 'generated').mkdir()
        (runtime / 'generated/private.json').write_text('do not package')
        (runtime / 'EasyKiConverterCore.lib').write_bytes(b'dev only')
        source = self.root / 'source'
        source.mkdir()
        (source / 'LICENSE').write_text('GNU GENERAL PUBLIC LICENSE Version 3')
        (source / 'CMakeLists.txt').write_text('fixture build')
        notices = self.root / 'notices'
        notices.mkdir()
        for name in ('Qt-GPL-3.0.txt', 'Qt-LGPL-3.0.txt', 'QXlsx-LICENSE', 'zlib-LICENSE', 'THIRD_PARTY.md'):
            (notices / name).write_text('fixture license')
        with patch.object(package_plugin, 'converter_source_commit', return_value='1acc47c028ed1f75066ba134f6a2f327c0751c0e'), patch.object(package_plugin, 'converter_version', return_value='3.1.11'), patch.object(package_plugin, 'source_archive', return_value=b'fixture source archive'), patch.object(package_plugin, 'verify_converter_runtime'):
            files = package_plugin.package_files(converter_runtime=runtime, converter_source=source, converter_notices=notices)
        prefix = 'plugins/third_party/EasyKiConverter/'
        self.assertIn(prefix + 'runtime/easykiconverter.exe', files)
        self.assertIn(prefix + 'source/EasyKiConverter-v3.1.11.zip', files)
        self.assertIn(prefix + 'LICENSE', files)
        self.assertFalse(any('/generated/' in path or path.endswith('.lib') for path in files))
        manifest = json.loads(files[prefix + 'bundle-manifest.json'])
        self.assertEqual(manifest['commit'], '1acc47c028ed1f75066ba134f6a2f327c0751c0e')
        for path, checksum in manifest['sha256'].items():
            self.assertEqual(hashlib.sha256(files[prefix + path]).hexdigest(), checksum)

    def test_converter_runtime_requires_pinned_upstream_archive(self):
        import package_plugin
        release = self.root / 'untrusted.zip'
        with zipfile.ZipFile(release, 'w') as archive:
            archive.writestr('easykiconverter.exe', b'not the pinned executable')
        with self.assertRaisesRegex(ValueError, 'upstream release'):
            package_plugin.verify_converter_runtime(release, {'runtime/easykiconverter.exe': b'untrusted'})

    def test_converter_runtime_rejects_modified_dll_even_with_valid_archive(self):
        import hashlib
        import package_plugin
        release = self.root / 'release.zip'
        with zipfile.ZipFile(release, 'w') as archive:
            archive.writestr('Qt6Core.dll', b'trusted fixture DLL')
        checksum = hashlib.sha256(release.read_bytes()).hexdigest()
        with patch.object(package_plugin, 'CONVERTER_RELEASE_SHA256', checksum):
            package_plugin.verify_converter_runtime(release, {'runtime/Qt6Core.dll': b'trusted fixture DLL'})
            with self.assertRaisesRegex(ValueError, 'Runtime does not match'):
                package_plugin.verify_converter_runtime(release, {'runtime/Qt6Core.dll': b'modified DLL'})

    def test_installer_only_updates_plugin_and_preserves_previous_version(self):
        import package_plugin
        directory = self.root / "user_plugins"
        target = package_plugin.install_plugin(directory, self.root / "backups")
        (target / "local_marker").write_text("previous", encoding="utf-8")
        package_plugin.install_plugin(directory, self.root / "backups")
        self.assertFalse((target / "local_marker").exists())
        self.assertEqual(len(list((self.root / "backups").glob("*/local_marker"))), 1)
        self.assertTrue((target / "plugin.json").exists())

    def test_installer_migrates_dotted_directory_without_duplicate_ipc_registration(self):
        import package_plugin
        directory = self.root / "user_plugins"
        old = directory / "com.smu.part-selection"
        old.mkdir(parents=True)
        (old / "plugin.json").write_bytes((PLUGIN / "plugin.json").read_bytes())
        (old / "marker").write_text("previous", encoding="utf-8")
        target = package_plugin.install_plugin(directory, self.root / "backups")
        self.assertEqual(target.name, "com_smu_part-selection")
        self.assertFalse(old.exists())
        self.assertEqual(len(list(directory.rglob("plugin.json"))), 1)
        self.assertEqual(len(list((self.root / "backups").glob("*/marker"))), 1)

    def test_failed_migration_restores_both_previous_plugin_directories(self):
        import package_plugin
        directory = self.root / "user_plugins"
        previous = [directory / name for name in ("com.smu.part-selection", "com_smu_part-selection")]
        for path in previous:
            path.mkdir(parents=True)
            (path / "marker").write_text(path.name, encoding="utf-8")
        copytree = shutil.copytree

        def fail_payload(source, destination, *args, **kwargs):
            if Path(source).name.startswith(".selection-install-"):
                Path(destination).mkdir()
                (Path(destination) / "partial").touch()
                raise OSError("Simulated migration failure")
            return copytree(source, destination, *args, **kwargs)

        with patch.object(package_plugin.shutil, "copytree", side_effect=fail_payload):
            with self.assertRaisesRegex(OSError, "Simulated migration"):
                package_plugin.install_plugin(directory, self.root / "backups")
        for path in previous:
            self.assertEqual((path / "marker").read_text(), path.name)
            self.assertFalse((path / "partial").exists())

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
foreach ($name in @('Get-KiCadCandidateWindows','Get-CachedKiCadWindow','Save-KiCadWindowCache','Test-SchematicEditorWindowName','Get-KiCadProjectManagerWindow','Ensure-KiCadProjectOpen','Wait-KiCadEditorFrameClosed','Set-KiCadForeground')) {
    $node = $tree.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    . ([scriptblock]::Create($node.Extent.Text))
}
function Write-Log { param($Message) }
function Test-KiCadProcessBelongsToProject { param($Window) return $true }
function Get-HostedProjectStatus { param($Window) return 'match' }
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
$script:window = [pscustomobject]@{Id=123;ProcessName='kicad';MainWindowHandle=[IntPtr]44;MainWindowTitle='board - KiCad 10.0'}
$manager = Ensure-KiCadProjectOpen
if ($manager.MainWindowHandle -ne 44) { throw 'Must return the manager, not an editor in the same process' }
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
function Assert-KiCadWindowResponsive { param($Target, $Phase) }
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
