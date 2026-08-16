@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "ROOT=%~dp0"
set "RUNTIME=%ROOT%runtime"
set "PYTHON=%RUNTIME%\python.exe"
set "PYVER=3.12.10"
set "PYZIP=%TEMP%\tagger2-python-%PYVER%.zip"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip"

if exist "%PYTHON%" goto ready
echo [INFO] Downloading portable Python %PYVER%...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYZIP%'"
if errorlevel 1 goto failed
if not exist "%RUNTIME%" mkdir "%RUNTIME%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%PYZIP%' -DestinationPath '%RUNTIME%' -Force"
if errorlevel 1 goto failed
del /q "%PYZIP%" >nul 2>nul

:ready
if exist "%RUNTIME%\python312._pth" powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p='%RUNTIME%\python312._pth'; $s=Get-Content -Raw -LiteralPath $p; $s=$s -replace '#import site','import site'; if ($s -notmatch 'Lib\\site-packages') { $s += [Environment]::NewLine + 'Lib\\site-packages' + [Environment]::NewLine }; Set-Content -LiteralPath $p -Value $s -Encoding ascii"
"%PYTHON%" -m pip --version >nul 2>nul
if not errorlevel 1 goto pip_ready
echo [INFO] Preparing pip...
if not exist "%RUNTIME%\get-pip.py" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%RUNTIME%\get-pip.py'"
  if errorlevel 1 goto failed
)
"%PYTHON%" "%RUNTIME%\get-pip.py" --disable-pip-version-check "pip==26.1.2"
if errorlevel 1 goto failed

:pip_ready
echo [INFO] Portable Python is ready.
if defined TAGGER2_SETUP_ONLY exit /b 0
call "%ROOT%start.bat"
exit /b %ERRORLEVEL%

:failed
echo [ERROR] Portable Python setup failed. Check network access and try again.
pause
exit /b 1
