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


def test_max_instances_comes_from_a_variable_whose_default_is_3():
    run = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    assert re.search(r"max_instance_count\s*=\s*var\.max_instances\b", run)
    var = block("variables.tf", 'variable "max_instances"')
    assert re.search(r"default\s*=\s*3\b", var)


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


def test_service_account_has_only_the_three_roles():
    roles = set(re.findall(r'"(roles/[\w.]+)"', all_code()))
    assert roles == {
        "roles/cloudsql.client",
        "roles/cloudsql.instanceUser",
        "roles/secretmanager.secretAccessor",
        "roles/run.invoker",
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
