import os
from pathlib import Path
import shutil
import subprocess
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/kicad_schematic_search.ps1"
POWERSHELL = shutil.which("powershell")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class KiCadManagerOpenTests(unittest.TestCase):
    def run_functions(self, body):
        setup = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TEST_SEARCH_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Get-KiCadProjectManagerWindow','Ensure-KiCadProjectOpen','Find-KiCadManagerSchematicCommand','Open-SchematicPage','Test-KiCadProcessBelongsToProject')) {
    $node = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    if (-not $node) { throw "Missing function: $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
function Write-Log { param($Message) }
function Assert-KiCadWindowResponsive { param($Target, $Phase) }
function Start-Process { throw 'Must not launch a standalone editor' }
function Get-HostedProjectStatus { return 'match' }
$script:manager = [pscustomobject]@{Id=123; MainWindowHandle=[IntPtr]55; MainWindowTitle='board - KiCad 10.0'; ProcessName='kicad'}
'''
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", setup + body],
            env={**os.environ, "TEST_SEARCH_SCRIPT": str(SCRIPT)},
            capture_output=True, text=True, errors="replace", timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_existing_schematic_is_reused_without_touching_manager(self):
        self.assertEqual(self.run_functions(r'''
function Get-KiCadCandidateWindows { return [pscustomobject]@{Id=123} }
function Ensure-KiCadProjectOpen { throw 'Must reuse the existing editor' }
Open-SchematicPage -Page Power
Write-Output 'reused'
'''), "reused")

    def test_manager_selection_excludes_editors_and_other_projects(self):
        self.assertEqual(self.run_functions(r'''
function Get-KiCadTopLevelWindows {
    [pscustomobject]@{Id=1; MainWindowHandle=[IntPtr]11; MainWindowTitle='board - Schematic Editor'; ProcessName='kicad'}
    [pscustomobject]@{Id=2; MainWindowHandle=[IntPtr]22; MainWindowTitle='other - KiCad 10.0'; ProcessName='kicad'}
    [pscustomobject]@{Id=3; MainWindowHandle=[IntPtr]33; MainWindowTitle='unknown - KiCad 10.0'; ProcessName='kicad'}
    $script:manager
}
function Get-HostedProjectStatus {
    param($Window)
    if ($Window.Id -eq 1) { throw 'Editor must not be considered a manager' }
    if ($Window.Id -eq 3) { throw 'Cannot verify project' }
    if ($Window.Id -eq 123) { return 'match' }
    return 'other'
}
$found = Get-KiCadProjectManagerWindow
if ($found.MainWindowHandle -ne 55) { throw 'Wrong manager selected' }
Write-Output 'matched'
'''), "matched")

    def test_existing_manager_is_returned_without_relaunch(self):
        self.assertEqual(self.run_functions(r'''
function Get-KiCadProjectFile { return 'C:\project\board.kicad_pro' }
function Get-KiCadProjectManagerWindow { return $script:manager }
$found = Ensure-KiCadProjectOpen
if ($found.MainWindowHandle -ne 55) { throw 'Expected exact manager window' }
Write-Output 'existing'
'''), "existing")

    def test_pcb_manager_unknown_identity_never_starts_a_second_manager(self):
        self.assertEqual(self.run_functions(r'''
function Get-KiCadProjectFile { return 'C:\project\board.kicad_pro' }
function Get-KiCadTopLevelWindows { return $script:manager }
function Get-HostedProjectStatus { return 'unknown' }
try { Ensure-KiCadProjectOpen -RequireUnique; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'KICAD_MANAGER_UNVERIFIED') { throw }; 'blocked' }
'''), 'blocked')

    def test_pcb_multiple_matching_managers_are_not_chosen_by_order(self):
        self.assertEqual(self.run_functions(r'''
function Get-KiCadTopLevelWindows {
    $script:manager
    [pscustomobject]@{Id=456; MainWindowHandle=[IntPtr]66; MainWindowTitle='board - KiCad 10.0'; ProcessName='kicad'}
}
try { Get-KiCadProjectManagerWindow -RequireUnique; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'KICAD_MANAGER_AMBIGUOUS') { throw }; 'blocked' }
'''), 'blocked')

    def test_missing_project_fails_without_starting_standalone_editor(self):
        self.assertEqual(self.run_functions(r'''
function Get-KiCadProjectFile { return '' }
try { Ensure-KiCadProjectOpen; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'project file') { throw }; Write-Output 'blocked' }
'''), "blocked")

    def test_new_manager_waits_until_verified_and_launches_project_only(self):
        self.assertEqual(self.run_functions(r'''
$KicadExe = 'C:\KiCad\kicad.exe'
$script:poll = 0
function Get-KiCadProjectFile { return 'C:\project with spaces\board.kicad_pro' }
function Test-Path { return $true }
function Start-Sleep {}
function Start-Process {
    param($FilePath, $ArgumentList)
    if ($FilePath -ne $KicadExe -or $ArgumentList[0] -ne '"C:\project with spaces\board.kicad_pro"') { throw 'Wrong launch' }
}
function Get-KiCadProjectManagerWindow {
    $script:poll++
    if ($script:poll -ge 3) { return $script:manager }
}
$found = Ensure-KiCadProjectOpen
if ($found.MainWindowHandle -ne 55 -or $script:poll -lt 3) { throw 'Manager readiness not verified' }
Write-Output 'project launched'
'''), "project launched")

    def menu_stub(self, label="Schematic Editor", disabled=False, accepted=True):
        # A nonstandard ID makes a hard-coded KiCad menu ID fail this test.
        return r'''
Add-Type @'
using System; using System.Text;
public class NativeMethods {
public static uint Command = 0;
public static IntPtr GetMenu(IntPtr hwnd) { if (hwnd != (IntPtr)55) throw new Exception("Wrong manager"); return (IntPtr)1; }
public static int GetMenuItemCount(IntPtr menu) { return menu == (IntPtr)1 ? 1 : 2; }
public static IntPtr GetSubMenu(IntPtr menu, int pos) { return menu == (IntPtr)1 ? (IntPtr)2 : IntPtr.Zero; }
public static int GetMenuString(IntPtr menu, uint pos, StringBuilder text, int max, uint flags) { text.Append(menu == (IntPtr)1 ? "Tools" : (pos == 0 ? "PCB Editor" : "LABEL\tCtrl+E")); return text.Length; }
public static uint GetMenuItemID(IntPtr menu, int pos) { return pos == 1 ? 8765u : 9999u; }
public static uint GetMenuState(IntPtr menu, uint item, uint flags) { return DISABLED; }
public static bool PostMessage(IntPtr hwnd, uint msg, IntPtr command, IntPtr param) {
if (hwnd != (IntPtr)55 || msg != 0x111 || command != (IntPtr)8765) throw new Exception("Wrong command");
Command = (uint)command.ToInt64(); return ACCEPTED;
}
}
'@
function Get-KiCadCandidateWindows { return @() }
function Get-TopLevelSchematicFile { return 'C:\project\board.kicad_sch' }
function Test-Path { return $true }
function Ensure-KiCadProjectOpen { return $script:manager }
function Get-HostedProjectStatus { return 'match' }
'''.replace("LABEL", label).replace("DISABLED", "1u" if disabled else "0u").replace("ACCEPTED", "true" if accepted else "false")

    def test_open_dispatches_discovered_manager_menu_in_supported_languages(self):
        for label in ("Schematic &Editor", "原理图编辑器", "原理圖編輯器"):
            with self.subTest(label=label):
                self.assertEqual(self.run_functions(self.menu_stub(label) + r'''
Open-SchematicPage -Page Power
if ([NativeMethods]::Command -ne 8765) { throw 'Menu command was not invoked' }
Write-Output 'manager dispatched'
'''), "manager dispatched")

    def test_missing_or_disabled_command_never_falls_back_to_eeschema(self):
        for stub in (self.menu_stub("Other Editor"), self.menu_stub(disabled=True)):
            with self.subTest(stub=stub):
                self.assertEqual(self.run_functions(stub + r'''
try { Open-SchematicPage -Page Power; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'schematic.*command') { throw } }
if ([NativeMethods]::Command -ne 0) { throw 'Disabled or wrong command invoked' }
Write-Output 'blocked'
'''), "blocked")

    def test_failed_dispatch_is_reported_without_standalone_fallback(self):
        self.assertEqual(self.run_functions(self.menu_stub(accepted=False) + r'''
try { Open-SchematicPage -Page Power; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'dispatch') { throw }; Write-Output 'blocked' }
'''), "blocked")

    def test_project_changed_before_dispatch_is_rejected(self):
        self.assertEqual(self.run_functions(self.menu_stub() + r'''
function Get-HostedProjectStatus { return 'other' }
function Test-KiCadProcessBelongsToProject { return $true }
try { Open-SchematicPage -Page Power; throw 'unexpected success' }
catch { if ($_.Exception.Message -notmatch 'project') { throw } }
if ([NativeMethods]::Command -ne 0) { throw 'Wrong project received command' }
Write-Output 'protected'
'''), "protected")

    def test_window_level_project_identity_overrides_other_process_launch_argument(self):
        self.assertEqual(self.run_functions(r'''
function Get-CimInstance { return [pscustomobject]@{CommandLine='kicad.exe "C:\\other\\board.kicad_pro"'} }
function Get-CommandLineProjectStatus { return 'other' }
function Get-ProjectRoot { return 'C:\\project' }
function Get-HostedProjectStatus { param($Window) return 'match' }
if (-not (Test-KiCadProcessBelongsToProject -Window ([pscustomobject]@{Id=12}))) { throw 'Exact hosted window identity was ignored' }
function Get-HostedProjectStatus { param($Window) return 'other' }
if (Test-KiCadProcessBelongsToProject -Window ([pscustomobject]@{Id=12})) { throw 'Mismatched hosted window was accepted' }
Write-Output 'window identity checked'
'''), "window identity checked")


if __name__ == "__main__":
    unittest.main()
