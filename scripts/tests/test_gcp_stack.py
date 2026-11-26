"""Text checks on the Google Cloud root module (ADR 0021): the settings that keep the database and the
public broker safe. No cloud call, so a regression fails in CI before anyone runs a plan. Comments are
stripped first, so a comment that mentions a setting cannot satisfy a check."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "infra" / "gcp" / "fleet"


def code(name: str) -> str:
    text = (STACK / name).read_text(encoding="utf-8")
    return re.sub(r"(?m)(^|\s)(#|//).*$", "", text)


def all_code() -> str:
    return "\n".join(code(p.name) for p in sorted(STACK.glob("*.tf")))


def block(name: str, header: str) -> str:
    """The text of a resource block, up to the next top-level resource."""
    text = code(name)
    start = text.index(header)
    rest = text[start + len(header) :]
    end = re.search(r"(?m)^(resource|data|variable|output)\b", rest)
    return rest[: end.start()] if end else rest


def sql() -> str:
    return block("cloudsql.tf", 'resource "google_sql_database_instance" "fleet"')


def test_connector_enforcement_required():
    assert re.search(r'connector_enforcement\s*=\s*"REQUIRED"', sql())


def test_no_authorized_networks():
    assert "authorized_networks" not in all_code()


def test_ssl_mode_encrypted_only():
    assert re.search(r'ssl_mode\s*=\s*"ENCRYPTED_ONLY"', sql())


def test_deletion_protection_on_instance_both_levels_and_service():
    s = sql()
    assert re.search(r"(?m)^\s*deletion_protection\s*=\s*true", s)
    assert re.search(r"deletion_protection_enabled\s*=\s*true", s)
    run = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    assert re.search(r"(?m)^\s*deletion_protection\s*=\s*true", run)


def test_max_instances_comes_from_a_variable_whose_default_is_1():
    # 1 until volunteers join (Q65, ADR 0021), then 3.
    run = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    assert re.search(r"max_instance_count\s*=\s*var\.max_instances\b", run)
    var = block("variables.tf", 'variable "max_instances"')
    assert re.search(r"default\s*=\s*1\b", var)
    assert "var.max_instances <= 6" in var


def test_images_are_pinned_by_digest():
    var = block("variables.tf", 'variable "broker_image"')
    assert "@sha256:[0-9a-f]{64}" in var and "validation" in var
    run = code("cloudrun.tf")
    proxy = re.search(r'image\s*=\s*"(gcr\.io/cloud-sql-connectors/cloud-sql-proxy[^"]*)"', run)
    assert proxy, "proxy image missing"
    assert re.fullmatch(r".+:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", proxy.group(1)), proxy.group(1)


def test_no_secret_version_and_no_root_password():
    text = all_code()
    assert "google_secret_manager_secret_version" not in text
    assert "root_password" not in text


def test_secret_accessor_is_bound_per_secret_not_per_project():
    text = all_code()
    assert "google_secret_manager_secret_iam_member" in text
    assert "google_project_iam_member" in text  # the Cloud SQL roles
    for m in re.finditer(r'resource "google_project_iam_\w+"[^{]*\{(.*?)\n\}', text, re.DOTALL):
        assert "secretmanager" not in m.group(1)
    assert "secretmanager.secretAccessor" in text
    assert text.count("google_secret_manager_secret_iam_member") >= 1


def test_resource_manager_api_is_enabled_and_iam_members_wait_for_the_apis():
    assert '"cloudresourcemanager.googleapis.com"' in code("main.tf")
    for name in ("sql_client", "sql_instance_user"):
        b = block("main.tf", f'resource "google_project_iam_member" "{name}"')
        assert "google_project_service.api" in b, name


def test_cloud_sql_roles_are_limited_to_this_instance_by_condition():
    for name in ("sql_client", "sql_instance_user"):
        b = block("main.tf", f'resource "google_project_iam_member" "{name}"')
        assert re.search(r"condition\s*\{", b), name
        assert "title" in b and "expression" in b, name
        plain = b.replace('\\"', '"')  # the expression is a quoted HCL string with escaped quotes
        assert (
            'resource.name == "projects/${var.project_id}/instances/${google_sql_database_instance.fleet.name}"'
            in plain
        ), name
        assert 'resource.type == "sqladmin.googleapis.com/Instance"' in plain, name


def test_registry_tags_are_immutable():
    b = block("main.tf", 'resource "google_artifact_registry_repository" "fleet"')
    assert re.search(r"docker_config\s*\{\s*immutable_tags\s*=\s*true", b)


def test_proxy_sidecar_is_small_and_shuts_down_cleanly():
    run = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    proxy = run[run.index('name = "cloud-sql-proxy"') :]
    # Cloud Run takes whole CPUs above 1, so 1 + 0.25 would likely be refused: every container has cpu "1".
    assert re.search(r'cpu\s*=\s*"1"', proxy)
    assert not re.search(r'cpu\s*=\s*"0\.\d+"', run)
    assert '"--max-sigterm-delay=10s"' in proxy
    assert '"--structured-logs"' in proxy
    assert '"--auto-iam-authn"' in proxy


def test_database_logs_connections_and_other_security_events():
    s = sql()
    for flag, value in [
        ("log_connections", "on"),
        ("log_disconnections", "on"),
        ("log_lock_waits", "on"),
        ("log_checkpoints", "on"),
        ("log_temp_files", "0"),
    ]:
        assert re.search(rf'name\s*=\s*"{flag}"\s*value\s*=\s*"{value}"', s), flag


def test_trivy_ignores_only_the_public_ip_finding():
    ignores = re.findall(r"trivy:ignore:(\S+)", (STACK / "cloudsql.tf").read_text(encoding="utf-8"))
    assert ignores == ["GCP-0017"]


def test_request_log_is_excluded_so_client_ips_are_not_stored():
    text = all_code()
    b = block("logging.tf", 'resource "google_logging_project_exclusion"')
    assert (
        'resource.type=\\"cloud_run_revision\\"' in b or 'resource.type="cloud_run_revision"' in b
    )
    plain = b.replace('\\"', '"')
    assert 'log_id("run.googleapis.com/requests")' in plain
    assert 'resource.labels.service_name="fleet-broker"' in plain
    assert "httpRequest.status < 500" in plain  # Cloud Run's own 5xx entries are kept
    assert "google_logging_project_exclusion" in text


def test_a_4xx_rate_alert_goes_to_an_email_from_a_sensitive_variable():
    b = block("monitoring.tf", 'resource "google_monitoring_alert_policy"')
    assert "run.googleapis.com/request_count" in b
    assert 'response_code_class = \\"4xx\\"' in b or 'response_code_class="4xx"' in b
    assert "ALIGN_RATE" in b
    assert re.search(r"threshold_value\s*=\s*5\b", b)
    assert re.search(r'duration\s*=\s*"300s"', b)
    ch = block("monitoring.tf", 'resource "google_monitoring_notification_channel"')
    assert "var.alert_email" in ch and '"email"' in ch
    var = block("variables.tf", 'variable "alert_email"')
    assert re.search(r"sensitive\s*=\s*true", var)
    assert not re.search(r"(?m)^\s*default\s*=", var)


def test_token_rotation_secrets_are_optional_and_per_secret():
    var = block("variables.tf", 'variable "rotation_in_progress"')
    assert re.search(r"default\s*=\s*false", var) and "bool" in var
    main = code("main.tf")
    assert '"fleet-worker-token-previous"' in main and '"fleet-admin-token-previous"' in main
    # the secrets always exist (empty); only the broker's env refs wait for the flag, so versions can come first
    assert "var.rotation_in_progress" not in main
    run = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    assert "FLEET_WORKER_TOKEN_PREVIOUS" in run and "FLEET_ADMIN_TOKEN_PREVIOUS" in run
    assert "var.rotation_in_progress" in run


def test_readme_runbook_keeps_secrets_off_command_lines():
    text = (STACK / "README.md").read_text(encoding="utf-8")
    assert "cmd /c" not in text and "Format-Hex" not in text
    fenced = "\n".join(
        re.findall(r"```.*?```", text, re.DOTALL)
    )  # commands only; prose may name the flag
    assert "--password=" not in fenced
    # Security review Low 3: no clipboard (Windows clipboard history and cloud sync keep it). The
    # password goes from the vault to the Cloud SQL Admin API through Python's environment, no argv.
    assert "Clipboard" not in text and "--prompt-for-password" not in text
    assert "sqladmin.googleapis.com" in fenced and "os.environ[" in fenced
    assert re.search(r"with-secrets\.ps1 -Names FLEET_CLOUDSQL_POSTGRES_PASSWORD", fenced)
    assert "UNVERIFIED" in text
    # Security review Low 4: the temp file of the length check is removed even if the check fails.
    assert re.search(r"try \{.*python -c.*\} finally \{\s*Remove-Item \$f", fenced, re.DOTALL)
    assert "bootstrap-cloudsql.ps1" in text and "cloud-sql-proxy $conn" not in text
    assert "set-quota-project" in text
    # A PowerShell pipe to gcloud stored the token plus "\r\n" (measured): Python hands it over on stdin.
    assert "| gcloud secrets versions add" not in fenced and "raw bytes" not in text
    assert "input=os.environ[sys.argv[1]].encode()" in fenced
    assert "--out-file=" in fenced  # the stored bytes are measured from a file, not stdout
    assert "/healthz" not in fenced  # reserved by Google's front end on run.app URLs
    assert '"$url/nope"' in fenced and "subprocess.run" in fenced
    assert "--expand-header" not in text and "curl.exe -i -X POST --variable" not in fenced
    assert '-d "@body.json"' in text and '\\"' not in fenced
    for line in text.splitlines():  # a plaintext secret is never printed to the console
        assert "-AsPlainText" not in line, line
    assert "secrets versions disable" in text and "secrets versions destroy" in text


def test_adr_0024_records_that_the_kill_switch_relies_on_cloud_run_iam():
    adr = next((ROOT / "docs" / "adr").glob("0024-*.md")).read_text(encoding="utf-8")
    assert "no JWT check" in adr or "does no JWT check" in adr
    assert "fail-safe" in adr and "Accepted" in adr


def test_bootstrap_script_uses_the_pinned_images_and_least_privilege():
    script = (ROOT / "scripts" / "bootstrap-cloudsql.ps1").read_text(encoding="utf-8")
    proxy = re.search(r"\$proxyImage\s*=\s*'([^']+)'", script).group(1)
    pg = re.search(r"\$pgImage\s*=\s*'([^']+)'", script).group(1)
    assert proxy in (STACK / "cloudrun.tf").read_text(encoding="utf-8")
    assert pg in (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "GRANT CONNECT, CREATE ON DATABASE fleet" in script
    assert "GRANT USAGE, CREATE ON SCHEMA public" in script
    assert not re.search(r"OWNER TO|GRANT \"\$user\" TO|ALL PRIVILEGES", script)
    assert ":ro" in script  # the login is mounted read-only
    assert not re.search(r"[A-Za-z]:[\\/]Users", script)  # no absolute user path


def test_alert_cost_is_recorded_as_unverified():
    assert "$1.50" in (STACK / "monitoring.tf").read_text(encoding="utf-8")
    adr = (ROOT / "docs/adr/0021-cloud-run-and-cloud-sql-deployment.md").read_text(encoding="utf-8")
    assert "$1.50/month per condition" in adr


def test_service_account_has_only_the_three_roles():
    roles = set(re.findall(r'"(roles/[\w.]+)"', all_code()))
    assert roles == {
        "roles/cloudsql.client",
        "roles/cloudsql.instanceUser",
        "roles/secretmanager.secretAccessor",
        "roles/run.invoker",
        # the kill switch (killswitch.tf; test_killswitch_stack.py checks their scope)
        "roles/pubsub.publisher",  # Cloud Billing, on the budget topic
        "roles/iam.serviceAccountTokenCreator",  # Pub/Sub's service agent, on the pusher account
    }


def test_tier_is_db_f1_micro():
    assert re.search(r'tier\s*=\s*"db-f1-micro"', sql())


def test_backend_key_is_the_gcp_stack():
    versions = code("versions.tf")
    assert re.search(r'key\s*=\s*"gcp/fleet/terraform\.tfstate"', versions)
    aws = (ROOT / "infra/aws/fleet/versions.tf").read_text(encoding="utf-8")
    bucket = re.search(r'bucket\s*=\s*"([^"]+)"', aws).group(1)
    assert f'"{bucket}"' in versions


def test_broker_is_cleared_for_open_data_only():
    run = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    assert re.search(r'name\s*=\s*"FLEET_WORKER_CLEARANCE"\s*value\s*=\s*"open"', run)


def test_no_tfvars_or_state_files_in_the_stack():
    names = [p.name for p in STACK.iterdir()]
    assert not [n for n in names if n.endswith((".tfvars", ".tfstate"))]
