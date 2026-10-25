@echo off
setlocal
cd /d "%~dp0"
"..\neko-lab\upstream\.venv\Scripts\python.exe" tools\start_neko.py %*
set "companion_exit=%errorlevel%"
if not defined COMPANION_NO_PAUSE pause
exit /b %companion_exit%
