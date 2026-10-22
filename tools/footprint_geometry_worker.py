"""JSON stdin/stdout native pcbnew helper. Run with KiCad's bundled Python."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from part_selection_footprint_geometry import _outside_project, _validate_supported


def _load(pcbnew, path, *, allow_models=False):
    source = Path(path).read_text(encoding='utf-8-sig')
    _validate_supported(source, allow_models=allow_models)
    item = pcbnew.PCB_IO_KICAD_SEXPR().Parse(source)
    footprint = item.Cast() if item else None
    if footprint is None or not isinstance(footprint,pcbnew.FOOTPRINT):
        raise ValueError('KiCad could not read a footprint')
    if footprint.Models().size() and not allow_models:
        raise ValueError('Unsupported footprint object: 3D model')
    return footprint


def _items(footprint):
    return (list(footprint.Pads()) + list(footprint.GraphicalItems())
            + list(footprint.GetFields()) + list(footprint.Zones()))


def _inventory(footprint):
    return (len(list(footprint.Pads())), len(list(footprint.GraphicalItems())),
            len(list(footprint.GetFields())),len(list(footprint.Zones())))


def _align(pcbnew, payload):
    output = Path(payload['output'])
    _outside_project(output)
    if output.resolve() == Path(payload['converted']).resolve():
        raise ValueError('Output must not overwrite the input footprint')
    footprint = _load(pcbnew,payload['converted'])
    items = _items(footprint)
    for item in items:
        if item.GetClass() not in {'PAD','PCB_SHAPE','PCB_TEXT','PCB_TEXTBOX','PCB_FIELD','ZONE'}:
            raise ValueError(f'Unsupported native footprint object: {item.GetClass()}')
    inventory = _inventory(footprint)
    angle = float(payload['rotation_deg'])
    dx,dy = map(float,payload['offset_mm'])
    if not all(map(math.isfinite,(angle,dx,dy))):
        raise ValueError('Nonfinite native transform')
    origin = pcbnew.VECTOR2I(0,0)
    move = pcbnew.VECTOR2I(pcbnew.FromMM(dx),pcbnew.FromMM(dy))
    for item in items:
        item.Rotate(origin,pcbnew.EDA_ANGLE(angle,pcbnew.DEGREES_T))
        item.Move(move)
    # Transform children, not the footprint root: saving must keep library
    # origin (0,0) and orientation zero without undoing the child transforms.
    if footprint.GetPosition() != origin or abs(footprint.GetOrientationDegrees()) > 1e-9:
        raise ValueError('Native transform changed the library origin/orientation')
    name = footprint.GetFPID().GetLibItemName().c_str()
    if not name or any(char in name for char in '/\\:') or name in ('.','..'):
        raise ValueError('Unsupported library footprint name')
    output.parent.mkdir(parents=True,exist_ok=True)
    # A single shallow .pretty directory avoids deep Windows staging paths.
    with tempfile.TemporaryDirectory(prefix='g',suffix='.pretty',dir=output.parent) as directory:
        pcbnew.PCB_IO_KICAD_SEXPR().FootprintSave(directory,footprint)
        staged = Path(directory)/(name+'.kicad_mod')
        saved = _load(pcbnew,staged)
        if _inventory(saved) != inventory:
            raise ValueError('Native save dropped or changed footprint objects')
        if saved.GetPosition() != origin or abs(saved.GetOrientationDegrees()) > 1e-9:
            raise ValueError('Native save changed the library origin/orientation')
        os.replace(staged,output)
    return {'ok': True}


def _xy(pcbnew, vector):
    return [pcbnew.ToMM(vector.x),pcbnew.ToMM(vector.y)]


def _pad_one(pcbnew, footprint):
    pads = [pad for pad in footprint.Pads() if pad.GetNumber() == '1']
    return _xy(pcbnew,pads[0].GetPosition()) if len(pads) == 1 else None


def _normalize_svg(source, view_box, color):
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring(source,parser=parser)
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    root.set('viewBox',' '.join(f'{v:.6f}' for v in view_box))
    root.set('width',f'{view_box[2]:.6f}mm')
    root.set('height',f'{view_box[3]:.6f}mm')
    allowed = {'svg','g','path','circle','ellipse','rect','polygon','polyline','line',
               'text','tspan','title','desc','defs','clipPath'}
    for element in root.iter():
        if element.tag is ET.Comment:
            continue
        if element.tag.rsplit('}',1)[-1] not in allowed:
            raise ValueError('Unsupported object in native SVG')
        for key,value in list(element.attrib.items()):
            if key.lower().startswith('on') or key.rsplit('}',1)[-1] in ('href','src'):
                raise ValueError('Executable or external content in native SVG')
            if key in ('fill','stroke') and value != 'none':
                element.set(key,color)
            elif key == 'style':
                styles = []
                for declaration in value.split(';'):
                    if not declaration.strip():
                        continue
                    property_name, property_value = declaration.split(':',1)
                    property_name, property_value = property_name.strip(),property_value.strip()
                    if property_name in ('fill','stroke') and property_value != 'none':
                        property_value = color
                    if 'url(' in property_value.lower() or 'background' in property_name.lower():
                        raise ValueError('Unsupported resource or background in native SVG')
                    styles.append(f'{property_name}:{property_value}')
                element.set('style',';'.join(styles))
    return ET.tostring(root,encoding='unicode')


def _plot(pcbnew, source, directory, layer_id, layers, view_box, color):
    footprint = _load(pcbnew,source,allow_models=True)
    label = layer_id if layer_id in ('reference','value') else None
    if label:
        for item in list(footprint.Pads()) + list(footprint.GraphicalItems()) + list(footprint.Zones()):
            footprint.Delete(item)
        for field in footprint.GetFields():
            field.SetVisible(False)
        selected = footprint.Reference() if label == 'reference' else footprint.Value()
        selected.SetVisible(True)
        layers = [selected.GetLayer()]
    else:
        footprint.Reference().SetVisible(False)
        footprint.Value().SetVisible(False)
    board = pcbnew.BOARD()
    board.SetFileName(str(directory/'p.kicad_pcb'))
    board.Add(footprint)
    controller = pcbnew.PLOT_CONTROLLER(board)
    options = controller.GetPlotOptions()
    options.SetOutputDirectory(str(directory))
    options.SetPlotFrameRef(False)
    options.SetAutoScale(False)
    options.SetScale(1.0)
    options.SetMirror(False)
    options.SetNegative(False)
    options.SetUseAuxOrigin(False)
    options.SetSvgFitPageToBoard(False)
    options.SetSvgPrecision(6)
    options.SetPlotReference(label == 'reference')
    options.SetPlotValue(label == 'value')
    options.SetPlotFPText(True)
    options.SetPlotPadNumbers(False)
    options.SetTextMode(pcbnew.PLOT_TEXT_MODE_STROKE)
    controller.SetLayer(layers[0])
    try:
        if not controller.OpenPlotfile('v',pcbnew.PLOT_FORMAT_SVG,''):
            raise ValueError('KiCad could not open the SVG plot')
        sequence = pcbnew.LSEQ()
        for layer in layers:
            sequence.push_back(layer)
        if not controller.PlotLayers(sequence):
            raise ValueError('KiCad could not plot the requested layers')
    finally:
        controller.ClosePlot()
    return _normalize_svg(Path(controller.GetPlotFileName()).read_text(encoding='utf-8'),view_box,color)


def _render(pcbnew,payload):
    work = Path(payload['work'])
    _outside_project(work)
    existing, converted = (_load(pcbnew,payload[key],allow_models=True)
                           for key in ('reference','converted'))
    boxes = [footprint.GetBoundingBox(True,True) for footprint in (existing,converted)]
    left = min(0,*[pcbnew.ToMM(box.GetX()) for box in boxes])-0.5
    top = min(0,*[pcbnew.ToMM(box.GetY()) for box in boxes])-0.5
    right = max(0,*[pcbnew.ToMM(box.GetRight()) for box in boxes])+0.5
    bottom = max(0,*[pcbnew.ToMM(box.GetBottom()) for box in boxes])+0.5
    view_box = [round(left,6),round(top,6),round(right-left,6),round(bottom-top,6)]
    specifications = [
        ('copper','Copper',True,[pcbnew.F_Cu,pcbnew.B_Cu]),
        ('F.SilkS','F.SilkS',True,[pcbnew.F_SilkS]),
        ('B.SilkS','B.SilkS',True,[pcbnew.B_SilkS]),
        ('reference','Reference',False,[]), ('value','Value',False,[]),
        ('F.Fab','F.Fab',False,[pcbnew.F_Fab]),
        ('F.CrtYd','F.CrtYd',False,[pcbnew.F_CrtYd]),
    ]
    result = {'ok': True, 'view_box': view_box, 'origin_mm': [0,0],
              'layers': [{'id':id,'label':label,'default_visible':visible}
                         for id,label,visible,_ in specifications],
              'existing_pad_one': _pad_one(pcbnew,existing),
              'converted_pad_one': _pad_one(pcbnew,converted),
              'existing_layers': {}, 'converted_layers': {}}
    work.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='g',dir=work) as directory:
        directory = Path(directory)
        for key,path,color in [('existing_layers',payload['reference'],'#16806a'),
                               ('converted_layers',payload['converted'],'#c34078')]:
            for id,_,_,layers in specifications:
                result[key][id] = _plot(pcbnew,path,directory,id,layers,view_box,color)
    return result


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        import pcbnew

        payload = json.loads(sys.stdin.read(1024*1024))
        operation = payload.get('operation')
        if operation == 'align':
            result = _align(pcbnew,payload)
        elif operation == 'render':
            result = _render(pcbnew,payload)
        else:
            raise ValueError('Unknown geometry worker operation')
    except Exception as exc:
        result = {'ok': False,'reason': f'{type(exc).__name__}: {exc}'}
    print(json.dumps(result,ensure_ascii=True))


if __name__ == '__main__':
    main()
