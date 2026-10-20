param(
    [string]$ProtocolName = "kicad-sch-search",

    # Project shortcut rule: Ctrl+Shift+F opens schematic/PCB Find/Search.
    [string]$SearchHotkey = "^+f"
)

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "kicad_schematic_search.ps1"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Missing script: $scriptPath"
}
$launcherPath = Join-Path $PSScriptRoot "kicad_schematic_search_launcher.vbs"
if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "Missing launcher: $launcherPath"
}

$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
$command = "`"$wscript`" //B //Nologo `"$launcherPath`" `"%1`" `"$SearchHotkey`""
$base = "HKCU:\Software\Classes\$ProtocolName"

New-Item -Path $base -Force | Out-Null
New-ItemProperty -Path $base -Name "(default)" -Value "URL:KiCad Schematic Search" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $base -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null
New-Item -Path "$base\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path "$base\shell\open\command" -Name "(default)" -Value $command

Write-Host "Installed protocol: $ProtocolName"
Write-Host "Hotkey: $SearchHotkey"
Write-Host "Command: $command"
Write-Host "Test link: ${ProtocolName}://R1169"
Write-Host "Log: $env:TEMP\kicad_schematic_search.log"
Write-Host "Manual debug with popup: powershell -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" `"${ProtocolName}://R1169`" -ShowErrors"
