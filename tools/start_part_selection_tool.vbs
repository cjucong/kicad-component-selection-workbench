Option Explicit
Dim shell, fso, command, argument, batch, interpreter
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
batch = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "start_part_selection_tool.bat")
interpreter = shell.ExpandEnvironmentStrings("%ComSpec%")
command = "call " & Quote(batch) & " --hidden"
For Each argument In WScript.Arguments
    command = command & " " & Quote(argument)
Next
shell.Run Quote(interpreter) & " /d /s /c " & Chr(34) & command & Chr(34), 0, False

Function Quote(value)
    Quote = Chr(34) & Replace(CStr(value), Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
