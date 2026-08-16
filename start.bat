@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
if exist "%PROJECT_DIR%runtime\python.exe" (
  set "PYTHON=%PROJECT_DIR%runtime\python.exe"
  set "VENV_DIR=%PROJECT_DIR%runtime"
)
set "PYTHONUTF8=1"
set "PYTHONPATH=%PROJECT_DIR%backend"
set "TAGGER2_PROJECT_ROOT=%PROJECT_DIR%"

if not exist "%PYTHON%" (
  echo [INFO] Creating an isolated Python environment...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3.12 -m venv "%VENV_DIR%"
    if errorlevel 1 py -3 -m venv "%VENV_DIR%"
  ) else (
    python -m venv "%VENV_DIR%"
  )
  if errorlevel 1 (
    echo [ERROR] Python 3.12 or newer is required.
    if not defined TAGGER2_NO_PAUSE pause
    exit /b 1
  )
)
"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] The project virtual environment must use Python 3.12 or newer.
  echo [HINT] Remove .venv and run start.bat again after installing Python 3.12.
  if not defined TAGGER2_NO_PAUSE pause
  exit /b 1
)

rem Embedded Python uses an isolated _pth file and ignores PYTHONPATH.
rem Put this project's backend ahead of any stale source path from another copy.
if exist "%PROJECT_DIR%runtime\python312._pth" (
  "%PYTHON%" "%PROJECT_DIR%scripts\ensure_runtime_backend_path.py" --pth "%PROJECT_DIR%runtime\python312._pth" --backend "%PROJECT_DIR%backend"
  if errorlevel 1 (
    echo [ERROR] Could not prepare the embedded Python source path.
    if not defined TAGGER2_NO_PAUSE pause
    exit /b 1
  )
)

set "RUNTIME_VARIANT=%TAGGER2_TORCH_VARIANT%"
if not defined RUNTIME_VARIANT (
  where nvidia-smi >nul 2>nul
  if errorlevel 1 (set "RUNTIME_VARIANT=cpu") else (set "RUNTIME_VARIANT=cuda")
)
if /I not "!RUNTIME_VARIANT!"=="cuda" if /I not "!RUNTIME_VARIANT!"=="cpu" (
  echo [ERROR] TAGGER2_TORCH_VARIANT must be either cuda or cpu.
  if not defined TAGGER2_NO_PAUSE pause
  exit /b 1
)
if /I "!RUNTIME_VARIANT!"=="CUDA" set "RUNTIME_VARIANT=cuda"
if /I "!RUNTIME_VARIANT!"=="CPU" set "RUNTIME_VARIANT=cpu"

if /I "!RUNTIME_VARIANT!"=="cuda" (
  set "RUNTIME_LOCK=%PROJECT_DIR%requirements-gpu.lock"
) else (
  set "RUNTIME_LOCK=%PROJECT_DIR%requirements-cpu.lock"
)
set "RUNTIME_MARKER=%VENV_DIR%\.tagger2-runtime-v2-!RUNTIME_VARIANT!"
if not exist "!RUNTIME_LOCK!" (
  echo [ERROR] Missing dependency lock: !RUNTIME_LOCK!
  if not defined TAGGER2_NO_PAUSE pause
  exit /b 1
)

if not exist "!RUNTIME_MARKER!" goto install_runtime
"%PYTHON%" -c "import hashlib,pathlib,sys; lock=pathlib.Path(sys.argv[1]); marker=pathlib.Path(sys.argv[2]); expected=hashlib.sha256(lock.read_bytes()).hexdigest(); actual=marker.read_text(encoding='ascii').strip(); raise SystemExit(0 if actual == expected else 1)" "!RUNTIME_LOCK!" "!RUNTIME_MARKER!" >nul 2>nul
if errorlevel 1 goto install_runtime
"%PYTHON%" "%PROJECT_DIR%scripts\verify_lock.py" --lock "!RUNTIME_LOCK!" --check-installed --module fastapi --module httpx --module onnxruntime --module timm --module torch --module torchvision --module transformers --module peft --module lycoris
if errorlevel 1 goto install_runtime
goto runtime_ready

:install_runtime
echo [INFO] Installing the locked !RUNTIME_VARIANT! runtime...
"%PYTHON%" -m pip --version >nul 2>nul
if not errorlevel 1 goto pip_ready
set "GET_PIP=%PROJECT_DIR%runtime\get-pip.py"
if not exist "!GET_PIP!" (
  echo [ERROR] pip is unavailable and runtime\get-pip.py is missing.
  echo [HINT] Run setup.bat once, then retry start.bat.
  goto install_failed
)
echo [INFO] Preparing pip...
"%PYTHON%" "!GET_PIP!" --disable-pip-version-check "pip==26.1.2"
if errorlevel 1 goto install_failed

:pip_ready
"%PYTHON%" -m pip install --upgrade "pip==26.1.2"
if errorlevel 1 goto install_failed

if /I "!RUNTIME_VARIANT!"=="cuda" (
  "%PYTHON%" -m pip uninstall -y onnxruntime >nul 2>nul
) else (
  "%PYTHON%" -m pip uninstall -y onnxruntime-gpu >nul 2>nul
)
"%PYTHON%" -m pip install --upgrade --require-hashes --only-binary=:all: -r "!RUNTIME_LOCK!"
if errorlevel 1 goto install_failed
"%PYTHON%" -m pip check
if errorlevel 1 goto install_failed
"%PYTHON%" "%PROJECT_DIR%scripts\verify_lock.py" --lock "!RUNTIME_LOCK!" --check-installed --module fastapi --module httpx --module onnxruntime --module timm --module torch --module torchvision --module transformers --module peft --module lycoris
if errorlevel 1 goto install_failed

del /q "%VENV_DIR%\.tagger2-runtime-v1-*" "%VENV_DIR%\.tagger2-runtime-v2-*" >nul 2>nul
"%PYTHON%" -c "import hashlib,pathlib,sys; lock=pathlib.Path(sys.argv[1]); marker=pathlib.Path(sys.argv[2]); marker.write_text(hashlib.sha256(lock.read_bytes()).hexdigest(), encoding='ascii')" "!RUNTIME_LOCK!" "!RUNTIME_MARKER!"
if errorlevel 1 goto install_failed

:runtime_ready
if not exist "%PROJECT_DIR%frontend\dist\index.html" (
  echo [ERROR] frontend\dist\index.html is missing.
  echo [HINT] Run scripts\build_frontend.bat on a development machine first.
  if not defined TAGGER2_NO_PAUSE pause
  exit /b 1
)

echo [INFO] Starting Tagger2 Inference on http://127.0.0.1:20000 ...
"%PYTHON%" -m tagger2.main
set "SERVER_EXIT=!ERRORLEVEL!"
if not "!SERVER_EXIT!"=="0" echo [ERROR] Server exited with code !SERVER_EXIT!.
if not defined TAGGER2_NO_PAUSE pause
exit /b !SERVER_EXIT!

:install_failed
echo [ERROR] Dependency installation failed.
echo [HINT] Set TAGGER2_TORCH_VARIANT=cpu to force a CPU-only installation.
if not defined TAGGER2_NO_PAUSE pause
exit /b 1
