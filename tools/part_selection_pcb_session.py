"""Verified live KiCad PCB access; never saves or reloads the user's board."""
from base64 import b64decode, b64encode
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from uuid import uuid4

from kicad_sexpr import parse_sexpr
from part_selection_footprints import tree_data


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def select_document(documents, project_root):
    root = Path(project_root).resolve()
    matches = [doc for doc in documents if Path(doc.project.path).resolve() == root]
    if len(matches) != 1:
        raise ValueError('无法唯一确定当前工程的 PCB 编辑器，请打开目标 PCB 并从其插件入口启动')
    path = Path(matches[0].board_filename)
    path = (root / path).resolve()
    if path.parent != root or path.suffix.lower() != '.kicad_pcb':
        raise ValueError('PCB 文件与工程身份不匹配')
    return matches[0]


def encode(item):
    return b64encode(item.proto.SerializeToString()).decode('ascii')


def decode(value):
    from kipy.board_types import FootprintInstance
    from kipy.proto.board.board_types_pb2 import FootprintInstance as Proto
    proto = Proto()
    proto.ParseFromString(b64decode(value, validate=True))
    return FootprintInstance(proto)


def describe(fp):
    from kipy.proto.board.board_types_pb2 import BoardLayer
    from google.protobuf.json_format import MessageToDict
    pads = []
    for pad in fp.definition.pads:
        geometry = MessageToDict(pad.proto.pad_stack, preserving_proto_field_name=True)
        geometry.pop('angle', None)
        pads.append({'number': pad.number, 'position_mm': [pad.position.x / 1e6, pad.position.y / 1e6],
                     'uuid': pad.id.value, 'net': str(pad.proto.net),
                     'geometry': geometry,
                     'padstack': b64encode(pad.proto.pad_stack.SerializeToString()).decode('ascii')})
    return {'ref': fp.reference_field.text.value, 'uuid': fp.id.value,
            'symbol_path': '/' + '/'.join(item.value for item in fp.proto.symbol_path.path),
            'footprint': str(fp.definition.id),
            'position_mm': [fp.position.x / 1e6, fp.position.y / 1e6],
            'rotation_deg': fp.orientation.degrees, 'back': fp.layer == BoardLayer.BL_B_Cu,
            'locked': fp.locked, 'pads': pads, 'proto': encode(fp)}


def outside_parts(text, selected_ids):
    root = parse_sexpr(text, 'kicad_pcb')
    return [tree_data(node) for node in root.children[1:] if not
            (node.head == 'footprint' and node.child('uuid') and node.child('uuid').arg() in selected_ids)]


def replacement(original, target, placement):
    from kipy.board_types import Footprint3DModel, Pad, BoardText, BoardShape, Field, Zone
    from kipy.geometry import Angle, Vector2
    old, result = decode(original['proto']), decode(target['proto'])
    supported = (Pad, BoardText, BoardShape, Field, Zone, Footprint3DModel)
    for item in old.definition.items:
        if not isinstance(item, supported):
            raise ValueError('原封装含当前实时接口不能安全处理的对象：' + type(item).__name__)
    # Kipy's rotation setter omits 3D model children. Keep local models intact;
    # unfamiliar drawable objects must be rejected rather than silently dropped.
    models = []
    for item in result.definition.items:
        if isinstance(item, Footprint3DModel):
            models.append(item)
        elif not isinstance(item, (Pad, BoardText, BoardShape, Field, Zone)):
            raise ValueError('目标封装含当前实时接口不能安全变换的对象：' + type(item).__name__)
    result.orientation = Angle.from_degrees(placement['rotation_deg'])
    for model in models:
        result.definition.add_item(model)
    result.position = Vector2.from_xy(*(round(v * 1e6) for v in placement['position_mm']))
    proto = deepcopy(old.proto)
    proto.definition.CopyFrom(result.proto.definition)
    proto.position.CopyFrom(result.proto.position)
    proto.orientation.CopyFrom(result.proto.orientation)
    result = type(old)(proto)
    existing_fields = {item.name: item for item in old.definition.items if isinstance(item, Field)}
    result.definition.items = [item for item in result.definition.items
                               if not isinstance(item, Field) or item.name not in existing_fields]
    for field in existing_fields.values():
        result.definition.add_item(field)
    old_pads = {pad.number: pad for pad in old.definition.pads}
    for pad in result.definition.pads:
        previous = old_pads[pad.number]
        pad.proto.id.CopyFrom(previous.proto.id)
        pad.proto.net.CopyFrom(previous.proto.net)
        pad.proto.symbol_pin.CopyFrom(previous.proto.symbol_pin)
    for item in result.definition.items:
        if isinstance(item, (Pad, Footprint3DModel)):
            continue
        if isinstance(item, Field):
            if item.name not in existing_fields:
                item.proto.text.id.value = str(uuid4())
        else:
            item.proto.id.value = str(uuid4())
        if 'parent' in item.proto.DESCRIPTOR.fields_by_name and item.proto.HasField('parent'):
            item.proto.parent.CopyFrom(old.id)
    return result


def item_ids(fp):
    from kipy.board_types import Field, Footprint3DModel
    ids = [fp.id.value]
    ids += [getattr(fp, name).proto.text.id.value for name in
            ('reference_field', 'value_field', 'description_field', 'datasheet_field')]
    for item in fp.definition.items:
        if isinstance(item, Field):
            ids.append(item.proto.text.id.value)
        elif not isinstance(item, Footprint3DModel):
            ids.append(item.id.value)
    return [value for value in ids if value and value != '00000000-0000-0000-0000-000000000000']


def validate_item_ids(snapshot, prepared):
    existing = set()
    def visit(node):
        if node.head == 'uuid':
            existing.add(node.arg())
        for child in node.children:
            if child.children:
                visit(child)
    visit(parse_sexpr(snapshot['text'], 'kicad_pcb'))
    for part in snapshot['parts'].values():
        existing.difference_update(item_ids(decode(part['proto'])))
    for fp in prepared:
        for identifier in item_ids(fp):
            if identifier in existing:
                raise ValueError('封装子对象 UUID 重复，未修改 PCB')
            existing.add(identifier)


class PcbSession:
    def __init__(self, root, socket=None, library_reader=None):
        self.root = Path(root).resolve()
        self.socket = socket or os.environ.get('KICAD_API_SOCKET', '')
        self.library_reader = library_reader
        self.lock = threading.RLock()

    def _board(self):
        if not self.socket.startswith('ipc://'):
            raise ValueError('未取得启动 KiCad 的实时接口，请启用 KiCad IPC 并从 PCB 的原生插件入口重新打开工作台')
        from kipy import KiCad
        from kipy.board import Board
        from kipy.proto.common.types import DocumentType
        client = KiCad(socket_path=self.socket, client_name='component-workbench-pcb', timeout_ms=4000)
        doc = select_document(client.get_open_documents(DocumentType.DOCTYPE_PCB), self.root)
        return client, Board(client._client, doc), doc

    def snapshot(self, project_root: Path, refs: list[str]) -> dict:
        if Path(project_root).resolve() != self.root or not refs or len(set(refs)) != len(refs):
            raise ValueError('无效的工程或位号清单')
        with self.lock:
            client, board, doc = self._board()
            text = board.get_as_string()
            instances = board.get_footprints()
            parts = {}
            for fp in instances:
                ref = fp.reference_field.text.value
                if ref in refs:
                    if ref in parts:
                        raise ValueError('PCB 存在重复位号：' + ref)
                    parts[ref] = describe(fp)
            missing = set(refs) - set(parts)
            if missing:
                raise ValueError('PCB 缺少位号：' + ', '.join(sorted(missing)))
            locked = [ref for ref, part in parts.items() if part['locked']]
            if locked:
                raise ValueError('器件已锁定，请先解锁：' + ', '.join(locked))
            server_token = client._client._kicad_token
            if not server_token:
                raise ValueError('KiCad 未返回可核验的会话身份，禁止写入 PCB')
            return {'session_id': fingerprint([self.socket, str(doc), server_token]),
                    'document_path': str(self.root / doc.board_filename),
                    'revision': hashlib.sha256(text.encode()).hexdigest(), 'text': text, 'parts': parts}

    def read_library(self, lib_id: str, *, back=False) -> dict:
        if self.library_reader:
            return self.library_reader(lib_id, back=back)
        from part_selection_pcb_library import read_library
        return read_library(self.root, lib_id, back=back)

    @staticmethod
    def verify(before: dict, after: dict, replacements: list[dict]) -> dict:
        if before['session_id'] != after['session_id']:
            raise ValueError('PCB 编辑器会话已改变')
        selected = {p['uuid'] for p in before['parts'].values()}
        if outside_parts(before['text'], selected) != outside_parts(after['text'], selected):
            raise ValueError('未选中对象或板级数据发生变化')
        for item in replacements:
            ref = item['ref']
            old, new = before['parts'][ref], after['parts'][ref]
            expected = describe(decode(item['proto']))
            for key in ('uuid', 'back', 'footprint', 'position_mm', 'rotation_deg', 'pads'):
                if new[key] != expected[key]:
                    raise ValueError(ref + ' 写入回读不一致：' + key + '；实际 ' + str(new[key])[:160] + '；预期 ' + str(expected[key])[:160])
            a, b = decode(old['proto']), decode(new['proto'])
            for field in ('id', 'symbol_path', 'layer', 'locked', 'reference_field', 'value_field',
                          'datasheet_field', 'description_field', 'attributes', 'overrides',
                          'symbol_sheet_name', 'symbol_sheet_filename', 'symbol_footprint_filters'):
                if getattr(a.proto, field) != getattr(b.proto, field):
                    raise ValueError(ref + ' 实例信息改变：' + field)
            from kipy.board_types import Field
            preserved = {item.name: item.proto for item in a.definition.items if isinstance(item, Field)}
            actual_fields = {item.name: item.proto for item in b.definition.items if isinstance(item, Field)}
            if any(actual_fields.get(name) != value for name, value in preserved.items()):
                raise ValueError(ref + ' 自定义字段或文字位置改变')
            if encode(b) != item['proto']:
                raise ValueError(ref + ' 完整封装定义回读不一致，可能有图形、模型或属性未保留')
        return {'ok': True}

    def apply(self, snapshot: dict, replacements: list[dict], backup_dir: Path) -> dict:
        with self.lock:
            refs = list(snapshot['parts'])
            if not refs or len(replacements) != len(refs) or {item.get('ref') for item in replacements} != set(refs):
                raise ValueError('更新清单必须与预检选中器件完全一致，不能重复或扩大范围')
            current = self.snapshot(self.root, refs)
            if current != snapshot:
                raise ValueError('预览后 PCB 已改变，请重新预览')
            backup_dir = Path(backup_dir).resolve()
            if backup_dir.is_relative_to(self.root):
                raise ValueError('PCB 恢复材料必须放在工程外')
            backup_dir.mkdir(parents=True, exist_ok=False)
            (backup_dir / 'before.kicad_pcb').write_text(current['text'], encoding='utf-8')
            (backup_dir / 'transaction.json').write_text(json.dumps({
                'before': current, 'replacements': replacements}, ensure_ascii=False), encoding='utf-8')
            prepared = [decode(item['proto']) for item in replacements]
            for item, fp in zip(replacements, prepared):
                original = snapshot['parts'][item['ref']]
                if fp.id.value != original['uuid'] or fp.reference_field.text.value != item['ref']:
                    raise ValueError('待写入封装身份与选中器件不符，未修改 PCB')
            validate_item_ids(snapshot, prepared)
            _, board, _ = self._board()
            # Finish every source check before the first mutation.
            if self.snapshot(self.root, refs) != snapshot:
                raise ValueError('备份期间 PCB 已改变，请重新预览')
            commit = board.begin_commit()
            try:
                staged = board.update_items(prepared)
                if sorted(encode(item) for item in staged) != sorted(item['proto'] for item in replacements):
                    (backup_dir / 'staged.json').write_text(json.dumps([encode(item) for item in staged]), encoding='utf-8')
                    raise ValueError('KiCad 暂存返回内容与预检不一致，未发布更改')
            except BaseException as exc:
                try:
                    board.drop_commit(commit)
                    restored = self.snapshot(self.root, refs)
                    if restored != snapshot:
                        raise ValueError('无法确认本批次已完整撤回')
                except BaseException as rollback:
                    raise RuntimeError(f'PCB 更新失败，恢复未确认；请勿继续操作。恢复目录：{backup_dir}；{rollback}') from exc
                raise ValueError('PCB 更新失败，已撤回本批次：' + str(exc)) from exc
            publish_error = None
            try:
                board.push_commit(commit, '选料工作台：所选器件原位替换')
            except Exception as exc:
                publish_error = exc
            # A timed-out publish can already have taken effect. Reconnect and
            # classify without repeating writes or dropping an ended commit.
            import time
            last_error = publish_error
            for attempt in range(3):
                try:
                    actual = self.snapshot(self.root, refs)
                    if actual == snapshot:
                        try:
                            board.drop_commit(commit)
                        except Exception:
                            pass
                        if self.snapshot(self.root, refs) == snapshot:
                            raise _Unpublished('PCB 更新未生效或已撤回；当前 PCB 与更新前一致')
                    self.verify(snapshot, actual, replacements)
                    result = {'ok': True, 'saved': False, 'refs': refs, 'snapshot': actual,
                              'backup_dir': str(backup_dir)}
                    if publish_error:
                        result['warning'] = '提交回执丢失，已重新读取并验证更新结果；没有重复执行'
                    try:
                        (backup_dir / 'readback.json').write_text(json.dumps(actual, ensure_ascii=False), encoding='utf-8')
                    except OSError:
                        result['warning'] = 'PCB 已回读通过，但回读日志保存失败；更新前恢复材料仍保留'
                    return result
                except _Unpublished as exc:
                    raise ValueError(str(exc)) from publish_error
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(.15)
            # KiCad 10 exposes no atomic undo-stack ownership check. A generic
            # undo here could remove a newer user edit: leave the editor dirty
            # and recovery materials intact rather than claiming a rollback.
            raise RuntimeError(f'PCB 更新回读或恢复未确认；请检查编辑器并停止重复更新。'
                               f'未自动撤销其他编辑。恢复目录：{backup_dir}；{last_error}') from publish_error


class _Unpublished(Exception):
    pass
