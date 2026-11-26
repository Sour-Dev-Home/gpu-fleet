#Requires -RunAsAdministrator
<#
Points an already installed Grafana Alloy at observability/alloy/config.alloy (ADR 0027, Q91). Run it elevated;
re-running it is safe (every step sets a value, none appends) and is also how a rotated token is applied.

  winget install GrafanaLabs.Alloy                   # once; or the installer from grafana.com (observability/alloy/README.md)
  ./scripts/setup-alloy.ps1                           # elevated PowerShell

Reads these entries from the local `fleet` vault (docs/secrets.md); the token never appears in output, in a
parameter, on a command line or in the registry:
  FLEET_GRAFANA_OTLP_URL, FLEET_GRAFANA_INSTANCE_ID, FLEET_GRAFANA_TOKEN_PC_ALLOY

It then
  1. writes the token to %ProgramData%\fleet\alloy\grafana-token, readable by SYSTEM and Administrators only
     (inheritance off; the ACL is set on the EMPTY file before the token goes in),
  2. sets the Alloy service's environment (HKLM\SOFTWARE\GrafanaLabs\Alloy, value Environment): the URL, the
     instance ID, the token FILE's path and the worker's install ID,
  3. copies the repo's config to %ProgramData%\fleet\alloy\config.alloy (the service runs as SYSTEM, so it must not
     run a user-writable file), keeps the service's other arguments (the storage path) and points `run` at the copy,
  4. restarts the service.
The registry holds an identifier of the account (instance ID) and the endpoint, not the token.
#>
param(
  [string]$ConfigPath = (Join-Path $PSScriptRoot '..\observability\alloy\config.alloy'),
  [string]$ServiceName = 'Alloy',
  # The worker's install ID (worker/src/fleet_worker/identity.py): FLEET_WORKER_ID, else the stored worker-id file.
  [string]$InstallId = ''
)
$ErrorActionPreference = 'Stop'

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) { throw "The '$ServiceName' service is not installed. Install Alloy first: winget install GrafanaLabs.Alloy" }
$config = (Resolve-Path $ConfigPath).Path
# The service's registry key (documented for the Windows installer; UNVERIFIED on this machine, see
# observability/alloy/README.md). Checked first: nothing is changed if it is not there.
$key = 'HKLM:\SOFTWARE\GrafanaLabs\Alloy'
if (-not (Test-Path $key)) { throw "$key not found: is Alloy installed with the Windows installer?" }

if (-not $InstallId) { $InstallId = $env:FLEET_WORKER_ID }
if (-not $InstallId) {
  $idFile = Join-Path $env:APPDATA 'fleet\worker-id'
  if (Test-Path $idFile) { $InstallId = (Get-Content -Raw $idFile).Trim() }
}
if ($InstallId -notmatch '^[A-Za-z0-9._-]{1,64}$') {
  throw 'No usable install ID: run the worker once, or pass -InstallId (1-64 characters from A-Z a-z 0-9 . _ -).'
}

$url = Get-Secret -Name FLEET_GRAFANA_OTLP_URL -Vault fleet -AsPlainText
$instanceId = Get-Secret -Name FLEET_GRAFANA_INSTANCE_ID -Vault fleet -AsPlainText
if ($url -notmatch '^https://') { throw 'FLEET_GRAFANA_OTLP_URL must be an https:// URL.' }
if ($instanceId -notmatch '^[0-9]+$') { throw 'FLEET_GRAFANA_INSTANCE_ID must be the numeric instance ID.' }

# 1. The token file: create it empty, lock it, THEN write. SIDs, so a localized Windows works.
$parent = Join-Path $env:ProgramData 'fleet'
$dir = Join-Path $parent 'alloy'
New-Item -ItemType Directory -Force $dir | Out-Null
$tokenFile = Join-Path $dir 'grafana-token'
$configCopy = Join-Path $dir 'config.alloy'
# A symlink or junction planted where we write would redirect the token (or the ACL change) elsewhere.
function Assert-NotReparsePoint([string]$Path) {
  if (Test-Path -LiteralPath $Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw "$Path is a reparse point (symbolic link or junction); refusing to use it."
    }
  }
}
# The parent too: Users may create folders in ProgramData, and whoever owns `fleet` could later rename
# `alloy` and put their own config where the SYSTEM service looks.
Assert-NotReparsePoint $parent
Assert-NotReparsePoint $dir
# Recreated, never reused: a handle a user opened on a pre-planted file would survive the ACL change.
if (Test-Path -LiteralPath $tokenFile) { Assert-NotReparsePoint $tokenFile; Remove-Item -LiteralPath $tokenFile -Force }
New-Item -ItemType File $tokenFile | Out-Null
Assert-NotReparsePoint $configCopy
$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
foreach ($path in @($parent, $dir, $tokenFile)) {
  $acl = Get-Acl $path
  $acl.SetOwner($admins)                        # a pre-planted object may be owned by a user, who could undo the ACL
  $acl.SetAccessRuleProtection($true, $false)   # inheritance off, inherited rules dropped
  foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
  $flags = if ((Get-Item $path).PSIsContainer) { 'ContainerInherit,ObjectInherit' } else { 'None' }
  foreach ($sid in @($system, $admins)) {
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', $flags, 'None', 'Allow')))
  }
  Set-Acl -Path $path -AclObject $acl
}
# The SYSTEM service must not run a file a user can edit: it runs this COPY, in the protected directory
# (the ACL above is already in place). Re-running this script refreshes the copy from the repo file.
# A pre-existing copy is deleted first, so the new file takes the directory's ACL and owner.
if (Test-Path -LiteralPath $configCopy) { Remove-Item -LiteralPath $configCopy -Force }  # a failed delete stops here
Copy-Item -LiteralPath $config -Destination $configCopy
$token = Get-Secret -Name FLEET_GRAFANA_TOKEN_PC_ALLOY -Vault fleet -AsPlainText
# No trailing newline and no BOM: Alloy would send both as part of the password.
[IO.File]::WriteAllText($tokenFile, $token, (New-Object System.Text.UTF8Encoding($false)))
$token = $null

# 2 and 3. Environment is a multi-string of NAME=value, Arguments one of the command line.
$environment = @(
  "FLEET_GRAFANA_OTLP_URL=$url",
  "FLEET_GRAFANA_INSTANCE_ID=$instanceId",
  "FLEET_GRAFANA_TOKEN_FILE=$tokenFile",
  "FLEET_INSTALL_ID=$InstallId"
)
$current = Get-ItemProperty -Path $key
# Keep every existing flag (--storage.path and friends); replace only the config file.
$flagsKept = @($current.Arguments | Where-Object { $_ -like '--*' })
$arguments = @('run', $configCopy) + $flagsKept
Set-ItemProperty -Path $key -Name Environment -Value $environment -Type MultiString
Set-ItemProperty -Path $key -Name Arguments -Value $arguments -Type MultiString

# 4. Restart so the new environment, arguments and token are read.
Restart-Service -Name $ServiceName
Write-Output "Alloy configured: config copy $configCopy, $($environment.Count) environment entries, token file protected."
