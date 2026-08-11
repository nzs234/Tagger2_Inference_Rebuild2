param(
  [string]$Source = "E:\AI\Tagger2_Inference",
  [string]$Destination = "E:\AI\Tagger2_Inference_Rebuild",
  [switch]$CopyHfCache
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Source)) { throw "Source project does not exist: $Source" }
New-Item -ItemType Directory -Force "$Destination\models", "$Destination\data_cache" | Out-Null

Write-Host "Copying model assets..."
robocopy "$Source\models" "$Destination\models" /E /MT:8 /J /R:2 /W:2 /NFL /NDL /NP | Out-Host
if ($LASTEXITCODE -gt 7) { throw "Model copy failed with robocopy code $LASTEXITCODE" }

if ($CopyHfCache -and (Test-Path "$Source\data_cache\huggingface")) {
  Write-Host "Copying non-secret Hugging Face cache..."
  robocopy "$Source\data_cache\huggingface" "$Destination\data_cache\huggingface" /E /MT:8 /J /R:2 /W:2 /XD "$Source\data_cache\huggingface\token" "$Source\data_cache\huggingface\stored_tokens" /XF token stored_tokens token.json /NFL /NDL /NP | Out-Host
  if ($LASTEXITCODE -gt 7) { throw "Cache copy failed with robocopy code $LASTEXITCODE" }
}

if (Test-Path "$Source\data_cache\model_profile_cache.json") {
  Copy-Item "$Source\data_cache\model_profile_cache.json" "$Destination\data_cache\model_profile_cache.json" -Force
}

$oldConfigs = @("$Source\llm_simplify_config.json", "$Source\gemini_tagger_config.json")
$found = $oldConfigs | Where-Object { Test-Path $_ }
if ($found) {
  Write-Warning "Old online configuration was detected but was intentionally not copied. Rotate any keys found there."
}
Write-Host "Migration complete. Review config/app.toml before first run."
