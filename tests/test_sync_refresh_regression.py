"""Status refresh must not invent writes to absent per-reference target fields."""
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_design_source import app_for


@pytest.mark.skipif(not shutil.which('node'), reason='Node required')
def test_refresh_preserves_mixed_untouched_descriptions_and_real_code_edits():
    from part_selection_server import render_index
    page = render_index(SimpleNamespace(project_root=Path(__file__).resolve().parents[1], config={}))
    function = re.search(r'function updateSavedFieldDiffs\(.*?(?=function markRowNeedsSchematicSync)', page, re.S).group()
    script = r'''
const assert=require('node:assert/strict');
FUNCTION
const row={refs_list:['C1','C2'],description:'original A',value:'NC',
  per_ref_sync_fields:{C1:{value:'NC',lcsc_c_code:'NC'},C2:{value:'NC',lcsc_c_code:'NC'}},
  schematic_fields_by_ref:{C1:{value:'NC',lcsc_c_code:'NC',description:'original A'},
                          C2:{value:'NC',lcsc_c_code:'NC',description:'original B'}}};
const fields=['value','description','lcsc_c_code','lcsc_url','comment'];
updateSavedFieldDiffs(row,fields);
assert.equal(row.field_changed_refs,'','refresh invented an untouched description edit');
row.per_ref_sync_fields.C1.lcsc_c_code='C123';
updateSavedFieldDiffs(row,fields);
assert.equal(row.field_changed_refs,'C1');
assert.deepEqual(Object.keys(row.field_diffs_by_ref.C1),['lcsc_c_code']);
row.per_ref_sync_fields.C2.description='';
updateSavedFieldDiffs(row,fields);
assert.equal(row.field_diffs_by_ref.C2.description.new,'','explicit clearing must remain actionable');
delete row.per_ref_sync_fields.C2.description;
updateSavedFieldDiffs(row,fields);
assert.equal(row.field_diffs_by_ref.C2.description,undefined,'obsolete diffs must be cleared');
'''.replace('FUNCTION', function)
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_same_bytes_reuse_parse_but_changed_bytes_reparse_with_same_mtime(scratch_project):
    from part_selection_design_source import DesignSource
    from kicad_sexpr import parse_sexpr
    app, child = app_for(scratch_project)
    source = DesignSource(app)
    before = source.capture(['L9'])
    with patch('part_selection_design_source.parse_sexpr', wraps=parse_sexpr) as parse:
        assert source.capture(['L9'])['fields'] == before['fields']
        assert parse.call_count == 0, 'unchanged hierarchy was reparsed'
        import os
        stamp = child.stat()
        child.write_text(child.read_text().replace('10uH', '22uH'))
        os.utime(child, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert not source.validate(before)
        assert source.capture(['L9'])['fields']['L9']['value'] == '22uH'
        assert parse.call_count == 1, 'content change must reparse only the changed page'
