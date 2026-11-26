<#
Opt-in: runs a command (the worker) with OTLP settings for Grafana Cloud, for that one process tree only (ADR 0027).
Like with-secrets.ps1: nothing is written to disk or persisted, and the variables are removed when the command ends.

  ./scripts/grafana-env.ps1 -- fleet-worker <your usual arguments>
  ./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN -- pwsh -NoProfile -File ./scripts/grafana-env.ps1 -- fleet-worker <your usual arguments>

Reads FLEET_GRAFANA_OTLP_URL, FLEET_GRAFANA_INSTANCE_ID and FLEET_GRAFANA_TOKEN_PC_WORKER from the `fleet` vault
and sets OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS. The header is HTTP basic auth, user = the
instance ID, password = the write-only token, written the way Python's exporter needs it: the space after Basic as %20.
The worker still needs the [otel] extra installed (pip install "fleet-worker[otel]"); without it nothing is sent.
#>
param([Parameter(ValueFromRemainingArguments)][string[]]$Command)
$ErrorActionPreference = 'Stop'
if ($Command.Count -and $Command[0] -eq '--') { $Command = $Command[1..($Command.Count - 1)] }
if (-not $Command.Count) { throw 'Usage: grafana-env.ps1 -- <command> [args]' }
# Whatever the caller already had is put back afterwards (null = was not set), not just removed.
$savedEndpoint = $env:OTEL_EXPORTER_OTLP_ENDPOINT
$savedHeaders = $env:OTEL_EXPORTER_OTLP_HEADERS
try {
  $url = Get-Secret -Name FLEET_GRAFANA_OTLP_URL -Vault fleet -AsPlainText
  $instanceId = Get-Secret -Name FLEET_GRAFANA_INSTANCE_ID -Vault fleet -AsPlainText
  if ($url -notmatch '^https://') { throw 'FLEET_GRAFANA_OTLP_URL must be an https:// URL.' }
  if ($instanceId -notmatch '^[0-9]+$') { throw 'FLEET_GRAFANA_INSTANCE_ID must be the numeric instance ID.' }
  $token = Get-Secret -Name FLEET_GRAFANA_TOKEN_PC_WORKER -Vault fleet -AsPlainText
  $pair = "${instanceId}:$token"
  $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
  $header = "Authorization=Basic%20$b64"
  $token = $null
  Set-Item "env:OTEL_EXPORTER_OTLP_ENDPOINT" $url
  Set-Item "env:OTEL_EXPORTER_OTLP_HEADERS" $header
  & $Command[0] @($Command | Select-Object -Skip 1)
  exit $LASTEXITCODE
} finally {
  if ($null -ne $savedEndpoint) { Set-Item "env:OTEL_EXPORTER_OTLP_ENDPOINT" $savedEndpoint }
  else { Remove-Item "env:OTEL_EXPORTER_OTLP_ENDPOINT" -ErrorAction SilentlyContinue }
  if ($null -ne $savedHeaders) { Set-Item "env:OTEL_EXPORTER_OTLP_HEADERS" $savedHeaders }
  else { Remove-Item "env:OTEL_EXPORTER_OTLP_HEADERS" -ErrorAction SilentlyContinue }
}
