"""Native alignment through library publication, using disposable CAD only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from part_selection_footprints import FootprintChoices
from part_selection_footprint_geometry import _find_kicad_python, estimate_alignment
from part_selection_runtime import runtime_directory


ROOT = Path(__file__).resolve().parents[1]


def module(name, points, width):
    return f'''(footprint "{name}" (version 20241229) (generator "pcbnew") (layer "F.Cu")
      (property "Reference" "REF**" (at 0 -4 0) (layer "F.SilkS")
        (effects (font (size 1 1) (thickness 0.15))))
      (property "Value" "Fixture" (at 0 4 0) (layer "F.Fab")
        (effects (font (size 1 1) (thickness 0.15))))
      {''.join(f'(pad "{n}" smd rect (at {x} {y}) (size {width} 0.5) (layers "F.Cu" "F.Paste" "F.Mask"))' for n,x,y in points)}
      (fp_line (start -3 -2) (end 3 -2) (stroke (width 0.15) (type default)) (layer "F.SilkS")))'''


@pytest.fixture
def native_preview(scratch_project, monkeypatch, request):
    if _find_kicad_python() is None:
        pytest.skip('Bundled KiCad Python is required for native integration')
    root = Path(scratch_project)
    monkeypatch.setenv('KICAD_PART_SELECTION_CACHE', str(root.parent/'r'))
    pretty = root/'Libraries/Local.pretty'
    pretty.mkdir(parents=True)
    points = [('1', -2, -1), ('2', 2, -1), ('3', 1, 2)]
    original = pretty/'Original.kicad_mod'
    original.write_text(module('Original', [(n,y+4,-x-3) for n,x,y in points], 0.6), encoding='utf-8')
    (root/'fp-lib-table').write_text('(fp_lib_table (lib (name "Local") (type "KiCad") '
        '(uri "${KIPRJMOD}/Libraries/Local.pretty")))', encoding='utf-8')
    for name, source in [('fixture.kicad_sch', '(kicad_sch (version 20250114))'),
                         ('fixture.kicad_pcb', '(kicad_pcb (version 20241229))')]:
        (root/name).write_text(source, encoding='utf-8')
    service = FootprintChoices(root)

    def convert(code, progress=None):
        folder = service.staging_root/'Libraries/DSMU/LCSC'/code
        converted = folder/f'LCSC_{code}.pretty/Converted.kicad_mod'
        converted.parent.mkdir(parents=True)
        shift = getattr(request, 'param', 0)
        converted_points = [(n,x+(shift if n=='3' else 0),y) for n,x,y in points]
        converted.write_text(module('Converted', converted_points, 0.8), encoding='utf-8')
        (folder/f'LCSC_{code}.kicad_sym').write_text('(kicad_symbol_lib (version 20241209) '
            f'(generator test) (symbol "Part" (property "Footprint" "LCSC_{code}:Converted")))', encoding='utf-8')
        return {'footprint': f'LCSC_{code}:Converted', 'symbol': f'LCSC_{code}:Part', 'c_code': code}

    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with patch('part_selection_footprints.LibraryImporter.convert', side_effect=convert):
        preview = service.prepare('C123', ['Local:Original'], 'Original')
    detail = service.comparison(preview['preview_id'], 'Local:Original')
    yield root, service, preview, detail, original, before


def test_native_preview_and_published_geometry_match_original_pad_coordinates(native_preview):
    root, service, preview, detail, original, before = native_preview
    assert detail['alignment']['ok'], detail
    assert detail['alignment']['rotation_deg'] == pytest.approx(90)
    assert detail['alignment']['offset_mm'] == pytest.approx([4, -3])
    assert detail['existing_pad_one'] == detail['converted_pad_one'] == [3, -1]
    result = service.commit({'preview_id': preview['preview_id'], 'source': 'converted',
        'existing_id': 'Local:Original', 'target_mode': 'existing', 'target_library': 'Local',
        'name': 'Original', 'acknowledged': True})
    assert result['footprint'] == 'Local:Original_C123'
    published = root/result['target_path']/'Original_C123.kicad_mod'
    check = estimate_alignment(published.read_text(encoding='utf-8'), original.read_text(encoding='utf-8'))
    assert check['ok'] and check['max_error_mm'] < 1e-6
    assert check['rotation_deg'] == pytest.approx(0, abs=1e-6)
    assert check['offset_mm'] == pytest.approx([0, 0], abs=1e-6)
    for path, source in before.items():
        if path.name != 'fp-lib-table':
            assert path.read_bytes() == source, f'Existing design file changed: {path.name}'


@pytest.mark.parametrize('native_preview', [.2], indirect=True)
def test_centered_pitch_difference_can_be_reviewed_and_published_without_changing_design(native_preview):
    root, service, preview, detail, original, before = native_preview
    alignment = detail['alignment']
    assert alignment['ok'] and alignment['method'] == 'center' and alignment['requires_review']
    assert alignment['max_error_mm'] > .025
    assert service.comparison(preview['preview_id'], 'Local:Original') == detail
    result = service.commit({'preview_id': preview['preview_id'], 'source': 'converted',
        'existing_id': 'Local:Original', 'target_mode': 'existing', 'target_library': 'Local',
        'name': 'Centered', 'acknowledged': True})
    published = root/result['target_path']/'Centered.kicad_mod'
    check = estimate_alignment(published.read_text(encoding='utf-8'),
                               original.read_text(encoding='utf-8'), method='center')
    assert check['ok'] and check['offset_mm'] == pytest.approx([0,0],abs=3e-6)
    assert check['rotation_deg'] == pytest.approx(0,abs=1e-4)
    for path, source in before.items():
        if path.name != 'fp-lib-table':
            assert path.read_bytes() == source


@pytest.mark.parametrize('back,angle', [(False,0),(False,37),(True,90)])
def test_pcb_instance_is_reference_even_after_schematic_library_has_changed(native_preview, back, angle):
    root, service, _, _, original, _ = native_preview
    board = root/'fixture.kicad_pcb'
    script = '''import pcbnew,sys
b=pcbnew.BOARD()
f=pcbnew.PCB_IO_KICAD_SEXPR().Parse(open(sys.argv[1],encoding='utf-8').read()).Cast()
b.Add(f)
f.SetReference('U3')
f.SetFPID(pcbnew.LIB_ID('Local','Original'))
if sys.argv[3]=='True': f.Flip(pcbnew.VECTOR2I(0,0),False)
f.SetOrientationDegrees(float(sys.argv[4]))
f.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(90.5),pcbnew.FromMM(96.735)))
pcbnew.SaveBoard(sys.argv[2],b)
'''
    subprocess.run([str(_find_kicad_python()), '-B', '-c', script, str(original), str(board), str(back), str(angle)],
                   check=True, capture_output=True, timeout=30,
                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    unchanged = board.read_bytes()
    # The schematic already names the unaligned converted library. It must
    # never replace the PCB's embedded original geometry as the reference.
    (original.parent/'Changed.kicad_mod').write_text(
        module('Changed', [('1',-2,-1),('2',2,-1),('3',1,2)],.8),encoding='utf-8')
    with patch('part_selection_footprints.LibraryImporter.convert') as convert:
        def staged(code, progress=None):
            folder=service.staging_root/'Libraries/DSMU/LCSC'/code
            pretty=folder/f'LCSC_{code}.pretty'
            pretty.mkdir(parents=True)
            (pretty/'Converted.kicad_mod').write_bytes((original.parent/'Changed.kicad_mod').read_bytes())
            (folder/f'LCSC_{code}.kicad_sym').write_text('(kicad_symbol_lib (version 20241209) '
                f'(generator test) (symbol "Part" (property "Footprint" "LCSC_{code}:Converted")))')
            return {'footprint':f'LCSC_{code}:Converted','symbol':f'LCSC_{code}:Part','c_code':code}
        convert.side_effect=staged
        preview=service.prepare('C234',['Local:Changed'],refs=['U3'])
    reference=preview['default_reference_id']
    assert reference=='PCB:U3'
    assert 'Local:Original' in preview['alignment_ids']
    detail=service.comparison(preview['preview_id'],reference)
    assert detail['alignment']['rotation_deg']==pytest.approx(90,abs=1e-4)
    assert detail['alignment']['offset_mm']==pytest.approx([4,-3],abs=3e-6)
    choice={'preview_id':preview['preview_id'],'source':'converted',
        'existing_id':reference,'target_mode':'existing','target_library':'Local',
        'name':'FromBoard','acknowledged':True}
    board.write_bytes(unchanged+b'\n')
    with pytest.raises(ValueError,match='changed|变化'):
        service.commit(choice)
    assert not (original.parent/'FromBoard.kicad_mod').exists()
    board.write_bytes(unchanged)
    result=service.commit(choice)
    published=root/result['target_path']/'FromBoard.kicad_mod'
    check=estimate_alignment(published.read_text(),original.read_text())
    assert check['ok'] and check['max_error_mm']<3e-6
    assert board.read_bytes()==unchanged
    assert result['alignment_reference']['source']=='pcb'
    assert result['alignment_reference']['refs']==['U3']
    with patch('part_selection_footprints.LibraryImporter.convert') as convert:
        renamed = service.prepare_rename(['Local:Changed'], 'Renamed', code='C234', refs=['U3'])
    convert.assert_not_called()
    assert renamed['default_id'] == 'PCB:U3'
    service.library_detail(renamed['preview_id'], renamed['default_id'])
    result = service.commit({'preview_id':renamed['preview_id'], 'source':'rename',
        'existing_id':renamed['default_id'], 'target_mode':'existing', 'target_library':'Local',
        'name':'Renamed', 'acknowledged':True})
    published = root/result['target_path']/'Renamed.kicad_mod'
    check = estimate_alignment(published.read_text(), original.read_text())
    assert check['ok'] and check['max_error_mm'] < 3e-6
    assert check['offset_mm'] == pytest.approx([0,0], abs=3e-6)
    assert check['rotation_deg'] == pytest.approx(0, abs=1e-4)
    assert board.read_bytes() == unchanged
    with pytest.raises(ValueError, match='部分位号|拆行'):
        service.prepare_rename(['Local:Changed'], 'Partial', code='C234', refs=['U3','U4'])
    # A renamed or deleted schematic library entry is not required when the
    # saved board still provides the actual geometry to preserve.
    recovered = service.prepare_rename(['Missing:Absent'], 'Recovered', code='C234', refs=['U3'])
    assert recovered['default_id'] == 'PCB:U3'
    script = '''import pcbnew,sys
b=pcbnew.LoadBoard(sys.argv[1])
old=next(f for f in b.GetFootprints() if f.GetReference()=='U3')
new=pcbnew.PCB_IO_KICAD_SEXPR().Parse(open(sys.argv[2],encoding='utf-8').read()).Cast()
b.Add(new)
if old.IsFlipped(): new.Flip(pcbnew.VECTOR2I(0,0),False)
new.SetOrientationDegrees(old.GetOrientationDegrees())
new.SetPosition(old.GetPosition())
points={p.GetNumber():p.GetPosition() for p in old.Pads()}
for p in new.Pads():
    delta=p.GetPosition()-points[p.GetNumber()]
    assert abs(delta.x)<=3 and abs(delta.y)<=3, (p.GetNumber(),delta)
assert new.IsFlipped()==old.IsFlipped()
assert new.GetOrientationDegrees()==old.GetOrientationDegrees()
'''
    # Place the published library at the same instance transform using KiCad
    # itself, checking world coordinates rather than only preview alignment.
    for name in ('FromBoard','Renamed'):
        completed = subprocess.run([str(_find_kicad_python()),'-B','-c',script,str(board),str(original.parent/(name+'.kicad_mod'))],
                                   capture_output=True,timeout=30,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert completed.returncode == 0, completed.stderr.decode(errors='replace')
    assert board.read_bytes() == unchanged


def test_explicit_original_library_can_restore_a_previously_shifted_pcb(native_preview):
    root,service,preview,_,original,_=native_preview
    # An original library can be selected explicitly even if it is no longer
    # the schematic's current identifier; don't guess from name similarity.
    (original.parent/'Previous.kicad_mod').write_bytes(original.read_bytes())
    with patch('part_selection_footprints.LibraryImporter.convert') as convert:
        def staged(code,progress=None):
            folder=service.staging_root/'Libraries/DSMU/LCSC'/code
            pretty=folder/f'LCSC_{code}.pretty'; pretty.mkdir(parents=True)
            (pretty/'Converted.kicad_mod').write_text(module('Converted',[('1',-2,-1),('2',2,-1),('3',1,2)],.8))
            (folder/f'LCSC_{code}.kicad_sym').write_text('(kicad_symbol_lib (version 20241209) '
                f'(generator test) (symbol "Part" (property "Footprint" "LCSC_{code}:Converted")))')
            return {'footprint':f'LCSC_{code}:Converted','symbol':f'LCSC_{code}:Part','c_code':code}
        convert.side_effect=staged
        preview=service.prepare('C345',['Local:Original'])
    assert 'Local:Previous' in preview['alignment_ids']
    result=service.comparison(preview['preview_id'],'Local:Previous')
    assert result['alignment']['offset_mm']==pytest.approx([4,-3])


NATIVE_BROWSER = r'''
const {chromium} = require('playwright'), assert = require('node:assert/strict'), fs = require('node:fs');
(async()=>{
 const payload=JSON.parse(fs.readFileSync(0,'utf8')), browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>route.fulfill({body:'<!doctype html><html><body></body></html>',contentType:'text/html'}));
  await page.goto('http://native-fixture.test/');
  await page.addStyleTag({path:process.env.FP_CSS});
  await page.addScriptTag({path:process.env.FP_JS});
  await page.evaluate(({preview,detail})=>{
   window.nativeDetail=detail;
   window.fetch=async()=>({ok:true,json:async()=>({ok:true,detail})});
   window.chooseFootprint(preview);
  },payload);
  await page.waitForFunction(()=>document.querySelectorAll('[data-fp-layer-image]').length===14);
  await page.waitForFunction(()=>[...document.querySelectorAll('[data-fp-layer-image]')].every(e=>e.complete&&e.naturalWidth>0));
  const pixels=await page.evaluate(()=>{
   const d=window.nativeDetail, box=d.view_box;
   return ['existing','converted'].map(side=>{
    const img=document.querySelector(`[data-fp-layer-image][data-source="${side}"][data-layer="copper"]`);
    const canvas=document.createElement('canvas');canvas.width=1200;canvas.height=1200;
    const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0,1200,1200);
    const p=d[side+'_pad_one'];
    return [...ctx.getImageData(Math.round((p[0]-box[0])/box[2]*1200),Math.round((p[1]-box[1])/box[3]*1200),1,1).data];
   });
  });
  assert.ok(pixels.every(p=>p[3]>200),'Native pad-one centers must be opaque in both aligned copper layers');
  assert.deepEqual(pixels[0].slice(0,3),[22,128,106]);
  assert.deepEqual(pixels[1].slice(0,3),[195,64,120]);
  for(const width of [1440,390]) {
   await page.setViewportSize({width,height:1000});
   const bounds=await page.locator('.fp-dialog').boundingBox();
   assert.ok(bounds.x>=0 && bounds.x+bounds.width<=width+1);
   const stages=await page.locator('[data-fp-stage]').evaluateAll(es=>es.map(e=>({w:e.clientWidth,h:e.clientHeight})));
   assert.ok(stages.every(e=>e.h<=381),'Near-square previews must not push controls below an oversized canvas');
   assert.deepEqual(stages[0],stages[1],'Aligned layers must keep the same pixel frame');
   assert.equal(await page.locator('.fp-body').evaluate(e=>e.scrollWidth<=e.clientWidth+1),true);
   await page.screenshot({path:process.env.FP_SHOTS+`/native-overlay-${width}.png`});
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,pad_center_pixels:pixels,page_errors:errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def test_real_native_svg_pad_centers_are_aligned_in_chromium(native_preview):
    node = shutil.which('node')
    if not node or subprocess.run([node, '-e', "require.resolve('playwright')"], capture_output=True, timeout=15).returncode:
        pytest.skip('Node.js and Playwright are required for native SVG pixel checks')
    root, _, preview, detail, _, _ = native_preview
    with pytest.MonkeyPatch.context() as settings:
        settings.delenv('KICAD_PART_SELECTION_CACHE', raising=False)
        shots = runtime_directory(ROOT)/'footprint-overlay-smoke'
    shots.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'FP_JS': str(ROOT/'tools/web/footprint-compare.js'),
        'FP_CSS': str(ROOT/'tools/web/footprint-compare.css'), 'FP_SHOTS': str(shots)}
    result = subprocess.run([node, '-e', NATIVE_BROWSER], input=json.dumps({'preview': preview, 'detail': detail}),
        cwd=root, env=env, text=True, encoding='utf-8', capture_output=True, timeout=45,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr+result.stdout
    assert json.loads(result.stdout)['passed']
