"""Synthetic fixtures mirror the read-only item.szlcsc.com SSR probe (2026-10-22)."""

import importlib
import importlib.util
import io
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
MODULE = "part_selection_product_details"
CODE, MODEL, PID = "C860136", "RT0603BRB0715RL", "920474"
URL = "https://item.szlcsc.com/920474.html"
PAIRS = [
    ("\u5546\u54c1\u76ee\u5f55", "\u8d34\u7247\u7535\u963b"),
    ("\u7535\u963b\u7c7b\u578b", "\u8584\u819c\u7535\u963b"),
    ("\u963b\u503c", "15\u03a9"),
    ("\u7cbe\u5ea6", "\u00b10.1%"),
    ("\u529f\u7387", "100mW"),
    ("\u5de5\u4f5c\u7535\u538b", "75V"),
    ("\u6e29\u5ea6\u7cfb\u6570", "\u00b110ppm/\u2103"),
    ("\u5de5\u4f5c\u6e29\u5ea6", "-55\u2103~+155\u2103"),
]


def ssr(code=CODE, model=MODEL, pid=PID, params=None):
    return {"props": {"pageProps": {
        "webData": {
            "productRecord": {"productCode": code, "productModel": model, "productId": pid},
            "currentCatalog": {"catalogName": PAIRS[0][1]},
            "paramList": [
                {"parameterName": name, "parameterDetailValue": value,
                 "parameterValue": "raw value is not the display value"}
                for name, value in (PAIRS[1:] if params is None else params)
            ],
        },
        "similarProductRecommend": [
            {"productCode": CODE, "productModel": MODEL, "productId": PID,
             "paramList": [{"parameterName": "recommendation-only", "parameterDetailValue": "999V"}]}
        ],
    }}}


def table(pairs):
    return ('<table><thead><tr><th></th><th>\u5c5e\u6027</th><th>\u53c2\u6570\u503c</th>'
            '</tr></thead><tbody>' + ''.join(
                f'<tr><td><input type="checkbox"></td><td><span>{name}</span></td>'
                f'<td><span>{value}</span></td></tr>' for name, value in pairs
            ) + '</tbody></table>')


def html(data=None, pairs=None, code=CODE, model=MODEL):
    identity = (f'<h1>{model}</h1><dl><div><dt>\u5546\u54c1\u578b\u53f7</dt><dd>{model}</dd></div>'
                f'<div><dt>\u5546\u54c1\u7f16\u53f7</dt><dd>{code}</dd></div></dl>')
    tables = '' if pairs is None else ('<section id="productParamsTabItem"><h2>\u5546\u54c1\u53c2\u6570</h2>'
                                        + table(pairs[:4]) + table(pairs[4:]) + '</section>')
    script = '' if data is None else '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>'
    return ('<!doctype html><html><body>' + identity + tables
            + '<aside id="recommendations">' + table([("recommendation-only", "999V")])
            + '</aside>' + script + '</body></html>')


class Response(io.BytesIO):
    def __init__(self, body, url=URL, status=200, content_type="text/html; charset=utf-8"):
        super().__init__(body.encode('utf-8') if isinstance(body, str) else body)
        self.url, self.status = url, status
        self.headers = Message()
        self.headers['Content-Type'] = content_type

    def geturl(self):
        return self.url


@pytest.fixture
def details(monkeypatch):
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, '_CACHE', module._DetailCache())
    return module


@pytest.fixture
def transport(details, monkeypatch):
    opener = Mock()
    monkeypatch.setattr(details.urllib.request, 'build_opener', Mock(return_value=opener))

    def serve(page=None, **kwargs):
        opener.open.side_effect = None
        opener.open.return_value = Response(html(ssr()) if page is None else page, **kwargs)
        return opener.open

    serve.opener = opener
    return serve


def fetch(details, **kwargs):
    return details.fetch_product_details(CODE, product_id=PID, model=MODEL, **kwargs)


def test_module_exists_before_implementation():
    assert importlib.util.find_spec(MODULE) is not None, 'isolated detail-fetch module has not been implemented'


def test_actual_ssr_shape_returns_every_attribute(details, transport):
    request = transport()
    result = fetch(details)
    assert result['ok'] and result['status'] == 'ok'
    assert result['parameters'] == [{'name': n, 'value': v} for n, v in PAIRS]
    assert result['param_detail'] == '; '.join(f'{n}: {v}' for n, v in PAIRS)
    assert (result['c_code'], result['model'], result['product_id']) == (CODE, MODEL, PID)
    assert result['detail_url'] == URL and result['checked_at']
    req = request.call_args.args[0]
    assert req.full_url == URL and request.call_args.kwargs['timeout'] == 6
    assert req.get_header('Accept-encoding') == 'identity'
    assert req.get_header('Referer') == 'https://so.szlcsc.com/'
    assert 'Chrome/142' in req.get_header('User-agent')


def test_page_product_code_binds_both_full_parameter_tables(details, transport):
    data = ssr()
    del data['props']['pageProps']['webData']['paramList']
    transport(html(data, PAIRS))
    result = fetch(details)
    assert result['ok']
    assert result['parameters'] == [{'name': n, 'value': v} for n, v in PAIRS]


def test_html_only_identity_and_scoped_tables(details, transport):
    transport(html(pairs=PAIRS))
    assert fetch(details)['parameters'] == [{'name': n, 'value': v} for n, v in PAIRS]


def test_corrupt_ssr_can_use_verified_html(details, transport):
    transport(html(pairs=PAIRS) + '<script id="__NEXT_DATA__">{broken</script>')
    assert fetch(details)['ok']


@pytest.mark.parametrize('changes', [dict(code='C8601360'), dict(model=MODEL+'X'), dict(pid='920475'),
                                         dict(code=''), dict(model='')])
def test_primary_identity_mismatch_never_uses_recommendations_or_tables(details, transport, changes):
    transport(html(ssr(**changes), PAIRS))
    result = fetch(details)
    assert not result['ok'] and result['status'] == 'identity_mismatch'
    assert result['parameters'] == [] and result['param_detail'] == '' and result['error']


def test_html_identity_conflicting_with_ssr_is_rejected(details, transport):
    transport(html(ssr(), PAIRS, code='C999'))
    assert fetch(details)['status'] == 'identity_mismatch'


def test_model_optional_is_observed_not_invented(details, transport):
    transport()
    result = details.fetch_product_details(' c860136 ', PID)
    assert result['ok'] and result['model'] == MODEL and result['c_code'] == CODE


def test_parameter_values_are_not_split_and_zero_is_preserved(details, transport):
    data = ssr(params=[('conditions', 'A; B: C / D'), ('zero', 0), ('unspecified', '-')])
    data['props']['pageProps']['webData']['paramList'].extend([
        {'parameterName': 'fallback', 'parameterValue': '5V'}, None,
        {'parameterName': 'zero', 'parameterDetailValue': 0},
    ])
    transport(html(data))
    assert fetch(details)['parameters'][1:] == [
        {'name': 'conditions', 'value': 'A; B: C / D'}, {'name': 'zero', 'value': '0'},
        {'name': 'unspecified', 'value': '-'}, {'name': 'fallback', 'value': '5V'},
    ]


def test_ssr_values_preferred_and_table_can_supply_missing_attributes(details, transport):
    transport(html(ssr(params=PAIRS[1:4]), PAIRS + [(PAIRS[2][0], 'wrong')]))
    assert fetch(details)['parameters'] == [{'name': n, 'value': v} for n, v in PAIRS]


def test_recommendations_alone_are_not_a_primary_product(details, transport):
    data = ssr()
    del data['props']['pageProps']['webData']
    transport('<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>' + table(PAIRS))
    result = fetch(details)
    assert not result['ok'] and result['parameters'] == []


@pytest.mark.parametrize('data', [{}, [], None, {'props': []}, {'props': {'pageProps': None}}])
def test_unknown_ssr_shapes_are_unavailable_not_no_vendor_parameters(details, transport, data):
    transport('<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>')
    result = fetch(details)
    assert result['status'] == 'unavailable' and result['error'] and not result['ok']


def test_verified_page_without_parameter_source_is_unavailable(details, transport):
    data = ssr(params=[])
    del data['props']['pageProps']['webData']['currentCatalog']
    transport(html(data))
    result = fetch(details)
    assert result['status'] == 'unavailable' and result['parameters'] == []


def test_catalog_alone_is_not_a_full_parameter_source(details, transport):
    transport(html(ssr(params=[])))
    result = fetch(details)
    assert result['status'] == 'unavailable' and not result['ok']


def test_resolver_failure_is_reported_without_a_page_fetch(details, transport, monkeypatch):
    monkeypatch.setitem(sys.modules, 'part_stock_lookup', SimpleNamespace(
        fetch_szlcsc_domestic_products=Mock(side_effect=OSError('resolver offline'))))
    request = transport()
    result = details.fetch_product_details(CODE, model=MODEL)
    assert result['status'] == 'failed' and 'resolver offline' in result['error']
    request.assert_not_called()


def test_numeric_product_id_is_canonicalized(details, transport):
    request = transport()
    result = details.fetch_product_details(CODE, '000920474', model=MODEL)
    assert result['ok'] and result['product_id'] == PID
    assert request.call_args.args[0].full_url == URL


def test_huge_declared_length_is_rejected_before_read(details, transport):
    response = Response(html(ssr()))
    response.headers['Content-Length'] = str(4 * 1024 * 1024 + 1)
    response.read = Mock(wraps=response.read)
    transport.opener.open.return_value = response
    assert '4 MiB' in fetch(details)['error']
    response.read.assert_not_called()


def test_deeply_nested_page_reports_unavailable(details, transport):
    transport('<div>' * 300 + html(ssr()) + '</div>' * 300)
    assert fetch(details)['status'] == 'unavailable'


def test_negative_cache_can_be_refreshed(details, transport):
    transport.opener.open.side_effect = TimeoutError('offline')
    assert not fetch(details)['ok']
    request = transport()
    assert not fetch(details)['ok']
    assert request.call_count == 1
    assert fetch(details, force_refresh=True)['ok']


def test_missing_model_cannot_produce_success_without_requested_model(details, transport):
    transport(html(ssr(model=''), code=CODE, model=''))
    assert not details.fetch_product_details(CODE, PID)['ok']


@pytest.mark.parametrize('pid', ['https://127.0.0.1/x', '../920474', '920474.html', '920474?x=1',
                                 '1/../../2', '//evil.test', '\uff19\uff12', '0', '-1'])
def test_product_id_cannot_be_a_url_or_path(details, transport, pid):
    request = transport()
    result = details.fetch_product_details(CODE, pid)
    assert not result['ok'] and result['status'] == 'invalid_request'
    request.assert_not_called()
    assert result['detail_url'] == ''


@pytest.mark.parametrize('code', ['C860136 extra', 'https://evil.test/C860136', 'C860136/C1', '', 'C\uff11'])
def test_code_requires_whole_exact_ascii_identity(details, transport, code):
    request = transport()
    assert details.fetch_product_details(code, PID)['status'] == 'invalid_request'
    request.assert_not_called()


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan'), 'bad'])
def test_timeout_must_be_finite_positive(details, transport, timeout):
    request = transport()
    assert fetch(details, timeout=timeout)['status'] == 'invalid_request'
    request.assert_not_called()


def test_lazy_resolver_selects_exact_code_and_model_only(details, transport, monkeypatch):
    resolver = Mock(return_value=[
        {'productCode': CODE+'0', 'productModel': MODEL, 'productId': '11'},
        {'productCode': CODE, 'productModel': MODEL+'X', 'productId': '22'},
        {'productCode': CODE, 'productModel': MODEL, 'productId': PID},
    ])
    monkeypatch.setitem(sys.modules, 'part_stock_lookup', SimpleNamespace(fetch_szlcsc_domestic_products=resolver))
    transport()
    result = details.fetch_product_details(CODE, model=MODEL, force_refresh=True)
    assert result['ok'] and result['product_id'] == PID
    resolver.assert_called_once_with(CODE, timeout=6, force_refresh=True)


@pytest.mark.parametrize('records', [[], [{'productCode': CODE+'0', 'productId': PID}],
                                    [{'productCode': CODE, 'productId': 'https://evil.test'}],
                                    [{'productCode': CODE, 'productId': PID}, {'productCode': CODE, 'productId': '99'}]])
def test_no_exact_unambiguous_resolver_match_makes_no_page_request(details, transport, monkeypatch, records):
    monkeypatch.setitem(sys.modules, 'part_stock_lookup', SimpleNamespace(
        fetch_szlcsc_domestic_products=Mock(return_value=records)))
    request = transport()
    assert not details.fetch_product_details(CODE)['ok']
    request.assert_not_called()


@pytest.mark.parametrize('error, fragment', [
    (HTTPError(URL, 405, 'Not Allowed', {}, None), '405'),
    (URLError('offline'), 'offline'), (TimeoutError('timed out'), 'timed out'),
])
def test_network_failures_explain_failure_and_retain_source(details, transport, error, fragment):
    transport.opener.open.side_effect = error
    result = fetch(details)
    assert not result['ok'] and result['status'] == 'failed'
    assert fragment in result['error'] and result['detail_url'] == URL
    assert result['parameters'] == [] and result['checked_at']


def test_response_has_a_four_megabyte_read_limit(details, transport):
    response = Response(b'x' * (4 * 1024 * 1024 + 1))
    response.read = Mock(wraps=response.read)
    transport.opener.open.return_value = response
    result = fetch(details)
    assert not result['ok'] and '4 MiB' in result['error']
    response.read.assert_called_once_with(4 * 1024 * 1024 + 1)


def test_non_html_and_final_url_changes_are_rejected(details, transport):
    transport(html(ssr()), content_type='application/json')
    assert not fetch(details)['ok']
    transport(html(ssr()), url='https://127.0.0.1/920474.html')
    assert not fetch(details, force_refresh=True)['ok']


@pytest.mark.parametrize('target', ['http://item.szlcsc.com/920474.html', 'https://127.0.0.1/x',
                                    'https://item.szlcsc.com.evil.test/920474.html',
                                    'https://item.szlcsc.com/920475.html'])
def test_redirect_is_blocked_before_any_followup_fetch(details, target):
    handler = details._ItemRedirect()
    with pytest.raises(ValueError, match='redirect'):
        handler.redirect_request(Request(URL), None, 302, 'Found', {}, target)


def test_positive_cache_is_copy_safe_and_force_refresh_bypasses_it(details, transport):
    request = transport()
    result = fetch(details)
    result['parameters'][0]['value'] = 'tampered'
    assert fetch(details)['parameters'][0]['value'] == PAIRS[0][1]
    assert request.call_count == 1
    transport()
    assert fetch(details, force_refresh=True)['ok']
    assert request.call_count == 2


def test_cache_identity_includes_requested_model_and_product_id(details, transport):
    transport()
    assert fetch(details)['ok']
    transport()
    assert details.fetch_product_details(CODE, PID, MODEL+'X')['status'] == 'identity_mismatch'


def test_cache_has_separate_positive_and_negative_ttls(details, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(details.time, 'monotonic', lambda: now[0])
    cache = details._DetailCache()
    good, bad = Mock(return_value={'ok': True}), Mock(return_value={'ok': False})
    cache.get('positive', good)
    cache.get('negative', bad)
    now[0] += 21
    cache.get('positive', good)
    cache.get('negative', bad)
    assert good.call_count == 1 and bad.call_count == 2
    now[0] += 580
    cache.get('positive', good)
    assert good.call_count == 2


def test_cache_lru_is_bounded_to_256_entries(details):
    cache = details._DetailCache()
    loader = Mock(return_value={'ok': True})
    for i in range(256):
        cache.get(i, loader)
    cache.get(0, loader)
    cache.get(256, loader)
    cache.get(0, loader)
    assert loader.call_count == 257
    cache.get(1, loader)
    assert loader.call_count == 258


def test_concurrent_forced_requests_singleflight(details, transport, monkeypatch):
    started, release, followers_ready = threading.Event(), threading.Event(), threading.Event()
    follower_lock = threading.Lock()
    follower_count = 0

    def load(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return Response(html(ssr()))

    transport.opener.open.side_effect = load
    with ThreadPoolExecutor(max_workers=6) as pool:
        first = pool.submit(fetch, details, force_refresh=True)
        assert started.wait(2)
        shared = next(iter(details._CACHE._inflight.values()))
        original = shared.result

        def waiting(*args, **kwargs):
            nonlocal follower_count
            with follower_lock:
                follower_count += 1
                if follower_count == 5:
                    followers_ready.set()
            return original(*args, **kwargs)

        monkeypatch.setattr(shared, 'result', waiting)
        others = [pool.submit(fetch, details, force_refresh=True) for _ in range(5)]
        try:
            assert followers_ready.wait(2)
        finally:
            release.set()
        results = [first.result()] + [future.result() for future in others]
    assert all(result['ok'] for result in results)
    assert transport.opener.open.call_count == 1


def test_pending_limit_and_waiter_timeout_are_bounded(details):
    cache = details._DetailCache(max_inflight=1)
    entered, release = threading.Event(), threading.Event()

    def load():
        entered.set()
        assert release.wait(3)
        return {'ok': True}

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(cache.get, 'first', load)
        assert entered.wait(2)
        try:
            with pytest.raises(TimeoutError):
                cache.get('other', load, wait_timeout=0.01)
            with pytest.raises(TimeoutError):
                cache.get('first', load, wait_timeout=0.01)
        finally:
            release.set()
        assert pending.result()['ok']
