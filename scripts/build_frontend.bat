@echo off
setlocal
cd /d %~dp0..
where npm.cmd >nul 2>nul
if errorlevel 1 (echo [ERROR] Node/npm is required to build the frontend.& exit /b 1)
if not exist frontend\package-lock.json (
  echo [ERROR] frontend\package-lock.json is missing.
  exit /b 1
)
call npm.cmd --prefix frontend ci --no-audit --no-fund
if errorlevel 1 exit /b 1
call npm.cmd --prefix frontend run build
exit /b %errorlevel%
