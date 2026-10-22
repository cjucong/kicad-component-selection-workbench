"""Pure comparisons of verified selected-stock specs with saved passive fields.

The caller owns exact C-code verification and NC/selection policy. Nothing here
reads CAD, resolves libraries, looks up stock, or updates inputs or caches.
"""

from collections.abc import Mapping
from decimal import Decimal
import re


__all__ = ["validate_passive_specs"]

_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_TH_BODY_SIZE = re.compile(rf"(?:插件|直插)\s*[,，]\s*D\s*{_NUMBER}\s*[x×]\s*L?\s*{_NUMBER}\s*mm", re.I)
_SCALE = {"": 0, "R": 0, "r": 0, "p": -12, "n": -9, "u": -6,
          "m": -3, "k": 3, "K": 3, "M": 6, "G": 9}
_UNITS = {"R": r"(?:[Rr]|\u03a9|(?i:ohms?))", "C": r"[Ff]", "L": r"[Hh]"}
_LABELS = {
    "R": {"resistance", "nominal resistance", "\u963b\u503c", "\u7535\u963b\u503c"},
    "C": {"capacitance", "capacity", "nominal capacitance", "\u5bb9\u503c", "\u5bb9\u91cf",
          "\u7535\u5bb9\u503c", "\u7535\u5bb9\u91cf", "\u7535\u5bb9\u5bb9\u91cf"},
    "L": {"inductance", "nominal inductance", "\u611f\u503c", "\u7535\u611f\u503c", "\u7535\u611f\u91cf"},
}
_DIELECTRIC_LABELS = {'dielectric', 'temperature coefficient', 'temperature characteristic',
                      '\u6e29\u5ea6\u7cfb\u6570', '\u6e29\u5ea6\u7279\u6027', '\u4ecb\u8d28', '\u7535\u4ecb\u8d28'}
_DIELECTRIC = re.compile(r'(?<![\w-])(?:C0G|NP0|X[5678][RSTUV]|Y5V|Z5U)(?![\w-])', re.I)
_KIND_WORDS = {
    "R": r"\bresistor\b|\bRES\b|\u7535\u963b",
    "C": r"\bcapacitor\b|\bCAP\b|\u7535\u5bb9",
    "L": r"\binductor\b|\bIND\b|\u7535\u611f",
}
_SPECIAL = re.compile(
    r"\b(?:NTC|PTC|thermistor|varistor|ferrite|array|network)\b|"
    r"\u70ed\u654f|\u538b\u654f|\u78c1\u73e0|\u6392\u963b|\u6392\u5bb9|\u9635\u5217", re.I,
)
_SIZES = {"01005", "0201", "0402", "0603", "0805", "1206", "1210",
          "1806", "1808", "1812", "2010", "2512"}
_METRIC = {"0402": "01005", "0603": "0201", "1005": "0402", "1608": "0603",
           "2012": "0805", "3216": "1206", "3225": "1210", "4520": "1808", "4532": "1812",
           "5025": "2010", "6332": "2512"}
_CHINESE_UNITS = {
    "\u5146\u6b27\u59c6": "M\u03a9", "\u5343\u6b27\u59c6": "k\u03a9", "\u6beb\u6b27\u59c6": "m\u03a9",
    "\u5fae\u6b27\u59c6": "u\u03a9", "\u6b27\u59c6": "\u03a9", "\u5146\u6b27": "M\u03a9",
    "\u5343\u6b27": "k\u03a9", "\u6beb\u6b27": "m\u03a9", "\u5fae\u6b27": "u\u03a9", "\u6b27": "\u03a9",
    "\u5fae\u6cd5\u62c9": "uF", "\u7eb3\u6cd5\u62c9": "nF", "\u76ae\u6cd5\u62c9": "pF",
    "\u6beb\u6cd5\u62c9": "mF", "\u6cd5\u62c9": "F", "\u5fae\u6cd5": "uF", "\u7eb3\u6cd5": "nF",
    "\u76ae\u6cd5": "pF", "\u6beb\u6cd5": "mF", "\u6cd5": "F", "\u5fae\u4ea8\u5229": "uH",
    "\u6beb\u4ea8\u5229": "mH", "\u7eb3\u4ea8\u5229": "nH", "\u4ea8\u5229": "H",
    "\u5fae\u4ea8": "uH", "\u6beb\u4ea8": "mH", "\u7eb3\u4ea8": "nH", "\u4ea8": "H",
}


def _text(value):
    return str(value).strip() if isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool) else ""


def _references(row):
    source = row.get("refs_list") or row.get("refs") or row.get("ref")
    if not source:
        source = [link.get("ref") for link in row.get("ref_links", []) if isinstance(link, Mapping)]
    if isinstance(source, str):
        source = re.split(r"[,;\s\uff0c\uff1b]+", source)
    if not isinstance(source, (list, tuple)):
        return []
    refs = list(dict.fromkeys(_text(ref).upper() for ref in source if _text(ref)))
    return sorted(refs, key=lambda ref: [int(piece) if piece.isdigit() else piece
                                       for piece in re.split(r"(\d+)", ref)])


def _kind(ref, fields):
    comment = _text(fields.get("comment"))
    match = re.fullmatch(r"([RCL])\d+", ref, re.I)
    kinds = {kind for kind, pattern in _KIND_WORDS.items() if re.search(pattern, comment, re.I)}
    if match:
        kinds.add(match.group(1).upper())
    if _SPECIAL.search(comment) or re.match(r"(?:RN|CN|RA|CA)\d+$", ref, re.I):
        return "ambiguous"
    if len(kinds) > 1:
        return "ambiguous"
    return next(iter(kinds), None)


def _normalized_value(text):
    text = text.replace("\u03bc", "u").replace("\u00b5", "u").replace("\u2126", "\u03a9")
    text = text.replace('\uff0c', ',').replace('\uff1b', ';')
    for name, unit in _CHINESE_UNITS.items():
        text = text.replace(name, unit)
    return text


def _qualifiers_only(tail, kind):
    # Consume only recognized ratings/tolerances; leftover text can hide another
    # value, a range, an array count, or a part number, so it is not comparable.
    tolerance = rf"(?:\+/-|\u00b1)\s*{_NUMBER}\s*(?:%|[pnumkKM]?{_UNITS[kind]})"
    rating = rf"(?:{_NUMBER}(?:/{_NUMBER})?)\s*(?:[munkK]?(?i:V(?:AC|DC)?|A|W)|%)"
    dielectric = r"(?:X[5678][RSTUV]|C0G|NP0|Y5V|Z5U)"
    size = "(?:" + "|".join(sorted(_SIZES)) + ")"
    current = (rf"|(?i:(?:RMS(?: Current)?|Rated Current|温升电流|额定电流|"
               rf"Sat(?: Current)?|Saturation Current|饱和电流(?:\s*[(（]Isat[)）])?|Isat)\s*[:=]\s*"
               rf"(?:{_NUMBER}\s*[mun]?A|not specified))") if kind == "L" else ""
    tcr = (rf"|(?i:(?:TCR\s*[:=]\s*)?(?:\+/-|\u00b1|[+-])?\s*{_NUMBER}\s*ppm"
           rf"(?:\s*/\s*(?:\u2103|\u00b0C|K))?)(?![\w./])") if kind == "R" else ""
    inductor = (rf"|(?i:DCR\s*[:=]\s*{_NUMBER}\s*[munkKM]?{_UNITS['R']})"
                r"|(?i:Type\s*[:=]\s*[a-z\u4e00-\u9fff][a-z\u4e00-\u9fff -]*)") if kind == "L" else ""
    token = re.compile(rf"(?:{tolerance}|{rating}|{dielectric}|{size}{current}{tcr}{inductor})(?![\w.])")
    while tail:
        tail = tail.lstrip(" \t\r\n/,;()")
        if not tail:
            return True
        match = token.match(tail)
        if not match:
            return False
        tail = tail[match.end():]
    return True


def _nominal(value, kind, *, bare_resistance=True):
    original = _text(value)
    text = _normalized_value(original)
    if not text or _SPECIAL.search(text):
        return None
    embedded_prefix = "RrkKMmunpG" if kind == "R" else "pnumkKMG"
    embedded = re.match(rf"(?P<whole>\d+)(?P<prefix>[{embedded_prefix}])(?P<fraction>\d+)(?:{_UNITS[kind]})?", text)
    if embedded:
        number = Decimal(embedded["whole"] + "." + embedded["fraction"])
        prefix = embedded["prefix"]
        end = embedded.end()
    else:
        match = re.match(rf"(?P<number>{_NUMBER})\s*(?P<prefix>[pnumkKMG]?)(?P<unit>{_UNITS[kind]})?", text)
        if not match:
            return None
        prefix = match["prefix"]
        if not prefix and not match["unit"] and not (kind == "R" and bare_resistance):
            return None
        number = Decimal(match["number"])
        end = match.end()
    if not _qualifiers_only(text[end:], kind):
        return None
    # Keep stock's spelling for the warning instead of exposing base-unit decimals.
    return number * (Decimal(10) ** _SCALE[prefix]), original


def _parameter_entries(value):
    if isinstance(value, Mapping):
        return [(_text(key).casefold(), _text(item)) for key, item in value.items()]
    if isinstance(value, (list, tuple)):
        entries = []
        for item in value:
            if isinstance(item, Mapping):
                entries.append((_text(item.get("paramName") or item.get("name")).casefold(),
                                _text(item.get("paramValue") or item.get("value"))))
        return entries
    entries = []
    for segment in re.split(r"[;\uff1b\r\n]+|[,\uff0c](?=\s*[^:\uff1a,\uff0c;\uff1b]+[:\uff1a])", _text(value)):
        pair = re.split(r"[:\uff1a]", segment, maxsplit=1)
        if len(pair) == 2:
            entries.append((pair[0].strip().casefold(), pair[1].strip()))
    return entries


def _stock_nominal(stock, kind):
    sources = [stock.get("param_detail"), stock.get("param")]
    if any(_SPECIAL.search(_text(source)) for source in sources):
        return None
    entries = [entry for source in sources for entry in _parameter_entries(source)]
    values = [value for label, value in entries if label in _LABELS[kind]]
    if values:
        parsed = [_nominal(value, kind) for value in values]
        if any(value is None for value in parsed) or len({value[0] for value in parsed}) != 1:
            return None
        return parsed[0]
    if any(label in labels for label, _ in entries for labels in _LABELS.values()):
        return None
    candidates = []
    for source in sources:
        if not isinstance(source, str) or _parameter_entries(source):
            continue
        text = source.strip()
        kinds = {item for item, pattern in _KIND_WORDS.items() if re.search(pattern, text, re.I)}
        if kinds and kinds != {kind}:
            continue
        heading = {"R": r"(?:RES|RESISTOR)(?:\s+(?:SMD|CHIP|THICK FILM|THIN FILM))*",
                   "C": r"(?:CAP|CAPACITOR)(?:\s+(?:CER|CERAMIC|MLCC))*",
                   "L": r"(?:IND|INDUCTOR)(?:\s+(?:SMD|POWER|CHIP))*"}[kind]
        text = re.sub(rf"^{heading}\s+", "", text, flags=re.I)
        result = _nominal(text, kind, bare_resistance=False)
        if result is not None:
            candidates.append(result)
    if not candidates or len({value[0] for value in candidates}) != 1:
        return None
    return candidates[0]


def _package(value):
    text = _text(value)
    name = text.rsplit(":", 1)[-1]
    if name in _SIZES:
        return name
    match = re.fullmatch(r"([0-9]{4})Metric", name, re.I)
    if match:
        return _METRIC.get(match[1])
    match = re.fullmatch(r"[RCL]_?(\d{4,5})(?:_(\d{4})Metric)?", name, re.I)
    if match and match[1] in _SIZES:
        if match[2] and _METRIC.get(match[2]) != match[1]:
            return None
        return match[1]
    return None


def _schematic_nominal(value, kind, model):
    text, model = _text(value), _text(model)
    if model and text.startswith(model):
        tail = text[len(model):].lstrip()
        if tail.startswith((',', ';', '\uff0c', '\uff1b')):
            text = tail[1:].strip()
    return _nominal(text, kind)


def _dielectrics(text):
    return list(dict.fromkeys('C0G' if item.upper() == 'NP0' else item.upper()
                             for item in _DIELECTRIC.findall(_text(text))))


def _stock_dielectric(stock):
    sources = [stock.get('param_detail'), stock.get('param')]
    entries = [entry for source in sources for entry in _parameter_entries(source)]
    values = [value for label, value in entries if label in _DIELECTRIC_LABELS]
    if values:
        parsed = [_dielectrics(value) for value in values]
        if all(len(items) == 1 for items in parsed) and len({items[0] for items in parsed}) == 1:
            return parsed[0][0], ' / '.join(values)
        return None, ' / '.join(value or '(\u7a7a)' for value in values)
    parsed = {item for source in sources if isinstance(source, str) and not _parameter_entries(source)
              for item in _dielectrics(source)}
    return (next(iter(parsed)), next(iter(parsed))) if len(parsed) == 1 else (None, '')


def _unknown_message(field, source, fields, stock, kind):
    if field == 'footprint':
        value = _text(fields.get('footprint') if source == 'schematic' else stock.get('package'))
        origin = '\u5df2\u4fdd\u5b58\u539f\u7406\u56fe Footprint' if source == 'schematic' else '\u5546\u54c1\u5c01\u88c5'
        if source != 'schematic' and _TH_BODY_SIZE.fullmatch(value):
            return (origin + '\u300c' + value + '\u300d名称不足以确定焊盘布局；\n'
                    '名称未包含焊盘间距、钻孔直径和引脚方向，需核对数据手册或实际封装')
        return (origin + ' \u672a\u586b\u5199' if not value else
                origin + '\u300c' + value + '\u300d\u4e0d\u662f\u53ef\u81ea\u52a8\u6838\u5bf9\u7684\u6807\u51c6\u5c3a\u5bf8\u522b\u540d\uff0c\u9700\u4eba\u5de5\u6838\u5bf9\u5c01\u88c5\u51e0\u4f55')
    label = {'C': '\u5bb9\u503c', 'R': '\u963b\u503c', 'L': '\u611f\u503c'}[kind]
    if source == 'schematic':
        value = _text(fields.get('value'))
        return ('\u5df2\u4fdd\u5b58\u539f\u7406\u56fe Value \u672a\u586b\u5199\uff0c\u7f3a\u5c11' + label if not value else
                '\u5df2\u4fdd\u5b58\u539f\u7406\u56fe Value\u300c' + value + '\u300d\u65e0\u6cd5\u552f\u4e00\u8bc6\u522b' + label)
    values = [value for item in ('param_detail', 'param')
              for name, value in _parameter_entries(stock.get(item)) if name in _LABELS[kind]]
    if values:
        parsed = [_nominal(value, kind) for value in values]
        missing = {'', '-', '--', 'n/a', 'none', '\u672a\u63d0\u4f9b'}
        reason = ('\u672a\u63d0\u4f9b' if all(value.casefold() in missing for value in values) else
                  '\u591a\u9879\u51b2\u7a81' if all(parsed) and len({item[0] for item in parsed}) > 1 else
                  '\u65e0\u6cd5\u552f\u4e00\u8bc6\u522b')
        return '\u5546\u54c1' + label + reason + '\uff1a' + ' / '.join(value or '(\u7a7a)' for value in values)
    summary = _text(stock.get('param'))
    return '\u5546\u54c1\u672a\u63d0\u4f9b\u53ef\u552f\u4e00\u8bc6\u522b\u7684' + label + ('\uff1b\u6458\u8981\uff1a\u300c' + summary + '\u300d' if summary else '')


def _unknown_missing(field, source, fields, stock, kind):
    """Return the concrete information still needed for an unknown check."""
    if field == 'footprint':
        value = _text(fields.get('footprint') if source == 'schematic' else stock.get('package'))
        if source != 'schematic' and _TH_BODY_SIZE.fullmatch(value):
            return ['焊盘间距', '钻孔直径', '引脚方向']
        return ['可识别的封装几何', '焊盘数量与编号', '焊盘相对坐标和尺寸']
    if field == 'value':
        return ['可唯一识别的标称值']
    if field == 'dielectric':
        return ['温度系数 / 介质']
    return []


def _assessment(status, unknown_fields, details):
    """Explain a read-only check without granting replacement compatibility."""
    if status == 'matched':
        return {
            'confidence': 'checked_fields_only',
            'decision': 'check_only',
            'category': 'checked_fields_match',
            'summary': '已核对字段相符，不代表完整电气或封装兼容',
            'missing': [],
            'recommended_action': '封装替换仍需按实际焊盘布局及器件数据手册确认',
        }
    if status == 'mismatch':
        return {
            'confidence': 'conflict',
            'decision': 'review_conflict',
            'category': 'specification_conflict',
            'summary': '已核对字段存在明确差异，不是单纯信息不足',
            'missing': [],
            'recommended_action': '请先核实下列原理图要求与商品参数差异，再决定是否换料；此提示不自动修改选型',
        }
    missing = []
    for detail in details.values():
        for item in detail.get('missing', []):
            if item not in missing:
                missing.append(item)
    for field in unknown_fields:
        if any(detail.get('field') == field and detail.get('missing') for detail in details.values()):
            continue
        fallback = {
            'footprint': ['可识别的封装几何', '焊盘数量与编号', '焊盘相对坐标和尺寸'],
            'value': ['可唯一识别的标称值'],
            'dielectric': ['温度系数 / 介质'],
        }.get(field, [field])
        for item in fallback:
            if item not in missing:
                missing.append(item)
    category = 'footprint_geometry_missing' if unknown_fields == ['footprint'] else 'specification_insufficient'
    return {
        'confidence': 'insufficient',
        'decision': 'keep_existing',
        'category': category,
        'summary': '封装名称不足以判断兼容性' if unknown_fields == ['footprint'] else '核对信息不足，不能判定一致或不符',
        'missing': missing,
        'recommended_action': ('保留当前本地封装；如需替换，请核对转换封装或数据手册中的焊盘编号、坐标、尺寸及通孔钻孔。仅改名不证明兼容'
                               if 'footprint' in unknown_fields else '补充下列缺失信息后重新核对；不因问号修改原有确认状态'),
    }


def validate_passive_specs(row, stock, actual_by_ref=None, *, explain=False):
    """Compare selected stock's nominal value/size with a passive grouped row.

    ``expected`` is the schematic requested field; ``actual`` is the selected
    stock spec. Stock must carry ``ok=True``; the caller verifies C-code binding.
    If ``actual_by_ref`` is supplied, even an empty map, absent refs/fields remain
    unknown; unsaved row edits are never substituted. Without it the row is the
    source. Issues sharing a field and the same text are grouped by reference.
    ``mismatch`` takes precedence over ``unknown``. ``matched`` requires both
    fields to be comparable for every member. Unknown fields are ordered value,
    footprint, dielectric. Dielectric is checked only when explicitly requested.
    Complex footprints are deliberately not compared, even verbatim. With
    ``explain=True``, unknown_details names each source, raw value, and reason.
    """
    row = row if isinstance(row, Mapping) else {}
    stock = stock if isinstance(stock, Mapping) else {}
    refs = _references(row)
    members = []
    for ref in refs or [""]:
        fields = row if actual_by_ref is None else actual_by_ref.get(ref, {})
        fields = fields if isinstance(fields, Mapping) else {}
        # References can identify a missing passive, but missing saved fields
        # cannot inherit an edited comment or nominal value.
        kind = _kind(ref, fields)
        if kind is None and actual_by_ref is not None and "comment" not in fields and _kind(ref, row):
            kind = "ambiguous"
        members.append((ref, fields, kind))
    result = {"status": "not_applicable", "issues": [], "unknown_fields": []}
    details = {}

    def unavailable(field, source, ref, message):
        detail = details.setdefault((field, source, message), {
            'field': field, 'source': source, 'refs': [], 'message': message,
            'missing': _unknown_missing(field, source, fields, stock, kind)})
        if ref and ref not in detail['refs']:
            detail['refs'].append(ref)

    if explain:
        result['unknown_details'] = []
    if not any(kind for _, _, kind in members):
        return result
    if stock.get("ok") is not True:
        result.update(status='unknown', unknown_fields=['value', 'footprint'])
        if explain:
            result['unknown_details'] = [{'field': field, 'source': 'stock', 'refs': refs[:],
                'message': '\u5f53\u524d C \u7f16\u7801\u5c1a\u65e0\u5df2\u9a8c\u8bc1\u5546\u54c1' + label +
                ('\uff1b\u67e5\u8be2\u9519\u8bef\uff1a' + _text(stock.get('error')) if stock.get('error') else ''),
                'missing': _unknown_missing(field, 'stock', {}, stock, None)}
                for field, label in [('value', '\u6807\u79f0\u503c\u53c2\u6570'), ('footprint', '\u5c01\u88c5\u4fe1\u606f')]]
            result['assessment'] = _assessment(result['status'], result['unknown_fields'],
                                               {item['field']: item for item in result['unknown_details']})
        return result
    unknown = set()
    issues = {}
    stock_values = {kind: _stock_nominal(stock, kind) for _, _, kind in members if kind in _UNITS}
    stock_package = _package(stock.get("package"))
    for ref, fields, kind in members:
        if kind not in _UNITS:
            unknown.update(("value", "footprint"))
            unavailable('value', 'schematic', ref, '\u539f\u7406\u56fe\u5668\u4ef6\u7c7b\u578b\u65e0\u6cd5\u552f\u4e00\u786e\u5b9a\uff0c\u4e0d\u80fd\u6309\u666e\u901a\u963b\u5bb9\u611f\u6838\u5bf9\uff1bComment\uff1a' +
                        (_text(fields.get('comment')) or '(\u672a\u586b\u5199)'))
            unavailable('footprint', 'schematic', ref, '\u539f\u7406\u56fe\u5668\u4ef6\u7c7b\u578b\u65e0\u6cd5\u552f\u4e00\u786e\u5b9a\uff0c\u4e0d\u80fd\u6309\u666e\u901a\u963b\u5bb9\u611f\u5c3a\u5bf8\u6838\u5bf9')
            continue
        stock_value = stock_values[kind]
        schematic_value = _schematic_nominal(fields.get("value"), kind, stock.get('model'))
        comparisons = [
            ("value", schematic_value[0] if schematic_value else None,
             stock_value[0] if stock_value else None, stock_value[1] if stock_value else ""),
            ("footprint", _package(fields.get("footprint")), stock_package, _text(stock.get("package"))),
        ]
        requested = _dielectrics(fields.get('value')) if kind == 'C' else []
        if requested:
            actual_dielectric, raw_dielectric = _stock_dielectric(stock)
            comparisons.append(('dielectric', requested[0] if len(requested) == 1 else None,
                                actual_dielectric, raw_dielectric))
        for field, expected, actual, stock_text in comparisons:
            if expected is None or actual is None:
                unknown.add(field)
                for source, value in (('schematic', expected), ('stock', actual)):
                    if value is not None:
                        continue
                    if field == 'dielectric':
                        message = ('\u539f\u7406\u56fe Value \u4e2d\u6e29\u5ea6\u7cfb\u6570 / \u4ecb\u8d28\u8981\u6c42\u4e0d\u552f\u4e00\uff1a' + ' / '.join(requested)
                                   if source == 'schematic' else
                                   '\u539f\u7406\u56fe\u8981\u6c42\u6e29\u5ea6\u7cfb\u6570 / \u4ecb\u8d28 ' + ' / '.join(requested) +
                                   '\uff1b\u5546\u54c1\u6e29\u5ea6\u7cfb\u6570 / \u4ecb\u8d28\u672a\u63d0\u4f9b\u6216\u65e0\u6cd5\u552f\u4e00\u8bc6\u522b' +
                                   ('\uff1a' + raw_dielectric if raw_dielectric else '\uff08\u672a\u5217\u51fa\uff09'))
                    else:
                        message = _unknown_message(field, source, fields, stock, kind)
                    unavailable(field, source, ref, message)
            elif expected != actual:
                expected_text = ' / '.join(requested) if field == 'dielectric' else _text(fields.get(field))
                key = (field, expected_text, stock_text)
                issue = issues.setdefault(key, {"field": field, "refs": [],
                                                "expected": expected_text, "actual": stock_text})
                if ref:
                    issue["refs"].append(ref)
    result["issues"] = sorted(issues.values(), key=lambda issue: issue["field"] != "value")
    result["unknown_fields"] = [field for field in ("value", "footprint", "dielectric") if field in unknown]
    if explain:
        result['unknown_details'] = list(details.values())
    result["status"] = "mismatch" if issues else "unknown" if unknown else "matched"
    if explain:
        result['assessment'] = _assessment(result['status'], result['unknown_fields'], details)
    return result
