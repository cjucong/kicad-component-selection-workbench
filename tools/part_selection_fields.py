"""Project-scoped schematic property names shared by reads and writes."""
from collections import Counter


SCHEMATIC_FIELD_PROPERTIES = {
    "comment": "Comment", "value": "Value", "footprint": "Footprint",
    "lcsc_c_code": "LCSC", "lcsc_url": "LCSC_URL", "description": "Description",
    "lcsc_alt": "LCSC_ALT", "lcsc_alt_info": "LCSC_ALT_INFO",
    "procurement_url": "PROCUREMENT_URL", "procurement_info": "PROCUREMENT_INFO",
}
FIELD_LABELS = {
    "comment": "Comment / 器件类型", "value": "Value / 值", "footprint": "KiCad 封装",
    "lcsc_c_code": "立创 C 编码", "lcsc_url": "立创商品链接", "description": "物料参数",
    "lcsc_alt": "备选料编码", "lcsc_alt_info": "备选料信息",
    "procurement_url": "订货链接", "procurement_info": "订货渠道信息",
}
ALIASES = {
    "comment": ("注释", "备注", "器件类型"),
    "value": ("值", "数值", "元件值"),
    "footprint": ("封装", "封装标识", "封装库标识"),
    "description": ("参数详情", "物料参数", "参数", "描述"),
    "lcsc_c_code": ("LCSC Part #", "LCSC Part", "LCSC_Part", "LCSC Part Number", "立创编号", "立创料号", "C编码"),
    "lcsc_url": ("立创链接", "立创商品链接"),
    "lcsc_alt": ("备选料编码",), "lcsc_alt_info": ("备选料信息",),
    "procurement_url": ("订货链接",), "procurement_info": ("订货渠道信息",),
}


def normalize_field_mapping(mapping):
    if mapping is None:
        return {}
    if not isinstance(mapping, dict) or set(mapping) - SCHEMATIC_FIELD_PROPERTIES.keys():
        raise ValueError("字段映射包含不支持的字段")
    result, used = {}, {}
    protected = {"reference", "datasheet", "位号"}
    for key, value in mapping.items():
        if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value):
            raise ValueError("字段名称必须是 128 字以内的单行文字")
        value = value.strip()
        if not value:
            continue
        folded = value.casefold()
        other = next((k for k, name in SCHEMATIC_FIELD_PROPERTIES.items() if name.casefold() == folded), None)
        if folded in protected or (other and other != key):
            raise ValueError(f"字段映射不能占用其他内置字段：{value}")
        if folded in used:
            raise ValueError(f"多个用途不能映射到同一字段：{value}")
        result[key], used[folded] = value, key
    return result


def resolve_property(props, key, mapping=None):
    """Prefer a populated standard property; never guess among conflicting aliases."""
    if mapping and key in mapping:
        requested = mapping[key]
        if requested in props:
            return requested
        matches = [name for name in props if name.casefold() == requested.casefold()]
        if len(matches) > 1:
            raise ValueError(f"字段名称大小写不唯一，请精确指定：{requested}")
        return matches[0] if matches else requested
    default = SCHEMATIC_FIELD_PROPERTIES[key]
    names = (default, *ALIASES.get(key, ()))
    aliases = {name.casefold() for name in names}
    reserved = {name.casefold() for other, name in (mapping or {}).items() if other != key}
    candidates = [name for name in props if name.casefold() in aliases and name.casefold() not in reserved]
    populated = [name for name in candidates if props[name].strip()]
    if default in populated:
        return default
    if len({props[name].strip() for name in populated}) > 1:
        raise ValueError(f"{FIELD_LABELS[key]} 有多个冲突候选字段（{'、'.join(populated)}），请在工程设置中手动映射")
    for expected in names:
        for name in populated or candidates:
            if name.casefold() == expected.casefold():
                return name
    return default


def resolve_properties(props, mapping=None):
    resolved = {key: resolve_property(props, key, mapping) for key in SCHEMATIC_FIELD_PROPERTIES}
    targets = [name.casefold() for name in resolved.values()]
    duplicates = [name for name, count in Counter(targets).items() if count > 1]
    if duplicates:
        raise ValueError(f"字段映射发生重叠，请手动指定不同字段：{'、'.join(duplicates)}")
    return resolved


def field_mapping_snapshot(directory, mapping=None):
    from part_selection_helper import iter_symbol_blocks, parse_symbol_properties

    mapping = normalize_field_mapping(mapping)
    inventory, symbols = {}, []
    for path in sorted(directory.glob('*.kicad_sch')):
        for block in iter_symbol_blocks(path.read_text(encoding='utf-8')):
            props = parse_symbol_properties(block)
            if not props.get('Reference') or props['Reference'].startswith('#'):
                continue
            symbols.append(props)
            for name, value in props.items():
                item = inventory.setdefault(name, {'name': name, 'count': 0, 'samples': []})
                item['count'] += 1
                if value and value not in item['samples'] and len(item['samples']) < 2:
                    item['samples'].append(value[:100])
    resolutions = []
    for props in symbols:
        names, errors = {}, set()
        for key in SCHEMATIC_FIELD_PROPERTIES:
            try:
                names[key] = resolve_property(props, key, mapping)
            except ValueError:
                errors.add(key)
        counts = Counter(name.casefold() for name in names.values())
        errors.update(key for key, name in names.items() if counts[name.casefold()] > 1)
        resolutions.append((props, names, errors))
    fields = []
    for key, default in SCHEMATIC_FIELD_PROPERTIES.items():
        matched, missing, conflicts = Counter(), 0, 0
        for props, names, errors in resolutions:
            if key in errors:
                conflicts += 1
                continue
            name = names[key]
            if name in props:
                matched[name] += 1
            else:
                missing += 1
        fields.append({'key': key, 'label': FIELD_LABELS[key], 'default': default,
                       'matched': dict(matched), 'missing': missing, 'conflicts': conflicts})
    return {'mapping': mapping, 'properties': sorted(inventory.values(), key=lambda item: item['name'].casefold()),
            'fields': fields, 'symbol_count': len(symbols)}
