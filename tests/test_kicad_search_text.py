import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/kicad_schematic_search.ps1"
POWERSHELL = shutil.which("powershell")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class KiCadSearchTextTests(unittest.TestCase):
    def run_functions(self, body):
        # Load functions only: never run the editor/save/close entry point.
        setup = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TEST_SEARCH_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$names = @('Set-SearchText', 'Set-EditControlText', 'Set-EditWindowText', 'Get-EditWindowText', 'Invoke-SearchPanelFirstResult', 'Open-KiCadSearchPanel', 'Get-SearchPanelElementsFast', 'Test-KiCadWindowMatchesPage', 'Get-SchematicPageAliases')
foreach ($node in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    if ($node.Name -in $names) { . ([scriptblock]::Create($node.Extent.Text)) }
}
Add-Type @'
using System;
using System.Text;
namespace System.Windows.Forms {
    public class SendKeys { public static void SendWait(string text) { throw new Exception("Global keyboard input is forbidden"); } }
}
namespace System.Windows.Automation {
    public class ValuePattern {
        public static object Pattern = new object();
        public ValuePattern Current { get { return this; } }
        public string Value = "old";
        public bool IsReadOnly = false;
        public bool Corrupt = false;
        public void SetValue(string text) { Value = Corrupt ? "C03" : text; }
    }
    public class AutomationElement {
        public static AutomationElement FocusedElement = new AutomationElement();
        public AutomationElement Current { get { return this; } }
        public int ProcessId = 42;
        public string ClassName = "Edit";
        public string Name = "C103"; // A label is not the editable value.
        public int NativeWindowHandle = 123;
        public AutomationElement ControlType { get { return this; } }
        public string ProgrammaticName = "ControlType.Edit";
        public ValuePattern PatternValue = new ValuePattern();
        public bool Unsupported = false;
        public object GetCurrentPattern(object pattern) {
            if (Unsupported) throw new Exception("Unsupported pattern");
            return PatternValue;
        }
        public static AutomationElement FromHandle(IntPtr hwnd) { return FocusedElement; }
        public void SetFocus() { FocusedElement = this; }
    }
}
public class NativeMethods {
    public static string Text = "old";
    public static string ClassName = "Edit";
    public static bool CorruptSet = false, CorruptChars = false, Timeout = false, ReadTimeout = false, IgnoreClear = false;
    public static string ReplacedText = "C03";
    public static int Writes = 0;
    public static uint MenuState = 0;
    public static int Commands = 0;
    public static bool Enabled = true, PanelPresent = false;
    public static bool IsWindowEnabled(IntPtr hwnd) { return Enabled; }
    public static IntPtr GetMenu(IntPtr hwnd) { return (IntPtr)456; }
    public static uint GetMenuState(IntPtr menu, uint command, uint flags) {
        if (menu != (IntPtr)456 || command != 20312 || flags != 0) throw new Exception("Wrong menu target");
        return MenuState;
    }
    public static bool PostMessage(IntPtr hwnd, uint msg, IntPtr w, IntPtr l) {
        if (hwnd != (IntPtr)123 || msg != 0x111 || w != (IntPtr)20312 || l != IntPtr.Zero) throw new Exception("Wrong command target");
        Commands++; return true;
    }
    public static bool IsWindow(IntPtr hwnd) { return hwnd == (IntPtr)123; }
    public static int GetClassName(IntPtr hwnd, StringBuilder buffer, int count) { buffer.Append(ClassName); return buffer.Length; }
    public static IntPtr SetFocus(IntPtr hwnd) { return IntPtr.Zero; }
    static void Check(IntPtr hwnd, uint flags, uint timeout) {
        if (hwnd != (IntPtr)123 || flags != 0x22 || timeout > 1000) throw new Exception("Unsafe/unbounded edit message");
    }
    public static IntPtr SendMessageTimeoutText(IntPtr hwnd, uint msg, IntPtr w, string text, uint flags, uint timeout, out UIntPtr result) {
        Check(hwnd, flags, timeout); result = (UIntPtr)1;
        if (msg != 0xC) throw new Exception("Unexpected write message");
        Writes++;
        if (Timeout) return IntPtr.Zero;
        if (!(IgnoreClear && text == "")) Text = CorruptSet && text == "C103" ? ReplacedText : text;
        return (IntPtr)1;
    }
    public static IntPtr SendMessageTimeoutRead(IntPtr hwnd, uint msg, IntPtr w, StringBuilder buffer, uint flags, uint timeout, out UIntPtr result) {
        Check(hwnd, flags, timeout); result = UIntPtr.Zero;
        if (msg != 0xD || w.ToInt32() != buffer.Capacity) throw new Exception("Unexpected read message");
        if (Timeout || ReadTimeout) return IntPtr.Zero;
        buffer.Append(Text); result = (UIntPtr)Text.Length; return (IntPtr)1;
    }
    public static IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out UIntPtr result) {
        Check(hwnd, flags, timeout); result = UIntPtr.Zero;
        if (msg != 0x102) throw new Exception("Unexpected character message");
        Writes++;
        if (Timeout) return IntPtr.Zero;
        char ch = (char)w.ToInt32();
        if (!(CorruptChars && ch == '1')) Text += ch;
        return (IntPtr)1;
    }
    public static IntPtr SendMessage(IntPtr hwnd, int msg, IntPtr w, string text) {
        UIntPtr result;
        if (msg == 0x302) throw new Exception("Clipboard paste is forbidden");
        return SendMessageTimeoutText(hwnd, (uint)msg, w, text, 0x22, 750, out result);
    }
    public static IntPtr SendMessageInt(IntPtr hwnd, int msg, IntPtr w, IntPtr l) {
        UIntPtr result;
        return SendMessageTimeout(hwnd, (uint)msg, w, l, 0x22, 750, out result);
    }
}
public class NativeSearchList {
    public static int Row = 0;
    public static int SelectMatch(IntPtr frame, IntPtr list, uint pid, string reference) { return Row; }
}
'@
function Ensure-NativeMethods {}
function Ensure-UiAutomation {}
function Get-ChildWindowInfos {
    if ([NativeMethods]::PanelPresent) { [pscustomobject]@{Text='searchCtrl'} }
}
function Start-Sleep {}
function Write-Log { param($Message) }
function Get-WindowTextFast { return [NativeMethods]::Text }
function Get-Clipboard { throw 'Clipboard must not be read' }
function Set-ClipboardTextWithRetry { throw 'Clipboard must not be changed' }
'''
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", setup + body],
            env={**os.environ, "TEST_SEARCH_SCRIPT": str(SCRIPT)},
            capture_output=True, text=True, errors="replace", timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_focused_designator_write_preserves_all_characters_without_keys(self):
        self.assertEqual(self.run_functions(r'''
foreach ($ref in @('C103', 'C173', 'R1001', 'U46A')) {
    Set-SearchText -Text $ref -ProcessId 42
    [NativeMethods]::Text
}
''').splitlines(), ["C103", "C173", "R1001", "U46A"])

    def test_wrong_process_or_non_edit_focus_never_receives_input(self):
        for change in ("$edit.ProcessId = 99", "$edit = $null",
                       "$edit.ClassName = 'Canvas'; $edit.ProgrammaticName = 'ControlType.Pane'"):
            with self.subTest(change=change):
                self.assertEqual(self.run_functions(r'''
$edit = [System.Windows.Automation.AutomationElement]::FocusedElement
CHANGE
[System.Windows.Automation.AutomationElement]::FocusedElement = $edit
try { Set-SearchText -Text 'C103' -ProcessId 42; throw 'Unexpected success' }
catch { if ($_.Exception.Message -notmatch 'search box|search text') { throw } }
[NativeMethods]::Writes
'''.replace("CHANGE", change)), "0")

    def test_native_mismatch_is_repaired_using_targeted_characters(self):
        self.assertEqual(self.run_functions(r'''
[NativeMethods]::CorruptSet = $true
$ok = Set-EditWindowText -EditHwnd ([IntPtr]123) -Text 'C103'
@($ok, [NativeMethods]::Text) | ConvertTo-Json -Compress
'''), '[true,"C103"]')

    def test_persistent_dropped_character_is_not_reported_as_success(self):
        self.assertEqual(self.run_functions(r'''
[NativeMethods]::CorruptSet = $true
[NativeMethods]::CorruptChars = $true
$ok = Set-EditControlText -EditElement ([System.Windows.Automation.AutomationElement]::FocusedElement) -Text 'C103'
@($ok, [NativeMethods]::Text) | ConvertTo-Json -Compress
'''), '[false,"C03"]')

    def test_failed_clear_never_appends_more_characters(self):
        self.assertEqual(self.run_functions(r'''
[NativeMethods]::CorruptSet = $true
[NativeMethods]::IgnoreClear = $true
$ok = Set-EditWindowText -EditHwnd ([IntPtr]123) -Text 'C103'
@($ok, [NativeMethods]::Text) | ConvertTo-Json -Compress
'''), '[false,"C03"]')

    def test_timeout_or_invalid_native_control_fails_closed(self):
        for change in ("[NativeMethods]::Timeout = $true", "[NativeMethods]::ReadTimeout = $true",
                       "[NativeMethods]::ClassName = 'Canvas'"):
            with self.subTest(change=change):
                self.assertEqual(self.run_functions(change + r'''
Set-EditWindowText -EditHwnd ([IntPtr]123) -Text 'C103' | ConvertTo-Json
'''), "false")

    def test_matching_prefix_is_not_accepted_as_complete_designator(self):
        self.assertEqual(self.run_functions(r'''
[NativeMethods]::CorruptSet = $true
[NativeMethods]::ReplacedText = 'C103X'
[NativeMethods]::CorruptChars = $true
Set-EditWindowText -EditHwnd ([IntPtr]123) -Text 'C103' | ConvertTo-Json
'''), "false")

    def test_uia_value_is_checked_instead_of_accessible_name(self):
        for corrupt, expected in ((False, '[true,"C103"]'), (True, '[false,"C03"]')):
            with self.subTest(corrupt=corrupt):
                self.assertEqual(self.run_functions(r'''
$edit = [System.Windows.Automation.AutomationElement]::FocusedElement
$edit.NativeWindowHandle = 0
$edit.PatternValue.Corrupt = CORRUPT
$ok = Set-EditControlText -EditElement $edit -Text 'C103'
@($ok, $edit.PatternValue.Current.Value) | ConvertTo-Json -Compress
'''.replace("CORRUPT", "$true" if corrupt else "$false")), expected)

    def test_unsupported_uia_provider_never_falls_back_to_keyboard(self):
        self.assertEqual(self.run_functions(r'''
$edit = [System.Windows.Automation.AutomationElement]::FocusedElement
$edit.NativeWindowHandle = 0
$edit.Unsupported = $true
Set-EditControlText -EditElement $edit -Text 'C103' | ConvertTo-Json
'''), "false")

    def test_read_only_uia_value_is_not_overwritten(self):
        self.assertEqual(self.run_functions(r'''
$edit = [System.Windows.Automation.AutomationElement]::FocusedElement
$edit.NativeWindowHandle = 0
$edit.PatternValue.IsReadOnly = $true
$ok = Set-EditControlText -EditElement $edit -Text 'C103'
@($ok, $edit.PatternValue.Current.Value) | ConvertTo-Json -Compress
'''), '[false,"old"]')

    def test_unverified_find_text_throws_before_caller_can_send_enter(self):
        self.assertEqual(self.run_functions(r'''
[NativeMethods]::CorruptSet = $true
[NativeMethods]::CorruptChars = $true
try { Set-SearchText -Text 'C103' -ProcessId 42; throw 'Unexpected success' }
catch { if ($_.Exception.Message -notmatch 'search text') { throw }; 'blocked' }
'''), "blocked")

    def test_panel_activates_only_after_verified_write_without_retyping(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                result = json.loads(self.run_functions(r'''
[NativeMethods]::CorruptSet = CORRUPT
[NativeMethods]::CorruptChars = CORRUPT
$script:clicks = 0
function Wait-SearchPanelElementsFast {
    @{Mode='Win32'; EditHwnd=[IntPtr]123; GridRectSource='UIA'; GridRect=[pscustomobject]@{Left=10; Top=10}}
}
function Set-SearchText { throw 'Must not rewrite verified panel text using focused input' }
function Resolve-SearchPanelScreenPoint { [pscustomobject]@{X=42; Y=46} }
function Invoke-ProtectedSearchResultClicks { $script:clicks++ }
$ok = Invoke-SearchPanelFirstResult -Target ([pscustomobject]@{Id=42}) -Ref 'C103'
@($ok, $script:clicks, [NativeMethods]::Text) | ConvertTo-Json -Compress
'''.replace("CORRUPT", "$true" if corrupt else "$false")))
                self.assertEqual(result, [False, 0, "C03"] if corrupt else [True, 1, "C103"])

    def test_hidden_panel_opens_through_verified_window_without_global_shortcut(self):
        self.assertEqual(json.loads(self.run_functions(r'''
$script:opened = $false
$script:clicks = 0
function Wait-SearchPanelElementsFast {
    if ($script:opened) {
        @{Mode='Win32'; EditHwnd=[IntPtr]123; GridRectSource='Win32'; GridRect=[pscustomobject]@{Left=10; Top=10}}
    }
}
function Open-KiCadSearchPanel { param($Target) $script:opened = $true; return $true }
function Set-KiCadForeground {}
function Invoke-KiCadSearchPanelApi { return $false }
function Resolve-SearchPanelScreenPoint { [pscustomobject]@{X=42; Y=46} }
function Invoke-ProtectedSearchResultClicks { $script:clicks++ }
$ok = Invoke-SearchPanelFirstResult -Target ([pscustomobject]@{Id=42}) -Ref 'C1197'
@($ok, $script:opened, $script:clicks, [NativeMethods]::Text) | ConvertTo-Json -Compress
''')), [True, True, 1, 'C1197'])

    def test_native_panel_open_uses_visible_control_instead_of_cached_checkmark(self):
        for state, enabled, found, present, expected in (
            (0, True, True, False, [True, 1]), (8, True, True, False, [True, 1]),
            (0, True, True, True, [True, 0]), (8, True, True, True, [True, 0]),
            (1, True, True, False, [False, 0]), (2, True, True, False, [False, 0]),
            (0xFFFFFFFF, True, True, False, [False, 0]),
            (0, False, True, False, [False, 0]), (0, True, False, False, [False, 0]),
        ):
            with self.subTest(state=state, enabled=enabled, found=found):
                result = self.run_functions(r'''
$target = [pscustomobject]@{Id=42; MainWindowHandle=[IntPtr]123}
$target | Add-Member ScriptMethod Refresh {}
function Assert-KiCadWindowResponsive {}
function Find-KiCadMenuCommand { FOUND }
[NativeMethods]::MenuState = STATE
[NativeMethods]::Enabled = ENABLED
[NativeMethods]::PanelPresent = PRESENT
$ok = Open-KiCadSearchPanel -Target $target
@($ok, [NativeMethods]::Commands) | ConvertTo-Json -Compress
'''.replace('FOUND', 'return 20312' if found else 'return $null')
                    .replace('STATE', str(state)).replace('ENABLED', '$true' if enabled else '$false')
                    .replace('PRESENT', '$true' if present else '$false'))
                self.assertEqual(json.loads(result), expected)

    def test_short_visible_result_list_is_reused_without_uia(self):
        self.assertEqual(json.loads(self.run_functions(r'''
$target = [pscustomobject]@{Id=42; MainWindowHandle=[IntPtr]123}
$target | Add-Member ScriptMethod Refresh {}
function Ensure-UiAutomation { throw 'UIAutomation must not be loaded' }
function Get-ChildWindowInfos {
    [pscustomobject]@{ClassName='Edit';Hwnd=[IntPtr]123;Width=250;Height=18;Left=20;Right=270;Bottom=100;Text='C1197'}
    [pscustomobject]@{ClassName='SysListView32';Hwnd=[IntPtr]789;Width=260;Height=28;Left=15;Right=275;Top=125;Bottom=153;
                     Rect=[pscustomobject]@{Left=15;Top=125;Right=275;Bottom=153}}
}
$panel = Get-SearchPanelElementsFast -Target $target
@($panel.Mode, $panel.GridHwnd.ToInt32(), $panel.GridRectSource) | ConvertTo-Json -Compress
''')), ['Win32', 789, 'Win32'])

    def test_page_verification_accepts_root_and_arbitrary_nested_hierarchy(self):
        self.assertEqual(json.loads(self.run_functions(r'''
@(
    (Test-KiCadWindowMatchesPage -Window ([pscustomobject]@{MainWindowTitle='Probe - Schematic Editor'}) -Page 'Probe'),
    (Test-KiCadWindowMatchesPage -Window ([pscustomobject]@{MainWindowTitle='Parts [Probe/Module/Analog/Parts] - Schematic Editor'}) -Page 'Parts'),
    (Test-KiCadWindowMatchesPage -Window ([pscustomobject]@{MainWindowTitle='Other [Probe/Top/PartsExtra] - Schematic Editor'}) -Page 'Parts')
) | ConvertTo-Json -Compress
''')), [True, True, False])

    def test_failed_panel_open_never_writes_or_clicks(self):
        self.assertEqual(self.run_functions(r'''
function Wait-SearchPanelElementsFast { return $null }
function Open-KiCadSearchPanel { return $false }
function Invoke-ProtectedSearchResultClicks { throw 'Must not click without panel' }
Invoke-SearchPanelFirstResult -Target ([pscustomobject]@{Id=42}) -Ref 'C1197' | ConvertTo-Json -Compress
'''), 'false')

    def test_native_result_uses_explicit_view_action_without_enter_or_mouse(self):
        for show, expected in ((True, True), (False, False)):
            with self.subTest(show=show):
                self.assertEqual(json.loads(self.run_functions(r'''
$script:shown=0
function Wait-SearchPanelElementsFast {
    @{Mode='Win32'; EditHwnd=[IntPtr]123; GridHwnd=[IntPtr]789; GridRect=[pscustomobject]@{Left=10;Top=10}}
}
function Show-KiCadSearchSelection { $script:shown++; return SHOW }
function Invoke-ProtectedSearchResultClicks { throw 'Native list must not click by coordinate' }
$ok=Invoke-SearchPanelFirstResult -Target ([pscustomobject]@{Id=42;MainWindowHandle=[IntPtr]123}) -Ref 'C1197'
@($ok,$script:shown,[NativeMethods]::Text) | ConvertTo-Json -Compress
'''.replace('SHOW', '$true' if show else '$false'))), [expected, 1, 'C1197'])

    def test_real_native_edit_preserves_value_and_emits_change_notification(self):
        # A disposable hidden native Edit exercises the real P/Invoke declarations,
        # not KiCad or any user's window. No activation, keys, or clipboard.
        script = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TEST_SEARCH_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($node in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    if ($node.Name -in @('Ensure-NativeMethods', 'Set-EditWindowText', 'Get-EditWindowText', 'Get-WindowTextFast')) {
        . ([scriptblock]::Create($node.Extent.Text))
    }
}
function Write-Log { param($Message) }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -ReferencedAssemblies System.Windows.Forms @'
using System;
using System.Runtime.InteropServices;
using System.Windows.Forms;
public class SearchTestHost : Form {
    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateWindowEx(int ex, string cls, string text, int style, int x, int y, int w, int h, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);
    public IntPtr Edit;
    public IntPtr SearchList;
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct Item {
        public uint mask; public int item, subItem; public uint state, stateMask;
        public IntPtr text; public int textMax, image; public IntPtr param;
        public int indent, groupId; public uint columns; public IntPtr columnPtr, formatPtr;
        public int group;
    }
    [DllImport("user32.dll", EntryPoint="SendMessageW")]
    static extern IntPtr InsertItem(IntPtr hwnd, int message, IntPtr unused, ref Item item);
    [DllImport("user32.dll", EntryPoint="SendMessageW")]
    static extern IntPtr GetState(IntPtr hwnd, int message, IntPtr row, IntPtr mask);
    void AddRow(int index, string text) {
        var item = new Item { mask=1, item=index, text=Marshal.StringToHGlobalUni(text) };
        try { if (InsertItem(SearchList, 0x104d, IntPtr.Zero, ref item).ToInt64() != index) throw new Exception("Cannot add fixture row"); }
        finally { Marshal.FreeHGlobal(item.text); }
    }
    public bool Selected(int index) { return GetState(SearchList, 0x102c, (IntPtr)index, (IntPtr)2).ToInt64() == 2; }
    public int Changes;
    public SearchTestHost() {
        Edit = CreateWindowEx(0, "Edit", "", 0x40000080, 0, 0, 200, 24, Handle, (IntPtr)1, IntPtr.Zero, IntPtr.Zero);
        if (Edit == IntPtr.Zero) throw new Exception("Cannot create disposable edit");
        // Initialize common controls, then create KiCad's actual native class.
        using (var registration = new ListView()) { IntPtr created = registration.Handle; }
        SearchList = CreateWindowEx(0, "SysListView32", "", 0x40000001, 0, 30, 250, 28, Handle, (IntPtr)2, IntPtr.Zero, IntPtr.Zero);
        if (SearchList == IntPtr.Zero) throw new Exception("Cannot create disposable search list");
        AddRow(0, "C11970"); AddRow(1, "C1197");
    }
    protected override void WndProc(ref Message msg) {
        if (msg.Msg == 0x111 && ((msg.WParam.ToInt64() >> 16) & 0xffff) == 0x300) Changes++;
        base.WndProc(ref msg);
    }
}
'@
$hostWindow = New-Object SearchTestHost
try {
    foreach ($ref in @('C103', 'R1001', 'U46A', ('C' + ('1' * 600)))) {
        $before = $hostWindow.Changes
        if (-not (Set-EditWindowText -EditHwnd $hostWindow.Edit -Text $ref)) { throw "Native write failed: $ref" }
        $buffer = New-Object System.Text.StringBuilder 1024
        [NativeMethods]::GetWindowText($hostWindow.Edit, $buffer, $buffer.Capacity) | Out-Null
        if ($buffer.ToString() -cne $ref) { throw 'Actual edit value differs' }
        if ($hostWindow.Changes -le $before) { throw 'No text change notification' }
    }
    if (Set-EditWindowText -EditHwnd ([IntPtr]::Zero) -Text 'C103') { throw 'Accepted invalid handle' }
    $row = [NativeSearchList]::SelectMatch($hostWindow.Handle, $hostWindow.SearchList, $PID, 'C1197')
    if ($row -ne 1 -or $hostWindow.Selected(0) -or -not $hostWindow.Selected(1)) {
        throw "Native list did not select the exact reference in a short panel: row=$row"
    }
    if ([NativeSearchList]::SelectMatch($hostWindow.Handle, $hostWindow.SearchList, ($PID+1), 'C1197') -ne -2) {
        throw 'Accepted wrong process'
    }
    if ([NativeSearchList]::SelectMatch([IntPtr]::Zero, $hostWindow.SearchList, $PID, 'C1197') -ne -2) {
        throw 'Accepted wrong frame'
    }
    if ([NativeSearchList]::SelectMatch($hostWindow.Handle, $hostWindow.SearchList, $PID, 'C119') -ne -1) {
        throw 'Accepted a prefix as an exact designator'
    }
    'verified'
} finally { $hostWindow.Dispose() }
'''
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
            env={**os.environ, "TEST_SEARCH_SCRIPT": str(SCRIPT)},
            capture_output=True, text=True, errors="replace", timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "verified")
