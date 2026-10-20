"""Navigation policies without touching any user editor or cursor."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/kicad_schematic_search.ps1'


@pytest.mark.skipif(os.name != 'nt' or not shutil.which('powershell'), reason='Windows PowerShell required')
def test_activation_preserves_window_placement_and_cursor_policy():
    code = r'''
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($env:NAV_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Set-KiCadForeground','Invoke-ProtectedSearchResultClicks')) {
 $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 . ([scriptblock]::Create($node.Extent.Text))
}
Add-Type @'
using System;
public struct POINT { public int X,Y; }
public class NativeMethods {
 public static bool Minimized=false; public static int Restores=0,CursorRestores=0;
 public static IntPtr Foreground=IntPtr.Zero;
 public static IntPtr GetForegroundWindow(){return Foreground;}
 public static bool IsIconic(IntPtr h){return Minimized;}
 public static bool ShowWindowAsync(IntPtr h,int cmd){Restores++;return true;}
 public static bool SetWindowPos(IntPtr h,IntPtr z,int x,int y,int w,int height,int flags){
  if ((flags & 3)!=3) throw new Exception("Window position/size changed");return true;}
 public static bool SetForegroundWindow(IntPtr h){return true;}
 public static uint GetWindowThreadProcessId(IntPtr h,out uint p){p=1;return 0;}
 public static uint GetCurrentThreadId(){return 1;}
 public static bool GetCursorPos(out POINT p){p=new POINT();p.X=100;p.Y=200;return true;}
 public static bool SetCursorPos(int x,int y){CursorRestores++;return true;}
 public static bool BlockInput(bool block){return true;}
}
'@
function Ensure-NativeMethods {}
function Write-Log {param($Message)}
function Start-Sleep {}
function New-Object {param($ComObject) return [pscustomobject]@{} | Add-Member -MemberType ScriptMethod -Name AppActivate -Value {param($title) return $true} -PassThru}
$target=[pscustomobject]@{Id=1;MainWindowHandle=[IntPtr]123;MainWindowTitle='test'}
Set-KiCadForeground $target
if ([NativeMethods]::Restores -ne 0) {throw 'Visible normal/maximized window must not be restored'}
[NativeMethods]::Minimized=$true
Set-KiCadForeground $target
if ([NativeMethods]::Restores -ne 1) {throw 'Minimized window must be restored once'}
[NativeMethods]::Foreground=[IntPtr]123
Set-KiCadForeground $target
if ([NativeMethods]::Restores -ne 1) {throw 'Foreground activation should do nothing'}
Remove-Item Function:New-Object
function Invoke-MouseClick {param($X,$Y,$Count)}
$MoveCursorAfterJump=$false
Invoke-ProtectedSearchResultClicks -X 1 -Y 2
if ([NativeMethods]::CursorRestores -ne 1) {throw 'Default must restore original cursor'}
$MoveCursorAfterJump=$true
Invoke-ProtectedSearchResultClicks -X 1 -Y 2
if ([NativeMethods]::CursorRestores -ne 1) {throw 'Stay mode must not restore cursor'}
Write-Output 'passed'
'''
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', code],
        env={**os.environ, 'NAV_SCRIPT':str(SCRIPT)}, capture_output=True, text=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'passed'
