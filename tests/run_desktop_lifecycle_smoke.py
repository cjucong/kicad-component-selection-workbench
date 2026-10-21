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
        if not hasattr(kwargs['js_api'], '_lifecycle'):
            def mock_search():
                window.evaluate_js("""
                    const originalFetch = window.fetch.bind(window);
                    window.fetch = (url, options) => String(url).startsWith('/api/lcsc_search')
                      ? Promise.resolve(new Response(JSON.stringify({ok:true,done:true,items:[{
                        c_code:'C123456',model:'TEST-CAP',brand:'Test Maker',package:'0603',
                        stock:'1000',param:'220nF X7R 50V',param_detail:'Capacitance: 220nF'
                      }],sources:{domestic:'ready'},revision:1}),{headers:{'Content-Type':'application/json'}}))
                      : originalFetch(url, options);
                    null;
                """)
            window.events.loaded += mock_search
            return window

        def exercise():
            try:
                deadline = time.monotonic()+30
                while not window.evaluate_js("document.querySelectorAll('#rows tr').length===1 && !!window.pywebview?.api?.finish_exit"):
                    if time.monotonic()>deadline:
                        state = window.evaluate_js("JSON.stringify({url:location.href,ready:document.readyState,rows:document.querySelectorAll('#rows tr').length,state:document.querySelector('#state')?.textContent,bridge:!!window.pywebview?.api?.finish_exit,text:document.body.innerText.slice(0,1000)})")
                        raise RuntimeError('Workbench did not load: '+str(state))
                    time.sleep(0.1)
                if phase=='candidate':
                    print('candidate: opening',flush=True)
                    lifecycle = kwargs['js_api']._lifecycle
                    window.evaluate_js("document.querySelector('#rows [data-action=\\\"open-candidates\\\"]').click(); null")
                    while not lifecycle.candidates._ready.is_set():
                        if time.monotonic()>deadline:
                            raise RuntimeError('Native candidate child did not load')
                        time.sleep(0.1)
                    candidate = lifecycle.candidates._window
                    print('candidate: loaded',flush=True)
                    while not candidate.evaluate_js("!!window.pywebview?.api?.candidate_window && document.querySelector('#candidateModal').classList.contains('open')"):
                        if time.monotonic()>deadline:
                            raise RuntimeError('Native candidate request did not arrive')
                        time.sleep(0.1)
                    candidate.evaluate_js("candidateKeyword.value='native retained query'; null")
                    print('candidate: request ready',flush=True)
                    window.resize(800,500)
                    window.move(40,40)
                    candidate.resize(760,480)
                    candidate.move(880,70)
                    print('candidate: moved',flush=True)
                    assert candidate.x >= window.x + window.width, 'Child remains bounded inside main window'
                    assert not hasattr(candidate._js_api, 'finish_exit')
                    assert lifecycle.candidates.main_action('minimize')['ok']
                    print('candidate: hidden',flush=True)
                    window.evaluate_js("document.querySelector('#candidateMinimizedBar').click(); null")
                    time.sleep(0.3)
                    assert candidate.evaluate_js("candidateKeyword.value")=='native retained query'
                    assert candidate.evaluate_js("document.querySelector('header').getBoundingClientRect().height===0")
                    candidate.evaluate_js("candidateTargetRow.dataset.candidateSaving='1'; null")
                    assert not lifecycle.finish_exit()['ok'], 'Busy selection must retain the host'
                    candidate.destroy()
                    time.sleep(0.3)
                    assert lifecycle.candidates._request is not None
                    candidate.evaluate_js("delete candidateTargetRow.dataset.candidateSaving; null")
                    window.evaluate_js("document.querySelector('#rows [data-action=\\\"open-candidates\\\"]').click(); null")
                    deadline = time.monotonic()+15
                    while not candidate.evaluate_js("!!document.querySelector('#candidateRows [data-action=\\\"choose-candidate\\\"]')"):
                        if time.monotonic()>deadline:
                            raise RuntimeError('Mock candidate did not render')
                        time.sleep(0.1)
                    candidate.evaluate_js("document.querySelector('#candidateRows [data-action=\\\"choose-candidate\\\"]').click(); null")
                    while lifecycle.candidates._request is not None or not window.evaluate_js("document.querySelector('#rows').textContent.includes('C123456')"):
                        if time.monotonic()>deadline:
                            raise RuntimeError('Child selection did not update parent rows')
                        time.sleep(0.1)
                    window.evaluate_js("document.querySelector('#rows [data-action=\\\"open-candidates\\\"]').click(); null")
                    while lifecycle.candidates._request is None:
                        if time.monotonic()>deadline:
                            raise RuntimeError('Child did not reopen after saving')
                        time.sleep(0.1)
                    # Native close defers to JS and retains the host; exit disposes the child.
                    candidate.destroy()
                    print('candidate: close requested',flush=True)
                    while lifecycle.candidates._request is not None:
                        if time.monotonic()>deadline:
                            raise RuntimeError('Child native close did not hide the tool window')
                        time.sleep(0.1)
                    window.evaluate_js("document.getElementById('exitWorkbench').click()")
                    print('candidate: exit requested',flush=True)
                elif phase=='uninitialized':
                    window.evaluate_js("delete window.requestDesktopExit; document.querySelector('#rows').replaceChildren(); null")
                    window.destroy()
                elif phase=='close':
                    window.evaluate_js("document.querySelector('#openPluginUpdates').click(); null")
                    while not window.evaluate_js("document.querySelector('#pluginVersion').textContent.includes('0.1.5')"):
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
    parser.add_argument('--only-candidate',action='store_true')
    args = parser.parse_args()
    if args.child:
        child(args.child,args.phase)
        return
    with temporary_project(prefix='native-desktop-lifecycle-') as directory:
        root = Path(directory)
        (root / 'sample.kicad_pro').write_text('{}')
        schematic = '(kicad_sch (symbol (property "Reference" "C1") (property "Comment" "capacitor") (property "Value" "100nF") (property "Footprint" "Library:0603")))'
        (root / 'sample.kicad_sch').write_text(schematic)
        (root / 'settings.json').write_text(json.dumps({'schematic_dir':'.','source_bom':'__schematic__',
                                                      'open_lcsc_search_on_replace':False}))
        flags = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
        env = {**os.environ,'PYTHONDONTWRITEBYTECODE':'1',
               'KICAD_PART_SELECTION_HOME':str(root.parent / 'desktop'), 'PYTHONIOENCODING':'utf-8'}
        for phase in (('candidate',) if args.only_candidate else ('close','restart','uninitialized','candidate')):
            try:
                result = subprocess.run([str(args.python),'-B',str(Path(__file__).resolve()),'--child',str(root),'--phase',phase],
                    capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90,env=env,**flags)
            except subprocess.TimeoutExpired as exc:
                raise AssertionError(f'Native smoke timed out: {exc.stdout!r}\n{exc.stderr!r}') from exc
            if result.returncode:
                raise AssertionError(result.stdout+'\n'+result.stderr)
            assert (root / (phase+'.ok')).exists()
        if not args.only_candidate:
            assert (root / 'final.ok').exists(), (root / 'restarted.log').read_text(errors='replace')
            records = json.loads((root / 'BOM/part_selection_overrides.json').read_text(encoding='utf-8'))
            assert records['C1']['field_overrides']['value']=='220nF'
        assert (root / 'sample.kicad_sch').read_text()==schematic
        checks = {'real_webview2':'passed','native_candidate_move_restore_close':'passed',
                  'native_candidate_save_refresh':'passed','busy_selection_exit_protection':'passed',
                  'production_cad_written':False}
        if not args.only_candidate:
            checks.update(native_close_autosave='passed',port_release_and_reopen='passed',
                          restart_and_exit_menu='passed',uninitialized_window_close='passed')
        print(json.dumps(checks))


if __name__=='__main__':
    main()
