param(
  [string]$OutputDir = "dist",
  [string]$Version = "",
  [switch]$SkipRuntime,
  [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$safeVersion = if ([string]::IsNullOrWhiteSpace($Version)) {
  ""
} else {
  ($Version.Trim() -replace "[^A-Za-z0-9._-]", "-")
}
$name = if ($safeVersion) {
  "Tagger2_Inference_Rebuild_${safeVersion}_$stamp"
} else {
  "Tagger2_Inference_Rebuild_$stamp"
}
$stage = Join-Path $root (".release_$stamp")
$out = Join-Path $root $OutputDir
$smoke = Join-Path $root (".release_smoke_$stamp")

function Copy-ReleaseItem([string]$RelativePath) {
  $source = Join-Path $root $RelativePath
  if (-not (Test-Path -LiteralPath $source)) {
    throw "Required release item is missing: $RelativePath"
  }
  $destination = Join-Path $stage $RelativePath
  if ((Get-Item -LiteralPath $source).PSIsContainer) {
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Copy-Item -Path (Join-Path $source "*") -Destination $destination -Recurse -Force
  } else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
  }
}

try {
  New-Item -ItemType Directory -Force -Path $stage, $out | Out-Null
  $versionFile = Join-Path $stage "VERSION.txt"
  $releaseVersion = if ($safeVersion) { $safeVersion } else { "development" }
  @(
    "Tagger2 Inference",
    "Version: $releaseVersion",
    "Build timestamp: $stamp"
  ) | Set-Content -LiteralPath $versionFile -Encoding utf8

  # Models and caches are provisioned separately because they are large.  The
  # placeholders keep the expected directory layout without copying secrets.
  foreach ($item in @(
    "backend",
    "frontend/dist",
    "frontend/e2e",
    "frontend/src",
    "frontend/tests",
    "frontend/index.html",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/playwright.config.ts",
    "frontend/eslint.config.js",
    "frontend/tsconfig.json",
    "frontend/tsconfig.app.json",
    "frontend/tsconfig.node.json",
    "frontend/vite.config.ts",
    "frontend/vitest.config.ts",
    "scripts",
    "config/app.toml",
    "config/app.example.toml",
    "pyproject.toml",
    "requirements.txt",
    "requirements-cpu.txt",
    "requirements-cpu.lock",
    "requirements-gpu.txt",
    "requirements-gpu.lock",
    "requirements-dev.txt",
    "requirements-dev.lock",
    ".gitignore",
    "start.bat",
    "setup.bat",
    "USER_GUIDE_zh-CN.txt",
    "README.md",
    "docs/release_package_contents.md",
    "docs/workflow_manual_paths.md",
    "docs/workflow_compatibility_report.md"
  )) {
    Copy-ReleaseItem $item
  }
  if ((-not $SkipRuntime) -and (Test-Path -LiteralPath (Join-Path $root "runtime/python.exe"))) {
    Copy-ReleaseItem "runtime"
  }
  foreach ($directory in @("models", "data_cache", "data")) {
    $placeholder = Join-Path $stage "$directory/.gitkeep"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $placeholder) | Out-Null
    New-Item -ItemType File -Force -Path $placeholder | Out-Null
  }

  # Ship the immutable, non-model workflow resources that make the default
  # e621 profile usable out of the box.  Models and the OCR runtime/model
  # cache stay external: the latter contains machine-specific absolute paths
  # and is intentionally provisioned separately.
  $resourceRoot = Join-Path $root "data\workflows\resources"
  $resourceCategories = @("classify", "replacement_index", "tokenizer")
  foreach ($category in $resourceCategories) {
    $sourceCategory = Join-Path $resourceRoot $category
    if (-not (Test-Path -LiteralPath $sourceCategory -PathType Container)) {
      throw "Required release resource category is missing: data/workflows/resources/$category"
    }
    $resourceFiles = Get-ChildItem -LiteralPath $sourceCategory -File |
      Where-Object { $_.Extension -ne ".csv" }
    if (-not $resourceFiles) {
      throw "Required release resource category is empty: data/workflows/resources/$category"
    }
    $destinationCategory = Join-Path $stage "data\workflows\resources\$category"
    New-Item -ItemType Directory -Force -Path $destinationCategory | Out-Null
    $resourceFiles | Copy-Item -Destination $destinationCategory -Force
  }

  # Keep the embedded interpreter portable. The startup script will later
  # replace this relative entry with the actual extracted package path.
  $stagePython = Join-Path $stage "runtime\python.exe"
  $stagePth = Join-Path $stage "runtime\python312._pth"
  $stageBackend = Join-Path $stage "backend"
  if ((Test-Path -LiteralPath $stagePython) -and (Test-Path -LiteralPath $stagePth)) {
    & $stagePython (Join-Path $stage "scripts\ensure_runtime_backend_path.py") `
      --pth $stagePth --backend $stageBackend
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the portable Python runtime" }
    $portablePth = (Get-Content -LiteralPath $stagePth -Raw).Replace($stageBackend, "..\backend")
    Set-Content -LiteralPath $stagePth -Value $portablePth -Encoding ascii
  }

  # Never ship interpreter caches, runtime databases, or credential files even
  # if a future source tree accidentally contains them.
  Get-ChildItem -LiteralPath $stage -Directory -Recurse -Force |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv") } |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
  Get-ChildItem -LiteralPath $stage -File -Recurse -Force |
    Where-Object {
      $_.Name -in @(
        ".env", ".env.local", ".env.production", "token", "stored_tokens",
        "token.json", "credentials.json", "credentials.toml", "secrets.json",
        "secrets.toml", "secrets.yaml", "secrets.yml"
      ) -or
      $_.Name -match '^(credentials|secret|secrets)\.(ini|cfg|conf|txt)$' -or
      $_.FullName -match '(\\|/)(huggingface|\.cache)(\\|/)(token|stored_tokens)(\\|/|$)'
    } |
    Remove-Item -Force

  $credentialPattern = @(
    '(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{30,}',
    '(?<![A-Za-z0-9])sk-(?:proj-)?[0-9A-Za-z_-]{20,}',
    '(?<![A-Za-z0-9])hf_[0-9A-Za-z]{20,}',
    '(?<![A-Za-z0-9])gh[pousr]_[0-9A-Za-z]{20,}',
    '-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'
  ) -join '|'
  # Bundled third-party package sources include intentionally token-shaped test
  # fixtures. Scan every project-controlled release file, but not site-packages.
  $credentialFiles = Get-ChildItem -LiteralPath $stage -File -Recurse -Force |
    Where-Object { $_.FullName -notlike (Join-Path $stage "runtime\Lib\site-packages\*") }
  $credentialHits = $credentialFiles |
    Select-String -Pattern $credentialPattern -ErrorAction SilentlyContinue
  if ($credentialHits) {
    $files = $credentialHits | Select-Object -ExpandProperty Path -Unique
    throw "Potential credential material found in release staging: $($files -join ', ')"
  }

  $zip = Join-Path $out "$name.zip"
  if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
  Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal

  if (-not $SkipSmokeTest) {
    Write-Host "Running release smoke test..."
    Expand-Archive -LiteralPath $zip -DestinationPath $smoke -Force
    $packagedPython = Join-Path $smoke "runtime\python.exe"
    if (Test-Path -LiteralPath $packagedPython) {
      $python = $packagedPython
    } elseif (Test-Path -LiteralPath (Join-Path $root ".venv\Scripts\python.exe")) {
      $python = Join-Path $root ".venv\Scripts\python.exe"
    } else {
      $python = (Get-Command python -ErrorAction Stop).Source
    }
    $packagedPython = Join-Path $smoke "runtime\python.exe"
    if (Test-Path -LiteralPath $packagedPython) {
      & $packagedPython (Join-Path $smoke "scripts\ensure_runtime_backend_path.py") `
        --pth (Join-Path $smoke "runtime\python312._pth") `
        --backend (Join-Path $smoke "backend")
      if ($LASTEXITCODE -ne 0) { throw "Could not prepare the extracted portable Python runtime" }
    } else {
      $env:PYTHONPATH = Join-Path $smoke "backend"
    }
    $env:TAGGER2_PROJECT_ROOT = $smoke
    Push-Location $smoke
    try {
      & $python -c "from fastapi.testclient import TestClient; from tagger2.main import app; c=TestClient(app); r=c.get('/api/v1/health'); assert r.status_code == 200, (r.status_code, r.text); print('release smoke: health 200')"
      if ($LASTEXITCODE -ne 0) { throw "Release smoke test failed with exit code $LASTEXITCODE" }
    } finally {
      Pop-Location
      Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
      Remove-Item Env:TAGGER2_PROJECT_ROOT -ErrorAction SilentlyContinue
    }
  }
  Write-Host "Release package: $zip"
} finally {
  if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
  if (Test-Path -LiteralPath $smoke) { Remove-Item -LiteralPath $smoke -Recurse -Force }
}
