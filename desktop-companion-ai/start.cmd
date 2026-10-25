@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\start_plan.py %*
) else if exist "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe" (
  "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe" tools\start_plan.py %*
) else (
  python tools\start_plan.py %*
)
set "companion_exit=%errorlevel%"
if not defined COMPANION_NO_PAUSE pause
exit /b %companion_exit%
