@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows-start.ps1" %*
set "H3_EXIT_CODE=%ERRORLEVEL%"
if not "%H3_EXIT_CODE%"=="0" pause
exit /b %H3_EXIT_CODE%
