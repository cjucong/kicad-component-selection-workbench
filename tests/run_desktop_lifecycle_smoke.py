"""Hidden real WebView2 close/restart smoke, only on a disposable project."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from _scratch import temporary_project

ROOT = Path(__file__).resolve().parents[1]


def child(root, phase):
    sys.path.insert(0,str(ROOT / 'tools/kicad_plugin'))
    import desktop
    import webview
    original_create = webview.create_window
    failure = []

    def create(*args, **kwargs):
        kwargs['hidden'] = True
        window = original_create(*args, **kwargs)

        def exercise():
            try:
                deadline = time.monotonic()+30
                while not window.evaluate_js("document.querySelectorAll('#rows tr').length===1 && !!window.pywebview?.api?.finish_exit"):
                    if time.monotonic()>deadline:
                        raise RuntimeError('Workbench did not load')
                    time.sleep(0.1)
                if phase=='close':
                    window.evaluate_js("document.querySelector('#openPluginUpdates').click(); null")
                    while not window.evaluate_js("document.querySelector('#pluginVersion').textContent.includes('0.1.4')"):
                        if time.monotonic()>deadline:
                            raise RuntimeError('Native update bridge did not respond')
                        time.sleep(0.1)
                    window.evaluate_js("document.querySelector('#pluginUpdatesDialog [data-close-update]').click()")
                    window.evaluate_js("window.openPathSettings(); null")
                    while not window.evaluate_js("document.querySelector('#pathSettingsDialog').open"):
                        if time.monotonic()>deadline:
                            raise RuntimeError('Project settings did not open')
                        time.sleep(0.1)
                    assert window.evaluate_js("document.querySelector('#pathSourceSchematic').checked && !document.querySelector('#pathAdvanced').open"), 'Unexpected settings defaults'
                    window.evaluate_js("document.querySelector('#pathSettingsDialog [data-close-paths]').click()")
                    window.evaluate_js("const input=document.querySelector('#rows [data-field=value]'); input.value='220nF'; input.dispatchEvent(new Event('input',{bubbles:true}));")
                    # Real WinForms Closing must defer, flush JS edits and release the port.
                    window.destroy()
                else:
                    assert window.evaluate_js("document.querySelector('#rows [data-field=value]').value")=='220nF'
                    button = 'restartWorkbench' if phase=='restart' else 'exitWorkbench'
                    window.evaluate_js(f"document.getElementById('{button}').click()")
            except Exception as exc:
                failure.append(str(exc))
                lifecycle = kwargs['js_api']._lifecycle
                try:
                    lifecycle._stop_backend()
                finally:
                    lifecycle.allow_close = True
                    window.destroy()

        window.events.loaded += exercise
        return window

    webview.create_window = create

    def restart(project, config, logs):
        command = [sys.executable,'-B',str(Path(__file__).resolve()),'--child',str(root),'--phase','final']
        with (root / 'restarted.log').open('wb') as output:
            flags = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            subprocess.run(command,stdout=output,stderr=output,timeout=45,check=True,**flags)

    desktop.relaunch_desktop = restart
    desktop.show_error = lambda message:failure.append(message)
    status = desktop.main(['--project',str(root),'--config',str(root / 'settings.json')])
    assert status==0 and not failure, failure
    (root / (phase+'.ok')).write_text('passed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python',type=Path,default=Path(sys.executable),help='Interpreter with desktop requirements/WebView2')
    parser.add_argument('--child',type=Path)
    parser.add_argument('--phase',default='close')
    args = parser.parse_args()
    if args.child:
        child(args.child,args.phase)
        return
    with temporary_project(prefix='native-desktop-lifecycle-') as directory:
        root = Path(directory)
        (root / 'sample.kicad_pro').write_text('{}')
        schematic = '(kicad_sch (symbol (property "Reference" "C1") (property "Comment" "capacitor") (property "Value" "100nF") (property "Footprint" "Library:0603")))'
        (root / 'sample.kicad_sch').write_text(schematic)
        (root / 'settings.json').write_text(json.dumps({'schematic_dir':'.','source_bom':'__schematic__'}))
        flags = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
        env = {**os.environ,'PYTHONDONTWRITEBYTECODE':'1',
               'KICAD_PART_SELECTION_HOME':str(root.parent / 'desktop')}
        for phase in ('close','restart'):
            result = subprocess.run([str(args.python),'-B',str(Path(__file__).resolve()),'--child',str(root),'--phase',phase],
                capture_output=True,text=True,encoding='utf-8',timeout=90,env=env,**flags)
            if result.returncode:
                raise AssertionError(result.stdout+'\n'+result.stderr)
            assert (root / (phase+'.ok')).exists()
        assert (root / 'final.ok').exists(), (root / 'restarted.log').read_text(errors='replace')
        records = json.loads((root / 'BOM/part_selection_overrides.json').read_text(encoding='utf-8'))
        assert records['C1']['field_overrides']['value']=='220nF'
        assert (root / 'sample.kicad_sch').read_text()==schematic
        print(json.dumps({'real_webview2':'passed','native_close_autosave':'passed',
                          'port_release_and_reopen':'passed','restart_and_exit_menu':'passed',
                          'production_cad_written':False}))


if __name__=='__main__':
    main()
