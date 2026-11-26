# One-time Cloud SQL bootstrap (phase 4 step 4, runbook step 6), all in Docker: no local proxy or psql.
# Run from infra/gcp/fleet, under with-secrets so the postgres password is only an env var:
#   $env:AWS_PROFILE = "fleet"
#   <repo>/scripts/with-secrets.ps1 -Names FLEET_CLOUDSQL_POSTGRES_PASSWORD -- pwsh -NoProfile -File <this file>
$ErrorActionPreference = 'Stop'
$proxyImage = 'gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.28.1@sha256:c01857183fd88d7f4366500f8f89e79206d5dba596d1b4eada8974082c162000'
$pgImage    = 'postgres:18@sha256:c699303802144847d238196173655dbbecf7a96aeca1565f2b02eb671a1f0b6b'
$net = 'fleet-bootstrap'; $proxy = 'fleet-bootstrap-proxy'

if (-not $env:FLEET_CLOUDSQL_POSTGRES_PASSWORD) { throw 'Run this under with-secrets with FLEET_CLOUDSQL_POSTGRES_PASSWORD.' }
$conn = terraform output -raw instance_connection_name
$user = terraform output -raw iam_database_user
if (-not $conn -or -not $user) { throw 'terraform output failed: is AWS_PROFILE=fleet set and are you in infra/gcp/fleet?' }
$adc = Join-Path $env:APPDATA 'gcloud\application_default_credentials.json'

try {
  docker network create $net | Out-Null
  # The proxy uses YOUR login (application default credentials), mounted read-only. No ports leave Docker.
  docker run -d --rm --name $proxy --network $net `
    -v "${adc}:/creds.json:ro" -e GOOGLE_APPLICATION_CREDENTIALS=/creds.json `
    $proxyImage $conn --address 0.0.0.0 --port 5432 | Out-Null
  $ready = $false
  foreach ($i in 1..30) {
    if ((docker logs $proxy 2>&1 | Out-String) -match 'ready for new connections') { $ready = $true; break }
    Start-Sleep -Seconds 1
  }
  if (-not $ready) { docker logs $proxy 2>&1 | Select-Object -Last 10; throw 'proxy did not become ready' }

  # Least privilege for in-app migrations: connect and create in the database, use and create in public.
  $sql = @"
\set ON_ERROR_STOP on
GRANT CONNECT, CREATE ON DATABASE fleet TO "$user";
GRANT USAGE, CREATE ON SCHEMA public TO "$user";
SELECT 'granted' AS result;
SHOW max_connections;
"@
  $env:PGPASSWORD = $env:FLEET_CLOUDSQL_POSTGRES_PASSWORD
  $sql | docker run --rm -i --network $net -e PGPASSWORD $pgImage `
    psql -h $proxy -U postgres -d fleet -X -q -f -
  if ($LASTEXITCODE -ne 0) { throw "psql failed (exit $LASTEXITCODE)" }
} finally {
  Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
  docker rm -f $proxy 2>&1 | Out-Null
  docker network rm $net 2>&1 | Out-Null
}
