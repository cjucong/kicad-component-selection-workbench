"""Conservative cross-row library unification; never writes schematic, PCB or libraries."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from part_selection_design_source import DesignSource, content_hash
from part_selection_footprints import copper_equivalence, item_name
from part_selection_footprint_changes import make_footprint_change
from part_selection_library import checked_path
from part_selection_overrides import (
    OVERRIDES_UPDATE_LOCK, choose_effective_c_code, load_overrides,
    merge_override_record, normalize_schematic_field_snapshot, save_overrides,
)
from part_selection_runtime import runtime_directory
from part_stock_lookup import normalize_c_code


class FootprintUnifier:
    def __init__(self, app):
        self.app = app

    def run(self, *, apply=False, targets=None, progress=None):
        app = self.app
        notify = progress or (lambda *args: None)
        notify('source', '正在核对原理图、工作台记录和封装库')
        sources = DesignSource(app)
        source = sources.capture(allow_legacy=True)
        records = load_overrides(app.overrides_path)
        groups = defaultdict(list)
        effective = {}
        for ref, fields in source['fields'].items():
            record = records.get(ref, {})
            values = {**fields, **record.get('field_overrides', {})}
            effective[ref] = values
            key = tuple(str(values.get(k, '')).strip() for k in ('comment', 'value'))
            if all(key) and values.get('footprint'):
                groups[key].append(ref)
        groups = {key:refs for key,refs in groups.items()
                  if len({effective[ref]['footprint'] for ref in refs}) > 1}
        libraries = app.footprint_choices.libraries()
        hashes, changes, skipped = {}, [], []
        boards = sorted(app.project_root.glob('*.kicad_pcb'))
        preferred = app.project_root / (app.project_root.name + '.kicad_pcb')
        if preferred in boards:
            boards = [preferred]
        if len(boards) > 1:
            raise ValueError('多个 PCB，无法唯一核对实例；没有修改记录')
        if boards:
            hashes[boards[0]] = content_hash(boards[0])

        def library(identifier):
            nickname, name = identifier.split(':', 1)
            path = checked_path(app.project_root, libraries[nickname] / (item_name(name) + '.kicad_mod'))
            hashes.setdefault(path, content_hash(path))
            if hashes[path] is None:
                raise ValueError('封装文件不存在：' + identifier)
            return path

        with tempfile.TemporaryDirectory(prefix='footprint-unify-', dir=runtime_directory(app.project_root)) as temp:
            work = Path(temp)
            instances = {}
            if boards and groups:
                from part_selection_footprint_geometry import board_references
                refs = sorted({ref for group in groups.values() for ref in group})
                notify('pcb', '正在只读核对 PCB 实例铜焊盘')
                instances = {e['ref']:e for e in board_references(boards[0], refs, work)}
            for index, ((comment, value), refs) in enumerate(sorted(groups.items())):
                ids = Counter(effective[ref]['footprint'] for ref in refs)
                # Prefer a standard existing identifier over conversion-generated variants.
                ordered = sorted(ids, key=lambda identifier:
                    (bool(re.search(r'(?:_Row[\d.]|_mapped(?:_|$))', identifier, re.I)), -ids[identifier], identifier))
                requested = (targets or {}).get(value)
                notify('group-'+str(index), '正在核对 ' + value + '：' + ', '.join(refs))
                codes = {ref:normalize_c_code('' if records.get(ref,{}).get('decision') in
                    {'ClearSelection','Reject','NoChange','DNP'} else records.get(ref,{}).get('stock_c_code') or
                    choose_effective_c_code({'accepted_c_code':source['fields'][ref].get('lcsc_c_code','')},
                                            records.get(ref,{}))) for ref in refs}
                reviews = {ref:records.get(ref,{}).get('review_status','Pending') for ref in refs}
                arrivals = defaultdict(list)
                for ref in sorted(refs):
                    before = effective[ref]['footprint']
                    attempts = []
                    for target in ([requested] if requested else ordered[:ordered.index(before)]):
                        if before == target:
                            continue
                        row = {'ref':ref, 'comment':comment, 'value':value, 'before':before, 'after':target}
                        try:
                            peers = [other for other in refs if effective[other]['footprint'] == target] + arrivals[target]
                            if any(codes[other] != codes[ref] for other in peers):
                                raise ValueError('目标行 C 编码不同，避免合行清空选料，已跳过')
                            if any(reviews[other] != reviews[ref] for other in peers):
                                raise ValueError('目标行确认状态不同，避免合行改变确认，已跳过')
                            target_path = library(target)
                            if boards:
                                if ref not in instances:
                                    raise ValueError('已保存 PCB 中缺少该位号，无法核对原位置')
                                reference = Path(instances[ref]['path'])
                            else:
                                reference = library(before)
                            result = copper_equivalence(target_path, reference, work / ('c-' + uuid4().hex[:8]))
                            row.update(max_error_mm=result.get('max_error_mm'), rotation_deg=result.get('rotation_deg'),
                                       offset_mm=result.get('offset_mm'), reason=result['reason'])
                            if result['ok']:
                                changes.append(row)
                                arrivals[target].append(ref)
                                break
                        except (ValueError, KeyError, OSError) as error:
                            row['reason'] = str(error)
                        attempts.append(row)
                    else:
                        if attempts:
                            skipped.append({**attempts[0], 'alternatives':attempts[1:]})
            report = {'ok':True, 'groups':len(groups), 'changes':changes, 'skipped':skipped,
                      'applied':False, 'cad_written':False}
            if not apply or not changes:
                return report
            with app.selection_save_lock, OVERRIDES_UPDATE_LOCK:
                if (not sources.validate(source) or load_overrides(app.overrides_path) != records
                        or any(content_hash(path) != expected for path, expected in hashes.items())):
                    raise ValueError('核对期间原理图、PCB、库或记录发生变化；没有保存任何修改')
                notify('save', '正在备份并保存零偏移封装统一记录')
                backup = runtime_directory(app.project_root) / ('footprint_unify_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f')) / 'overrides_before.json'
                save_overrides(backup, records)
                updated = deepcopy(records)
                for row in changes:
                    ref, target = row['ref'], row['after']
                    previous = records.get(ref, {})
                    evidence = make_footprint_change(operation='replace',
                        before=instances.get(ref,{}).get('footprint',row['before']), after=target,
                        source={'copper_equivalent':True},
                        target={'path':str(library(target)), 'hash':hashes[library(target)]})
                    update = {'field_overrides':{'footprint':target}, 'footprint_change':evidence,
                              'schematic_field_snapshot':{**source['fields'][ref],
                                  **normalize_schematic_field_snapshot(previous.get('schematic_field_snapshot'))}}
                    updated[ref] = merge_override_record(previous, update,
                        datetime.now().isoformat(timespec='seconds'), capture_undo=True)
                if (not sources.validate(source) or any(content_hash(path) != expected for path, expected in hashes.items())):
                    raise ValueError('保存前源文件发生变化；没有保存任何修改')
                save_overrides(app.overrides_path, updated)
                return {**report, 'applied':True, 'backup':str(backup)}
