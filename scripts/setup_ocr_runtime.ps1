<#
.SYNOPSIS
  Create the isolated PaddleOCR runtime used by the Dataset Workflow OCR stage.

.DESCRIPTION
  PaddleOCR pulls in a numpy/opencv stack that conflicts with the main runtime,
  so the OCR stage shells out to a separate interpreter. This script builds that
  interpreter at .untime_ocr and installs pinned versions there.

  The OCR stage discovers runtime_ocr\Scripts\python.exe automatically. If this
  runtime is absent, OCR reports "ocr_unavailable" as a non-blocking warning and
  the rest of the pipeline still runs.

.PARAMETER BasePython
  Interpreter used to create the venv. Defaults to the "python" on PATH, which
  must be 3.12 or newer.

.EXAMPLE
  .\scripts\setup_ocr_runtime.ps1
#>

[CmdletBinding()]
param(
    [string]$BasePython = "python"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot "runtime_ocr"
$venvPython = Join-Path $runtimeDir "Scripts\python.exe"

# Pinned so an OCR upgrade is a deliberate change, not an accident of install date.
$packages = @(
    "setuptools==84.0.0",
    "paddlepaddle==3.0.0",
    "paddleocr==2.9.1"
)

Write-Host "Project root: $projectRoot"

if (Test-Path -LiteralPath $venvPython) {
    Write-Host "OCR runtime already exists at $runtimeDir"
    Write-Host "Delete that directory yourself if you want a clean rebuild."
} else {
    Write-Host "Creating OCR runtime at $runtimeDir ..."
    & $BasePython -m venv $runtimeDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the venv. Is $BasePython a Python 3.12+ interpreter?"
    }
}

Write-Host "Upgrading pip ..."
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }

foreach ($package in $packages) {
    Write-Host "Installing $package ..."
    & $venvPython -m pip install $package
    if ($LASTEXITCODE -ne 0) { throw "Failed to install $package" }
}

Write-Host "Verifying the runtime can import paddleocr ..."
& $venvPython -c "import paddleocr; print('paddleocr', paddleocr.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "paddleocr is installed but cannot be imported; OCR will report ocr_unavailable"
}

Write-Host "Probing the provisioned model cache ..."
$probe = Join-Path $projectRoot "scripts\probe_ocr_runtime.py"
& $venvPython $probe --output (Join-Path $runtimeDir "ocr-runtime.manifest.json")
if ($LASTEXITCODE -ne 0) {
    throw "PaddleOCR imports but its model cache is missing or incomplete"
}

Write-Host ""
Write-Host "OCR runtime ready: $venvPython"
Write-Host "Enable OCR per job in the Dataset Workflow page; it stays off by default."
