import os
from pathlib import Path
import shutil
import subprocess
from _scratch import temporary_project
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "kicad_schematic_search.ps1"
POWERSHELL = shutil.which("powershell")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class KiCadSaveFallbackTests(unittest.TestCase):
    def run_functions(self, body, extra_env=None):
        # Load just the functions, not the script's editor-control entry point.
        setup = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TEST_SEARCH_SCRIPT, [ref]$tokens, [ref]$errors)
$names = @('Invoke-KiCadApi', 'Wait-KiCadEditorSaved', 'Save-KiCadActiveFrame', 'Find-KiCadMenuCommand', 'Find-KiCadCloseCommand', 'Save-AndCloseKiCadEditorFrame', 'Get-KiCadTopLevelWindows')
foreach ($node in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    if ($node.Name -in $names) { Invoke-Expression $node.Extent.Text.Replace('$PSScriptRoot', '$env:TEST_IPC_ROOT') }
}
function Write-Log { param($Message) }
function Assert-KiCadWindowResponsive { param($Target, $Phase) }
'''
        env = {**os.environ, "TEST_SEARCH_SCRIPT": str(SCRIPT), **(extra_env or {})}
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", setup + body],
                                capture_output=True, text=True, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_ipc_native_stderr_returns_false_instead_of_terminating(self):
        with temporary_project() as directory:
            helper = Path(directory) / "kicad_ipc_action.py"
            helper.write_text("import sys\nsys.stderr.write('IPC unavailable\\n')\nsys.exit(1)\n", encoding="utf-8")
            import sys
            body = ("$KicadMcpPython = $env:TEST_PYTHON\n"
                    "Invoke-KiCadApi -ProcessId 1 -ApiName saveFrame | ConvertTo-Json")
            self.assertEqual(self.run_functions(body, {"TEST_IPC_ROOT": directory, "TEST_PYTHON": sys.executable}), "false")

    def test_ipc_save_success_does_not_send_keyboard_input(self):
        output = self.run_functions('''
function Invoke-KiCadApi { return $true }
function Wait-KiCadEditorSaved { return $true }
function Set-KiCadForeground { throw 'must not activate' }
$target = [pscustomobject]@{Id=1; MainWindowTitle='*test'}
$target | Add-Member ScriptMethod Refresh {}
Save-KiCadActiveFrame -Target $target
Write-Output 'saved'
''')
        self.assertEqual(output, "saved")

    def test_clean_editor_does_not_trigger_save_or_project_backup(self):
        output = self.run_functions('''
function Invoke-KiCadApi { throw 'must not save an unchanged project' }
$target = [pscustomobject]@{Id=1; MainWindowTitle='test'}
$target | Add-Member ScriptMethod Refresh {}
Save-KiCadActiveFrame -Target $target
Write-Output 'unchanged'
''')
        self.assertEqual(output, "unchanged")

    def test_unavailable_ipc_never_dispatches_hanging_native_save(self):
        output = self.run_functions('''
Add-Type 'public class NativeMethods { public static System.IntPtr GetMenu(System.IntPtr hwnd) { throw new System.Exception("unsafe menu save attempted"); } }'
function Invoke-KiCadApi { return $false }
$target = [pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]123; MainWindowTitle='*test'}
$target | Add-Member ScriptMethod Refresh {}
try { Save-KiCadActiveFrame -Target $target; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'manual save required') { throw }; Write-Output 'protected' }
''')
        self.assertEqual(output, "protected")

    def test_ipc_save_success_still_requires_clean_editor(self):
        output = self.run_functions('''
function Invoke-KiCadApi { return $true }
function Wait-KiCadEditorSaved { return $false }
$target = [pscustomobject]@{Id=1; MainWindowTitle='*test'}
$target | Add-Member ScriptMethod Refresh {}
try { Save-KiCadActiveFrame -Target $target; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'still has unsaved changes') { throw }; Write-Output 'protected' }
''')
        self.assertEqual(output, "protected")

    def test_busy_editor_remains_a_candidate_not_treated_as_closed(self):
        output = self.run_functions('''
Add-Type @'
using System;
public class NativeMethods {
public static IntPtr[] TopLevelWindows() { return new[] {(IntPtr)123}; }
public static uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid) { pid = 1; return 1; }
}
public class KiCadEditorWindow { public int Id = 1; public KiCadEditorWindow(IntPtr hwnd) {} }
'@
function Ensure-NativeMethods {}
function Get-Process {
    $p = [pscustomobject]@{Id=1; ProcessName='kicad'; HasExited=$false; Responding=$false}
    $p | Add-Member ScriptMethod Refresh {}
    return $p
}
@(Get-KiCadTopLevelWindows).Count
''')
        self.assertEqual(output, "1")

    def test_accepted_close_is_sent_once_and_waited_without_fallback(self):
        output = self.run_functions('''
Add-Type @'
using System;
public class NativeMethods {
public static int Posts = 0;
public static bool PostMessage(IntPtr hwnd, uint msg, IntPtr command, IntPtr param) { Posts++; return true; }
}
'@
function Find-KiCadCloseCommand { return 5001 }
function Save-KiCadActiveFrame {}
function Invoke-KiCadApi { throw 'must not queue another close' }
function Wait-KiCadEditorFrameClosed { param($Target, $TimeoutMs) if ($TimeoutMs -lt 60000) { throw 'wait too short' }; return $false }
$target = [pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]123}
$target | Add-Member ScriptMethod Refresh {}
$target | Add-Member ScriptMethod CloseMainWindow { throw 'must not queue WM_CLOSE' }
try { Save-AndCloseKiCadEditorFrame -Target $target; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'did not close') { throw } }
[NativeMethods]::Posts
''')
        self.assertEqual(output, "1")

    def test_dirty_editor_is_not_considered_saved(self):
        output = self.run_functions('''
$target = [pscustomobject]@{Id=1; MainWindowTitle='*test'}
$target | Add-Member ScriptMethod Refresh {}
Wait-KiCadEditorSaved -Target $target -TimeoutMs 0 | ConvertTo-Json
''')
        self.assertEqual(output, "false")

    def test_busy_editor_does_not_receive_save_or_close(self):
        output = self.run_functions('''
function Assert-KiCadWindowResponsive { throw 'KICAD_BUSY test' }
function Save-KiCadActiveFrame { throw 'must not save' }
function Find-KiCadCloseCommand { throw 'must not inspect menu' }
function Invoke-KiCadApi { throw 'must not close' }
try { Save-AndCloseKiCadEditorFrame -Target ([pscustomobject]@{Id=1}); throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'KICAD_BUSY') { throw }; 'protected' }
''')
        self.assertEqual(output, "protected")

    def test_editor_becoming_busy_after_save_does_not_receive_close(self):
        output = self.run_functions('''
$script:checks = 0
function Assert-KiCadWindowResponsive { $script:checks++; if ($script:checks -gt 1) { throw 'KICAD_BUSY after save' } }
function Find-KiCadCloseCommand { return 5001 }
function Save-KiCadActiveFrame {}
function Invoke-KiCadApi { throw 'must not close' }
$target = [pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]123}
$target | Add-Member ScriptMethod Refresh {}
$target | Add-Member ScriptMethod CloseMainWindow { throw 'must not close' }
try { Save-AndCloseKiCadEditorFrame -Target $target; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'KICAD_BUSY') { throw }; 'protected' }
''')
        self.assertEqual(output, "protected")

    def test_save_close_order_and_exact_window_fallback(self):
        output = self.run_functions('''
$script:events = @()
function Save-KiCadActiveFrame { param($Target) $script:events += "save:$($Target.MainWindowHandle)" }
function Invoke-KiCadApi { param($ProcessId, $ApiName) $script:events += $ApiName; return $false }
function Wait-KiCadEditorFrameClosed { param($Target, $TimeoutMs) return $script:events -contains 'close:123' }
$target = [pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]123}
$target | Add-Member ScriptMethod Refresh {}
$target | Add-Member ScriptMethod CloseMainWindow { $script:events += "close:$($this.MainWindowHandle)"; return $true }
Save-AndCloseKiCadEditorFrame -Target $target
Write-Output ($script:events -join ',')
''')
        self.assertEqual(output, "save:123,closeCurrentFrame,close:123")

    def test_menu_close_command_is_used_before_window_close_fallback(self):
        output = self.run_functions(r'''
Add-Type @'
using System;
using System.Text;
public class NativeMethods {
public static IntPtr GetMenu(IntPtr hwnd) { return (IntPtr)1; }
public static int GetMenuItemCount(IntPtr menu) { return menu == (IntPtr)1 ? 1 : 1; }
public static IntPtr GetSubMenu(IntPtr menu, int pos) { return menu == (IntPtr)1 ? (IntPtr)2 : IntPtr.Zero; }
public static int GetMenuString(IntPtr menu, uint pos, StringBuilder text, int max, uint flags) { text.Append(menu == (IntPtr)2 ? "\u5173\u95ed" : "File"); return text.Length; }
public static uint GetMenuItemID(IntPtr menu, int pos) { return menu == (IntPtr)2 ? 5001u : 4294967295u; }
public static bool PostMessage(IntPtr hwnd, uint msg, IntPtr command, IntPtr param) { if (msg != 0x111 || command != (IntPtr)5001) { throw new Exception("wrong close command"); } return true; }
}
'@
$script:events = @()
function Save-KiCadActiveFrame { param($Target) $script:events += "save" }
function Invoke-KiCadApi { throw 'ipc should not be called' }
function Wait-KiCadEditorFrameClosed { param($Target, $TimeoutMs) return $true }
$target = [pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]123}
$target | Add-Member ScriptMethod Refresh {}
$target | Add-Member ScriptMethod CloseMainWindow { $script:events += "window-close"; return $true }
function Write-Log { param($Message) if($Message -match 'requested_cached|requested_retry'){ $script:events += 'menu-close' } }
Save-AndCloseKiCadEditorFrame -Target $target
Write-Output ($script:events -join ',')
''')
        self.assertEqual(output, "save,menu-close")

    def test_cached_menu_close_survives_menu_disappearing_after_save(self):
        output = self.run_functions(r'''
Add-Type @'
using System;
using System.Text;
public class NativeMethods {
public static bool AfterSave = false;
public static IntPtr GetMenu(IntPtr hwnd) { return AfterSave ? IntPtr.Zero : (IntPtr)1; }
public static int GetMenuItemCount(IntPtr menu) { return (menu == (IntPtr)1 || menu == (IntPtr)2) ? 1 : 0; }
public static IntPtr GetSubMenu(IntPtr menu, int pos) { return menu == (IntPtr)1 ? (IntPtr)2 : IntPtr.Zero; }
public static int GetMenuString(IntPtr menu, uint pos, StringBuilder text, int max, uint flags) { text.Append("Close"); return text.Length; }
public static uint GetMenuItemID(IntPtr menu, int pos) { return 5001; }
public static bool PostMessage(IntPtr hwnd, uint msg, IntPtr command, IntPtr param) { if (msg != 0x111 || command != (IntPtr)5001) { throw new Exception("wrong close command"); } return true; }
}
'@
$script:events = @()
function Save-KiCadActiveFrame { [NativeMethods]::AfterSave = $true; $script:events += "save" }
function Invoke-KiCadApi { throw 'ipc should not be called' }
function Wait-KiCadEditorFrameClosed { param($Target, $TimeoutMs) return $script:events -contains 'menu-close' }
$target = [pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]123}
$target | Add-Member ScriptMethod Refresh {}
$target | Add-Member ScriptMethod CloseMainWindow { $script:events += "window-close"; return $true }
function Write-Log { param($Message) if($Message -match 'requested_cached'){ $script:events += 'menu-close' } }
Save-AndCloseKiCadEditorFrame -Target $target
Write-Output ($script:events -join ',')
''')
        self.assertEqual(output, "save,menu-close")

    def test_save_failure_never_closes_editor(self):
        output = self.run_functions('''
function Save-KiCadActiveFrame { throw 'save failed' }
function Invoke-KiCadApi { throw 'must not close' }
try { Save-AndCloseKiCadEditorFrame -Target ([pscustomobject]@{Id=1}); throw 'unexpected success' }
catch { if ($_.Exception.Message -ne 'save failed') { throw }; Write-Output 'protected' }
''')
        self.assertEqual(output, "protected")

    def test_close_failure_blocks_sync_without_force_killing(self):
        output = self.run_functions('''
function Save-KiCadActiveFrame {}
function Invoke-KiCadApi { return $false }
function Wait-KiCadEditorFrameClosed { return $false }
function Stop-Process { throw 'must not kill' }
$target = [pscustomobject]@{Id=1}
$target | Add-Member ScriptMethod Refresh {}
$target | Add-Member ScriptMethod CloseMainWindow { return $true }
try { Save-AndCloseKiCadEditorFrame -Target $target; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'did not close') { throw }; Write-Output 'protected' }
''')
        self.assertEqual(output, "protected")

    def test_save_only_entry_does_not_need_foreground_or_page_switch(self):
        script = SCRIPT.read_text(encoding="utf-8-sig")
        entry = script[script.index('    Ensure-NativeMethods\n    $target = $windows[0]'):]
        self.assertLess(entry.index('Save-AndCloseKiCadEditorFrame'), entry.index('Set-KiCadForeground'))

if __name__ == "__main__":
    unittest.main()
