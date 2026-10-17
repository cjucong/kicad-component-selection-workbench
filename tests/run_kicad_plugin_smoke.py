"""Opt-in Windows/WebView2 smoke test using an extracted package and disposable project."""
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/kicad_plugin"))
import package_plugin


def main():
    import jsonschema
    output = ROOT / "Temp/kicad_plugin_qa"
    output.mkdir(parents=True, exist_ok=True)
    archive_path = package_plugin.build_package(output / "smoke.zip")
    with zipfile.ZipFile(archive_path) as archive:
        for name in ("metadata.json", "plugins/plugin.json"):
            data = json.loads(archive.read(name))
            request = Request(data["$schema"], headers={"User-Agent": "curl/8.13.0"})
            with urlopen(request, timeout=30) as response:
                schema = json.load(response)
            jsonschema.validate(data, schema)
        archive.extractall(output / "installed")
    sys.path.insert(0, str(output / "installed/plugins"))
    import desktop
    assert desktop.CORE == output / "installed/plugins/app"
    if "--prepare-runtime" in sys.argv:
        print(desktop.ensure_desktop_runtime(output))
        return
    import webview
    project = output / "disposable project"
    project.mkdir(exist_ok=True)
    (project / "test.kicad_pro").write_text("{}", encoding="utf-8")
    (project / "test.kicad_sch").write_text('(kicad_sch (version 20250114))', encoding="utf-8")
    config = desktop.ensure_config(project)
    processes = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    result = {"schemas": "passed", "extracted_package": True}
    try:
        with patch.object(desktop.subprocess, "Popen", side_effect=launch):
            url = desktop.ensure_backend(project, config, output)
            assert desktop.ensure_backend(project, config, output) == url
        assert len(processes) == 1, "Duplicate backend started"
        with urlopen(url + "api/config") as response:
            assert desktop.matches_service(json.load(response), project, config)
        window = webview.create_window("Selection plugin QA", url, width=1100, height=720)

        def check_page():
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    state = window.evaluate_js("({title:document.title,bom:document.querySelector('#bomSelect')?.value,table:!!document.querySelector('table')})")
                    if state and state.get("bom") == "__schematic__" and state.get("table"):
                        result["webview"] = state
                        return
                    time.sleep(0.2)
                result["error"] = "WebView2 did not render the BOM controls"
            except Exception as exc:
                result["error"] = str(exc)
            finally:
                window.destroy()

        webview.start(check_page, gui="edgechromium", private_mode=False,
                      storage_path=str(output / "webview"))
        assert "webview" in result and "error" not in result, result
        result["backend_reused"] = True
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True))
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
