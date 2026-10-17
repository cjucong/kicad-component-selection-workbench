import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from part_selection_helper import read_schematic_symbol_field_values, update_schematic_symbol_fields
from part_selection_overrides import load_overrides, save_override
from part_selection_server import PartSelectionApp, make_handler, render_index, row_schematic_sync_fields
from part_stock_lookup import parse_lcsc_product


class CandidateSelectionTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "Temp").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "Temp")
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
        return self.app.select_candidate({"refs": refs, "candidate": self.item if item is None else item})

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
            self.assertEqual(fields["comment"], self.item["model"])
            self.assertEqual(fields["value"], "50kΩ")
            self.assertEqual(fields["footprint"], "0805")
            self.assertIn(self.item["param_detail"], fields["description"])
            self.assertEqual(stored[ref]["accepted_c_code"], "C4283613")
            self.assertFalse(stored[ref].get("procurement_choice"))
        self.assertEqual(stored["R85"]["note"], "keep note")
        self.assertEqual(stored["R85"]["review_status"], "Confirmed")
        self.assertEqual([p["c_code"] for p in stored["R85"]["alternate_parts"]], ["C123"])
        rows = PartSelectionApp(self.config).rows("R85,R86", "BOM/source.csv", include_stock=False)
        for row in rows:
            fields = row_schematic_sync_fields(row)
            self.assertEqual(fields["comment"], self.item["model"])
            self.assertEqual(fields["value"], "50kΩ")
            self.assertEqual(fields["footprint"], "0805")
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
                self.assertEqual(fields["comment"], model)
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

    def test_complete_parameters_are_not_limited_to_twelve_attributes(self):
        params = [{"paramName": f"attribute{i}", "paramValue": str(i)} for i in range(20)]
        item = parse_lcsc_product("C123", {"productModel": "MODEL", "paramVOList": params})
        self.assertIn("attribute19: 19", item["param_detail"])

    def test_selection_is_not_mistaken_for_an_external_kicad_edit(self):
        save_override(self.app.overrides_path, "R85", {"schematic_synced_at": "2099-01-01T00:00:00"})
        self.select()
        row = self.app.rows("R85", "BOM/source.csv", include_stock=False)[0]
        self.assertEqual(row["footprint"], "0805")
        self.assertEqual(row["comment"], self.item["model"])

    def test_different_saved_parameters_are_not_silently_grouped(self):
        self.select(refs="R85")
        self.select({**self.item, "param_detail": self.item["param_detail"] + "; rating: special"}, refs="R86")
        rows = self.app.rows("R85,R86", "BOM/source.csv", include_stock=False)
        self.assertEqual(len(rows), 2)

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
const ctx = {
  candidateTargetRow: {dataset:{ref:'R85',refs:'R85,R86'}},
  stockCode: row=>row.stock_c_code,
  currentCandidateItems: new Map([['C4283613',item]]),
  normalizeCCode: code=>code, rowAnchor: tr=>({ref:tr.dataset.ref}),
  fetch: async (url,options)=>{calls.push([url,JSON.parse(options.body)]);return {ok:true,json:async()=>({ok:true})}},
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
  assert.equal(ctx.schematicDescription({querySelector:()=>({dataset:{stockParamText:ctx.stockParamText(stock)}})}),'line one\nline two');
  await ctx.chooseCandidate('C4283613');
  assert.equal(calls[0][0],'/api/select_candidate');
  assert.deepEqual(calls[0][1].candidate,item);
  assert.equal(calls[0][1].refs,'R85,R86');
  assert.equal(calls[1],'close');
  assert.equal(calls[2][1].ref,'R85');
  calls.length=0;
  ctx.fetch=async()=>({ok:false,json:async()=>({ok:false,error:'cannot save'})});
  await assert.rejects(ctx.chooseCandidate('C4283613'),/cannot save/);
  assert.equal(calls.length,0);
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
            self.assertEqual(result["fields"]["value"], "50kΩ")
        finally:
            server.shutdown()
            worker.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
