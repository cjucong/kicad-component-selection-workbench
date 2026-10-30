"""Saved schematic navigation identities, with no UI, network, or CAD writes.

Hold the returned index alongside the server's existing source snapshot: lookup
does no I/O. Rebuild after a known schematic/project-settings change; is_current
is an optional byte check, not an automatic per-navigation scan. read_page may
reuse a caller's parsed-page cache and must return (Node, SHA256).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from kicad_sexpr import Node, parse_sexpr


@dataclass(frozen=True)
class NavigationTarget:
    ref: str
    search_name: str
    unit: int
    symbol_uuid: str
    instance_path: str
    page_file: Path
    page_name: str
    page_path: str

    @property
    def title_prefix(self) -> str:
        """KiCad 10 title before its localized editor suffix or dirty marker."""
        name = self.page_file.stem
        return name if name == self.page_path else f'{name} [{self.page_path}]'


class NavigationIdentity:
    def __init__(self, targets: Iterable[NavigationTarget], hashes: Mapping[str, str]):
        grouped: dict[str, list[NavigationTarget]] = {}
        for target in targets:
            if target not in grouped.setdefault(target.ref, []):
                grouped[target.ref].append(target)
        self._targets = {ref: tuple(items) for ref, items in grouped.items()}
        self.source_hashes = MappingProxyType(dict(hashes))

    def lookup(self, ref: str) -> tuple[NavigationTarget, ...]:
        """Exact base reference only: U1 never includes U10 or guessed U1A."""
        key = str(ref).strip().upper()
        if key not in self._targets:
            raise ValueError('No verified schematic navigation identity: ' + key)
        return self._targets[key]

    def search_names(self, ref: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(target.search_name for target in self.lookup(ref)))

    def matches(self, ref: str, search_name: str, *, page_path: str | None = None
                ) -> tuple[NavigationTarget, ...]:
        """Return all exact candidates; never silently choose a repeated instance."""
        return tuple(target for target in self.lookup(ref)
                     if target.search_name == search_name
                     and (page_path is None or target.page_path == page_path))

    def is_current(self) -> bool:
        try:
            return all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                       for path, digest in self.source_hashes.items())
        except OSError:
            return False


def _unit_suffix(unit: int, first: int, separator: int) -> str:
    if 48 <= first <= 57:
        suffix = str(unit)
    else:
        suffix = ''
        while unit:
            unit, remainder = divmod(unit - 1, 26)
            suffix = chr(first + remainder) + suffix
    return (chr(separator) if separator else '') + suffix


def _unit_count(page: Node, symbol: Node) -> int:
    lib = page.child('lib_symbols')
    name_node = symbol.child('lib_name') or symbol.child('lib_id')
    definitions = [item for item in lib.children_named('symbol')
                   if name_node and item.arg() == name_node.arg()] if lib else []
    if len(definitions) != 1 or definitions[0].child('extends'):
        raise ValueError('Cannot verify embedded symbol unit count: '
                         + (name_node.arg() if name_node else '<missing lib_id>'))
    units = [int(match.group(1)) for item in definitions[0].children_named('symbol')
             if (match := re.fullmatch(r'.+_(\d+)_(\d+)', item.arg()))]
    if not units or max(units) < 1:
        raise ValueError('Embedded symbol has no verifiable units: ' + name_node.arg())
    return max(units)


def load_navigation_identity(
    project: Path | str,
    refs: Iterable[str] | None = None,
    *,
    read_page: Callable[[Path], tuple[Node, str]] | None = None,
) -> NavigationIdentity:
    """Read an explicit absolute .kicad_pro or uniquely identified project folder.

    Reuses schematic_identity for project ownership and legacy root verification.
    Missing/ambiguous instances or unit definitions fail closed. page_path uses
    KiCad's full human-readable path, not an ambiguous leaf filename. A root page
    is the project basename; nested pages use Sheetname fields, not Sheetfile.
    """
    from part_selection_pcb import schematic_identity

    project = Path(project)
    if not project.is_absolute():
        raise ValueError('An explicit absolute project path is required')
    project = project.resolve()
    if project.is_dir():
        files = list(project.glob('*.kicad_pro'))
        if len(files) != 1:
            raise ValueError('Project folder must contain exactly one .kicad_pro')
        project = files[0]
    if project.suffix != '.kicad_pro' or not project.is_file():
        raise ValueError('Expected an existing .kicad_pro: ' + str(project))
    root = project.parent
    if len(list(root.glob('*.kicad_pro'))) != 1:
        raise ValueError('Multiple project files cannot share this navigation index')
    main = project.with_suffix('.kicad_sch')
    if not main.is_file():
        raise ValueError('Missing project root schematic: ' + str(main))
    project_bytes = project.read_bytes()
    settings = json.loads(project_bytes.decode('utf-8-sig')).get('schematic', {})
    first = settings.get('subpart_first_id', 65)
    separator = settings.get('subpart_id_separator', 0)
    if (type(first) is not int or not 49 <= first <= 122
            or type(separator) is not int or not 0 <= separator <= 126):
        raise ValueError('Invalid saved subpart naming settings')
    requested = None if refs is None else list(dict.fromkeys(str(ref).strip().upper() for ref in refs))
    if requested is not None and (not requested or any(not ref for ref in requested)):
        raise ValueError('Select nonempty exact references')
    pages: dict[Path, Node] = {}
    symbols: dict[tuple[str, Path, str], Node] = {}

    def read(path):
        path = path.resolve()
        if read_page:
            node, digest = read_page(path)
        else:
            content = path.read_bytes()
            node = parse_sexpr(content.decode('utf-8-sig'))
            digest = hashlib.sha256(content).hexdigest()
        pages[path] = node
        return node, digest

    def visit(ref, symbol, path):
        symbols[(ref, path, symbol.child('uuid').arg())] = symbol

    identity = schematic_identity(root, root, requested, read_page=read, visit_symbol=visit)
    root_uuid = pages[main].child('uuid').arg()
    targets: list[NavigationTarget] = []

    def walk(path, chain, labels):
        page = pages[path]
        for symbol in page.children_named('symbol'):
            identifier = symbol.child('uuid')
            if not identifier:
                continue
            instances = symbol.child('instances')
            for group in instances.children_named('project') if instances else []:
                if group.arg() != project.stem:
                    continue
                for instance in group.children_named('path'):
                    reference = instance.child('reference')
                    ref = reference.arg().upper() if reference else ''
                    if (ref, path, identifier.arg()) not in symbols:
                        continue
                    prefix = tuple(filter(None, instance.arg().split('/')))
                    if prefix != chain and not (len(prefix) == len(chain) + 1 and prefix[1:] == chain):
                        continue
                    full = '/' + '/'.join((*prefix, identifier.arg()))
                    # The existing hierarchy parser, not a leaf/path suffix,
                    # must have verified this exact instance for this reference.
                    if full not in identity['paths'][ref]:
                        continue
                    unit_node = instance.child('unit') or symbol.child('unit')
                    unit = int(unit_node.arg()) if unit_node else 1
                    count = _unit_count(page, symbol)
                    if not 1 <= unit <= count:
                        raise ValueError('Invalid symbol unit for ' + ref)
                    search_name = reference.arg() + (_unit_suffix(unit, first, separator) if count > 1 else '')
                    targets.append(NavigationTarget(
                        ref, search_name, unit, identifier.arg(),
                        '/' + '/'.join((root_uuid, *chain, identifier.arg())),
                        path, labels[-1], '/'.join(labels)))
        for sheet in page.children_named('sheet'):
            props = {prop.arg().casefold(): prop.arg(2) for prop in sheet.children_named('property')}
            name = props.get('sheetname', '')
            if not name or '${' in name:
                raise ValueError('Cannot verify literal sheet display name: ' + str(path))
            child = (path.parent / props['sheetfile'].replace('${KIPRJMOD}', str(root))).resolve()
            walk(child, (*chain, sheet.child('uuid').arg()), (*labels, name))

    walk(main, (), (main.stem,))
    result = NavigationIdentity(targets, {
        **identity['hashes'], str(project): hashlib.sha256(project_bytes).hexdigest()})
    for ref in requested if requested is not None else identity['paths']:
        result.lookup(ref)
    if not result.is_current():
        raise ValueError('Saved navigation sources changed while reading')
    return result
