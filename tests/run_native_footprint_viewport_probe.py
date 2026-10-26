"""Opt-in WebView2 preview-only QA; synthetic layers, no CAD or network writes."""
import argparse
import json
from pathlib import Path
import traceback

from run_native_tool_windows_probe import capture, wait

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
const svg = color=>`<svg xmlns="http://www.w3.org/2000/svg" viewBox="-5 -2.5 10 5"><rect x="-2" y="-1" width="1" height="2" fill="${color}"/></svg>`;
const detail={view_box:[-5,-2.5,10,5],layers:[{id:'F.Cu',label:'铜层',default_visible:true}],
 existing_layers:{'F.Cu':svg('#159b52')},converted_layers:{'F.Cu':svg('#d52798')},
 existing_pads:[{number:'1',position_mm:[-1.5,0]},{number:'2',position_mm:[1.5,0]}],
 converted_pads:[{number:'1',position_mm:[-1.5,0]},{number:'2',position_mm:[1.5,0]}],
 origin_mm:[0,0],alignment:{ok:true,rotation_deg:0,offset_mm:[0,0],max_error_mm:0}};
window.fetch=async url=>({ok:true,json:async()=>({ok:true,detail:url==='/api/library_footprint'?{compatible:true}:detail})});
const preview={preview_id:'viewport-only',c_code:'C123',compatible:true,original_ids:['Local:A'],
 suggested_name:'Preview',default_library:'Local',libraries:['Local'],existing:[{id:'Local:A'}],
 default_id:'Local:A',items:[{id:'Local:A',library:'Local'}]};
window.openProbe=source=>{
 if(source==='library') void chooseLibraryFootprint({...preview,kind:'library'});
 else void chooseFootprint(preview,{loadComparison:async()=>detail});
};
window.probeReady=true;
'''


def run(report):
    import webview
    assets = ROOT/'tools/web'
    html = '<!doctype html><html><meta charset="utf-8"><body>' + (assets/'toolbar-icons.svg').read_text(encoding='utf-8')
    html += '<style>' + (assets/'footprint-compare.css').read_text(encoding='utf-8') + '</style>'
    for name in ('footprint-adjust.js', 'footprint-compare.js'):
        html += '<script>' + (assets/name).read_text(encoding='utf-8') + '</script>'
    html += '<script>' + FIXTURE + '</script></body></html>'
    window = webview.create_window('Footprint viewport QA (synthetic)', html=html, width=1150, height=900)
    result = {'passed': False, 'checks': []}

    def checks():
        try:
            wait(window, 'window.probeReady === true')
            for source in ('library', 'converted'):
                window.evaluate_js(f'openProbe({json.dumps(source)}); true')
                wait(window, '!!document.querySelector(".fp-scene:not([hidden])")')
                assert window.evaluate_js(r'''
                const q=s=>document.querySelector(s), v=q('.fp-viewport');
                const before=q('[data-fp-dx]').value, r=v.getBoundingClientRect();
                q('[data-fp-zoom=in]').click();
                if(q('[data-fp-zoom-value]').value!=='125%') throw Error('Native button zoom failed');
                v.dispatchEvent(new WheelEvent('wheel',{deltaY:-200,clientX:r.x+r.width/2,clientY:r.y+r.height/2,cancelable:true}));
                if(parseInt(q('[data-fp-zoom-value]').value)<=125) throw Error('Native wheel handler failed');
                q('[data-fp-wheel]').value='pan';
                const old=q('.fp-scene').getBoundingClientRect().y;
                v.dispatchEvent(new WheelEvent('wheel',{deltaY:35,cancelable:true}));
                if(Math.abs(q('.fp-scene').getBoundingClientRect().y-old+35)>1) throw Error('Native pan failed');
                q('[data-fp-expand]').click();
                if(!q('.fp-expanded')||q('.fp-viewport').getBoundingClientRect().height<300) throw Error('Native expansion failed');
                q('[data-fp-zoom=in]').click();
                if(q('[data-fp-dx]').value!==before) throw Error('Camera changed geometry');
                true
                ''')
                capture(window, report/f'viewport-{source}.png')
                window.evaluate_js('window.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true,cancelable:true})); true')
                wait(window, '!document.querySelector(".fp-expanded") && !!document.querySelector(".fp-overlay")')
                window.evaluate_js('document.querySelector("[data-fp-cancel]").click(); true')
                wait(window, '!document.querySelector(".fp-overlay")')
                result['checks'].append(source)
            result['passed'] = True
        except Exception:
            result['error'] = traceback.format_exc()
        finally:
            (report/'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            window.destroy()

    webview.start(checks, gui='edgechromium', debug=False)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    report = parser.parse_args().report_dir.resolve()
    if report.is_relative_to(ROOT):
        parser.error('Reports must be outside the repository')
    report.mkdir(parents=True, exist_ok=True)
    outcome = run(report)
    print(json.dumps(outcome, indent=2))
    raise SystemExit(0 if outcome['passed'] else 1)
