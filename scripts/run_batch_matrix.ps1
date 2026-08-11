param(
  [Parameter(Mandatory)]
  [string]$InputRootId,
  [Parameter(Mandatory)]
  [string]$OutputRootId,
  [Parameter(Mandatory)]
  [string]$ProviderId,
  [Parameter(Mandatory)]
  [string]$SingleModelId,
  [Parameter(Mandatory)]
  [string]$SecondModelId,
  [string]$ApiBase = "http://127.0.0.1:20000/api/v1",
  [string]$ReportPath = "E:\6_santabear1\Tagger2_Batch_TestOutput\batch-matrix-report.json",
  [int]$BatchSize = 8,
  [int]$OnlineConcurrency = 3,
  [int]$TimeoutMinutes = 30
)

$ErrorActionPreference = "Stop"
$terminalStates = @("succeeded", "failed", "cancelled", "interrupted")

function Invoke-Api {
  param(
    [Parameter(Mandatory)][ValidateSet("GET", "POST")][string]$Method,
    [Parameter(Mandatory)][string]$Path,
    [object]$Body
  )

  $uri = "$ApiBase$Path"
  if ($Method -eq "GET") {
    return Invoke-RestMethod -Method Get -Uri $uri -TimeoutSec 60
  }
  $json = $Body | ConvertTo-Json -Depth 12 -Compress
  return Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json" -Body $json -TimeoutSec 120
}

function New-Output {
  param(
    [Parameter(Mandatory)][string]$RelativePath,
    [Parameter(Mandatory)][bool]$Json,
    [Parameter(Mandatory)][bool]$Txt
  )

  return @{
    root_id = $OutputRootId
    relative_path = $RelativePath
    json = $Json
    txt = $Txt
    txt_include_tags = $false
    replace_underscores = $false
    include_rating = $false
    escape_parentheses = $true
    conflict = "overwrite"
  }
}

function New-LocalJob {
  param(
    [Parameter(Mandatory)][string[]]$ModelIds,
    [Parameter(Mandatory)][string]$RelativePath
  )

  return @{
    mode = "local"
    source = @{
      type = "scan"
      root_id = $InputRootId
      relative_path = ""
      recursive = $false
      patterns = @("*.webp")
    }
    model_ids = $ModelIds
    separate_models = $true
    batch_size = $BatchSize
    output = New-Output -RelativePath $RelativePath -Json $false -Txt $true
  }
}

function New-OnlineJob {
  param(
    [Parameter(Mandatory)][string]$RelativePath,
    [Parameter(Mandatory)][ValidateSet("json", "nl")][string]$ResponseMode
  )

  $jsonOutput = $ResponseMode -eq "json"
  return @{
    mode = "online"
    source = @{
      type = "scan"
      root_id = $InputRootId
      relative_path = ""
      recursive = $false
      patterns = @("*.webp")
    }
    provider_id = $ProviderId
    online_response = $ResponseMode
    online_concurrency = $OnlineConcurrency
    output = New-Output -RelativePath $RelativePath -Json $jsonOutput -Txt (-not $jsonOutput)
  }
}

function Wait-ForJobs {
  param([Parameter(Mandatory)][object[]]$Jobs)

  $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
  $lastStates = @{}
  while ((Get-Date) -lt $deadline) {
    $current = @()
    foreach ($job in $Jobs) {
      $state = Invoke-Api -Method GET -Path "/jobs/$($job.id)"
      $current += $state
      $signature = "$($state.state):$($state.processed):$($state.total):$($state.failed)"
      if ($lastStates[$state.id] -ne $signature) {
        Write-Host ("[{0}] {1}: {2} {3}/{4}, failed {5}" -f $job.label, $state.id.Substring(0, 8), $state.state, $state.processed, $state.total, $state.failed)
        $lastStates[$state.id] = $signature
      }
    }
    if (@($current | Where-Object { $_.state -notin $terminalStates }).Count -eq 0) {
      return $current
    }
    Start-Sleep -Seconds 2
  }
  $ids = ($Jobs | ForEach-Object { $_.id }) -join ", "
  throw "Timed out waiting for jobs: $ids"
}

function Get-ResultSummary {
  param([Parameter(Mandatory)][object]$Job)

  $results = Invoke-Api -Method GET -Path "/jobs/$($Job.id)/results"
  $items = @($results.items)
  $artifacts = @($items | ForEach-Object { $_.artifacts } | Where-Object { $_ })
  return [ordered]@{
    label = $Job.label
    id = $Job.id
    mode = $Job.mode
    state = $Job.state
    total = $results.total
    succeeded = @($items | Where-Object { $_.status -eq "succeeded" }).Count
    skipped = @($items | Where-Object { $_.status -eq "skipped" }).Count
    failed = @($items | Where-Object { $_.status -eq "failed" }).Count
    artifact_kinds = @($artifacts | Group-Object kind | ForEach-Object { [ordered]@{ kind = $_.Name; count = $_.Count } })
    sample_artifacts = @($artifacts | Select-Object -First 3 kind,path,size)
    sample_file = if ($items.Count) { $items[0].file_name } else { $null }
  }
}

function Run-Scenario {
  param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][object[]]$Definitions
  )

  Write-Host "Starting $Name"
  $started = @()
  foreach ($definition in $Definitions) {
    $record = Invoke-Api -Method POST -Path "/jobs" -Body $definition.body
    $started += [pscustomobject]@{
      id = $record.id
      label = $definition.label
      mode = $record.mode
    }
  }
  $finished = Wait-ForJobs -Jobs $started
  $summaries = @()
  foreach ($state in $finished) {
    $job = $started | Where-Object { $_.id -eq $state.id } | Select-Object -First 1
    $job | Add-Member -NotePropertyName state -NotePropertyValue $state.state
    $summaries += Get-ResultSummary -Job $job
  }
  if (@($summaries | Where-Object { $_.state -ne "succeeded" -or $_.failed -gt 0 }).Count -gt 0) {
    throw "$Name did not complete cleanly."
  }
  return [ordered]@{ scenario = $Name; jobs = $summaries }
}

$single = @($SingleModelId)
$multiple = @($SingleModelId, $SecondModelId)
$runs = @()
$runs += Run-Scenario -Name "01_single_local_txt" -Definitions @(
  @{ label = "local-tag"; body = New-LocalJob -ModelIds $single -RelativePath "01_single_local_txt" }
)
$runs += Run-Scenario -Name "02_multi_local_txt" -Definitions @(
  @{ label = "local-tag"; body = New-LocalJob -ModelIds $multiple -RelativePath "02_multi_local_txt" }
)
$runs += Run-Scenario -Name "03_online_anima_json" -Definitions @(
  @{ label = "online-anima"; body = New-OnlineJob -RelativePath "03_online_anima_json" -ResponseMode "json" }
)
$runs += Run-Scenario -Name "04_single_local_online_txt" -Definitions @(
  @{ label = "local-tag"; body = New-LocalJob -ModelIds $single -RelativePath "04_single_local_online_txt/local_tags" },
  @{ label = "online-nl"; body = New-OnlineJob -RelativePath "04_single_local_online_txt/online_nl" -ResponseMode "nl" }
)
$runs += Run-Scenario -Name "05_multi_local_online_txt" -Definitions @(
  @{ label = "local-tag"; body = New-LocalJob -ModelIds $multiple -RelativePath "05_multi_local_online_txt/local_tags" },
  @{ label = "online-nl"; body = New-OnlineJob -RelativePath "05_multi_local_online_txt/online_nl" -ResponseMode "nl" }
)
$runs += Run-Scenario -Name "06_multi_local_online_json_txt" -Definitions @(
  @{ label = "local-tag"; body = New-LocalJob -ModelIds $multiple -RelativePath "06_multi_local_online_json_txt/local_tags" },
  @{ label = "online-anima"; body = New-OnlineJob -RelativePath "06_multi_local_online_json_txt/online_anima" -ResponseMode "json" }
)

$report = [ordered]@{
  started_at = (Get-Date).ToUniversalTime().ToString("o")
  input_root_id = $InputRootId
  output_root_id = $OutputRootId
  image_pattern = "*.webp"
  models = @($SingleModelId, $SecondModelId)
  provider_id = $ProviderId
  runs = $runs
}
$reportDirectory = Split-Path -Parent $ReportPath
New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
$report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ReportPath -Encoding utf8
Write-Host "Report: $ReportPath"
