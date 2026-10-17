param(
    [Parameter(Position = 0)]
    [string]$UriOrRef,

    # SendKeys syntax for Windows PowerShell 5.1:
    #   ^ = Ctrl, + = Shift
    # Default matches the project shortcut rule: Ctrl+Shift+F opens Find/Search.
    [string]$SearchHotkey = "^+f",

    [int]$DelayMs = 0,

    [int]$SearchFocusTimeoutMs = 2500,

    [string]$KicadExe = "C:\Program Files\KiCad\10.0\bin\kicad.exe",

    [string]$KicadSchematicExe = "C:\Program Files\KiCad\10.0\bin\eeschema.exe",

    [string]$KicadMcpPython = "C:\Program Files\KiCad\10.0\bin\kicad-mcp\.venv\Scripts\python.exe",

    [switch]$DryRun,

    [switch]$ShowErrors,

    [switch]$DisableIpcFind,

    [switch]$MoveCursorAfterJump,

    [switch]$SaveActiveFrame,

    [switch]$CloseAfterSave,

    [switch]$SaveOnly,

    [switch]$ReloadFromDisk,

    [switch]$NotifyOnly,

    [switch]$EnablePageSwitch,

    [ValidateSet("SearchPanel", "FindDialog")]
    [string]$Mode = "SearchPanel",

    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"
$LogPath = Join-Path $env:TEMP "kicad_schematic_search.log"
$WindowCachePath = Join-Path $env:TEMP "kicad_schematic_search.pid"

function Write-Log {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"), $Message
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        try {
            Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
            return
        } catch {
            Start-Sleep -Milliseconds 80
        }
    }
}

function Set-ClipboardTextWithRetry {
    param([string]$Text)

    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        try {
            Set-Clipboard -Value $Text -ErrorAction Stop
            return $true
        } catch {
            Start-Sleep -Milliseconds 50
        }
    }
    return $false
}

function Show-UserMessage {
    param([string]$Message)
    if (-not $ShowErrors) {
        return
    }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shell.Popup($Message, 8, "KiCad schematic search", 48) | Out-Null
    } catch {
        Write-Log ("POPUP_FAILED " + $_.Exception.Message)
    }
}

function Get-DesignatorFromInput {
    param([string]$InputText)

    if ($null -eq $InputText) {
        $text = ""
    } else {
        $text = $InputText.Trim().Trim('"')
    }

    if ($text -match "^[a-zA-Z][a-zA-Z0-9+.-]*:") {
        try {
            $uri = New-Object System.Uri($text)
            if ($uri.Host) {
                $text = $uri.Host + $uri.AbsolutePath
            } else {
                $text = $uri.AbsolutePath
            }
        } catch {
            $text = $text -replace "^[a-zA-Z][a-zA-Z0-9+.-]*:/*", ""
        }
    }

    $text = [System.Uri]::UnescapeDataString($text)
    $text = ($text -replace "[^A-Za-z0-9_?+-]", "").Trim().ToUpperInvariant()
    if ($text -notmatch "^[A-Z]+\d+[A-Z]?$") {
        throw "Invalid KiCad designator: $InputText"
    }
    return $text
}

function Get-PageFromInput {
    param([string]$InputText)

    if ($null -eq $InputText -or $InputText -notmatch "^[a-zA-Z][a-zA-Z0-9+.-]*:") {
        return ""
    }

    try {
        $uri = New-Object System.Uri($InputText)
        $query = [System.Web.HttpUtility]::ParseQueryString($uri.Query)
        return [System.Uri]::UnescapeDataString(($query["page"] + "").Trim())
    } catch {
        return ""
    }
}

function Ensure-NativeMethods {
    if ("NativeMethods" -as [type]) {
        return
    }

    Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public delegate bool EnumWindowProc(IntPtr hWnd, IntPtr lParam);

public static class NativeMethods {
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowProc callback, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    public static IntPtr[] TopLevelWindows() {
        var windows = new System.Collections.Generic.List<IntPtr>();
        EnumWindows(delegate(IntPtr hwnd, IntPtr param) {
            if (IsWindowVisible(hwnd)) windows.Add(hwnd);
            return true;
        }, IntPtr.Zero);
        return windows.ToArray();
    }
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    public static extern bool GetCursorPos(out POINT lpPoint);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);

    [DllImport("user32.dll")]
    public static extern bool BlockInput(bool fBlockIt);

    [DllImport("user32.dll")]
    public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("user32.dll")]
    public static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, string lParam);

    [DllImport("user32.dll", EntryPoint="SendMessageW")]
    public static extern IntPtr SendMessageInt(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern bool IsWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern IntPtr SetFocus(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern uint GetDpiForWindow(IntPtr hWnd);
}

[StructLayout(LayoutKind.Sequential)]
public struct RECT {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
}

[StructLayout(LayoutKind.Sequential)]
public struct POINT {
    public int X;
    public int Y;
}

// KiCad 10 can host multiple editors in one process; retain the exact frame HWND.
public class KiCadEditorWindow {
    public int Id { get; private set; }
    public string ProcessName { get; private set; }
    public IntPtr MainWindowHandle { get; private set; }
    public string MainWindowTitle { get; private set; }
    public KiCadEditorWindow(IntPtr hwnd) {
        uint pid;
        NativeMethods.GetWindowThreadProcessId(hwnd, out pid);
        Id = (int)pid;
        ProcessName = System.Diagnostics.Process.GetProcessById(Id).ProcessName;
        MainWindowHandle = hwnd;
        Refresh();
    }
    public void Refresh() {
        uint pid;
        NativeMethods.GetWindowThreadProcessId(MainWindowHandle, out pid);
        if (pid != Id || !NativeMethods.IsWindow(MainWindowHandle))
            throw new InvalidOperationException("KiCad editor frame no longer exists");
        var title = new StringBuilder(1024);
        NativeMethods.GetWindowText(MainWindowHandle, title, title.Capacity);
        MainWindowTitle = title.ToString();
    }
    public bool CloseMainWindow() {
        Refresh();
        return NativeMethods.PostMessage(MainWindowHandle, 0x0010, IntPtr.Zero, IntPtr.Zero);
    }
}
"@
}

function Ensure-UiAutomation {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
}

function Wait-FocusedEditControl {
    param(
        [int]$TimeoutMs,
        [int]$ProcessId
    )

    Ensure-UiAutomation
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    do {
        $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
        if ($null -ne $focused) {
            $controlType = $focused.Current.ControlType.ProgrammaticName
            $className = $focused.Current.ClassName
            $name = $focused.Current.Name
            $focusedPid = $focused.Current.ProcessId
            Write-Log ("FOCUS pid={0} type={1} class={2} name={3}" -f $focusedPid, $controlType, $className, $name)
            if ($focusedPid -eq $ProcessId -and ($controlType -eq "ControlType.Edit" -or $className -eq "Edit")) {
                return $true
            }
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)

    return $false
}

function Get-ForegroundProcessId {
    Ensure-NativeMethods
    $hwnd = [NativeMethods]::GetForegroundWindow()
    [uint32]$foregroundProcessId = 0
    [NativeMethods]::GetWindowThreadProcessId($hwnd, [ref]$foregroundProcessId) | Out-Null
    return [int]$foregroundProcessId
}

function Test-SchematicEditorWindowName {
    param([string]$Name)

    $schematicText = -join ([char]0x539F, [char]0x7406, [char]0x56FE, [char]0x7F16, [char]0x8F91, [char]0x5668)
    return ($Name -match "Schematic Editor" -or $Name.Contains($schematicText))
}

function Get-SchematicPageAliases {
    param([string]$Page)

    $aliases = New-Object System.Collections.Generic.List[string]
    if ($Page) {
        $aliases.Add($Page)
    }
    switch ($Page) {
        "FGND_POWER" { $aliases.Add("FGND") }
        "Interface_7030" { $aliases.Add("7030") }
        "Out_Stage" { $aliases.Add("Out") }
        "Sample_Process" { $aliases.Add("Sample_Proc") }
    }
    return $aliases
}

function Wait-ForegroundProcess {
    param(
        [int]$ProcessId,
        [int]$TimeoutMs
    )

    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    do {
        $foregroundPid = Get-ForegroundProcessId
        Write-Log ("FOREGROUND pid={0} target={1}" -f $foregroundPid, $ProcessId)
        if ($foregroundPid -eq $ProcessId) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)

    return $false
}

function Set-KiCadForeground {
    param($Target)

    Ensure-NativeMethods
    if ([NativeMethods]::GetForegroundWindow() -eq $Target.MainWindowHandle) {
        Write-Log ("ACTIVATE_SKIP already_foreground id={0}" -f $Target.Id)
        return
    }
    $shell = New-Object -ComObject WScript.Shell
    [NativeMethods]::ShowWindowAsync($Target.MainWindowHandle, 9) | Out-Null
    Start-Sleep -Milliseconds 20
    $appActivateByTitle = $shell.AppActivate($Target.MainWindowTitle)
    Start-Sleep -Milliseconds 20

    $hwndTopMost = [IntPtr](-1)
    $hwndNoTopMost = [IntPtr](-2)
    $swpNoMove = 0x0002
    $swpNoSize = 0x0001
    $swpShowWindow = 0x0040
    $flags = $swpNoMove -bor $swpNoSize -bor $swpShowWindow
    [NativeMethods]::SetWindowPos($Target.MainWindowHandle, $hwndTopMost, 0, 0, 0, 0, $flags) | Out-Null
    Start-Sleep -Milliseconds 25
    $setForeground = [NativeMethods]::SetForegroundWindow($Target.MainWindowHandle)
    [uint32]$foregroundProcessId = 0
    $foregroundHwnd = [NativeMethods]::GetForegroundWindow()
    $foregroundThreadId = [NativeMethods]::GetWindowThreadProcessId($foregroundHwnd, [ref]$foregroundProcessId)
    [uint32]$targetProcessId = 0
    $targetThreadId = [NativeMethods]::GetWindowThreadProcessId($Target.MainWindowHandle, [ref]$targetProcessId)
    $currentThreadId = [NativeMethods]::GetCurrentThreadId()
    if ($foregroundThreadId -ne 0 -and $targetThreadId -ne 0) {
        [NativeMethods]::AttachThreadInput($currentThreadId, $foregroundThreadId, $true) | Out-Null
        [NativeMethods]::AttachThreadInput($currentThreadId, $targetThreadId, $true) | Out-Null
        [NativeMethods]::BringWindowToTop($Target.MainWindowHandle) | Out-Null
        $setForeground = [NativeMethods]::SetForegroundWindow($Target.MainWindowHandle)
        [NativeMethods]::AttachThreadInput($currentThreadId, $targetThreadId, $false) | Out-Null
        [NativeMethods]::AttachThreadInput($currentThreadId, $foregroundThreadId, $false) | Out-Null
    }
    Start-Sleep -Milliseconds 25
    [NativeMethods]::SetWindowPos($Target.MainWindowHandle, $hwndNoTopMost, 0, 0, 0, 0, $flags) | Out-Null
    Write-Log ("ACTIVATE id={0} hwnd={1} byTitle={2} setForeground={3}" -f $Target.Id, $Target.MainWindowHandle, $appActivateByTitle, $setForeground)
}

function Get-KiCadTopLevelWindows {
    Ensure-NativeMethods
    $processIds = @{}
    foreach ($process in (Get-Process -Name kicad,eeschema -ErrorAction SilentlyContinue)) {
        $processIds[[int]$process.Id] = $true
    }
    foreach ($hwnd in [NativeMethods]::TopLevelWindows()) {
        [uint32]$windowPid = 0
        [NativeMethods]::GetWindowThreadProcessId($hwnd, [ref]$windowPid) | Out-Null
        if ($processIds.ContainsKey([int]$windowPid)) {
            try { [KiCadEditorWindow]::new($hwnd) } catch { Write-Log ("WINDOW_GONE hwnd=" + $hwnd) }
        }
    }
}

function Get-KiCadCandidateWindows {
    $schematicText = -join ([char]0x539F, [char]0x7406, [char]0x56FE)

    $processes = Get-KiCadTopLevelWindows | Where-Object {
        $title = $_.MainWindowTitle
        $_.MainWindowHandle -ne 0 -and
        $title -and
        $title -notmatch "KiCad\s*错误|KiCad\s*Error" -and
        ($_.ProcessName -match "kicad|eeschema") -and
        (Test-SchematicEditorWindowName -Name $title)
    }

    $processes | Where-Object { Test-KiCadProcessBelongsToProject -Window $_ } | Sort-Object @{
        Expression = {
            $title = $_.MainWindowTitle
            if ($title -match "Schematic|\.kicad_sch" -or $title.Contains($schematicText)) { 0 }
            elseif ($title -match "PCB|\.kicad_pcb") { 2 }
            else { 1 }
        }
    }, Id
}

function Wait-KiCadEditorFrameClosed {
    param(
        $Target,
        [int]$TimeoutMs
    )

    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    do {
        try {
            $Target.Refresh()
            if (-not (Test-SchematicEditorWindowName -Name $Target.MainWindowTitle)) {
                Write-Log ("CLOSE_CONFIRMED id={0} hwnd={1}" -f $Target.Id, $Target.MainWindowHandle)
                return $true
            }
        } catch {
            Write-Log ("CLOSE_CONFIRMED frame_closed id={0}" -f $Target.Id)
            return $true
        }
        Start-Sleep -Milliseconds 120
    } while ((Get-Date) -lt $deadline)

    try {
        $Target.Refresh()
        Write-Log ("CLOSE_WAIT_TIMEOUT id={0} title={1}" -f $Target.Id, $Target.MainWindowTitle)
    } catch {
        Write-Log ("CLOSE_WAIT_TIMEOUT id={0}" -f $Target.Id)
    }
    return $false
}

function Get-CachedKiCadWindow {
    if (-not (Test-Path -LiteralPath $WindowCachePath)) {
        return $null
    }
    try {
        $cached = [System.IO.File]::ReadAllText($WindowCachePath) | ConvertFrom-Json
        foreach ($candidate in (Get-KiCadTopLevelWindows)) {
            if ($candidate.Id -eq $cached.pid -and $candidate.MainWindowHandle.ToInt64() -eq $cached.hwnd -and
                (Test-SchematicEditorWindowName -Name $candidate.MainWindowTitle) -and
                (Test-KiCadProcessBelongsToProject -Window $candidate)) {
                Write-Log ("WINDOW_CACHE_HIT id={0} hwnd={1} title={2}" -f $candidate.Id, $candidate.MainWindowHandle, $candidate.MainWindowTitle)
                return $candidate
            }
        }
    } catch {
        Write-Log ("WINDOW_CACHE_MISS " + $_.Exception.Message)
    }
    return $null
}

function Save-KiCadWindowCache {
    param($Window)

    try {
        $value = @{pid=$Window.Id; hwnd=$Window.MainWindowHandle.ToInt64()} | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($WindowCachePath, $value, [System.Text.Encoding]::ASCII)
    } catch {
        Write-Log ("WINDOW_CACHE_SAVE_FAILED " + $_.Exception.Message)
    }
}

function Get-ProjectRoot {
    if ($ProjectRoot) {
        return (Resolve-Path -LiteralPath $ProjectRoot).Path
    }
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}

function Get-CommandLineProjectStatus {
    param([string]$CommandLine, [string]$Root)

    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]"\/")
    $prefix = $rootPath + [System.IO.Path]::DirectorySeparatorChar
    $foundOther = $false
    foreach ($match in [regex]::Matches($CommandLine, '"([^"]+)"|(\S+)')) {
        $argument = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
        if ($argument -notmatch '\.kicad_(pro|sch|pcb)$' -or -not [System.IO.Path]::IsPathRooted($argument)) {
            continue
        }
        $file = [System.IO.Path]::GetFullPath($argument)
        if ($file.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            return 'match'
        }
        $foundOther = $true
    }
    if ($foundOther) { return 'other' }
    return 'unknown'
}

function Test-KiCadProcessBelongsToProject {
    param($Window)

    $processInfo = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $Window.Id) -ErrorAction Stop
    $status = Get-CommandLineProjectStatus -CommandLine $processInfo.CommandLine -Root (Get-ProjectRoot)
    if ($status -eq 'unknown') {
        $status = Get-HostedProjectStatus -Window $Window
    }
    if ($status -eq 'unknown') {
        throw "Cannot verify schematic window project (PID=$($Window.Id)). Close that schematic manually and retry; no automatic save or close was performed."
    }
    return $status -eq 'match'
}

function Get-HostedProjectStatus {
    param($Window)

    $paths = @(Get-HostedProjectPaths -ProcessId $Window.Id | Select-Object -Unique)
    # A same-name window is insufficient: require one absolute project bound to this PID.
    if ($paths.Count -ne 1) { return 'unknown' }
    $expected = Get-KiCadProjectFile
    if (-not $expected -or -not $paths[0].Equals($expected, [StringComparison]::OrdinalIgnoreCase)) { return 'other' }
    $name = [System.IO.Path]::GetFileNameWithoutExtension((Get-KiCadProjectFile))
    $escapedName = [regex]::Escape($name)
    if (-not $name -or $Window.MainWindowTitle -notmatch ('(?:^\*?' + $escapedName + '(?:\s|\[|$)|\[' + $escapedName + '/)')) {
        return 'unknown'
    }
    return 'match'
}

function Get-HostedProjectPaths {
    param([int]$ProcessId)

    Ensure-UiAutomation
    foreach ($frame in (Get-KiCadTopLevelWindows)) {
        if ($frame.Id -ne $ProcessId -or $frame.MainWindowTitle -notmatch '\sKiCad\s+\d') { continue }
        try {
            $element = [System.Windows.Automation.AutomationElement]::FromHandle($frame.MainWindowHandle)
            $children = $element.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
            foreach ($child in $children) {
                $text = $child.Current.Name
                if ($text -match '(?i)((?:[A-Z]:[\\/]|\\\\).+\.kicad_pro)\s*$') {
                    $path = [System.IO.Path]::GetFullPath($matches[1])
                    Write-Log ("HOSTED_PROJECT pid={0} hwnd={1} path={2}" -f $ProcessId, $frame.MainWindowHandle, $path)
                    $path
                }
            }
        } catch { Write-Log ("HOSTED_PROJECT_FAILED " + $_.Exception.Message) }
    }
}

function Get-KiCadProjectFile {
    $projectRoot = Get-ProjectRoot
    $projects = @(Get-ChildItem -LiteralPath $projectRoot -Filter "*.kicad_pro" -File -ErrorAction SilentlyContinue | Sort-Object Name)
    if ($projects.Count -gt 0) {
        return $projects[0].FullName
    }
    return ""
}

function Get-TopLevelSchematicFile {
    $projectRoot = Get-ProjectRoot
    $projectPath = Get-KiCadProjectFile
    if ($projectPath) {
        $projectBaseName = [System.IO.Path]::GetFileNameWithoutExtension($projectPath)
        $sameNameSchematic = Join-Path $projectRoot ($projectBaseName + ".kicad_sch")
        if (Test-Path -LiteralPath $sameNameSchematic) {
            return $sameNameSchematic
        }
    }
    $schematics = @(Get-ChildItem -LiteralPath $projectRoot -Filter "*.kicad_sch" -File -ErrorAction SilentlyContinue | Sort-Object Name)
    if ($schematics.Count -gt 0) {
        return $schematics[0].FullName
    }
    return ""
}

function Ensure-KiCadProjectOpen {
    $projectPath = Get-KiCadProjectFile
    if (-not $projectPath) {
        Write-Log "OPEN_PROJECT missing_project_file"
        return
    }
    $openProjectWindows = @(Get-KiCadTopLevelWindows | Where-Object {
        $_.MainWindowHandle -ne 0 -and
        $_.MainWindowTitle -and
        $_.MainWindowTitle -notmatch "KiCad\s*错误|KiCad\s*Error"
    } | Where-Object {
        Test-KiCadProcessBelongsToProject -Window $_
    })
    if ($openProjectWindows.Count -gt 0) {
        Write-Log ("OPEN_PROJECT already_open count={0}" -f $openProjectWindows.Count)
        return
    }
    if (-not (Test-Path -LiteralPath $KicadExe)) {
        Write-Log "OPEN_PROJECT missing_kicad $KicadExe"
        return
    }

    Write-Log "OPEN_PROJECT $projectPath"
    Start-Process -FilePath $KicadExe -ArgumentList @('"' + $projectPath + '"')
    Start-Sleep -Milliseconds 1200
}

function Test-KiCadWindowMatchesPage {
    param(
        $Window,
        [string]$Page
    )

    if (-not $Page -or -not $Window.MainWindowTitle) {
        return $false
    }

    foreach ($alias in (Get-SchematicPageAliases -Page $Page)) {
        $escapedPage = [regex]::Escape($alias)
        if ($Window.MainWindowTitle -match ("/Top/" + $escapedPage + "\]")) {
            return $true
        }
    }
    return $false
}

function Open-SchematicPage {
    param([string]$Page)

    if (-not $Page) {
        Write-Log "OPEN_PAGE skipped no_page"
        return
    }
    if (@(Get-KiCadCandidateWindows).Count -gt 0) {
        Write-Log 'OPEN_PAGE reuse_existing_schematic'
        return
    }

    $topSchematicPath = Get-TopLevelSchematicFile
    if (-not $topSchematicPath -or -not (Test-Path -LiteralPath $topSchematicPath)) {
        Write-Log "OPEN_PAGE missing_top_schematic"
        return
    }
    if (-not (Test-Path -LiteralPath $KicadSchematicExe)) {
        Write-Log "OPEN_PAGE missing_eeschema $KicadSchematicExe"
        return
    }

    Ensure-KiCadProjectOpen
    Write-Log "OPEN_PAGE top_schematic=$topSchematicPath target_page=$Page"
    Write-Log "OPEN_PAGE eeschema=$KicadSchematicExe schematic=$topSchematicPath"
    Start-Process -FilePath $KicadSchematicExe -ArgumentList @('"' + $topSchematicPath + '"')
}

function Wait-KiCadWindow {
    param(
        [string]$Page,
        [int]$TimeoutMs
    )

    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    do {
        $windows = @(Get-KiCadCandidateWindows)
        $unsavedText = -join ([char]0x672A, [char]0x4FDD, [char]0x5B58)
        $readyWindows = @($windows | Where-Object {
            $_.MainWindowTitle -and
            $_.MainWindowTitle -notmatch "untitled" -and
            -not $_.MainWindowTitle.Contains($unsavedText)
        })
        if ($readyWindows.Count -gt 0) {
            $windows = $readyWindows
        } else {
            $windows = @()
        }
        if ($windows.Count -gt 0) {
            if ($Page) {
                $pageWindow = @($windows | Where-Object { Test-KiCadWindowMatchesPage -Window $_ -Page $Page })
                if ($pageWindow.Count -gt 0) {
                    return $pageWindow
                }
            }
            return $windows
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    return @()
}

function Switch-SchematicPageFromHierarchy {
    param(
        $Target,
        [string]$Page
    )

    if (-not $Page) {
        return $false
    }

    Ensure-UiAutomation
    $wins = @([System.Windows.Automation.AutomationElement]::FromHandle($Target.MainWindowHandle))
    $winCount = [int]$wins.Count
    Write-Log ("SEARCH_PANEL_SCAN windows={0}" -f $winCount)
    for ($i = 0; $i -lt $winCount; $i++) {
        $win = $wins[$i]
        Write-Log ("SEARCH_PANEL_SCAN_WINDOW name={0}" -f $win.Current.Name)
        if (-not (Test-SchematicEditorWindowName -Name $win.Current.Name)) {
            continue
        }
        $items = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        $itemCount = [int]$items.Count
        Write-Log ("SWITCH_PAGE scan_windows={0} scan_items={1} page={2}" -f $winCount, $itemCount, $Page)
        for ($j = 0; $j -lt $itemCount; $j++) {
            $item = $items.Item($j)
            if ($item.Current.ControlType.ProgrammaticName -ne "ControlType.TreeItem") {
                continue
            }
            $matched = $false
            foreach ($alias in (Get-SchematicPageAliases -Page $Page)) {
                if ($item.Current.Name -match ("^" + [regex]::Escape($alias) + "\b")) {
                    $matched = $true
                    break
                }
            }
            if (-not $matched) {
                continue
            }
            $rect = $item.Current.BoundingRectangle
            if ($rect.IsEmpty) {
                continue
            }
            $x = [int]($rect.Left + 18)
            $y = [int]($rect.Top + ($rect.Height / 2))
            Write-Log ("SWITCH_PAGE hierarchy page={0} click={1},{2}" -f $Page, $x, $y)
            Invoke-MouseClick -X $x -Y $y -Count 2
            Start-Sleep -Milliseconds 900
            return $true
        }
    }

    Write-Log "SWITCH_PAGE hierarchy_not_found page=$Page"
    return $false
}

function Get-KiCadWindowRect {
    param([System.IntPtr]$WindowHandle)

    Ensure-NativeMethods
    $rect = New-Object RECT
    if (-not [NativeMethods]::GetWindowRect($WindowHandle, [ref]$rect)) {
        throw "Failed to read KiCad window rectangle."
    }
    return $rect
}

function Get-WindowTextFast {
    param([System.IntPtr]$WindowHandle)

    Ensure-NativeMethods
    $buffer = New-Object System.Text.StringBuilder 512
    [NativeMethods]::GetWindowText($WindowHandle, $buffer, $buffer.Capacity) | Out-Null
    return $buffer.ToString()
}

function Get-ChildWindowInfos {
    param([System.IntPtr]$RootWindowHandle)

    Ensure-NativeMethods
    $items = New-Object System.Collections.ArrayList
    $callback = [EnumWindowProc]{
        param([System.IntPtr]$ChildHwnd, [System.IntPtr]$LParam)

        if (-not [NativeMethods]::IsWindowVisible($ChildHwnd)) {
            return $true
        }
        $classBuffer = New-Object System.Text.StringBuilder 256
        [NativeMethods]::GetClassName($ChildHwnd, $classBuffer, $classBuffer.Capacity) | Out-Null
        $rect = New-Object RECT
        if (-not [NativeMethods]::GetWindowRect($ChildHwnd, [ref]$rect)) {
            return $true
        }
        $width = $rect.Right - $rect.Left
        $height = $rect.Bottom - $rect.Top
        if ($width -le 0 -or $height -le 0) {
            return $true
        }
        $textBuffer = New-Object System.Text.StringBuilder 512
        [NativeMethods]::GetWindowText($ChildHwnd, $textBuffer, $textBuffer.Capacity) | Out-Null
        $items.Add([pscustomobject]@{
            Hwnd = $ChildHwnd
            ClassName = $classBuffer.ToString()
            Text = $textBuffer.ToString()
            Rect = $rect
            Left = $rect.Left
            Top = $rect.Top
            Right = $rect.Right
            Bottom = $rect.Bottom
            Width = $width
            Height = $height
        }) | Out-Null
        return $true
    }
    [NativeMethods]::EnumChildWindows($RootWindowHandle, $callback, [IntPtr]::Zero) | Out-Null
    return @($items.ToArray())
}

function Get-SearchPanelElementsFast {
    param($Target)

    $Target.Refresh()
    $windowHandle = $Target.MainWindowHandle
    if ($windowHandle -eq [IntPtr]::Zero) {
        Write-Log "SEARCH_PANEL_FAST no_window_handle"
        return $null
    }

    $children = @(Get-ChildWindowInfos -RootWindowHandle $windowHandle)
    $edits = @($children | Where-Object { $_.ClassName -eq "Edit" -and $_.Width -gt 200 -and $_.Height -ge 16 -and $_.Height -le 60 })
    $grids = @($children | Where-Object { $_.ClassName -eq "SysListView32" -and $_.Width -gt 200 -and $_.Height -gt 80 })
    Write-Log ("SEARCH_PANEL_FAST_COUNTS edits={0} grids={1}" -f $edits.Count, $grids.Count)

    $bestPair = $null
    foreach ($grid in ($grids | Sort-Object Height -Descending)) {
        foreach ($edit in ($edits | Sort-Object Width -Descending)) {
            $isAboveGrid = $edit.Bottom -le ($grid.Top - 4)
            $isHorizontallyAligned = $edit.Left -ge ($grid.Left - 40) -and $edit.Right -le ($grid.Right + 40)
            if ($isAboveGrid -and $isHorizontallyAligned) {
                $gridRect = $grid.Rect
                $gridRectSource = "Win32"
                try {
                    Ensure-UiAutomation
                    $gridElement = [System.Windows.Automation.AutomationElement]::FromHandle($grid.Hwnd)
                    if ($null -ne $gridElement) {
                        $uiaRect = $gridElement.Current.BoundingRectangle
                        if (-not $uiaRect.IsEmpty -and $uiaRect.Width -gt 0 -and $uiaRect.Height -gt 0) {
                            $gridRect = [pscustomobject]@{
                                Left = [int]$uiaRect.Left
                                Top = [int]$uiaRect.Top
                                Right = [int]($uiaRect.Left + $uiaRect.Width)
                                Bottom = [int]($uiaRect.Top + $uiaRect.Height)
                                Width = [int]$uiaRect.Width
                                Height = [int]$uiaRect.Height
                            }
                            $gridRectSource = "UIA"
                            Write-Log ("SEARCH_PANEL_GRID_RECT source=UIA rect={0},{1},{2},{3}" -f $gridRect.Left, $gridRect.Top, $gridRect.Right, $gridRect.Bottom)
                        }
                    }
                } catch {
                    Write-Log ("SEARCH_PANEL_GRID_RECT_UIA_FAILED " + $_.Exception.Message)
                }
                $bestPair = @{
                    Mode = "Win32"
                    EditHwnd = $edit.Hwnd
                    GridRect = $gridRect
                    GridRectSource = $gridRectSource
                    EditText = $edit.Text
                }
                Write-Log ("SEARCH_PANEL_FAST_MATCH edit={0} grid={1},{2},{3},{4}" -f $edit.Text, $grid.Left, $grid.Top, $grid.Right, $grid.Bottom)
                return $bestPair
            }
        }
    }

    return $null
}

function Invoke-MouseClick {
    param(
        [int]$X,
        [int]$Y,
        [int]$Count = 1
    )

    Ensure-NativeMethods
    [NativeMethods]::SetCursorPos($X, $Y) | Out-Null
    Start-Sleep -Milliseconds 20
    for ($i = 0; $i -lt $Count; $i++) {
        [NativeMethods]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 15
        [NativeMethods]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 25
    }
}

function Invoke-ProtectedSearchResultClicks {
    param(
        [int]$X,
        [int]$Y,
        [int]$IntervalMs = 460
    )

    Ensure-NativeMethods
    $original = New-Object POINT
    $hasOriginal = [NativeMethods]::GetCursorPos([ref]$original)
    $blocked = $false
    try {
        $blocked = [NativeMethods]::BlockInput($true)
        Write-Log ("MOUSE_PROTECT block_input={0} original={1},{2}" -f $blocked, $original.X, $original.Y)
        Invoke-MouseClick -X $X -Y $Y -Count 1
        Write-Log "SEARCH_PANEL_ACTIVATE method=first_click"
        Start-Sleep -Milliseconds $IntervalMs
        Invoke-MouseClick -X $X -Y $Y -Count 1
        Write-Log "SEARCH_PANEL_ACTIVATE method=second_click"
        Start-Sleep -Milliseconds 120
        try {
            [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
            Write-Log "SEARCH_PANEL_ACTIVATE method=enter"
        } catch {
            Write-Log ("SEARCH_PANEL_ENTER_FAILED " + $_.Exception.Message)
        }
        Start-Sleep -Milliseconds 180
    } finally {
        if ($blocked) {
            [NativeMethods]::BlockInput($false) | Out-Null
        }
        if ($hasOriginal) {
            [NativeMethods]::SetCursorPos($original.X, $original.Y) | Out-Null
        }
    }
}

function Resolve-SearchPanelScreenPoint {
    param(
        $Target,
        [object]$GridRect,
        [int]$LocalX,
        [int]$LocalY,
        [bool]$ScaleForWindowDpi = $false
    )

    $x = [int]$LocalX
    $y = [int]$LocalY
    if ($ScaleForWindowDpi -and $Target.MainWindowHandle -ne [IntPtr]::Zero) {
        try {
            $dpi = [double][NativeMethods]::GetDpiForWindow($Target.MainWindowHandle)
            if ($dpi -gt 0 -and [Math]::Abs($dpi - 96.0) -gt 0.1) {
                $scale = $dpi / 96.0
                $x = [int][Math]::Round($x * $scale)
                $y = [int][Math]::Round($y * $scale)
                Write-Log ("SEARCH_PANEL_COORDS_DPI dpi={0} scale={1:N3} local={2},{3} scaled={4},{5}" -f $dpi, $scale, $LocalX, $LocalY, $x, $y)
            }
        } catch {
            Write-Log ("SEARCH_PANEL_DPI_FAILED " + $_.Exception.Message)
        }
    }
    $windowRect = Get-KiCadWindowRect -WindowHandle $Target.MainWindowHandle
    $windowWidth = $windowRect.Right - $windowRect.Left
    $windowHeight = $windowRect.Bottom - $windowRect.Top
    if ($windowWidth -gt 0 -and $windowHeight -gt 0) {
        $gridLeft = [int]$GridRect.Left
        $gridTop = [int]$GridRect.Top
        $looksLocal = (
            $gridLeft -ge 0 -and $gridTop -ge 0 -and
            $gridLeft -lt $windowWidth -and $gridTop -lt $windowHeight -and
            ($gridLeft -lt $windowRect.Left -or $gridTop -lt $windowRect.Top)
        )
        if ($looksLocal) {
            $x += $windowRect.Left
            $y += $windowRect.Top
            Write-Log ("SEARCH_PANEL_COORDS_ADJUST local={0},{1} window={2},{3},{4},{5} screen={6},{7}" -f $LocalX, $LocalY, $windowRect.Left, $windowRect.Top, $windowRect.Right, $windowRect.Bottom, $x, $y)
        }
    }
    return [pscustomobject]@{ X = $x; Y = $y }
}

function Move-CursorToSchematicCanvas {
    param($Target)

    Ensure-NativeMethods
    $Target.Refresh()
    $windowHandle = $Target.MainWindowHandle
    if ($windowHandle -eq [IntPtr]::Zero) {
        Write-Log "CANVAS_CURSOR_SKIPPED no_window_handle"
        return
    }
    $rect = Get-KiCadWindowRect -WindowHandle $windowHandle
    $width = [Math]::Max(1, $rect.Right - $rect.Left)
    $height = [Math]::Max(1, $rect.Bottom - $rect.Top)
    $x = [int]($rect.Left + ($width * 0.62))
    $y = [int]($rect.Top + ($height * 0.42))
    [NativeMethods]::SetCursorPos($x, $y) | Out-Null
    Write-Log ("CANVAS_CURSOR_FAST x={0} y={1}" -f $x, $y)
}

function Set-SearchText {
    param([string]$Text)

    $oldClipboard = $null
    $hadClipboardText = $false
    try {
        $oldClipboard = Get-Clipboard -Raw -ErrorAction Stop
        $hadClipboardText = $true
    } catch {
        $hadClipboardText = $false
    }

    try {
        if (-not (Set-ClipboardTextWithRetry -Text $Text)) {
            throw "Clipboard is busy."
        }
        Start-Sleep -Milliseconds 120
        [System.Windows.Forms.SendKeys]::SendWait("^a")
        Start-Sleep -Milliseconds 80
        [System.Windows.Forms.SendKeys]::SendWait("^v")
        Start-Sleep -Milliseconds 160
    } finally {
        if ($hadClipboardText) {
            if (-not (Set-ClipboardTextWithRetry -Text $oldClipboard)) {
                Write-Log "CLIPBOARD_RESTORE_FAILED"
            }
        }
    }
}

function Set-EditControlText {
    param(
        [System.Windows.Automation.AutomationElement]$EditElement,
        [string]$Text
    )

    Ensure-NativeMethods
    $hwnd = [IntPtr]$EditElement.Current.NativeWindowHandle
    if ($hwnd -ne [IntPtr]::Zero) {
        [NativeMethods]::SendMessage($hwnd, 0x000C, [IntPtr]::Zero, $Text) | Out-Null
        for ($i = 0; $i -lt 6; $i++) {
            Start-Sleep -Milliseconds 25
            if ($EditElement.Current.Name -eq $Text) {
                Write-Log ("SEARCH_PANEL_VALUE method=WM_SETTEXT_FAST value={0}" -f $EditElement.Current.Name)
                return $true
            }
        }
        Write-Log ("SEARCH_PANEL_VALUE_WM_SETTEXT_FALLBACK current={0}" -f $EditElement.Current.Name)

        $oldClipboard = $null
        $hadClipboardText = $false
        try {
            $oldClipboard = Get-Clipboard -Raw -ErrorAction Stop
            $hadClipboardText = $true
        } catch {
            $hadClipboardText = $false
        }
        try {
            [NativeMethods]::SendMessage($hwnd, 0x000C, [IntPtr]::Zero, "") | Out-Null
            if (-not (Set-ClipboardTextWithRetry -Text $Text)) {
                throw "Clipboard is busy."
            }
            [NativeMethods]::SendMessage($hwnd, 0x0302, [IntPtr]::Zero, $null) | Out-Null
            for ($i = 0; $i -lt 8; $i++) {
                Start-Sleep -Milliseconds 25
                if ($EditElement.Current.Name -eq $Text) {
                    Write-Log ("SEARCH_PANEL_VALUE method=WM_PASTE_FAST value={0}" -f $EditElement.Current.Name)
                    return $true
                }
            }
            Write-Log ("SEARCH_PANEL_VALUE_PASTE_FALLBACK current={0}" -f $EditElement.Current.Name)
        } catch {
            Write-Log ("SEARCH_PANEL_VALUE_PASTE_FAILED " + $_.Exception.Message)
        } finally {
            if ($hadClipboardText) {
                if (-not (Set-ClipboardTextWithRetry -Text $oldClipboard)) {
                    Write-Log "CLIPBOARD_RESTORE_FAILED"
                }
            }
        }

        [NativeMethods]::SendMessage($hwnd, 0x000C, [IntPtr]::Zero, "") | Out-Null
        foreach ($ch in $Text.ToCharArray()) {
            [NativeMethods]::SendMessageInt($hwnd, 0x0102, [IntPtr]([int][char]$ch), [IntPtr]::Zero) | Out-Null
        }
        for ($i = 0; $i -lt 8; $i++) {
            Start-Sleep -Milliseconds 30
            if ($EditElement.Current.Name -eq $Text) {
                Write-Log ("SEARCH_PANEL_VALUE method=WM_CHAR value={0}" -f $EditElement.Current.Name)
                return $true
            }
        }
        Write-Log ("SEARCH_PANEL_VALUE_FALLBACK current={0}" -f $EditElement.Current.Name)
        return ($EditElement.Current.Name -eq $Text)
    }

    try {
        $valuePattern = $EditElement.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $valuePattern.SetValue($Text)
        Start-Sleep -Milliseconds 250
        Write-Log ("SEARCH_PANEL_VALUE method=ValuePattern value={0}" -f $valuePattern.Current.Value)
        return $true
    } catch {
        Write-Log ("SEARCH_PANEL_SETVALUE_FAILED " + $_.Exception.Message)
        Set-SearchText -Text $Text
        return $true
    }
}

function Set-EditWindowText {
    param(
        [System.IntPtr]$EditHwnd,
        [string]$Text
    )

    Ensure-NativeMethods
    if ($EditHwnd -eq [IntPtr]::Zero -or -not [NativeMethods]::IsWindow($EditHwnd)) {
        return $false
    }
    try {
        Ensure-UiAutomation
        $editElement = [System.Windows.Automation.AutomationElement]::FromHandle($EditHwnd)
        if ($null -ne $editElement) {
            Write-Log ("SEARCH_PANEL_FAST_FROM_HANDLE class={0} name={1}" -f $editElement.Current.ClassName, $editElement.Current.Name)
            return (Set-EditControlText -EditElement $editElement -Text $Text)
        }
    } catch {
        Write-Log ("SEARCH_PANEL_FAST_FROM_HANDLE_FAILED " + $_.Exception.Message)
    }

    [NativeMethods]::SendMessage($EditHwnd, 0x000C, [IntPtr]::Zero, "") | Out-Null
    foreach ($ch in $Text.ToCharArray()) {
        [NativeMethods]::SendMessageInt($EditHwnd, 0x0102, [IntPtr]([int][char]$ch), [IntPtr]::Zero) | Out-Null
    }
    for ($i = 0; $i -lt 6; $i++) {
        Start-Sleep -Milliseconds 20
        $current = Get-WindowTextFast -WindowHandle $EditHwnd
        if ($current -eq $Text) {
            Write-Log ("SEARCH_PANEL_VALUE method=WIN32_WM_CHAR value={0}" -f $current)
            return $true
        }
    }
    Write-Log ("SEARCH_PANEL_VALUE_WIN32_FALLBACK current={0}" -f (Get-WindowTextFast -WindowHandle $EditHwnd))
    return $false
}

function Invoke-KiCadFindApi {
    param([int]$ProcessId)

    $ipcScript = Join-Path $PSScriptRoot "kicad_ipc_action.py"
    if (-not (Test-Path -LiteralPath $KicadMcpPython)) {
        Write-Log "IPC_FIND skipped missing_python $KicadMcpPython"
        return $false
    }
    if (-not (Test-Path -LiteralPath $ipcScript)) {
        Write-Log "IPC_FIND skipped missing_script $ipcScript"
        return $false
    }

    $output = & $KicadMcpPython $ipcScript --pid $ProcessId --editor schematic --api find --cmd-type cpp_sdk_action 2>&1
    $exitCode = $LASTEXITCODE
    Write-Log ("IPC_FIND exit={0} output={1}" -f $exitCode, (($output | Out-String).Trim()))
    return ($exitCode -eq 0)
}

function Invoke-KiCadSearchPanelApi {
    param([int]$ProcessId)

    $ipcScript = Join-Path $PSScriptRoot "kicad_ipc_action.py"
    if (-not (Test-Path -LiteralPath $KicadMcpPython)) {
        Write-Log "IPC_SEARCH_PANEL skipped missing_python $KicadMcpPython"
        return $false
    }
    if (-not (Test-Path -LiteralPath $ipcScript)) {
        Write-Log "IPC_SEARCH_PANEL skipped missing_script $ipcScript"
        return $false
    }

    try {
        $output = & $KicadMcpPython $ipcScript --pid $ProcessId --editor schematic --api searchPanel --cmd-type cpp_sdk_action 2>&1
        $exitCode = $LASTEXITCODE
        Write-Log ("IPC_SEARCH_PANEL exit={0} output={1}" -f $exitCode, (($output | Out-String).Trim()))
        return ($exitCode -eq 0)
    } catch {
        Write-Log ("IPC_SEARCH_PANEL_UNAVAILABLE " + $_.Exception.Message)
        return $false
    }
}

function Get-ConfiguredSearchPanelVisible {
    $configPath = Join-Path $env:APPDATA "kicad\10.0\eeschema.json"
    if (-not (Test-Path -LiteralPath $configPath)) {
        return $false
    }
    try {
        $configText = Get-Content -Raw -LiteralPath $configPath
        return ($configText -match '"show_search"\s*:\s*true')
    } catch {
        Write-Log ("SEARCH_PANEL_CONFIG_FAILED " + $_.Exception.Message)
        return $false
    }
}

function Get-SearchPanelElements {
    param([int]$ProcessId)

    Ensure-UiAutomation
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $pidCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $ProcessId
    )
    $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCondition)
    $winCount = [int]$wins.Count
    for ($i = 0; $i -lt $winCount; $i++) {
        $win = $wins.Item($i)
        if (-not (Test-SchematicEditorWindowName -Name $win.Current.Name)) {
            continue
        }
        $editCondition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ClassNameProperty,
            "Edit"
        )
        $gridCondition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ClassNameProperty,
            "SysListView32"
        )
        $edits = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCondition)
        $grids = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $gridCondition)
        Write-Log ("SEARCH_PANEL_SCAN_COUNTS edits={0} grids={1}" -f ([int]$edits.Count), ([int]$grids.Count))
        $editCandidates = @()
        $resultGrid = $null
        $editCount = [int]$edits.Count
        for ($j = 0; $j -lt $editCount; $j++) {
            $item = $edits.Item($j)
            if ($item.Current.ClassName -eq "Edit") {
                $rect = $item.Current.BoundingRectangle
                if (-not $rect.IsEmpty -and $rect.Width -gt 200) {
                    $editCandidates += $item
                }
            }
        }
        $gridCount = [int]$grids.Count
        for ($j = 0; $j -lt $gridCount; $j++) {
            $item = $grids.Item($j)
            if ($item.Current.ClassName -eq "SysListView32") {
                $rect = $item.Current.BoundingRectangle
                if (-not $rect.IsEmpty -and $rect.Width -gt 200 -and $rect.Height -gt 80) {
                    $resultGrid = $item
                }
            }
        }
        $searchEdit = $null
        if ($null -ne $resultGrid) {
            $gridRect = $resultGrid.Current.BoundingRectangle
            Write-Log ("SEARCH_PANEL_GRID rect={0}" -f $gridRect)
            foreach ($candidate in $editCandidates) {
                $editRect = $candidate.Current.BoundingRectangle
                Write-Log ("SEARCH_PANEL_EDIT name={0} rect={1}" -f $candidate.Current.Name, $editRect)
                $isAboveGrid = $editRect.Bottom -le ($gridRect.Top - 10)
                $isHorizontallyAligned = $editRect.Left -ge ($gridRect.Left - 20) -and $editRect.Right -le ($gridRect.Right + 20)
                if ($isAboveGrid -and $isHorizontallyAligned) {
                    if ($null -eq $searchEdit -or $editRect.Width -gt $searchEdit.Current.BoundingRectangle.Width) {
                        $searchEdit = $candidate
                    }
                }
            }
        }
        if ($null -ne $searchEdit -and $null -ne $resultGrid) {
            return @{
                Edit = $searchEdit
                Grid = $resultGrid
            }
        }
    }

    return $null
}

function Close-FindDialogIfOpen {
    param([int]$ProcessId)

    Ensure-UiAutomation
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $pidCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $ProcessId
    )
    $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCondition)
    $winCount = [int]$wins.Count
    for ($i = 0; $i -lt $winCount; $i++) {
        $win = $wins.Item($i)
        if (-not (Test-SchematicEditorWindowName -Name $win.Current.Name)) {
            continue
        }
        $editCondition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ClassNameProperty,
            "Edit"
        )
        $edits = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCondition)
        $editCount = [int]$edits.Count
        for ($j = 0; $j -lt $editCount; $j++) {
            $edit = $edits.Item($j)
            $rect = $edit.Current.BoundingRectangle
            if (-not $rect.IsEmpty -and $rect.Width -lt 500 -and $rect.Top -lt 650) {
                Write-Log ("CLOSE_FIND_DIALOG edit={0}" -f $rect)
                try {
                    $edit.SetFocus()
                    Start-Sleep -Milliseconds 120
                    [System.Windows.Forms.SendKeys]::SendWait("{ESC}")
                    Start-Sleep -Milliseconds 350
                    return $true
                } catch {
                    Write-Log ("CLOSE_FIND_DIALOG_FAILED " + $_.Exception.Message)
                    return $false
                }
            }
        }
    }
    return $false
}

function Wait-SearchPanelElements {
    param(
        [int]$ProcessId,
        [int]$TimeoutMs
    )

    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    do {
        $elements = Get-SearchPanelElements -ProcessId $ProcessId
        if ($null -ne $elements) {
            return $elements
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)

    return $null
}

function Wait-SearchPanelElementsFast {
    param(
        $Target,
        [int]$TimeoutMs
    )

    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    do {
        $elements = Get-SearchPanelElementsFast -Target $Target
        if ($null -ne $elements) {
            return $elements
        }
        Start-Sleep -Milliseconds 50
    } while ((Get-Date) -lt $deadline)

    return $null
}

function Invoke-KiCadApi {
    param(
        [int]$ProcessId,
        [string]$ApiName,
        [string]$CmdType = "cpp_sdk_action",
        [string]$ParamsJson = "{}"
    )

    $ipcScript = Join-Path $PSScriptRoot "kicad_ipc_action.py"
    if (-not (Test-Path -LiteralPath $KicadMcpPython)) {
        Write-Log "IPC_API skipped missing_python $KicadMcpPython api=$ApiName"
        return $false
    }
    if (-not (Test-Path -LiteralPath $ipcScript)) {
        Write-Log "IPC_API skipped missing_script $ipcScript api=$ApiName"
        return $false
    }

    $output = & $KicadMcpPython $ipcScript --pid $ProcessId --editor schematic --api $ApiName --cmd-type $CmdType --params-json $ParamsJson 2>&1
    $exitCode = $LASTEXITCODE
    Write-Log ("IPC_API api={0} exit={1} output={2}" -f $ApiName, $exitCode, (($output | Out-String).Trim()))
    return ($exitCode -eq 0)
}

function Invoke-SearchPanelFirstResult {
    param(
        $Target,
        [string]$Ref
    )

    $elements = Wait-SearchPanelElementsFast -Target $Target -TimeoutMs 250
    if ($null -eq $elements) {
        Set-KiCadForeground -Target $Target
        if (-not (Invoke-KiCadSearchPanelApi -ProcessId $Target.Id)) {
            [System.Windows.Forms.SendKeys]::SendWait("^g")
            Write-Log 'SEARCH_PANEL_OPEN shortcut=Ctrl+G'
        }
        Start-Sleep -Milliseconds 160
        $elements = Wait-SearchPanelElementsFast -Target $Target -TimeoutMs 900
    }
    if ($null -eq $elements) {
        Write-Log "SEARCH_PANEL_ELEMENTS_NOT_FOUND_FAST"
        return $false
    }
    if ($elements.Mode -eq "Win32") {
        $gridRect = $elements.GridRect
        $inputOk = Set-EditWindowText -EditHwnd $elements.EditHwnd -Text $Ref
        if (-not $inputOk) {
            Write-Log "SEARCH_PANEL_WIN32_INPUT_FAILED"
            return $false
        }
    } else {
        $gridRect = $elements.Grid.Current.BoundingRectangle
        $inputOk = Set-EditControlText -EditElement $elements.Edit -Text $Ref
    }
    if (-not $inputOk) {
        return $false
    }
    if ($elements.Mode -eq "Win32") {
        [NativeMethods]::SetFocus($elements.EditHwnd) | Out-Null
    } else {
        try {
            $elements.Edit.SetFocus()
        } catch {
            Write-Log ("SEARCH_PANEL_FOCUS_WARN " + $_.Exception.Message)
        }
    }
    Start-Sleep -Milliseconds 60
    Set-SearchText -Text $Ref
    Start-Sleep -Milliseconds 260
    $localFirstResultX = [int]($gridRect.Left + 32)
    $localFirstResultY = [int]($gridRect.Top + 36)
    $firstPoint = Resolve-SearchPanelScreenPoint -Target $Target -GridRect $gridRect -LocalX $localFirstResultX -LocalY $localFirstResultY -ScaleForWindowDpi ($elements.Mode -eq "Win32" -and $elements.GridRectSource -ne "UIA")
    $firstResultX = [int]$firstPoint.X
    $firstResultY = [int]$firstPoint.Y

    Write-Log ("SEARCH_PANEL_COORDS grid={0} first={1},{2}" -f $gridRect, $firstResultX, $firstResultY)
    Start-Sleep -Milliseconds 420
    Invoke-ProtectedSearchResultClicks -X $firstResultX -Y $firstResultY -IntervalMs 460
    if ($MoveCursorAfterJump) {
        Start-Sleep -Milliseconds 40
        Move-CursorToSchematicCanvas -Target $Target
    }
    Write-Log "DONE ref=$Ref action=search_panel_first_result"
    return $true
}

$rootHasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $rootBytes = [System.Text.Encoding]::UTF8.GetBytes((Get-ProjectRoot).ToLowerInvariant())
    $rootKey = ([BitConverter]::ToString($rootHasher.ComputeHash($rootBytes))).Replace('-', '').Substring(0, 12)
} finally { $rootHasher.Dispose() }
$WindowCachePath = Join-Path $env:TEMP ("kicad_schematic_search_" + $rootKey + ".pid")
$searchMutex = New-Object System.Threading.Mutex($false, "DSMU_KiCadSchematicSearch")
$mutexAcquired = $false

try {
    $mutexAcquired = $searchMutex.WaitOne(15000)
    if (-not $mutexAcquired) {
        throw "Another KiCad schematic search is still running. Ref=$UriOrRef"
    }

    Write-Log "START raw=$UriOrRef hotkey=$SearchHotkey dryRun=$DryRun mode=$Mode"
    Add-Type -AssemblyName System.Web
    $ref = Get-DesignatorFromInput $UriOrRef
    $page = Get-PageFromInput $UriOrRef
    Write-Log "REF $ref page=$page"

    $cachedWindow = Get-CachedKiCadWindow
    if ($null -ne $cachedWindow -and -not $SaveActiveFrame) {
        $windows = @($cachedWindow)
    } else {
        $windows = @(Get-KiCadCandidateWindows)
        if ($windows.Count -gt 0) {
            Save-KiCadWindowCache -Window $windows[0]
        }
    }
    if ($SaveActiveFrame -and $windows.Count -gt 1) {
        throw "Multiple schematic windows belong to this project. Close extra windows manually before synchronization."
    }
    $pageWindows = @()
    if ($page) {
        $pageWindows = @($windows | Where-Object { Test-KiCadWindowMatchesPage -Window $_ -Page $page })
        if ($pageWindows.Count -gt 0) {
            $windows = $pageWindows
        }
    }
    if ($page -and $windows.Count -eq 0 -and -not $DryRun) {
        if ($SaveOnly) {
            Write-Log "DONE action=save_only no_schematic_window"
            exit 0
        }
        Write-Log "OPEN_PAGE no_schematic_window"
        Open-SchematicPage -Page $page
        $windows = @(Wait-KiCadWindow -Page "" -TimeoutMs 9000)
        if ($windows.Count -gt 0) {
            Save-KiCadWindowCache -Window $windows[0]
        }
    }
    Write-Log ("WINDOWS count={0}" -f $windows.Count)
    foreach ($window in $windows) {
        Write-Log ("WINDOW id={0} process={1} title={2}" -f $window.Id, $window.ProcessName, $window.MainWindowTitle)
    }

    if ($DryRun) {
        Write-Output "Ref=$ref"
        Write-Output "Page=$page"
        Write-Output ("Windows={0}" -f $windows.Count)
        foreach ($window in $windows) {
            Write-Output ("{0} {1} {2}" -f $window.Id, $window.ProcessName, $window.MainWindowTitle)
        }
        exit 0
    }

    if ($windows.Count -eq 0) {
        if ($SaveOnly) {
            Write-Log "DONE action=save_only no_schematic_window"
            exit 0
        }
        $message = "No KiCad schematic editor window found. Open the schematic page first, then click the web jump link again. Ref=$ref Page=$page"
        Show-UserMessage $message
        throw $message
    }

    Ensure-NativeMethods
    $target = $windows[0]
    Set-KiCadForeground -Target $target
    if ($EnablePageSwitch -and $Mode -eq "SearchPanel" -and $page -and -not (Test-KiCadWindowMatchesPage -Window $target -Page $page)) {
        Write-Log ("SWITCH_PAGE needed current={0} target_page={1}" -f $target.MainWindowTitle, $page)
        if (Switch-SchematicPageFromHierarchy -Target $target -Page $page) {
            Start-Sleep -Milliseconds 250
            $target.Refresh()
            Write-Log ("SWITCH_PAGE done title={0}" -f $target.MainWindowTitle)
        } else {
            Write-Log ("SWITCH_PAGE failed target_page={0}; continuing search in current page" -f $page)
        }
    }
    if ($Mode -ne "SearchPanel" -and -not (Wait-ForegroundProcess -ProcessId $target.Id -TimeoutMs 1800)) {
        Write-Log "FOREGROUND_WARN continuing_without_foreground"
    }

    Start-Sleep -Milliseconds $DelayMs
    Add-Type -AssemblyName System.Windows.Forms

    if ($NotifyOnly) {
        Write-Log "DONE action=notify_only"
        exit 0
    }

    if ($SaveActiveFrame) {
        Invoke-KiCadApi -ProcessId $target.Id -ApiName "saveFrame" | Out-Null
        Write-Log "SAVE_ACTIVE_FRAME requested"
        if ($CloseAfterSave) {
            $ipcCloseRequested = Invoke-KiCadApi -ProcessId $target.Id -ApiName "closeCurrentFrame"
            Write-Log ("CLOSE_CURRENT_FRAME requested ipc={0}" -f $ipcCloseRequested)
            $closed = Wait-KiCadEditorFrameClosed -Target $target -TimeoutMs 800
            if (-not $closed) {
                try {
                    $target.Refresh()
                    $windowCloseRequested = $target.CloseMainWindow()
                    Write-Log ("CLOSE_MAIN_WINDOW requested={0}" -f $windowCloseRequested)
                } catch {
                    Write-Log ("CLOSE_MAIN_WINDOW failed " + $_.Exception.Message)
                }
                $closed = Wait-KiCadEditorFrameClosed -Target $target -TimeoutMs 4000
            }
            if (-not $closed) {
                throw "KiCad schematic window did not close after save. Stop sync to avoid overwriting the new fields."
            }
            try {
                [System.IO.File]::Delete($WindowCachePath)
            } catch {
                Write-Log ("WINDOW_CACHE_DELETE_FAILED " + $_.Exception.Message)
            }
        }
        if ($SaveOnly) {
            Write-Log "DONE action=save_only"
            exit 0
        }
    }

    if ($ReloadFromDisk) {
        Write-Log "RELOAD_FROM_DISK skipped unsupported_revert_action"
        exit 0
    }

    if ($Mode -eq "SearchPanel") {
        if (Invoke-SearchPanelFirstResult -Target $target -Ref $ref) {
            exit 0
        }
        $message = "KiCad search panel was not available. Open the Search panel (Ctrl+G) in the schematic editor and click again. Ref=$ref"
        Show-UserMessage $message
        throw $message
    }

    Invoke-KiCadApi -ProcessId $target.Id -ApiName "unselectAll" | Out-Null

    if (-not (Wait-FocusedEditControl -TimeoutMs 200 -ProcessId $target.Id)) {
        $openedByIpc = $false
        if (-not $DisableIpcFind) {
            $openedByIpc = Invoke-KiCadFindApi -ProcessId $target.Id
        }
        if (-not $openedByIpc) {
            [System.Windows.Forms.SendKeys]::SendWait($SearchHotkey)
        } else {
            Set-KiCadForeground -Target $target
        }
    }
    Start-Sleep -Milliseconds 150

    if (-not (Wait-FocusedEditControl -TimeoutMs $SearchFocusTimeoutMs -ProcessId $target.Id)) {
        $message = "KiCad search box was not focused after hotkey $SearchHotkey. No paste was sent. Check KiCad search shortcut. Ref=$ref"
        Show-UserMessage $message
        throw $message
    }

    Set-SearchText -Text $ref
    Start-Sleep -Milliseconds 100
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
    Start-Sleep -Milliseconds 350
    for ($i = 0; $i -lt 2; $i++) {
        if (Wait-FocusedEditControl -TimeoutMs 150 -ProcessId $target.Id) {
            [System.Windows.Forms.SendKeys]::SendWait("{ESC}")
            Write-Log ("CLOSE_FIND esc_index={0}" -f $i)
            Start-Sleep -Milliseconds 180
        } else {
            break
        }
    }
    Invoke-KiCadApi -ProcessId $target.Id -ApiName "zoomFitObjects" | Out-Null
    Write-Log "DONE ref=$ref action=find_close_zoom"
} catch {
    Write-Log ("ERROR " + $_.Exception.Message)
    throw
} finally {
    if ($mutexAcquired) {
        $searchMutex.ReleaseMutex() | Out-Null
    }
    $searchMutex.Dispose()
}
