@echo off
call "%~dp0start-standalone.cmd" --simulation %*
exit /b %errorlevel%
