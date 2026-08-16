@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update_from_git.ps1"
set "UPDATE_EXIT=%ERRORLEVEL%"

if "%UPDATE_EXIT%"=="0" (
  echo [INFO] Git update check completed.
  echo [HINT] Run start.bat to apply any dependency changes and start Tagger2.
) else (
  echo [ERROR] Git update was not applied.
)

if not defined TAGGER2_NO_PAUSE pause
exit /b %UPDATE_EXIT%
