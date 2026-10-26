"""Verified live KiCad PCB access; saving is an explicit orchestration step."""
from base64 import b64decode, b64encode
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import uuid4

from kicad_sexpr import parse_sexpr
from part_selection_footprints import tree_data


SAVE_TIMEOUT_MS = 60000
SAVE_RECONCILE_SECONDS = 30


class PcbSaveUnverified(RuntimeError):
    def __init__(self, message, backup_dir):
        super().__init__(message)
        self.pcb_result = {'ok':False, 'applied':True, 'readback_verified':True,
                           'saved':False, 'save_uncertain':True, 'backup_dir':str(backup_dir)}


def save_document_once(client, board, notify):
    """Bound one save, never let Req0 retransmit this non-idempotent command."""
    native = getattr(client, '_client', None)
    transport = getattr(native, '_conn', None)
    previous = []
    failure = []
    worker = None
    done = threading.Event()
    def save():
        try:
            board.save()
        except BaseException as exc:
            failure.append(exc)
        finally:
            done.set()
    try:
        for owner, name, value in ((native, '_timeout_ms', SAVE_TIMEOUT_MS),
                                   (transport, 'send_timeout', 4000),
                                   (transport, 'recv_timeout', SAVE_TIMEOUT_MS),
                                   (transport, 'resend_time', SAVE_TIMEOUT_MS + 60000)):
            if owner is not None and hasattr(owner, name):
                previous.append((owner, name, getattr(owner, name)))
                setattr(owner, name, value)
        worker = threading.Thread(target=save, name='pcb-save-once', daemon=True)
        started = time.monotonic()
        worker.start()
        while not done.wait(5):
            notify('save', f'正在等待 KiCad 保存 PCB（{time.monotonic()-started:.0f} s / 最多 60 s），不会重复提交')
        worker.join()
        if failure:
            raise failure[0]
    finally:
        if worker is not None and worker.ident is not None:
            worker.join()
        for owner, name, value in reversed(previous):
            setattr(owner, name, value)
        if failure and transport is not None:
            # An unanswered request must not later resend or consume a stale reply.
            transport.close()
            native._connected = False


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def select_document(documents, project_root):
    root = Path(project_root).resolve()
    matches = [doc for doc in documents if doc.project.path and Path(doc.project.path).resolve() == root]
    if len(matches) != 1:
        raise ValueError('无法唯一确定当前工程的 PCB 编辑器，请只保留一个目标 PCB 编辑器')
    path = Path(matches[0].board_filename)
    path = (root / path).resolve()
    if path.parent != root or path.suffix.lower() != '.kicad_pcb':
        raise ValueError('PCB 文件与工程身份不匹配')
    return matches[0]


def encode(item):
    return b64encode(item.proto.SerializeToString()).decode('ascii')


def footprint_bytes(item):
    """Compare complete definitions without treating object order as geometry."""
    proto = deepcopy(item.proto)
    objects = sorted(obj.SerializeToString(deterministic=True) for obj in proto.definition.items)
    proto.definition.ClearField('items')
    for obj in objects:
        proto.definition.items.add().ParseFromString(obj)
    return proto.SerializeToString(deterministic=True)


def persisted_footprint_matches(before, after):
    """Only reopen may quantize coordinates and KiCad's saved decimal angles."""
    first, second = deepcopy(before.proto), deepcopy(after.proto)
    first.ParseFromString(footprint_bytes(before))
    second.ParseFromString(footprint_bytes(after))

    def same(a, b, parent=''):
        if a.DESCRIPTOR.full_name == 'google.protobuf.Any':
            if a.type_url != b.type_url:
                return False
            from google.protobuf import descriptor_pool, message_factory
            try:
                descriptor = descriptor_pool.Default().FindMessageTypeByName(a.type_url.rsplit('/', 1)[-1])
            except KeyError:
                return a == b
            message = message_factory.GetMessageClass(descriptor)
            x, y = message(), message()
            x.ParseFromString(a.value)
            y.ParseFromString(b.value)
            if not same(x, y, parent):
                return False
            a.value = b.value
            return a == b
        for field in a.DESCRIPTOR.fields:
            coordinate = field.name in ('x_nm', 'y_nm', 'z_nm') and parent in ('position', 'point')
            angle = field.name == 'value_degrees' and parent in ('angle', 'orientation')
            if coordinate or angle:
                x, y = getattr(a, field.name), getattr(b, field.name)
                if angle and (not math.isfinite(x) or not math.isfinite(y)):
                    return False
                if abs(x - y) > (1 if coordinate else 5e-8):
                    return False
                if angle and x != y and not any(round(x, digits) == y for digits in (7, 8)):
                    return False
                setattr(a, field.name, y)
        left, right = a.ListFields(), b.ListFields()
        if [field.number for field, _ in left] != [field.number for field, _ in right]:
            return False
        for (field, x), (_, y) in zip(left, right):
            repeated = getattr(field, 'is_repeated', None)
            if repeated is None:
                repeated = field.label == field.LABEL_REPEATED
            if repeated:
                if len(x) != len(y):
                    return False
                if field.message_type:
                    if not all(same(v, w, field.name) for v, w in zip(x, y)):
                        return False
                elif x != y:
                    return False
            elif field.message_type:
                if not same(x, y, field.name):
                    return False
            elif x != y:
                return False
        return a == b

    return same(first, second)


def decode(value):
    from kipy.board_types import FootprintInstance
    from kipy.proto.board.board_types_pb2 import FootprintInstance as Proto
    proto = Proto()
    proto.ParseFromString(b64decode(value, validate=True))
    return FootprintInstance(proto)


def geometry_data(original):
    """Native geometry for completion recognition; not transaction validation."""
    from google.protobuf.json_format import MessageToDict
    from kipy.board_types import Field
    fp = decode(original['proto'])
    def clean(value):
        if isinstance(value, dict):
            return {key:clean(item) for key,item in value.items()
                    if key not in ('id','parent','net','symbol_pin','locked')}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    items = sorted(([type(item).__name__, clean(MessageToDict(item.proto, preserving_proto_field_name=True))]
                    for item in fp.definition.items if not isinstance(item, Field)), key=lambda item:json.dumps(item, sort_keys=True))
    definition = deepcopy(fp.proto.definition)
    definition.ClearField('items')
    return [clean(MessageToDict(definition, preserving_proto_field_name=True)), items,
            MessageToDict(fp.proto.position, preserving_proto_field_name=True),
            MessageToDict(fp.proto.orientation, preserving_proto_field_name=True), fp.proto.layer]


def geometry_signature(original):
    return fingerprint(geometry_data(original))


def geometry_matches(original, proposed):
    # Completion recognition only: native nm/angle round-trips can vary slightly.
    # The transaction's complete staged/readback equality remains exact.
    def same(a, b, key=''):
        if key.endswith('_nm'):
            return abs(int(a)-int(b)) <= 5
        if key == 'value_degrees':
            return abs(math.remainder(float(a)-float(b), 360)) <= .0001
        if isinstance(a, dict) and isinstance(b, dict):
            keys = set(a) | set(b)
            for k in keys:
                if k.endswith('_nm') or k == 'value_degrees':
                    if not same(a.get(k,0), b.get(k,0), k): return False
                elif k in a and k in b:
                    if not same(a[k], b[k], k): return False
                else:
                    value = a.get(k,b.get(k))
                    if not (isinstance(value, dict) and value and set(value) <= {
                            'x_nm','y_nm','z_nm','value_nm','value_degrees'} and same({}, value)):
                        return False
            return True
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(same(x,y) for x,y in zip(a,b))
        return type(a) is type(b) and a == b
    before, after = geometry_data(original), geometry_data(proposed)
    if same(before, after):
        return True
    if not same([before[0], *before[2:]], [after[0], *after[2:]]) or len(before[1]) != len(after[1]):
        return False
    # A one-nanometre round-trip can change the sort order of otherwise
    # identical graphics. Match the item multiset, not sorted neighbours.
    candidates = [[j for j, target in enumerate(after[1]) if same(item, target)] for item in before[1]]
    assigned, reverse = {}, {}
    for start in range(len(candidates)):
        queue, seen, parent, free = [start], {start}, {}, None
        for row in queue:
            for column in candidates[row]:
                if column in parent:
                    continue
                parent[column] = row
                if column not in assigned:
                    free = column
                    break
                other = assigned[column]
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
            if free is not None:
                break
        if free is None:
            return False
        while free is not None:
            row = parent[free]
            previous = reverse.get(row)
            assigned[free], reverse[row] = row, free
            free = previous
    return True


def describe(fp):
    from kipy.proto.board.board_types_pb2 import BoardLayer
    from google.protobuf.json_format import MessageToDict
    pads = []
    for pad in fp.definition.pads:
        geometry = MessageToDict(pad.proto.pad_stack, preserving_proto_field_name=True)
        geometry.pop('angle', None)
        pads.append({'number': pad.number, 'position_mm': [pad.position.x / 1e6, pad.position.y / 1e6],
                     'uuid': pad.id.value, 'net': str(pad.proto.net),
                     'rotation_deg': math.remainder(pad.proto.pad_stack.angle.value_degrees, 360),
                     'geometry': geometry,
                     'padstack': b64encode(pad.proto.pad_stack.SerializeToString()).decode('ascii')})
    return {'ref': fp.reference_field.text.value, 'uuid': fp.id.value,
            'symbol_path': '/' + '/'.join(item.value for item in fp.proto.symbol_path.path),
            'footprint': str(fp.definition.id),
            'position_mm': [fp.position.x / 1e6, fp.position.y / 1e6],
            'rotation_deg': fp.orientation.degrees, 'back': fp.layer == BoardLayer.BL_B_Cu,
            'locked': fp.locked, 'pads': pads, 'proto': encode(fp)}


def validate_locked_update(original, item):
    """A lock permits metadata only, never an implicit unlock or geometry edit."""
    if not original.get('locked'):
        return
    message = item['ref'] + ' 已锁定；更换封装或调整位置前请在 PCB 解锁并重新预检'
    if item.get('kind') not in ('rename', 'fields', 'none'):
        raise ValueError(message)
    from kipy.board_types import Field
    actual = decode(item['proto'])
    expected = decode(metadata_replacement(original, footprint=str(actual.definition.id), fields=item.get('fields')))
    old_names = {field.name for field in decode(original['proto']).definition.items if isinstance(field, Field)}
    actual_fields = {field.name:field for field in actual.definition.items if isinstance(field, Field)}
    # New metadata fields get fresh UUIDs on each construction; only those IDs vary.
    for field in expected.definition.items:
        if isinstance(field, Field) and field.name not in old_names and field.name in actual_fields:
            field.proto.text.id.CopyFrom(actual_fields[field.name].proto.text.id)
    if footprint_bytes(actual) != footprint_bytes(expected):
        raise ValueError(message)


def metadata_replacement(original: dict, *, footprint=None, fields=None) -> str:
    """Copy the live instance verbatim; change only explicitly supplied metadata."""
    from kipy.board_types import Field
    result = decode(original['proto'])
    if footprint is not None:
        if not isinstance(footprint, str) or footprint.count(':') != 1 or not all(footprint.split(':')):
            raise ValueError('缺少完整库封装标识')
        result.definition.id.library, result.definition.id.name = footprint.split(':')
    fields = fields or {}
    if not isinstance(fields, dict) or any(not isinstance(name, str) or not name.strip()
            or name != name.strip() or len(name) > 128 or any(ord(c) < 32 for c in name)
            or not isinstance(value, str) for name, value in fields.items()):
        raise ValueError('PCB 字段更新参数无效')
    if len({name.casefold() for name in fields}) != len(fields):
        raise ValueError('PCB 字段目标大小写冲突')
    if {'reference', '位号', 'footprint'} & {name.casefold() for name in fields}:
        raise ValueError('不能通过属性更新修改位号或封装身份')
    builtins = {'value':'value_field', 'description':'description_field', 'datasheet':'datasheet_field'}
    existing = {key:[getattr(result, attr)] for key, attr in builtins.items()}
    for field in result.definition.items:
        if isinstance(field, Field):
            existing.setdefault(field.name.casefold(), []).append(field)
    if any(len(items) > 1 for items in existing.values()):
        raise ValueError('PCB 存在重复大小写属性，请先核对')
    next_id = max([3, *(f.field_id for group in existing.values() for f in group)]) + 1
    for name, value in fields.items():
        matching = existing.get(name.casefold())
        if matching:
            matching[0].text.value = value
        else:
            field = Field(deepcopy(result.value_field.proto))
            field.name = name
            field.proto.id.id = next_id
            next_id += 1
            field.proto.text.id.value = str(uuid4())
            # KiCad serializes embedded fields without a parent pointer.
            field.proto.text.ClearField('parent')
            field.visible = False
            field.text.value = value
            result.definition.add_item(field)
    return encode(result)


def metadata_values(original):
    from kipy.board_types import Field
    fp = decode(original['proto'])
    result = {'Value':fp.value_field.text.value, 'Description':fp.description_field.text.value,
              'Datasheet':fp.datasheet_field.text.value}
    folded = {name.casefold() for name in result}
    for field in fp.definition.items:
        if isinstance(field, Field):
            if field.name.casefold() in folded:
                raise ValueError('PCB 存在重复大小写属性：' + field.name)
            result[field.name] = field.text.value
            folded.add(field.name.casefold())
    return result


def outside_parts(text, selected_ids):
    root = parse_sexpr(text, 'kicad_pcb')
    return [tree_data(node) for node in root.children[1:] if not
            (node.head == 'footprint' and node.child('uuid') and node.child('uuid').arg() in selected_ids)]


def saved_board_data(text):
    """IPC exports standalone footprint headers that PCB files omit."""
    root = parse_sexpr(text, 'kicad_pcb')
    for footprint in root.children_named('footprint'):
        footprint.children = [node for node in footprint.children
                              if node.head not in ('version', 'generator', 'generator_version')]
    return tree_data(root)


def replacement(original, target, placement):
    from kipy.board_types import Footprint3DModel, Pad, BoardText, BoardShape, Field, Zone
    from kipy.geometry import Angle, Vector2
    old, result = decode(original['proto']), decode(target['proto'])
    mapping = placement.get('mapping')
    if mapping is not None:
        from part_selection_pad_mapping import validate_mapping
        mapping = validate_mapping(mapping, [p.number for p in old.definition.pads if p.number],
                                   [p.number for p in result.definition.pads if p.number])
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
    target_numbers = (dict(mapping['number_map']) if mapping is not None
                      else {number:number for number in old_pads})
    for pad in result.definition.pads:
        original_number = target_numbers.get(pad.number)
        if original_number is None:
            if mapping is None:
                raise ValueError('目标封装存在未映射焊盘：' + pad.number)
            # Extra target pads are real geometry, but have no electrical
            # identity until the user explicitly wires them in the PCB.
            pad.proto.id.value = str(uuid4())
            pad.proto.net.Clear()
            pad.proto.symbol_pin.Clear()
            continue
        previous = old_pads.get(original_number)
        if previous is None:
            raise ValueError('手动焊盘对应缺少原端子：' + original_number)
        pad.number = original_number
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


def require_safe_commit_route(schematic_close_verified):
    # This KiCad build has no GetOpenDocuments(SCHEMATIC) handler. The UI
    # adapter must first save/close only an existing, verified project schematic.
    if schematic_close_verified is not True:
        raise ValueError('PCB 未写入：尚未确认同工程原理图已安全暂关，请从工作台重新预检后重试')


def pcb_socket_candidates(inherited):
    """Discover local KiCad transports only; a transport is never proof of identity."""
    candidates = [inherited] if inherited.startswith('ipc://') else []
    directories = {Path(tempfile.gettempdir()) / 'kicad'}
    if candidates:
        directories.add(Path(inherited.removeprefix('ipc://')).parent)
    if os.name == 'nt':
        try:
            with os.scandir(r'\\.\pipe') as pipes:
                candidates.extend('ipc://' + pipe.name for pipe in pipes
                                  if re.fullmatch(r'.+[\\/]kicad[\\/]api(?:-\d+)?\.sock', pipe.name, re.I))
        except OSError:
            pass
    else:
        for directory in directories:
            candidates.extend('ipc://' + str(path) for path in directory.glob('api*.sock')
                              if re.fullmatch(r'api(?:-\d+)?\.sock', path.name))
    # The standard endpoint can be a Windows named pipe, not a disk file.
    candidates.extend('ipc://' + str(directory / 'api.sock') for directory in directories)
    unique = {}
    for candidate in candidates:
        unique.setdefault(os.path.normcase(candidate), candidate)
    return list(unique.values())


class PcbSession:
    def __init__(self, root, socket=None, library_reader=None, connection=None):
        self.root = Path(root).resolve()
        self.socket = socket or os.environ.get('KICAD_API_SOCKET', '')
        self.library_reader = library_reader
        self.lock = threading.RLock()
        if connection is None:
            from part_selection_pcb_connection import PcbConnection
            connection = PcbConnection(self.root, discover=self.discover)
        self.connection = connection

    def discover(self, *, timeout_seconds=None):
        from kipy import KiCad
        from kipy.proto.common.types import DocumentType
        matches, errors = {}, []
        busy = False
        deadline = None if timeout_seconds is None else time.monotonic()+max(0, timeout_seconds)
        def exhausted():
            return {'state':'busy','errors':errors,'inventory_incomplete':True,
                    'reason':'PCB 接口探测等待时间已用尽；尚未核实全部会话，请稍后重试'}
        for socket in pcb_socket_candidates(self.socket):
            remaining = None if deadline is None else deadline-time.monotonic()
            if remaining is not None and remaining < .003:
                return exhausted()
            # Dial, send and receive can each consume a transport timeout.
            timeout_ms = 600 if remaining is None else min(600, max(1, int(remaining*1000/3)))
            try:
                # A token inherited from the schematic belongs to that server,
                # not necessarily to the PCB server. Learn each server's token.
                client = KiCad(socket_path=socket, client_name='component-workbench-pcb',
                               kicad_token='', timeout_ms=timeout_ms)
                documents = [doc for doc in client.get_open_documents(DocumentType.DOCTYPE_PCB)
                             if doc.project.path and Path(doc.project.path).resolve() == self.root]
            except Exception as exc:
                errors.append({'socket':socket,'type':type(exc).__name__,'reason':str(exc)[:400]})
                # A missing endpoint is not another session; a server that
                # accepted a request but timed out cannot be ruled out safely.
                busy = busy or isinstance(exc, TimeoutError) or (
                    'timed out' in str(exc).lower() and not str(exc).startswith('Failed to connect to KiCad:'))
                if getattr(exc, 'code', None) is not None:
                    from kipy.proto.common.envelope_pb2 import AS_BUSY
                    busy = busy or exc.code == AS_BUSY
                continue
            if deadline is not None and time.monotonic() >= deadline:
                return exhausted()
            if not documents:
                continue
            try:
                doc = select_document(documents, self.root)
            except ValueError as exc:
                return {'state':'ambiguous','reason':str(exc),'errors':errors}
            token = client._client._kicad_token
            if not token:
                return {'state':'unknown','reason':'KiCad 未返回可核验的会话身份，禁止写入 PCB',
                        'errors':errors, 'identity_unverified':True}
            identity = (token, str((self.root / doc.board_filename).resolve()))
            matches.setdefault(identity, (socket, client, doc))
        if deadline is not None and time.monotonic() >= deadline:
            return exhausted()
        if not matches:
            if busy:
                return {'state':'busy','errors':errors,
                        'reason':'KiCad 正在交互操作或有阻塞对话框；请完成操作后重试，不会重复打开 PCB'}
            return {'state':'unknown','errors':errors,
                    'reason':'未找到当前工程可连接的 PCB 编辑器。请打开当前工程的 PCB 并启用 KiCad IPC，再点击重试；无需关闭或更换工作台入口'}
        if len(matches) != 1:
            return {'state':'ambiguous','errors':errors,
                    'reason':'检测到多个属于当前工程的 PCB 编辑器，无法唯一确定目标；请只保留一个后重试'}
        if busy:
            return {'state':'busy','errors':errors,'inventory_incomplete':True,
                    'reason':'部分 KiCad 会话忙碌或回复超时，尚不能唯一核实 PCB；请稍后重试'}
        socket, client, doc = next(iter(matches.values()))
        return {'state':'connected','socket':socket,'client':client,'document':doc,
                'project':str(self.root),'file':str((self.root / doc.board_filename).resolve()),
                'session':fingerprint([socket, str(doc), client._client._kicad_token]),'errors':errors}

    def _board(self, progress=None, *, open_if_closed=True):
        from kipy.board import Board
        options = {'progress':progress} if progress else {}
        result = self.connection.ensure(open_if_closed=open_if_closed, **options)
        if result['state'] != 'connected':
            raise ValueError(result.get('reason') or 'PCB 无法连接，请打开当前工程 PCB 并启用 IPC 后重试')
        client, doc = result['client'], result['document']
        select_document([doc], self.root)
        if not client._client._kicad_token:
            raise ValueError('KiCad 未返回可核验的会话身份，禁止写入 PCB')
        self.socket = result['socket']
        client._client._timeout_ms = 4000
        transport = getattr(client._client, '_conn', None)
        if transport is not None:
            transport.send_timeout = transport.recv_timeout = 4000
        return client, Board(client._client, doc), doc

    def snapshot(self, project_root: Path, refs: list[str], progress=None, *, open_if_closed=True) -> dict:
        if Path(project_root).resolve() != self.root or not refs or len(set(refs)) != len(refs):
            raise ValueError('无效的工程或位号清单')
        with self.lock:
            client, board, doc = (self._board(progress, open_if_closed=False) if not open_if_closed else
                                  self._board(progress) if progress else self._board())
            if progress:
                progress('reading', '正在读取 PCB 实时内容和所选封装')
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
            server_token = client._client._kicad_token
            if not server_token:
                raise ValueError('KiCad 未返回可核验的会话身份，禁止写入 PCB')
            return {'session_id': fingerprint([self.socket, str(doc), server_token]),
                    'document_path': str(self.root / doc.board_filename),
                    'revision': hashlib.sha256(text.encode()).hexdigest(), 'text': text, 'parts': parts}

    def save_verified(self, expected: dict, backup_dir: Path, progress=None) -> dict:
        notify = progress or (lambda *args: None)
        with self.lock:
            refs = list(expected['parts'])
            if self.snapshot(self.root, refs) != expected:
                raise ValueError('PCB 在保存前已改变，请重新核验；未自动关闭编辑器')
            path = Path(expected['document_path']).resolve()
            if path.parent != self.root or path.suffix.lower() != '.kicad_pcb':
                raise ValueError('PCB 保存目标不属于当前工程')
            backup_dir = Path(backup_dir).resolve()
            if backup_dir.is_relative_to(self.root):
                raise ValueError('保存前备份必须在工程外')
            backup_dir.mkdir(parents=True, exist_ok=False)
            if path.exists():
                (backup_dir / 'disk-before.kicad_pcb').write_bytes(path.read_bytes())
            client, board, document = self._board(open_if_closed=False)
            if document is not None and fingerprint([self.socket, str(document), client._client._kicad_token]) != expected['session_id']:
                raise ValueError('PCB 编辑器会话在保存前已改变，未发出保存请求')
            if board.get_as_string() != expected['text']:
                raise ValueError('PCB 在保存前已改变，未发出保存请求')
            (backup_dir / 'expected-live.json').write_text(json.dumps(expected, ensure_ascii=False), encoding='utf-8')
            notify('save', '正在保存 PCB（包括此前未保存的编辑）')
            save_error = None
            try:
                save_document_once(client, board, notify)
            except Exception as exc:
                save_error = exc
            timed_out = save_error is not None and (isinstance(save_error, TimeoutError) or
                any(word in str(save_error).lower() for word in ('timed out', 'timeout')))
            deadline = time.monotonic() + (SAVE_RECONCILE_SECONDS if timed_out else 0)
            wanted = saved_board_data(expected['text'])
            actual, error = None, str(save_error or '')
            while True:
                notify('save_reconcile' if timed_out else 'save_verify',
                    '保存回复超时，正在只读核验磁盘与 PCB 实时内容；不会重复写入' if timed_out else '正在核验 PCB 磁盘文件与实时内容')
                try:
                    actual = self.snapshot(self.root, refs, open_if_closed=False)
                    if actual != expected:
                        error = 'PCB 实时内容或编辑器会话已改变'
                        break
                    if path.is_file() and saved_board_data(path.read_text(encoding='utf-8-sig')) == wanted:
                        if save_error is None or timed_out:
                            notify('save_confirmed', 'PCB 已保存，磁盘与实时内容核验完成' + ('（保存回复超时已恢复）' if timed_out else ''))
                            return {'ok':True, 'saved':True, 'path':str(path), 'backup_dir':str(backup_dir),
                                    'reply_timeout_recovered':timed_out}
                    error = str(save_error or 'PCB 磁盘文件与实时内容不一致')
                except Exception as exc:
                    error = str(exc)
                if not timed_out or time.monotonic() >= deadline:
                    break
                time.sleep(1)
            if actual is not None:
                (backup_dir / 'actual-live.json').write_text(json.dumps(actual, ensure_ascii=False), encoding='utf-8')
            if path.is_file():
                (backup_dir / 'disk-after.kicad_pcb').write_bytes(path.read_bytes())
            (backup_dir / 'save-error.json').write_text(json.dumps({'save_error':str(save_error or ''),
                'verification_error':error}, ensure_ascii=False), encoding='utf-8')
            raise PcbSaveUnverified('PCB 更新及实时回读已完成，但保存结果未确认；已保留编辑器，不会重复更新或关闭工程。'
                '请检查保存对话框或磁盘错误。详情：' + error + '；恢复材料：' + str(backup_dir), backup_dir)

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
                actual_value, expected_value = new[key], expected[key]
                if key == 'pads':
                    actual_value = sorted(actual_value, key=lambda pad: pad['number'])
                    expected_value = sorted(expected_value, key=lambda pad: pad['number'])
                if actual_value != expected_value:
                    raise ValueError(ref + ' 写入回读不一致：' + key + '；实际 ' + str(new[key])[:160] + '；预期 ' + str(expected[key])[:160])
            a, b = decode(old['proto']), decode(new['proto'])
            changed = {name.casefold() for name in item.get('fields', {})}
            for field in ('id', 'symbol_path', 'layer', 'locked', 'reference_field', 'value_field',
                          'datasheet_field', 'description_field', 'attributes', 'overrides',
                          'symbol_sheet_name', 'symbol_sheet_filename', 'symbol_footprint_filters'):
                if field.removesuffix('_field') in changed and field in (
                        'value_field','description_field','datasheet_field'):
                    expected_field = deepcopy(getattr(b.proto, field))
                    expected_field.text.text.text = getattr(a.proto, field).text.text.text
                    if expected_field != getattr(a.proto, field):
                        raise ValueError(ref + ' 字段位置或样式改变：' + field)
                elif getattr(a.proto, field) != getattr(b.proto, field):
                    raise ValueError(ref + ' 实例信息改变：' + field)
            from kipy.board_types import Field
            preserved = {item.name: item.proto for item in a.definition.items if isinstance(item, Field)}
            actual_fields = {item.name: item.proto for item in b.definition.items if isinstance(item, Field)}
            for name, value in preserved.items():
                actual_field = deepcopy(actual_fields.get(name))
                if actual_field is not None and name.casefold() in changed:
                    actual_field.text.text.text = value.text.text.text
                if actual_field != value:
                    raise ValueError(ref + ' 自定义字段或文字位置改变')
            if footprint_bytes(b) != footprint_bytes(decode(item['proto'])):
                raise ValueError(ref + ' 完整封装定义回读不一致，可能有图形、模型或属性未保留')
        return {'ok': True}

    def apply(self, snapshot: dict, replacements: list[dict], backup_dir: Path, *, schematic_close_verified=False, progress=None) -> dict:
        notify = progress or (lambda *args: None)
        with self.lock:
            refs = list(snapshot['parts'])
            if not refs or len(replacements) != len(refs) or {item.get('ref') for item in replacements} != set(refs):
                raise ValueError('更新清单必须与预检选中器件完全一致，不能重复或扩大范围')
            current = self.snapshot(self.root, refs)
            if current != snapshot:
                raise ValueError('预览后 PCB 已改变，请重新预览')
            for item in replacements:
                validate_locked_update(current['parts'][item['ref']], item)
            backup_dir = Path(backup_dir).resolve()
            if backup_dir.is_relative_to(self.root):
                raise ValueError('PCB 恢复材料必须放在工程外')
            backup_dir.mkdir(parents=True, exist_ok=False)
            notify('backup', '正在保存本次 PCB 事务的恢复材料（工程外）')
            (backup_dir / 'before.kicad_pcb').write_text(current['text'], encoding='utf-8')
            (backup_dir / 'transaction.json').write_text(json.dumps({
                'before': current, 'replacements': replacements}, ensure_ascii=False), encoding='utf-8')
            prepared = [decode(item['proto']) for item in replacements]
            for item, fp in zip(replacements, prepared):
                original = snapshot['parts'][item['ref']]
                if fp.id.value != original['uuid'] or fp.reference_field.text.value != item['ref']:
                    raise ValueError('待写入封装身份与选中器件不符，未修改 PCB')
            validate_item_ids(snapshot, prepared)
            client, board, _ = self._board()
            # Finish every source check before the first mutation.
            if self.snapshot(self.root, refs) != snapshot:
                raise ValueError('备份期间 PCB 已改变，请重新预览')
            require_safe_commit_route(schematic_close_verified)
            notify('commit', '正在向 KiCad 提交所选器件更新')
            commit = board.begin_commit()
            try:
                staged = board.update_items(prepared)
                if sorted(footprint_bytes(item) for item in staged) != sorted(footprint_bytes(item) for item in prepared):
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
            notify('readback', '正在回读核验位置、方向、焊盘、网络及未选对象')
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
                    if attempt == 2 and 'actual' in locals():
                        try:
                            (backup_dir / 'failed-readback.json').write_text(json.dumps(actual, ensure_ascii=False), encoding='utf-8')
                        except OSError:
                            pass
                    if attempt < 2:
                        time.sleep(.15)
            # KiCad 10 exposes no atomic undo-stack ownership check. A generic
            # undo here could remove a newer user edit: leave the editor dirty
            # and recovery materials intact rather than claiming a rollback.
            raise RuntimeError(f'PCB 更新回读或恢复未确认；请检查编辑器并停止重复更新。'
                               f'未自动撤销其他编辑。恢复目录：{backup_dir}；{last_error}') from publish_error


class _Unpublished(Exception):
    pass
