<#
Runs a command with secrets from the local "fleet" vault as environment variables, for that process only.
  ./scripts/with-secrets.ps1 -Names HF_TOKEN,KAGGLE_KEY -- uv run pytest
  ./scripts/with-secrets.ps1 -Names TF_VAR_broker_admin_token -- terraform plan
Nothing is written to disk; variables are removed when the command ends.
#>
param([Parameter(Mandatory)][string[]]$Names, [Parameter(ValueFromRemainingArguments)][string[]]$Command)
$ErrorActionPreference = 'Stop'
if ($Command.Count -and $Command[0] -eq '--') { $Command = $Command[1..($Command.Count - 1)] }
$set = @()
try {
  foreach ($n in $Names) { Set-Item "env:$n" (Get-Secret -Name $n -Vault fleet -AsPlainText); $set += $n }
  & $Command[0] @($Command | Select-Object -Skip 1)
  exit $LASTEXITCODE
} finally { foreach ($n in $set) { Remove-Item "env:$n" -ErrorAction SilentlyContinue } }
