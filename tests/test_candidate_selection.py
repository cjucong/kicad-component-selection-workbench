import json
import re
import shutil
import subprocess
import sys
from _scratch import temporary_project
import threading
import time
import unittest
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from part_selection_helper import read_schematic_symbol_field_values, update_schematic_symbol_fields
from part_selection_overrides import load_overrides, save_override
from part_selection_server import PartSelectionApp, make_handler, render_index, row_schematic_sync_fields
from part_stock_lookup import parse_lcsc_product


class CandidateSelectionTest(unittest.TestCase):
    def test_preview_does_not_save_and_confirmation_uses_bound_candidate(self):
        from unittest.mock import Mock
        service = Mock()
        service.prepare.return_value = {"ok": True, "preview_id": "preview", "compatible": True}
        self.app.footprint_choices = service
        payload = {"refs": "R85,R86", "candidate": self.item, "footprint_mode": "convert", "phase": "preview"}
        before = (self.root / "DOC/test.kicad_sch").read_bytes()
        result = self.app.select_candidate(payload)
        self.assertEqual(result['preview']['preview_id'], 'preview')
        self.assertFalse(self.app.overrides_path.exists())
        self.assertFalse(self.app.stock_cache_path.exists())
        service.context.return_value = service.bind.call_args.args[1]
        service.commit.return_value = {'footprint': 'DSMU:0805_C4283613', 'symbol': 'LCSC_C4283613:Part'}
        result = self.app.select_candidate({'footprint_choice': {'preview_id': 'preview'},
            'candidate': {'c_code': 'C999', 'model': 'injected'}, 'refs': 'R999', 'replace_value': True})
        self.assertEqual(result['refs'], ['R85', 'R86'])
        saved = load_overrides(self.app.overrides_path)
        self.assertEqual(saved['R85']['accepted_c_code'], 'C4283613')
        self.assertEqual(saved['R85']['field_overrides']['footprint'], 'DSMU:0805_C4283613')
        self.assertNotIn('value', saved['R85']['field_overrides'])
        self.assertEqual((self.root / 'DOC/test.kicad_sch').read_bytes(), before)

    def test_preview_stale_row_rejected_before_library_publish(self):
        from unittest.mock import Mock
        service = Mock()
        service.prepare.return_value = {'ok': True, 'preview_id': 'preview'}
        self.app.footprint_choices = service
        self.app.select_candidate({'refs': 'R85', 'candidate': self.item, 'footprint_mode': 'convert', 'phase': 'preview'})
        service.context.return_value = service.bind.call_args.args[1]
        save_override(self.app.overrides_path, 'R85', {'note': 'new decision'})
        with self.assertRaisesRegex(ValueError, '变化|修改'):
            self.app.select_candidate({'footprint_choice': {'preview_id': 'preview'}})
        service.commit.assert_not_called()

    def test_preview_http_job_and_confirm_protocol(self):
        from unittest.mock import Mock
        service = Mock()
        service.prepare.return_value = {'ok': True, 'preview_id': 'p-http', 'compatible': True}
        service.commit.return_value = {'footprint': 'DSMU:0805_C4283613'}
        self.app.footprint_choices = service
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            req = Request('http://127.0.0.1:' + str(server.server_port) + path, data=data,
                          headers={'Content-Type':'application/json'})
            with urlopen(req, timeout=10) as response:
                return json.load(response)
        try:
            started = request('/api/start_selection', {'refs':'R85', 'candidate':self.item,
                              'footprint_mode':'convert', 'phase':'preview'})
            for _ in range(100):
                status = request('/api/selection_job?id=' + started['job_id'])
                if status['done']:
                    break
                time.sleep(.01)
            self.assertEqual(status['result']['preview']['preview_id'], 'p-http')
            self.assertFalse(self.app.overrides_path.exists())
            service.context.return_value = service.bind.call_args.args[1]
            saved = request('/api/select_candidate', {'footprint_choice':{'preview_id':'p-http'}})
            self.assertEqual(saved['fields']['footprint'], 'DSMU:0805_C4283613')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            self.app.selection_jobs.close()

    def test_row_change_while_waiting_for_save_lock_does_not_publish_library(self):
        from unittest.mock import Mock
        service = Mock()
        service.prepare.return_value = {'ok': True, 'preview_id': 'race'}
        self.app.footprint_choices = service
        self.app.select_candidate({'refs':'R85', 'candidate':self.item, 'footprint_mode':'convert', 'phase':'preview'})
        service.context.return_value = service.bind.call_args.args[1]
        service.commit.return_value = {'footprint':'DSMU:0805_C4283613'}
        real_lock = self.app.selection_save_lock
        root_app = self.app
        class RacingLock:
            def __enter__(self):
                real_lock.acquire()
                save_override(root_app.overrides_path, 'R85', {'note':'changed while waiting'})
            def __exit__(self, *args):
                real_lock.release()
        self.app.selection_save_lock = RacingLock()
        with self.assertRaisesRegex(ValueError, '变化|修改'):
            self.app.select_candidate({'footprint_choice':{'preview_id':'race'}})
        service.commit.assert_not_called()

    def test_replacement_controls_are_three_default_keep_selects_after_search(self):
        html = render_index(self.app)
        self.assertTrue('id="runCandidateSearch"' not in html)
        self.assertTrue('forceCandidateSearch' not in html)
        self.assertIn('candidateKeyword.addEventListener("keydown"', html)
        self.assertGreater(html.index('id="candidateReplacementOptions"'), html.index('id="candidateKeyword"'))
        self.assertLess(html.index('id="candidateReplacementOptions"'), html.index('id="openOfficialSearch"'))
        self.assertIn('-webkit-user-select: text', html)

        class SelectParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.controls = {}
                self.current = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'select':
                    self.current = attrs.get('id')
                    self.controls[self.current] = []
                elif tag == 'option' and self.current:
                    self.controls[self.current].append((attrs.get('value'), 'selected' in attrs))

            def handle_endtag(self, tag):
                if tag == 'select':
                    self.current = None

        parser = SelectParser()
        parser.feed(html)
        for key, values in [('candidateFootprintMode', ['keep', 'library', 'convert']),
                            ('candidateReplaceValue', ['false', 'true']),
                            ('candidateReplaceComment', ['false', 'true'])]:
            with self.subTest(key=key):
                options = parser.controls.get(key, [])
                self.assertEqual([value for value, _ in options], values)
                self.assertEqual([value for value, selected in options if selected], [values[0]])

    def test_library_mode_binds_candidate_and_uses_library_service_without_conversion(self):
        from unittest.mock import Mock
        service = Mock()
        service.prepare_library.return_value = {'kind':'library', 'preview_id':'lib-pick', 'items':[]}
        service.commit.return_value = {'footprint':'DSMU:0805', 'reused':True}
        self.app.footprint_choices = service
        with patch('part_selection_library.LibraryImporter.convert') as convert:
            result = self.app.select_candidate({'refs':'R85,R86', 'candidate':self.item,
                'footprint_mode':'library', 'phase':'preview'})
            self.assertEqual(result['preview']['kind'], 'library')
            self.assertFalse(self.app.overrides_path.exists())
            service.context.return_value = service.bind.call_args.args[1]
            saved = self.app.select_candidate({'footprint_choice':{'preview_id':'lib-pick'}})
            self.assertEqual(saved['fields']['footprint'], 'DSMU:0805')
            self.assertNotIn('value', saved['fields'])
            self.assertNotIn('comment', saved['fields'])
            convert.assert_not_called()
        service.prepare.assert_not_called()

    def test_library_mode_cannot_bypass_picker_validation(self):
        with self.assertRaisesRegex(ValueError, '预览|选择|preview'):
            self.app.select_candidate({'refs':'R85', 'candidate':self.item, 'footprint_mode':'library',
                'footprint':'DSMU:0805'})
        self.assertFalse(self.app.overrides_path.exists())

    def setUp(self):
        self.tmp = temporary_project()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "BOM").mkdir()
        (self.root / "DOC").mkdir()
        self.config = self.root / "BOM/config.json"
        self.config.write_text(json.dumps({
            "source_bom": "BOM/source.csv", "schematic_dir": "DOC",
            "output_overrides": "BOM/overrides.json", "stock_cache": "BOM/stock.json",
        }), encoding="utf-8")
        (self.root / "BOM/source.csv").write_text(
            'Comment,Description,Designator,Footprint,Value\nResistor,old,"R85,R86",OLD_FP,10k\n',
            encoding="utf-8",
        )
        symbols = "".join(
            f'(symbol (lib_id "Device:R") (at 0 0 0) '
            f'(property "Reference" "{ref}" (at 0 0 0)) '
            '(property "Comment" "Resistor" (at 0 0 0)) '
            '(property "Value" "10k" (at 0 0 0)) '
            '(property "Footprint" "OLD_FP" (at 0 0 0)))'
            for ref in ("R85", "R86")
        )
        (self.root / "DOC/test.kicad_sch").write_text(f"(kicad_sch {symbols})", encoding="utf-8")
        self.app = PartSelectionApp(self.config)
        self.item = {
            "c_code": "C4283613", "model": "P2TC0805D5002LNTA", "package": "0805",
            "param": "RES 50kΩ ±0.01% 200mW 0805",
            "param_detail": "电阻类型: 薄膜电阻; 阻值: 50kΩ; 精度: ±0.01%; 功率: 200mW",
            "stock": "1", "arrange": "Tape & Reel (TR)", "category": "Chip Resistor - Surface Mount",
        }

    def select(self, item=None, refs="R85,R86"):
        self.assertTrue(callable(getattr(self.app, "select_candidate", None)),
                        "Selecting a candidate must persist all replacement fields, not just its C code")
        return self.app.select_candidate({"refs": refs, "candidate": self.item if item is None else item,
                                         "replace_comment": True, "replace_value": True})

    def test_omitted_replacement_options_keep_actual_and_pending_fields(self):
        save_override(self.app.overrides_path, "R85", {"field_overrides": {"comment": "my resistor", "value": "12k"}})
        before = (self.root / "DOC/test.kicad_sch").read_bytes()
        self.app.select_candidate({"refs": "R85,R86", "candidate": self.item})
        stored = load_overrides(self.app.overrides_path)
        self.assertEqual(stored["R85"]["field_overrides"]["comment"], "my resistor")
        self.assertEqual(stored["R85"]["field_overrides"]["value"], "12k")
        self.assertNotIn("comment", stored["R86"]["field_overrides"])
        self.assertNotIn("value", stored["R86"]["field_overrides"])
        rows = PartSelectionApp(self.config).rows("R85,R86", "__schematic__", include_stock=False)
        by_ref = {ref: row for row in rows for ref in row["refs_list"]}
        self.assertEqual((by_ref["R86"]["comment"], by_ref["R86"]["value"]), ("Resistor", "10k"))
        self.assertEqual(stored["R86"]["accepted_c_code"], "C4283613")
        self.assertIn("P2TC0805D5002LNTA", stored["R86"]["field_overrides"]["description"])
        self.assertEqual((self.root / "DOC/test.kicad_sch").read_bytes(), before)

    def test_value_and_comment_replacement_can_be_selected_independently(self):
        for options, want_comment, want_value in [
            ({}, "Resistor", "10k"),
            ({"replace_value": True}, "Resistor", "50kΩ"),
            ({"replace_comment": True}, "resistor", "10k"),
            ({"replace_comment": True, "replace_value": True}, "resistor", "50kΩ"),
        ]:
            with self.subTest(options=options):
                if self.app.overrides_path.exists():
                    self.app.overrides_path.unlink()
                self.app.select_candidate({"refs": "R85", "candidate": self.item, **options})
                row = self.app.rows("R85", "__schematic__", include_stock=False)[0]
                self.assertEqual(row["comment"], want_comment)
                self.assertEqual(row["value"], want_value)
                self.assertIn("P2TC0805D5002LNTA", row_schematic_sync_fields(row)["description"])

    def test_passive_category_and_chip_model_fields_are_derived_per_reference(self):
        cases = [
            ("R85", self.item, "resistor", "50kΩ"),
            ("C1", {**self.item, "model": "GRM155R71H104KE14D", "param_detail": "电容量: 100nF"}, "capacitor", "100nF"),
            ("L1", {**self.item, "model": "XAL4020-102MEB", "param_detail": "电感值: 1uH"}, "inductor", "1uH"),
            ("U1", {**self.item, "model": "AD4630-24BBCZ-RL", "param_detail": "分辨率: 24"}, "AD4630-24BBCZ-RL", "AD4630-24BBCZ-RL"),
        ]
        for ref, candidate, want_comment, want_value in cases:
            with self.subTest(ref=ref):
                self.select(candidate, ref)
                fields = load_overrides(self.app.overrides_path)[ref]["field_overrides"]
                self.assertEqual(fields["comment"], want_comment)
                self.assertEqual(fields["value"], want_value)
                self.assertIn(candidate["model"], fields["description"])

    def test_empty_parameters_still_preserve_selected_model_in_description(self):
        self.app.select_candidate({"refs": "R85", "candidate": {**self.item, "param": "", "param_detail": ""}})
        stored = load_overrides(self.app.overrides_path)["R85"]["field_overrides"]
        self.assertIn("P2TC0805D5002LNTA", stored["description"])
        self.assertEqual(stored["description_c_code"], "C4283613")

    def test_non_boolean_replacement_options_are_rejected_before_conversion(self):
        for field, value in [("replace_value", "false"), ("replace_comment", 1), ("replace_value", None)]:
            with self.subTest(field=field), patch("part_selection_library.LibraryImporter.convert") as convert:
                with self.assertRaisesRegex(ValueError, field):
                    self.app.select_candidate({"refs": "R85", "candidate": self.item, "footprint_mode": "convert", field: value})
                convert.assert_not_called()
        self.assertFalse(self.app.overrides_path.exists())
        self.assertFalse(self.app.stock_cache_path.exists())

    def test_selection_updates_group_and_survives_app_restart(self):
        save_override(self.app.overrides_path, "R85", {
            "note": "keep note", "review_status": "Confirmed",
            "alternate_parts": [{"c_code": "C4283613"}, {"c_code": "C123"}],
            "procurement_choice": {"order_url": "https://so.szlcsc.com/order/old"},
        })
        self.assertTrue(self.select()["ok"])
        stored = load_overrides(self.app.overrides_path)
        for ref in ("R85", "R86"):
            fields = stored[ref]["field_overrides"]
            self.assertEqual(fields["comment"], "resistor")
            self.assertEqual(fields["value"], "50kΩ")
            self.assertEqual(fields["footprint"], "OLD_FP")
            self.assertIn(self.item["param_detail"], fields["description"])
            self.assertEqual(stored[ref]["accepted_c_code"], "C4283613")
            self.assertFalse(stored[ref].get("procurement_choice"))
        self.assertEqual(stored["R85"]["note"], "keep note")
        self.assertEqual(stored["R85"]["review_status"], "Confirmed")
        self.assertEqual([p["c_code"] for p in stored["R85"]["alternate_parts"]], ["C123"])
        rows = PartSelectionApp(self.config).rows("R85,R86", "BOM/source.csv", include_stock=False)
        for row in rows:
            fields = row_schematic_sync_fields(row)
            self.assertEqual(fields["comment"], "resistor")
            self.assertEqual(fields["value"], "50kΩ")
            self.assertEqual(fields["footprint"], "OLD_FP")
            self.assertIn(self.item["param_detail"], fields["description"])
            self.assertTrue(row["field_changed_refs"])
            self.assertIn(self.item["param_detail"], row["description"])

    def test_passive_values_and_chip_models_are_not_truncated(self):
        cases = [
            ("C1", "GRM155R71H104KE14D", "电容量：100nF、精度：±10%", "100nF"),
            ("L1", "XAL4020-102MEB", "电感值: 1uH; 饱和电流: 8A", "1uH"),
            ("U1", "AD4630-24BBCZ-RL", "分辨率: 24; 通道数: 2", "AD4630-24BBCZ-RL"),
            ("R1", "P2TC0805D5002LNTA", "", "50kΩ"),
        ]
        for ref, model, detail, expected in cases:
            with self.subTest(ref=ref):
                self.select({**self.item, "model": model, "param_detail": detail}, refs=ref)
                fields = load_overrides(self.app.overrides_path)[ref]["field_overrides"]
                self.assertEqual(fields["comment"], {"C1": "capacitor", "L1": "inductor", "R1": "resistor"}.get(ref, model))
                self.assertEqual(fields["value"], expected)

    def test_missing_candidate_data_does_not_erase_existing_fields(self):
        self.select({**self.item, "package": "", "param": "", "param_detail": ""})
        row = self.app.rows("R85", "BOM/source.csv", include_stock=False)[0]
        self.assertEqual(row["footprint"], "OLD_FP")
        self.assertEqual(row["value"], "10k")

    def test_description_remains_available_without_stock_cache(self):
        self.select()
        self.app.stock_cache_path.unlink()
        row = PartSelectionApp(self.config).rows("R85", "BOM/source.csv", include_stock=False)[0]
        self.assertIn(self.item["param_detail"], row_schematic_sync_fields(row)["description"])
        self.app.save_override({"refs": "R85", "accepted_c_code": "C123", "stock_c_code": "C123"})
        row = self.app.rows("R85", "BOM/source.csv", include_stock=False)[0]
        self.assertNotIn("description", row_schematic_sync_fields(row))

    def test_selected_fields_round_trip_to_schematic(self):
        self.select()
        row = self.app.rows("R85,R86", "BOM/source.csv", include_stock=False)[0]
        desired = row_schematic_sync_fields(row)
        result = update_schematic_symbol_fields(self.root / "DOC", ["R85", "R86"], desired)
        self.assertEqual(sorted(result["updated_refs"]), ["R85", "R86"])
        actual = read_schematic_symbol_field_values(self.root / "DOC", ["R85", "R86"], tuple(desired))
        for ref in actual:
            self.assertEqual(actual[ref], desired)
        row = PartSelectionApp(self.config).rows("R85,R86", "__schematic__", include_stock=False)[0]
        self.assertFalse(row["field_changed_refs"])

    def test_invalid_selection_does_not_write_any_files(self):
        for item in ({**self.item, "c_code": "invalid"}, {**self.item, "model": ""}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.select(item)
        self.assertFalse(self.app.overrides_path.exists())
        self.assertFalse(self.app.stock_cache_path.exists())

    def test_local_save_error_is_distinct_from_conversion_failure(self):
        with patch('part_selection_server.save_overrides', side_effect=PermissionError('file locked')):
            with self.assertRaisesRegex(RuntimeError, '本地保存失败.*file locked'):
                self.select()
        self.assertFalse(self.app.overrides_path.exists())

    def test_default_keeps_each_references_pending_or_actual_footprint(self):
        save_override(self.app.overrides_path, "R85", {"field_overrides": {"footprint": "DSMU:Pending"}})
        self.select()
        stored = load_overrides(self.app.overrides_path)
        self.assertEqual(stored["R85"]["field_overrides"]["footprint"], "DSMU:Pending")
        self.assertEqual(stored["R86"]["field_overrides"]["footprint"], "OLD_FP")

    def test_conversion_failure_does_not_save_candidate_or_stock(self):
        with patch("part_selection_library.LibraryImporter.convert", side_effect=RuntimeError("conversion failed")):
            with self.assertRaisesRegex(RuntimeError, "conversion failed"):
                self.app.select_candidate({"refs": "R85", "candidate": self.item, "footprint_mode": "convert"})
        self.assertFalse(self.app.overrides_path.exists())
        self.assertFalse(self.app.stock_cache_path.exists())

    def test_converted_footprint_comes_from_verified_import_not_candidate(self):
        library = {"footprint": "LCSC_C4283613:Part", "symbol": "LCSC_C4283613:Part"}
        with patch("part_selection_library.LibraryImporter.convert", return_value=library):
            result = self.app.select_candidate({"refs": "R85", "candidate": {**self.item, "footprint": "Fake:Injected"}, "footprint_mode": "convert"})
        self.assertEqual(result["library"], library)
        self.assertEqual(load_overrides(self.app.overrides_path)["R85"]["field_overrides"]["footprint"], library["footprint"])
        self.assertNotIn("comment", result["fields"])
        self.assertNotIn("value", result["fields"])

    def test_mixed_references_do_not_use_first_references_category_for_all(self):
        self.select(refs="R85,U1")
        stored = load_overrides(self.app.overrides_path)
        self.assertEqual(stored["R85"]["field_overrides"]["comment"], "resistor")
        self.assertEqual(stored["R85"]["field_overrides"]["value"], "50kΩ")
        self.assertEqual(stored["U1"]["field_overrides"]["comment"], "P2TC0805D5002LNTA")
        self.assertEqual(stored["U1"]["field_overrides"]["value"], "P2TC0805D5002LNTA")

    def test_unknown_footprint_mode_fails_before_saving(self):
        with self.assertRaises(ValueError):
            self.app.select_candidate({"refs": "R85", "candidate": self.item, "footprint_mode": "bad"})
        self.assertFalse(self.app.overrides_path.exists())

    def test_old_conversion_cannot_replace_newer_selection(self):
        def convert(code, progress=None):
            self.app.select_candidate({"refs": "R85", "candidate": {**self.item, "c_code": "C456"}})
            return {"footprint": "LCSC_C4283613:Part"}
        with patch("part_selection_library.LibraryImporter.convert", side_effect=convert):
            with self.assertRaisesRegex(ValueError, "旧转换结果"):
                self.app.select_candidate({"refs": "R85", "candidate": self.item, "footprint_mode": "convert"})
        self.assertEqual(load_overrides(self.app.overrides_path)["R85"]["accepted_c_code"], "C456")

    def test_complete_parameters_are_not_limited_to_twelve_attributes(self):
        params = [{"paramName": f"attribute{i}", "paramValue": str(i)} for i in range(20)]
        item = parse_lcsc_product("C123", {"productModel": "MODEL", "paramVOList": params})
        self.assertIn("attribute19: 19", item["param_detail"])

    def test_search_highlights_never_enter_stock_parameters(self):
        item = parse_lcsc_product("C88946", {
            "productModel": "GRM155R71A474KE01D",
            "productKeyAttributes": "<span class='lucene_highlight_class'>470nF</span> 10V",
            "paramVOList": [{"paramName": "容值",
                             "paramValue": "<SPAN CLASS='LUCENE_HIGHLIGHT_CLASS'>470nF</SPAN>"}],
        })
        self.assertEqual(item["param"], "470nF 10V")
        self.assertEqual(item["param_detail"], "容值: 470nF")

    def test_selection_is_not_mistaken_for_an_external_kicad_edit(self):
        save_override(self.app.overrides_path, "R85", {"schematic_synced_at": "2099-01-01T00:00:00"})
        self.select()
        row = self.app.rows("R85", "BOM/source.csv", include_stock=False)[0]
        self.assertEqual(row["footprint"], "OLD_FP")
        self.assertEqual(row["comment"], "resistor")

    def test_different_saved_parameters_stay_per_ref_inside_shared_group(self):
        self.select(refs="R85")
        self.select({**self.item, "param_detail": self.item["param_detail"] + "; rating: special"}, refs="R86")
        rows = self.app.rows("R85,R86", "BOM/source.csv", include_stock=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["refs"], "R85,R86")
        self.assertNotEqual(rows[0]["description_by_ref"]["R85"], rows[0]["description_by_ref"]["R86"])
        self.assertEqual(rows[0]["per_ref_sync_fields"]["R85"]["description"],
                         rows[0]["description_by_ref"]["R85"])
        self.assertEqual(rows[0]["per_ref_sync_fields"]["R86"]["description"],
                         rows[0]["description_by_ref"]["R86"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser-function verification")
    def test_browser_candidate_selection_sends_full_record_and_keeps_row_anchor(self):
        html = render_index(self.app)
        function = re.search(r"async function chooseCandidate\(code\) \{.*?\n\}", html, re.S).group()
        functions = "\n".join(re.search(rf"function {name}\(.*?\n\}}", html, re.S).group()
                              for name in ("stockWithSavedDescription", "stockParamText", "initialStock", "schematicDescription"))
        js = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const item = {c_code:'C4283613',model:'P2TC0805D5002LNTA',package:'0805',param_detail:'full details'};
const calls = [];
const button = {disabled:false};
const ctx = {
  rowDataForElement: ()=>null, candidateOnly:false,
  candidateTargetRow: {dataset:{ref:'R85',refs:'R85,R86'}},
  candidateSearchController: {abort:()=>calls.push('abort')},
  candidateSearchToken: 0,
  candidateSearchProgress: {textContent:''},
  candidateRows: {querySelectorAll:()=>[button]},
  stockCode: row=>row.stock_c_code,
  currentCandidateItems: new Map([['C4283613',item]]),
  normalizeCCode: code=>code, rowAnchor: tr=>({ref:tr.dataset.ref}),
  fetch: async (url,options)=>{assert.equal(button.disabled,true);assert.equal(ctx.candidateTargetRow.dataset.candidateSaving,'1');calls.push([url,JSON.parse(options.body)]);return {ok:true,json:async()=>({ok:true})}},
  closeCandidates: ()=>calls.push('close'),
  loadRows: async anchor=>calls.push(['reload',anchor]), setState: text=>calls.push(['state',text])
};
vm.createContext(ctx);
vm.runInContext(FUNCTION,ctx);
vm.runInContext(FUNCTIONS,ctx);
(async()=>{
  const row={stock_c_code:'C4283613',override:{field_overrides:{description_c_code:'C4283613',description:'line one\nline two'}}};
  const stock=ctx.initialStock(row);
  assert.equal(ctx.stockParamText(stock),'line one\nline two');
  const material = ctx.stockParamText({param:'Capacitance: 100nF',param_detail:'Brand: Murata; Temperature: X7R',package:'0402',brand:'Murata',arrange:'Tape Reel',category:'MLCC capacitor'});
  assert.equal(material,'Capacitance: 100nF\nBrand: Murata; Temperature: X7R\n\u5c01\u88c5: 0402');
  assert.equal(ctx.schematicDescription({querySelector:()=>({dataset:{stockParamText:ctx.stockParamText(stock)}})}),'line one\nline two');
  await ctx.chooseCandidate('C4283613');
  assert.equal(calls[0],'abort');
  assert.equal(calls[1][0],'/api/select_candidate');
  assert.deepEqual(calls[1][1].candidate,item);
  assert.equal(calls[1][1].refs,'R85,R86');
  assert.equal(calls[1][1].replace_value,false);
  assert.equal(calls[1][1].replace_comment,false);
  assert.equal(calls[2],'close');
  assert.equal(calls[3][0],'state');
  assert.equal(calls[4][0],'reload');
  assert.equal(calls[4][1].ref,'R85');
  assert.equal(calls[5][0],'state');
  assert.ok(ctx.candidateSearchProgress.textContent.length > 0);
  assert.equal(button.disabled,false);
  assert.equal(ctx.candidateTargetRow.dataset.candidateSaving,undefined);
  calls.length=0;
  ctx.candidateFootprintMode={value:'convert'};
  ctx.candidateReplaceValue={value:'true'};
  ctx.candidateReplaceComment={value:'false'};
  ctx.Date=Date;
  ctx.setTimeout=fn=>fn();
  let polls=0;
  ctx.fetch=async(url,options)=>{
    if(url==='/api/start_selection'){
      assert.equal(JSON.parse(options.body).footprint_mode,'convert');
      assert.equal(JSON.parse(options.body).replace_value,true);
      assert.equal(JSON.parse(options.body).replace_comment,false);
      return {ok:true,json:async()=>({ok:true,job_id:'test',done:false,stage:'queued'})};
    }
    assert.equal(url,'/api/selection_job?id=test');
    polls++;
    if(polls===1) throw new Error('temporary network failure');
    return {ok:true,json:async()=>({ok:true,job_id:'test',done:true,result:{ok:true}})};
  };
  await ctx.chooseCandidate('C4283613');
  assert.equal(polls,2);
  assert.equal(calls[0],'close');
  assert.ok(calls.at(-1)[1].includes('PCB'));
  assert.equal(ctx.candidateTargetRow.dataset.candidateSaving,undefined);
  calls.length=0;
  let commits=0;
  ctx.window={chooseFootprint:async preview=>{assert.equal(preview.preview_id,'p1');return null}};
  ctx.fetch=async(url,options)=>{
    if(url==='/api/start_selection'){
      assert.equal(JSON.parse(options.body).phase,'preview');
      return {ok:true,json:async()=>({ok:true,preview:{preview_id:'p1'}})};
    }
    assert.equal(url,'/api/select_candidate');
    assert.equal(JSON.parse(options.body).footprint_choice.preview_id,'p1');
    commits++;
    return {ok:true,json:async()=>({ok:true})};
  };
  await ctx.chooseCandidate('C4283613');
  assert.equal(commits,0);
  assert.equal(calls.length,0);
  assert.equal(button.disabled,false);
  ctx.window.chooseFootprint=async()=>({preview_id:'p1',source:'converted'});
  await ctx.chooseCandidate('C4283613');
  assert.equal(commits,1);
  assert.equal(calls[0],'close');
  calls.length=0;
  ctx.candidateFootprintMode={value:'library'};
  ctx.window.chooseLibraryFootprint=async preview=>{assert.equal(preview.kind,'library');return null};
  ctx.window.chooseFootprint=async()=>{throw Error('conversion picker must not run')};
  let libraryReads=0;
  ctx.fetch=async(url,options)=>{
    assert.equal(url,'/api/select_candidate');
    const payload=JSON.parse(options.body);
    if(payload.footprint_choice){
      assert.equal(payload.footprint_choice.existing_id,'DSMU:0805');
      commits++;
      return {ok:true,json:async()=>({ok:true,library:{footprint:'DSMU:0805'}})};
    }
    assert.equal(payload.footprint_mode,'library');
    assert.equal(payload.phase,'preview');
    libraryReads++;
    return {ok:true,json:async()=>({ok:true,preview:{preview_id:'l1',kind:'library'}})};
  };
  await ctx.chooseCandidate('C4283613');
  assert.equal(commits,1);
  assert.equal(calls.length,0);
  ctx.window.chooseLibraryFootprint=async()=>({preview_id:'l1',source:'existing',existing_id:'DSMU:0805',acknowledged:true});
  await ctx.chooseCandidate('C4283613');
  assert.equal(commits,2);
  assert.equal(libraryReads,2);
  assert.ok(calls.at(-1)[1].includes('现有库封装'));
  calls.length=0;
  ctx.candidateSearchController=null;
  ctx.fetch=async()=>({ok:false,json:async()=>({ok:false,error:'cannot save'})});
  await assert.rejects(ctx.chooseCandidate('C4283613'),/cannot save/);
  assert.equal(calls.length,0);
  assert.ok(ctx.candidateSearchProgress.textContent.includes('cannot save'));
  assert.equal(button.disabled,false);
  assert.equal(ctx.candidateTargetRow.dataset.candidateSaving,undefined);
})().catch(err=>{console.error(err);process.exitCode=1});
""".replace("FUNCTIONS", json.dumps(functions)).replace("FUNCTION", json.dumps(function))
        result = subprocess.run([shutil.which("node"), "-e", js], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_select_candidate_api(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            req = Request(f"http://127.0.0.1:{server.server_port}/api/select_candidate",
                          data=json.dumps({"refs": "R85,R86", "candidate": self.item}).encode(),
                          headers={"Content-Type": "application/json"})
            try:
                with urlopen(req) as response:
                    result = json.load(response)
            except HTTPError as exc:
                self.fail(f"Candidate selection API must accept all fields (HTTP {exc.code})")
            self.assertTrue(result["ok"])
            self.assertNotIn("value", result["fields"])
            self.assertNotIn("comment", result["fields"])
            self.assertIn("P2TC0805D5002LNTA", result["fields"]["description"])
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_library_picker_api_round_trip_without_conversion_or_render(self):
        pretty = self.root / 'Libraries/DSMU/DSMU.pretty'
        pretty.mkdir(parents=True)
        (pretty / '0805.kicad_mod').write_text('(footprint "0805" (pad "1" smd rect (at -1 0) (size 1 1)) (pad "2" smd rect (at 1 0) (size 1 1)))')
        (self.root / 'fp-lib-table').write_text('(fp_lib_table (lib (name "DSMU") (type "KiCad") (uri "${KIPRJMOD}/Libraries/DSMU/DSMU.pretty")))')
        sch = self.root / 'DOC/test.kicad_sch'
        sch.write_text(sch.read_text().replace('OLD_FP', 'DSMU:0805'))
        before = sch.read_bytes()
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def post(path, payload):
            req = Request(f'http://127.0.0.1:{server.server_port}/api/{path}',
                          data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
            with urlopen(req) as response:
                return json.load(response)
        try:
            with patch('part_selection_library.LibraryImporter.convert') as convert, patch('part_selection_footprints.render_previews') as render:
                preview = post('select_candidate', {'refs':'R85,R86', 'candidate':self.item,
                    'footprint_mode':'library', 'phase':'preview'})['preview']
                detail = post('library_footprint', {'preview_id':preview['preview_id'], 'id':'DSMU:0805'})['detail']
                self.assertTrue(detail['compatible'])
                self.assertNotIn('svg', detail)
                with self.assertRaises(HTTPError):
                    post('library_footprint', {'preview_id':preview['preview_id'], 'id':'../outside'})
                result = post('select_candidate', {'footprint_choice':{'preview_id':preview['preview_id'],
                    'source':'existing', 'existing_id':'DSMU:0805', 'acknowledged':True}})
                self.assertEqual(result['footprint_mode'], 'library')
                for ref in ('R85', 'R86'):
                    self.assertEqual(load_overrides(self.app.overrides_path)[ref]['field_overrides']['footprint'], 'DSMU:0805')
                self.assertEqual(sch.read_bytes(), before)
                convert.assert_not_called()
                render.assert_not_called()
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_conversion_job_api_saves_only_verified_footprint(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        library = {"footprint": "LCSC_C4283613:R0805", "symbol": "LCSC_C4283613:Resistor"}
        try:
            with patch("part_selection_library.LibraryImporter.convert", return_value=library):
                req = Request(base + "/api/start_selection",
                    data=json.dumps({"refs": "R85,R86", "candidate": self.item, "footprint_mode": "convert", "replace_comment": True}).encode(),
                    headers={"Content-Type": "application/json"})
                with urlopen(req) as response:
                    state = json.load(response)
                deadline = time.monotonic() + 5
                while not state["done"] and time.monotonic() < deadline:
                    time.sleep(.01)
                    with urlopen(base + "/api/selection_job?id=" + state["job_id"]) as response:
                        state = json.load(response)
                self.assertTrue(state["done"])
                self.assertTrue(state["ok"])
                for ref in ("R85", "R86"):
                    fields = load_overrides(self.app.overrides_path)[ref]["field_overrides"]
                    self.assertEqual(fields["footprint"], library["footprint"])
                    self.assertEqual(fields["comment"], "resistor")
                    self.assertNotIn("value", fields)
                original = (self.root / "DOC/test.kicad_sch").read_text()
                self.assertNotIn("LCSC_C4283613", original, "Candidate selection must not silently modify schematic files")
        finally:
            server.shutdown()
            worker.join()
            server.server_close()
            self.app.selection_jobs.close()


if __name__ == "__main__":
    unittest.main()
