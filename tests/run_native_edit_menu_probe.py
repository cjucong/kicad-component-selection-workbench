"""Opt-in native selection/context-menu probe; no project data is loaded."""
import argparse
import json
import sys
import time
from pathlib import Path

from kicad_plugin.desktop import enable_browser_shortcuts


def run(report):
    import webview
    result = {'checks': {}, 'passed': False}
    window = webview.create_window('Workbench edit-menu QA', html='''<!doctype html>
        <html><body style="font:20px sans-serif;padding:30px">
        <p id="sample">Workbench selected text QA</p>
        <input id="editor" value="Editable text QA" style="font:20px sans-serif;width:400px">
        <input id="destination" value="" style="font:20px sans-serif;width:400px">
        <dialog><p id="dialogText">Modal selected text QA</p></dialog>
        </body></html>''', text_select=True, width=700, height=400)

    def worker():
        original = []
        try:
            window.events.loaded.wait(20)
            enable_browser_shortcuts(window)
            from System import Action
            from System.Windows.Forms import Clipboard

            def wait(expression):
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    if window.evaluate_js(expression) is True:
                        return
                    time.sleep(.05)
                raise AssertionError(expression)

            def click(command):
                def select():
                    popup = window._workbench_edit_popup
                    item = popup.Items[['undo','redo','cut','copy','paste','selectAll'].index(command)]
                    assert item.Enabled, command
                    item.PerformClick()
                    popup.Close()
                window.native.Invoke(Action(select))

            def menu_visible():
                values = []
                window.native.Invoke(Action(lambda: values.append(bool(
                    getattr(window, '_workbench_edit_popup', None) is not None and
                    window._workbench_edit_popup.Visible))))
                return values[0]

            def context(selector):
                point = window.evaluate_js('JSON.stringify((()=>{const r=document.querySelector('
                    + json.dumps(selector)+').getBoundingClientRect();return {x:r.x+20,y:r.y+10}})())')
                point = json.loads(point)
                tasks = []
                def send():
                    core = window.native.webview.CoreWebView2
                    for kind in ('mousePressed','mouseReleased'):
                        tasks.append(core.CallDevToolsProtocolMethodAsync('Input.dispatchMouseEvent', json.dumps({
                            'type':kind,'button':'right','buttons':2 if kind=='mousePressed' else 0,
                            'clickCount':1, **point})))
                window.native.Invoke(Action(send))
                for task in tasks:
                    assert task.Wait(5000) and not task.IsFaulted
                end = time.monotonic() + 10
                while time.monotonic() < end and not menu_visible():
                    time.sleep(.05)
                assert menu_visible(), 'Native editing menu did not open'

            wait('window.workbenchEditMenu === true && typeof window.pywebview.api.workbench_edit === "function"')
            window.evaluate_js('''
                const r=document.createRange(); r.selectNodeContents(document.querySelector('#sample'));
                getSelection().removeAllRanges(); getSelection().addRange(r); true''')
            context('#sample')
            result['checks']['selected_text_right_click_menu'] = True
            def capture_menu():
                from System.Drawing import Bitmap, Rectangle
                from System.Drawing.Imaging import ImageFormat
                popup = window._workbench_edit_popup
                bitmap = Bitmap(popup.Width, popup.Height)
                try:
                    popup.DrawToBitmap(bitmap, Rectangle(0,0,popup.Width,popup.Height))
                    bitmap.Save(str(report.with_suffix('.png')), ImageFormat.Png)
                finally:
                    bitmap.Dispose()
            report.parent.mkdir(parents=True, exist_ok=True)
            window.native.Invoke(Action(capture_menu))
            window.evaluate_js('document.addEventListener("copy", e=>{window.nativeCopyEvent=true;e.preventDefault();},{once:true}); true')
            click('copy')
            wait('window.nativeCopyEvent === true')
            result['checks']['native_copy_command_event'] = True
            window.evaluate_js('''document.querySelector('dialog').showModal();
                const r=document.createRange();r.selectNodeContents(document.querySelector('#dialogText'));
                getSelection().removeAllRanges();getSelection().addRange(r);
                document.addEventListener('copy',e=>{window.modalCopyEvent=true;e.preventDefault()},{once:true});true''')
            context('#dialogText'); click('copy')
            wait('window.modalCopyEvent === true')
            assert window.evaluate_js('document.querySelector("dialog").open') is True
            window.evaluate_js('document.querySelector("dialog").close();true')
            result['checks']['native_modal_menu_item_click'] = True
            window.evaluate_js('window.pywebview.api.workbench_edit("paste").then(r=>window.directCommandRejected=r.ok===false);true')
            wait('window.directCommandRejected === true')
            result['checks']['direct_clipboard_command_rejected'] = True
            try:
                window.native.Invoke(Action(lambda: original.append(Clipboard.GetDataObject())))
            except Exception:
                result['clipboard'] = 'OS clipboard unavailable; copy/paste content not tested'
                window.evaluate_js('const el=document.querySelector("#editor");el.focus();el.select();true')
                tasks = []
                window.native.Invoke(Action(lambda: tasks.append(window.native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(
                    'Input.insertText', json.dumps({'text':'Changed QA'})))))
                assert tasks[0].Wait(5000) and not tasks[0].IsFaulted
                wait('document.querySelector("#editor").value === "Changed QA"')
                context('#editor'); click('undo')
                wait('document.querySelector("#editor").value === "Editable text QA"')
                result['checks']['native_undo'] = True
                context('#editor'); click('redo')
                wait('document.querySelector("#editor").value === "Changed QA"')
                result['checks']['native_redo'] = True
                context('#editor'); click('selectAll')
                wait('document.querySelector("#editor").selectionEnd - document.querySelector("#editor").selectionStart === 10')
                result['checks']['native_select_all'] = True
                result['passed'] = True
                return
            window.evaluate_js('const r=document.createRange();r.selectNodeContents(document.querySelector("#sample"));getSelection().removeAllRanges();getSelection().addRange(r);true')
            context('#sample'); click('copy')
            window.evaluate_js('getSelection().removeAllRanges(); document.querySelector("#destination").focus(); true')
            context('#destination'); click('paste')
            wait('document.querySelector("#destination").value === "Workbench selected text QA"')
            result['checks']['native_copy_and_paste'] = True
            window.evaluate_js('const el=document.querySelector("#destination"); el.focus(); el.select(); true')
            context('#destination'); click('cut')
            wait('document.querySelector("#destination").value === ""')
            result['checks']['native_cut'] = True
            context('#destination'); click('undo')
            wait('document.querySelector("#destination").value === "Workbench selected text QA"')
            result['checks']['native_undo'] = True
            context('#destination'); click('redo')
            wait('document.querySelector("#destination").value === ""')
            result['checks']['native_redo'] = True
            context('#editor'); click('selectAll')
            wait('document.querySelector("#editor").selectionEnd - document.querySelector("#editor").selectionStart === 16')
            result['checks']['native_select_all'] = True
            result['passed'] = True
        except Exception as exc:
            result['error'] = repr(exc)
        finally:
            if original:
                from System import Action
                from System.Windows.Forms import Clipboard
                def restore():
                    if Clipboard.ContainsText() and Clipboard.GetText() == 'Workbench selected text QA':
                        if original[0] is None:
                            Clipboard.Clear()
                        else:
                            Clipboard.SetDataObject(original[0], True)
                window.native.Invoke(Action(restore))
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(result, indent=2), encoding='utf-8')
            window.destroy()
    webview.start(worker, gui='edgechromium')
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    sys.exit(run(parser.parse_args().report))
