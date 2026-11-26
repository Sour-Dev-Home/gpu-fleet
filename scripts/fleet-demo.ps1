<#
Phase 2c: run the local fleet end to end and time it.

  ./scripts/fleet-demo.ps1              echo (1 worker), then smoke on the GPU with 1 and with 3 workers
  ./scripts/fleet-demo.ps1 -Jobs 40     more jobs per run
  ./scripts/fleet-demo.ps1 -SkipEcho    GPU runs only

Each run starts from empty volumes: demo inputs are the same every time, so a reused results volume would
answer "cached" without touching the GPU. Tokens come from the local "fleet" vault (created there if missing,
never printed) and live in this process only. A summary is written to data/ (git-ignored).
#>
param(
  [int]$Jobs = 20,
  [string]$Model = 'qwen3:8b',
  [string]$ModelUrl = 'http://localhost:11434',
  [int]$TimeoutMinutes = 15,
  [switch]$SkipEcho
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$tokenNames = 'FLEET_WORKER_TOKEN', 'FLEET_ADMIN_TOKEN', 'FLEET_DB_PASSWORD'

function Write-Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

function Invoke-Compose {
  # Native command: check its exit code ourselves (ErrorActionPreference doesn't cover exe failures).
  # Out-Host: show compose's output without it becoming this function's return value (a PowerShell
  # function returns everything written to its output, which put build logs into the summary table).
  & docker compose @args 2>&1 | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

function Get-JobCounts {
  # psql inside the postgres container, over its socket (no password, no published port).
  $out = & docker compose exec -T postgres psql -U fleet -d fleet -At -F ' ' -c 'SELECT state, count(*) FROM jobs GROUP BY state'
  if ($LASTEXITCODE -ne 0) { throw 'could not read the job store' }
  $counts = @{ queued = 0; leased = 0; done = 0; dead = 0 }
  foreach ($m in [regex]::Matches("$out", '(\w+) (\d+)')) { $counts[$m.Groups[1].Value] = [int]$m.Groups[2].Value }
  $counts
}

function Invoke-Run([string]$Runner, [string]$PromptVersion, [int]$Workers) {
  Write-Step "$Runner x $Workers worker(s), $Jobs jobs ($PromptVersion)"
  Invoke-Compose down -v --remove-orphans 2>$null
  $env:FLEET_RUNNER = $Runner
  Invoke-Compose up -d --wait broker
  Invoke-Compose up -d --scale "worker=$Workers" worker
  # echo jobs name the placeholder model "echo" (fleet-enqueue's default for echo-v1, ADR 0015);
  # only the smoke jobs name the real model.
  $modelArgs = if ($Runner -eq 'smoke') { @('--model', $Model) } else { @() }
  Invoke-Compose run --rm broker fleet-enqueue --demo $Jobs --prompt-version $PromptVersion @modelArgs

  $clock = [Diagnostics.Stopwatch]::StartNew()
  $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
  while ($true) {
    Start-Sleep -Milliseconds 1000
    $c = Get-JobCounts
    Write-Host ("`r  {0,5:N1}s  done {1,3}  leased {2,2}  queued {3,3}  dead {4,2}" -f
      $clock.Elapsed.TotalSeconds, $c.done, $c.leased, $c.queued, $c.dead) -NoNewline
    if ($c.done + $c.dead -ge $Jobs) { break }
    $running = (& docker compose ps --status running -q worker | Measure-Object).Count
    if ($running -eq 0) { Write-Host ''; Invoke-Compose logs --tail 5 worker; throw 'every worker stopped; see the log above' }
    if ((Get-Date) -gt $deadline) { Write-Host ''; throw "timed out after $TimeoutMinutes minutes" }
  }
  $clock.Stop()
  Write-Host ''
  $results = (& docker compose exec -T worker sh -c 'ls /results | wc -l').Trim()
  [pscustomobject]@{
    Run       = "$Runner x$Workers"
    Jobs      = $Jobs
    Done      = $c.done
    Dead      = $c.dead
    Results   = [int]$results
    Seconds   = [math]::Round($clock.Elapsed.TotalSeconds, 1)
    JobsPerMin = [math]::Round($c.done / $clock.Elapsed.TotalMinutes, 1)
  }
}

try {
  Write-Step 'Preflight'
  & docker info --format '{{.ServerVersion}}' | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Docker is not running: start Docker Desktop first' }
  try { Get-SecretInfo -Vault fleet -ErrorAction Stop | Out-Null }
  catch {
    # SecretStore locks itself after its timeout; Unlock-SecretStore prompts for the password (masked).
    Write-Host '  the fleet vault is locked: enter its password' -ForegroundColor Yellow
    Unlock-SecretStore
  }
  foreach ($n in $tokenNames) {
    if (-not (Get-SecretInfo -Vault fleet -Name $n)) {
      Write-Host "  creating $n in the fleet vault (random, 32 bytes)"
      $bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
      # The database password goes into a URL: hex, because base64's + / = would break it (docs/secrets.md).
      $value = if ($n -eq 'FLEET_DB_PASSWORD') { [Convert]::ToHexString($bytes) } else { [Convert]::ToBase64String($bytes) }
      Set-Secret -Vault fleet -Name $n -Secret $value
    }
    Set-Item "env:$n" (Get-Secret -Vault fleet -Name $n -AsPlainText)
  }
  Write-Host '  tokens loaded for this process only'
  Write-Step 'Building images (once, quietly)'
  Invoke-Compose build -q

  $rows = @()
  if (-not $SkipEcho) { $rows += Invoke-Run echo echo-v1 1 }

  $tags = Invoke-RestMethod "$ModelUrl/api/tags" -TimeoutSec 5
  if ($Model -notin $tags.models.name) { throw "Ollama at $ModelUrl doesn't have ${Model}: run 'ollama pull $Model'" }
  Write-Step "Warming $Model (same options as the smoke runner, so the timed runs don't pay the load)"
  $warm = @{ model = $Model; prompt = 'ok'; stream = $false; think = $false
             options = @{ num_ctx = 2048; num_predict = 1; use_mmap = $false } } | ConvertTo-Json
  Invoke-RestMethod "$ModelUrl/api/generate" -Method Post -Body $warm -ContentType 'application/json' -TimeoutSec 300 | Out-Null

  $rows += Invoke-Run smoke smoke-v1 1
  $rows += Invoke-Run smoke smoke-v1 3

  Write-Step 'Summary'
  $table = $rows | Format-Table -AutoSize | Out-String
  Write-Host $table
  New-Item -ItemType Directory -Force data | Out-Null
  $file = "data/fleet-demo-$(Get-Date -Format yyyyMMdd-HHmmss).txt"
  "fleet-demo $(Get-Date -Format s), model $Model`n$table" | Set-Content $file
  Write-Host "Saved to $file"
}
finally {
  Write-Step 'Cleanup'
  & docker compose down -v --remove-orphans 2>$null | Out-Null
  foreach ($n in $tokenNames + 'FLEET_RUNNER') { Remove-Item "env:$n" -ErrorAction SilentlyContinue }
  Write-Host '  stack stopped, volumes removed, tokens cleared from this process'
}
