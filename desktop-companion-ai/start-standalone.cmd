@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
if exist "portable.json" if not exist "runtime\python.exe" (
  echo Portable Python is missing. Extract the original ZIP again.
  if not defined COMPANION_NO_PAUSE pause
  exit /b 1
)
if exist "runtime\python.exe" (
  "runtime\python.exe" -I -B -X utf8 tools\start_standalone.py %*
) else if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\start_standalone.py %*
) else if exist "..\neko-lab\upstream\.venv\Scripts\python.exe" (
  "..\neko-lab\upstream\.venv\Scripts\python.exe" tools\start_standalone.py %*
) else (
  python tools\start_standalone.py %*
)
set "companion_exit=%errorlevel%"
if not "%companion_exit%"=="0" if not defined COMPANION_NO_PAUSE pause
exit /b %companion_exit%
