[CmdletBinding()]
param(
  [string]$ProjectRoot = "",
  [string]$Remote = "origin",
  [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

trap {
  Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}

function Invoke-GitCommand {
  param([string[]]$Arguments)

  $previousPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = @(& $script:GitExecutable -C $script:ResolvedRoot @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousPreference
  }

  [pscustomobject]@{
    ExitCode = $exitCode
    Output = @($output | ForEach-Object { $_.ToString() })
  }
}

function Require-GitSuccess {
  param(
    [string[]]$Arguments,
    [string]$FailureMessage
  )

  $result = Invoke-GitCommand -Arguments $Arguments
  if ($result.ExitCode -ne 0) {
    throw $FailureMessage
  }
  return $result
}

$rootCandidate = if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
  Split-Path -Parent $PSScriptRoot
} else {
  $ProjectRoot
}

if (-not (Test-Path -LiteralPath $rootCandidate -PathType Container)) {
  throw "Project root does not exist: $rootCandidate"
}
$script:ResolvedRoot = (Resolve-Path -LiteralPath $rootCandidate).Path

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
  throw "Git is required. Install Git for Windows, then run update.bat again."
}
$script:GitExecutable = $git.Source

$workTree = Invoke-GitCommand -Arguments @("rev-parse", "--is-inside-work-tree")
if ($workTree.ExitCode -ne 0 -or ($workTree.Output -join "").Trim() -ne "true") {
  throw "This folder is not a Git checkout. Download the latest V1.04 package from GitHub Releases instead."
}

$remoteNames = Require-GitSuccess -Arguments @("remote") -FailureMessage "Could not read Git remotes."
if ($Remote -notin $remoteNames.Output) {
  throw "Git remote '$Remote' does not exist."
}

$validBranch = Invoke-GitCommand -Arguments @("check-ref-format", "--branch", $Branch)
if ($validBranch.ExitCode -ne 0) {
  throw "Invalid Git branch name: $Branch"
}

$currentBranch = Invoke-GitCommand -Arguments @("symbolic-ref", "--quiet", "--short", "HEAD")
if ($currentBranch.ExitCode -ne 0) {
  throw "Detached HEAD is not supported. Check out a local branch before updating."
}

$trackedStatus = Require-GitSuccess `
  -Arguments @("status", "--porcelain=v1", "--untracked-files=no") `
  -FailureMessage "Could not inspect the Git working tree."
if ($trackedStatus.Output.Count -gt 0) {
  throw "The project has uncommitted tracked changes. Commit or stash them before updating."
}

Write-Host "[INFO] Fetching $Remote/$Branch..."
$remoteRef = "refs/remotes/$Remote/$Branch"
$refspec = "+refs/heads/$Branch`:$remoteRef"
$fetch = Invoke-GitCommand -Arguments @("fetch", "--prune", $Remote, $refspec)
if ($fetch.ExitCode -ne 0) {
  throw "Git fetch failed. Check network access and remote credentials."
}

$head = Require-GitSuccess -Arguments @("rev-parse", "HEAD") -FailureMessage "Could not resolve the current commit."
$remoteHead = Require-GitSuccess -Arguments @("rev-parse", $remoteRef) -FailureMessage "Could not resolve $Remote/$Branch."
$headSha = ($head.Output -join "").Trim()
$remoteSha = ($remoteHead.Output -join "").Trim()

if ($headSha -eq $remoteSha) {
  Write-Host "[INFO] Already up to date at $($headSha.Substring(0, 7))."
  exit 0
}

$canFastForward = Invoke-GitCommand -Arguments @("merge-base", "--is-ancestor", $headSha, $remoteSha)
if ($canFastForward.ExitCode -eq 0) {
  Write-Host "[INFO] Fast-forwarding $($currentBranch.Output[0]) to $Remote/$Branch..."
  $merge = Invoke-GitCommand -Arguments @("merge", "--ff-only", $remoteRef)
  if ($merge.ExitCode -ne 0) {
    throw "Fast-forward failed. An untracked file may conflict with the update."
  }
  foreach ($line in $merge.Output) {
    Write-Host $line
  }
  $updatedHead = Require-GitSuccess -Arguments @("rev-parse", "HEAD") -FailureMessage "Could not verify the updated commit."
  $updatedSha = ($updatedHead.Output -join "").Trim()
  if ($updatedSha -ne $remoteSha) {
    throw "Update verification failed: HEAD does not match $Remote/$Branch."
  }
  Write-Host "[INFO] Updated $headSha -> $remoteSha."
  exit 0
}
if ($canFastForward.ExitCode -ne 1) {
  throw "Could not compare the local and remote Git history."
}

$localContainsRemote = Invoke-GitCommand -Arguments @("merge-base", "--is-ancestor", $remoteSha, $headSha)
if ($localContainsRemote.ExitCode -eq 0) {
  Write-Host "[INFO] The local branch already contains $Remote/$Branch and has additional local commits."
  exit 0
}
if ($localContainsRemote.ExitCode -ne 1) {
  throw "Could not compare the local and remote Git history."
}

throw "The local branch has diverged from $Remote/$Branch. Resolve it manually; no files were changed."
