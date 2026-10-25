@echo off
setlocal
cd /d "%~dp0.."
if errorlevel 1 exit /b 1
if not exist "portable.json" goto development
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "tools\setup_windows.ps1"
set "companion_exit=%errorlevel%"
if not "%companion_exit%"=="0" goto finished
"runtime\python.exe" -I -B -X utf8 "tools\first_run.py" %*
set "companion_exit=%errorlevel%"
goto finished
:development
rem Only a source checkout may skip distribution installation/integrity checks.
if not exist "tools\build_portable.py" (
  echo Distribution manifest missing. Extract the original ZIP again.
  set "companion_exit=1"
  goto finished
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -B -X utf8 "tools\first_run.py" --development %*
) else if exist "..\neko-lab\upstream\.venv\Scripts\python.exe" (
  "..\neko-lab\upstream\.venv\Scripts\python.exe" -B -X utf8 "tools\first_run.py" --development %*
) else (
  python -B -X utf8 "tools\first_run.py" --development %*
)
set "companion_exit=%errorlevel%"
:finished
if not "%companion_exit%"=="0" if not defined COMPANION_NO_PAUSE pause
exit /b %companion_exit%
