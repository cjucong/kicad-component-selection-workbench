"""Pure comparisons of verified selected-stock specs with saved passive fields.

The caller owns exact C-code verification and NC/selection policy. Nothing here
reads CAD, resolves libraries, looks up stock, or updates inputs or caches.
"""

from collections.abc import Mapping
from decimal import Decimal
import re


__all__ = ["validate_passive_specs"]

_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_SCALE = {"": 0, "R": 0, "r": 0, "p": -12, "n": -9, "u": -6,
          "m": -3, "k": 3, "K": 3, "M": 6, "G": 9}
_UNITS = {"R": r"(?:[Rr]|\u03a9|(?i:ohms?))", "C": r"[Ff]", "L": r"[Hh]"}
_LABELS = {
    "R": {"resistance", "nominal resistance", "\u963b\u503c", "\u7535\u963b\u503c"},
    "C": {"capacitance", "capacity", "nominal capacitance", "\u5bb9\u503c", "\u5bb9\u91cf",
          "\u7535\u5bb9\u503c", "\u7535\u5bb9\u91cf", "\u7535\u5bb9\u5bb9\u91cf"},
    "L": {"inductance", "nominal inductance", "\u611f\u503c", "\u7535\u611f\u503c", "\u7535\u611f\u91cf"},
}
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
          "1806", "1812", "2010", "2512"}
_METRIC = {"0402": "01005", "0603": "0201", "1005": "0402", "1608": "0603",
           "2012": "0805", "3216": "1206", "3225": "1210", "4532": "1812",
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
    current = (rf"|(?i:(?:Rated Current|Saturation Current)\s*[:=]\s*"
               rf"(?:{_NUMBER}\s*[mun]?A|not specified))") if kind == "L" else ""
    token = re.compile(rf"(?:{tolerance}|{rating}|{dielectric}|{size}{current})(?![\w.])")
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


def validate_passive_specs(row, stock, actual_by_ref=None):
    """Compare selected stock's nominal value/size with a passive grouped row.

    ``expected`` is the schematic requested field; ``actual`` is the selected
    stock spec. Stock must carry ``ok=True``; the caller verifies C-code binding.
    If ``actual_by_ref`` is supplied, even an empty map, absent refs/fields remain
    unknown; unsaved row edits are never substituted. Without it the row is the
    source. Issues sharing a field and the same text are grouped by reference.
    ``mismatch`` takes precedence over ``unknown``. ``matched`` requires both
    fields to be comparable for every member. Unknown fields are ordered value,
    footprint. Complex footprints are deliberately not compared, even verbatim.
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
    if not any(kind for _, _, kind in members):
        return result
    if stock.get("ok") is not True:
        return {"status": "unknown", "issues": [], "unknown_fields": ["value", "footprint"]}
    unknown = set()
    issues = {}
    stock_values = {kind: _stock_nominal(stock, kind) for _, _, kind in members if kind in _UNITS}
    stock_package = _package(stock.get("package"))
    for ref, fields, kind in members:
        if kind not in _UNITS:
            unknown.update(("value", "footprint"))
            continue
        stock_value = stock_values[kind]
        schematic_value = _nominal(fields.get("value"), kind)
        comparisons = (
            ("value", schematic_value[0] if schematic_value else None,
             stock_value[0] if stock_value else None, stock_value[1] if stock_value else ""),
            ("footprint", _package(fields.get("footprint")), stock_package, _text(stock.get("package"))),
        )
        for field, expected, actual, stock_text in comparisons:
            if expected is None or actual is None:
                unknown.add(field)
            elif expected != actual:
                expected_text = _text(fields.get(field))
                key = (field, expected_text, stock_text)
                issue = issues.setdefault(key, {"field": field, "refs": [],
                                                "expected": expected_text, "actual": stock_text})
                if ref:
                    issue["refs"].append(ref)
    result["issues"] = sorted(issues.values(), key=lambda issue: issue["field"] != "value")
    result["unknown_fields"] = [field for field in ("value", "footprint") if field in unknown]
    result["status"] = "mismatch" if issues else "unknown" if unknown else "matched"
    return result
