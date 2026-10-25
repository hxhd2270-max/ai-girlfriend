@echo off
call "%~dp0start-standalone.cmd" --desktop --simulation %*
exit /b %errorlevel%
