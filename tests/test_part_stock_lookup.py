import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from part_stock_lookup import (  # noqa: E402
    enrich_rows_with_cached_stock,
    enrich_rows_with_stock,
    load_stock_cache,
    lookup_stock_with_cache,
    normalize_c_code,
    parse_lcsc_product,
    parse_szlcsc_item_price,
    parse_szlcsc_next_data,
    parse_szlcsc_overseas_channels,
    parse_szlcsc_partner_data,
    parse_szlcsc_partner_response,
    product_param_summary,
    product_order_channels,
    query_lcsc_stock,
    search_lcsc_candidates,
    build_candidate_item,
    candidate_match,
    extract_model_tokens,
    format_price_ladder,
    stock_status,
)
import part_stock_lookup as stock_lookup  # noqa: E402


class FetchCacheTest(unittest.TestCase):
    def test_two_domestic_reads_run_in_isolated_profiles_without_serial_queue(self):
        simultaneous, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        profiles = []

        def browser(command, **kwargs):
            profile = next((Path(arg.split('=',1)[1]) for arg in command if arg.startswith('--user-data-dir=')), None)
            with lock:
                profiles.append(profile)
                if len(profiles) == 2:
                    simultaneous.set()
            self.assertTrue(release.wait(2))
            return SimpleNamespace(stdout=b'<html>fixture</html>')

        with patch.object(stock_lookup,'find_chrome_executable',return_value='chrome'), \
                patch.object(stock_lookup.subprocess,'run',side_effect=browser), \
                ThreadPoolExecutor(max_workers=2) as executor:
            reads=[executor.submit(stock_lookup._fetch_szlcsc_page, code, 8) for code in ('C100','C200')]
            try:
                self.assertTrue(simultaneous.wait(.5),'different-code domestic reads still serialize')
            finally:
                release.set()
                for read in reads:
                    read.result(timeout=2)
        self.assertEqual(len(set(profiles)),2)
        self.assertTrue(all(p is not None and not p.exists() for p in profiles))

    def test_stock_read_completion_at_wait_timeout_returns_real_result(self):
        from concurrent.futures import Future, TimeoutError

        class FinishesAtTimeout(Future):
            def result(self, timeout=None):
                value = super().result(timeout=timeout)
                if timeout is not None:
                    raise TimeoutError('wait expired just before completion')
                return value

        with patch.object(stock_lookup, 'Future', FinishesAtTimeout):
            result = stock_lookup.StockQueryGroup().run(
                lambda: stock_lookup._run_stock_read(lambda code: {'c_code': code}, 'C100'))
        self.assertEqual(result, {'c_code': 'C100'})

    def test_shared_completion_at_wait_timeout_returns_real_result(self):
        from concurrent.futures import Future, TimeoutError

        class FinishesAtTimeout(Future):
            def result(self, timeout=None):
                value = super().result(timeout=timeout)
                if timeout is not None:
                    raise TimeoutError('wait expired just before completion')
                return value

        cache = stock_lookup._FetchCache()
        pending = FinishesAtTimeout()
        pending.set_result(['completed'])
        cache._inflight[('same', 8)] = pending
        self.assertEqual(cache.get(('same', 8), lambda: self.fail('unexpected fetch')), ['completed'])

    def test_completed_source_timeout_is_not_retried_as_a_wait_timeout(self):
        from concurrent.futures import Future, TimeoutError
        cache = stock_lookup._FetchCache()
        pending = Future()
        pending.set_exception(TimeoutError('source timed out'))
        cache._inflight[('same', 8)] = pending
        with self.assertRaisesRegex(TimeoutError, 'source timed out'):
            cache.get(('same', 8), lambda: self.fail('unexpected fetch'))

    def test_shared_owner_cancel_at_timeout_boundary_retries_independent_search(self):
        from concurrent.futures import CancelledError, Future, TimeoutError
        cache = stock_lookup._FetchCache()
        key = ('same', 8)
        pending = Future()
        pending.set_exception(CancelledError())
        cache._inflight[key] = pending

        def result(timeout=None):
            if timeout is not None:
                del cache._inflight[key]
                raise TimeoutError('wait expired just before cancellation')
            raise CancelledError()

        with patch.object(pending, 'result', side_effect=result):
            self.assertEqual(cache.get(key, lambda: ['independent search']), ['independent search'])

    def test_batch_cancellation_drains_queued_and_active_reads_without_cache_writes(self):
        from concurrent.futures import CancelledError
        from _scratch import temporary_project
        group = stock_lookup.StockQueryGroup()
        started, release = threading.Event(), threading.Event()
        finished = threading.Event()
        calls, completed = [], []
        lock = threading.Lock()

        def fetch(code):
            with lock:
                calls.append(code)
                if len(calls) == 4:
                    started.set()
            try:
                self.assertTrue(release.wait(3))
                return {'ok': True, 'c_code': code, 'stock': '123'}
            finally:
                with lock:
                    completed.append(code)
                    if len(completed) == 4:
                        finished.set()

        with temporary_project(prefix='cancel-stock-batch-') as root:
            path = Path(root) / 'stock.json'
            rows = [{'effective_c_code': 'C'+str(code)} for code in range(100, 108)]
            with ThreadPoolExecutor(max_workers=1) as executor:
                request = executor.submit(group.run,
                    lambda: stock_lookup.enrich_rows_with_stock(rows, path, fetcher=fetch))
                try:
                    self.assertTrue(started.wait(2))
                    group.cancel()
                    with self.assertRaises(CancelledError):
                        request.result(timeout=1)
                    self.assertFalse(path.exists())
                    self.assertEqual(len(calls), 4)
                finally:
                    release.set()
                    self.assertTrue(finished.wait(2))
            self.assertFalse(path.exists())

    def test_partner_fetch_reuses_results_without_sharing_nested_quotes(self):
        cache = type(stock_lookup._PARTNER_PRODUCT_CACHE)()
        payload = json.dumps({"code": 200, "result": [{
            "provider": "waldom", "isLcscBookShow": True,
            "products": [{"productCode": "C5136981", "tieredPrices": [{"price": "623.95"}]}],
        }]}).encode()
        with patch.object(stock_lookup, "_PARTNER_PRODUCT_CACHE", cache), patch(
            "part_stock_lookup.urllib.request.urlopen"
        ) as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = payload
            first = stock_lookup.fetch_szlcsc_partner_products(" AD4630-24BBCZ ")
            first[0]["tieredPrices"][0]["price"] = "changed"
            second = stock_lookup.fetch_szlcsc_partner_products("AD4630-24BBCZ")

        self.assertEqual(second[0]["tieredPrices"][0]["price"], "623.95")
        self.assertEqual(urlopen.call_count, 1)

    def test_domestic_products_refresh_when_page_cache_expires(self):
        cache = type(stock_lookup._DOMESTIC_PAGE_CACHE)()
        fixture = PROJECT_ROOT / "tests" / "fixtures" / "szlcsc_c6929829_next_data.html"
        page = fixture.read_bytes()
        with patch.object(stock_lookup, "_DOMESTIC_PAGE_CACHE", cache), patch(
            "part_stock_lookup.find_chrome_executable", return_value="chrome"
        ), patch("part_stock_lookup.subprocess.run", return_value=SimpleNamespace(stdout=page)) as run, patch(
            "time.monotonic", return_value=100.0
        ) as now:
            first = stock_lookup.fetch_szlcsc_domestic_products("C6929829")
            first[0]["cnyProductPriceList"][0]["productPrice"] = "changed"
            second = stock_lookup.fetch_szlcsc_domestic_products("C6929829")
            self.assertNotEqual(second[0]["cnyProductPriceList"][0]["productPrice"], "changed")
            self.assertEqual(run.call_count, 1)
            now.return_value = 161.0
            third = stock_lookup.fetch_szlcsc_domestic_products("C6929829")

        self.assertEqual(third[0]["stockNumber"], 170)
        self.assertEqual(run.call_count, 2)

    def test_page_cache_evicts_oldest_search_and_keeps_recent_hit(self):
        cache = type(stock_lookup._DOMESTIC_PAGE_CACHE)()
        with patch.object(stock_lookup, "_DOMESTIC_PAGE_CACHE", cache), patch(
            "part_stock_lookup.find_chrome_executable", return_value="chrome"
        ), patch("part_stock_lookup.subprocess.run", return_value=SimpleNamespace(stdout=b"<html>page</html>")) as run:
            for code in range(33):
                stock_lookup.fetch_szlcsc_page(f"C{code}")
            stock_lookup.fetch_szlcsc_page("C32")
            self.assertEqual(run.call_count, 33)
            stock_lookup.fetch_szlcsc_page("C0")
        self.assertEqual(run.call_count, 34)

    def test_duplicate_partner_fetches_share_inflight_response(self):
        cache = type(stock_lookup._PARTNER_PRODUCT_CACHE)()
        started = threading.Event()
        release = threading.Event()
        callers = threading.Barrier(8)

        def open_response(*_args, **_kwargs):
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test release timed out")
            return response

        def fetch():
            callers.wait(timeout=3)
            return stock_lookup.fetch_szlcsc_partner_products("AD4630-24BBCZ")

        with patch.object(stock_lookup, "_PARTNER_PRODUCT_CACHE", cache), patch(
            "part_stock_lookup.urllib.request.urlopen", side_effect=open_response
        ) as urlopen:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = (
                b'{"code":200,"result":[{"provider":"waldom","products":[{"productCode":"C5136981"}]}]}'
            )
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(fetch) for _ in range(8)]
                try:
                    self.assertTrue(started.wait(timeout=3))
                finally:
                    release.set()
                results = [future.result(timeout=3) for future in futures]

        self.assertTrue(all(result[0]["productCode"] == "C5136981" for result in results))
        self.assertEqual(urlopen.call_count, 1)

    def test_empty_and_failed_partner_responses_are_retried(self):
        cache = type(stock_lookup._PARTNER_PRODUCT_CACHE)()
        with patch.object(stock_lookup, "_PARTNER_PRODUCT_CACHE", cache), patch(
            "part_stock_lookup.urllib.request.urlopen"
        ) as urlopen:
            urlopen.side_effect = OSError("offline")
            self.assertEqual(stock_lookup.fetch_szlcsc_partner_products("AD4630-24BBCZ"), [])
            urlopen.side_effect = None
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"code":200,"result":[]}'
            self.assertEqual(stock_lookup.fetch_szlcsc_partner_products("AD4630-24BBCZ"), [])
            self.assertEqual(stock_lookup.fetch_szlcsc_partner_products("AD4630-24BBCZ"), [])
        self.assertEqual(urlopen.call_count, 3)

    def test_cache_limits_distinct_inflight_fetches_and_recovers_after_error(self):
        cache = stock_lookup._FetchCache(max_inflight=1)
        started = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        second_started = threading.Event()
        original_wait = cache._condition.wait

        def wait(*args, **kwargs):
            waiting.set()
            return original_wait(*args, **kwargs)

        def fail():
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test release timed out")
            raise OSError("offline")

        def succeed():
            second_started.set()
            return ["recovered"]

        with patch.object(cache._condition, "wait", side_effect=wait), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(cache.get, ("first", 8), fail)
            try:
                self.assertTrue(started.wait(timeout=3))
                second = executor.submit(cache.get, ("second", 8), succeed)
                self.assertTrue(waiting.wait(timeout=3))
                self.assertFalse(second_started.is_set())
            finally:
                release.set()
            with self.assertRaisesRegex(OSError, "offline"):
                first.result(timeout=3)
            self.assertEqual(second.result(timeout=3), ["recovered"])
        self.assertEqual(cache.get(("first", 8), lambda: ["retry"]), ["retry"])

    def test_inflight_waiters_share_empty_results_and_errors_then_retry(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                cache = stock_lookup._FetchCache()
                started = threading.Event()
                release = threading.Event()
                waiting = threading.Event()
                calls = []

                def load():
                    calls.append("load")
                    started.set()
                    if not release.wait(timeout=3):
                        raise TimeoutError("test release timed out")
                    if fails:
                        raise OSError("offline")
                    return []

                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(cache.get, ("same", 8), load)
                    try:
                        self.assertTrue(started.wait(timeout=3))
                        pending = cache._inflight[("same", 8)]
                        original_result = pending.result

                        def wait(*args, **kwargs):
                            waiting.set()
                            return original_result(*args, **kwargs)

                        with patch.object(pending, "result", side_effect=wait):
                            second = executor.submit(cache.get, ("same", 8), load)
                            self.assertTrue(waiting.wait(timeout=3))
                            release.set()
                            for future in (first, second):
                                if fails:
                                    with self.assertRaisesRegex(OSError, "offline"):
                                        future.result(timeout=3)
                                else:
                                    self.assertEqual(future.result(timeout=3), [])
                    finally:
                        release.set()
                self.assertEqual(calls, ["load"])
                self.assertEqual(cache.get(("same", 8), lambda: ["retry"]), ["retry"])

    def test_fetch_cache_separates_timeout_budgets_and_expires_partner_results(self):
        cache = stock_lookup._FetchCache()
        with patch("time.monotonic", return_value=100.0) as now:
            self.assertEqual(cache.get(("model", 3), lambda: ["short"]), ["short"])
            self.assertEqual(cache.get(("model", 8), lambda: ["long"]), ["long"])
            self.assertEqual(cache.get(("model", 3), lambda: ["unused"]), ["short"])
            now.return_value = 160.0
            self.assertEqual(cache.get(("model", 3), lambda: ["fresh"]), ["fresh"])

    def test_cancelled_stock_owner_does_not_cancel_unrelated_shared_search(self):
        from concurrent.futures import CancelledError
        group = stock_lookup.StockQueryGroup()
        cache = stock_lookup._FetchCache()
        started, waiting, release = threading.Event(), threading.Event(), threading.Event()

        def load_stock():
            started.set()
            self.assertTrue(release.wait(3))
            return ['cancelled owner']

        with ThreadPoolExecutor(max_workers=2) as executor:
            owner = executor.submit(group.run, lambda: cache.get(('same', 8), load_stock))
            self.assertTrue(started.wait(3))
            pending = cache._inflight[('same', 8)]
            original_result = pending.result

            def wait(*args, **kwargs):
                waiting.set()
                return original_result(*args, **kwargs)

            with patch.object(pending, 'result', side_effect=wait):
                search = executor.submit(cache.get, ('same', 8), lambda: ['search retry'])
                self.assertTrue(waiting.wait(3))
                group.cancel()
                release.set()
                with self.assertRaises(CancelledError):
                    owner.result(timeout=3)
                self.assertEqual(search.result(timeout=3), ['search retry'])

    def test_cancelled_stock_does_not_replace_cache_or_poison_next_query(self):
        from concurrent.futures import CancelledError
        from _scratch import temporary_project
        group = stock_lookup.StockQueryGroup()
        with temporary_project(prefix='cancel-stock-cache-') as root:
            path = Path(root) / 'stock.json'
            original = {'C100': {'ok': True, 'stock': '123', 'c_code': 'C100'}}
            stock_lookup.save_stock_cache(path, original)
            before = path.read_bytes()

            def fetch(code):
                group.cancel()
                return {'ok': True, 'stock': '0', 'c_code': code}

            with self.assertRaises(CancelledError):
                group.run(lambda: stock_lookup.lookup_stock_with_cache(path, 'C100', fetch))
            self.assertEqual(path.read_bytes(), before)
            result = group.run(lambda: stock_lookup.lookup_stock_with_cache(path, 'C100',
                lambda code: {'ok': True, 'stock': '321', 'c_code': code}))
            self.assertEqual(result['stock'], '321')

    def test_exit_cancels_wait_inside_http_without_writing_late_result(self):
        from concurrent.futures import CancelledError
        from _scratch import temporary_project
        group = stock_lookup.StockQueryGroup()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def blocked_urlopen(*args, **kwargs):
            started.set()
            try:
                self.assertTrue(release.wait(3))
                raise OSError('test offline')
            finally:
                finished.set()

        with temporary_project(prefix='cancel-stock-http-') as root:
            path = Path(root) / 'stock.json'
            with patch('part_stock_lookup.urllib.request.urlopen', side_effect=blocked_urlopen), \
                    ThreadPoolExecutor(max_workers=1) as executor:
                request = executor.submit(group.run, lambda: stock_lookup.lookup_stock_with_cache(path, 'C100'))
                try:
                    self.assertTrue(started.wait(3))
                    group.cancel()
                    with self.assertRaises(CancelledError):
                        request.result(timeout=.5)
                    self.assertFalse(path.exists())
                finally:
                    release.set()
                    self.assertTrue(finished.wait(3))

    def test_duplicate_stock_requests_share_entire_http_query(self):
        from _scratch import temporary_project
        started, waiting, release = threading.Event(), threading.Event(), threading.Event()
        group = stock_lookup.StockQueryGroup()
        calls = []

        def fetch(code):
            calls.append(code)
            started.set()
            self.assertTrue(release.wait(3))
            return {'ok': True, 'stock': '100', 'c_code': code}

        with temporary_project(prefix='shared-stock-http-') as root:
            path = Path(root) / 'stock.json'
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(group.run, lambda: stock_lookup.lookup_stock_with_cache(path, 'C100', fetch))
                try:
                    self.assertTrue(started.wait(3))
                    pending = next(iter(stock_lookup._STOCK_FETCH_CACHE._inflight.values()))
                    original_result = pending.result

                    def wait(*args, **kwargs):
                        waiting.set()
                        return original_result(*args, **kwargs)

                    with patch.object(pending, 'result', side_effect=wait):
                        second = executor.submit(group.run, lambda: stock_lookup.lookup_stock_with_cache(path, 'C100', fetch))
                        self.assertTrue(waiting.wait(3))
                        release.set()
                        self.assertEqual(first.result(timeout=3)['stock'], '100')
                        self.assertEqual(second.result(timeout=3)['stock'], '100')
                finally:
                    release.set()
        self.assertEqual(calls, ['C100'])


class PartStockLookupTest(unittest.TestCase):
    def domestic_product_fixture(self):
        fixture = PROJECT_ROOT / "tests" / "fixtures" / "szlcsc_c6929829_next_data.html"
        return parse_szlcsc_next_data(fixture.read_text(encoding="utf-8"))

    def test_model_search_overlaps_fetches_and_keeps_all_partner_quotes(self):
        partner_started = threading.Event()
        domestic_started = threading.Event()
        overlapped = []
        queries = []

        def partner_fetcher(keyword, timeout):
            queries.append((keyword, timeout))
            partner_started.set()
            overlapped.append(domestic_started.wait(timeout=2))
            return [
                {
                    "productCode": "C5136981",
                    "productCodeManufacturer": "AD4630-24BBCZ",
                    "provider": "waldom",
                    "vendorCode": vendor,
                    "productCodeProvider": "AD4630-24BBCZ",
                    "stockNumber": stock,
                    "minBuyNumber": 1,
                    "productMinEncapsulationNumber": 1,
                    "incrementalQuantity": 1,
                    "deliveryTimeWayDays": "3-5 days",
                    "tieredPrices": [{"countStart": 1, "price": "623.954925"}],
                }
                for vendor, stock in (("G9330", 520), ("G14518", 1))
            ]

        def domestic_fetcher(keyword, timeout):
            queries.append((keyword, timeout))
            domestic_started.set()
            overlapped.append(partner_started.wait(timeout=2))
            return [{"productCode": "C5136981", "productModel": "AD4630-24BBCZ", "stockNumber": 0}]

        with patch("part_stock_lookup.urllib.request.urlopen") as urlopen:
            result = search_lcsc_candidates(
                "AD4630-24BBCZ BGA-64", timeout=3,
                domestic_fetcher=domestic_fetcher, partner_fetcher=partner_fetcher,
            )

        self.assertEqual(overlapped, [True, True])
        self.assertCountEqual(queries, [("AD4630-24BBCZ", 3), ("AD4630-24BBCZ BGA-64", 3)])
        self.assertEqual(result["mode"], "domestic")
        self.assertEqual(result["items"][0]["stock"], "0")
        self.assertEqual([channel["stock"] for channel in result["items"][0]["order_channels"]], ["520", "1"])
        urlopen.assert_not_called()

    def test_search_fetch_failures_preserve_other_sources(self):
        product = {"productCode": "C5136981", "productModel": "AD4630-24BBCZ", "stockNumber": 7}

        def failed_fetch(_keyword, _timeout):
            raise OSError("unavailable")

        for failed_source in ("partner", "domestic"):
            with self.subTest(failed_source=failed_source), patch("part_stock_lookup.urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
                    {"code": 200, "result": {"dataList": [product]}}
                ).encode()
                result = search_lcsc_candidates(
                    "AD4630-24BBCZ",
                    domestic_fetcher=failed_fetch if failed_source == "domestic" else lambda *_: [product],
                    partner_fetcher=failed_fetch if failed_source == "partner" else lambda *_: [],
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result["items"][0]["c_code"], "C5136981")
                self.assertEqual(result["items"][0]["stock"], "7")

    def test_exact_search_still_skips_broad_partner_query(self):
        with patch("part_stock_lookup.urllib.request.urlopen") as urlopen, patch(
            "part_stock_lookup.fetch_szlcsc_partner_products"
        ) as partner:
            result = search_lcsc_candidates(
                " c6929829 ", include_substitutes=False,
                domestic_fetcher=lambda *_: self.domestic_product_fixture(), partner_fetcher=partner,
                overseas_fetcher=lambda *_: [{"supplier": "Mouser", "url": "https://example.com/quote"}],
            )

        self.assertEqual([item["c_code"] for item in result["items"]], ["C6929829"])
        self.assertEqual(result["items"][0]["order_channels"][0]["supplier"], "Mouser")
        partner.assert_not_called()
        urlopen.assert_not_called()

    def test_parse_domestic_next_data_maps_c6929829_to_stock_schema(self):
        products = self.domestic_product_fixture()

        self.assertEqual(len(products), 1)
        stock = parse_lcsc_product("C6929829", products[0])
        self.assertEqual(stock["model"], "MWSA1707S-330MT")
        self.assertEqual(stock["brand"], "Sunlord(顺络)")
        self.assertEqual(stock["package"], "SMD,17.2x17.2mm")
        self.assertEqual(stock["stock"], "170")
        self.assertEqual(stock["stock_sz"], "170")
        self.assertEqual(stock["mpq"], "200")
        self.assertEqual(stock["price"], "¥10.38@1")
        self.assertIn("额定电流: 7A", stock["param_detail"])

    def test_lcbook_order_channels_build_detail_url(self):
        product = {
            "productCode": "C5136981",
            "productModel": "AD4630-24BBCZ",
            "stockNumber": 520,
            "deliveryTimeWayDays": "3-5个工作日",
            "tieredPrices": [
                {"countStart": 1, "price": "623.954925"},
                {"countStart": 10, "price": "607.67784"},
                {"countStart": 50, "price": "596.82645"},
            ],
            "lcBookProductVO": {
                "provider": "waldom",
                "productCodeProvider": "AD4630-24BBCZad26+",
                "productCodeManufacturer": "AD4630-24BBCZ",
                "vendorCode": "G11436",
                "batchNumber": "26+",
            },
        }

        channels = product_order_channels(product)

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["supplier"], "waldom")
        self.assertEqual(channels[0]["stock"], "520")
        self.assertEqual(channels[0]["lead_time"], "3-5个工作日")
        self.assertIn("1+: ¥623.954925", channels[0]["price"])
        self.assertIn("/details/RzExNDM2/AD4630-24BBCZad26%252B.html", channels[0]["url"])

    def test_parse_partner_next_data_maps_orderable_channel(self):
        html = """
        <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"partnerResultRaw":{"lcbookLandingPageProductVOS":[{
          "vendorCode":"G15040",
          "vendorCodeEncode":"RzE1MDQw",
          "productCodeProvider":"FAN73912MX ON 22+",
          "provider":"waldom",
          "productCodeManufacturer":"FAN73912MX",
          "productCode":"C899115",
          "brandName":"onsemi(安森美)",
          "selfStandard":"SOIC-16-300mil",
          "tieredPrices":[{"countStart":150,"price":13.2},{"countStart":800,"price":12.204}],
          "deliveryTimeWayDays":"3-5个工作日",
          "stockNumber":4000,
          "productMinEncapsulationNumber":1000,
          "minBuyNumber":150,
          "selfParams":[{"parameterName":"驱动配置","parameterDetailValue":"半桥"}],
          "lcCatalogName":"栅极驱动芯片"
        }]}}}}</script>
        """

        products = parse_szlcsc_partner_data(html)
        item = build_candidate_item("FAN73912MX", products[0])

        self.assertEqual(item["c_code"], "C899115")
        self.assertEqual(item["model"], "FAN73912MX")
        self.assertEqual(item["channel"], "合作库存/订货")
        self.assertEqual(item["stock"], "4000")
        self.assertEqual(item["delivery"], "3-5个工作日")
        self.assertEqual(item["order_channels"][0]["price"], "150+: ¥13.2; 800+: ¥12.204")
        self.assertIn("/details/RzE1MDQw/FAN73912MX%2520ON%252022%252B.html", item["order_channels"][0]["url"])

    def test_parse_rendered_overseas_cards_keeps_supplier_quote_fields(self):
        html = """
        <section data-custom-data='{"productType":"oversea","provider":"digikey","productCodeManufacturer":"P2TC0805D5002LNTA"}'>
          <dl><dd>P2TC0805D5002LNTA</dd></dl>
          <dl><dt>品牌</dt><dd>Vishay Sfernice</dd></dl>
          <dl><dt>封装</dt><dd>0805</dd></dl>
          <dl><dt>参数</dt><dd>阻值: 50kOhm</dd></dl>
          <dl><dt>描述</dt><dd>Thin Film Resistors</dd></dl>
          <dl><dt>供应商编号</dt><dd>716-P2TC-ND</dd></dl>
          <dl><dt>库存</dt><dd>118</dd></dl>
          <dl><dt>增量</dt><dd>100</dd></dl>
          <dl><dt>最小包装</dt><dd>100个</dd></dl>
          <span>100+ ￥124.879691</span><span>8-14个工作日</span>
          <a href="https://item.szlcsc.com/details/Digikey/716-P2TC-ND.html">下单</a>
        </section>
        """

        channels = parse_szlcsc_overseas_channels(html, "C4283613")

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["supplier"], "Digikey")
        self.assertEqual(channels[0]["supplier_sku"], "716-P2TC-ND")
        self.assertEqual(channels[0]["related_lcsc_code"], "C4283613")
        self.assertEqual(channels[0]["stock"], "118")
        self.assertEqual(channels[0]["lead_time"], "8-14个工作日")
        self.assertEqual(channels[0]["moq"], "100")
        self.assertIn("100+: ¥124.879691", channels[0]["price"])
        self.assertIn("item.szlcsc.com/details/Digikey/716-P2TC-ND.html", channels[0]["url"])

    def test_exact_candidate_search_attaches_official_overseas_channels(self):
        product = {
            "productCode": "C4283613",
            "productModel": "P2TC0805D5002LNTA",
            "productGradePlateName": "VISHAY",
            "encapsulationModel": "0805",
            "stockNumber": 1,
        }
        payload = json.dumps({"code": 200, "result": {"dataList": [product]}}).encode()
        overseas = [{
            "channel_type": "overseas",
            "supplier": "Mouser",
            "provider_model": "P2TC0805D5002LNTA",
            "supplier_sku": "72-P2TC0805D5002LNTA",
            "url": "https://item.szlcsc.com/details/Mouser/72-P2TC0805D5002LNTA.html",
        }]

        with patch("part_stock_lookup.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = payload
            result = search_lcsc_candidates(
                "C4283613",
                include_substitutes=False,
                domestic_fetcher=lambda _code, _timeout: [],
                partner_fetcher=lambda _keyword, _timeout: [],
                overseas_fetcher=lambda _code, _timeout: overseas,
            )

        self.assertEqual([item["c_code"] for item in result["items"]], ["C4283613"])
        self.assertEqual(result["items"][0]["order_channels"][0]["supplier"], "Mouser")

    def test_exact_c_code_candidate_search_falls_back_to_domestic_page(self):
        products = self.domestic_product_fixture()
        wmsc_empty = b'{"code":200,"result":{"dataList":[]}}'

        with patch("part_stock_lookup.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = wmsc_empty
            result = search_lcsc_candidates("C6929829", domestic_fetcher=lambda _code, _timeout: products)

        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["c_code"], "C6929829")
        self.assertEqual(result["items"][0]["model"], "MWSA1707S-330MT")
        self.assertEqual(result["items"][0]["stock"], "170")
        self.assertEqual(result["items"][0]["match_rank"], 0)

    def test_model_candidate_search_prefers_domestic商城_records(self):
        domestic_products = [
            {
                "productCode": "C6929829",
                "productModel": "MWSA1707S-330MT",
                "productGradePlateName": "顺络",
                "encapsulationModel": "SMD,17.2x17.2mm",
                "stockNumber": 170,
            },
            {
                "productCode": "C9999999",
                "productModel": "MWSA1707S-330MT-ALT",
                "productGradePlateName": "其他品牌",
                "encapsulationModel": "SMD",
                "stockNumber": 0,
            },
        ]

        with patch("part_stock_lookup.urllib.request.urlopen") as urlopen:
            result = search_lcsc_candidates(
                "MWSA1707S-330MT",
                domestic_fetcher=lambda _keyword, _timeout: domestic_products,
                partner_fetcher=lambda _keyword, _timeout: [],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "domestic")
        self.assertEqual([item["c_code"] for item in result["items"]], ["C6929829", "C9999999"])
        urlopen.assert_not_called()

    def test_exact_c_code_candidate_search_can_exclude_parameter_substitutes(self):
        products = [
            {
                "productCode": "C4283613",
                "productModel": "P2TC0805D5002LNTA",
                "productGradePlateName": "VISHAY",
                "encapsulationModel": "0805",
                "stockNumber": 1,
            },
            {
                "productCode": "C6738529",
                "productModel": "PTN0805E5002BST1",
                "productGradePlateName": "VISHAY",
                "encapsulationModel": "0805",
                "stockNumber": 11,
            },
        ]
        payload = json.dumps({"code": 200, "result": {"dataList": products}}).encode()

        with patch("part_stock_lookup.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = payload
            result = search_lcsc_candidates(
                "C4283613",
                include_substitutes=False,
                domestic_fetcher=lambda _code, _timeout: [],
                partner_fetcher=lambda _keyword, _timeout: [],
            )

        self.assertEqual([item["c_code"] for item in result["items"]], ["C4283613"])
        self.assertEqual(result["mode"], "exact")

    def test_model_search_accepts_single_character_bom_model_difference(self):
        match_type, match_rank = candidate_match(
            "P2TC0805D50021NTA 50k 0805",
            {"productModel": "P2TC0805D5002LNTA"},
        )

        self.assertEqual(match_type, "型号近似")
        self.assertEqual(match_rank, 1)

    def test_exact_c_code_stock_lookup_falls_back_to_domestic_page(self):
        products = self.domestic_product_fixture()
        wmsc_empty = b'{"code":200,"result":{"dataList":[]}}'

        with patch("part_stock_lookup.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = wmsc_empty
            stock = query_lcsc_stock("C6929829", domestic_fetcher=lambda _code, _timeout: products)

        self.assertTrue(stock["ok"])
        self.assertEqual(stock["c_code"], "C6929829")
        self.assertEqual(stock["source"], "so.szlcsc.com/domestic")

    def test_exact_c_code_candidate_search_falls_back_when_wmsc_is_unavailable(self):
        products = self.domestic_product_fixture()

        with patch("part_stock_lookup.urllib.request.urlopen", side_effect=OSError("wmsc unavailable")):
            result = search_lcsc_candidates("C6929829", domestic_fetcher=lambda _code, _timeout: products)

        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["c_code"], "C6929829")

    def test_model_candidate_search_enriches_missing_order_channels(self):
        wmsc_product = {
            "productCode": "C5136981",
            "productModel": "AD4630-24BBCZ",
            "productGradePlateName": "ADI",
            "encapsulationModel": "BGA-64",
            "stockNumber": 0,
        }
        partner_products = [
            {
                "productCode": "C5136981",
                "productCodeManufacturer": "AD4630-24BBCZ",
                "provider": "waldom",
                "vendorCode": "G11436",
                "vendorCodeEncode": "RzExNDM2",
                "productCodeProvider": "AD4630-24BBCZad26+",
                "stockNumber": 26000,
                "minBuyNumber": 1040,
                "productMinEncapsulationNumber": 260,
                "incrementalQuantity": 1040,
                "deliveryTimeWayDays": "39-41个工作日",
                "tieredPrices": [{"countStart": 1040, "price": "313.632"}],
            },
            {
                "productCode": "C5136981",
                "productCodeManufacturer": "AD4630-24BBCZ",
                "provider": "waldom",
                "vendorCode": "G9330",
                "vendorCodeEncode": "RzkzMzA=",
                "productCodeProvider": "AD4630-24BBCZ ADI 26+",
                "stockNumber": 520,
                "minBuyNumber": 1,
                "productMinEncapsulationNumber": 1,
                "incrementalQuantity": 1,
                "deliveryTimeWayDays": "3-5个工作日",
                "tieredPrices": [{"countStart": 1, "price": "623.954925"}],
            },
            {
                "productCode": "C5136981",
                "productCodeManufacturer": "AD4630-24BBCZ",
                "provider": "waldom",
                "vendorCode": "G14518",
                "vendorCodeEncode": "RzE0NTE4",
                "productCodeProvider": "AD4630-24BBCZ ADI/亚德诺 26+",
                "stockNumber": 1,
                "minBuyNumber": 1,
                "productMinEncapsulationNumber": 1,
                "incrementalQuantity": 1,
                "deliveryTimeWayDays": "1-3个工作日",
                "tieredPrices": [{"countStart": 1, "price": "713"}],
            },
        ]
        wmsc_payload = json.dumps({"code": 200, "result": {"dataList": [wmsc_product]}}).encode()
        partner_queries = []

        def partner_fetcher(keyword, _timeout):
            partner_queries.append(keyword)
            return partner_products

        with patch("part_stock_lookup.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = wmsc_payload
            result = search_lcsc_candidates(
                "AD4630-24BBCZ CSPBGA-64(7x7) AD4630-24BBCZ",
                domestic_fetcher=lambda _code, _timeout: [],
                partner_fetcher=partner_fetcher,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["c_code"], "C5136981")
        channels = result["items"][0]["order_channels"]
        self.assertEqual(len(channels), 3)
        self.assertEqual([channel["stock"] for channel in channels], ["26000", "520", "1"])
        self.assertEqual(channels[0]["moq"], "1040")
        self.assertEqual(channels[0]["mpq"], "260")
        self.assertEqual(channels[0]["increment"], "1040")
        self.assertEqual(partner_queries, ["AD4630-24BBCZ"])

    def test_parse_partner_response_keeps_every_orderable_quote(self):
        payload = {
            "code": 200,
            "result": [
                {"provider": "te", "isLcscBookShow": False, "products": []},
                {
                    "provider": "waldom",
                    "isLcscBookShow": True,
                    "products": [
                        {"vendorCode": "G11436", "productCode": "C5136981"},
                        {"vendorCode": "G9330", "productCode": "C5136981"},
                        {"vendorCode": "G14518", "productCode": "C5136981"},
                    ],
                },
            ],
        }

        products = parse_szlcsc_partner_response(payload)

        self.assertEqual([item["vendorCode"] for item in products], ["G11436", "G9330", "G14518"])
        self.assertTrue(all(item["provider"] == "waldom" for item in products))

    def test_normalize_c_code_extracts_single_lcsc_code_and_ignores_nc(self):
        self.assertEqual(normalize_c_code(" c25741 "), "C25741")
        self.assertEqual(normalize_c_code("C 5182461"), "C5182461")
        self.assertEqual(normalize_c_code("C25741,C123"), "C25741")
        self.assertEqual(normalize_c_code("NC"), "")
        self.assertEqual(normalize_c_code(""), "")

    def test_parse_lcsc_product_stock_price_and_order_fields(self):
        product = {
            "productCode": "C25741",
            "productModel": "0402WGF1003TCE",
            "brandNameEn": "UNI-ROYAL",
            "encapStandard": "0402",
            "stockNumber": 12345,
            "stockSz": 10000,
            "stockJs": 2000,
            "wmStockHk": 345,
            "minBuyNumber": 100,
            "minPacketNumber": 10000,
            "productPriceList": [{"ladder": 100, "productPrice": "0.0076", "currencySymbol": "$"}],
            "url": "https://www.lcsc.com/product-detail/C25741.html",
        }

        stock = parse_lcsc_product("C25741", product)

        self.assertTrue(stock["ok"])
        self.assertEqual(stock["stock"], "12345")
        self.assertEqual(stock["stock_sz"], "10000")
        self.assertEqual(stock["stock_js"], "2000")
        self.assertEqual(stock["stock_hk"], "345")
        self.assertEqual(stock["moq"], "100")
        self.assertEqual(stock["mpq"], "10000")
        self.assertEqual(stock["price"], "¥0.0464@100")
        self.assertEqual(stock["model"], "0402WGF1003TCE")
        self.assertEqual(stock["url"], "https://so.szlcsc.com/global.html?k=C25741")

    def test_parse_lcsc_product_includes_parameter_summary(self):
        product = {
            "productCode": "C49678",
            "productKeyAttributes": "CAP CER 100nF 50V X7R 0805",
            "paramVOList": [{"paramName": "精度", "paramValue": "±10%"}, {"paramName": "温度系数", "paramValue": "X7R"}],
            "productImageUrl": "https://assets.lcsc.com/example.jpg",
            "domesticStockVO": {"shipImmediately": 120, "ship3Days": 0},
            "productArrange": "Tape & Reel (TR)",
            "catalogName": "贴片电容(MLCC)",
        }

        stock = parse_lcsc_product("C49678", product)

        self.assertEqual(stock["param"], "CAP CER 100nF 50V X7R 0805")
        self.assertEqual(stock["param_detail"], "精度: ±10%; 温度系数: X7R")
        self.assertEqual(stock["image_url"], "https://assets.lcsc.com/example.jpg")
        self.assertEqual(stock["delivery"], "可立即发货 120")
        self.assertEqual(stock["arrange"], "Tape & Reel (TR)")
        self.assertEqual(stock["category"], "贴片电容(MLCC)")

    def test_extract_model_tokens_ignores_value_and_package_tokens(self):
        self.assertEqual(extract_model_tokens("499k 0805 RN73C2A499KBTDF"), ["RN73C2A499KBTDF"])

    def test_build_candidate_item_marks_exact_model_match(self):
        item = build_candidate_item(
            "499k 0805 RN73C2A499KBTDF",
            {
                "productCode": "C4336148",
                "productModel": "RN73C2A499KBTDF",
                "brandNameEn": "TE Connectivity",
                "encapStandard": "0805",
                "stockNumber": 160,
                "productPriceList": [{"ladder": 1, "productPrice": "1.2501", "currencySymbol": "$"}],
            },
        )

        self.assertEqual(item["match_type"], "型号精确")
        self.assertEqual(item["match_rank"], 0)

    def test_product_param_summary_falls_back_to_param_list(self):
        product = {
            "paramVOList": [
                {"paramName": "容值", "paramValue": "100nF"},
                {"paramName": "耐压", "paramValue": "50V"},
            ]
        }

        self.assertEqual(product_param_summary(product), "容值: 100nF; 耐压: 50V")

    def test_parse_lcsc_product_converts_usd_ladder_to_rmb_when_cny_is_absent(self):
        product = {
            "productCode": "C49678",
            "stockNumber": 2252140,
            "productPriceList": [{"ladder": 20, "productPrice": "0.0247", "currencySymbol": "$"}],
        }

        stock = parse_lcsc_product("C49678", product)

        self.assertEqual(stock["price"], "¥0.1508@20")

    def test_parse_lcsc_product_prefers_cny_price(self):
        product = {
            "productCode": "C25741",
            "stockNumber": 99,
            "cnyProductPriceList": [{"ladder": 10, "productPrice": "0.1234"}],
            "productPriceList": [{"ladder": 10, "productPrice": "0.0172", "currencySymbol": "$"}],
        }

        stock = parse_lcsc_product("C25741", product)

        self.assertEqual(stock["price"], "¥0.1234@10")

    def test_format_price_ladder_keeps_only_two_levels_by_default(self):
        product = {
            "productPriceList": [
                {"ladder": 5, "productPrice": "0.0979", "currencySymbol": "$"},
                {"ladder": 50, "productPrice": "0.0783", "currencySymbol": "$"},
                {"ladder": 150, "productPrice": "0.0685", "currencySymbol": "$"},
            ]
        }

        self.assertEqual(format_price_ladder(product), "5+: ¥0.5977; 50+: ¥0.478")

    def test_parse_szlcsc_item_price_reads_rmb_price_ladder(self):
        html = """
        <div>20+：￥0.1508</div>
        <div>200+ : ¥0.1198</div>
        """

        self.assertEqual(parse_szlcsc_item_price(html), "¥0.1508@20")

    def test_parse_szlcsc_item_price_uses_meta_rmb_price_when_ladder_is_absent(self):
        html = '<meta name="description" content="封装0805，价格￥0.0865元。正品现货。">'

        self.assertEqual(parse_szlcsc_item_price(html), "¥0.0865")

    def test_enrich_rows_with_cached_stock_uses_saved_result_without_fetching(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "stock.json"
            cache_path.write_text(
                '{"C49678":{"ok":true,"c_code":"C49678","stock":"2252140","status":"OK","fetched_at":"2026-08-12T23:51:14"}}',
                encoding="utf-8",
            )
            rows = [
                {"effective_c_code": "C49678", "accepted_c_code": "", "lcsc_match_c_code": ""},
                {"effective_c_code": "", "accepted_c_code": "", "lcsc_match_c_code": ""},
            ]

            enrich_rows_with_cached_stock(rows, cache_path)

        self.assertEqual(rows[0]["stock"]["stock"], "2252140")
        self.assertEqual(rows[0]["stock"]["fetched_at"], "2026-08-12T23:51:14")
        self.assertEqual(rows[1]["stock"]["status"], "请提供料号")

    def test_missing_c_code_stock_status_prompts_for_part_number(self):
        stock = stock_status("", "NeedCCode")

        self.assertEqual(stock["status"], "请提供料号")
        self.assertEqual(stock["error"], "请提供料号")

    def test_enrich_rows_fetches_each_unique_c_code_once(self):
        calls = []

        def fake_fetch(c_code):
            calls.append(c_code)
            return {"ok": True, "c_code": c_code, "stock": "42", "moq": "1", "mpq": "10"}

        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"effective_c_code": "C25741", "accepted_c_code": "", "lcsc_match_c_code": ""},
                {"effective_c_code": "C25741", "accepted_c_code": "", "lcsc_match_c_code": ""},
                {"effective_c_code": "NC", "accepted_c_code": "", "lcsc_match_c_code": ""},
            ]

            enrich_rows_with_stock(rows, Path(tmp) / "stock.json", fetcher=fake_fetch)

        self.assertEqual(calls, ["C25741"])
        self.assertEqual(rows[0]["stock"]["stock"], "42")
        self.assertEqual(rows[1]["stock"]["stock"], "42")
        self.assertEqual(rows[2]["stock"]["status"], "请提供料号")

    def test_lookup_stock_with_cache_refreshes_single_code_and_saves_result(self):
        calls = []

        def fake_fetch(c_code):
            calls.append(c_code)
            return {"ok": True, "c_code": c_code, "stock": "99", "status": "OK"}

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "stock.json"
            stock = lookup_stock_with_cache(cache_path, "c25741", fetcher=fake_fetch)

            self.assertEqual(calls, ["C25741"])
            self.assertEqual(stock["stock"], "99")
            self.assertIn("C25741", cache_path.read_text(encoding="utf-8"))

    def test_concurrent_stock_refreshes_preserve_all_cache_entries(self):
        barrier = threading.Barrier(2)

        def fake_fetch(c_code):
            barrier.wait(timeout=5)
            return {"ok": True, "c_code": c_code, "stock": c_code[-1], "status": "OK"}

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "stock.json"
            threads = [
                threading.Thread(target=lookup_stock_with_cache, args=(cache_path, code), kwargs={"fetcher": fake_fetch})
                for code in ("C25741", "C49678")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            cache = load_stock_cache(cache_path)

        self.assertEqual(sorted(cache), ["C25741", "C49678"])
        self.assertEqual(cache["C25741"]["stock"], "1")
        self.assertEqual(cache["C49678"]["stock"], "8")

    def test_lookup_stock_with_cache_keeps_previous_success_when_refresh_fails(self):
        def fake_fetch(_c_code):
            return {"ok": False, "c_code": "C25741", "status": "QueryFailed", "error": "timeout"}

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "stock.json"
            cache_path.write_text(
                '{"C25741":{"ok":true,"c_code":"C25741","stock":"123","status":"OK","price":"$0.01@1"}}',
                encoding="utf-8",
            )

            stock = lookup_stock_with_cache(cache_path, "C25741", fetcher=fake_fetch)

            self.assertEqual(stock["stock"], "123")
            self.assertEqual(stock["status"], "QueryFailed/Cached")
            self.assertEqual(load_stock_cache(cache_path)["C25741"]["price"], "无人民币价")

    def test_lookup_stock_with_cache_keeps_cached_rmb_price_when_refresh_has_no_rmb_price(self):
        def fake_fetch(_c_code):
            return {"ok": True, "c_code": "C49678", "stock": "2400000", "status": "OK", "price": "无人民币价"}

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "stock.json"
            cache_path.write_text(
                '{"C49678":{"ok":true,"c_code":"C49678","stock":"2252140","status":"OK","price":"¥0.1508@20"}}',
                encoding="utf-8",
            )

            stock = lookup_stock_with_cache(cache_path, "C49678", fetcher=fake_fetch)

            self.assertEqual(stock["stock"], "2400000")
            self.assertEqual(stock["price"], "¥0.1508@20")
            self.assertEqual(load_stock_cache(cache_path)["C49678"]["price"], "¥0.1508@20")


if __name__ == "__main__":
    unittest.main()
