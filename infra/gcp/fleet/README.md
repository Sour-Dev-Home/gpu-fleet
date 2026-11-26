# infra/gcp/fleet

The broker on Google Cloud (ADR 0021): Artifact Registry, a Cloud SQL for PostgreSQL instance, two empty Secret
Manager secrets, a service account, and the Cloud Run service. State is in the AWS state bucket
(`gcp/fleet/terraform.tfstate`). No tfvars: the project ID comes from the environment and is never committed.

Run order matters: the service needs an image, the image needs the registry, so there are two applies.

## 1. Sign in (no keys)

```powershell
gcloud auth login
gcloud auth application-default login     # what Terraform's google provider reads
aws login --profile fleet                 # the state bucket is in AWS, as in infra/aws/README.md
$env:AWS_PROFILE = "fleet"
```

## 2. The project ID, from the vault

```powershell
Set-Secret -Vault fleet -Name TF_VAR_project_id     # prompts; paste the project ID
cd infra/gcp/fleet
../../../scripts/with-secrets.ps1 -Names TF_VAR_project_id -- terraform init
```

Every Terraform command below runs as `../../../scripts/with-secrets.ps1 -Names TF_VAR_project_id -- terraform ...`
(shortened to `tf` here). The same goes for `gcloud` commands that need the ID: `$env:TF_VAR_project_id` inside
`with-secrets`, or `gcloud config set project` once.

## 3. First apply (no image)

```powershell
tf plan "-out=tfplan"      # review: the infra-reviewer agent, then you (CLAUDE.md)
tf apply tfplan            # about 10 minutes: the Cloud SQL instance is the slow part
```

This makes everything except the Cloud Run service (`broker_image` is null).

## 4. The two broker tokens (new ones for the cloud)

Generate NEW tokens (not the local ones; docs/secrets.md) straight into the vault under cloud names, then add them as
secret versions, piped so they never show or enter Terraform state:

```powershell
foreach ($n in 'FLEET_CLOUD_WORKER_TOKEN','FLEET_CLOUD_ADMIN_TOKEN') {
  Set-Secret -Vault fleet -Name $n -Secret ([Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)))
}
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUD_WORKER_TOKEN -- cmd /c "<nul set /p=%FLEET_CLOUD_WORKER_TOKEN%| gcloud secrets versions add fleet-worker-token --data-file=-"
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUD_ADMIN_TOKEN  -- cmd /c "<nul set /p=%FLEET_CLOUD_ADMIN_TOKEN%| gcloud secrets versions add fleet-admin-token --data-file=-"
```

UNTESTED. `cmd`'s `set /p` prints the value with no trailing newline; PowerShell's `$x | gcloud` would add one, and
Secret Manager keeps it, so the stored token would never match what workers send. A `401` in step 9 with the right
token means this: check `gcloud secrets versions access latest --secret=fleet-worker-token | Format-Hex` (44 bytes).

## 5. The `postgres` password (never in Terraform)

```powershell
Set-Secret -Vault fleet -Name FLEET_CLOUDSQL_POSTGRES_PASSWORD -Secret ([Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)))
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUDSQL_POSTGRES_PASSWORD -- pwsh -NoProfile -Command 'gcloud sql users set-password postgres --instance=fleet --password=$env:FLEET_CLOUDSQL_POSTGRES_PASSWORD'
```

The password is on the command line for that one process. Acceptable on a single-user PC; the alternative is
`gcloud sql users set-password postgres --instance=fleet --prompt-for-password` and storing it in the vault by hand.

## 6. One-time bootstrap: let the IAM user own the database (UNTESTED)

The IAM user exists but has no privileges. As `postgres`, through the proxy under your own login (you need
`roles/cloudsql.client`; a project Owner has it). Names come from `tf output`:

```powershell
$conn = tf output -raw instance_connection_name
$u    = tf output -raw iam_database_user
cloud-sql-proxy $conn --port 5432          # leave running; a second terminal for psql
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUDSQL_POSTGRES_PASSWORD -- pwsh -NoProfile -Command '$env:PGPASSWORD=$env:FLEET_CLOUDSQL_POSTGRES_PASSWORD; psql -h 127.0.0.1 -U postgres -d fleet'
```

```sql
GRANT "<iam user>" TO postgres;
ALTER DATABASE fleet OWNER TO "<iam user>";
-- fallback if the broker's migrations still say "permission denied for schema public":
GRANT ALL ON SCHEMA public TO "<iam user>";
```

UNTESTED: written from the docs. The first request after deploy runs the broker's migrations as the IAM user, so
this is where a mistake shows up (a 503 and a log line naming SQLSTATE 42501).

## 7. Build and push the image

```powershell
gcloud auth configure-docker us-central1-docker.pkg.dev
$repo = tf output -raw registry_path
docker build --platform linux/amd64 -t "$repo/broker:latest" broker     # Cloud Run is amd64
docker push "$repo/broker:latest"
docker buildx imagetools inspect "$repo/broker:latest" | Select-String Digest     # sha256:...
```

## 8. Second apply, by digest

```powershell
$env:TF_VAR_broker_image = "$repo/broker@sha256:<digest from step 7>"
tf plan "-out=tfplan"; tf apply tfplan
```

(Not a secret, so it can live in the shell. A tag is refused by the variable's validation.)

## 9. Smoke test

```powershell
$url = tf output -raw service_url
curl.exe -i "$url/healthz"                                   # 204 (the first call is a cold start: a few seconds)
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUD_WORKER_TOKEN -- pwsh -NoProfile -Command 'curl.exe -i -X POST -H "Authorization: Bearer $env:FLEET_CLOUD_WORKER_TOKEN" -H "Content-Type: application/json" -d "{\"capabilities\":[\"smoke\"],\"models\":[\"smoke\"]}" "$env:URL/lease"'
```

Set `$env:URL = $url` first. With an empty queue (or nothing matching) the answer is `204`. A `401` means the secret version has a stray newline (step 4); a `503` means the database
(step 6).

## 10. Teardown

Deletion protection is on at three levels. Set `deletion_protection = false` on the instance AND
`deletion_protection_enabled = false` in its `settings`, and `deletion_protection = false` on the Cloud Run
service; `terraform apply`; then `terraform destroy`. The APIs are left enabled (`disable_on_destroy = false`).
Stopping the instance instead (`gcloud sql instances patch fleet --activation-policy=NEVER`) stops compute charges
but keeps the disk (about $1.70/month).

The console budget is not in this stack; it is imported later (as with AWS).
