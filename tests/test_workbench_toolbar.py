import re
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from part_selection_server import render_index


class ToolbarMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.groups = {}
        self.group = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and "toolbar-section" in attrs.get("class", ""):
            self.group = attrs.get("aria-label")
            self.groups[self.group] = []
        if attrs.get("id"):
            self.ids.append(attrs["id"])
            if self.group:
                self.groups[self.group].append(attrs["id"])

    def handle_endtag(self, tag):
        if tag == "section":
            self.group = None


class WorkbenchToolbarTest(unittest.TestCase):
    def setUp(self):
        self.html = render_index(SimpleNamespace(project_root=ROOT, config={}))

    def test_operations_are_grouped_with_unique_existing_command_ids(self):
        markup = ToolbarMarkup(self.html)
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        expected = {
            "原理图数据": ["bomSelect", "refreshSchematicRows"],
            "库存查询": ["queryMissingStock", "stockFreshness", "pauseStockQuery", "queryAllStock", "importLocalInventory", "localInventorySetCount"],
            "同步设计": ["designSyncScope", "designSyncMenu", "syncSelectedSchematic", "syncAllSchematic", "updateSelectedPcb"],
            "表格导出": ["exportPumpMenu", "pumpExportMode", "exportPumpTable", "exportChanges"],
            "列表视图": ["toggleColumns", "columnPanel", "autoSizeColumns", "columnList", "defaultViewMenu", "saveDefaultView", "loadDefaultView"],
        }
        self.assertEqual(markup.groups, expected)

    def test_toolbar_has_local_icons_text_labels_and_accessible_tooltips(self):
        for command in ("refreshSchematicRows", "queryMissingStock", "queryAllStock",
                        "designSyncMenu", "exportPumpMenu",
                        "exportChanges", "toggleColumns", "defaultViewMenu"):
            button = re.search(r'<button\b[^>]*id="' + command + r'"[^>]*>.*?</button>', self.html, re.S)
            self.assertIsNotNone(button, command)
            self.assertIn('data-tooltip=', button.group())
            self.assertIn('aria-label=', button.group())
            self.assertIn('<use href="#toolbar-icon-', button.group())
            self.assertIn('data-button-label', button.group())
        self.assertIn('role="tooltip"', self.html)
        self.assertNotIn('unpkg.com', self.html)
        self.assertIn('id="toolbar-icon-package-search"', self.html)

    def test_status_and_progress_live_outside_the_toolbar(self):
        status = self.html[self.html.index('<footer class="workbench-statusbar"'):self.html.index('</footer>')]
        for id_ in ("state", "stockProgress", "syncProgress", "workbenchSummary"):
            self.assertIn(f'id="{id_}"', status)
        self.assertIn('class="workbench-chrome"', self.html)

    @unittest.skipUnless(shutil.which("node"), "Node is required for full page script validation")
    def test_entire_rendered_page_script_is_valid_javascript(self):
        scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', self.html, re.S)
        self.assertTrue(scripts)
        result = subprocess.run([shutil.which("node"), "--check"],
                                input="\n".join(scripts), capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_toolbar_dropdowns_stack_above_filter_controls(self):
        toolbar = re.search(r'\.toolbar \{([^}]+)\}', self.html).group(1)
        filters = re.search(r'\.filter-bar \{([^}]+)\}', self.html).group(1)
        self.assertIn('position: relative', toolbar)
        toolbar_z = int(re.search(r'z-index: (\d+)', toolbar).group(1))
        filter_z = int(re.search(r'z-index: (\d+)', filters).group(1))
        self.assertGreater(toolbar_z, filter_z)

    @unittest.skipUnless(shutil.which("node"), "Node is required for UI behavior checks")
    def test_dynamic_labels_keep_icons_and_summary_counts_all_rows(self):
        functions = re.search(r'function setToolbarLabel\(.*?(?=function organizeToolbar\()', self.html, re.S)
        self.assertIsNotNone(functions)
        script = r'''
const assert = require('node:assert/strict');
FUNCTIONS
const label = {textContent: ''};
const button = {querySelector(){return label;}, textContent:'ICON', dataset:{}};
setToolbarLabel(button,'查询中');
assert.equal(label.textContent,'查询中');
assert.equal(button.textContent,'ICON');
const rows = [
 {dataset:{refs:'C1,C2',syncNeeded:'1',reviewStatus:'Pending'}},
 {dataset:{refs:'R1',syncNeeded:'0',reviewStatus:'Confirmed'}},
 {dataset:{refs:'U1',missingRef:'1',reviewStatus:'Rejected'}},
 {dataset:{refs:'U2',syncError:'1',reviewStatus:'Pending'}}
];
assert.deepEqual(workbenchCounts(rows),{groups:4,refs:5,needsSync:3,unconfirmed:3});
assert.deepEqual(workbenchCounts([]),{groups:0,refs:0,needsSync:0,unconfirmed:0});
'''.replace('FUNCTIONS', functions.group())
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_plugin_package_includes_icon_assets_and_license(self):
        from kicad_plugin.package_plugin import package_files
        files = package_files()
        for name in ("web/toolbar-icons.svg", "web/LUCIDE-LICENSE", "part_selection_footprints.py",
                     "web/footprint-compare.js", "web/footprint-compare.css", "part_selection_inventory.py",
                     "web/local-inventory.js", "web/local-inventory.css"):
            self.assertIn("plugins/app/" + name, files.keys())


if __name__ == "__main__":
    unittest.main()
