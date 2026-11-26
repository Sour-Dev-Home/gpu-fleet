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
Set-Secret -Vault fleet -Name TF_VAR_billing_account   # prompts; the billing account ID (kill switch)
cd infra/gcp/fleet
../../../scripts/with-secrets.ps1 -Names TF_VAR_project_id -- pwsh -NoProfile -Command 'gcloud auth application-default set-quota-project $env:TF_VAR_project_id'
../../../scripts/with-secrets.ps1 -Names TF_VAR_project_id,TF_VAR_alert_email,TF_VAR_billing_account -- terraform init
```

(The quota project makes Application Default Credentials bill API calls to this project, not a Google default.)
Every Terraform command below runs as `../../../scripts/with-secrets.ps1 -Names TF_VAR_project_id,TF_VAR_alert_email,TF_VAR_billing_account -- terraform ...`,
shortened to `tf`. `gcloud` commands that need the ID run under `with-secrets` too, or use `gcloud config set project` once.

## 3. First apply (no image)

```powershell
tf plan "-out=tfplan"      # review: the infra-reviewer agent, then you (CLAUDE.md)
tf apply tfplan            # about 10 minutes: the Cloud SQL instance is the slow part
```

This makes everything except the Cloud Run service (`broker_image` is null).

## 4. The two broker tokens (new ones for the cloud)

Generate NEW tokens (not the local ones; docs/secrets.md) straight into the vault under cloud names, then add them as
secret versions. Python reads the variable itself and hands the exact bytes to gcloud on its stdin, so the token
appears on no command line. Do NOT pipe it from PowerShell (`python ... | gcloud ... --data-file=-`): that stored the
token plus "\r\n" (46 bytes, measured), even on PowerShell 7.6.

```powershell
foreach ($n in 'FLEET_CLOUD_WORKER_TOKEN','FLEET_CLOUD_ADMIN_TOKEN') {
  Set-Secret -Vault fleet -Name $n -Secret ([Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)))
}
$add = "import os,shutil,subprocess,sys; subprocess.run([shutil.which('gcloud'),'secrets','versions','add',sys.argv[2],'--data-file=-'],input=os.environ[sys.argv[1]].encode(),check=True)"
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUD_WORKER_TOKEN -- python -c $add FLEET_CLOUD_WORKER_TOKEN fleet-worker-token
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUD_ADMIN_TOKEN -- python -c $add FLEET_CLOUD_ADMIN_TOKEN fleet-admin-token
```

Check the STORED bytes, never the value. Printing to stdout adds its own newline, so write to a file, measure it and
delete it (a base64 token of 32 bytes is 44 bytes, with no whitespace at either end). Expect `44 True`:

```powershell
$f = Join-Path $env:TEMP "fleet-secret-check"
try {
  gcloud secrets versions access latest --secret=fleet-worker-token --out-file=$f
  python -c "import sys;b=open(sys.argv[1],'rb').read();print(len(b),b==b.strip())" $f
} finally {
  Remove-Item $f -ErrorAction SilentlyContinue   # always, even if gcloud or python failed
}
```

A different number (45 or 46) means a stray newline got in, and the broker would reject its own workers.
`version = "latest"` is read when an instance starts: after adding a version, a new revision is needed
(change nothing but redeploy: `gcloud run services update fleet-broker --region us-central1`).

## 5. The `postgres` password (never in Terraform, never on a command line)

```powershell
Set-Secret -Vault fleet -Name FLEET_CLOUDSQL_POSTGRES_PASSWORD -Secret ([Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)))
$set = "import json,os,subprocess,shutil,urllib.request as u; g=shutil.which('gcloud'); p=subprocess.check_output([g,'config','get-value','project'],text=True).strip(); t=subprocess.check_output([g,'auth','print-access-token'],text=True).strip(); r=u.Request(f'https://sqladmin.googleapis.com/v1/projects/{p}/instances/fleet/users?name=postgres',data=json.dumps({'password':os.environ['FLEET_CLOUDSQL_POSTGRES_PASSWORD']}).encode(),method='PUT',headers={'Authorization':'Bearer '+t,'Content-Type':'application/json'}); print(u.urlopen(r,timeout=60).status)"
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUDSQL_POSTGRES_PASSWORD -- python -c $set
```

The value goes from the vault into Python's environment and from there into the Cloud SQL Admin API request body
(`users.update`): never the clipboard (Windows clipboard history and cloud sync keep it), never a command line
(`gcloud sql users set-password` has only `--password=` and an interactive prompt, no file or stdin option, and gcloud records its command
line in its logs). **UNVERIFIED:** this call is written against the API reference and has not been run; expect
`200` (or `204`), and check the first run before relying on it. The access token is short-lived and is in argv of
nothing: it is read from `gcloud auth print-access-token` inside Python.

## 6. One-time bootstrap: let the IAM user create tables

This must come before step 8. The broker runs its migrations at STARTUP as the IAM user; without the grant the
startup probe fails, the revision never becomes ready and step 8's apply errors. The cause is SQLSTATE 42501
("permission denied") in the revision's logs, not a 503.

`scripts/bootstrap-cloudsql.ps1` does it all in Docker: it runs the pinned Auth Proxy and `psql` images on a private
Docker network (nothing is published on the host, so it can't touch a local postgres), with your own login
(application default credentials) mounted read-only, and the postgres password only as an environment variable from
the vault. It needs `roles/cloudsql.client` (an Owner has it). Least privilege, no ownership transfer and no role
membership (on PostgreSQL 16+ granting the IAM role to `postgres` may be refused):
`GRANT CONNECT, CREATE ON DATABASE fleet` and `GRANT USAGE, CREATE ON SCHEMA public` to the IAM user.

```powershell
$env:AWS_PROFILE = "fleet"      # the script calls terraform output
../../../scripts/with-secrets.ps1 -Names FLEET_CLOUDSQL_POSTGRES_PASSWORD -- pwsh -NoProfile -File ../../../scripts/bootstrap-cloudsql.ps1
```

Expected output (the second line is `max_connections`; 25 on `db-f1-micro`):

```
 result
---------
 granted
 max_connections
-----------------
 25
```

(psql's table layout may differ slightly.) Both image references in the script are checked against the pins in
`cloudrun.tf` and `compose.yaml` by `scripts/tests/test_gcp_stack.py`.

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

Old tagged images are never deleted automatically. When the registry nears 0.5 GB, delete old ones by hand
(`gcloud artifacts docker images delete`), never the deployed digest.

## 8. Second apply, by digest

```powershell
$env:TF_VAR_broker_image = "$repo/broker@sha256:<digest from step 7>"
tf plan "-out=tfplan"; tf apply tfplan
```

(Not a secret, so it can live in the shell. A tag is refused by the variable's validation.)

## 9. Smoke test

Don't use `/healthz` from outside: Google's front end reserves it on run.app URLs and answers with its own HTML 404
(measured); startup probes bypass it. Three checks instead (the first call is a cold start: a few seconds):

```powershell
$url = tf output -raw service_url
Set-Content body.json '{"capabilities":["smoke"],"models":["smoke"]}'     # no secret in it
curl.exe -i "$url/nope"                                                    # 404 {"detail":"Not Found"}: the broker answered
curl.exe -i -X POST -H "Content-Type: application/json" -d "@body.json" "$url/lease"      # 401: no token
Remove-Item body.json
```

Then `/lease` with the worker token. Python reads the token from Secret Manager itself, so it is never on a command
line; expect `204` (an empty queue, or nothing matching):

```powershell
python -c "import http.client,json,shutil,subprocess,sys,urllib.parse as p; t=subprocess.run([shutil.which('gcloud'),'secrets','versions','access','latest','--secret=fleet-worker-token'],capture_output=True,text=True,check=True).stdout.strip(); c=http.client.HTTPSConnection(p.urlparse(sys.argv[1]).netloc); c.request('POST','/lease',json.dumps({'capabilities':['smoke'],'models':['smoke']}),{'Authorization':'Bearer '+t,'Content-Type':'application/json'}); print(c.getresponse().status)" $url
```

The unknown-path 404 as JSON proves the request reached the container; a revision that never started means the grant
(step 6); a `401` with the token means the stored version has stray bytes (step 4's check).

## Rotating the broker's tokens

Steps follow docs/secrets.md; the Cloud Run parts:

The `-previous` secrets always exist, empty. A revision that references one with no version can't start, so versions
come first, then the flag.
1. Copy the current values into versions of `fleet-worker-token-previous` and `fleet-admin-token-previous` (the step 4
   commands, reading the current token from the vault, e.g. a `..._PREVIOUS` vault entry as in docs/secrets.md).
2. Add the NEW tokens as new versions of `fleet-worker-token` and `fleet-admin-token` (step 4 commands).
3. Apply with `TF_VAR_rotation_in_progress=true`: a new revision passes the previous tokens too and reads `latest`.
   Both old and new tokens now work. Switch the workers.
4. Set `rotation_in_progress` back to false and apply (the env refs go). Then disable and destroy superseded versions
   (`gcloud secrets versions disable N --secret=...`, then `gcloud secrets versions destroy N --secret=...`): the free tier is 6 active versions.

## Kill switch (ADR 0024)

At 100% of the $20 monthly budget (`budget_usd`) a private Cloud Run service, pushed by Pub/Sub, removes `allUsers`
from the broker's invoker role: callers get Google's 403. Budget data lags by hours, so this caps the damage, not
the first dollar. It needs `TF_VAR_billing_account` (the billing account ID, in the vault: `Set-Secret -Vault fleet
-Name TF_VAR_billing_account`) alongside the other variables, and the deploying user needs Billing Account
Administrator (or Costs Manager) on the billing account, plus Pub/Sub Admin. The google provider here sets
`user_project_override` and `billing_project` so the budget API call is billed to the project.

**Before trusting $20**: open the billing report for this project with credits excluded and confirm normal running
is about $10-11 gross (the budget counts gross spend, free-tier usage included). `currency_code = "USD"` must
match the billing account's currency.

**Build, push, apply** (the same as steps 7 and 8, image name `killswitch`, tag = short SHA). The first apply with
`killswitch_image` null already creates the budget, topic and accounts; the service and subscription come with the image:

```powershell
$sha  = git rev-parse --short HEAD
docker build --platform linux/amd64 -t "$repo/killswitch:$sha" killswitch
docker push "$repo/killswitch:$sha"
docker buildx imagetools inspect "$repo/killswitch:$sha" | Select-String Digest     # sha256:...
$env:TF_VAR_killswitch_image = "$repo/killswitch@sha256:<digest>"
tf plan "-out=tfplan"; tf apply tfplan
```

(UNVERIFIED until the first apply: whether Cloud Billing's publisher account is
`billing-budget-alert@system.gserviceaccount.com` and whether the Pub/Sub service agent needs the token-creator
grant. If the apply refuses a member, check Cloud Billing's budget-notification docs. The first real notification can
take hours: after the drill, check the killswitch logs again once the budget has sent one.)

**How to tell it fired**
- `curl.exe -i "$url/lease"` answers `403` with Google's HTML page (not the broker's JSON 401).
- The killswitch logs (`gcloud run services logs read fleet-killswitch --region us-central1`) show
  `budget reached: public access removed` with the cost and budget.
- `tf plan` shows `google_cloud_run_v2_service_iam_member.public` "to add". That is the switch having fired, and
  the `broker_is_public` check warns too. If you did not intend a reset: stop and investigate first, because any
  apply (even an unrelated one) restores public access.
- An email "fleet kill switch changed the broker's IAM policy" arrives for every SetIamPolicy by the switch.

**Reset**: fix the cause (a flood? check the broker's logs and the bill), raise `budget_usd` if the spend was
legitimate, then `tf apply`: the plan re-adds the `allUsers` binding. If spend is still at the budget, the next
notification (several a day) trips it again.

**Drill** (do this after the first deploy, and after any change to the switch). It makes the broker UNREACHABLE for
a minute or so, so do it when nobody depends on it:

```powershell
gcloud pubsub topics publish fleet-budget --message='{"costAmount": 21, "budgetAmount": 20}'
# wait up to a minute, then:
curl.exe -i "$url/nope"        # a GET: expect 403 (Google's page); the killswitch logs say "public access removed"
                               # (not a bodyless POST: Google answers 411 before any IAM check)
tf plan                        # expect the public binding "to add"
tf apply                       # restores it; the broker answers 404 JSON on /nope again
```

This proves the push, the OIDC token and the IAM edit, not that Cloud Billing can publish to the topic: only a real
notification does (the budget's topic subscription shows deliveries in the console).

**After the apply that makes the kill switch's ingress internal-only, re-run the drill** (above): Pub/Sub push to an
internal-only service in the same project is documented as allowed but UNVERIFIED here. If the push is refused,
revert `ingress` to `INGRESS_TRAFFIC_ALL` (the invoker binding is still the gate).

## S3 mode (ADR 0025)

The broker leases presigned S3 URLs and signs them with AWS credentials from workload identity federation
(no stored key). The four settings (`FLEET_RESULTS_BUCKET`, `FLEET_AWS_ROLE_ARN`, `FLEET_AWS_AUDIENCE`,
`AWS_REGION`) are set together from `s3_mode` (default true); the bucket and role are read from the AWS stack's
remote state, so this stack needs the AWS login too. The audience is `aws_federation_audience` and must equal the
AWS stack's `federation_audience` (a test compares the defaults).

**Apply order**
1. The AWS stack first (`infra/aws/fleet`; done).
2. Build and push a new broker image from `main` (steps 7: it now includes boto3), and note its digest.
3. This stack: `$env:TF_VAR_broker_image = "$repo/broker@sha256:<digest>"`, then `tf plan` / `tf apply` as in step 8.

**First-federation check** (broker logs: `gcloud run services logs read fleet-broker --region us-central1`)
- A revision that starts is a good sign (partial config is a startup error), but credentials are fetched lazily.
- Enqueue an open job and lease it with the worker token: the response must carry `input_url`, an https URL on
  the bucket's host.
- A failure shows as `AccessDenied` on `AssumeRoleWithWebIdentity` in the logs and a 5xx on `/lease`: the role's
  trust policy disagrees with the token (audience, or the service account's unique ID). Check the audience first.

**Rollback**: `tf apply -var s3_mode=false` removes all four settings together (and skips the AWS state read). The
broker is back to its previous mode; workers must not set `FLEET_RESULTS_HOST` against it.

**Workers** need `FLEET_RESULTS_HOST` = the bucket's virtual-hosted host, `<bucket>.s3.amazonaws.com` in us-east-1:

```powershell
$bucket = terraform -chdir=infra/aws/fleet output -raw results_bucket
$env:FLEET_RESULTS_HOST = "$bucket.s3.amazonaws.com"
```

## 10. Teardown

Deletion protection is on at three levels. Set `deletion_protection = false` on the instance AND
`deletion_protection_enabled = false` in its `settings`, and `deletion_protection = false` on the Cloud Run
service; `terraform apply`; then `terraform destroy`. The APIs are left enabled (`disable_on_destroy = false`).
Stopping the instance instead (`gcloud sql instances patch fleet --activation-policy=NEVER`) stops compute charges
but keeps the disk (about $1.70/month).

The existing console budget (if any) is not in this stack; it is imported later (as with AWS). The kill switch's own
budget is (see "Kill switch").
