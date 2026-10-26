"""Read-only, identity-checked domestic LCSC product-page parameters.

Live schema (2026-10-22): __NEXT_DATA__.props.pageProps.webData contains
productRecord, currentCatalog.catalogName, and paramList entries with
parameterName/parameterDetailValue/parameterValue. Recommendation payloads
are deliberately never traversed. Only the standard library is required.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser


_ITEM_URL = 'https://item.szlcsc.com/{}.html'
_MAX_BYTES = 4 * 1024 * 1024
_CATALOG = '\u5546\u54c1\u76ee\u5f55'
_CODE_LABEL = '\u5546\u54c1\u7f16\u53f7'
_MODEL_LABEL = '\u5546\u54c1\u578b\u53f7'


class _DetailCache:
    """Copy-safe LRU; refresh callers also share bounded in-flight work."""

    def __init__(self, max_entries=256, max_inflight=4):
        self._entries = OrderedDict()
        self._inflight = {}
        self._condition = threading.Condition()
        self._max_entries = max_entries
        self._max_inflight = max_inflight

    def get(self, key, loader, force=False, wait_timeout=6):
        deadline = time.monotonic() + wait_timeout
        with self._condition:
            while True:
                cached = self._entries.get(key)
                if cached is not None and not force:
                    expires, value = cached
                    if time.monotonic() < expires:
                        self._entries.move_to_end(key)
                        return deepcopy(value)
                    del self._entries[key]
                pending = self._inflight.get(key)
                if pending is not None:
                    owner = False
                    break
                if len(self._inflight) < self._max_inflight:
                    pending = Future()
                    self._inflight[key] = pending
                    owner = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Product-detail request capacity wait timed out')
                self._condition.wait(remaining)
        if not owner:
            return deepcopy(pending.result(timeout=max(0, deadline - time.monotonic())))
        try:
            value = deepcopy(loader())
            with self._condition:
                ttl = 600 if value.get('ok') else 20
                self._entries[key] = (time.monotonic() + ttl, value)
                self._entries.move_to_end(key)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            pending.set_result(value)
            return deepcopy(value)
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self._condition:
                del self._inflight[key]
                self._condition.notify_all()


_CACHE = _DetailCache()


def _text(value):
    return str(value).strip() if isinstance(value, (str, int, float)) else ''


def _clean(value):
    return ' '.join(_text(value).split())


def _product_id(value):
    value = _text(value)
    if not re.fullmatch(r'[0-9]{1,20}', value) or not value.strip('0'):
        return ''
    return value.lstrip('0')


def _result(code, model, pid, status, error=''):
    return {
        'ok': status == 'ok', 'c_code': code, 'model': model, 'product_id': pid,
        'parameters': [], 'param_detail': '',
        'detail_url': _ITEM_URL.format(pid) if pid else '',
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'status': status, 'error': error,
    }


class _ItemRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reject before urllib contacts another host, path, or product.
        if newurl != req.full_url:
            raise ValueError('Product-detail redirect to a different URL is blocked')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_page(pid, timeout):
    url = _ITEM_URL.format(pid)
    request = urllib.request.Request(url, headers={
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Accept-Encoding': 'identity',
        'Referer': 'https://so.szlcsc.com/',
    })
    opener = urllib.request.build_opener(_ItemRedirect())
    with opener.open(request, timeout=timeout) as response:
        if response.geturl() != url:
            raise ValueError('Unexpected product-detail response URL')
        if response.status != 200:
            raise ValueError(f'HTTP {response.status} from product-detail page')
        content_type = response.headers.get_content_type()
        if content_type not in ('text/html', 'application/xhtml+xml'):
            raise ValueError(f'Unexpected product-detail content type: {content_type}')
        length = response.headers.get('Content-Length')
        if length and int(length) > _MAX_BYTES:
            raise ValueError('Product-detail response exceeds 4 MiB')
        body = response.read(_MAX_BYTES + 1)
        if len(body) > _MAX_BYTES:
            raise ValueError('Product-detail response exceeds 4 MiB')
        return body.decode(response.headers.get_content_charset() or 'utf-8')


class _Node:
    def __init__(self, tag='', attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def text(self):
        parts, stack = [], list(reversed(self.children))
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag == 'br':
                parts.append(' ')
            else:
                stack.extend(reversed(item.children))
        return _clean(''.join(parts))


def _walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in reversed(node.children) if isinstance(child, _Node))


class _PageParser(HTMLParser):
    _VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
             'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self._stack = [self.root]
        self._nodes = 0

    def handle_starttag(self, tag, attrs):
        self._nodes += 1
        if self._nodes > 100000 or len(self._stack) > 256:
            raise ValueError('Product-detail HTML structure exceeds parser limits')
        node = _Node(tag, attrs)
        self._stack[-1].children.append(node)
        if tag not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data):
        self._stack[-1].children.append(data)


class _IdentityMismatch(ValueError):
    pass


def parse_search_dispatch(page):
    """Read shipping labels only from identity-bearing domestic product cards."""
    parser = _PageParser()
    parser.feed(page)
    parser.close()
    found = {}
    for card in _walk(parser.root):
        try:
            identity = json.loads(card.attrs.get('data-custom-data') or '{}')
        except (ValueError, TypeError):
            continue
        if not isinstance(identity, dict) or identity.get('productType') != 'main':
            continue
        pid = _product_id(identity.get('productId'))
        sections = [node for node in card.children if isinstance(node, _Node)
                    and node.tag == 'section' and node.attrs.get('data-spm') == 'zy']
        if not pid or len(sections) != 1:
            continue
        nodes = list(_walk(sections[0]))
        codes, models, labels = set(), set(), set()
        for node in nodes:
            if node.tag == 'dl':
                terms = [part.text() for part in node.children if isinstance(part, _Node) and part.tag == 'dt']
                values = [part.text() for part in node.children if isinstance(part, _Node) and part.tag == 'dd']
                if terms == ['编号'] and len(values) == 1 and re.fullmatch(r'C[0-9]+', values[0]):
                    codes.add(values[0])
            if node.tag == 'a' and node.attrs.get('data-spm') == 'n':
                url = urllib.parse.urlsplit(node.attrs.get('href', ''))
                if (url.scheme == 'https' and url.netloc == 'item.szlcsc.com'
                        and url.path == f'/{pid}.html' and node.text()):
                    models.add(node.text())
            if node.tag == 'span' and 'whitespace-pre-line' in node.attrs.get('class', '').split():
                text = node.text()
                if len(text) <= 240 and re.search(r'\d+.*(?:小时|天|工作日|周).*发货|\d+.*工作日', text):
                    labels.add(text)
        if len(codes) == len(models) == len(labels) == 1:
            key = (pid, next(iter(codes)), next(iter(models)))
            found.setdefault(key, set()).update(labels)
    return {key: next(iter(labels)) for key, labels in found.items() if len(labels) == 1}


def _check_identity(code, model, pid, record):
    actual_code = _text(record.get('productCode')).upper()
    actual_model = _text(record.get('productModel'))
    actual_pid = _text(record.get('productId'))
    if (actual_code != code or not actual_model or (model and actual_model != model)
            or (actual_pid and _product_id(actual_pid) != pid)):
        raise _IdentityMismatch('Primary product identity does not match the requested C code, model, or product ID')
    return actual_model


def _parse_page(page, code, model, pid):
    parser = _PageParser()
    parser.feed(page)
    parser.close()
    nodes = list(_walk(parser.root))
    web, record = {}, None
    for node in nodes:
        if node.tag != 'script' or node.attrs.get('id') != '__NEXT_DATA__':
            continue
        try:
            data = json.loads(''.join(child for child in node.children if isinstance(child, str)))
            candidate = data['props']['pageProps']['webData']
        except (ValueError, TypeError, KeyError):
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get('productRecord'), dict):
            if record is not None:
                raise ValueError('Multiple primary product SSR records are unavailable for safe parsing')
            web, record = candidate, candidate['productRecord']

    actual_model = _check_identity(code, model, pid, record) if record is not None else ''
    section = next((node for node in nodes if node.attrs.get('id') == 'productParamsTabItem'), None)
    # Only identity definition lists preceding the main parameter section count.
    html_records = []
    for node in nodes:
        if node is section:
            break
        if node.tag != 'dl':
            continue
        fields, label = {}, ''
        for part in _walk(node):
            if part.tag == 'dt':
                label = part.text()
            elif part.tag == 'dd' and label:
                fields[label] = part.text()
                label = ''
        if _CODE_LABEL in fields:
            html_records.append({'productCode': fields[_CODE_LABEL],
                                 'productModel': fields.get(_MODEL_LABEL, '')})
    for identity in html_records:
        rendered_model = _check_identity(code, model or actual_model, pid, identity)
        actual_model = actual_model or rendered_model
    if not actual_model:
        raise ValueError('Primary product identity unavailable in the product-detail page')

    pairs, seen = [], set()

    def add(name, value):
        name, value = _clean(name), _clean(value)
        if name and (name, value) not in seen:
            pairs.append({'name': name, 'value': value})
            seen.add((name, value))

    catalog = web.get('currentCatalog')
    if isinstance(catalog, dict) and _clean(catalog.get('catalogName')):
        add(_CATALOG, catalog['catalogName'])
    params = web.get('paramList')
    if isinstance(params, list):
        for item in params:
            if not isinstance(item, dict):
                continue
            value = item.get('parameterDetailValue')
            if value is None:
                value = item.get('parameterValue')
            if value is not None:
                add(item.get('parameterName'), value)
    if section is not None:
        structured_names = {pair['name'] for pair in pairs}
        for row in _walk(section):
            if row.tag != 'tr':
                continue
            cells = [cell for cell in row.children if isinstance(cell, _Node) and cell.tag == 'td']
            if len(cells) in (2, 3):
                name, value = cells[-2].text(), cells[-1].text()
                if name not in structured_names:
                    add(name, value)
    if not any(pair['name'] != _CATALOG for pair in pairs):
        raise ValueError('Full product parameter source unavailable or unrecognized; vendor parameters are not known to be absent')
    return actual_model, pairs


def _resolve_product_id(code, model, timeout, force_refresh):
    # Lazy import: stock lookup may import this module during UI integration.
    from part_stock_lookup import fetch_szlcsc_domestic_products

    records = fetch_szlcsc_domestic_products(code, timeout=timeout, force_refresh=force_refresh)
    matches = set()
    for record in records or []:
        if not isinstance(record, dict) or _text(record.get('productCode')).upper() != code:
            continue
        if model and _text(record.get('productModel')) != model:
            continue
        pid = _product_id(record.get('productId'))
        if pid:
            matches.add(pid)
    return next(iter(matches)) if len(matches) == 1 else ''


def fetch_product_details(c_code, product_id='', model='', timeout=6, force_refresh=False) -> dict:
    """Return all verified page attributes, or an explicit non-success status.

    URLs are constructed only from numeric product IDs on item.szlcsc.com.
    Omitted IDs resolve through exact domestic search matches. Results have
    UTC checked_at timestamps and independent nested copies. The process-local
    256-entry cache lives for 600 s on success or 20 s on failure; force_refresh
    bypasses completed entries, not in-flight requests. No files are written.
    """
    code, requested_id, model = _text(c_code).upper(), _text(product_id), _text(model)
    pid = _product_id(requested_id)
    try:
        valid_timeout = float(timeout)
    except (ValueError, TypeError, OverflowError):
        valid_timeout = 0
    if (not re.fullmatch(r'C[0-9]+', code) or (requested_id and not pid)
            or not math.isfinite(valid_timeout) or valid_timeout <= 0):
        return _result(code, model, '', 'invalid_request', 'Expected an exact C code, numeric product ID, and finite positive timeout')

    def load():
        resolved = pid
        try:
            if not resolved:
                resolved = _resolve_product_id(code, model, valid_timeout, force_refresh)
                if not resolved:
                    return _result(code, model, '', 'not_found', 'No unambiguous exact domestic product ID match is available')
            page = _read_page(resolved, valid_timeout)
            actual_model, pairs = _parse_page(page, code, model, resolved)
            result = _result(code, actual_model, resolved, 'ok')
            result['parameters'] = pairs
            result['param_detail'] = '; '.join(f"{pair['name']}: {pair['value']}" for pair in pairs)
            return result
        except _IdentityMismatch as exc:
            return _result(code, model, resolved, 'identity_mismatch', str(exc))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return _result(code, model, resolved, 'failed', f'Product-detail fetch failed: {exc}')
        except (ValueError, TypeError, LookupError, RecursionError) as exc:
            return _result(code, model, resolved, 'unavailable', f'Product-detail source unavailable: {exc}')

    try:
        return _CACHE.get((code, pid, model), load, force=force_refresh, wait_timeout=valid_timeout)
    except TimeoutError:
        return _result(code, model, pid, 'failed', 'Product-detail request wait timed out')
