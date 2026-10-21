import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from part_selection_helper import PartInfo, search_keyword
from part_selection_server import render_index


ORDINARY_CASES = [
    ("R1", "resistor", "100k,1%", "DSMU:0603", "100k 0603"),
    ("R2", "RES", "4k7 ±1%", "Resistor_SMD:R_0603_1608Metric", "4k7 0603"),
    ("C1", "Capacitor", "100nF,10%", "Capacitor_SMD:C_0402_1005Metric", "100nF 0402"),
    ("C2", "电容", "4.7uF", "DSMU:0805", "4.7uF 0805"),
    ("R3", "", "0", "0603", "0 0603"),
    ("C3", "capacitor", "100nF 50V X7R", "DSMU:0603", "100nF 0603"),
    ("R5", "resistor", "100k 0.1% 25ppm/℃", "DSMU:0603", "100k 0603"),
    ("R6", "电阻", "100k 25ppm/℃ 75V", "DSMU:0603", "100k 0603"),
    ("C4", "", "100 nF 50V X7R", "DSMU:0603", "100 nF 0603"),
    ("C5", "Chip Capacitor - Surface Mount", "4.7 µF 25V X5R", "DSMU:0603", "4.7 µF 0603"),
    ("R7", "resistor", "10 kΩ 0.1% 25ppm/℃", "DSMU:0603", "10 kΩ 0603"),
    ("C6", "capacitor", "10pF 25V C0G", "Capacitor_SMD:C_01005_0402Metric", "10pF 01005"),
    ("R8", "resistor", "1k 1%", "Resistor_SMD:R_01005_0402Metric", "1k 01005"),
    ("R9", "resistor", "100 1/10W 1%", "DSMU:0603", "100 0603"),
    ("L1", "inductor", "4.7uH, Rated Current: 800mA, Saturation Current: 1A", "DSMU:0805", "4.7uH 0805"),
    ("L2", "电感", "10 uH, Rated Current: 2A, Saturation Current: not specified", "DSMU:0603", "10 uH 0603"),
]


class CandidateSearchTest(unittest.TestCase):
    def test_ordinary_passive_search_uses_value_and_package(self):
        for ref, comment, value, footprint, expected in ORDINARY_CASES:
            with self.subTest(ref=ref):
                part = PartInfo(ref=ref, comment=comment, value=value, footprint=footprint)
                before = vars(part).copy()
                self.assertEqual(search_keyword(part), expected)
                self.assertEqual(vars(part), before)

    def test_explicit_passive_model_search_keeps_previous_terms(self):
        self.assertEqual(search_keyword(PartInfo(ref="R1", comment="RC0603FR-07100KL",
                                                value="100k,1%", footprint="DSMU:0603")),
                         "RC0603FR-07100KL 100k,1% DSMU:0603")
        self.assertEqual(search_keyword(PartInfo(ref="R1", comment="RC0603FR-07100KL",
                                                value="100k 0.1% 25ppm/℃", footprint="DSMU:0603")),
                         "RC0603FR-07100KL 100k 0.1% 25ppm/℃ DSMU:0603")

    @unittest.skipUnless(shutil.which("node"), "Node.js required for candidate search checks")
    def test_candidate_keyword_simplifies_ordinary_passives(self):
        page = render_index(SimpleNamespace(project_root=ROOT, config={}))
        names = ["uniqueSearchTerms", "passiveSearchType", "isExplicitPassiveComment",
                 "candidateKeywordFromFields"]
        functions = []
        for name in names:
            match = re.search(r"function " + name + r"\([^)]*\) \{.*?\n\}", page, re.S)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        script = "\n".join(functions) + r'''
const assert = require('node:assert/strict');
for (const [fields, expected] of [
 [{refs:'R4',comment:'RC0603FR-07100KL',value:'100k,1%',footprint:'DSMU:0603'}, 'RC0603FR-07100KL 100k,1% DSMU:0603'],
 [{refs:'R4',comment:'RC0603FR-07100KL',value:'100k 0.1% 25ppm/℃',footprint:'DSMU:0603'}, 'RC0603FR-07100KL 100k 0.1% 25ppm/℃ DSMU:0603'],
 [{refs:'U1',comment:'AD4630-24BBCZ',value:'ADC',footprint:'BGA-64'}, 'AD4630-24BBCZ'],
 [{refs:'U2',comment:'C4283613',value:'ADC',footprint:'BGA-64'}, 'C4283613'],
 [{refs:'L1',comment:'inductor',value:'10uH',footprint:'0805'}, '10uH 0805'],
 [{refs:'FB1',comment:'inductor',value:'600ohm@100MHz',footprint:'0603'}, 'inductor 600ohm@100MHz 0603'],
 [{refs:'L1',comment:'XAL4020-102MEC',value:'1uH, Rated Current: 9.6A',footprint:'DSMU:SMD-2C'}, 'XAL4020-102MEC 1uH, Rated Current: 9.6A DSMU:SMD-2C']
]) assert.equal(candidateKeywordFromFields(fields), expected);
'''
        cases = [[{"refs": ref, "comment": comment, "value": value, "footprint": footprint}, expected]
                 for ref, comment, value, footprint, expected in ORDINARY_CASES]
        script += "\nconst ordinaryCases = " + json.dumps(cases) + r''';
for (const [fields, expected] of ordinaryCases) {
 const before = JSON.stringify(fields);
 assert.equal(candidateKeywordFromFields(fields), expected, fields.refs);
 assert.equal(JSON.stringify(fields), before, 'Search must not mutate stored fields');
}
'''
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                                text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
