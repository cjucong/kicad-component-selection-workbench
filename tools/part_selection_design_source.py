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

    def capture(self, refs: list[str]) -> dict:
        from part_selection_pcb import schematic_identity
        if not refs or len(set(refs)) != len(refs):
            raise ValueError('请选择不重复的位号')
        config = self._configuration()
        files = self._files(config)
        hashes = {path:content_hash(path) for path in files}
        for path in (Path(config['root']) / 'fp-lib-table', Path(config['records'])):
            hashes[str(path)] = content_hash(path)
        fields, properties = {}, {}

        def read(path):
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            previous = hashes.setdefault(str(path), digest)
            if previous != digest:
                raise ValueError('读取期间原理图已改变，请重新预检')
            return parse_sexpr(content.decode('utf-8-sig'), 'kicad_sch'), digest

        def visit(ref, symbol, path):
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

        identity = schematic_identity(Path(config['root']), Path(config['directory']), refs,
                                       read_page=read, visit_symbol=visit)
        records = load_overrides(Path(config['records']))
        result = {'refs':list(refs), 'fields':fields,
                  'records':{ref:deepcopy(records.get(ref, {})) for ref in refs},
                  'identity':identity, 'mapping':config['mapping'], 'properties':properties,
                  'versions':{'configuration':config,'files':files,'hashes':hashes}}
        if not self.validate(result):
            raise ValueError('读取期间原理图、库表或工作台记录已改变，请重新预检')
        return result

    def validate(self, snapshot: dict) -> bool:
        try:
            versions = snapshot['versions']
            config = self._configuration()
            return (config == versions['configuration'] and self._files(config) == versions['files']
                    and all(content_hash(path) == digest for path, digest in versions['hashes'].items()))
        except (OSError, ValueError, KeyError, TypeError):
            return False
