@echo off
call "%~dp0start-standalone.cmd" --desktop %*
exit /b %errorlevel%
