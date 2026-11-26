# infra/gcp/fleet

The broker on Google Cloud (ADR 0021): Artifact Registry, a Cloud SQL for PostgreSQL instance, empty Secret
Manager secrets, a service account, the Cloud Run service, a request-log exclusion and a 4xx alert. State is in
the AWS state bucket (`gcp/fleet/terraform.tfstate`). No tfvars: the project ID and the alert email come from the
vault and are never committed.

Run order matters: the service needs an image, the image needs the registry, and the broker's database user needs
its grant before the first revision starts. So: apply, tokens, password, grant, push, apply.

**Who may run it.** A project Owner can do everything below. Otherwise the deploying user needs: Service Usage
Admin, Project IAM Admin, Cloud Run Admin, Cloud SQL Admin, Artifact Registry Admin, Secret Manager Admin,
Logs Configuration Writer, Monitoring Editor, Service Account Admin, and Service Account User on `fleet-broker`.

## 1. Sign in (no keys)

```powershell
gcloud auth login
gcloud auth application-default login     # what Terraform's google provider reads
aws login --profile fleet                 # the state bucket is in AWS, as in infra/aws/README.md
$env:AWS_PROFILE = "fleet"
```

## 2. The project ID and alert email, from the vault

```powershell
Set-Secret -Vault fleet -Name TF_VAR_project_id     # prompts; paste the project ID
Set-Secret -Vault fleet -Name TF_VAR_alert_email    # prompts
cd infra/gcp/fleet
../../../scripts/with-secrets.ps1 -Names TF_VAR_project_id -- pwsh -NoProfile -Command 'gcloud auth application-default set-quota-project $env:TF_VAR_project_id'
../../../scripts/with-secrets.ps1 -Names TF_VAR_project_id,TF_VAR_alert_email -- terraform init
```

(The quota project makes Application Default Credentials bill API calls to this project, not a Google default.)
Every Terraform command below runs as `../../../scripts/with-secrets.ps1 -Names TF_VAR_project_id,TF_VAR_alert_email -- terraform ...`,
shortened to `tf`. `gcloud` commands that need the ID run under `with-secrets` too, or use `gcloud config set project` once.

## 3. First apply (no image)

```powershell
tf plan "-out=tfplan"      # review: the infra-reviewer agent, then you (CLAUDE.md)
tf apply tfplan            # about 10 minutes: the Cloud SQL instance is the slow part
```

This makes everything except the Cloud Run service (`broker_image` is null).

## 4. The two broker tokens (new ones for the cloud)

Generate NEW tokens (not the local ones; docs/secrets.md) straight into the vault under cloud names, then add them as
secret versions. Python reads the variable itself and writes it with no newline, so the token appears on no command line:

```powershell
foreach ($n in 'FLEET_CLOUD_WORKER_TOKEN','FLEET_CLOUD_ADMIN_TOKEN') {
  Set-Secret -Vault fleet -Name $n -Secret ([Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)))
}
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUD_WORKER_TOKEN -- pwsh -NoProfile -Command "python -c `"import os,sys;sys.stdout.write(os.environ['FLEET_CLOUD_WORKER_TOKEN'])`" | gcloud secrets versions add fleet-worker-token --data-file=-"
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUD_ADMIN_TOKEN -- pwsh -NoProfile -Command "python -c `"import os,sys;sys.stdout.write(os.environ['FLEET_CLOUD_ADMIN_TOKEN'])`" | gcloud secrets versions add fleet-admin-token --data-file=-"
```

UNTESTED. Check only the length, never the value (a base64 token of 32 bytes is 44 bytes):

```powershell
gcloud secrets versions access latest --secret=fleet-worker-token | python -c "import sys;print(len(sys.stdin.buffer.read()))"
```

A different number (45 or 46) means a stray newline got in, and the broker would reject its own workers.
`version = "latest"` is read when an instance starts: after adding a version, a new revision is needed
(change nothing but redeploy: `gcloud run services update fleet-broker --region us-central1`).

## 5. The `postgres` password (never in Terraform, never on a command line)

```powershell
Set-Secret -Vault fleet -Name FLEET_CLOUDSQL_POSTGRES_PASSWORD -Secret ([Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)))
gcloud sql users set-password postgres --instance=fleet --prompt-for-password
```

At the prompt, paste the value from the vault (`Get-Secret -Vault fleet FLEET_CLOUDSQL_POSTGRES_PASSWORD -AsPlainText`).
Don't use `--password=`: gcloud records its command line in its logs.

## 6. One-time bootstrap: let the IAM user own the database (UNTESTED)

This must come before step 8. The broker runs its migrations at STARTUP as the IAM user; without the grant the
startup probe fails, the revision never becomes ready and step 8's apply errors. The cause is SQLSTATE 42501
("permission denied") in the revision's logs, not a 503.

As `postgres`, through the proxy under your own login (you need `roles/cloudsql.client`; an Owner has it). Port
5433, so it can't reach a local Docker postgres on 5432:

```powershell
$conn = tf output -raw instance_connection_name
$u    = tf output -raw iam_database_user
cloud-sql-proxy $conn --port 5433          # leave running; use a second terminal for psql
psql -h 127.0.0.1 -p 5433 -U postgres -d fleet      # asks for the password (vault, as in step 5)
```

```sql
GRANT "<iam user>" TO postgres;
ALTER DATABASE fleet OWNER TO "<iam user>";
-- fallback if the migrations still say "permission denied for schema public":
GRANT ALL ON SCHEMA public TO "<iam user>";
-- optional afterwards, so postgres isn't a member of the broker's role:
REVOKE "<iam user>" FROM postgres;
```

UNTESTED: written from the docs.

## 7. Build and push the image (a unique tag per build)

Tags are immutable and `:latest` is never used: the tag is the short git SHA.

```powershell
gcloud auth configure-docker us-central1-docker.pkg.dev
$repo = tf output -raw registry_path
$sha  = git rev-parse --short HEAD
docker build --platform linux/amd64 -t "$repo/broker:$sha" broker     # Cloud Run is amd64
docker push "$repo/broker:$sha"
docker buildx imagetools inspect "$repo/broker:$sha" | Select-String Digest     # sha256:...
```

## 8. Second apply, by digest

```powershell
$env:TF_VAR_broker_image = "$repo/broker@sha256:<digest from step 7>"
tf plan "-out=tfplan"; tf apply tfplan
```

(Not a secret, so it can live in the shell. A tag is refused by the variable's validation.)

## 9. Smoke test

The token goes through curl's variables, not its command line (`--variable` and `--expand-header` need curl 8.3+;
this PC has curl 8.19.0 as `curl.exe`, so they work; check yours with `curl.exe --version`).

```powershell
$url = tf output -raw service_url
curl.exe -i "$url/healthz"                                   # 204 (the first call is a cold start: a few seconds)
$env:URL = $url
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUD_WORKER_TOKEN -- pwsh -NoProfile -Command 'curl.exe -i -X POST --variable %FLEET_CLOUD_WORKER_TOKEN --expand-header "Authorization: Bearer {{FLEET_CLOUD_WORKER_TOKEN}}" -H "Content-Type: application/json" -d "{\"capabilities\":[\"smoke\"],\"models\":[\"smoke\"]}" "$env:URL/lease"'
```

With an empty queue (or nothing matching) the answer is `204`. A `401` means the secret version has a stray newline
(step 4); a revision that never started means the grant (step 6).

## Rotating the broker's tokens

Steps follow docs/secrets.md; the Cloud Run parts:

1. Add the current values as versions of `fleet-worker-token-previous` and `fleet-admin-token-previous`. Those secrets
   exist only while `rotation_in_progress` is true, so set `TF_VAR_rotation_in_progress=true` and apply first (the
   revision then needs a version in each; do the apply and the `gcloud secrets versions add` before the redeploy).
2. Add the NEW tokens as new versions of `fleet-worker-token` and `fleet-admin-token` (step 4 commands).
3. Redeploy so a new revision reads `latest` (`gcloud run services update fleet-broker --region us-central1`).
   Both old and new tokens now work. Switch the workers.
4. Set `rotation_in_progress` back to false and apply: the previous secrets, their access and the env refs go away.

## 10. Teardown

Deletion protection is on at three levels. Set `deletion_protection = false` on the instance AND
`deletion_protection_enabled = false` in its `settings`, and `deletion_protection = false` on the Cloud Run
service; `terraform apply`; then `terraform destroy`. The APIs are left enabled (`disable_on_destroy = false`).
Stopping the instance instead (`gcloud sql instances patch fleet --activation-policy=NEVER`) stops compute charges
but keeps the disk (about $1.70/month).

The console budget is not in this stack; it is imported later (as with AWS). The budget kill switch is a later PR.
