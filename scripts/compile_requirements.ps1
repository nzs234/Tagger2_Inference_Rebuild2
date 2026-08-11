param(
  [ValidateSet("all", "cpu", "gpu", "dev")]
  [string]$Target = "all"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$compile = Join-Path $root ".venv\Scripts\pip-compile.exe"

if (-not (Test-Path -LiteralPath $python)) {
  throw "Create the Python 3.12 virtual environment before compiling locks."
}

& $python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Locks must be generated with Python 3.12." }

& $python -c "import importlib.metadata; raise SystemExit(0 if importlib.metadata.version('pip-tools') == '7.6.0' else 1)" 2>$null
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $compile)) {
  & $python -m pip install "pip-tools==7.6.0"
  if ($LASTEXITCODE -ne 0) { throw "Unable to install pip-tools 7.6.0." }
}

function Invoke-LockCompile([string]$Output, [string[]]$Sources) {
  $arguments = @(
    "--resolver=backtracking",
    "--generate-hashes",
    "--allow-unsafe",
    "--strip-extras",
    "--quiet",
    "--newline=lf",
    "--output-file=$Output"
  ) + $Sources
  Push-Location $root
  try {
    & $compile @arguments
    if ($LASTEXITCODE -ne 0) { throw "pip-compile failed for $Output." }
    & $python scripts\verify_lock.py --lock $Output
    if ($LASTEXITCODE -ne 0) { throw "Generated lock validation failed for $Output." }
  } finally {
    Pop-Location
  }
}

if ($Target -in @("all", "cpu")) {
  Invoke-LockCompile "requirements-cpu.lock" @("requirements.txt", "requirements-cpu.txt")
}
if ($Target -in @("all", "gpu")) {
  Invoke-LockCompile "requirements-gpu.lock" @("requirements.txt", "requirements-gpu.txt")
}
if ($Target -in @("all", "dev")) {
  Invoke-LockCompile "requirements-dev.lock" @("requirements-dev.txt")
}
