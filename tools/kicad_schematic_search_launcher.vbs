Option Explicit

Dim args, shell, fso, scriptPath, scriptDir, powerShellPath, uri, hotkey, command

Set args = WScript.Arguments
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

uri = ""
If args.Count >= 1 Then
    uri = args.Item(0)
End If

hotkey = "^+f"
If args.Count >= 2 Then
    hotkey = args.Item(1)
End If

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(scriptDir, "kicad_schematic_search.ps1")
powerShellPath = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"

command = Quote(powerShellPath) & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Quote(scriptPath) & " " & Quote(uri) & " -SearchHotkey " & Quote(hotkey)
shell.Run command, 0, False

Function Quote(value)
    Quote = Chr(34) & Replace(CStr(value), Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
