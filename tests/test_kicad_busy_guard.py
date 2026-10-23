import os
from pathlib import Path
import shutil
import subprocess
from _scratch import temporary_project
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'kicad_schematic_search.ps1'
POWERSHELL = shutil.which('powershell')


@unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'Windows PowerShell required')
class KiCadBusyGuardTests(unittest.TestCase):
    def run_functions(self, body):
        setup = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TEST_SEARCH_SCRIPT, [ref]$tokens, [ref]$errors)
$names = @('Assert-KiCadWindowResponsive', 'Invoke-KiCadProjectProbe', 'Get-HostedProjectPaths', 'Get-CachedKiCadWindow', 'Get-KiCadProjectManagerWindow')
foreach ($node in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    if ($node.Name -in $names) { Invoke-Expression $node.Extent.Text }
}
function Write-Log { param($Message) }
'''
        result = subprocess.run(
            [POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', setup + body],
            env={**os.environ, 'TEST_SEARCH_SCRIPT': str(SCRIPT)},
            # Cold PowerShell/Add-Type startup can be slow on hosted runners;
            # the actual 250 ms/3000 ms worker bounds are asserted below.
            capture_output=True, text=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_wm_null_probe_is_bounded_and_does_not_send_commands(self):
        self.assertEqual(self.run_functions(r'''
Add-Type @'
using System;
public class NativeMethods {
public static IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out UIntPtr result) {
 result = UIntPtr.Zero;
 if (msg != 0 || timeout > 1000 || flags != 0x22) throw new Exception("unsafe probe");
 return IntPtr.Zero;
}
}
'@
function Ensure-NativeMethods {}
try { Assert-KiCadWindowResponsive -Target ([pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]123}) -Phase ownership; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'KICAD_BUSY') { throw }; 'protected' }
'''), 'protected')

    def test_read_only_uia_worker_is_bounded_and_success_preserves_unicode(self):
        self.assertEqual(self.run_functions(r'''
$result = Invoke-KiCadProjectProbe -ProbeScript '"E:\\工程\\test.kicad_pro" | ConvertTo-Json -Compress' -TimeoutMs 2500
if ($result -ne 'E:\\工程\\test.kicad_pro') { throw "wrong path: $result" }
$timer = [Diagnostics.Stopwatch]::StartNew()
try { Invoke-KiCadProjectProbe -ProbeScript 'Start-Sleep -Seconds 30' -TimeoutMs 250; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'KICAD_BUSY') { throw } }
if ($timer.ElapsedMilliseconds -gt 3000) { throw 'unbounded worker' }
'protected'
'''), 'protected')

    def test_ownership_timeout_is_not_swallowed_by_manager(self):
        self.assertEqual(self.run_functions(r'''
# The manager must propagate an ownership timeout, never launch another instance.
function Get-KiCadTopLevelWindows { [pscustomobject]@{Id=1; ProcessName='kicad'; MainWindowHandle=[IntPtr]123; MainWindowTitle='test - KiCad 10.0'} }
function Get-HostedProjectStatus { throw 'KICAD_BUSY ownership' }
try { Get-KiCadProjectManagerWindow; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'KICAD_BUSY') { throw }; 'protected' }
'''), 'protected')

    def test_ownership_timeout_is_not_swallowed_by_cache(self):
        with temporary_project() as directory:
            cache = Path(directory) / 'cache.json'
            cache.write_text('{"pid":1,"hwnd":123}', encoding='ascii')
            self.assertEqual(self.run_functions(r'''
$WindowCachePath = 'CACHE_PATH'
function Get-KiCadTopLevelWindows { [pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]123; MainWindowTitle='schematic'} }
function Test-SchematicEditorWindowName { return $true }
function Test-KiCadProcessBelongsToProject { throw 'KICAD_BUSY ownership' }
try { Get-CachedKiCadWindow; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'KICAD_BUSY') { throw }; 'protected' }
'''.replace('CACHE_PATH', str(cache))), 'protected')

    def test_busy_manager_prevents_unbounded_uia_read(self):
        self.assertEqual(self.run_functions(r'''
function Get-KiCadTopLevelWindows { [pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]123; MainWindowTitle='test - KiCad 10.0'} }
function Assert-KiCadWindowResponsive { throw 'KICAD_BUSY ownership' }
function Invoke-KiCadProjectProbe { throw 'must not read UIA' }
try { Get-HostedProjectPaths -ProcessId 1; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'KICAD_BUSY') { throw }; 'protected' }
'''), 'protected')
