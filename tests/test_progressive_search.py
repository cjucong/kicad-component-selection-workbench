import importlib
import json
import re
import shutil
import subprocess
import sys
from _scratch import temporary_project
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import part_stock_lookup as stock
from part_selection_server import PartSelectionApp, make_handler, render_index

PRODUCT = {"productCode": "C4283613", "productModel": "P2TC0805D5002LNTA", "stockNumber": 1}


class ProgressiveSourceTest(unittest.TestCase):
    def test_fallback_reports_its_progress_and_result_source(self):
        snapshots = []
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"code": 200, "result": {"dataList": [PRODUCT]}}
        ).encode()
        with patch.object(stock.urllib.request, "urlopen", return_value=response):
            result = stock.search_lcsc_candidates(
                "capacitor 4.7uF 0805", domestic_fetcher=lambda *_: [],
                partner_fetcher=lambda *_: [], on_partial=snapshots.append)
        self.assertTrue(result["items"])
        self.assertTrue(any(state["sources"].get("fallback") == "querying" for state in snapshots))
        self.assertEqual(result["sources"].get("fallback"), "ready")

    def test_empty_and_failed_fallback_are_distinct(self):
        for payload, expected in (({"code": 200, "result": {"dataList": []}}, "empty"),
                                  ({"code": 503, "msg": "busy"}, "failed")):
            with self.subTest(expected=expected):
                response = unittest.mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                with patch.object(stock.urllib.request, "urlopen", return_value=response), \
                     patch.object(stock, "domestic_exact_candidate_items", return_value=[]):
                    result = stock.search_lcsc_candidates(
                        "capacitor 4.7uF 0805", domestic_fetcher=lambda *_: [], partner_fetcher=lambda *_: [])
                self.assertEqual(result["sources"]["fallback"], expected)
                self.assertEqual(result["ok"], expected == "empty")

    def test_domestic_result_is_published_while_partner_is_still_waiting(self):
        release = threading.Event()
        partial = threading.Event()
        results = []
        errors = []

        def partner(*_args):
            release.wait(3)
            return []

        def publish(result):
            results.append(result)
            if result.get("items"):
                partial.set()

        def run():
            try:
                stock.search_lcsc_candidates("P2TC0805D5002LNTA", domestic_fetcher=lambda *_: [PRODUCT],
                                             partner_fetcher=partner, on_partial=publish)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(partial.wait(1), f"No early result: {errors}")
            self.assertTrue(worker.is_alive(), "Partner should still be waiting")
            self.assertEqual(results[-1]["items"][0]["c_code"], "C4283613")
            self.assertEqual(results[-1]["sources"]["partner"], "querying")
        finally:
            release.set()
            worker.join(4)

    def test_force_refresh_bypasses_source_cache_without_mutating_saved_value(self):
        cache = stock._FetchCache()
        self.assertEqual(cache.get(("model", 8), lambda: [1]), [1])
        self.assertEqual(cache.get(("model", 8), lambda: [2], force=True), [2])
        self.assertEqual(cache.get(("model", 8), lambda: [3]), [2])

    def test_successful_empty_refresh_does_not_resurrect_source_cache(self):
        cache = stock._FetchCache()
        cache.get(("model", 8), lambda: [1])
        self.assertEqual(cache.get(("model", 8), lambda: [], force=True), [])
        self.assertEqual(cache.get(("model", 8), lambda: [2]), [2])

    def test_running_cancellation_releases_orchestration_before_source_finishes(self):
        release, entered = threading.Event(), threading.Event()
        cancelled = threading.Event()
        def domestic(*args):
            entered.set()
            release.wait(3)
            return [PRODUCT]
        errors = []
        def run():
            try:
                stock.search_lcsc_candidates("C4283613", domestic_fetcher=domestic, cancelled=cancelled.is_set)
            except Exception as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            cancelled.set()
            thread.join(.5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["CancelledError"])
        finally:
            release.set()
            thread.join(3)

    def test_canceled_search_does_not_start_sources(self):
        with patch.object(stock, "urllib") as network:
            with self.assertRaises(Exception) as error:
                stock.search_lcsc_candidates("C4283613", cancelled=lambda: True)
            self.assertEqual(type(error.exception).__name__, "CancelledError")
            network.request.urlopen.assert_not_called()

    def test_partner_network_failure_is_not_reported_as_empty_success(self):
        with patch.object(stock.urllib.request, "urlopen", side_effect=TimeoutError("offline")):
            result = stock.search_lcsc_candidates("UNIQUE-NETWORK-FAILURE", domestic_fetcher=lambda *_: [PRODUCT])
        self.assertTrue(result["ok"])
        self.assertEqual(result["sources"]["partner"], "failed")

    def test_partner_error_response_is_not_cached_as_empty_success(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"code": 503, "message": "busy"}'
        with patch.object(stock.urllib.request, "urlopen", return_value=response):
            result = stock.search_lcsc_candidates("UNIQUE-PARTNER-ERROR", domestic_fetcher=lambda *_: [PRODUCT])
            self.assertEqual(result["sources"]["partner"], "failed")
            self.assertEqual(stock.fetch_szlcsc_partner_products("UNIQUE-LEGACY-ERROR"), [])

    def test_cancel_overseas_phase_releases_orchestration(self):
        release, entered, cancelled = threading.Event(), threading.Event(), threading.Event()
        def overseas(*args):
            entered.set()
            release.wait(3)
            return []
        errors = []
        def run():
            try:
                stock.search_lcsc_candidates("C4283613", domestic_fetcher=lambda *_: [PRODUCT],
                    overseas_fetcher=overseas, cancelled=cancelled.is_set)
            except Exception as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            cancelled.set()
            thread.join(.5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["CancelledError"])
        finally:
            release.set()
            thread.join(3)


class SearchJobsTest(unittest.TestCase):
    def setUp(self):
        module = importlib.import_module("part_selection_search")
        self.factory = module.CandidateSearchJobs

    def create(self, loader, **kwargs):
        jobs = self.factory(loader, **kwargs)
        self.addCleanup(jobs.close)
        return jobs

    def finish(self, jobs, started):
        for _ in range(150):
            state = jobs.status(started["job_id"])
            if state["done"]:
                return state
            time.sleep(.01)
        self.fail("Search did not complete")

    def test_partial_result_then_final_channel_data(self):
        release = threading.Event()
        published = threading.Event()
        self.addCleanup(release.set)

        def loader(keyword, substitutes, **options):
            options["on_partial"]({"ok": True, "items": [{"c_code": "C1"}],
                                   "sources": {"partner": "querying"}})
            published.set()
            release.wait(3)
            return {"ok": True, "items": [{"c_code": "C1", "order_channels": [{"supplier": "Digikey"}]}]}

        jobs = self.create(loader)
        started = jobs.start("model", True, "window", 1)
        self.assertTrue(published.wait(1))
        state = jobs.status(started["job_id"])
        self.assertFalse(state["done"])
        self.assertEqual(state["items"][0]["c_code"], "C1")
        release.set()
        final = self.finish(jobs, started)
        self.assertEqual(final["items"][0]["order_channels"][0]["supplier"], "Digikey")

    def test_cache_is_immediate_stale_refreshes_and_force_bypasses(self):
        calls = []
        clock = [0.0]
        def loader(keyword, substitutes, **options):
            calls.append(options["force_refresh"])
            return {"ok": True, "items": [{"c_code": "C1", "stock": str(len(calls))}]}
        jobs = self.create(loader, clock=lambda: clock[0])
        first = jobs.start(" model ", True, "a", 1)
        self.finish(jobs, first)
        fresh = jobs.start("MODEL", True, "a", 2)
        self.assertTrue(fresh["done"])
        self.assertTrue(fresh["cached"])
        self.assertEqual(calls, [False])
        clock[0] = 61
        stale = jobs.start("model", True, "a", 3)
        self.assertTrue(stale["cached"])
        self.assertEqual(stale["items"][0]["stock"], "1")
        self.finish(jobs, stale)
        forced = jobs.start("model", True, "a", 4, force_refresh=True)
        self.finish(jobs, forced)
        self.assertEqual(calls, [False, False, True])

    def test_new_request_cancels_queued_old_query_and_rejects_late_start(self):
        release = threading.Event()
        entered = threading.Event()
        self.addCleanup(release.set)
        calls = []
        def loader(keyword, substitutes, **options):
            calls.append(keyword)
            if keyword == "busy":
                entered.set()
                release.wait(3)
            return {"ok": True, "items": [{"model": keyword}]}
        jobs = self.create(loader, max_workers=1)
        jobs.start("busy", True, "other", 1)
        self.assertTrue(entered.wait(1))
        old = jobs.start("old", True, "window", 1)
        new = jobs.start("new", True, "window", 2)
        late = jobs.start("late", True, "window", 1)
        self.assertTrue(jobs.status(old["job_id"])["cancelled"])
        self.assertTrue(late["cancelled"])
        release.set()
        self.finish(jobs, new)
        self.assertEqual(calls, ["busy", "new"])

    def test_refresh_failure_keeps_cache_but_marks_it_stale(self):
        count = [0]
        def loader(*args, **kwargs):
            count[0] += 1
            if count[0] == 2:
                raise RuntimeError("upstream unavailable")
            return {"ok": True, "items": [{"c_code": "C1"}]}
        jobs = self.create(loader)
        self.finish(jobs, jobs.start("model", True, "a", 1))
        final = self.finish(jobs, jobs.start("model", True, "a", 2, force_refresh=True))
        self.assertTrue(final["cached"])
        self.assertTrue(final["stale"])
        self.assertIn("upstream unavailable", final["error"])
        self.assertEqual(final["items"][0]["c_code"], "C1")
        retried = jobs.start("model", True, "a", 3)
        self.assertFalse(retried["done"], "A failed refresh must not make old cache fresh again")
        self.finish(jobs, retried)

    def test_successful_empty_refresh_invalidates_old_result_cache(self):
        calls = []
        def loader(*args, **options):
            calls.append(1)
            return {"ok": True, "items": [{"c_code": "C1"}] if len(calls) == 1 else []}
        jobs = self.create(loader)
        self.finish(jobs, jobs.start("model", True, "a", 1))
        self.finish(jobs, jobs.start("model", True, "a", 2, force_refresh=True))
        next_search = jobs.start("model", True, "a", 3)
        self.assertEqual(next_search["items"], [])
        self.assertFalse(next_search["cached"])


class ProgressiveFrontendTest(unittest.TestCase):
    def test_progressive_search_polling_starts_fast_and_backs_off(self):
        html = render_index(SimpleNamespace(project_root=ROOT, config={}))
        self.assertIn(
            "const pollDelay = Math.min(250, 80 + Math.floor((Date.now() - started) / 500) * 50);",
            html,
        )
        self.assertIn("setTimeout(resolve, pollDelay)", html)

    def test_stock_toggle_only_filters_current_items_without_searching(self):
        html = render_index(SimpleNamespace(project_root=ROOT, config={}))
        self.assertIn('inStockOnly.addEventListener("change", renderCandidateResults)', html)
        self.assertTrue('forceCandidateSearch' not in html)
        self.assertIn('progressive=1', html)
        self.assertIn('candidateKeyword.addEventListener("input", invalidateCandidateSearch)', html)
        self.assertNotIn('inStockOnly.addEventListener("change", searchCandidates)', html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_late_response_and_local_filter_behavior_in_node(self):
        page = render_index(SimpleNamespace(project_root=ROOT, config={}))
        functions = re.search(r'function invalidateCandidateSearch\(\).*?(?=async function chooseCandidate)', page, re.S).group()
        harness = r'''
const assert = require('node:assert/strict');
let candidateSearchToken=0, candidateSearchController=null, candidateRawItems=[];
const candidateParameterRequests=new Map(), normalizeCCode=value=>value||'';
let currentCandidateItems, currentProcurementQuotes;
const candidateSearchClient='test', candidateKeyword={value:'OLD'}, inStockOnly={checked:false};
const candidateModal={classList:{contains:()=>true}}, candidateRows={innerHTML:''};
const candidateSearchProgress={textContent:''}, pending=[], urls=[];
let rendered=[];
const esc=String, candidateStockNumber=item=>Number(item.stock);
const renderCandidateRows=items=>{rendered=items;};
const candidateFilters=new Map();
const resetCandidateFilters=()=>candidateFilters.clear(), renderCandidateFilterPanel=()=>{};
const candidateResultEntries=items=>items.map(item=>({item}));
const filterCandidateEntries=(entries,filters,stockOnly)=>entries.filter(e=>!stockOnly || Number(e.item.stock)>0);
const fetch=(url,options)=>{
  urls.push(url);
  if(url.includes('_cancel')) return Promise.resolve({ok:true});
  return new Promise(resolve=>pending.push({url,resolve}));
};
'''
        verify = r'''
(async()=>{
 const old=searchCandidates();
 candidateKeyword.value='NEW'; invalidateCandidateSearch();
 const recent=searchCandidates(true);
 assert.ok(pending[1].url.includes('force=1'));
 pending[1].resolve({ok:true,json:async()=>({ok:true,done:false,job_id:'new',revision:1,items:[],sources:{domestic:'empty',partner:'empty'}})});
 await new Promise(resolve=>setTimeout(resolve,300));
 assert.doesNotMatch(candidateSearchProgress.textContent,/无结果|查询完成/,'A running search must not claim no results');
 pending[2].resolve({ok:true,json:async()=>({ok:true,done:true,revision:2,items:[{model:'NEW',stock:1},{model:'EMPTY',stock:0}],sources:{domestic:'empty',partner:'empty',fallback:'ready'}})});
 await recent;
 assert.match(candidateSearchProgress.textContent,/补充检索：已返回/);
 assert.match(candidateSearchStatusText({done:true,items:[]}),/未找到匹配器件/);
 assert.doesNotMatch(candidateSearchStatusText({done:false,items:[]}),/未找到|无结果|完成/);
 assert.match(candidateSearchStatusText({done:false,items:[{}]}),/部分结果/);
 assert.match(candidateSearchStatusText({done:false,cached:true,items:[{}]}),/显示缓存，正在更新/);
 assert.match(candidateSearchStatusText({done:true,cached:true,stale:true,items:[{}],error:'offline'}),/offline/);
 pending[0].resolve({ok:true,json:async()=>({ok:true,done:true,revision:1,items:[{model:'OLD',stock:1}]})});
 await old;
 assert.equal(rendered[0].model,'NEW');
 const requests=urls.length;
 inStockOnly.checked=true; renderCandidateResults();
 assert.deepEqual(rendered.map(item=>item.model),['NEW']);
 inStockOnly.checked=false; renderCandidateResults();
 assert.equal(rendered.length,2); assert.equal(urls.length,requests);
 candidateKeyword.value='TYPED'; invalidateCandidateSearch();
 assert.equal(candidateRawItems.length,0);
 console.log('race, force, invalidation and local stock filter: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        completed = subprocess.run([shutil.which("node"), "-e", harness + functions + verify], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


class ProgressiveApiTest(unittest.TestCase):
    def test_start_poll_force_cancel_and_legacy_route(self):
        with temporary_project() as directory:
            root = Path(directory)
            (root / "BOM").mkdir()
            config = root / "BOM/config.json"
            config.write_text(json.dumps({"source_bom": "BOM/source.csv", "schematic_dir": "DOC",
                "output_overrides": "BOM/overrides.json", "stock_cache": "BOM/stock.json"}), encoding="utf-8")
            app = PartSelectionApp(config)
            app.search_jobs.close()
            entered, release = threading.Event(), threading.Event()
            calls = []

            def loader(keyword, substitutes, **options):
                calls.append(options["force_refresh"])
                options["on_partial"]({"ok": True, "items": [{"c_code": "C1"}],
                                       "sources": {"partner": "querying"}})
                entered.set()
                release.wait(3)
                return {"ok": True, "items": [{"c_code": "C1", "stock": "10"}], "sources": {"partner": "ready"}}

            app.search_jobs = importlib.import_module("part_selection_search").CandidateSearchJobs(loader)
            http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{http.server_port}"
            def get(path):
                with urlopen(base + path, timeout=3) as response:
                    return json.load(response)
            try:
                start = get("/api/lcsc_search?progressive=1&keyword=model&client=test&sequence=1")
                self.assertFalse(start["done"])
                self.assertTrue(entered.wait(1))
                progress = get("/api/lcsc_search?progressive=1&job=" + start["job_id"])
                self.assertEqual(progress["items"][0]["c_code"], "C1")
                self.assertFalse(progress["done"])
                release.set()
                for _ in range(100):
                    final = get("/api/lcsc_search?progressive=1&job=" + start["job_id"])
                    if final["done"]:
                        break
                    time.sleep(.01)
                self.assertTrue(final["done"])
                fresh = get("/api/lcsc_search?progressive=1&keyword=MODEL&client=test&sequence=2")
                self.assertTrue(fresh["cached"])
                forced = get("/api/lcsc_search?progressive=1&keyword=model&client=test&sequence=3&force=1")
                for _ in range(100):
                    if get("/api/lcsc_search?progressive=1&job=" + forced["job_id"])["done"]:
                        break
                    time.sleep(.01)
                self.assertEqual(calls, [False, True])
                req = Request(base + "/api/lcsc_search_cancel", data=json.dumps({"client": "test", "sequence": 4}).encode(),
                              headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=3) as response:
                    self.assertTrue(json.load(response)["ok"])
                self.assertTrue(get("/api/lcsc_search?progressive=1&keyword=old&client=test&sequence=3")["cancelled"])
                with patch.object(app, "lcsc_search", return_value={"ok": True, "items": []}) as legacy:
                    self.assertTrue(get("/api/lcsc_search?keyword=legacy")["ok"])
                    legacy.assert_called_once_with("legacy", True)
                self.assertFalse(app.stock_cache_path.exists())
            finally:
                release.set()
                http.shutdown()
                http.server_close()
                thread.join(2)
                app.search_jobs.close()


if __name__ == "__main__":
    unittest.main()
