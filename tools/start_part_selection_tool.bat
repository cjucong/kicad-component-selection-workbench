@echo off
setlocal

if /i "%~1"=="--find-python" goto find_python_diagnostic

rem The visible launcher only creates a hidden worker and exits immediately.
if /i not "%~1"=="--hidden" (
    wscript.exe //B "%~dp0start_part_selection_tool.vbs" %*
    exit /b 0
)

cd /d "%~dp0"
set "LOG=%TEMP%\part_selection_tool_startup.log"
>"%LOG%" echo [%date% %time%] Starting part-selection tool

call :find_python
if not defined PYTHON (
    >>"%LOG%" echo [%date% %time%] No usable Python 3.10 or newer interpreter was found.
    >>"%LOG%" echo [%date% %time%] Set PART_SELECTION_PYTHON to a valid python.exe path and retry.
    exit /b 1
)

for %%P in ("%PYTHON%") do set "PYTHONW=%%~dpPpythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=%PYTHON%"

>>"%LOG%" echo [%date% %time%] Python: %PYTHON%
>>"%LOG%" echo [%date% %time%] PythonW: %PYTHONW%

"%PYTHON%" -B -c "import openpyxl" >>"%LOG%" 2>&1
if errorlevel 1 (
    "%PYTHON%" -m pip install -r requirements.txt >>"%LOG%" 2>&1
    if errorlevel 1 exit /b 1
)
"%PYTHONW%" -B launch_part_selection.py %* >>"%LOG%" 2>&1
exit /b %errorlevel%

:find_python
set "PYTHON="

if defined PART_SELECTION_PYTHON (
    call :validate_python "%PART_SELECTION_PYTHON%"
    if defined PYTHON exit /b 0
)

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    call :validate_python "%%~fD\python.exe"
    if defined PYTHON exit /b 0
)

rem Normalize quoted PATH entries before validating their python.exe files.
set "SEARCH_PATH=%PATH:"=%"
for %%D in ("%SEARCH_PATH:;=" "%") do (
    call :validate_python "%%~D\python.exe"
    if defined PYTHON exit /b 0
    call :validate_python "%%~D\..\python.exe"
    if defined PYTHON exit /b 0
)

rem Conda is often installed at a drive root without adding python.exe to PATH.
for %%V in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    call :validate_python "%%V:\anaconda\python.exe"
    if defined PYTHON exit /b 0
    call :validate_python "%%V:\Anaconda3\python.exe"
    if defined PYTHON exit /b 0
    call :validate_python "%%V:\Miniconda3\python.exe"
    if defined PYTHON exit /b 0
)

for %%P in (
    "%USERPROFILE%\anaconda3\python.exe"
    "%USERPROFILE%\miniconda3\python.exe"
    "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    "%ProgramData%\anaconda3\python.exe"
    "%ProgramData%\miniconda3\python.exe"
) do (
    call :validate_python "%%~P"
    if defined PYTHON exit /b 0
)

exit /b 1

:validate_python
if not exist "%~1" exit /b 1
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info.__ge__((3, 10)) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYTHON=%~f1"
exit /b 0

:find_python_diagnostic
cd /d "%~dp0"
call :find_python
if not defined PYTHON exit /b 1
echo %PYTHON%
exit /b 0
