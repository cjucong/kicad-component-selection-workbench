import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from _scratch import temporary_project

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from part_selection_server import PartSelectionApp, render_index
from part_stock_lookup import build_candidate_item, load_stock_cache, save_stock_cache, lookup_stock_with_cache, preserve_candidate_parameters


class CandidateParameterCompletionTest(unittest.TestCase):
    def setUp(self):
        directory = temporary_project(prefix="candidate-detail-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = self.root / "config.json"
        config.write_text(json.dumps({"schematic_dir": ".", "source_bom": "__schematic__"}))
        self.app = PartSelectionApp(config)
        self.item = build_candidate_item("C101", {"productId": 321, "productCode": "C101",
            "productModel": "SYNTHETIC-RESISTOR", "catalogName": "Resistors", "stockNumber": 200,
            "paramVOList": [{"name": "阻值", "value": "15Ω"}, {"name": "功率", "value": "100mW"}]})

    def test_search_candidates_keep_numeric_product_identity_for_detail_lookup(self):
        self.assertEqual(self.item["product_id"], "321")

    def test_partial_search_cannot_erase_known_parameter_fields_or_replace_fresh_stock(self):
        cached = {**self.item, "stock": "1", "parameters": [],
                  "param_detail": "阻值: 15Ω; 功率: 100mW; 工作电压: 75V; 温度系数: ±10ppm/℃"}
        save_stock_cache(self.app.stock_cache_path, {"C101": cached})
        def search(*args, **kwargs):
            result = {"ok": True, "items": [dict(self.item)]}
            kwargs["on_partial"](result)
            return result
        partial = []
        with patch("part_selection_server.search_lcsc_candidates", side_effect=search):
            result = self.app.lcsc_search("C101", on_partial=partial.append)
        for result in (result, partial[0]):
            item = result["items"][0]
            self.assertIn("75V", item["param_detail"])
            self.assertIn("10ppm", item["param_detail"])
            self.assertEqual(item["stock"], "200")
        self.assertIn("75V", load_stock_cache(self.app.stock_cache_path)["C101"]["param_detail"])

    def test_cached_model_mismatch_cannot_leak_old_product_parameters(self):
        save_stock_cache(self.app.stock_cache_path, {"C101": {**self.item,
            "model": "WRONG-MODEL", "param_detail": "工作电压: 900V"}})
        with patch("part_selection_server.search_lcsc_candidates", return_value={"ok": True, "items": [self.item]}):
            result = self.app.lcsc_search("C101")
        self.assertNotIn("900V", result["items"][0]["param_detail"])

    def test_late_search_preserves_detail_saved_while_search_was_running(self):
        save_stock_cache(self.app.stock_cache_path, {"C101": {**self.item, "parameters": [],
            "param_detail": "阻值: 15Ω; 工作电压: 50V"}})
        def search(*args, **kwargs):
            save_stock_cache(self.app.stock_cache_path, {"C101": {**self.item, "parameters": [],
                "param_detail": "阻值: 15Ω; 工作电压: 75V; 温度系数: ±10ppm/℃"}})
            return {"ok": True, "items": [dict(self.item)]}
        with patch("part_selection_server.search_lcsc_candidates", side_effect=search):
            result = self.app.lcsc_search("C101")
        self.assertIn("75V", load_stock_cache(self.app.stock_cache_path)["C101"]["param_detail"])
        self.assertIn("75V", result["items"][0]["param_detail"])
        self.assertNotIn("50V", result["items"][0]["param_detail"])

    def test_summary_unknown_placeholders_do_not_replace_known_ratings(self):
        cached = {**self.item, "parameters": [{"name": "Working Voltage", "value": "75V"}]}
        for unknown in ("-", "", "N/A", "not specified", "未提供", None):
            summary = {**self.item, "parameters": [{"name": "Working Voltage", "value": unknown}]}
            with self.subTest(unknown=unknown):
                result = preserve_candidate_parameters(summary, cached)
                self.assertIn("75V", result["param_detail"])

    def test_stock_refresh_does_not_erase_full_ratings(self):
        save_stock_cache(self.app.stock_cache_path, {"C101": {**self.item, "ok": True, "parameters": [],
            "stock": "1", "param_detail": "阻值: 15Ω; 温度系数: ±10ppm/℃; 工作电压: 75V"}})
        result = lookup_stock_with_cache(self.app.stock_cache_path, "C101", fetcher=lambda code: {**self.item, "ok": True})
        self.assertEqual(result["stock"], "200")
        self.assertIn("75V", result["param_detail"])
        self.assertIn("10ppm", load_stock_cache(self.app.stock_cache_path)["C101"]["param_detail"])

    def test_selecting_summary_only_candidate_keeps_verified_cached_parameters(self):
        save_stock_cache(self.app.stock_cache_path, {"C101": {**self.item, "parameters": [],
            "param_detail": "阻值: 15Ω; 工作电压: 75V; 温度系数: ±10ppm/℃"}})
        with patch.object(self.app, "read_schematic_fields", return_value={"R1": {}}):
            result = self.app.select_candidate({"refs": "R1", "candidate": self.item, "footprint_mode": "keep"})
        self.assertIn("75V", result["fields"]["description"])

    def test_detail_api_is_read_only_and_exposes_verified_full_attributes(self):
        before = self.app.overrides_path.exists(), self.app.stock_cache_path.exists()
        detail = {"ok": True, "c_code": "C101", "model": "SYNTHETIC-RESISTOR", "product_id": "321",
                  "parameters": [{"name": "工作电压", "value": "75V"}, {"name": "温度系数", "value": "±10ppm/℃"}],
                  "param_detail": "工作电压: 75V; 温度系数: ±10ppm/℃", "detail_url": "https://item.szlcsc.com/321.html"}
        with patch("part_selection_product_details.fetch_product_details", return_value=detail):
            result = self.app.candidate_parameters({"c_code": "C101", "product_id": "321", "model": "SYNTHETIC-RESISTOR"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["parameters"], detail["parameters"])
        self.assertEqual(before, (self.app.overrides_path.exists(), self.app.stock_cache_path.exists()))

    def test_detail_api_rejects_arbitrary_url_or_invalid_code(self):
        for payload in ({"c_code": "C101", "product_id": "https://example.com/321"},
                        {"c_code": "bad", "product_id": "321"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.app.candidate_parameters(payload)

    def test_explicit_detail_retry_bypasses_completed_failure_cache(self):
        with patch("part_selection_product_details.fetch_product_details", return_value={"ok": False}) as fetch:
            self.app.candidate_parameters({"c_code": "C101", "product_id": "321", "force_refresh": True})
        self.assertIs(fetch.call_args.kwargs.get("force_refresh"), True)

    def test_ui_supplements_details_without_blocking_initial_search_or_accepting_late_results(self):
        source = render_index(self.app)
        self.assertIn("function supplementCandidateParameters", source)
        block = source.split("function supplementCandidateParameters", 1)[1].split("async function searchCandidates", 1)[0]
        self.assertIn('"/api/candidate_parameters"', block)
        self.assertIn("candidateSearchToken", block)
        self.assertIn("参数来源", source)

    def test_install_bundle_contains_full_parameter_reader(self):
        from kicad_plugin.package_plugin import package_files
        self.assertIn("plugins/app/part_selection_product_details.py", package_files())


if __name__ == "__main__":
    unittest.main()
