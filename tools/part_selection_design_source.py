"""One immutable hierarchy snapshot for fields, instance identity and byte validation."""
from copy import deepcopy
import hashlib
from pathlib import Path

from kicad_sexpr import parse_sexpr
from part_selection_fields import normalize_field_mapping, resolve_properties
from part_selection_overrides import load_overrides


def content_hash(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class DesignSource:
    def __init__(self, app):
        self.app = app

    def _configuration(self):
        return {'root':str(Path(self.app.project_root).resolve()),
                'directory':str(Path(self.app.config['schematic_dir']).resolve()),
                'records':str(Path(self.app.overrides_path).resolve()),
                'mapping':normalize_field_mapping(self.app.config.get('field_mapping'))}

    @staticmethod
    def _files(config):
        root, directory = Path(config['root']), Path(config['directory'])
        return sorted(str(path.resolve()) for path in
                      set(root.glob('*.kicad_sch')) | set(directory.glob('*.kicad_sch')) |
                      set(root.glob('*.kicad_pro')))

    def capture(self, refs: list[str] | None = None, *, allow_legacy=False) -> dict:
        from part_selection_pcb import schematic_identity
        if refs is not None and (not refs or len(set(refs)) != len(refs)):
            raise ValueError('请选择不重复的位号')
        config = self._configuration()
        files = self._files(config)
        hashes = {path:content_hash(path) for path in files}
        for path in (Path(config['root']) / 'fp-lib-table', Path(config['records'])):
            hashes[str(path)] = content_hash(path)
        fields, properties, terminals, pages = {}, {}, {}, {}

        def read(path):
            if path in pages:
                return pages[path], hashes[str(path)]
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            previous = hashes.setdefault(str(path), digest)
            if previous != digest:
                raise ValueError('读取期间原理图已改变，请重新预检')
            pages[path] = parse_sexpr(content.decode('utf-8-sig'), 'kicad_sch')
            return pages[path], digest

        def visit(ref, symbol, path):
            from part_selection_pad_mapping import symbol_terminals
            pins = symbol_terminals(symbol, pages[path])
            previous = terminals.get(ref, [])
            terminals[ref] = sorted(set(previous) | set(pins)) if previous is not None and pins is not None else None
            props = {}
            for prop in symbol.children_named('property'):
                name = prop.arg()
                if name in props:
                    raise ValueError(ref + ' 存在重复原理图属性：' + name)
                props[name] = prop.arg(2)
            names = resolve_properties(props, config['mapping'])
            values = {key:props[name] for key, name in names.items() if name in props}
            if ref in fields and (fields[ref] != values or properties[ref] != names):
                raise ValueError(ref + ' 多单元或多实例的字段不一致，请先核对')
            fields[ref], properties[ref] = values, names

        # Only old single-page, identity-free fixtures may use property references.
        # This is opt-in for footprint editing, never for PCB association checks.
        legacy = None
        if allow_legacy and len(files) == 1 and Path(files[0]).suffix == '.kicad_sch':
            path = Path(files[0])
            page, _ = read(path)
            symbols = page.children_named('symbol')
            if (not page.child('uuid') and not page.children_named('sheet')
                    and all(not symbol.child('uuid') and not symbol.child('instances') for symbol in symbols)):
                legacy = {}
                for symbol in symbols:
                    prop = next((prop for prop in symbol.children_named('property') if prop.arg() == 'Reference'), None)
                    ref = prop.arg(2).strip().upper() if prop else ''
                    if ref and (refs is None or ref in refs):
                        visit(ref, symbol, path)
                        legacy[ref] = []
                if refs is not None and set(refs) != set(legacy):
                    raise ValueError('原理图缺少位号，请重新预检')
        identity = ({'paths':legacy, 'hashes':{str(path):hashes[str(path)]}}
                    if legacy is not None else
                    schematic_identity(Path(config['root']), Path(config['directory']), refs,
                                       read_page=read, visit_symbol=visit))
        if refs is None:
            refs = list(identity['paths'])
        records = load_overrides(Path(config['records']))
        result = {'refs':list(refs), 'fields':fields,
                  'records':{ref:deepcopy(records.get(ref, {})) for ref in refs},
                  'identity':identity, 'mapping':config['mapping'], 'properties':properties, 'terminals':terminals,
                  'versions':{'configuration':config,'files':files,'hashes':hashes}}
        if hasattr(self.app, 'design_sync_parameter_snapshot'):
            result['parameters'] = self.app.design_sync_parameter_snapshot(result)
        if not self.validate(result):
            raise ValueError('读取期间原理图、库表或工作台记录已改变，请重新预检')
        return result

    def validate(self, snapshot: dict) -> bool:
        try:
            versions = snapshot['versions']
            config = self._configuration()
            return (config == versions['configuration'] and self._files(config) == versions['files']
                    and ('parameters' not in snapshot or snapshot['parameters'] ==
                         self.app.design_sync_parameter_snapshot(snapshot))
                    and all(content_hash(path) == digest for path, digest in versions['hashes'].items()))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def refresh_after_save(self, snapshot: dict) -> bool:
        """Accept native formatting/settings saves, never changed selected design data."""
        fresh = self.capture(snapshot['refs'])
        for key in ('refs', 'fields', 'records', 'properties', 'mapping', 'parameters', 'terminals'):
            if fresh.get(key) != snapshot.get(key):
                return False
        if fresh['identity']['paths'] != snapshot['identity']['paths']:
            return False
        before, after = snapshot['versions'], fresh['versions']
        if before['configuration'] != after['configuration'] or before['files'] != after['files']:
            return False
        # Only KiCad document saves may change bytes. Library and workbench
        # changes remain strict even if selected fields happen to be unchanged.
        for path, digest in before['hashes'].items():
            if Path(path).suffix.lower() not in ('.kicad_pro', '.kicad_sch') and after['hashes'].get(path) != digest:
                return False
        snapshot.clear()
        snapshot.update(fresh)
        return True
